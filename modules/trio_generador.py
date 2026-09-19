"""
Tasas reales de ML por escalon de cuotas + titulo de la publicacion
duplicada del trio — Calculadora de Estrategia de Precios.

El generador de titulos "creativo" (con Claude, uno por cluster de
busqueda) que vivia en este archivo se eliminó el 2026-09-19 a pedido de
Guille: "Generador de trío nunca funciono... prefiero que lo quites de
ahi" — ver docs/CEREBRO.md seccion 12. Lo que quedó fue un pedido más
puntual, el mismo dia: "Medio y Compensa" son publicaciones DISTINTAS de
Batalla (necesitan su propio item_id, no se le puede cambiar el precio a
la misma publicacion tres veces con tres nombres) y para duplicar hace
falta un titulo distinto — pero determinista, reordenando las keywords
reales del autosuggest de ML por relevancia, SIN llamar a Claude
("utilizando autossugets como la informacion mas valiosa... las palabras
mas relevantes en importancia").
"""
from modules.seo_optimizer import _STOPWORDS, get_autosuggest_keywords, score_and_classify_keywords

# ── Escalon de cuotas -> (listing_type_id, tags) — ver docs/CEREBRO.md seccion 12 ──
# Confirmado contra documentacion oficial de ML (2026-09-18): el escalon SI se
# puede fijar por API, via el campo `tags` combinado con `listing_type_id`.
CUOTAS_A_TAGS = {
    0:  ("gold_special", []),                    # sin cuotas propias (Batalla)
    4:  ("gold_special", ["pcj-co-funded"]),      # interes bajo, comprador elige 3-12
    3:  ("gold_pro", ["3x_campaign"]),
    6:  ("gold_pro", []),                          # default de gold_pro, sin tag
    9:  ("gold_pro", ["9x_campaign"]),
    12: ("gold_pro", ["12x_campaign"]),
}


def tasas_reales_por_escalon(client, domain_id: str, precio_referencia: float) -> dict:
    """Comision pura y costo de cuotas real por escalon, consultando
    /sites/MLA/listing_prices EN VIVO para el domain_id real del producto.

    Verificado en vivo 2026-09-19 (cuenta NOVARA, dominio
    HAIR_CLIPPERS_ELECTRIC_SHAVERS_AND_HAIR_TRIMMERS): `percentage_fee` NO
    es la comision pura — ya trae sumado el `financing_add_on_fee` adentro
    (percentage_fee = meli_percentage_fee + financing_add_on_fee, confirmado
    en los 6 escalones). La comision pura es `meli_percentage_fee`, y se
    mantuvo CONSTANTE (16%) en los 6 escalones de esta prueba — el costo de
    cuotas varia por escalon Y por dominio (acá: 0/5/8.9/13.4/17.8/21.6%
    para sin-cuotas/interes-bajo/3/6/9/12 cuotas — el interes bajo dio 5%,
    no el 4% que traía un ejemplo generico de la documentacion. Nunca
    asumir un numero fijo sin consultarlo para el dominio real.

    Devuelve {escalon: {"comision": float|None, "costo_cuotas": float|None}}
    — algun escalon puede faltar si la categoria no lo tiene habilitado.
    """
    import requests as _req
    token = client.account.access_token
    headers = {'Authorization': f'Bearer {token}'}
    precio = max(precio_referencia, 100000)  # arriba del umbral, para que el cargo fijo no distorsione el %
    tasas = {}
    for escalon, (listing_type, tags) in CUOTAS_A_TAGS.items():
        params = {'price': precio, 'listing_type_id': listing_type, 'domain_id': domain_id}
        if tags:
            params['tags'] = tags[0]
        try:
            r = _req.get('https://api.mercadolibre.com/sites/MLA/listing_prices',
                         headers=headers, params=params, timeout=8)
            if r.ok:
                data = r.json()
                d = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
                sfd = d.get('sale_fee_details', {})
                if sfd:
                    tasas[escalon] = {
                        'comision': sfd.get('meli_percentage_fee'),
                        'costo_cuotas': sfd.get('financing_add_on_fee'),
                    }
        except Exception:
            pass
    return tasas


