"""
Sprint 3.3 — Replicar Patrón Ganador.

Cierra el loop de optimización: cuando el Veredicto IA (Sprint 3.2) declara una
optimización como "ganadora" con recomendación "replicar", este módulo:

  1. Extrae el patrón de cambios que funcionaron (keywords agregadas al título,
     atributos completados, ajustes de precio).
  2. Detecta otros productos del catálogo en la misma categoría con título
     similar (Jaccard sobre tokens) que TODAVÍA NO usan el patrón.
  3. Genera una propuesta de réplica adaptada al producto destino vía Claude
     Haiku 4.5 (modelo barato — adaptación, no análisis profundo).
  4. Persiste decisiones del usuario (aplicada / descartada) para no volver a
     ofrecer el mismo destino para el mismo origen.

NO toca `modules/seo_optimizer.py`. NO modifica monitor_evolucion.json.
Solo CONSUME veredictos vigentes con recomendación "replicar".

API pública:
    listar_oportunidades(alias, data_dir) → list[dict]
    detectar_productos_similares(alias, item_id_origen, client, data_dir,
                                 max_resultados=10) → list[dict]
    extraer_patron_ganador(veredicto, optimizacion_origen, monitor_entry) → dict
    generar_replica_haiku(patron, item_destino_data, data_dir) → dict
    registrar_decision(alias, item_origen, item_destino, accion, data_dir,
                       payload=None) → bool
    obtener_replicas(alias, data_dir) → dict

Reglas críticas:
- NO replica si el destino ya tiene optimización <30 días.
- NO replica si el destino fue marcado como duplicado/canibalizando.
- NO regenera preview si ya hay una pendiente <7d (idempotencia).
- Hard cap costo Haiku: $5/mes (revisado por endpoint, no por cron).
- Threshold mínimo de similitud: 0.30 (Jaccard) — debajo de eso, no se ofrece.
- Aprobación 1-por-1 (decisión Sprint 3.3): nunca aplica masivo.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from modules.alertas_estado import fingerprint as _alert_fingerprint

_logger = logging.getLogger(__name__)

# ── Constantes del sprint ────────────────────────────────────────────────────

SIMILARITY_MIN_JACCARD       = 0.30   # Debajo de esto, no es "similar"
SIMILARITY_BOOST_CATEGORIA   = 0.15   # Bonus si comparten category_id
MAX_CANDIDATOS_DEFAULT       = 10
DIAS_BLOQUEO_OPTIMIZACION    = 30     # No replicar a productos optimizados <30d
TTL_DECISION_DESCARTADA_DIAS = 60     # Decisiones "descartada" se purgan a los 60d
TTL_PREVIEW_PENDIENTE_DIAS   = 7      # Preview pendiente vigente 7d

# Hard cap del feature (Haiku es barato, ~$0.005 por preview)
HARD_CAP_USD_MENSUAL         = 5.0

# Modelo elegido: Haiku (adaptación textual, no análisis profundo)
MODELO_DEFAULT  = "claude-haiku-4-5-20251001"
PROMPT_VERSION  = "1.0"
MAX_OUTPUT_TOKENS = 1024

ACCIONES_VALIDAS = ("aplicada", "descartada", "preview_generado", "pendiente")

# Espejado de modules/veredicto_optimizacion.py (sin importar para evitar circ)
_TOKEN_PRICES_USD_PER_M = {
    "claude-opus-4-6":           {"input": 15.0,  "output": 75.0},
    "claude-haiku-4-5-20251001": {"input":  0.25, "output":  1.25},
    "claude-haiku-4-5":          {"input":  0.25, "output":  1.25},
}

_ML_API = "https://api.mercadolibre.com"

# Stopwords para tokenización de títulos ML (artículos, preposiciones, etc.)
_STOPWORDS_ES = {
    "de", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "o",
    "con", "sin", "para", "por", "en", "del", "al", "a", "x", "tu", "tus",
    "mi", "su", "lo", "ml", "mla", "ar", "arg", "argentina",
}


# ── Persistencia ─────────────────────────────────────────────────────────────

def _replicas_path(alias: str, data_dir: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", alias or "default")
    return os.path.join(data_dir, f"replicas_{safe}.json")


def _load_json(path: str, default=None):
    """Lee JSON desde DB (Render) o filesystem (local). Tolerante a errores."""
    try:
        from core.db_storage import db_load
        result = db_load(path)
        return result if result is not None else default
    except Exception as e:
        _logger.warning("[replicador] failed loading %s: %s", path, e)
        return default


def _save_json(path: str, data) -> None:
    try:
        from core.db_storage import db_save
        db_save(path, data)
    except Exception as e:
        _logger.error("[replicador] failed saving %s: %s", path, e)
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_fecha(s: str):
    """Tolera formatos: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', ISO con/sin TZ."""
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


