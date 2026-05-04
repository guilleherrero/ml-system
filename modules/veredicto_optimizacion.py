"""
Sprint 3.2 — Veredicto IA sobre optimizaciones aplicadas.

Cierra el loop "Hacer → Medir → Aprender": una vez aplicada una optimización en
ML y transcurridos ≥7 días, este módulo lee el baseline T0 + los snapshots
diarios capturados, calcula los deltas y le pide a Claude Opus que emita un
veredicto accionable: ✅ ganadora / 🟡 neutra / ❌ perdedora.

NO toca `modules/seo_optimizer.py`. Solo CONSUME el baseline producido por
`modules/baseline_capture.py` y los snapshots persistidos en monitor_evolucion.json.

API pública:
    evaluar_optimizacion(item_id, alias, data_dir, force=False) → dict
    listar_pendientes(data_dir, dias_minimos=7) → list[dict]
    obtener_veredicto(item_id, alias, data_dir) → dict | None
    descartar_veredicto(item_id, alias, data_dir) → bool

Reglas críticas:
- NO llama a Claude API si han pasado <7 días desde la optimización.
- NO regenera si ya existe un veredicto vigente <30 días (idempotencia).
- Threshold dinámico: 20% si snapshots_usados<5, 15% si ≥5.
- Hard cap de costo: $30/mes (revisado en cron — ver scheduler).
- Cada veredicto incluye `_debug` con tokens, costo, prompt_version y modelo.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from modules.alertas_estado import fingerprint as _alert_fingerprint

_logger = logging.getLogger(__name__)

# ── Constantes del sprint ────────────────────────────────────────────────────

DIAS_MINIMOS_VEREDICTO    = 7      # No emitimos veredicto antes de T+7
SNAPSHOTS_MINIMOS         = 3      # Si hay <3 snapshots, "data_insuficiente"
TTL_VEREDICTO_DIAS        = 30     # Veredicto válido 30d antes de re-evaluar
THRESHOLD_DEFAULT_PCT     = 15.0   # Threshold base (≥5 snapshots)
THRESHOLD_DATA_RUIDOSA    = 20.0   # Threshold cuando snapshots<5
SNAPSHOTS_PARA_THRESHOLD_BAJO = 5

PROMPT_VERSION = "1.0"
MODELO_DEFAULT = "claude-opus-4-6"

# Token budget (input ~3K, output ~1K → ~$0.10 por veredicto)
MAX_INPUT_TOKENS_OBJETIVO = 3000
MAX_OUTPUT_TOKENS         = 1024

# Veredictos válidos
VEREDICTOS_VALIDOS    = ("ganadora", "neutra", "perdedora")
RECOMENDACIONES_OK    = ("replicar", "mantener", "revertir", "regenerar")
ESTADOS_VEREDICTO     = ("vigente", "descartado", "obsoleto")


# ── Persistencia ─────────────────────────────────────────────────────────────

def _veredictos_path(data_dir: str) -> str:
    return os.path.join(data_dir, "veredictos_optimizacion.json")


def _monitor_path(data_dir: str) -> str:
    return os.path.join(data_dir, "monitor_evolucion.json")


def _load_json(path: str, default=None):
    """Lee JSON desde DB (Render) o filesystem (local). Tolerante a errores."""
    try:
        from core.db_storage import db_load
        result = db_load(path)
        return result if result is not None else default
    except Exception as e:
        _logger.warning("[veredicto] failed loading %s: %s", path, e)
        return default


def _save_json(path: str, data) -> None:
    try:
        from core.db_storage import db_save
        db_save(path, data)
    except Exception as e:
        _logger.error("[veredicto] failed saving %s: %s", path, e)
        raise


# ── Fingerprint (reusa patrón de alertas_estado) ─────────────────────────────

def fingerprint_veredicto(alias: str, item_id: str, fecha_optimizacion: str) -> str:
    """Hash idempotente: mismo (alias + item_id + fecha_opt) = mismo fp.
    Una optimización tiene UN veredicto vigente."""
    return _alert_fingerprint("veredicto", alias, item_id, fecha_optimizacion)


# ── Lookup de optimización en monitor_evolucion.json ─────────────────────────

def _buscar_entry_monitor(data_dir: str, alias: str, item_id: str) -> dict | None:
    """Devuelve el item del monitor para (alias, item_id) o None si no existe."""
    mon = _load_json(_monitor_path(data_dir), default={"items": []}) or {"items": []}
    if not isinstance(mon, dict):
        return None
    for it in mon.get("items", []):
        if it.get("item_id") == item_id and it.get("alias") == alias:
            return it
    return None


# ── Helpers de fecha ─────────────────────────────────────────────────────────

def _parse_fecha(s: str) -> datetime | None:
    """Tolera formatos: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', ISO con o sin TZ."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) if "T" not in s else 19], fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _dias_transcurridos(fecha_str: str) -> int:
    dt = _parse_fecha(fecha_str)
    if not dt:
        return 0
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)
    delta = datetime.now() - dt
    return max(0, delta.days)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Cálculo de deltas T0 vs último snapshot ──────────────────────────────────