def titulo_variante(titulo_original: str, variante_idx: int) -> dict:
    """Titulo para la publicacion duplicada de Medio (variante_idx=1) o
    Compensa (variante_idx=2): reordena el titulo original alrededor de la
    keyword mejor rankeada por el autosuggest REAL de ML que todavia no lo
    lidera — no genera contenido nuevo, solo reacomoda lo que ya existe.
    Determinista, sin llamar a Claude (gratis, ~1-2s). Medio y Compensa
    reciben keywords lider distintas entre si (candidatas[0] vs [1]).

    Devuelve {"titulo": str, "keyword_usada": str|None, "keywords_rankeadas": list}
    — si el autosuggest no trae nada util, devuelve el titulo original sin
    tocar y `keywords_rankeadas` vacia. `keywords_rankeadas` es SIEMPRE dato
    real: posicion y cantidad de queries en las que aparecio, tal cual las
    devolvio el autosuggest de ML — nada inventado ni estimado.
    """
    autosuggest_raw, position_map = get_autosuggest_keywords(titulo_original)
    if not autosuggest_raw:
        return {"titulo": titulo_original, "keyword_usada": None, "keywords_rankeadas": []}

    keywords_rankeadas = _rankear_por_autosuggest(autosuggest_raw, position_map)

    ranked = score_and_classify_keywords(autosuggest_raw, titulo_original, [], [], position_map)

    palabras_originales = titulo_original.split()
    lider_actual = " ".join(palabras_originales[:3]).lower()

    candidatas = [r["keyword"] for r in ranked
                  if r["compatibilidad"] in ("alta", "media") and r["keyword"].lower() != lider_actual]
    if not candidatas:
        return {"titulo": titulo_original, "keyword_usada": None, "keywords_rankeadas": keywords_rankeadas}

    idx = min(variante_idx - 1, len(candidatas) - 1)
    elegida = candidatas[idx]

    # Solo se agregan palabras del resto del titulo que aporten algo — sin
    # esto quedaban conectores sueltos al final ("De ... Para") cuando la
    # keyword elegida ya cubria las palabras con contenido real.
    palabras_elegida = set(elegida.lower().split())
    resto = [w for w in palabras_originales
             if w.lower() not in palabras_elegida and w.lower() not in _STOPWORDS and len(w) > 3]
    nuevo = f"{elegida.title()} {' '.join(resto)}".strip()
    if len(nuevo) > 60:  # limite de titulo de ML
        nuevo = nuevo[:60].rsplit(" ", 1)[0]
    return {"titulo": nuevo, "keyword_usada": elegida, "keywords_rankeadas": keywords_rankeadas}


def _rankear_por_autosuggest(autosuggest_raw: list, position_map: dict, top_n: int = 10) -> list:
    """Lista de keywords con su relevancia, calculada SOLO a partir de la
    posicion real que devolvio el autosuggest de ML (`best_pos` sobre el
    total de sugerencias) y en cuantas de las hasta 4 queries derivadas del
    titulo aparecio (`query_count`). Nada estimado ni inventado: es la
    misma posicion que ya uso `get_autosuggest_keywords` para armar
    `position_map`. No es volumen de busqueda (ML no lo expone via
    autosuggest) — es relevancia por posicion, aclarado en el label del
    lado del frontend.
    """
    total = len(autosuggest_raw)
    filas = []
    for kw in autosuggest_raw:
        pm = position_map.get(kw, {})
        pos = pm.get("best_pos", total)
        relevancia_pct = round(max(0.0, (1 - (pos - 1) / total)) * 100) if total else 0
        filas.append({
            "keyword": kw, "relevancia_pct": relevancia_pct,
            "posicion": pos, "de": total, "query_count": pm.get("query_count", 1),
        })
    filas.sort(key=lambda f: -f["relevancia_pct"])
    return filas[:top_n]
