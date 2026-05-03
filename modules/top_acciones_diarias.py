"""
Sprint 4.1 — Top 3 acciones del día para el Command Center.

Detecta oportunidades cuantificables monetariamente desde 7 fuentes,
las rankea por impacto mensual ARS desc, y devuelve las 3 más rentables.

Diseño:
  - Cada detector lee su fuente y devuelve list[Oportunidad] con impacto $.
  - Defensivos: si la fuente no existe o falla, devuelven [] sin romper el motor.
  - Cada detector loggea conteo + tiempo + razones de skip.
  - Cómputo cacheable: top3() guarda result en data/top_acciones_cache_<alias>.json
  - Dismiss: TTL 30 días, persistido en data/top_acciones_dismissed_<alias>.json
  - Done: snapshot al marcar hecho, persistido en data/top_acciones_hechas_<alias>.json
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from core.impact_calculator import (
    impacto_ads_gap,
    impacto_buybox_perdido,
    impacto_stock_critico,
    impacto_stock_muerto_full,
    impacto_trafico_desperdiciado,
)

_logger = logging.getLogger(__name__)

ROOT_DIR    = os.path.dirname(os.path.dirname(__file__))
DATA_DIR    = os.path.join(ROOT_DIR, "data")
CONFIG_DIR  = os.path.join(ROOT_DIR, "config")

DISMISS_TTL_DAYS = 30
CACHE_TTL_HOURS  = 24


# ── Modelo ────────────────────────────────────────────────────────────────────

@dataclass
class Oportunidad:
    fingerprint: str
    tipo: str
    descripcion: str
    impacto_mensual_ars: float
    urgencia: str            # 'critica' | 'alta' | 'media'
    cta_label: str
    cta_url: str
    fuente: str
    snapshot: dict = field(default_factory=dict)


# ── Helpers de I/O ────────────────────────────────────────────────────────────

def _safe(alias: str) -> str:
    return alias.replace(" ", "_").replace("/", "-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(tipo: str, *parts) -> str:
    raw = tipo + ":" + ":".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_json(path: str, default=None):
    try:
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _logger.warning("[top_acciones] failed loading %s: %s", path, e)
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _dismissed_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f"top_acciones_dismissed_{_safe(alias)}.json")


def _done_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f"top_acciones_hechas_{_safe(alias)}.json")


def _cache_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f"top_acciones_cache_{_safe(alias)}.json")


def _first_run_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f"top_acciones_first_run_{_safe(alias)}.json")


def _stock_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f"stock_{_safe(alias)}.json")


# ── Dismiss / Done API ────────────────────────────────────────────────────────

def get_dismissed_fingerprints(alias: str) -> set:
    """Devuelve fingerprints descartados que aún están dentro del TTL.
    Limpia los expirados al pasar."""
    data = _load_json(_dismissed_path(alias), default={"items": []}) or {"items": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=DISMISS_TTL_DAYS)
    active = []
    for d in data.get("items", []):
        try:
            ts = datetime.fromisoformat(d["dismissed_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                active.append(d)
        except Exception:
            continue
    if len(active) < len(data.get("items", [])):
        _save_json(_dismissed_path(alias), {"items": active})
    return {d["fingerprint"] for d in active}


def add_dismissed(alias: str, fingerprint: str, tipo: str = "",
                  descripcion: str = "", razon: str = "") -> None:
    data = _load_json(_dismissed_path(alias), default={"items": []}) or {"items": []}
    if any(d.get("fingerprint") == fingerprint for d in data["items"]):
        return
    data["items"].append({
        "fingerprint":   fingerprint,
        "tipo":          tipo,
        "descripcion":   descripcion,
        "razon":         razon,
        "dismissed_at":  _now_iso(),
    })
    _save_json(_dismissed_path(alias), data)
    _logger.info("[top_acciones] dismissed %s for %s (razon=%r)",
                 fingerprint, alias, razon)


def add_done(alias: str, fingerprint: str, tipo: str, descripcion: str,
             impacto_reportado_ars: float, snapshot: dict) -> None:
    data = _load_json(_done_path(alias), default={"items": []}) or {"items": []}
    data["items"].append({
        "fingerprint":             fingerprint,
        "tipo":                    tipo,
        "impacto_reportado_ars":   impacto_reportado_ars,
        "marcado_hecho_en":        _now_iso(),
        "snapshot_estado_al_marcar": snapshot or {},
        "descripcion_humana":      descripcion,
    })
    _save_json(_done_path(alias), data)
    _logger.info("[top_acciones] done %s for %s (impacto=$%s)",
                 fingerprint, alias, impacto_reportado_ars)


def get_done_summary(alias: str, days: int = 7) -> dict:
    """Devuelve resumen de acciones marcadas como hechas en los últimos N días."""
    data = _load_json(_done_path(alias), default={"items": []}) or {"items": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    activas = []
    for d in data.get("items", []):
        try:
            ts = datetime.fromisoformat(d["marcado_hecho_en"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                activas.append(d)
        except Exception:
            continue
    impacto_total = sum(float(d.get("impacto_reportado_ars") or 0) for d in activas)
    return {
        "count":          len(activas),
        "impacto_total":  round(impacto_total, 2),
        "ventana_dias":   days,
    }


# ── Detectors ─────────────────────────────────────────────────────────────────

def _candidates_duplicados(alias: str) -> list[Oportunidad]:
    """A — clusters duplicados con impacto monetario ya calculado por el módulo."""
    out: list[Oportunidad] = []
    skipped: dict = {"sin_impacto": 0, "sano": 0}

    stock = _load_json(_stock_path(alias))
    if not stock or not stock.get("items"):
        _logger.info("[top_acciones] duplicados: skip — sin stock_<alias>.json")
        return out

    try:
        from modules.detector_duplicados import detectar_duplicados
        clusters = detectar_duplicados(stock["items"], alias, DATA_DIR,
                                       incluir_sanos=False)
    except Exception as e:
        _logger.exception("[top_acciones] duplicados: detector falló: %s", e)
        return out

    for cl in clusters:
        impacto = float(getattr(cl, "impacto_monetario_estimado", 0) or 0)
        if impacto <= 0:
            skipped["sin_impacto"] += 1
            continue

        items_ids = sorted([it.id for it in cl.items])
        n_dup = len(items_ids)
        canonico = (cl.titulo_corto or "").strip() or items_ids[0]
        fp = _fingerprint("pausar_duplicados", *items_ids)

        out.append(Oportunidad(
            fingerprint=fp,
            tipo="pausar_duplicados",
            descripcion=f"PAUSAR {max(0, n_dup-1)} publicaciones canibalizando {canonico[:60]}",
            impacto_mensual_ars=impacto,
            urgencia="alta" if cl.severidad == "puro" else "media",
            cta_label="Ver detalle →",
            cta_url=f"/duplicados/{alias}",
            fuente="duplicados",
            snapshot={
                "cluster_severidad":     cl.severidad,
                "items_count":           n_dup,
                "items_ids":             items_ids,
                "visitas_perdidas_30d":  cl.visitas_perdidas_30d,
            },
        ))
    _logger.info("[top_acciones] duplicados: %d candidates (skipped=%s)", len(out), skipped)
    return out


def _candidates_ads(alias: str) -> list[Oportunidad]:
    """D — campañas/SKUs en Meli Ads sangrando (spend > revenue) en los últimos 30d.

    Lee data/raw/ads_performance.csv (formato del módulo meli_ads_engine).
    Threshold: gap >= $5.000 ARS para evitar ruido."""
    out: list[Oportunidad] = []

    try:
        from modules.meli_ads_engine import _load_ads_performance
        agg = _load_ads_performance()
    except Exception as e:
        _logger.exception("[top_acciones] ads: load falló: %s", e)
        return out

    if not agg:
        _logger.info("[top_acciones] ads: skip — sin data/raw/ads_performance.csv")
        return out

    skipped = {"con_revenue": 0, "gap_chico": 0}
    for sku, m in agg.items():
        spend   = float(m.get("spend") or 0)
        revenue = float(m.get("revenue") or 0)
        gap = impacto_ads_gap(spend, revenue)
        if gap < 5000:
            if revenue > spend:
                skipped["con_revenue"] += 1
            else:
                skipped["gap_chico"] += 1
            continue

        fp = _fingerprint("pausar_ads", sku)
        out.append(Oportunidad(
            fingerprint=fp,
            tipo="pausar_ads",
            descripcion=f"PAUSAR ADS de {sku} — ${spend:,.0f} gastados, ${revenue:,.0f} en ventas",
            impacto_mensual_ars=gap,
            urgencia="alta" if gap >= 30000 else "media",
            cta_label="Pausar →",
            cta_url=f"/meli-ads",
            fuente="ads",
            snapshot={
                "sku":         sku,
                "spend_30d":   round(spend, 2),
                "revenue_30d": round(revenue, 2),
                "gap":         gap,
                "clicks":      int(m.get("clicks") or 0),
                "conversions": int(m.get("conversions") or 0),
            },
        ))
    _logger.info("[top_acciones] ads: %d candidates from %d SKUs (skipped=%s)",
                 len(out), len(agg), skipped)
    return out


def _candidates_funnel(alias: str) -> list[Oportunidad]:
    """E — items con tráfico alto y 0 ventas en 30d (visitas >= 50, ventas == 0).

    Lee directamente stock_<alias>.json (que tiene visitas_30d, ventas_30d)."""
    out: list[Oportunidad] = []
    skipped = {"sin_trafico": 0, "con_ventas": 0, "sin_costo": 0}

    stock = _load_json(_stock_path(alias))
    if not stock or not stock.get("items"):
        _logger.info("[top_acciones] funnel: skip — sin stock_<alias>.json")
        return out

    for it in stock["items"]:
        vis  = int(it.get("visitas_30d") or 0)
        vtas = int(it.get("ventas_30d") or 0)
        if vtas > 0:
            skipped["con_ventas"] += 1
            continue
        if vis < 50:
            skipped["sin_trafico"] += 1
            continue

        precio  = float(it.get("precio") or 0)
        margen  = it.get("margen_pct")
        if margen is None or margen <= 0:
            skipped["sin_costo"] += 1
            continue

        impacto = impacto_trafico_desperdiciado(vis, precio, float(margen))
        if impacto <= 0:
            continue

        fp = _fingerprint("optimizar_trafico", it["id"])
        out.append(Oportunidad(
            fingerprint=fp,
            tipo="optimizar_trafico",
            descripcion=f"OPTIMIZAR pub con {vis} visitas/0 ventas — {it.get('titulo','')[:55]}",
            impacto_mensual_ars=impacto,
            urgencia="media",
            cta_label="Analizar →",
            cta_url=f"/funnel/{alias}",
            fuente="funnel",
            snapshot={
                "item_id":    it["id"],
                "titulo":     it.get("titulo", ""),
                "visitas_30d": vis,
                "ventas_30d":  vtas,
                "precio":     precio,
                "margen_pct": float(margen),
            },
        ))
    _logger.info("[top_acciones] funnel: %d candidates (skipped=%s)", len(out), skipped)
    return out


def _candidates_stock_critico(alias: str) -> list[Oportunidad]:
    """G — top vendedores quedándose sin stock antes de los próximos 30 días."""
    out: list[Oportunidad] = []
    skipped = {"vel_baja": 0, "stock_amplio": 0, "sin_costo": 0, "sin_stock": 0}

    stock = _load_json(_stock_path(alias))
    if not stock or not stock.get("items"):
        _logger.info("[top_acciones] stock_critico: skip — sin stock_<alias>.json")
        return out

    for it in stock["items"]:
        vel = float(it.get("velocidad") or 0)
        if vel < 0.5:
            skipped["vel_baja"] += 1
            continue

        st = int(it.get("stock") or 0)
        if st <= 0:
            skipped["sin_stock"] += 1
            continue

        dias = it.get("dias_stock")
        if dias is None or dias >= 30:
            skipped["stock_amplio"] += 1
            continue

        precio = float(it.get("precio") or 0)
        margen = it.get("margen_pct")
        if margen is None or margen <= 0:
            skipped["sin_costo"] += 1
            continue

        impacto = impacto_stock_critico(vel, float(dias), precio, float(margen))
        if impacto <= 0:
            continue

        fp = _fingerprint("reponer_stock", it["id"])
        out.append(Oportunidad(
            fingerprint=fp,
            tipo="reponer_stock",
            descripcion=f"REPONER stock — {it.get('titulo','')[:55]} se agota en {dias:.0f}d",
            impacto_mensual_ars=impacto,
            urgencia="critica" if dias <= 7 else "alta",
            cta_label="Ver stock →",
            cta_url=f"/stock/{alias}",
            fuente="stock_critico",
            snapshot={
                "item_id":     it["id"],
                "titulo":      it.get("titulo", ""),
                "velocidad":   vel,
                "stock":       st,
                "dias_stock":  float(dias),
                "ventas_30d":  int(it.get("ventas_30d") or 0),
                "precio":      precio,
                "margen_pct":  float(margen),
            },
        ))
    _logger.info("[top_acciones] stock_critico: %d candidates (skipped=%s)", len(out), skipped)
    return out


def _candidates_buybox(alias: str) -> list[Oportunidad]:
    """F — items perdiendo Buy Box. STUB: requiere snapshot persistido de /salud
    (que aún no existe en disco — el endpoint llama API live). Devuelve [] hasta
    que se implemente persistencia. Tampoco se debe llamar API live desde un cron diario."""
    _logger.info("[top_acciones] buybox: skip — needs persisted salud_<alias>.json snapshot")
    return []


def _candidates_repricing(alias: str) -> list[Oportunidad]:
    """B — wizard de repricing (BB perdidos con costo cargado). STUB: depende del
    snapshot de buybox para no llamar API live en el cron diario."""
    _logger.info("[top_acciones] repricing: skip — depends on buybox snapshot")
    return []


def _candidates_full(alias: str) -> list[Oportunidad]:
    """C — stock muerto en Mercado Full. STUB: requiere snapshot persistido.
    full_manager.run() llama API live, no apto para cron diario."""
    _logger.info("[top_acciones] full: skip — needs persisted full_<alias>.json snapshot")
    # Suprimir hint de unused import: la fórmula está disponible para cuando agreguemos snapshot.
    _ = impacto_stock_muerto_full
    _ = impacto_buybox_perdido
    return []


# ── Motor ─────────────────────────────────────────────────────────────────────

_DETECTORS = [
    ("duplicados",    _candidates_duplicados),
    ("repricing",     _candidates_repricing),
    ("full",          _candidates_full),
    ("ads",           _candidates_ads),
    ("funnel",        _candidates_funnel),
    ("buybox",        _candidates_buybox),
    ("stock_critico", _candidates_stock_critico),
]


def compute(alias: str) -> dict:
    """Corre todos los detectors, rankea por impacto desc, devuelve dict completo
    con top_3, all_candidates, by_source y métricas. NO usa cache (siempre recomputa).
    """
    t0 = time.perf_counter()
    by_source: dict = {}
    all_cands: list[Oportunidad] = []

    for name, fn in _DETECTORS:
        ts = time.perf_counter()
        try:
            cands = fn(alias)
        except Exception as e:
            _logger.exception("[top_acciones] detector %s raised: %s", name, e)
            cands = []
        ms = int((time.perf_counter() - ts) * 1000)
        by_source[name] = {
            "count":       len(cands),
            "tiempo_ms":   ms,
            "candidates":  [asdict(c) for c in cands],
        }
        all_cands.extend(cands)

    dismissed = get_dismissed_fingerprints(alias)
    filtered  = [c for c in all_cands if c.fingerprint not in dismissed]
    filtered.sort(key=lambda c: c.impacto_mensual_ars, reverse=True)

    result = {
        "top_3":             [asdict(c) for c in filtered[:3]],
        "all_candidates":    [asdict(c) for c in filtered],
        "by_source":         by_source,
        "dismissed_count":   len(dismissed),
        "computed_at":       _now_iso(),
        "from_cache":        False,
        "compute_time_ms":   int((time.perf_counter() - t0) * 1000),
        "alias":             alias,
    }
    return result


def top3(alias: str, *, force_recompute: bool = False) -> dict:
    """Devuelve top 3 desde cache si está fresco (<24h), sino recomputa.
    Persiste cache en data/top_acciones_cache_<alias>.json."""
    if not force_recompute:
        cached = _load_json(_cache_path(alias))
        if cached and cached.get("computed_at"):
            try:
                ts = datetime.fromisoformat(cached["computed_at"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - ts
                if age < timedelta(hours=CACHE_TTL_HOURS):
                    cached["from_cache"] = True
                    return cached
            except Exception:
                pass

    result = compute(alias)
    _save_json(_cache_path(alias), result)

    # Primera corrida: dump verbose para postmortem
    if not os.path.exists(_first_run_path(alias)):
        _save_json(_first_run_path(alias), result)
        _logger.warning("[top_acciones] first-run snapshot saved for %s "
                        "(top_3=%d, all=%d, time=%dms)",
                        alias, len(result["top_3"]),
                        len(result["all_candidates"]),
                        result["compute_time_ms"])

    return result