def _safe_num(v) -> float | None:
    """Convierte a float o devuelve None si no es numérico."""
    if v is None:
        return None
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _delta_pct(t0: float | None, actual: float | None) -> float | None:
    """Delta porcentual con guardas: requiere ambos numéricos y t0>0."""
    if t0 is None or actual is None:
        return None
    if t0 == 0:
        # Evitar división por cero. Si T0=0 y actual>0, lo marcamos como
        # "infinito" → reportamos como +999% (señal de mejora extrema).
        return 999.0 if actual > 0 else 0.0
    return round((actual - t0) / abs(t0) * 100, 1)


def _calcular_deltas(baseline: dict, ultimo_snapshot: dict) -> dict:
    """Calcula deltas absoluto + porcentaje para las métricas clave del veredicto.

    Soporta dos formatos:
      - Baseline v2: `baseline.trafico.visitas_7d`, `baseline.ventas.unidades_30d`, etc.
      - Snapshot ligero (api/monitor-refresh): campos planos en root.

    Para el veredicto solo nos interesan las métricas que ML actualiza con
    refresh diario: visitas_7d, ventas_30d, ventas_total, conv_pct, posicion.
    """
    # T0 — preferir secciones v2, fallback a campos planos legacy
    traf = (baseline.get("trafico") or {}) if isinstance(baseline, dict) else {}
    vent = (baseline.get("ventas")  or {}) if isinstance(baseline, dict) else {}

    t0 = {
        "visitas_7d":    _safe_num(traf.get("visitas_7d")    if traf else baseline.get("visitas_7d")),
        "ventas_30d":    _safe_num(vent.get("unidades_30d")  if vent else baseline.get("ventas_30d")),
        "ventas_total":  _safe_num(vent.get("ventas_total_historica") if vent else baseline.get("ventas_total")),
        "conv_pct":      _safe_num(traf.get("conversion_30d") if traf else baseline.get("conv_pct")),
        "posicion":      _safe_num(baseline.get("posicion")),
    }
    # Actual — el snapshot de monitor-refresh siempre tiene campos planos
    actual = {
        "visitas_7d":   _safe_num(ultimo_snapshot.get("visitas_7d")),
        "ventas_30d":   _safe_num(ultimo_snapshot.get("ventas_30d")),
        "ventas_total": _safe_num(ultimo_snapshot.get("ventas_total")),
        "conv_pct":     _safe_num(ultimo_snapshot.get("conv_pct")),
        "posicion":     _safe_num(ultimo_snapshot.get("posicion")),
    }

    # Para posición: lower-is-better, calculamos delta_abs invertido para que
    # "positivo = mejoró" como en el resto.
    deltas: dict[str, dict] = {}
    for k in ("visitas_7d", "ventas_30d", "ventas_total", "conv_pct"):
        deltas[k] = {
            "t0":        t0[k],
            "actual":    actual[k],
            "delta_abs": (round(actual[k] - t0[k], 2) if t0[k] is not None and actual[k] is not None else None),
            "delta_pct": _delta_pct(t0[k], actual[k]),
        }
    # Posición: delta_abs negativo = mejoró (subió en ranking).
    # Lo expresamos invertido para que "positivo = mejoró" coherente con el resto.
    deltas["posicion"] = {
        "t0":        t0["posicion"],
        "actual":    actual["posicion"],
        "delta_abs": (round(t0["posicion"] - actual["posicion"], 1) if t0["posicion"] is not None and actual["posicion"] is not None else None),
        "delta_pct": None,   # posición no se evalúa por % (ranking discreto)
    }

    return deltas


