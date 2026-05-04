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

import logging
import os
from datetime import datetime, timedelta, timezone

from modules.alertas_estado import fingerprint as _alert_fingerprint

_logger = logging.getLogger(__name__)

# ── Constantes del sprint ────────────────────────────────────────────────────

DIAS_MINIMOS_VEREDICTO    = 7      # No emitimos veredicto antes de T+7
SNAPSHOTS_MINIMOS         = 3      # Si hay <3 snapshots, "data_insuficiente"
TTL_VEREDICTO_DIAS        = 30     # Veredicto vigente válido 30d antes de re-evaluar
TTL_DESCARTADO_DIAS       = 30     # Descartados se purgan del store a los 30d
THRESHOLD_DEFAULT_PCT     = 15.0   # Threshold base (≥5 snapshots)
THRESHOLD_DATA_RUIDOSA    = 20.0   # Threshold cuando snapshots<5
SNAPSHOTS_PARA_THRESHOLD_BAJO = 5

PROMPT_VERSION = "1.0"
MODELO_DEFAULT = "claude-opus-4-6"

# Token budget (input ~3K, output ~1K → ~$0.10 por veredicto en peor caso)
MAX_INPUT_TOKENS_OBJETIVO = 3000
MAX_OUTPUT_TOKENS         = 1024

# Hard cap mensual de costo del feature Veredicto IA (decisión Sprint 3.2)
# Si el costo acumulado del mes corriente supera esto, el cron se pausa.
HARD_CAP_USD_MENSUAL      = 30.0
# Cap blando por corrida del cron (anti-runaway en una sola ejecución)
MAX_VEREDICTOS_POR_CORRIDA = 100

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

def _cargar_store(data_dir: str, purgar_expirados: bool = True) -> dict:
    """Carga el store de veredictos. Por default purga descartados >30d.

    La purga es transparente: si encuentra descartados expirados los elimina
    del array y reescribe el archivo. Devuelve el store ya limpio.
    """
    raw = _load_json(_veredictos_path(data_dir), default={"items": []}) or {"items": []}
    if not isinstance(raw, dict) or "items" not in raw:
        return {"items": []}

    if not purgar_expirados:
        return raw

    cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DESCARTADO_DIAS)
    antes = len(raw.get("items", []))
    items_limpios = []
    for v in raw.get("items", []):
        if v.get("estado") == "descartado":
            ts = _parse_fecha(v.get("actualizado_en") or v.get("fecha_veredicto") or "")
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts and ts < cutoff:
                continue   # Purgar
        items_limpios.append(v)

    if len(items_limpios) != antes:
        raw["items"] = items_limpios
        _save_json(_veredictos_path(data_dir), raw)
        _logger.info("[veredicto] purgados %d descartados expirados (TTL %dd)",
                     antes - len(items_limpios), TTL_DESCARTADO_DIAS)

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


# ── Historial / Reportes ─────────────────────────────────────────────────────

