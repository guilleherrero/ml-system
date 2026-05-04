"""
Sprint 4.4 — Estado y persistencia de alertas del Centro de Alertas.

NO contiene la lógica de detección de alertas (eso vive en web/app.py:api_alertas()).
Solo gestiona el estado por fingerprint:
  - pendiente | resuelta | pospuesta | descartada | auto_resuelta

Reutiliza patrones de top_acciones_diarias.py:
  - fingerprint sha1(tipo + key_data)
  - persistencia via core.db_storage (filesystem local + PostgreSQL en Render)
  - TTL para descartada (30 días)
  - Snapshot opcional al cambiar estado (para análisis posterior)
"""

import hashlib
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

_logger = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")

# TTL para alertas descartadas: si la condición sigue activa después de X días,
# la alerta vuelve a aparecer (con flag "Reapareció" si está dentro de las 24h)
DESCARTE_TTL_DAYS = 30

# Ventana en horas para mostrar resueltas/auto-resueltas en la lista principal
# antes de que se vayan al tab "Resueltas (último mes)"
RECIEN_RESUELTA_HORAS = 24


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class EstadoAlerta:
    fingerprint: str
    tipo: str
    estado: str  # pendiente | resuelta | pospuesta | descartada | auto_resuelta
    creada_en: str
    actualizada_en: str
    pospuesta_hasta: str | None = None
    razon: str | None = None
    snapshot: dict = field(default_factory=dict)
    # Para detectar el caso "estaba descartada hace >30d, volvió a aparecer".
    # Se marca cuando una descartada expira y la condición sigue.
    reaparicion_en: str | None = None


# ── Helpers de I/O ────────────────────────────────────────────────────────────