# ── Threshold dinámico ───────────────────────────────────────────────────────

def threshold_aplicado(snapshots_usados: int) -> float:
    """Retorna el threshold % a aplicar según cantidad de snapshots disponibles.

    Reglas confirmadas:
      - snapshots <5 → 20% (data ruidosa)
      - snapshots ≥5 → 15%
    """
    if snapshots_usados < SNAPSHOTS_PARA_THRESHOLD_BAJO:
        return THRESHOLD_DATA_RUIDOSA
    return THRESHOLD_DEFAULT_PCT


# ── Lectura/escritura del store de veredictos ────────────────────────────────

def _cargar_store(data_dir: str) -> dict:
    raw = _load_json(_veredictos_path(data_dir), default={"items": []}) or {"items": []}
    if not isinstance(raw, dict) or "items" not in raw:
        return {"items": []}
    return raw


def _persistir_store(data_dir: str, store: dict) -> None:
    _save_json(_veredictos_path(data_dir), store)


def obtener_veredicto(item_id: str, alias: str, data_dir: str,
                      incluir_no_vigentes: bool = False) -> dict | None:
    """Devuelve el veredicto vigente para un item o None si no existe.

    Args:
      incluir_no_vigentes: si True, devuelve también veredictos descartados/obsoletos.
    """
    store = _cargar_store(data_dir)
    for v in store.get("items", []):
        if v.get("item_id") != item_id or v.get("alias") != alias:
            continue
        if not incluir_no_vigentes and v.get("estado") != "vigente":
            continue
        return v
    return None


def _veredicto_vigente_reciente(item_id: str, alias: str, data_dir: str) -> dict | None:
    """Devuelve el veredicto vigente <30d o None. Marca como 'obsoleto' los >30d."""
    store = _cargar_store(data_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_VEREDICTO_DIAS)
    cambio = False
    encontrado: dict | None = None
    for v in store.get("items", []):
        if v.get("item_id") != item_id or v.get("alias") != alias:
            continue
        if v.get("estado") != "vigente":
            continue
        ts = _parse_fecha(v.get("fecha_veredicto", ""))
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts < cutoff:
            v["estado"] = "obsoleto"
            v["actualizado_en"] = _now_iso()
            cambio = True
            continue
        encontrado = v
    if cambio:
        _persistir_store(data_dir, store)
    return encontrado


def descartar_veredicto(item_id: str, alias: str, data_dir: str,
                        razon: str = "") -> bool:
    """Marca el veredicto vigente como 'descartado'. Idempotente."""
    store = _cargar_store(data_dir)
    cambio = False
    for v in store.get("items", []):
        if v.get("item_id") == item_id and v.get("alias") == alias and v.get("estado") == "vigente":
            v["estado"] = "descartado"
            v["actualizado_en"] = _now_iso()
            if razon:
                v["razon_descarte"] = razon[:300]
            cambio = True
    if cambio:
        _persistir_store(data_dir, store)
    return cambio


# ── Pendientes (helpers para el cron) ────────────────────────────────────────