def historial_veredictos(alias: str | None, data_dir: str,
                         days: int = 90, incluir_descartados: bool = False) -> dict:
    """Devuelve veredictos generados en los últimos N días con resumen agregado.

    Args:
      alias:               filtra por cuenta. None = todas.
      days:                ventana de tiempo (default 90d).
      incluir_descartados: si False, solo vigente + obsoleto.

    Returns:
      {
        "items":   [veredicto, ...] ordenado más recientes primero,
        "resumen": {
          "count_total":      int,
          "count_ganadora":   int,
          "count_neutra":     int,
          "count_perdedora":  int,
          "score_promedio":   float,
          "costo_total_usd":  float,
          "ventana_dias":     int,
        }
      }
    """
    store = _cargar_store(data_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    estados_validos = {"vigente", "obsoleto"}
    if incluir_descartados:
        estados_validos.add("descartado")

    items: list[dict] = []
    counts = {"ganadora": 0, "neutra": 0, "perdedora": 0}
    score_acc = 0.0
    score_n = 0
    costo_acc = 0.0

    for v in store.get("items", []):
        if alias is not None and v.get("alias") != alias:
            continue
        if v.get("estado") not in estados_validos:
            continue
        ts = _parse_fecha(v.get("fecha_veredicto", ""))
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if not ts or ts < cutoff:
            continue
        items.append(v)
        ver = v.get("veredicto")
        if ver in counts:
            counts[ver] += 1
        sc = v.get("score_exito")
        if isinstance(sc, (int, float)):
            score_acc += float(sc)
            score_n += 1
        debug = v.get("_debug") or {}
        costo_acc += float(debug.get("cost_usd") or 0)

    items.sort(key=lambda d: d.get("fecha_veredicto", ""), reverse=True)
    return {
        "items": items,
        "resumen": {
            "count_total":     len(items),
            "count_ganadora":  counts["ganadora"],
            "count_neutra":    counts["neutra"],
            "count_perdedora": counts["perdedora"],
            "score_promedio":  round(score_acc / score_n, 1) if score_n else 0.0,
            "costo_total_usd": round(costo_acc, 4),
            "ventana_dias":    days,
        },
    }


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

    try:
        veredicto_dict = _generar_veredicto_ia(
            alias=alias,
            item_id=item_id,
            titulo_producto=entry.get("titulo_producto") or "",
            fecha_opt=fecha_opt,
            dias=dias,
            baseline=baseline,
            deltas=deltas,
            snapshots_usados=len(snapshots),
            threshold=threshold,
            applied=entry.get("applied") or [],
            data_dir=data_dir,
        )
    except Exception as e:
        _logger.error("[veredicto] error generando IA para %s/%s: %s",
                      alias, item_id, e)
        return {
            "estado":             "error",
            "dias_transcurridos": dias,
            "snapshots_usados":   len(snapshots),
            "mensaje":            f"Error al generar veredicto: {e}",
            "veredicto":          None,
        }

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


# ── Generación IA (Claude Opus) ──────────────────────────────────────────────

# Costo por modelo en USD por 1M tokens (espejado de web/app.py para no
# crear dependencia circular con el server)
_TOKEN_PRICES_USD_PER_M = {
    "claude-opus-4-6":           {"input": 15.0,  "output": 75.0},
    "claude-haiku-4-5-20251001": {"input":  0.25, "output":  1.25},
}


def _fmt_num(v, suffix: str = "") -> str:
    """Formato amigable para el prompt: enteros con separador, floats con 2 dec."""
    if v is None:
        return "n/d"
    try:
        f = float(v)
        if f.is_integer():
            return f"{int(f):,}".replace(",", ".") + suffix
        return f"{f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + suffix
    except (TypeError, ValueError):
        return str(v) + suffix


def _fmt_delta_pct(d: float | None) -> str:
    if d is None:
        return "n/d"
    sign = "+" if d > 0 else ""
    return f"{sign}{d}%"


def _serializar_deltas_para_prompt(deltas: dict) -> str:
    """Tabla compacta T0 | actual | Δ — minimiza tokens y maximiza claridad."""
    rows = []
    metricas = [
        ("Visitas 7d",   "visitas_7d",   ""),
        ("Ventas 30d",   "ventas_30d",   " unid"),
        ("Ventas total", "ventas_total", " unid"),
        ("Conversión",   "conv_pct",     "%"),
    ]
    for label, key, suf in metricas:
        d = deltas.get(key) or {}
        rows.append(
            f"  - {label:<14}T0: {_fmt_num(d.get('t0'), suf):<12} "
            f"actual: {_fmt_num(d.get('actual'), suf):<12} "
            f"Δ: {_fmt_delta_pct(d.get('delta_pct'))}"
        )
    # Posición — formato especial (ranking discreto, lower is better)
    pd = deltas.get("posicion") or {}
    pos_t0   = _fmt_num(pd.get("t0"))
    pos_now  = _fmt_num(pd.get("actual"))
    pos_delta = pd.get("delta_abs")
    if pos_delta is None:
        pos_str = "n/d"
    elif pos_delta > 0:
        pos_str = f"subió {int(pos_delta)} puesto(s) (mejoró)"
    elif pos_delta < 0:
        pos_str = f"bajó {abs(int(pos_delta))} puesto(s) (empeoró)"
    else:
        pos_str = "sin cambio"
    rows.append(
        f"  - Posición       T0: #{pos_t0:<11} actual: #{pos_now:<11} Δ: {pos_str}"
    )
    return "\n".join(rows)


def _construir_prompt(titulo_producto: str, fecha_opt: str, dias: int,
                      baseline: dict, deltas: dict,
                      snapshots_usados: int, threshold: float,
                      applied: list) -> str:
    """Prompt compacto y accionable. ~1.2-1.8K tokens input."""
    pos_kw = baseline.get("posicion_kw") or "n/d"
    score_ml = (baseline.get("visibilidad") or {}).get("score_calidad_ml")
    bb       = (baseline.get("visibilidad") or {}).get("tiene_buy_box")
    cat      = (baseline.get("visibilidad") or {}).get("esta_en_catalogo")

    cambios_aplicados = ", ".join(applied) if applied else "no especificado"
    data_parcial      = "SÍ" if snapshots_usados < SNAPSHOTS_PARA_THRESHOLD_BAJO else "no"
    deltas_block      = _serializar_deltas_para_prompt(deltas)

    return f"""Sos un consultor de e-commerce evaluando si una optimización SEO de MercadoLibre Argentina fue exitosa. Tenés acceso a métricas reales antes/después capturadas por nuestro sistema. Reglas duras:

1. NO inventes datos. Solo razoná sobre lo que está abajo.
2. NO especules sobre el algoritmo de ML (es opaco).
3. Sé conciso, accionable, y citá deltas CONCRETOS (números, no "mejoró").
4. La recomendación debe ser un siguiente paso claro: replicar / mantener / revertir / regenerar.
5. Si la data es parcial (snapshots < 5), mencionalo en "alertas" — no inventes confianza.

═══════════════════════════════════════════════════════
PRODUCTO: {titulo_producto[:120]}
KEYWORD PRIORITARIA: "{pos_kw}"
OPTIMIZACIÓN APLICADA hace {dias} días ({fecha_opt})
CAMBIOS APLICADOS: {cambios_aplicados}

CONTEXTO DEL BASELINE T0:
  - Score de calidad ML: {score_ml if score_ml is not None else "n/d"}/100
  - En catálogo: {"sí" if cat else "no" if cat is not None else "n/d"}
  - Buy Box: {"sí" if bb else "no" if bb is not None else "n/d"}

DELTAS (T0 → snapshot más reciente):
{deltas_block}

DATA PARCIAL: {data_parcial}  (snapshots usados: {snapshots_usados})
THRESHOLD APLICADO: {threshold}% (cambios menores se consideran ruido)
═══════════════════════════════════════════════════════

REGLAS DE VEREDICTO (basadas en threshold {threshold}%):
- "ganadora": ≥2 métricas clave (visitas, ventas, conversión, posición) mejoran ≥{threshold}%, sin caídas significativas.
- "perdedora": ≥2 métricas clave caen ≥{threshold}%, o ventas 30d caen ≥10%.
- "neutra": el resto, o data muy ruidosa para conclusión fuerte.

RECOMENDACIÓN según veredicto:
- "replicar":  ganadora con patrón claro → llevar el cambio a publicaciones similares (Sprint 3.3).
- "mantener":  ganadora moderada o neutra positiva → no tocar, seguir midiendo.
- "regenerar": neutra ambigua o métricas mixtas → re-ejecutar Optimizar IA con aprendizajes.
- "revertir":  perdedora clara con caída sostenida → volver al título/ficha anterior.

DEVOLVÉ ÚNICAMENTE JSON VÁLIDO (sin markdown, sin texto adicional, sin ```), con esta estructura:
{{
  "veredicto": "ganadora" | "neutra" | "perdedora",
  "score_exito": <int 0-100>,
  "razonamiento": "<3-5 oraciones citando deltas concretos>",
  "recomendacion": "replicar" | "mantener" | "revertir" | "regenerar",
  "razon_recomendacion": "<1-2 oraciones explicando por qué esa acción>",
  "metricas_clave": ["<2-3 métricas que más movieron, ej: visitas_7d>"],
  "alertas": ["<observaciones, ej: 'data parcial: solo 4 snapshots'>"]
}}"""


def _parse_respuesta_claude(texto: str) -> dict:
    """Parsea el JSON devuelto por Claude. Tolerante a cercos de markdown."""
    import json as _json
    import re

    # Si vino envuelto en ```json ... ```, extraer
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", texto, re.DOTALL)
    candidato = fence.group(1) if fence else texto

    # Si hay texto antes/después del JSON, extraer el primer objeto balanceado
    inicio = candidato.find("{")
    if inicio == -1:
        raise ValueError(f"Respuesta sin JSON: {texto[:200]}")
    profundidad = 0
    fin = -1
    for i, ch in enumerate(candidato[inicio:], inicio):
        if ch == "{":
            profundidad += 1
        elif ch == "}":
            profundidad -= 1
            if profundidad == 0:
                fin = i
                break
    if fin == -1:
        raise ValueError(f"JSON sin cerrar: {candidato[:200]}")

    obj = _json.loads(candidato[inicio:fin + 1])

    # Validaciones / saneamiento
    if obj.get("veredicto") not in VEREDICTOS_VALIDOS:
        raise ValueError(f"Veredicto inválido: {obj.get('veredicto')!r}. "
                         f"Esperaba uno de {VEREDICTOS_VALIDOS}")
    if obj.get("recomendacion") not in RECOMENDACIONES_OK:
        raise ValueError(f"Recomendación inválida: {obj.get('recomendacion')!r}. "
                         f"Esperaba una de {RECOMENDACIONES_OK}")
    score = obj.get("score_exito")
    if not isinstance(score, (int, float)) or not (0 <= score <= 100):
        raise ValueError(f"score_exito fuera de rango: {score!r}")

    # Listas pueden venir vacías o como string — normalizar
    if not isinstance(obj.get("metricas_clave"), list):
        obj["metricas_clave"] = []
    if not isinstance(obj.get("alertas"), list):
        obj["alertas"] = []

    return obj


def _log_token_usage_local(data_dir: str, funcion: str, modelo: str,
                           tokens_in: int, tokens_out: int) -> float:
    """Loggea uso de tokens en data/token_log.json. Devuelve costo USD calculado.

    Espeja la estructura de web/app.py:_log_token_usage para que el cap mensual
    los pueda agregar uniformemente."""
    rates = _TOKEN_PRICES_USD_PER_M.get(modelo, {"input": 3.0, "output": 15.0})
    costo = (tokens_in * rates["input"] + tokens_out * rates["output"]) / 1_000_000
    entry = {
        "ts":      datetime.now().isoformat(timespec="seconds"),
        "funcion": funcion,
        "modelo":  modelo,
        "in":      tokens_in,
        "out":     tokens_out,
        "usd":     round(costo, 6),
    }
    path = os.path.join(data_dir, "token_log.json")
    try:
        log = _load_json(path, default={"entries": []}) or {"entries": []}
        log.setdefault("entries", []).append(entry)
        if len(log["entries"]) > 1000:
            log["entries"] = log["entries"][-1000:]
        _save_json(path, log)
    except Exception as e:
        _logger.error("[veredicto] no pude loggear tokens: %s", e)
    return round(costo, 6)


def _generar_veredicto_ia(alias: str, item_id: str, titulo_producto: str,
                          fecha_opt: str, dias: int,
                          baseline: dict, deltas: dict,
                          snapshots_usados: int, threshold: float,
                          applied: list, data_dir: str = "") -> dict:
    """Genera el veredicto vía Claude Opus. Devuelve el dict completo listo
    para persistir, con campo `_debug` rico para calibración futura.

    Lanza excepción si la API key no está, si Claude falla o si el JSON es
    inválido. El caller (evaluar_optimizacion) decide cómo manejar el error.
    """
    import os as _os
    api_key = _os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no configurada en el entorno.")

    # Import lazy para no exigir anthropic en tests con stub
    from anthropic import Anthropic  # type: ignore

    client = Anthropic(api_key=api_key)
    prompt = _construir_prompt(
        titulo_producto=titulo_producto, fecha_opt=fecha_opt, dias=dias,
        baseline=baseline, deltas=deltas,
        snapshots_usados=snapshots_usados, threshold=threshold,
        applied=applied,
    )

    t0 = datetime.now()
    msg = client.messages.create(
        model=MODELO_DEFAULT,
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    duracion_ms = int((datetime.now() - t0).total_seconds() * 1000)

    # Extraer texto del response
    texto = ""
    for block in (msg.content or []):
        if getattr(block, "type", "") == "text":
            texto += block.text
    if not texto:
        raise RuntimeError(f"Claude devolvió respuesta vacía. Stop reason: {msg.stop_reason}")

    parsed = _parse_respuesta_claude(texto)

    tokens_in  = msg.usage.input_tokens
    tokens_out = msg.usage.output_tokens
    costo = _log_token_usage_local(
        data_dir or _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data"),
        funcion="Veredicto IA — Evaluar optimización",
        modelo=MODELO_DEFAULT,
        tokens_in=tokens_in, tokens_out=tokens_out,
    )

    fp = fingerprint_veredicto(alias, item_id, fecha_opt)
    return {
        "fingerprint":         fp,
        "alias":               alias,
        "item_id":             item_id,
        "titulo_producto":     titulo_producto,
        "fecha_optimizacion":  fecha_opt,
        "fecha_veredicto":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dias_transcurridos":  dias,
        "veredicto":           parsed["veredicto"],
        "score_exito":         int(parsed["score_exito"]),
        "razonamiento":        parsed["razonamiento"],
        "recomendacion":       parsed["recomendacion"],
        "razon_recomendacion": parsed["razon_recomendacion"],
        "metricas_clave":      parsed["metricas_clave"],
        "alertas":             parsed["alertas"],
        "deltas":              deltas,
        "snapshots_usados":    snapshots_usados,
        "estado":              "vigente",
        "_debug": {
            "timestamp":             datetime.now().isoformat(timespec="seconds"),
            "tokens_in":             tokens_in,
            "tokens_out":            tokens_out,
            "cost_usd":              costo,
            "duracion_ms":           duracion_ms,
            "threshold_aplicado":    threshold,
            "snapshots_usados":      snapshots_usados,
            "snapshots_disponibles": snapshots_usados,
            "prompt_version":        PROMPT_VERSION,
            "modelo":                MODELO_DEFAULT,
        },
    }


# ── Cost guard (hard cap $30/mes) ────────────────────────────────────────────

def costo_mes_actual_usd(data_dir: str,
                         feature_substr: str = "Veredicto IA") -> float:
    """Suma el costo USD del feature en el mes corriente (calendario AR).

    Lee data/token_log.json (estructura compartida con web/app.py). Filtra por
    la subcadena del campo `funcion` y por mes/año actual. Tolerante: si no
    hay log o no es parseable, devuelve 0.0.
    """
    path = os.path.join(data_dir, "token_log.json")
    log = _load_json(path, default={"entries": []}) or {"entries": []}
    if not isinstance(log, dict):
        return 0.0
    ahora = datetime.now()
    año, mes = ahora.year, ahora.month
    acumulado = 0.0
    for e in log.get("entries", []):
        if feature_substr not in (e.get("funcion") or ""):
            continue
        ts = _parse_fecha(e.get("ts") or "")
        if not ts or ts.year != año or ts.month != mes:
            continue
        try:
            acumulado += float(e.get("usd") or 0)
        except (TypeError, ValueError):
            continue
    return round(acumulado, 4)


def supera_cap_mensual(data_dir: str) -> tuple[bool, float]:
    """Devuelve (supera, costo_actual). True si el costo del mes ya alcanzó
    o superó HARD_CAP_USD_MENSUAL."""
    actual = costo_mes_actual_usd(data_dir)
    return (actual >= HARD_CAP_USD_MENSUAL, actual)


__all__ = [
    "evaluar_optimizacion",
    "listar_pendientes",
    "obtener_veredicto",
    "descartar_veredicto",
    "historial_veredictos",
    "fingerprint_veredicto",
    "threshold_aplicado",
    "costo_mes_actual_usd",
    "supera_cap_mensual",
    "DIAS_MINIMOS_VEREDICTO",
    "SNAPSHOTS_MINIMOS",
    "TTL_VEREDICTO_DIAS",
    "TTL_DESCARTADO_DIAS",
    "HARD_CAP_USD_MENSUAL",
    "MAX_VEREDICTOS_POR_CORRIDA",
    "PROMPT_VERSION",
    "MODELO_DEFAULT",
]