def _dias_desde(fecha_str: str) -> int:
    dt = _parse_fecha(fecha_str)
    if not dt:
        return 99999
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)
    return max(0, (datetime.now() - dt).days)


# ── Tokenización y similitud ─────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase + strip de caracteres no alfanuméricos. NO toca acentos
    (ML usa títulos con acentos y nuestros competidores también)."""
    if not text:
        return ""
    return re.sub(r"[^\w\sáéíóúñ]", " ", str(text).lower(), flags=re.UNICODE)


def _tokenize(text: str) -> set:
    """Devuelve set de tokens significativos (sin stopwords, len>=3)."""
    norm = _normalize(text)
    tokens = {t for t in norm.split() if len(t) >= 3 and t not in _STOPWORDS_ES}
    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return round(inter / union, 4) if union else 0.0


# ── Fingerprint ──────────────────────────────────────────────────────────────

def fingerprint_replica(alias: str, item_origen: str, item_destino: str) -> str:
    """Hash idempotente para una sugerencia de réplica."""
    return _alert_fingerprint("replica", alias, item_origen, item_destino)


# ── Carga del store de réplicas ──────────────────────────────────────────────

def _cargar_store(alias: str, data_dir: str, purgar: bool = True) -> dict:
    """Carga replicas_<alias>.json. Estructura:
        {"items": [{fingerprint, item_origen, item_destino, accion, fecha,
                    payload, _debug}, ...]}
    """
    raw = _load_json(_replicas_path(alias, data_dir), default={"items": []}) or {"items": []}
    if not isinstance(raw, dict) or "items" not in raw:
        return {"items": []}

    if not purgar:
        return raw

    cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DECISION_DESCARTADA_DIAS)
    antes = len(raw.get("items", []))
    items = []
    for it in raw.get("items", []):
        if it.get("accion") == "descartada":
            ts = _parse_fecha(it.get("fecha") or "")
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts and ts < cutoff:
                continue
        items.append(it)
    if len(items) != antes:
        raw["items"] = items
        _save_json(_replicas_path(alias, data_dir), raw)
        _logger.info("[replicador] purgadas %d decisiones expiradas",
                     antes - len(items))
    return raw


def _persistir_store(alias: str, data_dir: str, store: dict) -> None:
    _save_json(_replicas_path(alias, data_dir), store)


def obtener_replicas(alias: str, data_dir: str) -> dict:
    """Devuelve el store completo de réplicas para una cuenta."""
    return _cargar_store(alias, data_dir)


def _buscar_decision(store: dict, item_origen: str, item_destino: str) -> dict | None:
    """Devuelve la decisión más reciente para (origen, destino) o None."""
    encontrado = None
    fecha_max = ""
    for it in store.get("items", []):
        if (it.get("item_origen") == item_origen
                and it.get("item_destino") == item_destino):
            f = it.get("fecha", "")
            if f >= fecha_max:
                fecha_max = f
                encontrado = it
    return encontrado


# ── Lookup de monitor + optimizaciones ───────────────────────────────────────

def _buscar_entry_monitor(data_dir: str, alias: str, item_id: str) -> dict | None:
    mon_path = os.path.join(data_dir, "monitor_evolucion.json")
    mon = _load_json(mon_path, default={"items": []}) or {"items": []}
    if not isinstance(mon, dict):
        return None
    for it in mon.get("items", []):
        if it.get("item_id") == item_id and it.get("alias") == alias:
            return it
    return None


def _buscar_optimizacion(data_dir: str, alias: str, item_id: str) -> dict | None:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", alias or "default")
    opt_path = os.path.join(data_dir, f"optimizaciones_{safe}.json")
    data = _load_json(opt_path, default={}) or {}
    for o in (data.get("optimizaciones") or []):
        if o.get("item_id") == item_id:
            return o
    return None


# ── Lookup de veredictos ganadores ───────────────────────────────────────────

def _veredictos_ganadores(alias: str, data_dir: str,
                          ventana_dias: int = 90) -> list[dict]:
    """Devuelve veredictos vigentes con recomendacion='replicar' en la ventana."""
    vpath = os.path.join(data_dir, "veredictos_optimizacion.json")
    store = _load_json(vpath, default={"items": []}) or {"items": []}
    if not isinstance(store, dict):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=ventana_dias)
    out = []
    for v in store.get("items", []):
        if v.get("alias") != alias:
            continue
        if v.get("estado") != "vigente":
            continue
        if v.get("recomendacion") != "replicar":
            continue
        if v.get("veredicto") != "ganadora":
            continue
        ts = _parse_fecha(v.get("fecha_veredicto") or "")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if not ts or ts < cutoff:
            continue
        out.append(v)
    out.sort(key=lambda d: d.get("score_exito", 0), reverse=True)
    return out


# ── Extracción de patrón ganador ─────────────────────────────────────────────

def extraer_patron_ganador(veredicto: dict,
                           optimizacion_origen: dict | None,
                           monitor_entry: dict | None) -> dict:
    """Extrae el patrón concreto de cambios que funcionaron en una optimización.

    Returns:
      {
        "item_origen":             str,
        "titulo_antes":            str,
        "titulo_despues":          str,
        "keywords_agregadas":      list[str],   # tokens nuevos en título
        "keywords_removidas":      list[str],
        "aplicado":                list[str],   # ['titulo', 'descripcion', 'atributos (5)']
        "descripcion_estilo":      str | None,  # primeras 280 chars de la nueva
        "atributos_completados":   bool,
        "veredicto_score":         int,
        "metricas_clave":          list[str],   # qué movió: visitas/ventas/etc
        "razonamiento_origen":     str,         # razonamiento del veredicto
        "fecha_optimizacion":      str,
      }
    """
    titulo_antes   = (monitor_entry or {}).get("titulo_antes", "") if monitor_entry else ""
    titulo_despues = (monitor_entry or {}).get("titulo_despues", "") if monitor_entry else ""
    aplicado_list  = (monitor_entry or {}).get("applied", []) if monitor_entry else []

    # Diff de keywords: tokens en "después" que no están en "antes"
    tokens_antes   = _tokenize(titulo_antes)
    tokens_despues = _tokenize(titulo_despues)
    kw_agregadas = sorted(tokens_despues - tokens_antes)
    kw_removidas = sorted(tokens_antes - tokens_despues)

    # Estilo de descripción: primeros 280 chars de la nueva (sirve como guía)
    desc_estilo = None
    if optimizacion_origen:
        desc_nueva = optimizacion_origen.get("descripcion_nueva") or ""
        if desc_nueva:
            desc_estilo = desc_nueva[:280].strip()

    atributos_completados = any("atributos" in str(a).lower() for a in aplicado_list)

    return {
        "item_origen":           (monitor_entry or {}).get("item_id") or veredicto.get("item_id"),
        "titulo_antes":          titulo_antes,
        "titulo_despues":        titulo_despues,
        "keywords_agregadas":    kw_agregadas,
        "keywords_removidas":    kw_removidas,
        "aplicado":              list(aplicado_list),
        "descripcion_estilo":    desc_estilo,
        "atributos_completados": atributos_completados,
        "veredicto_score":       int(veredicto.get("score_exito") or 0),
        "metricas_clave":        list(veredicto.get("metricas_clave") or []),
        "razonamiento_origen":   veredicto.get("razonamiento") or "",
        "fecha_optimizacion":    veredicto.get("fecha_optimizacion") or "",
    }


# ── Detección de productos similares ─────────────────────────────────────────

def _fetch_user_listings(client, max_items: int = 100) -> list[str]:
    """Devuelve hasta `max_items` IDs de publicaciones activas del seller."""
    try:
        resp = client.get_my_listings(limit=min(max_items, 50), offset=0, status="active")
        ids = list(resp.get("results") or [])
        # Si hay >50 y queremos más, paginar
        total = resp.get("paging", {}).get("total", len(ids))
        offset = 50
        while len(ids) < max_items and offset < total:
            resp2 = client.get_my_listings(limit=50, offset=offset, status="active")
            new_ids = resp2.get("results") or []
            if not new_ids:
                break
            ids.extend(new_ids)
            offset += 50
        return ids[:max_items]
    except Exception as e:
        _logger.warning("[replicador] error fetching listings: %s", e)
        return []


def _fetch_items_multiget(item_ids: list[str], token: str) -> list[dict]:
    """Multiget público (max 20 por call) — más rápido que N llamadas individuales."""
    import requests
    out = []
    for i in range(0, len(item_ids), 20):
        chunk = item_ids[i:i+20]
        try:
            r = requests.get(
                f"{_ML_API}/items",
                params={"ids": ",".join(chunk),
                        "attributes": "id,title,category_id,price,sold_quantity,permalink,status"},
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=8,
            )
            if r.status_code == 200:
                for entry in r.json():
                    body = entry.get("body") or {}
                    if body and body.get("id"):
                        out.append(body)
        except Exception as e:
            _logger.warning("[replicador] multiget chunk failed: %s", e)
        time.sleep(0.15)
    return out


def detectar_productos_similares(alias: str, item_id_origen: str,
                                 client, data_dir: str,
                                 max_resultados: int = MAX_CANDIDATOS_DEFAULT) -> list[dict]:
    """Detecta productos del catálogo del seller similares al origen.

    Criterios (configuración A — recomendada):
      - Misma categoría ML (boost +0.15 a similitud)
      - Solapamiento de keywords en título (Jaccard ≥ 0.30)
      - NO optimizado en últimos 30 días
      - NO ya decidido (aplicada o descartada) para este origen

    Returns lista ordenada por similitud desc:
      [{item_id, titulo, category_id, similitud, mismo_categoria,
        bloqueado_por, motivo_inclusion}, ...]
    """
    # 1) Datos del producto origen
    try:
        origen_data = client.get_item(item_id_origen)
    except Exception as e:
        _logger.warning("[replicador] no pude obtener origen %s: %s",
                        item_id_origen, e)
        return []
    titulo_origen = origen_data.get("title") or ""
    category_origen = origen_data.get("category_id") or ""
    tokens_origen = _tokenize(titulo_origen)
    if not tokens_origen:
        return []

    # 2) Lista completa de items activos del seller (max 200 — anti-runaway)
    todos_ids = _fetch_user_listings(client, max_items=200)
    todos_ids = [i for i in todos_ids if i != item_id_origen]
    if not todos_ids:
        return []

    # 3) Multiget para tener title + category + sold
    token = client.account.access_token if hasattr(client, "account") else ""
    items_full = _fetch_items_multiget(todos_ids, token=token)

    # 4) Optimizaciones recientes para excluir
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", alias or "default")
    opt_path = os.path.join(data_dir, f"optimizaciones_{safe}.json")
    opt_data = _load_json(opt_path, default={}) or {}
    opt_recientes = {
        o.get("item_id"): o.get("fecha", "")
        for o in (opt_data.get("optimizaciones") or [])
        if _dias_desde(o.get("fecha", "")) < DIAS_BLOQUEO_OPTIMIZACION
    }

    # 5) Decisiones ya registradas para este origen
    store = _cargar_store(alias, data_dir)
    decisiones_origen = {
        it["item_destino"]: it.get("accion")
        for it in store.get("items", [])
        if it.get("item_origen") == item_id_origen
    }

    # 6) Scoring
    candidatos: list[dict] = []
    for it in items_full:
        item_id = it.get("id")
        if not item_id or item_id == item_id_origen:
            continue
        titulo = it.get("title") or ""
        cat = it.get("category_id") or ""
        tokens = _tokenize(titulo)
        if not tokens:
            continue
        jacc = _jaccard(tokens_origen, tokens)
        misma_cat = (cat == category_origen and cat != "")
        score = jacc + (SIMILARITY_BOOST_CATEGORIA if misma_cat else 0)

        # Filtro mínimo de similitud
        if score < SIMILARITY_MIN_JACCARD:
            continue

        # Bloqueos
        bloqueado_por = None
        if item_id in opt_recientes:
            dias = _dias_desde(opt_recientes[item_id])
            bloqueado_por = f"Optimizado hace {dias}d (esperar {DIAS_BLOQUEO_OPTIMIZACION - dias}d más)"
        elif item_id in decisiones_origen:
            acc = decisiones_origen[item_id]
            if acc == "aplicada":
                bloqueado_por = "Ya replicaste el patrón a este destino"
            elif acc == "descartada":
                bloqueado_por = "Descartado previamente para este origen"

        # Motivo de inclusión legible
        kw_compartidas = sorted(tokens_origen & tokens)[:5]
        motivo = (f"{int(jacc * 100)}% keywords comunes "
                  f"({', '.join(kw_compartidas[:3])}…)" if kw_compartidas
                  else f"{int(jacc * 100)}% similitud")

        candidatos.append({
            "item_id":          item_id,
            "titulo":           titulo,
            "category_id":      cat,
            "price":            it.get("price"),
            "sold_quantity":    it.get("sold_quantity") or 0,
            "permalink":        it.get("permalink"),
            "similitud":        round(score, 3),
            "jaccard":          jacc,
            "mismo_categoria":  misma_cat,
            "kw_compartidas":   kw_compartidas,
            "bloqueado_por":    bloqueado_por,
            "motivo_inclusion": motivo,
        })

    # Ordenar: no bloqueados primero, luego por score desc
    candidatos.sort(key=lambda c: (c["bloqueado_por"] is not None, -c["similitud"]))
    return candidatos[:max_resultados]


# ── Listado de oportunidades agregadas ───────────────────────────────────────

def listar_oportunidades(alias: str, data_dir: str) -> list[dict]:
    """Devuelve veredictos ganadores con metadata enriquecida (no busca similares,
    eso es on-demand para no consumir API ML innecesariamente).

    Returns:
      [{item_origen, titulo_producto, fecha_veredicto, score_exito,
        razonamiento, metricas_clave, replicas_aplicadas, replicas_descartadas},
       ...]
    """
    ganadores = _veredictos_ganadores(alias, data_dir)
    store = _cargar_store(alias, data_dir)

    # Conteo de decisiones por origen
    counts = {}
    for it in store.get("items", []):
        ori = it.get("item_origen")
        if not ori:
            continue
        counts.setdefault(ori, {"aplicada": 0, "descartada": 0, "preview": 0})
        acc = it.get("accion")
        if acc == "aplicada":
            counts[ori]["aplicada"] += 1
        elif acc == "descartada":
            counts[ori]["descartada"] += 1
        elif acc == "preview_generado":
            counts[ori]["preview"] += 1

    out = []
    for v in ganadores:
        item_id = v.get("item_id")
        c = counts.get(item_id, {"aplicada": 0, "descartada": 0, "preview": 0})
        out.append({
            "item_origen":         item_id,
            "titulo_producto":     v.get("titulo_producto") or "",
            "fecha_veredicto":     v.get("fecha_veredicto") or "",
            "fecha_optimizacion":  v.get("fecha_optimizacion") or "",
            "score_exito":         int(v.get("score_exito") or 0),
            "razonamiento":        v.get("razonamiento") or "",
            "metricas_clave":      list(v.get("metricas_clave") or []),
            "replicas_aplicadas":  c["aplicada"],
            "replicas_descartadas": c["descartada"],
            "previews_generados":  c["preview"],
        })
    return out


# ── Generación de réplica vía Claude Haiku ───────────────────────────────────

def _log_token_usage_local(data_dir: str, funcion: str, modelo: str,
                           tokens_in: int, tokens_out: int) -> float:
    rates = _TOKEN_PRICES_USD_PER_M.get(modelo, {"input": 0.25, "output": 1.25})
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
        _logger.error("[replicador] no pude loggear tokens: %s", e)
    return round(costo, 6)


def costo_mes_actual_usd(data_dir: str,
                         feature_substr: str = "Replicador IA") -> float:
    path = os.path.join(data_dir, "token_log.json")
    log = _load_json(path, default={"entries": []}) or {"entries": []}
    if not isinstance(log, dict):
        return 0.0
    ahora = datetime.now()
    año, mes = ahora.year, ahora.month
    acc = 0.0
    for e in log.get("entries", []):
        if feature_substr not in (e.get("funcion") or ""):
            continue
        ts = _parse_fecha(e.get("ts") or "")
        if not ts or ts.year != año or ts.month != mes:
            continue
        try:
            acc += float(e.get("usd") or 0)
        except (TypeError, ValueError):
            continue
    return round(acc, 4)


def supera_cap_mensual(data_dir: str) -> tuple:
    actual = costo_mes_actual_usd(data_dir)
    return (actual >= HARD_CAP_USD_MENSUAL, actual)


def _construir_prompt_replica(patron: dict, item_destino: dict) -> str:
    """Prompt compacto para Haiku — adaptación textual, no análisis profundo.

    ~600-900 tokens input → ~400-600 output → ~$0.005 por preview.
    """
    titulo_actual = (item_destino.get("title") or "")[:120]
    cat = item_destino.get("category_id") or "n/d"
    kw_agregar = ", ".join(patron.get("keywords_agregadas") or []) or "(ninguna)"
    kw_remover = ", ".join(patron.get("keywords_removidas") or []) or "(ninguna)"
    score = patron.get("veredicto_score", 0)
    metricas = ", ".join(patron.get("metricas_clave") or []) or "métricas múltiples"

    desc_estilo = patron.get("descripcion_estilo") or ""
    desc_block = (
        f"\nESTILO DE DESCRIPCIÓN GANADORA (primeros 280 chars de la del origen):\n"
        f"---\n{desc_estilo}\n---\n"
        if desc_estilo else ""
    )

    return f"""Sos un copywriter de MercadoLibre Argentina. Te paso un patrón ganador validado por data real (un producto cuya optimización SEO subió score {score}/100, mejorando: {metricas}). Tu tarea: ADAPTAR ese patrón al producto destino, manteniendo el espíritu pero respetando las particularidades del nuevo producto.