def listar_pendientes(data_dir: str,
                      dias_minimos: int = DIAS_MINIMOS_VEREDICTO) -> list[dict]:
    """Devuelve optimizaciones del monitor que cumplen TODAS estas condiciones:

    1. Tienen `fecha_opt` con ≥`dias_minimos` días transcurridos.
    2. Tienen `baseline` capturado (no en estado `_capturing`).
    3. Tienen al menos `SNAPSHOTS_MINIMOS` snapshots.
    4. NO tienen un veredicto vigente <30 días.

    Cada elemento del resultado: {alias, item_id, fecha_opt, dias, snapshots_n}.
    Útil para el cron semanal.
    """
    mon = _load_json(_monitor_path(data_dir), default={"items": []}) or {"items": []}
    if not isinstance(mon, dict):
        return []
    pendientes: list[dict] = []
    for it in mon.get("items", []):
        if it.get("_capturing"):
            continue
        if not it.get("baseline"):
            continue
        alias = it.get("alias")
        item_id = it.get("item_id")
        fecha_opt = it.get("fecha_opt") or ""
        if not alias or not item_id or not fecha_opt:
            continue
        dias = _dias_transcurridos(fecha_opt)
        if dias < dias_minimos:
            continue
        snaps = it.get("snapshots") or []
        if len(snaps) < SNAPSHOTS_MINIMOS:
            continue
        if _veredicto_vigente_reciente(item_id, alias, data_dir):
            continue
        pendientes.append({
            "alias":       alias,
            "item_id":     item_id,
            "fecha_opt":   fecha_opt,
            "dias":        dias,
            "snapshots_n": len(snaps),
        })
    return pendientes


# ── Función pública principal ────────────────────────────────────────────────

def evaluar_optimizacion(item_id: str, alias: str, data_dir: str = "",
                         force: bool = False) -> dict:
    """Genera o devuelve un veredicto IA sobre una optimización aplicada.

    Args:
      item_id:  ID de la publicación ML (ej. "MLA1234567").
      alias:    alias de la cuenta.
      data_dir: ruta a /data del proyecto.
      force:    si True, salta el check de "ya_existe" (override manual).
                NO salta el check de 7 días — eso es regla dura.

    Returns:
      dict con shape:
      {
        "estado":               "veredicto" | "data_insuficiente" | "ya_existe" | "no_encontrado",
        "dias_transcurridos":   int,
        "dias_faltantes":       int,        # solo si data_insuficiente por <7d
        "proxima_evaluacion":   "YYYY-MM-DD",
        "snapshots_usados":     int,
        "veredicto":            dict | None,
        "mensaje":              str,
      }
    """
    # ── 1) Buscar la optimización en el monitor ─────────────────────────────
    entry = _buscar_entry_monitor(data_dir, alias, item_id)
    if not entry:
        return {
            "estado":   "no_encontrado",
            "mensaje":  f"No hay entry en monitor_evolucion para {alias}/{item_id}.",
            "veredicto": None,
        }

    fecha_opt = entry.get("fecha_opt") or ""
    baseline  = entry.get("baseline") or {}
    snapshots = entry.get("snapshots") or []
    if not fecha_opt or not baseline:
        return {
            "estado":   "data_insuficiente",
            "mensaje":  "Optimización sin fecha o baseline aún capturándose.",
            "snapshots_usados": len(snapshots),
            "veredicto": None,
        }

    # ── 2) Safeguard de tiempo ───────────────────────────────────────────────
    dias = _dias_transcurridos(fecha_opt)
    if dias < DIAS_MINIMOS_VEREDICTO:
        faltantes = DIAS_MINIMOS_VEREDICTO - dias
        proxima = (datetime.now() + timedelta(days=faltantes)).strftime("%Y-%m-%d")
        return {
            "estado":             "data_insuficiente",
            "dias_transcurridos": dias,
            "dias_faltantes":     faltantes,
            "proxima_evaluacion": proxima,
            "snapshots_usados":   len(snapshots),
            "mensaje":            f"Necesita {faltantes} día(s) más de data. Próximo veredicto: {proxima}.",
            "veredicto":          None,
        }

    # ── 3) Safeguard de snapshots ────────────────────────────────────────────
    if len(snapshots) < SNAPSHOTS_MINIMOS:
        proxima = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        return {
            "estado":             "data_insuficiente",
            "dias_transcurridos": dias,
            "snapshots_usados":   len(snapshots),
            "proxima_evaluacion": proxima,
            "mensaje":            (f"Solo {len(snapshots)} snapshot(s) capturados. "
                                   f"Necesita ≥{SNAPSHOTS_MINIMOS} para emitir veredicto."),
            "veredicto":          None,
        }

    # ── 4) Safeguard de idempotencia ─────────────────────────────────────────
    existente = _veredicto_vigente_reciente(item_id, alias, data_dir)
    if existente and not force:
        return {
            "estado":             "ya_existe",
            "dias_transcurridos": dias,
            "snapshots_usados":   len(snapshots),
            "mensaje":            ("Ya existe un veredicto vigente para esta optimización. "
                                   "Pasá force=True para regenerar."),
            "veredicto":          existente,
        }

    # ── 5) Calcular deltas y delegar al motor IA ─────────────────────────────
    ultimo = entry.get("ultimo_snapshot") or (snapshots[-1] if snapshots else {})
    deltas = _calcular_deltas(baseline, ultimo)
    threshold = threshold_aplicado(len(snapshots))

    veredicto_dict = _generar_veredicto_ia(
        alias=alias,
        item_id=item_id,
        titulo_producto=entry.get("titulo_producto") or "",
        fecha_opt=fecha_opt,
        dias=dias,
        baseline=baseline,
        ultimo=ultimo,
        deltas=deltas,
        snapshots_usados=len(snapshots),
        threshold=threshold,
        applied=entry.get("applied") or [],
    )

    # ── 6) Persistir ─────────────────────────────────────────────────────────
    _persistir_veredicto(data_dir, veredicto_dict, force_replace=bool(existente))

    return {
        "estado":             "veredicto",
        "dias_transcurridos": dias,
        "snapshots_usados":   len(snapshots),
        "mensaje":            "Veredicto generado.",
        "veredicto":          veredicto_dict,
    }