def _safe(alias: str) -> str:
    return alias.replace(" ", "_").replace("/", "-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _estados_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f"alertas_estado_{_safe(alias)}.json")


def _load_json(path: str, default=None):
    try:
        from core.db_storage import db_load
        result = db_load(path)
        return result if result is not None else default
    except Exception as e:
        _logger.warning("[alertas_estado] failed loading %s: %s", path, e)
        return default


def _save_json(path: str, data) -> None:
    try:
        from core.db_storage import db_save
        db_save(path, data)
    except Exception as e:
        _logger.error("[alertas_estado] failed saving %s: %s", path, e)


# ── Fingerprint ───────────────────────────────────────────────────────────────

def fingerprint(tipo: str, *parts) -> str:
    """Hash idempotente de tipo + datos clave. Mismo tipo + mismos parts = mismo fp.
    Ejemplo: fingerprint('stock_critico', alias, item_id) → '4f3a...'"""
    raw = tipo + ":" + ":".join(str(p) for p in parts if p is not None)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ── API pública ───────────────────────────────────────────────────────────────

def cargar_estados(alias: str) -> dict[str, EstadoAlerta]:
    """Carga todos los estados persistidos para una cuenta. Devuelve dict indexado
    por fingerprint. Limpia automáticamente las descartadas expiradas (TTL 30d)."""
    raw = _load_json(_estados_path(alias), default={"items": []}) or {"items": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=DESCARTE_TTL_DAYS)
    estados: dict[str, EstadoAlerta] = {}
    cambio = False
    for d in raw.get("items", []):
        try:
            est = EstadoAlerta(**d)
        except TypeError:
            # Esquema viejo o corrupto — skip
            cambio = True
            continue
        if est.estado == "descartada":
            ts = _parse_iso(est.actualizada_en)
            if ts and ts < cutoff:
                # TTL expirado — limpiamos del estado.
                # La alerta volverá a aparecer pendiente y se marcará "reaparicion"
                # en `marcar_reaparicion_si_corresponde()`.
                cambio = True
                continue
        estados[est.fingerprint] = est
    if cambio:
        _persistir(alias, list(estados.values()))
    return estados


def _persistir(alias: str, lista: list[EstadoAlerta]) -> None:
    _save_json(_estados_path(alias), {"items": [asdict(e) for e in lista]})


def marcar(alias: str, fingerprint_str: str, tipo: str, estado: str,
           snapshot: dict | None = None, razon: str | None = None,
           pospuesta_hasta: str | None = None) -> EstadoAlerta:
    """Crea o actualiza el estado de una alerta. Función única para mutar."""
    estados = cargar_estados(alias)
    ahora = _now_iso()
    if fingerprint_str in estados:
        est = estados[fingerprint_str]
        est.estado = estado
        est.actualizada_en = ahora
        if razon is not None:
            est.razon = razon
        if snapshot is not None:
            est.snapshot = snapshot
        est.pospuesta_hasta = pospuesta_hasta
    else:
        est = EstadoAlerta(
            fingerprint=fingerprint_str,
            tipo=tipo,
            estado=estado,
            creada_en=ahora,
            actualizada_en=ahora,
            pospuesta_hasta=pospuesta_hasta,
            razon=razon,
            snapshot=snapshot or {},
        )
        estados[fingerprint_str] = est
    _persistir(alias, list(estados.values()))
    _logger.info("[alertas_estado] %s for %s → %s", fingerprint_str, alias, estado)
    return est


def marcar_resuelta(alias: str, fp: str, tipo: str = "",
                    razon: str = "", snapshot: dict | None = None) -> EstadoAlerta:
    return marcar(alias, fp, tipo, "resuelta", snapshot=snapshot, razon=razon)


def marcar_descartada(alias: str, fp: str, tipo: str = "",
                      razon: str = "", snapshot: dict | None = None) -> EstadoAlerta:
    return marcar(alias, fp, tipo, "descartada", snapshot=snapshot, razon=razon)


def marcar_pospuesta(alias: str, fp: str, horas: int, tipo: str = "",
                     razon: str = "", snapshot: dict | None = None) -> EstadoAlerta:
    if horas not in (4, 24, 168):  # 4h, 24h, 7d
        horas = 24
    hasta = (datetime.now(timezone.utc) + timedelta(hours=horas)).isoformat(timespec="seconds")
    return marcar(alias, fp, tipo, "pospuesta", snapshot=snapshot, razon=razon,
                  pospuesta_hasta=hasta)


def auto_resolver_ausentes(alias: str, fingerprints_actuales: set[str]) -> list[str]:
    """Diff: alertas que estaban `pendiente` (o `pospuesta` cuyo timer expiró)
    y ya no aparecen en la nueva detección → marcar `auto_resuelta`.

    Devuelve la lista de fingerprints auto-resueltos en este pase."""
    estados = cargar_estados(alias)
    ahora_iso = _now_iso()
    nuevas_auto = []
    cambio = False
    for fp, est in estados.items():
        if est.estado not in ("pendiente", "pospuesta"):
            continue
        if fp in fingerprints_actuales:
            continue
        # No aparece y estaba pendiente/pospuesta → la condición desapareció
        est.estado = "auto_resuelta"
        est.actualizada_en = ahora_iso
        est.razon = "Condición que disparó la alerta ya no aplica"
        nuevas_auto.append(fp)
        cambio = True
    if cambio:
        _persistir(alias, list(estados.values()))
        _logger.info("[alertas_estado] %s: %d alertas auto-resueltas",
                     alias, len(nuevas_auto))
    return nuevas_auto


def marcar_reaparicion_si_corresponde(alias: str, fingerprint_str: str) -> bool:
    """Si una alerta había sido descartada hace >30d (ya expiró del estado) y
    ahora vuelve a aparecer, no hay forma de saberlo desde el estado actual
    (ya no está en disco). Esta función se llama desde el código de detección
    cuando un fingerprint pendiente reaparece para registrar la reaparición."""
    # Marca el campo `reaparicion_en` en el estado nuevo si existe.
    estados = cargar_estados(alias)
    if fingerprint_str in estados:
        est = estados[fingerprint_str]
        if est.estado == "pendiente" and not est.reaparicion_en:
            est.reaparicion_en = _now_iso()
            _persistir(alias, list(estados.values()))
            return True
    return False


def estado_efectivo(alias: str, fingerprint_str: str) -> dict:
    """Devuelve el estado efectivo de una alerta para mergear en `/api/alertas`.

    Returns dict con:
      - estado: 'pendiente' por default
      - es_pospuesta_vigente: True si estado='pospuesta' y pospuesta_hasta>now
      - es_descartada_vigente: True si estado='descartada' (TTL ya filtrado en cargar_estados)
      - es_recien_resuelta: True si estado in ('resuelta','auto_resuelta') y <24h
      - resuelta_automaticamente: True si estado='auto_resuelta'
      - reaparicion_reciente: True si reaparicion_en está dentro de las últimas 24h
      - timestamps relevantes
    """
    estados = cargar_estados(alias)
    if fingerprint_str not in estados:
        return {"estado": "pendiente"}
    est = estados[fingerprint_str]
    ahora = datetime.now(timezone.utc)
    out: dict = {
        "estado":         est.estado,
        "actualizada_en": est.actualizada_en,
        "razon":          est.razon,
    }

    if est.estado == "pospuesta":
        ts = _parse_iso(est.pospuesta_hasta)
        out["pospuesta_hasta"] = est.pospuesta_hasta
        out["es_pospuesta_vigente"] = bool(ts and ts > ahora)

    if est.estado == "descartada":
        out["es_descartada_vigente"] = True

    if est.estado in ("resuelta", "auto_resuelta"):
        ts = _parse_iso(est.actualizada_en)
        out["es_recien_resuelta"] = bool(ts and (ahora - ts) < timedelta(hours=RECIEN_RESUELTA_HORAS))
        out["resuelta_automaticamente"] = est.estado == "auto_resuelta"

    if est.reaparicion_en:
        ts = _parse_iso(est.reaparicion_en)
        out["reaparicion_reciente"] = bool(ts and (ahora - ts) < timedelta(hours=24))

    return out


def historial_resueltas(alias: str, days: int = 30) -> dict:
    """Devuelve resueltas + auto_resueltas en los últimos N días, con métricas.

    Returns:
      {
        "items": [{fingerprint, tipo, estado, actualizada_en, razon, snapshot}, ...]
                 ordenado por más recientes primero,
        "resumen": {
          "count_total": int,
          "count_manuales": int,
          "count_auto": int,
          "impacto_evitado_ars": float,  # suma de scores de los snapshots
          "ventana_dias": int,
        }
      }
    """
    estados = cargar_estados(alias)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = []
    impacto_total = 0.0
    count_manuales = 0
    count_auto = 0
    for est in estados.values():
        if est.estado not in ("resuelta", "auto_resuelta"):
            continue
        ts = _parse_iso(est.actualizada_en)
        if not ts or ts < cutoff:
            continue
        snap = est.snapshot or {}
        impacto = float(snap.get("score_impacto_ars") or 0)
        impacto_total += impacto
        if est.estado == "auto_resuelta":
            count_auto += 1
        else:
            count_manuales += 1
        items.append(asdict(est))
    items.sort(key=lambda d: d.get("actualizada_en", ""), reverse=True)
    return {
        "items":   items,
        "resumen": {
            "count_total":         len(items),
            "count_manuales":      count_manuales,
            "count_auto":          count_auto,
            "impacto_evitado_ars": round(impacto_total, 2),
            "ventana_dias":        days,
        },
    }