REGLAS DURAS:
1. NO copies el título del origen literal — adaptá las keywords ganadoras al producto nuevo.
2. NO inventes atributos del producto destino que no estén en su título actual.
3. Título máx 60 caracteres (límite ML).
4. Descripción: prosa fluida, sin emojis, sin checklists, primer párrafo arranca con la keyword principal.
5. Si el producto destino es de categoría muy distinta y el patrón no aplica, devolvé `aplicable: false` con razón.

═══════════════════════════════════════════════
PATRÓN GANADORA (origen):
  Título antes:   {patron.get("titulo_antes", "")[:120]}
  Título después: {patron.get("titulo_despues", "")[:120]}
  Keywords AGREGADAS clave: {kw_agregar}
  Keywords REMOVIDAS:       {kw_remover}
  Atributos completados:    {"sí" if patron.get("atributos_completados") else "no"}
  Veredicto IA: {patron.get("razonamiento_origen", "")[:300]}
{desc_block}
PRODUCTO DESTINO:
  ID: {item_destino.get("id")}
  Título actual: {titulo_actual}
  Categoría: {cat}
  Precio: {item_destino.get("price", "n/d")}
═══════════════════════════════════════════════

DEVOLVÉ ÚNICAMENTE JSON VÁLIDO (sin markdown, sin ```), con esta estructura:
{{
  "aplicable": true | false,
  "razon_no_aplicable": "<solo si aplicable=false, explicar en 1 oración>",
  "titulo_propuesto": "<máx 60 chars, adaptado>",
  "titulo_motivo": "<1-2 oraciones explicando qué del patrón ganador se aplicó>",
  "descripcion_propuesta": "<descripción nueva, mismo estilo, sin emojis, sin checklists, prosa fluida>",
  "cambios_clave": ["<lista de 2-4 cambios concretos respecto al título actual>"],
  "confianza": <int 0-100, qué tan probable es que el patrón funcione acá>
}}"""


def _parse_respuesta_haiku(texto: str) -> dict:
    """Parsea JSON de Haiku. Tolerante a ```json fence``` y a texto antes/después."""
    import json as _json
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", texto, re.DOTALL)
    candidato = fence.group(1) if fence else texto
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

    # Validaciones
    if not isinstance(obj.get("aplicable"), bool):
        raise ValueError(f"Campo 'aplicable' inválido: {obj.get('aplicable')!r}")
    if obj.get("aplicable"):
        for campo in ("titulo_propuesto", "descripcion_propuesta"):
            if not obj.get(campo) or not isinstance(obj[campo], str):
                raise ValueError(f"Falta o inválido: {campo}")
        # Truncar título a 60 chars (límite ML)
        if len(obj["titulo_propuesto"]) > 60:
            obj["titulo_propuesto"] = obj["titulo_propuesto"][:60].rstrip()
            obj["_truncado"] = True
    if not isinstance(obj.get("cambios_clave"), list):
        obj["cambios_clave"] = []
    conf = obj.get("confianza")
    if not isinstance(conf, (int, float)) or not (0 <= conf <= 100):
        obj["confianza"] = 50
    return obj


def generar_replica_haiku(patron: dict, item_destino_data: dict,
                          data_dir: str) -> dict:
    """Genera la propuesta de réplica para un destino vía Claude Haiku.

    Args:
      patron: dict de extraer_patron_ganador().
      item_destino_data: dict con al menos {id, title, category_id, price}.
      data_dir: ruta a /data del proyecto.

    Returns:
      dict con shape:
      {
        "ok": bool,
        "aplicable": bool,
        "titulo_propuesto": str,
        "descripcion_propuesta": str,
        "cambios_clave": list[str],
        "confianza": int,
        "_debug": {tokens_in, tokens_out, cost_usd, modelo, prompt_version, duracion_ms}
      }

    Lanza excepción si la API key no está, si Claude falla o si el JSON es inválido.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no configurada en el entorno.")

    from anthropic import Anthropic  # type: ignore
    client = Anthropic(api_key=api_key)

    prompt = _construir_prompt_replica(patron, item_destino_data)
    t0 = datetime.now()
    msg = client.messages.create(
        model=MODELO_DEFAULT,
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    duracion_ms = int((datetime.now() - t0).total_seconds() * 1000)

    texto = ""
    for block in (msg.content or []):
        if getattr(block, "type", "") == "text":
            texto += block.text
    if not texto:
        raise RuntimeError(f"Haiku devolvió respuesta vacía. Stop reason: {msg.stop_reason}")

    parsed = _parse_respuesta_haiku(texto)

    tokens_in = msg.usage.input_tokens
    tokens_out = msg.usage.output_tokens
    costo = _log_token_usage_local(
        data_dir,
        funcion="Replicador IA — Generar preview",
        modelo=MODELO_DEFAULT,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )

    return {
        "ok":                    True,
        "aplicable":             parsed.get("aplicable", False),
        "razon_no_aplicable":    parsed.get("razon_no_aplicable", ""),
        "titulo_propuesto":      parsed.get("titulo_propuesto", ""),
        "titulo_motivo":         parsed.get("titulo_motivo", ""),
        "descripcion_propuesta": parsed.get("descripcion_propuesta", ""),
        "cambios_clave":         parsed.get("cambios_clave", []),
        "confianza":             int(parsed.get("confianza", 50)),
        "truncado":              parsed.get("_truncado", False),
        "_debug": {
            "timestamp":      datetime.now().isoformat(timespec="seconds"),
            "tokens_in":      tokens_in,
            "tokens_out":     tokens_out,
            "cost_usd":       costo,
            "duracion_ms":    duracion_ms,
            "modelo":         MODELO_DEFAULT,
            "prompt_version": PROMPT_VERSION,
        },
    }


# ── Registro de decisiones ───────────────────────────────────────────────────

def registrar_decision(alias: str, item_origen: str, item_destino: str,
                       accion: str, data_dir: str,
                       payload: dict | None = None) -> bool:
    """Registra la decisión del usuario sobre una sugerencia de réplica.

    Args:
      accion: 'aplicada' | 'descartada' | 'preview_generado' | 'pendiente'
      payload: opcional, ej. {titulo_propuesto, descripcion_propuesta, ...}
               para previews_generados; o {razon} para descartadas.

    Returns True si se registró nuevo, False si reemplazó uno existente.
    """
    if accion not in ACCIONES_VALIDAS:
        raise ValueError(f"Acción inválida: {accion!r}. Esperaba {ACCIONES_VALIDAS}")

    store = _cargar_store(alias, data_dir, purgar=False)
    fp = fingerprint_replica(alias, item_origen, item_destino)
    nueva = {
        "fingerprint":   fp,
        "alias":         alias,
        "item_origen":   item_origen,
        "item_destino":  item_destino,
        "accion":        accion,
        "fecha":         _now_iso(),
        "payload":       payload or {},
    }
    items = store.get("items", [])
    es_nueva = True
    for i, it in enumerate(items):
        if (it.get("item_origen") == item_origen
                and it.get("item_destino") == item_destino):
            items[i] = nueva
            es_nueva = False
            break
    if es_nueva:
        items.append(nueva)
    store["items"] = items
    _persistir_store(alias, data_dir, store)
    return es_nueva


def obtener_decision(alias: str, item_origen: str, item_destino: str,
                     data_dir: str) -> dict | None:
    """Devuelve la decisión más reciente para (origen, destino) o None."""
    store = _cargar_store(alias, data_dir)
    return _buscar_decision(store, item_origen, item_destino)


__all__ = [
    "extraer_patron_ganador",
    "detectar_productos_similares",
    "generar_replica_haiku",
    "listar_oportunidades",
    "registrar_decision",
    "obtener_decision",
    "obtener_replicas",
    "fingerprint_replica",
    "costo_mes_actual_usd",
    "supera_cap_mensual",
    "SIMILARITY_MIN_JACCARD",
    "SIMILARITY_BOOST_CATEGORIA",
    "MAX_CANDIDATOS_DEFAULT",
    "DIAS_BLOQUEO_OPTIMIZACION",
    "HARD_CAP_USD_MENSUAL",
    "MODELO_DEFAULT",
    "PROMPT_VERSION",
]