def _persistir_veredicto(data_dir: str, veredicto_dict: dict,
                         force_replace: bool = False) -> None:
    """Agrega o reemplaza un veredicto en el store. Marca al previo como
    'obsoleto' si force_replace=True."""
    store = _cargar_store(data_dir)
    items: list[dict] = store.get("items", [])
    fp = veredicto_dict["fingerprint"]

    if force_replace:
        for v in items:
            if (v.get("item_id") == veredicto_dict["item_id"]
                    and v.get("alias") == veredicto_dict["alias"]
                    and v.get("estado") == "vigente"):
                v["estado"] = "obsoleto"
                v["actualizado_en"] = _now_iso()

    # Si el fingerprint ya existe en el store (por colisión), lo reemplazamos.
    found = False
    for i, v in enumerate(items):
        if v.get("fingerprint") == fp:
            items[i] = veredicto_dict
            found = True
            break
    if not found:
        items.append(veredicto_dict)

    store["items"] = items
    _persistir_store(data_dir, store)


# ── Generación IA — definida en commit 3, stub aquí para test ────────────────

def _generar_veredicto_ia(alias: str, item_id: str, titulo_producto: str,
                          fecha_opt: str, dias: int,
                          baseline: dict, ultimo: dict, deltas: dict,
                          snapshots_usados: int, threshold: float,
                          applied: list) -> dict:
    """Genera el veredicto via Claude API. Stub implementado en commit 3.

    Hasta entonces, los tests usan monkey-patch para evitar llamadas reales."""
    raise NotImplementedError(
        "_generar_veredicto_ia se implementa en commit 3 (prompt + Claude API)."
    )


__all__ = [
    "evaluar_optimizacion",
    "listar_pendientes",
    "obtener_veredicto",
    "descartar_veredicto",
    "fingerprint_veredicto",
    "threshold_aplicado",
    "DIAS_MINIMOS_VEREDICTO",
    "SNAPSHOTS_MINIMOS",
    "TTL_VEREDICTO_DIAS",
    "PROMPT_VERSION",
    "MODELO_DEFAULT",
]
