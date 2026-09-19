"""
Generador de trio — Sprint 5 de la Calculadora de Estrategia de Precios.

Genera los 3 titulos (uno por cluster de busqueda real), descripcion y ficha
tecnica de las publicaciones nuevas del trio, llamando a las piezas ya
existentes de modules/seo_optimizer.py. Regla #1 del proyecto: ese archivo
NUNCA se modifica — este modulo solo importa y llama sus funciones.

`run_full_optimization` (el pipeline "v2" completo) genera UN titulo optimo
para una publicacion; no esta pensado para producir varios titulos que
apunten a clusters de busqueda distintos a proposito. Por eso este modulo
llama directo a las piezas de mas abajo del pipeline (fetch de item/categoria,
armado del prompt, llamada a Claude, parseo, validacion) una vez por cluster,
en vez de pasar por el wrapper completo. Investigado y confirmado antes de
escribir esto: esas funciones son estateless (no dependen de estado de modulo),
son importables tal cual estan.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.seo_optimizer import (  # noqa: E402
    _ancla_from_title,
    _build_synthesis_prompt,
    _call_claude,
    _classify_product_complexity,
    _cluster_keywords,
    _get_category_attributes,
    _get_category_info,
    _get_description,
    _get_item,
    _parse_synthesis,
    get_autosuggest_keywords,
    score_and_classify_keywords,
    validar_sintesis,
)

MIN_QUERY_COUNT_CLUSTER = 1  # volumen minimo (en queries donde aparecio) para usar un cluster


def clusters_de_busqueda(suggestions: list, position_map: dict, max_clusters: int = 3) -> tuple[list, bool]:
    """Agrupa el autosuggest real en hasta `max_clusters` clusters de busqueda
    distintos, ordenados por volumen (mismo criterio que `_cluster_keywords`
    ya usa en el resto del sistema: similitud de tokens, Jaccard >= 0.40).

    Nunca inventa un cluster de mas: si hay menos utiles, devuelve los que hay.
    Devuelve (clusters, alcanzo_el_maximo).
    """
    if not suggestions:
        return [], False
    clusters = _cluster_keywords(suggestions, position_map)
    utiles = [c for c in clusters if c["query_count"] >= MIN_QUERY_COUNT_CLUSTER]
    elegidos = utiles[:max_clusters]
    return elegidos, len(elegidos) >= max_clusters


def _titulo_para_cluster(item_data: dict, description: str, category_attrs: dict,
                         category_name: str, category_path: str, product_type: str,
                         cluster: dict, autosuggest_raw: list, position_map: dict,
                         console=None, modelo_economico: bool = False) -> dict:
    """Genera UN titulo orientado a un cluster de busqueda especifico.

    El representante del cluster se empuja a TIER 1 (mayor prioridad, lo que
    determina la estructura del titulo en el prompt) — asi cada llamada
    produce un titulo distinto, en vez de reconverger siempre al mismo termino
    mas buscado del producto.

    `modelo_economico`: usa Haiku (`_call_claude(..., fast=True)`) en vez de
    Opus. El propio seo_optimizer.py reserva Haiku para analisis/validacion
    estructurada, no para "sintesis creativa, titulos y descripciones
    finales" (su docstring literal) — por eso esto es opt-in, decision
    explicita de Guille caso a caso, no un default silencioso.
    """
    keyword_analysis = score_and_classify_keywords(
        autosuggest_raw, item_data.get("title", ""), [], [], position_map)

    cluster_kws = {cluster["representative"]} | set(cluster["variants"])
    for ka in keyword_analysis:
        if ka["keyword"] in cluster_kws:
            ka["priority_score"] = max(ka.get("priority_score", 0), 999)
    keyword_analysis.sort(key=lambda k: -k.get("priority_score", 0))

    tier1_kw = cluster["representative"]
    ancla = tier1_kw.split()[0] if tier1_kw else _ancla_from_title(item_data.get("title", ""))
    keyword_principal = tier1_kw

    prompt = _build_synthesis_prompt(
        item_data, description, keyword_analysis, category_attrs, category_name,
        root_causes=[], comp_patterns={}, analysis_text="",
        category_path=category_path, tier1_kw=tier1_kw,
    )
    try:
        # SIN reintento automatico (a diferencia de run_full_optimization):
        # cada reintento duplica tiempo Y costo de credito de Claude, y con
        # productos que solo tienen 1 cluster de busqueda (comun) no hay
        # nada que paralelizar entre clusters, asi que el reintento era el
        # factor que mas empujaba el tiempo total por encima del timeout del
        # servidor Y el gasto de credito por click. Pedido explicito de
        # Guille (2026-09-18): "me consumio creditos muy caros". Si la
        # validacion falla, se devuelve tal cual con los errores marcados —
        # el usuario decide si generar de nuevo (con el costo de un nuevo
        # click) o editar el texto a mano en la vista previa.
        raw = _call_claude(prompt, max_tokens=3500, console=console, fast=modelo_economico)
        parsed = _parse_synthesis(raw)
        errores = validar_sintesis(parsed, product_type, [tier1_kw], ancla, keyword_principal)
    except Exception as e:
        return {
            "titulo": "", "descripcion": "", "ficha_attrs": {},
            "cluster_representative": cluster["representative"],
            "cluster_variantes": cluster["variants"],
            "volumen_query_count": cluster["query_count"],
            "tier1_kw": tier1_kw, "ancla": ancla, "keyword_principal": keyword_principal,
            "errores_validacion": [f"No se pudo generar con Claude: {e}"],
        }

    return {
        "titulo": parsed.get("titulo_recomendado", ""),
        "descripcion": parsed.get("descripcion_nueva", ""),
        "ficha_attrs": parsed.get("ficha_attrs", {}),
        "cluster_representative": cluster["representative"],
        "cluster_variantes": cluster["variants"],
        "volumen_query_count": cluster["query_count"],
        "tier1_kw": tier1_kw, "ancla": ancla, "keyword_principal": keyword_principal,
        "errores_validacion": errores,
    }


def generar_titulos_trio(item_id: str, client, console=None, modelo_economico: bool = False) -> dict:
    """Punto de entrada: hasta 3 titulos (uno por cluster de busqueda real)
    para las publicaciones nuevas del trio, mas la descripcion y ficha
    tecnica de cada uno. No escribe nada en ML — es la "vista previa".
    """
    token = client.account.access_token
    item_data = _get_item(item_id, token)
    if not item_data or not item_data.get("id"):
        return {"ok": False, "error": f"No se pudo traer la publicación {item_id}."}

    description = _get_description(item_id, token)
    category_id = item_data.get("category_id", "")
    category_name, category_path = _get_category_info(category_id)
    category_attrs = _get_category_attributes(category_id, token)

    tipo, _justif, _is_fallback, _signals = _classify_product_complexity(
        category_name=category_name,
        attrs_count=len(category_attrs.get("required", [])),
        kw_info_count=0,
        my_price=float(item_data.get("price") or 0),
        avg_comp_price=0.0,
    )

    autosuggest_raw, position_map = get_autosuggest_keywords(item_data.get("title", ""))
    if not autosuggest_raw:
        return {"ok": False, "error": "El autosuggest no devolvió keywords para este producto — sin eso no se pueden generar títulos por cluster."}

    clusters, alcanzo_3 = clusters_de_busqueda(autosuggest_raw, position_map)
    if not clusters:
        return {"ok": False, "error": "No se encontraron clusters de búsqueda con volumen suficiente."}

    # En paralelo, no secuencial: cada cluster es 1-2 llamadas a Claude
    # independientes entre si, y con 3 clusters en serie el tiempo total
    # podia superar el timeout del servidor (gunicorn --timeout 120, ver
    # docs/CEREBRO.md seccion 12). Paralelizar corta el tiempo de punta a
    # punta a lo que tarda el cluster mas lento, no la suma de los tres.
    with ThreadPoolExecutor(max_workers=len(clusters)) as pool:
        resultados = list(pool.map(
            lambda cluster: _titulo_para_cluster(
                item_data, description, category_attrs, category_name,
                category_path, tipo, cluster, autosuggest_raw, position_map, console,
                modelo_economico=modelo_economico),
            clusters,
        ))

    return {
        "ok": True,
        "titulos": resultados,
        "alcanzo_3_clusters": alcanzo_3,
        "category_id": category_id,
        "category_name": category_name,
        "ficha_requerida": category_attrs.get("required", []),
        "product_type": tipo,
        "pictures": item_data.get("pictures", []),
    }


# ── Escalon de cuotas -> (listing_type_id, tags) — ver docs/CEREBRO.md seccion 12 ──
# Confirmado contra documentacion oficial de ML (2026-09-18): el escalon SI se
# puede fijar por API, via el campo `tags` combinado con `listing_type_id`.
CUOTAS_A_TAGS = {
    0:  ("gold_special", []),                    # sin cuotas propias (Batalla)
    4:  ("gold_special", ["pcj-co-funded"]),      # interes bajo, comprador elige 3-12, vendedor paga 4% fijo
    3:  ("gold_pro", ["3x_campaign"]),
    6:  ("gold_pro", []),                          # default de gold_pro, sin tag
    9:  ("gold_pro", ["9x_campaign"]),
    12: ("gold_pro", ["12x_campaign"]),
}


def perfiles_duplicados(perfiles: list) -> list:
    """Bloquea crear 2 publicaciones identicas en tipo+cuotas (spec parrafo 9.5).

    `perfiles`: lista de dicts con al menos {'nombre', 'cuotas'} (0/4/3/6/9/12,
    clave de CUOTAS_A_TAGS). Devuelve los pares de nombres que chocan.
    """
    choques = []
    vistos: dict = {}
    for p in perfiles:
        listing_type, tags = CUOTAS_A_TAGS.get(p.get("cuotas", 0), ("gold_special", []))
        clave = (listing_type, tuple(tags))
        if clave in vistos:
            choques.append((vistos[clave], p["nombre"]))
        else:
            vistos[clave] = p["nombre"]
    return choques


def ficha_faltante(ficha_attrs: dict, atributos_requeridos: list) -> list:
    """Compara la ficha generada contra los atributos obligatorios reales de
    la categoria (`_get_category_attributes`). Devuelve los que faltan — la
    vista previa no deja crear hasta que esta lista este vacia (spec 9.3)."""
    presentes = {str(k).strip().lower() for k in (ficha_attrs or {}).keys()}
    faltan = []
    for attr in atributos_requeridos or []:
        nombre = attr.get("name") or attr.get("id") or ""
        if nombre.strip().lower() not in presentes:
            faltan.append(nombre)
    return faltan


def revalidar_titulo(titulo: str, descripcion: str, product_type: str, tier1_kw: str,
                     ancla: str, keyword_principal: str) -> list:
    """Vuelve a correr validar_sintesis() sobre el titulo/descripcion que van
    a crearse — pedido explicito de Guille (2026-09-18): "no quiero perder
    calidad, quiero que los titulos esten bien". La vista previa ya mostraba
    los errores en rojo, pero nada impedia crear igual si el usuario tocaba
    "Crear" de todas formas; ahora /api/pricing/trio/crear vuelve a validar
    (por si edito el texto a mano en la vista previa, este chequeo es sobre
    lo que realmente se va a mandar a ML, no sobre lo que genero Claude) y
    bloquea la creacion si sigue habiendo errores.
    """
    parsed = {"titulo_recomendado": titulo, "descripcion_nueva": descripcion}
    return validar_sintesis(parsed, product_type, [tier1_kw], ancla, keyword_principal)
