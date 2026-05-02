"""
JobManager — wrapper alrededor de APScheduler con retry, histórico y overrides.

Capacidades:
  - register_job(id, func, trigger, ...) — registra con retry + max_instances=1
    + coalesce=True + misfire_grace_time=300 por default.
  - Retry automático: 3 intentos con backoff 10s/30s/90s ante excepciones.
  - Histórico persistente: data/scheduler_history.json con últimas 100 corridas
    por job (rolling). Guarda inicio, fin, duración, status, retries, error.
  - Overrides: config/scheduler_overrides.json — el usuario puede pausar jobs
    sin tocar código. Si está pausado, el wrapper saltea sin ejecutar.
  - Skipped tracking: cuando max_instances bloquea una corrida, queda registrado
    como entrada con status='skipped'.

Diseñado para usarse desde web/app.py:
  jm = JobManager(scheduler, data_dir=DATA_DIR, config_dir=CONFIG_DIR)
  jm.register_job('daily_update', _scheduler_run_all, CronTrigger(hour=7))
  jm.register_job('repricing_hourly', _job_repricing, CronTrigger(hour='8-22'))
  ...
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable

_logger = logging.getLogger(__name__)

# Defaults APScheduler para todos los jobs registrados con JobManager
_DEFAULT_JOB_KWARGS = {
    'max_instances':       1,
    'coalesce':            True,
    'misfire_grace_time':  300,   # 5 min de tolerancia tras la hora
    'replace_existing':    True,
}

# Retry policy: 3 reintentos con backoff exponencial
_RETRY_BACKOFFS = [10, 30, 90]   # segundos entre intentos
_MAX_HISTORY_PER_JOB = 100        # rolling

_history_lock = threading.Lock()
_overrides_lock = threading.Lock()


class JobManager:
    """Wrapper alrededor de APScheduler con retry, histórico y overrides."""

    def __init__(self, scheduler, data_dir: str, config_dir: str):
        self.scheduler   = scheduler
        self.data_dir    = data_dir
        self.config_dir  = config_dir
        self._registered: dict[str, dict] = {}   # id → metadata (func original, trigger desc, ...)

    # ── API pública ──────────────────────────────────────────────────────────

    def register_job(self, job_id: str, func: Callable, trigger,
                     name: str = '', description: str = '',
                     reserved: bool = False) -> None:
        """Registra un job con retry y record_run automáticos.

        Si reserved=True, NO se agrega al scheduler real — solo se reserva
        el slot para que aparezca en la UI con estado "Reservado". Útil para
        jobs que están planificados pero no se quiere activar todavía.
        """
        self._registered[job_id] = {
            'func':         func,
            'name':         name or job_id,
            'description':  description,
            'trigger_desc': self._describe_trigger(trigger),
            'reserved':     reserved,
        }

        if reserved:
            _logger.info("[scheduler] Job '%s' RESERVADO (no se ejecuta hasta activación manual).", job_id)
            return

        wrapped = self._wrap_with_retry(job_id, func)
        self.scheduler.add_job(
            wrapped,
            trigger=trigger,
            id=job_id,
            name=name or job_id,
            **_DEFAULT_JOB_KWARGS,
        )

        # Listener para detectar misfire / skipped por max_instances
        self.scheduler.add_listener(
            lambda ev: self._on_event(job_id, ev),
            mask=self._event_mask(),
        )

    def run_now(self, job_id: str) -> bool:
        """Dispara un job manualmente, sin esperar al cron.

        Funciona también para jobs reservados (no necesita estar en el scheduler).
        """
        meta = self._registered.get(job_id)
        if not meta:
            return False

        wrapped = self._wrap_with_retry(job_id, meta['func'])
        threading.Thread(target=wrapped, daemon=True).start()
        return True

    def is_paused(self, job_id: str) -> bool:
        return _load_overrides(self.config_dir).get(job_id, False)

    def pause_job(self, job_id: str) -> None:
        with _overrides_lock:
            ovs = _load_overrides(self.config_dir)
            ovs[job_id] = True
            _save_overrides(self.config_dir, ovs)

    def resume_job(self, job_id: str) -> None:
        with _overrides_lock:
            ovs = _load_overrides(self.config_dir)
            ovs[job_id] = False
            _save_overrides(self.config_dir, ovs)

    def list_jobs(self) -> list[dict]:
        """Lista de jobs registrados con metadata + estado.

        Returns: list[dict] con id, name, description, trigger_desc, reserved,
                 paused, next_run, last_run, last_status, last_duration_sec.
        """
        history = _load_history(self.data_dir)
        overrides = _load_overrides(self.config_dir)

        result: list[dict] = []
        for jid, meta in self._registered.items():
            entry = {
                'id':           jid,
                'name':         meta['name'],
                'description':  meta['description'],
                'trigger_desc': meta['trigger_desc'],
                'reserved':     meta['reserved'],
                'paused':       overrides.get(jid, False),
                'next_run':     None,
                'last_run':     None,
                'last_status':  None,
                'last_duration_sec': None,
            }

            # Next run desde APScheduler (solo si no es reservado)
            if not meta['reserved']:
                try:
                    job = self.scheduler.get_job(jid)
                    if job and job.next_run_time:
                        entry['next_run'] = job.next_run_time.strftime('%Y-%m-%d %H:%M (%Z)')
                except Exception:
                    pass

            # Last run desde histórico
            runs = history.get(jid, [])
            if runs:
                last = runs[-1]
                entry['last_run']     = last.get('started_at')
                entry['last_status']  = last.get('status')
                entry['last_duration_sec'] = last.get('duration_sec')

            result.append(entry)
        return result

    def get_history(self, job_id: str, limit: int = 20) -> list[dict]:
        """Devuelve últimas N corridas del job (más reciente primero)."""
        runs = _load_history(self.data_dir).get(job_id, [])
        return list(reversed(runs[-limit:]))

    # ── Internos ─────────────────────────────────────────────────────────────

    def _wrap_with_retry(self, job_id: str, func: Callable) -> Callable:
        """Devuelve una función wrapeada que maneja retry, pause y record_run."""
        data_dir = self.data_dir
        config_dir = self.config_dir

        def wrapped():
            if _load_overrides(config_dir).get(job_id, False):
                _logger.info("[scheduler] Job '%s' pausado por override — saltando.", job_id)
                return

            started = datetime.now()
            last_error: Exception | None = None

            for attempt in range(len(_RETRY_BACKOFFS) + 1):  # 1 + N retries
                try:
                    func()
                    _record_run(data_dir, job_id, started, datetime.now(),
                                status='ok', retries=attempt, error=None)
                    return
                except Exception as e:
                    last_error = e
                    _logger.warning("[scheduler] Job '%s' falló intento %d: %s",
                                    job_id, attempt + 1, e)
                    if attempt < len(_RETRY_BACKOFFS):
                        time.sleep(_RETRY_BACKOFFS[attempt])

            _record_run(data_dir, job_id, started, datetime.now(),
                        status='error', retries=len(_RETRY_BACKOFFS),
                        error=str(last_error)[:500] if last_error else 'desconocido')

        wrapped.__name__ = f'wrapped_{job_id}'
        return wrapped

    @staticmethod
    def _describe_trigger(trigger) -> str:
        try:
            return str(trigger)
        except Exception:
            return repr(trigger)

    @staticmethod
    def _event_mask():
        try:
            from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
            return EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED
        except Exception:
            return 0

    def _on_event(self, job_id: str, event) -> None:
        """Listener para skipped/missed."""
        try:
            ev_job_id = getattr(event, 'job_id', None)
            if ev_job_id != job_id:
                return
            from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
            now = datetime.now()
            if event.code == EVENT_JOB_MAX_INSTANCES:
                _record_run(self.data_dir, job_id, now, now,
                            status='skipped', retries=0,
                            error='max_instances reached (corrida anterior aún en curso)')
            elif event.code == EVENT_JOB_MISSED:
                _record_run(self.data_dir, job_id, now, now,
                            status='missed', retries=0,
                            error='misfire — el cron pasó mientras el proceso estaba caído')
        except Exception:
            pass


# ── Persistencia: histórico ──────────────────────────────────────────────────

def _history_path(data_dir: str) -> str:
    return os.path.join(data_dir, 'scheduler_history.json')


def _load_history(data_dir: str) -> dict:
    path = _history_path(data_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _record_run(data_dir: str, job_id: str,
                started: datetime, finished: datetime,
                status: str, retries: int, error: str | None) -> None:
    """Append a la entrada del job, rolling de 100."""
    duration = (finished - started).total_seconds()
    entry = {
        'started_at':   started.strftime('%Y-%m-%d %H:%M:%S'),
        'finished_at':  finished.strftime('%Y-%m-%d %H:%M:%S'),
        'duration_sec': round(duration, 1),
        'status':       status,    # 'ok' | 'error' | 'skipped' | 'missed'
        'retries':      retries,
        'error':        error,
    }

    with _history_lock:
        history = _load_history(data_dir)
        runs = deque(history.get(job_id, []), maxlen=_MAX_HISTORY_PER_JOB)
        runs.append(entry)
        history[job_id] = list(runs)
        try:
            os.makedirs(data_dir, exist_ok=True)
            with open(_history_path(data_dir), 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _logger.error("[scheduler] No se pudo persistir historial: %s", e)


# ── Persistencia: overrides ──────────────────────────────────────────────────

def _overrides_path(config_dir: str) -> str:
    return os.path.join(config_dir, 'scheduler_overrides.json')


def _load_overrides(config_dir: str) -> dict:
    path = _overrides_path(config_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_overrides(config_dir: str, ovs: dict) -> None:
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(_overrides_path(config_dir), 'w', encoding='utf-8') as f:
            json.dump(ovs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _logger.error("[scheduler] No se pudo persistir overrides: %s", e)
