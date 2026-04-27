"""
Motor SEO v3 — Sistema Profesional de Optimización para MercadoLibre Argentina
===============================================================================

7 módulos funcionales:
  M1  KEYWORD DISCOVERY    — Autosuggest real + scoring compuesto + clasificación de intención
  M2  POSITION TRACKER     — Posición actual por keyword + gap vs top 3 + saturación
  M3  COMPETITOR INTEL     — Análisis profundo: atributos, patrones, estructuras de título
  M4  ROOT CAUSE ENGINE    — Causas reales por qué no rankea, ordenadas por impacto
  M5  DIFFICULTY SCORE     — Dificultad real de posicionamiento
  M6  OPTIMIZATION ENGINE  — Claude: 3 títulos (MAX SEO / BALANCE / DIFERENCIADOR) + ficha + descripción
  M7  CONFIDENCE LAYER     — Evidencia y nivel de confianza por recomendación

REGLAS:
  ✓  Solo datos observables reales (API ML, autosuggest, competidores)
  ✓  Inferencias claramente marcadas como tales
  ✗  NO inventar volumen de búsqueda
  ✗  NO asumir pesos del algoritmo interno
  ✗  NO generar atributos falsos
"""

import re
import time
import unicodedata
import os
import requests
import anthropic

from core.ml_client import MLClient

# Limpiar el API key de caracteres invisibles (newline, espacios) que rompen httpcore
_raw_key = os.environ.get('ANTHROPIC_API_KEY', '')
if _raw_key != _raw_key.strip():
    os.environ['ANTHROPIC_API_KEY'] = _raw_key.strip()

# ── Constantes ────────────────────────────────────────────────────────────────

ML_SITE   = "MLA"
ML_API    = "https://api.mercadolibre.com"
ML_AS_URL = "https://http2.mlstatic.com/resources/sites/MLA/autosuggest"

_STOPWORDS = {
    "de", "para", "con", "sin", "y", "el", "la", "los", "las", "un", "una",
    "en", "a", "por", "al", "del", "se", "su", "es", "que", "o", "e",
    "x", "cm", "ml", "gr", "kg", "mm", "lt",
}

_AS_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer":         "https://www.mercadolibre.com.ar/",
    "Accept":          "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Origin":          "https://www.mercadolibre.com.ar",
}

_TRANSACTIONAL_SIGNALS = {
    "comprar", "precio", "barato", "economico", "oferta", "descuento",
    "original", "nuevo", "envio gratis", "cuotas", "mercadopago", "venta",
}
_COMPARATIVE_SIGNALS = {
    "mejor", "vs", "versus", "cual", "comparar", "diferencia",
    "alternativa", "recomendado", "opinion",
}
_INFORMATIONAL_SIGNALS = {
    "que es", "como", "para que", "sirve", "funciona", "significa",
    "beneficios", "usos", "propiedades",
}

# Atributos físicos comunes que sugieren intención de atributo
_ATTRIBUTE_WORDS = {
    "negro", "blanco", "rojo", "azul", "verde", "rosa", "gris", "beige",
    "grande", "chico", "mediano", "pequeño", "largo", "corto",
    "talle", "talla", "numero", "litros", "metros", "centimetros",
    "mujer", "hombre", "adulto", "niño", "bebe",
}

# Pesos del score ML (suman 100)
_ML_SCORE_WEIGHTS = {
    "attrs_required": 35,
    "attrs_optional": 20,
    "title_keywords": 20,
    "photos":         10,
    "free_shipping":  10,
    "catalog_match":   5,
}


# ── Contexto por categoría: FAQs típicas + objeciones del nicho ──────────────
# Usado como fallback cuando M3.5 no devuelve Q&A real de competidores.

_CATEGORY_CONTEXT = [
    {
        "kws": ["moda", "ropa", "calzado", "cartera", "billetera", "reloj", "joyeria",
                "joya", "vestido", "pantalon", "camisa", "remera", "zapatilla", "accesorio moda"],
        "faqs": "¿Cómo calza? ¿El color es igual a la foto? ¿El material es genuino? ¿Se puede cambiar de talle? ¿Viene con packaging de regalo?",
        "objections": "miedo al talle incorrecto, color diferente a foto, material de baja calidad. Neutralizar con: medidas exactas, tabla de talles comparativa, descripción honesta y verificable del material.",
    },
    {
        "kws": ["celular", "notebook", "computadora", "tv", "audio", "gaming",
                "electronica", "tecnologia", "tablet", "auricular", "smartwatch", "monitor"],
        "faqs": "¿Es original/sellado? ¿Garantía oficial? ¿Compatible con mi operadora/sistema? ¿Trae todos los accesorios? ¿Funciona con el voltaje argentino?",
        "objections": "miedo a refurbished/reparado, incompatibilidad, falta de accesorios, garantía insuficiente. Neutralizar con: confirmar estado sellado/original, listar TODOS los accesorios incluidos, especificar garantía exacta en meses.",
    },
    {
        "kws": ["hogar", "mueble", "deco", "silla", "mesa", "sofa", "colchon",
                "escritorio", "estante", "armario", "cocina", "baño"],
        "faqs": "¿Las medidas son exactas? ¿Viene armado? ¿Cuánto peso aguanta? ¿Es resistente a humedad? ¿Incluye herrajes?",
        "objections": "tamaño real vs foto, dificultad de armado, fragilidad en envío. Neutralizar con: medidas exactas con foto de referencia, instrucciones de armado claras, descripción del embalaje.",
    },
    {
        "kws": ["deporte", "fitness", "running", "ciclismo", "crossfit", "yoga",
                "gimnasio", "bicicleta", "pelota", "natacion"],
        "faqs": "¿Sirve para mi nivel? ¿Es impermeable? ¿Cuál es el peso máximo soportado? ¿Cómo calza comparado con otras marcas?",
        "objections": "potencia o resistencia insuficiente para el nivel, talla incorrecta, specs que no se sostienen en uso real. Neutralizar con: nivel recomendado explícito, comparativa de talles, especificaciones técnicas verificables.",
    },
    {
        "kws": ["belleza", "cosmetico", "maquillaje", "crema", "shampoo", "perfume",
                "cuidado personal", "serum", "aceite capilar", "tratamiento"],
        "faqs": "¿Sirve para mi tipo de piel/cabello? ¿Es original? ¿Cuál es el vencimiento? ¿Contiene alérgenos?",
        "objections": "miedo a falsificación, vencimiento cercano, reacción alérgica. Neutralizar con: confirmar autenticidad, informar vencimiento o durabilidad estimada, listar ingredientes activos.",
    },
    {
        "kws": ["bebe", "niño", "juguete", "infantil", "pañal", "materna", "lactancia"],
        "faqs": "¿Desde qué edad se usa? ¿Es seguro? ¿Se puede lavar? ¿Cuánto peso/tamaño soporta?",
        "objections": "material tóxico, inadecuado para la edad, difícil de higienizar. Neutralizar con: certificaciones de seguridad, libre BPA, edad recomendada explícita, instrucciones de lavado.",
    },
    {
        "kws": ["auto", "moto", "vehiculo", "repuesto", "autoparte", "freno", "filtro",
                "accesorio vehiculo", "neumatico", "llanta"],
        "faqs": "¿Sirve para mi auto/moto modelo/año? ¿Es pieza original o alternativa? ¿Garantía en km o tiempo? ¿Necesita instalación profesional?",
        "objections": "incompatibilidad con el modelo/año exacto, pieza no original, garantía insuficiente. Neutralizar con: listar compatibilidades exactas (marca/modelo/año), código OEM si existe, tipo de pieza claramente indicado.",
    },
    {
        "kws": ["herramienta", "construccion", "taladro", "sierra", "soldadora",
                "compresor", "amoladora", "lijadora"],
        "faqs": "¿Tiene suficiente potencia para mi uso? ¿Cuánto dura la batería? ¿Qué accesorios incluye? ¿Sirve para uso profesional?",
        "objections": "potencia insuficiente, batería de corta duración, accesorios necesarios no incluidos. Neutralizar con: potencia real con ejemplos de uso concretos, autonomía en horas, lista completa de accesorios incluidos.",
    },
    {
        "kws": ["salud", "medico", "ortopedico", "farmacia", "suplemento", "vitamina",
                "rehabilitacion", "fisioterapia"],
        "faqs": "¿Para qué condición específica sirve? ¿Tiene contraindicaciones? ¿Cuál es el vencimiento? ¿Requiere receta?",
        "objections": "eficacia no comprobada, contraindicaciones no informadas, vencimiento cercano. Neutralizar con: indicaciones claras, contraindicaciones explícitas, fecha de vencimiento, modo de uso.",
    },
    {
        "kws": ["mascota", "perro", "gato", "veterinaria", "alimento animal", "collar",
                "correa", "acuario", "ave"],
        "faqs": "¿Para qué raza/tamaño de animal sirve? ¿Desde qué edad? ¿Contiene ingredientes nocivos? ¿El talle es correcto?",
        "objections": "inadecuado para la raza/tamaño del animal, ingredientes nocivos, talle incorrecto. Neutralizar con: peso/tamaño recomendado explícito, composición completa, guía de talles.",
    },
]

_COMPLEX_CATEGORY_KEYWORDS = {
    "electronica", "tecnologia", "celular", "notebook", "computadora", "tablet",
    "tv", "monitor", "audio", "gaming", "hogar", "mueble", "colchon", "sofa",
    "auto", "moto", "repuesto", "autoparte", "herramienta", "construccion",
    "salud", "medico", "ortopedico", "electrodomestico",
}


def _smart_truncate(text: str, max_chars: int) -> str:
    """Corta en el último encabezado ## antes del límite para no partir secciones a la mitad."""
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    last_section = chunk.rfind('\n##')
    # Solo cortar en sección si se conserva al menos la mitad del contenido
    if last_section > max_chars // 2:
        return chunk[:last_section].strip() + "\n[análisis truncado — se conservan secciones completas]"
    return chunk.rstrip() + "…"


def _strip_accents(text: str) -> str:
    """Elimina tildes para comparación sin acento. 'Electrónica' → 'electronica'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).lower()


def _get_category_context(category_name: str) -> str:
    """Retorna FAQs y objeciones típicas del nicho. Fallback cuando no hay Q&A real."""
    cat_norm = _strip_accents(category_name)
    for ctx in _CATEGORY_CONTEXT:
        if any(kw in cat_norm for kw in ctx["kws"]):
            return (
                f"FAQs típicas del nicho:\n  {ctx['faqs']}\n"
                f"Objeciones a neutralizar en la descripción:\n  {ctx['objections']}"
            )
    return ""


def _is_complex_product(category_name: str) -> bool:
    """Detecta si el producto es complejo (requiere descripción más larga)."""
    cat_norm = _strip_accents(category_name)
    return any(kw in cat_norm for kw in _COMPLEX_CATEGORY_KEYWORDS)


# ── Helpers de tokenización ───────────────────────────────────────────────────

def _tokenize(text: str) -> list:
    words = re.findall(r"[a-záéíóúüñ0-9]+", text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def _jaccard(a: str, b: str) -> float:
    sa = set(_tokenize(a))
    sb = set(_tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _cluster_keywords(keywords: list, position_map: dict) -> list:
    """
    Agrupa keywords semánticamente similares usando Jaccard sobre tokens.
    Dentro de cada cluster elige el representante = menor best_pos (más buscado).
    Si dos keywords tienen el mismo best_pos, gana el de mayor query_count.

    Retorna lista de clusters ordenada por best_pos del representante ASC:
      [{'representative': str, 'best_pos': int, 'query_count': int, 'variants': [str, ...]}, ...]

    Umbral de similitud: 0.40 — comparten al menos 40% de tokens significativos.
    """
    THRESHOLD = 0.40
    assigned = [False] * len(keywords)
    clusters = []

    def _pm(kw):
        pm = position_map.get(kw, {})
        return pm.get('best_pos', 99), pm.get('query_count', 0)

    for i, kw_i in enumerate(keywords):
        if assigned[i]:
            continue
        group = [kw_i]
        assigned[i] = True
        for j, kw_j in enumerate(keywords):
            if assigned[j] or i == j:
                continue
            if _jaccard(kw_i, kw_j) >= THRESHOLD:
                group.append(kw_j)
                assigned[j] = True

        # Elegir representante: menor best_pos; si empate, mayor query_count
        group.sort(key=lambda k: (_pm(k)[0], -_pm(k)[1]))
        rep = group[0]
        rep_pos, rep_qc = _pm(rep)
        clusters.append({
            'representative': rep,
            'best_pos':       rep_pos,
            'query_count':    rep_qc,
            'variants':       group[1:],
        })

    # Ordenar clusters de mayor a menor poder (menor best_pos = más buscado)
    clusters.sort(key=lambda c: (c['best_pos'], -c['query_count']))
    return clusters


# ══════════════════════════════════════════════════════════════════════════════
# M0 — COMPETITOR PHRASE EXTRACTION (alimenta M1 con vocabulario real)
# ══════════════════════════════════════════════════════════════════════════════

_PROMO_WORDS_SEO = {
    'oferta', 'descuento', 'promo', 'promocion', 'gratis', 'envio',
    'cuotas', 'nuevo', 'original', 'garantia', 'mejor', 'barato',
    'economico', 'precio', 'venta', 'pack', 'kit', 'combo', 'super',
    'mega', 'ultra', 'premium', 'calidad',
}

_KW_STOP_SEO = _STOPWORDS | _PROMO_WORDS_SEO


def _norm(text: str) -> str:
    import unicodedata
    return unicodedata.normalize('NFKD', text.lower()).encode('ascii', 'ignore').decode()


def _extract_competitor_phrases_seo(competitor_titles: list, seed_title: str) -> list:
    """
    Extrae bigramas y trigramas de títulos de competidores.
    · Filtra stopwords y palabras promocionales
    · Descarta frases literalmente presentes en el seed (ya cubiertas)
    · NO filtra frases únicas por competidor: son los perfiles de búsqueda alternativos
      que buscamos descubrir (maquinita / tijera / clipper para el mismo producto).
      La validación semántica via autosuggest actúa como filtro real.
    · Prioriza frases compartidas; trigrama antes que bigrama de igual peso
    """
    seed_norm = _norm(seed_title)
    token_lists = []
    for t in competitor_titles:
        tokens = [
            w for w in _norm(t).split()
            if w not in _KW_STOP_SEO and not w.isdigit() and len(w) > 2
        ]
        if tokens:
            token_lists.append(tokens)

    if not token_lists:
        return []

    phrase_count = {}

    for tokens in token_lists:
        seen_here = set()
        for i in range(len(tokens)):
            for n in (2, 3):
                if i + n <= len(tokens):
                    phrase = ' '.join(tokens[i:i + n])
                    if phrase not in seen_here:
                        phrase_count[phrase] = phrase_count.get(phrase, 0) + 1
                        seen_here.add(phrase)

    candidates = [
        (phrase, count) for phrase, count in phrase_count.items()
        if phrase not in seed_norm
    ]
    candidates.sort(key=lambda x: (-x[1], -len(x[0].split())))
    # 20 candidatos: antes con solo títulos alcanzaba con 10,
    # ahora que incluye descripciones hay más vocabulario valioso para explorar
    return [p for p, _ in candidates[:20]]


def _competitor_seeded_autosuggest_seo(competitor_titles: list, seed_title: str) -> list:
    """
    Genera keywords adicionales validadas usando frases de competidores como semillas.
    Validación doble:
      1. Autosuggest devuelve >= 2 resultados  → la familia léxica existe en el mercado
      2. Al menos 1 resultado comparte palabras con el universo del producto
         (seed + todos los títulos de competidores) → evita deriva a otra categoría

    Usar vocabulario ampliado (seed + competidores) como referencia semántica cubre
    los distintos perfiles de búsqueda del mismo producto sin restringirlos al seed.
    """
    phrases = _extract_competitor_phrases_seo(competitor_titles, seed_title)
    if not phrases:
        return []

    _sw = {'de','para','con','sin','y','el','la','los','las','un','una','en','a','por','al','del'}
    all_product_text = seed_title + ' ' + ' '.join(competitor_titles)
    product_words = {
        w for w in _norm(all_product_text).split()
        if len(w) > 3 and w not in _sw and w not in _KW_STOP_SEO
    }

    new_kws = []
    seen    = set()

    for phrase in phrases:
        suggestions = _ml_autosuggest(phrase, limit=8)
        if len(suggestions) < 2:
            time.sleep(0.1)
            continue
        if product_words:
            relevant = any(
                any(pw in _norm(s) for pw in product_words)
                for s in suggestions
            )
            if not relevant:
                time.sleep(0.1)
                continue
        for s in suggestions:
            if s not in seen:
                seen.add(s)
                new_kws.append(s)
        time.sleep(0.15)

    return new_kws


# ══════════════════════════════════════════════════════════════════════════════
# M1 — KEYWORD DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

def _ml_autosuggest(query: str, limit: int = 10) -> list:
    """
    Consulta el autosuggest real de ML.
    El orden devuelto por ML es el orden de popularidad de búsqueda.
    """
    try:
        r = requests.get(
            ML_AS_URL,
            params={"q": query, "limit": limit, "lang": "es_AR"},
            headers=_AS_HEADERS,
            timeout=6,
        )
        if r.ok:
            return [s["q"] for s in r.json().get("suggested_queries", []) if s.get("q")]
    except Exception:
        pass
    return []


def get_autosuggest_keywords(title: str) -> tuple:
    """
    Ejecuta hasta 4 queries cortas derivadas del título para maximizar cobertura.
    Retorna (keywords_list, position_map) donde position_map = {kw: {'best_pos': int, 'query_count': int}}.
    best_pos = mejor posición que tuvo la keyword en cualquier query (1 = más buscada).
    query_count = en cuántas queries distintas apareció (validación cruzada de volumen).

    Estrategia de queries:
      q1 = primeras 3 palabras sig. → identidad principal del producto
      q2 = primeras 2 palabras sig. → búsqueda más genérica/popular
      q3 = palabras del medio (pos 1-3) → términos diferenciadores del título
      q4 = últimas 3 palabras sig.  → calificadores específicos (profesional, seco, etc.)

    Usar el título completo como query produce sugerencias de esa frase larga específica
    en vez de las búsquedas reales del nicho — los compradores buscan con 2-3 palabras.
    Fallback: extrae palabras clave del título si el autosuggest falla.
    """
    words = [w for w in title.lower().split() if len(w) > 3 and w not in _STOPWORDS]

    q1 = " ".join(words[:3]) if len(words) >= 3 else " ".join(words)
    q2 = " ".join(words[:2]) if len(words) >= 2 else ""
    q3 = " ".join(words[1:4]) if len(words) >= 4 else ""   # medio del título
    q4 = " ".join(words[-3:]) if len(words) >= 5 else ""   # calificadores del final

    seen_q: set = set()
    queries = []
    for q in [q1, q2, q3, q4]:
        if q and q not in seen_q:
            seen_q.add(q)
            queries.append(q)

    seen_s: set    = set()
    suggestions: list = []
    position_map: dict = {}   # {kw: {'best_pos': int, 'query_count': int}}

    for q in queries:
        for pos, s in enumerate(_ml_autosuggest(q, limit=10), 1):
            if s not in position_map:
                position_map[s] = {'best_pos': pos, 'query_count': 0}
            else:
                # Conservar la mejor posición (menor número = más popular)
                position_map[s]['best_pos'] = min(position_map[s]['best_pos'], pos)
            position_map[s]['query_count'] += 1
            if s not in seen_s:
                seen_s.add(s)
                suggestions.append(s)
        time.sleep(0.1)

    # Fallback si autosuggest devolvió vacío (producto muy nicho)
    if not suggestions and words:
        candidates = [" ".join(words[:4]), " ".join(words[:3]), " ".join(words[:2])]
        suggestions = list(dict.fromkeys(c for c in candidates if c))
        for i, s in enumerate(suggestions, 1):
            position_map[s] = {'best_pos': i, 'query_count': 1}

    return suggestions, position_map


def _classify_intent(keyword: str) -> str:
    """
    Clasifica la intención de búsqueda de una keyword.
    Basado en señales léxicas observables, no en asumir comportamiento.
    """
    kw = keyword.lower()
    tokens = set(_tokenize(kw))

    if any(s in kw for s in _INFORMATIONAL_SIGNALS):
        return "informativa"
    if any(s in kw for s in _COMPARATIVE_SIGNALS):
        return "comparativa"
    if any(s in kw for s in _TRANSACTIONAL_SIGNALS):
        return "transaccional"
    if tokens & _ATTRIBUTE_WORDS:
        return "atributo"
    return "ambigua"


def _classify_compatibility(keyword: str, title: str) -> str:
    """
    Estima compatibilidad semántica entre la keyword y el producto.
    Alta: keyword es una forma de buscar exactamente este producto.
    Peligrosa: la keyword describe otro producto (riesgo de irrelevancia).
    """
    sim = _jaccard(keyword, title)
    if sim >= 0.55:
        return "alta"
    elif sim >= 0.30:
        return "media"
    elif sim >= 0.12:
        return "baja"
    else:
        return "peligrosa"


def _calculate_priority_score(
    as_position: int,       # posición en la lista de autosuggest (1-based, 0 = no está)
    as_total: int,          # total de resultados del autosuggest
    competitor_presence: float,  # fracción de competidores top que usan esta keyword (0-1)
    in_title: bool,         # está en el título actual
    current_position,       # posición actual en búsqueda ML (None si no rankea)
    semantic_relevance: float,   # similitud jaccard con el título (0-1)
) -> float:
    """
    Score compuesto 0-100 con los pesos del spec:
      40% posición en autosuggest
      25% presencia en competidores top
      15% coincidencia exacta en título
      10% gap de posición actual
      10% relevancia semántica
    """
    # 40% — posición autosuggest (1º = 100, último = gradual, ausente = 0)
    if as_position > 0 and as_total > 0:
        as_score = max(0, (1 - (as_position - 1) / as_total)) * 100
    else:
        as_score = 0.0

    # 25% — presencia en competidores
    comp_score = min(competitor_presence, 1.0) * 100

    # 15% — coincidencia en título
    title_score = 100.0 if in_title else 0.0

    # 10% — gap de posición (posición 1 = 100, posición 48 = ~2, sin ranking = 0)
    if current_position is not None and current_position > 0:
        gap_score = max(0, (49 - current_position) / 48) * 100
    else:
        gap_score = 0.0

    # 10% — relevancia semántica
    sem_score = min(semantic_relevance, 1.0) * 100

    total = (
        0.40 * as_score +
        0.25 * comp_score +
        0.15 * title_score +
        0.10 * gap_score +
        0.10 * sem_score
    )
    return round(total, 1)


def score_and_classify_keywords(
    autosuggest_raw: list,
    title: str,
    position_data: list,
    competitors: list,
    position_map: dict = None,
) -> list:
    """
    M1 completo: toma las keywords del autosuggest y las enriquece con:
    - intent type
    - compatibility
    - priority score (basado en datos reales, sin inventar volumen)

    position_map: {kw: {'best_pos': int, 'query_count': int}} — si se provee,
    usa la posición REAL del autosuggest en lugar del índice de la lista merged.
    Las keywords de competidores (M1.5) reciben best_pos=5 por convención.
    """
    as_total    = len(autosuggest_raw)
    title_lower = title.lower()

    # Índice de posiciones por keyword (M2)
    pos_index = {p["keyword"]: p.get("position") for p in position_data}

    # Presencia en competidores
    comp_titles_tokens = [set(_tokenize(c.get("title", ""))) for c in competitors]

    result = []
    for idx, kw in enumerate(autosuggest_raw, 1):
        kw_tokens = set(_tokenize(kw))

        # Posición real en autosuggest — usar position_map si está disponible
        if position_map and kw in position_map:
            _pm       = position_map[kw]
            real_pos  = _pm['best_pos']
            # Bonus por validación cruzada: si apareció en 2+ queries, reducir posición efectiva
            if _pm.get('query_count', 0) >= 2:
                real_pos = max(1, real_pos - 2)
            as_pos = real_pos
        else:
            as_pos = idx   # fallback: orden de llegada

        # ¿Cuántos competidores top usan esta keyword?
        if comp_titles_tokens:
            comp_matches  = sum(1 for ct in comp_titles_tokens if kw_tokens & ct)
            comp_presence = comp_matches / len(comp_titles_tokens)
        else:
            comp_presence = 0.0

        in_title      = kw.lower() in title_lower
        current_pos   = pos_index.get(kw)
        sem_relevance = _jaccard(kw, title)

        score = _calculate_priority_score(
            as_position         = as_pos,
            as_total            = as_total,
            competitor_presence = comp_presence,
            in_title            = in_title,
            current_position    = current_pos,
            semantic_relevance  = sem_relevance,
        )

        result.append({
            "keyword":               kw,
            "autosuggest_position":  as_pos,
            "priority_score":        score,
            "tipo_intencion":        _classify_intent(kw),
            "compatibilidad":        _classify_compatibility(kw, title),
            "en_titulo_actual":      in_title,
            "posicion_actual":       current_pos,
            "presencia_competidores": round(comp_presence * 100),
        })

    # Ordenar por priority_score DESC
    result.sort(key=lambda x: x["priority_score"], reverse=True)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# M2 — POSITION TRACKER
# ══════════════════════════════════════════════════════════════════════════════

def track_positions(item_id: str, keywords: list, token: str) -> list:
    """
    Para cada keyword: busca en ML y detecta si el item aparece y en qué posición.
    Límite de búsqueda: 48 resultados (top 2 páginas, lo que ML entrega fácilmente).
    Calcula gap vs top 3 y nivel de saturación del mercado.
    """
    results = []
    headers = {"Authorization": f"Bearer {token}"}

    for kw in keywords[:8]:  # máx 8 para no tardar más de ~2 min
        position = None
        top3_ids = []
        total_results = 0

        try:
            r = requests.get(
                f"{ML_API}/sites/{ML_SITE}/search",
                headers=headers,
                params={"q": kw, "limit": 48, "sort": "relevance"},
                timeout=8,
            )
            if r.ok:
                data = r.json()
                total_results = data.get("paging", {}).get("total", 0)
                search_results = data.get("results", [])
                top3_ids = [res.get("id") for res in search_results[:3] if res.get("id")]

                for i, res in enumerate(search_results, 1):
                    if res.get("id") == item_id:
                        position = i
                        break
        except Exception:
            pass

        # Saturación basada en total de resultados (observable, no asumido)
        if total_results >= 1000:
            saturacion = "alta"
        elif total_results >= 200:
            saturacion = "media"
        else:
            saturacion = "baja"

        # Gap vs top 3: si no aparecemos en top 3, gap = None (no podemos medirlo sin más búsquedas)
        gap_vs_top3 = None
        if position is not None and position > 3:
            gap_vs_top3 = position - 3
        elif position is not None and position <= 3:
            gap_vs_top3 = 0

        results.append({
            "keyword":         kw,
            "aparece":         position is not None,
            "position":        position,
            "gap_vs_top3":     gap_vs_top3,
            "total_resultados": total_results,
            "saturacion":      saturacion,
            "top3_ids":        top3_ids,
        })
        time.sleep(0.15)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# M3 — COMPETITOR INTELLIGENCE
# ══════════════════════════════════════════════════════════════════════════════

def fetch_competitors_full(keyword: str, token: str, exclude_id: str = "", limit: int = 8) -> list:
    """
    Obtiene los top N competidores con datos completos:
    título, precio, tipo, shipping, atributos, descripción, ventas, fotos.
    """
    headers = {"Authorization": f"Bearer {token}"}
    results = []

    try:
        resp = requests.get(
            f"{ML_API}/sites/{ML_SITE}/search",
            headers=headers,
            params={"q": keyword, "limit": 20, "sort": "relevance"},
            timeout=10,
        )
        if not resp.ok:
            return []

        search_items = [it for it in resp.json().get("results", []) if it.get("id") != exclude_id]

        for item in search_items[:limit]:
            item_id = item["id"]
            comp = {
                "id":            item_id,
                "title":         item.get("title", ""),
                "seller":        item.get("seller", {}).get("nickname", "—"),
                "sold_quantity": item.get("sold_quantity", 0),
                "price":         float(item.get("price", 0)),
                "listing_type":  item.get("listing_type_id", ""),
                "premium":       item.get("listing_type_id", "") in ("gold_special", "gold_pro"),
                "free_ship":     item.get("shipping", {}).get("free_shipping", False),
                "full_ship":     item.get("shipping", {}).get("logistic_type", "") == "fulfillment",
                "photos_count":  0,
                "attributes":    [],
                "description":   "",
            }
            # Datos completos del item
            try:
                ir = requests.get(f"{ML_API}/items/{item_id}", headers=headers, timeout=8)
                if ir.ok:
                    full = ir.json()
                    comp["photos_count"] = len(full.get("pictures", []))
                    comp["full_ship"]    = full.get("shipping", {}).get("logistic_type", "") == "fulfillment"
                    comp["attributes"]   = [
                        {"name": a.get("name", ""), "value": a.get("value_name", "") or ""}
                        for a in full.get("attributes", [])
                        if a.get("value_name")
                    ]
            except Exception:
                pass
            # Descripción
            try:
                dr = requests.get(f"{ML_API}/items/{item_id}/description", headers=headers, timeout=8)
                if dr.ok:
                    comp["description"] = dr.json().get("plain_text", "")[:800]
            except Exception:
                pass

            results.append(comp)
            time.sleep(0.15)

    except Exception:
        pass
    return results


def fetch_competitor_qa(competitor_ids: list, token: str, max_q: int = 15, max_r: int = 10) -> list:
    """
    M3.5: Para cada competidor obtiene preguntas respondidas + reseñas.
    Top 5 competidores para mayor cobertura de Q&A y reseñas.
    Retorna lista de dicts: {id, title, questions: [...], reviews: [...]}
    """
    headers = {"Authorization": f"Bearer {token}"}
    results = []

    for item_id in competitor_ids[:5]:
        data = {"id": item_id, "questions": [], "reviews": []}

        # Preguntas respondidas
        try:
            r = requests.get(
                f"{ML_API}/questions/search",
                headers=headers,
                params={"item_id": item_id, "status": "answered", "limit": max_q},
                timeout=8,
            )
            if r.ok:
                for q in r.json().get("questions", []):
                    text  = (q.get("text") or "").strip()
                    answ  = (q.get("answer", {}) or {}).get("text", "").strip()
                    if text and answ:
                        data["questions"].append({"q": text[:200], "a": answ[:300]})
        except Exception:
            pass

        # Reseñas
        try:
            r = requests.get(
                f"{ML_API}/reviews/item/{item_id}",
                headers=headers,
                params={"limit": max_r},
                timeout=8,
            )
            if r.ok:
                for rev in r.json().get("reviews", []):
                    rating  = rev.get("rate", 0)
                    content = (rev.get("content") or "").strip()
                    title_r = (rev.get("title") or "").strip()
                    if content or title_r:
                        data["reviews"].append({
                            "rating":  rating,
                            "title":   title_r[:100],
                            "content": content[:300],
                        })
        except Exception:
            pass

        if data["questions"] or data["reviews"]:
            results.append(data)
        time.sleep(0.2)

    return results


def analyze_qa_insights(qa_data: list, product_title: str, console=None) -> str:
    """
    M3.5: Claude Haiku analiza las Q&As y reseñas de competidores y extrae
    insights accionables: dudas frecuentes, debilidades, valoraciones, lenguaje.
    """
    if not qa_data:
        return ""

    # Armar bloque de texto con Q&As y reseñas
    blocks = []
    for comp in qa_data:
        cid = comp["id"]
        qs  = comp.get("questions", [])
        rs  = comp.get("reviews", [])
        if qs:
            block = f"[Competidor {cid} — PREGUNTAS RESPONDIDAS]\n"
            block += "\n".join(f"P: {q['q']}\nR: {q['a']}" for q in qs[:12])
            blocks.append(block)
        if rs:
            positivas  = [r for r in rs if r["rating"] >= 4]
            negativas  = [r for r in rs if r["rating"] <= 2]
            block = f"[Competidor {cid} — RESEÑAS]\n"
            block += "POSITIVAS:\n" + "\n".join(
                f"★{r['rating']} {r['title']} — {r['content']}" for r in positivas[:5]
            )
            if negativas:
                block += "\nNEGATIVAS:\n" + "\n".join(
                    f"★{r['rating']} {r['title']} — {r['content']}" for r in negativas[:5]
                )
            blocks.append(block)

    if not blocks:
        return ""

    raw_text = "\n\n".join(blocks)

    prompt = f"""Sos un analista experto en comportamiento de compradores en MercadoLibre Argentina.
Analizá las siguientes preguntas respondidas y reseñas de competidores del producto: "{product_title}"

{raw_text[:3500]}

Extraé exactamente estas 4 secciones. Sé concreto, usa viñetas cortas, sin paja:

## DEBILIDADES DE COMPETIDORES
(qué critican los compradores en reseñas negativas — tu oportunidad de diferenciarte)
- [debilidad concreta]

## DUDAS FRECUENTES DE COMPRADORES
(qué preguntan antes de comprar — estas dudas DEBEN responderse en la descripción)
- [duda concreta]

## QUÉ MÁS VALORAN LOS COMPRADORES
(qué elogian en reseñas positivas — reforzar en descripción y título si aplica)
- [valoración concreta]

## LENGUAJE REAL DEL MERCADO
(palabras y frases exactas que usan los compradores — incorporar en descripción naturalmente)
- [frase o término]"""

    return _call_claude(prompt, max_tokens=800, console=console, fast=True)


def analyze_competitor_patterns(competitors: list, title: str) -> dict:
    """
    M3: Extrae patrones observables de los competidores:
    - keywords más repetidas en sus títulos
    - atributos más completados
    - longitudes de título
    - ratio premium / shipping
    """
    if not competitors:
        return {}

    all_title_tokens: list = []
    all_attr_names: list   = []
    title_lengths: list    = []
    premium_count          = 0
    full_count             = 0
    prices: list           = []

    for c in competitors:
        all_title_tokens.extend(_tokenize(c.get("title", "")))
        all_attr_names.extend(a["name"] for a in c.get("attributes", []) if a.get("value"))
        title_lengths.append(len(c.get("title", "")))
        if c.get("premium"):
            premium_count += 1
        if c.get("full_ship"):
            full_count += 1
        if c.get("price", 0) > 0:
            prices.append(c["price"])

    # Keywords más frecuentes en títulos de competidores
    from collections import Counter
    token_freq = Counter(all_title_tokens)
    top_kws = [w for w, _ in token_freq.most_common(20) if w not in _STOPWORDS and len(w) > 2]

    # Keywords de competidores que no están en nuestro título
    my_tokens = set(_tokenize(title))
    kw_gaps = [w for w in top_kws if w not in my_tokens]

    # Atributos más completados por la competencia
    attr_freq = Counter(all_attr_names)
    top_attrs = [a for a, _ in attr_freq.most_common(15)]

    avg_price = round(sum(prices) / len(prices)) if prices else 0

    # Fotos
    photo_counts = [c.get("photos_count", 0) for c in competitors if c.get("photos_count", 0) > 0]
    avg_photos   = round(sum(photo_counts) / len(photo_counts)) if photo_counts else 0
    max_photos   = max(photo_counts) if photo_counts else 0

    return {
        "keywords_frecuentes":    top_kws[:15],
        "keywords_gap":           kw_gaps[:10],
        "atributos_frecuentes":   top_attrs[:10],
        "avg_title_length":       round(sum(title_lengths) / len(title_lengths)) if title_lengths else 0,
        "premium_ratio":          round(premium_count / len(competitors), 2),
        "full_shipping_ratio":    round(full_count / len(competitors), 2),
        "avg_price":              avg_price,
        "price_range":            {"min": min(prices) if prices else 0, "max": max(prices) if prices else 0},
        "avg_photos":             avg_photos,
        "max_photos":             max_photos,
    }


# ══════════════════════════════════════════════════════════════════════════════
# M4 — ROOT CAUSE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def analyze_root_causes(
    item_data: dict,
    description: str,
    keyword_analysis: list,
    position_data: list,
    competitors: list,
    category_attrs: dict,
    comp_patterns: dict,
) -> list:
    """
    Determina por qué la publicación no rankea.
    Solo causas basadas en datos observables — impacto calculado, no asumido.
    Devuelve lista ordenada por impacto.
    """
    causes = []

    title        = item_data.get("title", "")
    price        = float(item_data.get("price", 0))
    photos       = len(item_data.get("pictures", []))
    free_ship    = item_data.get("shipping", {}).get("free_shipping", False)
    full_ship    = item_data.get("shipping", {}).get("logistic_type", "") == "fulfillment"
    listing_type = item_data.get("listing_type_id", "")
    my_sold      = item_data.get("sold_quantity", 0)
    attributes   = item_data.get("attributes", [])

    filled_attr_names = {
        a.get("attribute_name") or a.get("name", "")
        for a in attributes
        if a.get("value_name") and a.get("value_name") not in ("", "N/A")
    }

    # ── Causa 1: Cobertura SEO (keywords del autosuggest ausentes del título) ──
    top_kws = [k for k in keyword_analysis[:5] if k["compatibilidad"] in ("alta", "media")]
    kws_en_titulo   = [k for k in top_kws if k["en_titulo_actual"]]
    kws_sin_titulo  = [k for k in top_kws if not k["en_titulo_actual"]]

    if len(kws_sin_titulo) >= 3:
        impacto = "alto"
    elif len(kws_sin_titulo) >= 1:
        impacto = "medio"
    else:
        impacto = "bajo"

    if kws_sin_titulo:
        causes.append({
            "causa":    "baja cobertura de keywords core en el título",
            "detalle":  f"De las top 5 keywords del autosuggest, {len(kws_sin_titulo)} no están en el título: "
                        f"{', '.join(k['keyword'] for k in kws_sin_titulo[:3])}",
            "impacto":  impacto,
            "tipo":     "dato_real",
        })

    # ── Causa 2: Posicionamiento bajo para keywords principales ───────────────
    no_ranking   = [p for p in position_data if not p["aparece"]]
    bajo_ranking = [p for p in position_data if p["aparece"] and (p["position"] or 99) > 20]

    if len(no_ranking) >= 3:
        causes.append({
            "causa":   "sin posicionamiento en mayoría de búsquedas clave",
            "detalle": f"No aparece en top 48 para {len(no_ranking)} de {len(position_data)} keywords analizadas",
            "impacto": "alto",
            "tipo":    "dato_real",
        })
    elif bajo_ranking:
        causes.append({
            "causa":   "posicionamiento bajo en búsquedas clave",
            "detalle": f"Aparece pero en posición baja (>20) para {len(bajo_ranking)} keywords",
            "impacto": "medio",
            "tipo":    "dato_real",
        })

    # ── Causa 3: Atributos requeridos incompletos ─────────────────────────────
    req_attrs = category_attrs.get("required", [])
    missing_req = [a["name"] for a in req_attrs if a["name"] not in filled_attr_names]

    if len(missing_req) >= 3:
        impacto = "alto"
    elif len(missing_req) >= 1:
        impacto = "medio"
    else:
        impacto = "bajo"

    if missing_req:
        causes.append({
            "causa":   "atributos requeridos de la categoría incompletos",
            "detalle": f"{len(missing_req)} atributos obligatorios vacíos: {', '.join(missing_req[:4])}",
            "impacto": impacto,
            "tipo":    "dato_real",
        })

    # ── Causa 4: Precio no competitivo ───────────────────────────────────────
    avg_price = comp_patterns.get("avg_price", 0)
    if avg_price > 0 and price > 0:
        diff_pct = ((price - avg_price) / avg_price) * 100
        if diff_pct > 20:
            causes.append({
                "causa":   "precio significativamente por encima del promedio",
                "detalle": f"Precio propio ${price:,.0f} vs promedio competidores ${avg_price:,.0f} (+{diff_pct:.0f}%)",
                "impacto": "alto",
                "tipo":    "dato_real",
            })
        elif diff_pct > 10:
            causes.append({
                "causa":   "precio moderadamente por encima del promedio",
                "detalle": f"Precio propio ${price:,.0f} vs promedio ${avg_price:,.0f} (+{diff_pct:.0f}%)",
                "impacto": "medio",
                "tipo":    "dato_real",
            })

    # ── Causa 5: Pocas fotos (impacto en CTR observable) ─────────────────────
    avg_comp_photos = round(
        sum(c.get("photos_count", 0) for c in competitors) / len(competitors)
    ) if competitors else 0

    if photos < 4:
        causes.append({
            "causa":   "cantidad de fotos crítica — penaliza CTR y visibilidad",
            "detalle": f"Solo {photos} fotos. Competidores tienen promedio de {avg_comp_photos}. Mínimo recomendado: 6",
            "impacto": "alto",
            "tipo":    "dato_real",
        })
    elif photos < 6:
        causes.append({
            "causa":   "pocas fotos vs competidores",
            "detalle": f"{photos} fotos vs promedio competidores {avg_comp_photos}",
            "impacto": "medio",
            "tipo":    "dato_real",
        })

    # ── Causa 6: Shipping inferior ────────────────────────────────────────────
    full_ratio = comp_patterns.get("full_shipping_ratio", 0)
    if full_ratio >= 0.6 and not full_ship:
        causes.append({
            "causa":   "shipping inferior — competidores con Full/Flex vs sin Full",
            "detalle": f"{round(full_ratio*100)}% de competidores top usa Full. Sin Full penaliza visibilidad en algoritmo.",
            "impacto": "alto",
            "tipo":    "inferencia",  # el impacto exacto del algoritmo no es público
        })
    elif not free_ship and comp_patterns.get("full_shipping_ratio", 0) > 0:
        causes.append({
            "causa":   "sin envío gratis — competidores top lo ofrecen",
            "detalle": f"La publicación no tiene envío gratis, lo que reduce el score de visibilidad.",
            "impacto": "medio",
            "tipo":    "inferencia",
        })

    # ── Causa 7: Baja autoridad (ventas propias vs competidores) ─────────────
    comp_sales = [c.get("sold_quantity", 0) for c in competitors if c.get("sold_quantity", 0) > 0]
    avg_comp_sales = round(sum(comp_sales) / len(comp_sales)) if comp_sales else 0

    if avg_comp_sales > 0 and my_sold < avg_comp_sales * 0.1:
        causes.append({
            "causa":   "baja autoridad de ventas vs competidores",
            "detalle": f"Ventas propias: {my_sold}. Promedio competidores: {avg_comp_sales}. "
                       f"El historial de ventas influye en el ranking.",
            "impacto": "alto" if my_sold == 0 else "medio",
            "tipo":    "inferencia",  # el peso exacto no es público
        })

    # ── Causa 8: Tipo de publicación inferior ────────────────────────────────
    premium_ratio = comp_patterns.get("premium_ratio", 0)
    is_premium = listing_type in ("gold_special", "gold_pro")
    if premium_ratio >= 0.6 and not is_premium:
        causes.append({
            "causa":   "publicación clásica compitiendo contra mayoría premium",
            "detalle": f"{round(premium_ratio*100)}% de competidores son Premium. Las publicaciones premium tienen mayor exposición.",
            "impacto": "medio",
            "tipo":    "inferencia",
        })

    # ── Causa 9: Saturación de mercado ───────────────────────────────────────
    high_sat_kws = [p for p in position_data if p.get("saturacion") == "alta"]
    if len(high_sat_kws) >= 3:
        causes.append({
            "causa":   "mercado saturado — alta cantidad de publicaciones competidoras",
            "detalle": f"{len(high_sat_kws)} keywords con más de 1000 resultados en ML. Mayor dificultad estructural.",
            "impacto": "medio",
            "tipo":    "dato_real",
        })

    # Ordenar: alto → medio → bajo
    order = {"alto": 0, "medio": 1, "bajo": 2}
    causes.sort(key=lambda c: order.get(c["impacto"], 3))

    return causes


# ══════════════════════════════════════════════════════════════════════════════
# M5 — DIFFICULTY SCORE
# ══════════════════════════════════════════════════════════════════════════════

def calculate_difficulty(competitors: list, position_data: list, keywords: list) -> dict:
    """
    Calcula la dificultad real de posicionamiento basada en datos observables.
    NO asume pesos del algoritmo — evalúa factores de competencia directa.
    """
    if not competitors:
        return {"nivel": "desconocida", "factores": []}

    premium_ratio = sum(1 for c in competitors if c.get("premium")) / len(competitors)
    full_ratio    = sum(1 for c in competitors if c.get("full_ship")) / len(competitors)

    comp_sales = [c.get("sold_quantity", 0) for c in competitors if c.get("sold_quantity", 0) > 0]
    avg_sales  = sum(comp_sales) / len(comp_sales) if comp_sales else 0

    avg_sat = sum(
        {"alta": 3, "media": 2, "baja": 1}.get(p.get("saturacion", "baja"), 1)
        for p in position_data
    ) / len(position_data) if position_data else 1

    factores = []

    if premium_ratio >= 0.7:
        factores.append(f"{round(premium_ratio*100)}% de competidores son Premium")
    if full_ratio >= 0.6:
        factores.append(f"{round(full_ratio*100)}% tienen Full shipping")
    if avg_sales >= 500:
        factores.append(f"Promedio de {round(avg_sales)} ventas en top competidores")
    if avg_sat >= 2.5:
        factores.append("Mercado con alta saturación de publicaciones")

    # Nivel: combinación de factores
    score = (premium_ratio * 3 + full_ratio * 2 + (min(avg_sales, 1000) / 1000) * 3 + (avg_sat / 3) * 2)

    if score >= 6:
        nivel = "muy_alta"
    elif score >= 4:
        nivel = "alta"
    elif score >= 2:
        nivel = "media"
    else:
        nivel = "baja"

    return {
        "nivel":     nivel,
        "factores":  factores,
        "detalle": {
            "premium_ratio":       round(premium_ratio, 2),
            "full_shipping_ratio": round(full_ratio, 2),
            "avg_competitor_sales": round(avg_sales),
            "saturacion_promedio": round(avg_sat, 1),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Score ML (visual, no oficial)
# ══════════════════════════════════════════════════════════════════════════════

def calculate_ml_score(item_data: dict, category_attrs: dict, top_keywords: list) -> dict:
    """
    Score 0-100 basado en factores observables.
    No es el score oficial de ML (ese no es público), es un índice para diagnóstico.
    """
    title         = item_data.get("title", "")
    attributes    = item_data.get("attributes", [])
    pictures      = item_data.get("pictures", [])
    free_shipping = item_data.get("shipping", {}).get("free_shipping", False)
    catalog_match = bool(item_data.get("catalog_product_id"))

    filled = {
        a.get("attribute_name") or a.get("name", "")
        for a in attributes
        if a.get("value_name") and a.get("value_name") not in ("", "N/A")
    }

    req_attrs = category_attrs.get("required", [])
    opt_attrs = category_attrs.get("optional", [])
    req_filled = sum(1 for a in req_attrs if a["name"] in filled)
    opt_filled = sum(1 for a in opt_attrs if a["name"] in filled)

    req_pct = (req_filled / len(req_attrs) * 100) if req_attrs else 100.0
    opt_pct = (opt_filled / len(opt_attrs) * 100) if opt_attrs else 0.0

    title_lower    = title.lower()
    kws_core       = [k["keyword"] for k in top_keywords if k.get("compatibilidad") in ("alta", "media")][:8]
    kw_in_title    = [k for k in kws_core if k.lower() in title_lower]
    kw_missing     = [k for k in kws_core if k.lower() not in title_lower]
    kw_pct         = (len(kw_in_title) / len(kws_core) * 100) if kws_core else 0.0

    photo_count = len(pictures)
    photo_score = min(photo_count / 10 * 100, 100)

    breakdown = {
        "attrs_required": round(req_pct / 100 * _ML_SCORE_WEIGHTS["attrs_required"], 1),
        "attrs_optional": round(opt_pct / 100 * _ML_SCORE_WEIGHTS["attrs_optional"], 1),
        "title_keywords": round(kw_pct / 100 * _ML_SCORE_WEIGHTS["title_keywords"], 1),
        "photos":         round(photo_score / 100 * _ML_SCORE_WEIGHTS["photos"], 1),
        "free_shipping":  _ML_SCORE_WEIGHTS["free_shipping"] if free_shipping else 0,
        "catalog_match":  _ML_SCORE_WEIGHTS["catalog_match"] if catalog_match else 0,
    }

    return {
        "total":              round(sum(breakdown.values())),
        "attrs_required_pct": round(req_pct, 1),
        "attrs_optional_pct": round(opt_pct, 1),
        "photos":             photo_count,
        "free_shipping":      free_shipping,
        "catalog_match":      catalog_match,
        "breakdown":          breakdown,
        "missing_required":   [a["name"] for a in req_attrs if a["name"] not in filled],
        "missing_optional":   [a["name"] for a in opt_attrs if a["name"] not in filled],
        "kw_in_title":        kw_in_title,
        "kw_missing":         kw_missing,
    }


# ══════════════════════════════════════════════════════════════════════════════
# AUDITOR DE TÍTULO — detecta violaciones actuales antes de optimizar
# ══════════════════════════════════════════════════════════════════════════════

_TITLE_PROHIBITED_WORDS = {
    "envío gratis", "cuotas", "nuevo", "usado", "oferta", "promo", "oficial",
    "garantía", "envio gratis", "descuento", "rebaja", "liquidación", "liquidacion",
}
_TITLE_INFRINGEMENT_TERMS = {"símil", "simil", "tipo", "igual a", "estilo"}
_TITLE_SYMBOLS = set('@#*-!_+/|;:.,()[]{}=<>%$&"\'\\^~`')
_TITLE_STOPWORDS_START = {"de", "para", "con", "el", "la", "los", "las", "un", "una",
                           "y", "o", "a", "en", "por", "al", "del"}


def audit_title(title: str) -> list:
    """Audita el título actual contra las reglas de ML.
    Devuelve lista de dicts con {nivel, regla, detalle, sugerencia}.
    nivel: 'critico' | 'advertencia'
    """
    violations = []
    t = title.strip()
    t_lower = t.lower()
    words = t_lower.split()

    # 1. Longitud
    if len(t) > 60:
        violations.append({
            "nivel":      "critico",
            "regla":      "Título demasiado largo",
            "detalle":    f"{len(t)} caracteres — ML trunca y penaliza todo lo que supere 60",
            "sugerencia": f"Reducir {len(t) - 60} caracteres",
        })

    # 2. Símbolos prohibidos
    found_symbols = sorted({c for c in t if c in _TITLE_SYMBOLS})
    if found_symbols:
        violations.append({
            "nivel":      "critico",
            "regla":      "Símbolos prohibidos por ML",
            "detalle":    f"Se encontraron: {' '.join(found_symbols)}",
            "sugerencia": "Eliminar todos los símbolos — ML los penaliza directamente",
        })

    # 3. Palabras en MAYÚSCULAS SOSTENIDAS (≥3 letras, todo caps)
    caps_words = [w for w in t.split() if len(w) >= 3 and w.isupper() and w.isalpha()]
    if caps_words:
        violations.append({
            "nivel":      "critico",
            "regla":      "Palabras en MAYÚSCULAS sostenidas",
            "detalle":    f"{', '.join(caps_words)}",
            "sugerencia": "Usar solo la primera letra en mayúscula (Título Case o sentence case)",
        })

    # 4. Palabras prohibidas por ML
    found_prohibited = [p for p in _TITLE_PROHIBITED_WORDS if p in t_lower]
    if found_prohibited:
        violations.append({
            "nivel":      "critico",
            "regla":      "Palabras prohibidas por ML",
            "detalle":    f"{', '.join(f'\"{p}\"' for p in found_prohibited)}",
            "sugerencia": "ML filtra estas palabras y pueden causar baja o eliminación de la publicación",
        })

    # 5. Términos de infracción de marca
    found_infr = [p for p in _TITLE_INFRINGEMENT_TERMS if re.search(r'\b' + re.escape(p) + r'\b', t_lower)]
    if found_infr:
        violations.append({
            "nivel":      "critico",
            "regla":      "Términos de infracción de marca",
            "detalle":    f"{', '.join(f'\"{p}\"' for p in found_infr)}",
            "sugerencia": "Pueden causar baja o suspensión — eliminar inmediatamente",
        })

    # 6. Palabras repetidas
    word_counts = {}
    for w in words:
        if len(w) > 3:  # ignorar palabras cortas/artículos
            word_counts[w] = word_counts.get(w, 0) + 1
    repeated = [w for w, n in word_counts.items() if n > 1]
    if repeated:
        violations.append({
            "nivel":      "advertencia",
            "regla":      "Palabras repetidas",
            "detalle":    f"{', '.join(repeated)}",
            "sugerencia": "ML penaliza el keyword stuffing — usar cada palabra una sola vez",
        })

    # 7. Empieza con palabra vacía
    if words and words[0] in _TITLE_STOPWORDS_START:
        violations.append({
            "nivel":      "advertencia",
            "regla":      "Empieza con palabra vacía",
            "detalle":    f"El título empieza con \"{words[0]}\" — desperdicia el token más importante",
            "sugerencia": "El primer token debe ser la keyword principal (sustantivo del producto)",
        })

    # 8. Espacios múltiples o caracteres raros invisibles
    if "  " in t or "\t" in t or "\n" in t:
        violations.append({
            "nivel":      "advertencia",
            "regla":      "Espacios o caracteres invisibles",
            "detalle":    "Espacios dobles o tabulaciones detectados",
            "sugerencia": "Limpiar el título — pueden afectar el parsing del algoritmo",
        })

    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Helpers de datos ML
# ══════════════════════════════════════════════════════════════════════════════

def _get_ml_quality_score(item_id: str, token: str) -> dict:
    """Score oficial de calidad de ML para la publicación del vendedor.
    Devuelve dict con 'score' (0-100), 'level' y 'reasons'. Vacío si no disponible."""
    import sys
    try:
        r = requests.get(
            f"{ML_API}/items/{item_id}/quality_score",
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        if r.ok:
            data = r.json()
            # Normalizar distintos formatos que puede devolver la API
            score = (data.get("score") or data.get("quality_score") or
                     data.get("overall_score") or 0)
            level = data.get("level") or data.get("score_level") or ""
            reasons_raw = data.get("reasons") or data.get("issues") or []
            reasons = []
            for rr in reasons_raw:
                if isinstance(rr, dict):
                    txt = (rr.get("description") or rr.get("message") or
                           rr.get("reason_id") or str(rr))
                    reasons.append(txt)
                elif isinstance(rr, str):
                    reasons.append(rr)
            if score:
                return {"score": int(score), "level": level, "reasons": reasons, "_raw": data}
            # Score 0 puede ser un item recién publicado — igualmente devolvemos el raw
            if data:
                print(f"[quality_score] {item_id} → respuesta sin score reconocible: {data}", file=sys.stderr)
        else:
            print(f"[quality_score] {item_id} → HTTP {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"[quality_score] {item_id} → {e}", file=sys.stderr)
    return {}


def _get_item(item_id: str, token: str) -> dict:
    try:
        r = requests.get(f"{ML_API}/items/{item_id}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=8)
        if r.ok:
            return r.json()
        import sys
        print(f"[_get_item] {item_id} → HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return {}
    except Exception as e:
        import sys
        print(f"[_get_item] {item_id} → excepción: {e}", file=sys.stderr)
        return {}


def _get_description(item_id: str, token: str) -> str:
    try:
        r = requests.get(f"{ML_API}/items/{item_id}/description",
                         headers={"Authorization": f"Bearer {token}"}, timeout=8)
        return r.json().get("plain_text", "").strip() if r.ok else ""
    except Exception:
        return ""


def _get_category_name(category_id: str) -> str:
    try:
        r = requests.get(f"{ML_API}/categories/{category_id}", timeout=6)
        return r.json().get("name", category_id) if r.ok else category_id
    except Exception:
        return category_id


def _get_category_attributes(category_id: str, token: str) -> dict:
    required, optional = [], []
    try:
        r = requests.get(
            f"{ML_API}/categories/{category_id}/attributes",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if not r.ok:
            return {"required": [], "optional": []}
        for attr in r.json():
            name = attr.get("name", "")
            if not name:
                continue
            tags  = attr.get("tags", {})
            entry = {
                "id":     attr.get("id", ""),
                "name":   name,
                "values": [v.get("name") for v in attr.get("values", []) if v.get("name")],
            }
            if tags.get("required") or tags.get("catalog_required"):
                required.append(entry)
            else:
                optional.append(entry)
    except Exception:
        pass
    return {"required": required, "optional": optional}


# ══════════════════════════════════════════════════════════════════════════════
# M6 — OPTIMIZATION ENGINE (Claude)
# ══════════════════════════════════════════════════════════════════════════════

def _build_analysis_prompt(
    item_data: dict,
    description: str,
    keyword_analysis: list,
    position_data: list,
    competitors: list,
    comp_patterns: dict,
    root_causes: list,
    difficulty: dict,
    ml_score: dict,
    category_attrs: dict,
    category_name: str,
    ml_quality_oficial: dict = None,
    title_violations: list = None,
    is_new_listing: bool = False,
) -> str:
    title        = item_data.get("title", "")
    price        = float(item_data.get("price", 0))
    listing_type = item_data.get("listing_type_id", "")
    tipo_pub     = "PREMIUM" if listing_type in ("gold_special", "gold_pro") else "CLÁSICA"
    free_ship    = item_data.get("shipping", {}).get("free_shipping", False)
    full_ship    = item_data.get("shipping", {}).get("logistic_type", "") == "fulfillment"
    photos       = len(item_data.get("pictures", []))
    my_sold      = item_data.get("sold_quantity", 0)

    sold_note = (
        f"ATENCIÓN: {my_sold} ventas registradas — el título NO se puede modificar en ML. "
        f"Igualmente generá los títulos alternativos para una nueva publicación o variante."
        if my_sold > 0
        else "Sin ventas — el título SÍ se puede cambiar."
    )

    # Bloque de keywords con scores — separado por tiers para jerarquía clara
    def _build_kw_block_tiered(kw_analysis: list) -> str:
        t1, t2, t3, info = [], [], [], []
        for k in kw_analysis[:15]:
            pos_s  = f"pos #{k['posicion_actual']}" if k["posicion_actual"] else "no rankea"
            label  = f"  \"{k['keyword']}\" [{k['priority_score']:.0f}pts | {k['tipo_intencion']} | {pos_s} | {'✓ título' if k['en_titulo_actual'] else '✗ falta'} | {k['presencia_competidores']}% comps]"
            intent = k['tipo_intencion']
            score  = k['priority_score']
            if intent == "informativa":
                info.append(label)
            elif score >= 50:
                t1.append(label)
            elif score >= 25:
                t2.append(label)
            else:
                t3.append(label)
        lines = []
        if t1:
            lines.append("TIER 1 — MÁXIMO VOLUMEN (posición 1-2 del título):")
            lines.extend(t1)
        if t2:
            lines.append("TIER 2 — VOLUMEN ALTO (complementar título o descripción):")
            lines.extend(t2)
        if t3:
            lines.append("TIER 3 — PERFILES ALTERNATIVOS (título alternativo 3 o descripción):")
            lines.extend(t3)
        if info:
            lines.append("INFORMACIONALES — solo descripción, NUNCA en título:")
            lines.extend(info)
        return "\n".join(lines) or "  (sin datos)"

    kw_block = _build_kw_block_tiered(keyword_analysis)

    # Bloque de posiciones
    pos_lines = []
    for p in position_data:
        ap = f"posición #{p['position']}" if p["aparece"] else "no aparece en top 48"
        pos_lines.append(
            f"  \"{p['keyword']}\" → {ap} | {p['total_resultados']:,} resultados | saturación: {p['saturacion']}"
        )
    pos_block = "\n".join(pos_lines) or "  (sin datos)"

    # Bloque de competidores
    comp_lines = []
    for i, c in enumerate(competitors, 1):
        kw_gap = set(_tokenize(c.get("title", ""))) - set(_tokenize(title))
        lines = [
            f"━━ COMPETIDOR {i}: {c.get('title', '')} ━━",
            f"Vendedor: {c.get('seller','—')} | Ventas: {c.get('sold_quantity',0):,} | "
            f"Precio: ${c.get('price',0):,.0f} | Fotos: {c.get('photos_count',0)} | "
            f"{'PREMIUM' if c.get('premium') else 'CLÁSICA'} | "
            f"{'Full' if c.get('full_ship') else 'Envío gratis' if c.get('free_ship') else 'Envío pago'}",
        ]
        if c.get("attributes"):
            lines.append("Ficha: " + " | ".join(f"{a['name']}: {a['value']}" for a in c["attributes"][:10]))
        if c.get("description"):
            lines.append(f"Descripción: {c['description'][:400]}")
        if kw_gap:
            lines.append(f"Keywords que ellos tienen y yo no: {', '.join(sorted(kw_gap)[:5])}")
        comp_lines.append("\n".join(lines))
    comp_block = "\n\n".join(comp_lines) or "Sin competidores disponibles."

    # Root causes ya calculadas
    rc_lines = "\n".join(
        f"  [{c['impacto'].upper()}] {c['causa']} — {c['detalle']} ({c['tipo']})"
        for c in root_causes
    ) or "  Sin causas identificadas."

    # Atributos
    req_attrs = category_attrs.get("required", [])
    miss_req  = ml_score.get("missing_required", [])
    miss_opt  = ml_score.get("missing_optional", [])

    if is_new_listing:
        return f"""Sos un ingeniero SEO de marketplace y estratega de conversión especializado en MercadoLibre Argentina.
Tenés acceso a datos REALES del mercado. Tu análisis debe ser preciso, accionable y basado únicamente en estos datos.

JERARQUÍA DE FUENTES: autosuggest ML = fuente primaria (refleja búsquedas reales de compradores).
Competidores = fuente secundaria (patrones observables, no autoridad). Nunca invertir esta prioridad.

CONTEXTO: Este es un PRODUCTO NUEVO a lanzar — no existe publicación propia aún.
El objetivo es entender el mercado, identificar oportunidades y preparar la publicación perfecta desde el día 1.

═══ PRODUCTO A LANZAR ═══
Producto: {title}
Categoría detectada: {category_name}
Precio esperado: ${price:,.0f}
Dificultad de entrada: {difficulty.get('nivel', '?').upper()}

═══ M1: KEYWORD ANALYSIS — FUENTE PRINCIPAL ═══
(40% posición autosuggest + 25% presencia competidores + 15% en título + 10% semántica)
{kw_block}

Keywords con mayor volumen de búsqueda: {', '.join(f'"{k}"' for k in ml_score.get('kw_missing', [])[:6]) or 'ver tabla arriba'}

═══ M3: COMPETITOR INTELLIGENCE ═══
{comp_block}

PATRONES DETECTADOS EN COMPETIDORES:
Keywords frecuentes en títulos top: {', '.join(comp_patterns.get('keywords_frecuentes', [])[:10])}
Keywords gap (oportunidades no aprovechadas): {', '.join(comp_patterns.get('keywords_gap', [])[:8])}
Longitud promedio de títulos: {comp_patterns.get('avg_title_length', 0)} chars
Precio promedio competidores: ${comp_patterns.get('avg_price', 0):,.0f}

═══ M5: DIFFICULTY SCORE: {difficulty.get('nivel', '?').upper()} ═══
Factores: {' | '.join(difficulty.get('factores', ['Sin datos suficientes']))}

═══ ATRIBUTOS CATEGORÍA "{category_name}" ═══
Requeridos a completar ({len(miss_req)}): {', '.join(miss_req[:8]) or 'ninguno'}
Opcionales recomendados ({len(miss_opt)}): {', '.join(miss_opt[:8]) or 'ninguno'}

─────────────────────────────────────────────────────
INSTRUCCIÓN: Analizá el mercado desde la perspectiva de un nuevo entrante.
Identificá debilidades de los competidores que son oportunidades de diferenciación.
Separar claramente: DATO REAL vs INFERENCIA.
─────────────────────────────────────────────────────

## ANÁLISIS DE COMPETIDORES
[Por competidor: fortalezas y debilidades concretas. Qué hacen bien y qué dejan sin cubrir. Máximo 3 bullets por competidor.]

## OPORTUNIDAD DE ENTRADA
[Dónde están los gaps reales del mercado: keywords no aprovechadas, objeciones sin responder, segmentos sin atender. Máximo 5 bullets concretos basados en los datos.]

## PUNTAJE DE CALIDAD DE COMPETIDORES
[Para los 3 mejores competidores: puntaje estimado de su publicación en título/ficha/descripción/fotos. Identificar cuál es el más vulnerable y por qué.]

## ANÁLISIS DE FICHA TÉCNICA
Atributos requeridos de la categoría ({len(miss_req)}): {', '.join(miss_req[:6]) or 'ninguno'}
Opcionales importantes ({len(miss_opt)}): {', '.join(miss_opt[:6]) or 'ninguno'}
[Para cada atributo requerido: qué valor típico usan los competidores. Solo datos observables.]"""

    return f"""Sos un ingeniero SEO de marketplace y estratega de conversión especializado en MercadoLibre Argentina.
Tenés acceso a datos REALES del mercado. Tu análisis debe ser preciso, accionable y basado únicamente en estos datos.

JERARQUÍA DE FUENTES: autosuggest ML = fuente primaria (refleja búsquedas reales de compradores).
Competidores = fuente secundaria (patrones observables, no autoridad). Nunca invertir esta prioridad.

{sold_note}

═══ PUBLICACIÓN ACTUAL ═══
Título ({len(title)} chars): {title}
Categoría: {category_name} | Precio: ${price:,.0f} | Tipo: {tipo_pub}
Shipping: {'Full' if full_ship else 'Envío gratis' if free_ship else 'Envío pago'} | Fotos: {photos} | Ventas: {my_sold:,}
Score interno (estimado): {ml_score['total']}/100{(chr(10) + 'VIOLACIONES DETECTADAS EN TÍTULO ACTUAL:' + chr(10) + chr(10).join('  [' + v['nivel'].upper() + '] ' + v['regla'] + ': ' + v['detalle'] + ' → ' + v['sugerencia'] for v in (title_violations or []))) if title_violations else (chr(10) + '✓ Título actual sin violaciones de reglas ML')}{(chr(10) + 'Score OFICIAL ML: ' + str((ml_quality_oficial or {}).get('score', '')) + '/100' + (' — ' + (ml_quality_oficial or {}).get('level', '') if (ml_quality_oficial or {}).get('level') else '') + (chr(10) + 'Razones ML: ' + ' | '.join((ml_quality_oficial or {}).get('reasons', [])[:5]) if (ml_quality_oficial or {}).get('reasons') else '')) if (ml_quality_oficial or {}).get('score') else ''}
Descripción ({len(description)} chars): {description[:400] or '(sin descripción)'}

═══ M1: KEYWORD ANALYSIS — FUENTE PRINCIPAL ═══
(40% posición autosuggest + 25% presencia competidores + 15% en título + 10% posición actual + 10% semántica)
{kw_block}

Keywords ya en el título: {', '.join(f'"{k}"' for k in ml_score.get('kw_in_title', [])) or 'ninguna'}
Keywords core FALTANTES: {', '.join(f'"{k}"' for k in ml_score.get('kw_missing', [])) or 'ninguna'}

═══ M2: POSITION TRACKING ═══
{pos_block}

═══ M3: COMPETITOR INTELLIGENCE ═══
{comp_block}

PATRONES DETECTADOS EN COMPETIDORES:
Keywords frecuentes en títulos top: {', '.join(comp_patterns.get('keywords_frecuentes', [])[:10])}
Keywords gap (ellos tienen, yo no): {', '.join(comp_patterns.get('keywords_gap', [])[:8])}
Longitud promedio de títulos: {comp_patterns.get('avg_title_length', 0)} chars
Precio promedio competidores: ${comp_patterns.get('avg_price', 0):,.0f}

═══ M4: ROOT CAUSES (ya calculadas) ═══
{rc_lines}

═══ M5: DIFFICULTY SCORE: {difficulty.get('nivel', '?').upper()} ═══
Factores: {' | '.join(difficulty.get('factores', ['Sin datos suficientes']))}

═══ ATRIBUTOS CATEGORÍA "{category_name}" ═══
Requeridos sin completar ({len(miss_req)}): {', '.join(miss_req[:8]) or 'ninguno'}
Opcionales sin completar ({len(miss_opt)}): {', '.join(miss_opt[:8]) or 'ninguno'}

─────────────────────────────────────────────────────
INSTRUCCIÓN: Validá y enriquecé el análisis. Identificá causas adicionales que los datos sugieran
(especialmente intención de búsqueda y alineación semántica). Usá formato bullet conciso.
Separar claramente: DATO REAL vs INFERENCIA.
─────────────────────────────────────────────────────

## VALIDACIÓN DE ROOT CAUSES
[Confirmá, ajustá el impacto o agregá causas que los datos sugieran pero no estén listadas]

## ANÁLISIS DE COMPETIDORES
[Por competidor: qué tiene que yo no tengo. Máximo 3 bullets por competidor. Solo datos concretos.]

## PUNTAJE DE CALIDAD ACTUAL
TÍTULO: [X/10] — [razón concreta] — [{'⚠ NO modificable' if my_sold > 0 else '✓ modificable'}]
FICHA TÉCNICA: [X/10] — [razón]
DESCRIPCIÓN: [X/10] — [{len(description)} chars — razón]
FOTOS: [X/10] — [{photos} fotos]
SHIPPING: [X/10] — [{'Full ✓' if full_ship else 'gratis' if free_ship else 'pago ✗'}]
PRECIO: [X/10] — [vs ${comp_patterns.get('avg_price', 0):,.0f} promedio competidores]
TIPO: [X/10] — [{tipo_pub}]
TOTAL: [X/70] → [BAJO / MEDIO / BUENO / EXCELENTE]

## ANÁLISIS DE FICHA TÉCNICA
Requeridos faltantes ({len(miss_req)}): {', '.join(miss_req[:6]) or 'ninguno'}
Opcionales importantes faltantes ({len(miss_opt)}): {', '.join(miss_opt[:6]) or 'ninguno'}
[Para cada faltante relevante: impacto concreto en visibilidad/conversión y orden de prioridad para completar. Solo datos reales — no inventar valores. Máximo 6 bullets.]

## ANÁLISIS DE DESCRIPCIÓN
[Longitud, estructura, keywords ausentes, argumento de conversión más urgente]"""


def _build_faq_addendum(own_questions: list, qa_insights: str) -> str:
    """Construye el bloque de instrucción FAQ que se agrega al final del prompt de síntesis."""
    lines = []

    if own_questions:
        lines.append("\nPREGUNTAS REALES DE COMPRADORES DE ESTE ITEM:")
        for q in own_questions[:20]:
            lines.append(f"  - {q}")

    has_comp_qa = bool(qa_insights and "PREGUNTAS FRECUENTES" in qa_insights.upper()
                       or qa_insights and "DUDA" in qa_insights.upper())

    if not lines and not has_comp_qa:
        return ""

    addendum = "\n\n═══ BLOQUE 10 OBLIGATORIO — FAQ AL FINAL DE LA DESCRIPCIÓN ═══\n"
    addendum += "Después de los 9 bloques de descripción, agregá una sección de preguntas frecuentes.\n"
    addendum += "Fuentes a usar (en orden de prioridad):\n"
    if lines:
        addendum += "\n".join(lines) + "\n"
    if has_comp_qa:
        addendum += "  + Las dudas y preguntas frecuentes detectadas en los Q&A de competidores (ya presentes en el análisis anterior).\n"
    addendum += """
REGLAS DEL BLOQUE FAQ:
- Elegí las 5 preguntas más frecuentes o que más impactan en la decisión de compra
- Respondé cada una de forma directa y convincente (1–2 oraciones, español rioplatense)
- Las respuestas deben eliminar dudas reales, no repetir lo ya dicho en la descripción
- CRÍTICO — redactar las preguntas exactamente como las buscaría un comprador en ML o Google:
  → búsqueda conversacional y específica, no técnica formal
  → incluir la keyword o término del producto dentro de la pregunta misma
  → Ejemplo correcto: "¿El cortador puntas abiertas funciona para cabello muy rizado?"
  → Ejemplo incorrecto: "¿Es el producto apto para cabellos con textura rizada?"
  → ML indexa el texto de las preguntas — una pregunta bien redactada captura búsquedas long-tail reales
- Sin markdown, sin bullets, solo texto plano con este formato exacto:

PREGUNTAS FRECUENTES

P: [pregunta 1]
R: [respuesta 1]

P: [pregunta 2]
R: [respuesta 2]

P: [pregunta 3]
R: [respuesta 3]

P: [pregunta 4]
R: [respuesta 4]

P: [pregunta 5]
R: [respuesta 5]"""
    return addendum


def _build_synthesis_prompt(
    item_data: dict,
    description: str,
    keyword_analysis: list,
    category_attrs: dict,
    category_name: str,
    root_causes: list,
    comp_patterns: dict,
    analysis_text: str,
    gap_keywords: list = None,
    catalog_linked: bool = False,
    catalog_available: bool = False,
    my_photos: int = 0,
    my_price: float = 0.0,
    qa_insights: str = "",
    own_questions: list = None,
    keyword_clusters: list = None,
) -> str:
    title        = item_data.get("title", "")
    my_sold      = item_data.get("sold_quantity", 0)
    listing_type = item_data.get("listing_type_id", "")
    is_premium   = listing_type in ("gold_special", "gold_pro")

    req_attrs = category_attrs.get("required", [])
    opt_attrs = category_attrs.get("optional", [])

    def fmt_attr(a):
        line = f"  {a['name']}"
        if a.get("values"):
            line += f" → posibles: {', '.join(a['values'][:5])}"
        return line

    attrs_block = ""
    if req_attrs:
        attrs_block += "OBLIGATORIOS:\n" + "\n".join(fmt_attr(a) for a in req_attrs)
    if opt_attrs:
        attrs_block += f"\nOPCIONALES ({len(opt_attrs)}):\n" + "\n".join(fmt_attr(a) for a in opt_attrs[:20])

    # Atributos de texto libre — oportunidad SEO para keywords que no entraron en el título
    _all_attrs = req_attrs + opt_attrs
    _free_text_attrs = [a for a in _all_attrs if not a.get("values") and a.get("name")]
    free_text_block = ""
    if _free_text_attrs:
        free_text_block = (
            "\nATRIBUTOS DE TEXTO LIBRE — OPORTUNIDAD SEO CRÍTICA:\n"
            "Estos campos NO tienen valores predefinidos → aceptan cualquier texto.\n"
            "Estrategia: usá las variantes del cluster y long-tails que NO entraron en el título.\n"
            "ML indexa estos campos de forma independiente al título — reforzás la señal de relevancia\n"
            "para búsquedas secundarias sin tocar el límite de 60 caracteres del título.\n"
            "REGLAS: no repetir la keyword principal exacta del título, no usar marcas de competidores,\n"
            "no poner texto promocional (oferta, descuento, gratis). Solo términos descriptivos reales.\n\n"
            "Campos disponibles:\n"
            + "\n".join(f"  • {a['name']}" for a in _free_text_attrs[:10])
        )

    # Keywords por tiers — jerarquía explícita para generación de títulos
    _t1s, _t2s, _t3s, _inf = [], [], [], []
    for k in keyword_analysis[:15]:
        _lbl  = f"  \"{k['keyword']}\" [{k['priority_score']:.0f}pts | {k['tipo_intencion']} | {'✓' if k['en_titulo_actual'] else '✗'}]"
        if k['tipo_intencion'] == "informativa":
            _inf.append(_lbl)
        elif k['priority_score'] >= 50:
            _t1s.append(_lbl)
        elif k['priority_score'] >= 25:
            _t2s.append(_lbl)
        else:
            _t3s.append(_lbl)
    _kw_lines = []
    if _t1s:
        _kw_lines.append("TIER 1 — MÁXIMO VOLUMEN (primer token obligatorio en títulos 1 y 2):")
        _kw_lines.extend(_t1s)
    if _t2s:
        _kw_lines.append("TIER 2 — VOLUMEN ALTO (complementar título o descripción):")
        _kw_lines.extend(_t2s)
    if _t3s:
        _kw_lines.append("TIER 3 — PERFILES ALTERNATIVOS (título alternativo 3):")
        _kw_lines.extend(_t3s)
    if _inf:
        _kw_lines.append("INFORMACIONALES — solo descripción, NUNCA en título:")
        _kw_lines.extend(_inf)
    kw_block = "\n".join(_kw_lines) or "  (no disponible)"

    # Clusters de keywords — indica variantes intercambiables y su poder real
    cluster_block = ""
    if keyword_clusters:
        cl_lines = ["CLUSTERS DE KEYWORDS (agrupadas por significado similar):"]
        cl_lines.append("  Regla: usá el REPRESENTANTE en el título; las VARIANTES en descripción y atributos.")
        for cl in keyword_clusters[:10]:
            rep   = cl['representative']
            pos   = cl['best_pos']
            qc    = cl['query_count']
            power = f"pos {pos}" + (f" ×{qc}q" if qc >= 2 else "")
            if cl['variants']:
                cl_lines.append(f"  • [{power}] \"{rep}\" ← variantes: {', '.join(repr(v) for v in cl['variants'][:3])}")
            else:
                cl_lines.append(f"  • [{power}] \"{rep}\" (sin variantes)")
        cluster_block = "\n".join(cl_lines)

    # Root causes de alto impacto
    high_causes = [c for c in root_causes if c["impacto"] == "alto"]
    rc_summary  = " | ".join(c["causa"] for c in high_causes) or "Sin causas de alto impacto"

    # Modificabilidad del título
    sold_note = (
        f"ATENCIÓN: {my_sold} ventas — título NO modificable en ML. "
        f"Generá igualmente los 3 títulos alternativos (para nueva publicación o referencia)."
        if my_sold > 0
        else "Sin ventas — título SÍ modificable directamente."
    )

    # Prueba social real disponible
    social_proof_note = ""
    if my_sold > 0:
        social_proof_note = f"\nPRUEBA SOCIAL: {my_sold} unidades vendidas — dato real para usar en bloque 8 de la descripción."
    if is_premium:
        social_proof_note += "\nPUBLICACIÓN PREMIUM (Gold Special/Pro) — mencionable como diferencial de confianza si es relevante."

    # Precios
    avg_price = comp_patterns.get("avg_price", 0)
    price_min = comp_patterns.get("price_range", {}).get("min", 0)
    price_max = comp_patterns.get("price_range", {}).get("max", 0)
    price_block = ""
    if avg_price > 0:
        price_block = (
            f"\nPRECIOS:\n"
            f"  Mi precio actual: ${my_price:,.0f}\n"
            f"  Promedio competidores: ${avg_price:,.0f}\n"
            f"  Rango: ${price_min:,.0f} – ${price_max:,.0f}"
        )

    # Fotos
    avg_photos = comp_patterns.get("avg_photos", 0)
    max_photos = comp_patterns.get("max_photos", 0)
    photos_block = ""
    if avg_photos > 0 or max_photos > 0:
        photos_block = (
            f"\nFOTOS:\n"
            f"  Mis fotos actuales: {my_photos}\n"
            f"  Promedio competidores: {avg_photos}\n"
            f"  Máximo competidor: {max_photos}"
        )

    # Catálogo
    catalog_block = ""
    if catalog_available and not catalog_linked:
        catalog_block = "\nCATÁLOGO ML: Existe producto de catálogo disponible — publicación NO vinculada (oportunidad)."
    elif catalog_linked:
        catalog_block = "\nCATÁLOGO ML: Publicación YA vinculada al catálogo."

    # Contexto de categoría: usar Q&A real si hay, fallback estático si no
    if qa_insights:
        buyer_context = (
            "\nVOZ DEL COMPRADOR REAL — Q&A y reseñas de competidores (fuente primaria):\n"
            + _smart_truncate(qa_insights, 1500)
        )
    else:
        static_ctx = _get_category_context(category_name)
        buyer_context = (
            f"\nCONTEXTO DE NICHO (fallback — sin Q&A real disponible):\n{static_ctx}"
            if static_ctx else ""
        )

    # Gap keywords block
    gap_block = ""
    if gap_keywords:
        gap_block = (
            "\nGAP KEYWORDS — lenguaje del mercado que tus competidores usan y vos no tenés:\n"
            "  → Incorporar al menos las 2 más importantes: en TÍTULO y en DESCRIPCIÓN de forma natural.\n"
            "  → En campos de texto libre de la ficha: sí permitido (ML los indexa).\n"
            "  → PROHIBIDO ponerlas en campos con valores predefinidos (los que tienen lista fija de opciones).\n"
            + "\n".join(f"  ▸ {kw}" for kw in gap_keywords[:10])
        )

    # Longitud adaptativa según complejidad del producto
    is_complex  = _is_complex_product(category_name)
    desc_range  = "1200–2500 caracteres" if is_complex else "600–1200 caracteres"
    desc_type   = "producto complejo — justifica detalle técnico extendido" if is_complex else "producto simple — descripción directa, sin relleno"

    return f"""Sos un experto senior en SEO y conversión para MercadoLibre Argentina.
Tu objetivo es producir el contenido más competitivo posible usando los datos reales del mercado provistos.
Nunca inventés características técnicas. Nunca usés frases genéricas. Siempre priorizá claridad sobre creatividad.

{sold_note}{social_proof_note}

═══ DATOS REALES DEL MERCADO ═══
TÍTULO ACTUAL ({len(title)} chars): {title}
CATEGORÍA: {category_name}
CAUSAS DE ALTO IMPACTO: {rc_summary}{price_block}{photos_block}{catalog_block}

KEYWORDS REALES DEL AUTOSUGGEST ML (fuente primaria — prioridad absoluta sobre todo):
{kw_block}
{("" + cluster_block + chr(10)) if cluster_block else ""}{gap_block}

ATRIBUTOS OFICIALES DE LA CATEGORÍA "{category_name}":
{attrs_block}
{free_text_block}
ANÁLISIS PREVIO (Haiku):
{_smart_truncate(analysis_text, 2500)}
{buyer_context}

═══ PASO 0 — COMPROMISO DE KEYWORDS (escribirlo explícitamente como primera sección del output) ═══
Antes de generar cualquier contenido, declarar por escrito las keywords elegidas.
Esto es obligatorio — sirve como ancla para toda la generación posterior.
Formato exacto a reproducir en ## KEYWORDS ELEGIDAS:

  keyword_principal: [una sola keyword del autosuggest con priority_score más alto y compatibilidad alta/media]
  keywords_secundarias: [kw2], [kw3], [kw4] — misma condición
  long_tail: [frase1], [frase2], [frase3], [frase4], [frase5] — 3+ palabras, intención transaccional
  EXCLUIDAS: [cualquier keyword con compatibilidad "peligrosa" o intención solo informativa — listarlas]

El autosuggest tiene prioridad absoluta. Los competidores complementan solo si no contradicen el autosuggest.

═══ INSTRUCCIONES POR SECCIÓN ═══

──── CORRECCIONES DE TÍTULO ────
{"Con ventas: NO se puede cambiar el título completo. Solo correcciones mínimas aprobadas por ML." if my_sold > 0 else "Sin ventas: título modificable libremente. Indicar qué alternativo usar directamente."}
{"Listar 2-3 cambios específicos: qué eliminar/agregar/reemplazar y por qué." if my_sold > 0 else ""}
Formato por corrección: CAMBIO: [texto actual] → [texto nuevo] | Motivo: [una línea]

──── PRECIO RECOMENDADO ────
Analizar datos de precio del mercado y recomendar un monto concreto con estrategia.
IMPORTANTE: la recomendación es solo estratégica vs el mercado — no conocemos el costo/margen del vendedor. Siempre incluir la nota de verificación de margen.
Formato: PRECIO SUGERIDO: $[monto] | Estrategia: [una línea] | ⚠ Verificar margen antes de aplicar

──── FOTOS RECOMENDADAS ────
Comparar cantidad actual vs competidores y recomendar cantidad + tipos prioritarios.
Formato: CANTIDAD SUGERIDA: [N] | Tipos: [fondo blanco, lifestyle, detalle, comparativa, etc.]

{"──── ALERTA CATÁLOGO ────" + chr(10) + "Explicar concisamente: ventajas de vincularse (mejor posicionamiento, comparte reseñas) y desventajas (precio igualado al catálogo). ¿Conviene en este caso?" if catalog_available and not catalog_linked else ""}

──── TÍTULOS — REGLAS DEL ALGORITMO ML (PROHIBICIONES ABSOLUTAS) ────
✗ Más de 60 caracteres — contar exactamente antes de escribir
✗ Signos de puntuación o símbolos: @ # * - ! _ + / | ; : . , ( ) [ ]
✗ Palabras en MAYÚSCULAS SOSTENIDAS
✗ Palabras prohibidas por ML: "envío gratis", "cuotas", "nuevo", "usado", "oferta", "promo", "oficial"
✗ Términos de infracción de marca: "símil", "tipo", "igual a", "estilo X" — pueden bajar o eliminar la publicación
✗ Color si el producto tiene variantes de color
✗ Palabras vacías: de, para, con, el, la, los, las, un, una
✗ Palabras repetidas dentro del mismo título
✗ Keywords INFORMACIONALES en el título (solo van en descripción)

JERARQUÍA OBLIGATORIA DE KEYWORDS EN TÍTULOS:
✓ TÍTULO 1: primer token = TIER 1 keyword #1 (mayor priority_score, no informacional)
✓ TÍTULO 2: primer token = TIER 1 keyword #2 o diferente combinación de TIER 1
✓ TÍTULO 3: primer token = TIER 3 keyword (perfil alternativo de comprador)
✓ Los 3 títulos deben ser estructuralmente distintos entre sí
✓ Estructura ideal: Producto + Marca/Modelo + Especificación clave

──── FICHA TÉCNICA ────
- Nombres EXACTOS de los atributos oficiales de la categoría (los listados arriba)
- Completar TODOS los obligatorios + todos los opcionales aplicables
- Valores semánticamente correctos y limpios — sin keywords SEO dentro del valor
  EJEMPLO PROHIBIDO: COLOR = "Negro para cabello dañado" → correcto: COLOR = "Negro"
- Si el valor no se puede inferir con certeza → [SUGERIR: descripción de qué dato va aquí]
- Gap keywords y long-tail van en título, descripción y campos de texto libre — NUNCA en campos con valores predefinidos (los que tienen lista de opciones fija)

──── DESCRIPCIÓN ────
REGLAS TÉCNICAS DE ML:
- NUNCA repetir información que ya está en la ficha técnica — el comprador ya la leyó arriba. La descripción complementa, no repite
- NUNCA repetir datos que ML ya muestra al comprador: condiciones de envío, cuotas, devolución estándar, garantía propia de ML
  ACLARACIÓN: sí podés mencionar garantía PROPIA del producto/vendedor (ej: "garantía del fabricante 12 meses") y plazo de despacho propio del vendedor (ej: "despachamos en 24hs hábiles") — son datos distintos a los que ML muestra
- NUNCA inventar características técnicas no confirmadas en los datos provistos
- Texto plano únicamente — sin HTML, markdown, bullets con *, links, teléfonos, emails, URLs
- Español rioplatense (vos, tus, tu)
- PROHIBIDO: frases genéricas ("alta calidad", "excelente producto", "no te arrepentirás", "el mejor del mercado")

LONGITUD CORRECTA ({desc_type}):
- Zona crítica: PRIMEROS 300-400 CARACTERES — doble función obligatoria:
  → FUNCIÓN SEO: el algoritmo de ML decide si el contenido es relevante o genérico
     DEBEN contener: keyword_principal + qué es el producto + para quién es
  → FUNCIÓN MOBILE [CRÍTICO DE CONVERSIÓN]: más del 70% del tráfico en ML Argentina es mobile
     En mobile ML muestra SOLO estos primeros 300-400 chars antes del botón "Ver más"
     Si este tramo no engancha al comprador mobile, no expande y no compra
     DEBE funcionar como argumento de venta completo por sí solo:
     problema que resuelve + beneficio principal + para quién es → todo en ese espacio
     Ejemplo correcto: "El cortador puntas abiertas Frizz Ender elimina el daño sin acortar el largo.
     Ideal para cabello rizado, teñido o con tratamientos. Separás un mechón, lo pasás por la ranura
     y solo corta las puntas dañadas — sin tijera, sin peluquería, sin perder largo."
     Ese bloque ya vendió en mobile aunque el comprador no expanda.
- Rango objetivo: {desc_range}
- Por encima de 3000 caracteres: retornos decrecientes — evitar salvo excepción justificada
- Si la ficha técnica está bien completada → la descripción puede y debe ser más corta y enfocada

DISTRIBUCIÓN DE KEYWORDS:
- keyword_principal: 2–3 apariciones (densidad máx 1–2% del texto total — contar; más repeticiones activa filtro de spam)
- keywords_secundarias: 2–3 apariciones cada una
- long_tail: mínimo 5 frases distribuidas naturalmente — nunca forzadas
- DENSIDAD EN PRIMEROS 500 CHARS [CRÍTICO]: la keyword_principal debe aparecer al menos 2 veces
  dentro de los primeros 500 caracteres — ML tiene un gancho de indexación en ese tramo inicial
  que determina para qué búsquedas considera relevante la publicación
- CONCORDANCIA SEMÁNTICA TÍTULO-DESCRIPCIÓN [CRÍTICO]: los tokens principales del título generado
  (sustantivos y adjetivos clave) deben aparecer en los primeros 2-3 párrafos de la descripción
  → ML cruza vocabulario del título con el de la descripción para validar coherencia semántica
  → Si hay baja concordancia, puede bajar el ranking aunque cada sección esté bien optimizada
  → Ejemplo: si el título dice "Cortador Puntas Abiertas Cabello Rizado", los párrafos 1-3 deben
    usar "cortador", "puntas", "rizado" — no sinónimos distintos en esa zona inicial

ESTRUCTURA DE 9 BLOQUES (párrafos separados por línea en blanco, sin títulos ni bullets):
1. APERTURA SEO [CRÍTICO — dentro de los primeros 300 chars]: keyword_principal + problema real + beneficio concreto
   → La PRIMERA ORACIÓN debe contener la keyword_principal exacta — ML pondera el inicio del texto más que el resto
   → Ejemplo correcto: "El cortador puntas abiertas Frizz Ender elimina el daño sin acortar el largo."
   → Ejemplo incorrecto: "¿Cansado de las puntas dañadas? Este producto es para vos." (keyword ausente en la primera oración)
2. QUÉ ES + PARA QUIÉN: descripción del producto + perfil del comprador ideal
3. CÓMO FUNCIONA: mecanismo real de uso + por qué es efectivo
4. BENEFICIOS COMPROBABLES: concretos y verificables — sin generalidades
5. DIFERENCIACIÓN REAL: comparar contra los competidores analizados usando solo datos reales del análisis
6. DUDAS + NEUTRALIZACIÓN DE OBJECIONES: dos objetivos en un mismo bloque
   PARTE A — Responder dudas frecuentes: integrar en prosa natural las 3–4 preguntas más frecuentes del nicho
     → fuente primaria: DUDAS FRECUENTES DE COMPRADORES de los insights de Q&A
     → fallback: FAQs típicas del CONTEXTO DE NICHO listadas arriba
   PARTE B — Neutralizar objeciones activamente: para cada miedo/objeción del nicho, escribir una frase que lo desarme con un dato concreto (no con una promesa vacía)
     → fuente primaria: sección DEBILIDADES DE COMPETIDORES de los insights (las quejas de sus compradores = tus oportunidades de diferenciarte)
     → fallback: objeciones del CONTEXTO DE NICHO listadas arriba
     → ejemplos de neutralización correcta:
        · miedo talle → "Las medidas exactas son X cm de contorno / Y cm de largo" (no "el talle es el correcto")
        · miedo material falso → "El cuero utilizado es [tipo] verificable al tacto por [característica]" (no "es material genuino")
        · miedo refurbished → "El equipo llega sellado de fábrica con [número de serie / sticker]" (no "es original")
     → formato: prosa corrida, integrado naturalmente — nunca en formato Q&A explícito
7. CARACTERÍSTICAS TÉCNICAS: especificaciones en lenguaje del comprador — COMPLEMENTA la ficha técnica, no la repite
   → Propósito: traducir los datos técnicos de la ficha a beneficios concretos que el comprador entiende
   → NO copiar valores de la ficha técnica tal cual — transformarlos en contexto de uso real
   → Ejemplo correcto: "Potencia 2200W: suficiente para secar cabello grueso en menos de 10 minutos sin dañar la fibra."
   → Ejemplo incorrecto: "Potencia: 2200W. Peso: 350g." (eso ya está en la ficha técnica arriba)
   → Cada característica debe comenzar con el término de búsqueda más relevante
   → ML pondera las primeras palabras de cada segmento del texto
8. PRUEBA SOCIAL + CONFIANZA: {"usar los " + str(my_sold) + " ventas reales como dato de credibilidad. " if my_sold > 0 else "garantía propia, respaldo, experiencia de uso — solo datos reales, sin inventar. "}Nunca mencionar garantía ML (ya la muestra arriba)
9. CIERRE Y CTA: refuerzo del beneficio principal + llamada a la acción + keyword_principal

PALABRAS DE ALTA CONVERSIÓN — POSICIONES ESTRATÉGICAS [CRÍTICO]:
ML correlaciona estas palabras con tasas de compra altas y las pondera en el ranking.
No van en el título (muchas están prohibidas ahí). Van en la descripción en estas zonas:
  → Bloque 7 (características): "original", "certificado", "compatible con", "incluye", "viene con"
  → Bloque 8 (confianza): "garantía", "stock disponible", "entrega en [N] días hábiles", "sellado de fábrica"
  → Bloque 9 (cierre): "disponible", "unidades disponibles", "enviamos hoy", "entrega inmediata"
REGLAS: solo usar las que apliquen realmente al producto — nunca inventar garantías ni stocks que no existen.
Estas palabras en estas posiciones son señal de intención de venta real para el algoritmo.

TONO según tipo de producto (detectar y aplicar):
- Belleza/cuidado personal → resultado visual, sensación, experiencia
- Salud/soporte/ortopedia → dolor, alivio, funcionalidad real
- Electrónica/gadget → facilidad de uso, resultado concreto, compatibilidad
- Moda/indumentaria → estilo, comodidad, ocasión de uso
- Hogar/muebles → practicidad, medidas reales, integración en el espacio

CHECKLIST DE VALIDACIÓN INTERNA (completar antes de entregar):
□ keyword_principal aparece 2–3 veces — contadas (no más, evita penalización por spam)
□ 5+ long-tail keywords distribuidas en el texto
□ Longitud total dentro de {desc_range} — contada
□ Bloque 1 completo dentro de los primeros 300 chars
□ 9 bloques presentes y diferenciados
□ Sin información de ficha técnica repetida
□ Sin datos que ML ya muestra arriba
□ Sin características inventadas
□ Sin frases genéricas
□ Títulos: sin símbolos, sin CAPS, sin prohibidos, ≤60 chars cada uno (contados)
□ Título recomendado indicado con justificación

═══ FORMATO DE ENTREGA — EXACTAMENTE ESTAS SECCIONES EN ESTE ORDEN ═══

## KEYWORDS ELEGIDAS
keyword_principal: [...]
keywords_secundarias: [...], [...], [...]
long_tail: [...], [...], [...], [...], [...]
EXCLUIDAS: [...] — motivo

## CORRECCIONES DE TÍTULO
[correcciones mínimas o indicar qué alternativo usar]

## PRECIO RECOMENDADO
PRECIO SUGERIDO: $[monto] | Estrategia: [una línea]

## FOTOS RECOMENDADAS
CANTIDAD SUGERIDA: [N] | Tipos: [lista concreta]
[1–2 líneas de razonamiento basadas en los datos]
{"" if not (catalog_available and not catalog_linked) else chr(10) + "## ALERTA CATÁLOGO" + chr(10) + "[explicación concisa de la oportunidad y si conviene vincularse]"}

## TÍTULO ALTERNATIVO 1
TÍTULO: [keyword principal primero · máxima cobertura de búsqueda · ≤60 chars]
Estrategia: [una línea]

## TÍTULO ALTERNATIVO 2
TÍTULO: [keyword principal + atributo diferencial · balance SEO + legibilidad · ≤60 chars]
Estrategia: [una línea]

## TÍTULO ALTERNATIVO 3
TÍTULO: [long tail de alta intención transaccional · más específico · ≤60 chars]
Estrategia: [una línea]

## TÍTULO RECOMENDADO
OPCIÓN: [1 / 2 / 3] | Motivo: [una línea — por qué esta opción maximiza ranking + conversión para este producto específico]

## FICHA TÉCNICA PERFECTA
[atributo]: [valor]

## DESCRIPCIÓN SUPERADORA
[texto final — 9 bloques + bloque FAQ al final, párrafos separados por línea en blanco, sin títulos internos, sin markdown]{_build_faq_addendum(own_questions or [], qa_insights)}"""


def _call_claude(prompt: str, max_tokens: int = 3500, console=None, fast: bool = False) -> str:
    """Llama a Claude con streaming.
    fast=True → usa Haiku (análisis/validación estructurada, mucho más barato).
    fast=False → usa Opus (síntesis creativa, títulos y descripciones finales).
    """
    ai    = anthropic.Anthropic()
    model = "claude-haiku-4-5-20251001" if fast else "claude-opus-4-6"
    full_txt = ""

    with ai.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            full_txt += text
            if console:
                console.print(text, end="", markup=False)

    if console:
        console.print()
    return full_txt


# ══════════════════════════════════════════════════════════════════════════════
# M6 Parseo de output
# ══════════════════════════════════════════════════════════════════════════════

def _extract_section(text: str, header: str) -> str:
    pattern = rf"## {re.escape(header)}[ \t]*\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _parse_synthesis(text: str) -> dict:
    titulos = []
    for n in ["1", "2", "3"]:
        bloque = _extract_section(text, f"TÍTULO ALTERNATIVO {n}")
        if not bloque:
            continue
        lines = [re.sub(r'\*+', '', l).strip() for l in bloque.split("\n") if l.strip()]
        titulo = next(
            (l.replace("TÍTULO:", "").strip() for l in lines if l.upper().startswith("TÍTULO:")), ""
        )
        if not titulo:
            titulo = next(
                (l for l in lines if not l.lower().startswith(("estrategia", "caracter", "título:"))), ""
            )
        estrategia = next(
            (l.replace("Estrategia:", "").strip() for l in lines if l.lower().startswith("estrategia")), ""
        )
        if titulo:
            titulos.append({
                "titulo":     titulo[:60],
                "estrategia": estrategia,
            })

    # Extraer título recomendado — con regex tolerante a variaciones de formato
    tr_block  = _extract_section(text, "TÍTULO RECOMENDADO")
    tr_opcion = 0
    tr_motivo = ""
    tr_titulo = ""
    if tr_block:
        # Acepta: "OPCIÓN: 2", "OPCION 2", "**OPCIÓN: 2**", "Recomiendo la opción 2", "opción 2"
        m_op = re.search(r'opci[oó]n[^\d]*([123])', tr_block, re.IGNORECASE)
        # Si no, buscar cualquier dígito suelto 1/2/3 en el bloque
        if not m_op:
            m_op = re.search(r'\b([123])\b', tr_block)
        m_mo = re.search(r'[Mm]otivo\s*:?\s*\*{0,2}(.+)', tr_block)
        tr_opcion = int(m_op.group(1)) if m_op else 0
        tr_motivo = re.sub(r'\*+', '', m_mo.group(1)).strip() if m_mo else ""
        if tr_opcion and 1 <= tr_opcion <= len(titulos):
            tr_titulo = titulos[tr_opcion - 1]["titulo"]
    # Fallback: si no se pudo parsear, usar el primer título
    if not tr_titulo and titulos:
        tr_titulo = titulos[0]["titulo"]
        tr_opcion = 1

    return {
        "titles":                titulos,
        "titulo_principal":      titulos[0]["titulo"] if titulos else "",
        "titulo_recomendado":    tr_titulo,
        "titulo_recomendado_n":  tr_opcion,
        "titulo_recomendado_motivo": tr_motivo,
        "keywords_elegidas":     _extract_section(text, "KEYWORDS ELEGIDAS"),
        "ficha_perfecta":        _extract_section(text, "FICHA TÉCNICA PERFECTA"),
        "descripcion_nueva":     _extract_section(text, "DESCRIPCIÓN SUPERADORA"),
        "correcciones_titulo":   _extract_section(text, "CORRECCIONES DE TÍTULO"),
        "precio_recomendado":    _extract_section(text, "PRECIO RECOMENDADO"),
        "fotos_recomendadas":    _extract_section(text, "FOTOS RECOMENDADAS"),
        "alerta_catalogo":       _extract_section(text, "ALERTA CATÁLOGO"),
        "raw":                   text,
    }


# ══════════════════════════════════════════════════════════════════════════════
# M7 — CONFIDENCE LAYER
# ══════════════════════════════════════════════════════════════════════════════

def build_confidence_layer(keyword_analysis: list, root_causes: list, difficulty: dict) -> list:
    """
    M7: Adjunta nivel de confianza y tipo de evidencia a los hallazgos principales.
    Alta = dato directamente observable. Media = inferencia razonable. Baja = estimación.
    """
    items = []

    # Keywords con mayor score → alta confianza (vienen del autosuggest real)
    for k in keyword_analysis[:5]:
        conf = "alta" if k["autosuggest_position"] <= 3 else "media"
        items.append({
            "hallazgo":         f"Keyword \"{k['keyword']}\" tiene priority score {k['priority_score']}",
            "evidencia":        f"Posición {k['autosuggest_position']} en autosuggest, "
                                f"{k['presencia_competidores']}% de competidores la usan, "
                                f"posición actual: {k['posicion_actual'] or 'no rankea'}",
            "confianza":        conf,
            "tipo":             "dato_real",
        })

    # Root causes
    for c in root_causes[:4]:
        items.append({
            "hallazgo":   c["causa"],
            "evidencia":  c["detalle"],
            "confianza":  "alta" if c["tipo"] == "dato_real" else "media",
            "tipo":       c["tipo"],
        })

    # Difficulty score
    items.append({
        "hallazgo":   f"Dificultad de posicionamiento: {difficulty.get('nivel', '?').upper()}",
        "evidencia":  " | ".join(difficulty.get("factores", [])) or "Calculado con datos de competidores",
        "confianza":  "media",   # el peso real del algoritmo no es público
        "tipo":       "inferencia",
    })

    return items


# ══════════════════════════════════════════════════════════════════════════════
# Flujo principal
# ══════════════════════════════════════════════════════════════════════════════

def run_full_optimization(item_id: str, client: MLClient, console=None,
                          competitor_products=None, gap_keywords=None) -> dict:
    """
    Orquesta los 7 módulos para una publicación existente.
    Retorna dict con la estructura completa del spec.
    """
    from rich.console import Console as RC
    _c = console or RC()

    client._ensure_token()                  # refrescar primero
    token = client.account.access_token     # capturar después del refresh

    # ── Datos base ────────────────────────────────────────────────────────────
    _c.print("  [dim]Leyendo publicación...[/dim]", end=" ")
    item_data = _get_item(item_id, token)
    if not item_data:
        _c.print("[red]Error al obtener el item[/red]")
        raise ValueError(f"No se encontró la publicación {item_id}. Verificá que el ID sea correcto y pertenezca a esta cuenta.")

    # ── Chequeo de catálogo ML ────────────────────────────────────────────────
    catalog_product_id = item_data.get("catalog_product_id") or ""
    catalog_linked     = bool(catalog_product_id)
    catalog_available  = False
    if not catalog_linked:
        # Buscar si existe un producto de catálogo equivalente
        try:
            title_q = item_data.get("title", "")[:50]
            cr = requests.get(
                f"{ML_API}/products/search",
                params={"site_id": ML_SITE, "q": title_q, "limit": 1},
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            if cr.ok and cr.json().get("results"):
                catalog_available = True
        except Exception:
            pass
    description = _get_description(item_id, token)
    title       = item_data.get("title", "")
    category_id = item_data.get("category_id", "")
    _c.print("[green]✓[/green]")

    # ── Auditoría del título actual ───────────────────────────────────────────
    title_violations = audit_title(title)
    if title_violations:
        criticos = [v for v in title_violations if v["nivel"] == "critico"]
        _c.print(f"  [red]⚠ Título actual: {len(criticos)} violación(es) crítica(s) detectada(s)[/red]")
        for v in title_violations:
            color = "red" if v["nivel"] == "critico" else "yellow"
            _c.print(f"    [{color}]• {v['regla']}: {v['detalle']}[/{color}]")
    else:
        _c.print("  [green]✓ Título actual sin violaciones de reglas ML[/green]")

    # ── M1: Keyword Discovery ─────────────────────────────────────────────────
    _c.print("  [dim]M1 — Keyword discovery (autosuggest real)...[/dim]", end=" ")
    autosuggest_raw, as_position_map = get_autosuggest_keywords(title)
    _c.print(f"[green]{len(autosuggest_raw)} keywords reales[/green]")

    # ── M3: Competitor Intelligence ───────────────────────────────────────────
    if competitor_products:
        _c.print(f"  [dim]M3 — Usando {len(competitor_products)} competidores pre-seleccionados...[/dim]", end=" ")
        competitors = competitor_products
    else:
        main_kw = autosuggest_raw[0] if autosuggest_raw else title
        _c.print(f"  [dim]M3 — Competitor intel para \"{main_kw[:35]}\"...[/dim]", end=" ")
        client._ensure_token()
        competitors = fetch_competitors_full(main_kw, token, exclude_id=item_id, limit=8)
    comp_patterns = analyze_competitor_patterns(competitors, title)
    _c.print(f"[green]{len(competitors)} competidores analizados[/green]")

    # ── M1.5: Expandir keywords con vocabulario de competidores ──────────────
    # Fuentes: títulos + descripciones de competidores (primeras 300 chars) + descripción propia
    # Las descripciones contienen long-tails y sinónimos que no entran en el título de 60 chars
    _c.print("  [dim]M1.5 — Expandiendo keywords (títulos + descripciones propias y competidores)...[/dim]", end=" ")
    _comp_titles_raw = [c.get("title", "") for c in competitors if c.get("title")]
    _comp_desc_raw   = [c.get("description", "")[:300] for c in competitors if c.get("description")]
    _own_desc_chunk  = [description[:300]] if description else []
    _source_texts    = _comp_titles_raw + _comp_desc_raw + _own_desc_chunk
    _extra_kws = _competitor_seeded_autosuggest_seo(_source_texts, title)
    _seen_raw  = set(autosuggest_raw)
    _gap_kws   = []  # keywords nuevas del vocabulario competidor — las más valiosas para trackear
    for _kw in _extra_kws:
        if _kw not in _seen_raw:
            _seen_raw.add(_kw)
            autosuggest_raw.append(_kw)
            as_position_map[_kw] = {'best_pos': 5, 'query_count': 0}
            _gap_kws.append(_kw)
    _c.print(f"[green]+{len(_gap_kws)} keywords nuevas ({len(autosuggest_raw)} total)[/green]")

    # ── M2: Position Tracker — DESPUÉS de M1.5 para cubrir vocabulario real ──
    # Trackea las 6 keywords propias (autosuggest del título) + las 3 mejores
    # gap keywords de competidores — así el diagnóstico refleja el mercado completo
    _c.print("  [dim]M2 — Position tracking (propio + vocabulario competidores)...[/dim]", end=" ")
    _kws_to_track = list(dict.fromkeys(autosuggest_raw[:6] + _gap_kws[:3]))
    position_data = track_positions(item_id, _kws_to_track, token)
    ranking_n = sum(1 for p in position_data if p["aparece"])
    _gap_ranking = sum(1 for p in position_data if p["aparece"] and p["keyword"] in _gap_kws)
    _c.print(f"[green]aparecés en {ranking_n}/{len(position_data)} keywords "
             f"({_gap_ranking} del vocabulario competidor)[/green]")

    # ── M3.5: Q&A y reseñas de competidores ──────────────────────────────────
    _c.print("  [dim]M3.5 — Minería de Q&A y reseñas de competidores...[/dim]", end=" ")
    comp_ids = [c["id"] for c in competitors if c.get("id")]
    qa_raw   = fetch_competitor_qa(comp_ids, token)
    if qa_raw:
        total_q = sum(len(c["questions"]) for c in qa_raw)
        total_r = sum(len(c["reviews"])   for c in qa_raw)
        _c.print(f"[green]{total_q} preguntas + {total_r} reseñas[/green]")
        qa_insights = analyze_qa_insights(qa_raw, title, console=_c)
    else:
        _c.print("[dim]sin datos disponibles[/dim]")
        qa_insights = ""

    # ── M3.6: Preguntas reales del propio item ────────────────────────────────
    _c.print("  [dim]M3.6 — Preguntas de compradores del propio item...[/dim]", end=" ")
    own_questions = []
    try:
        for _status in ("ANSWERED", "UNANSWERED"):
            _qr = requests.get(
                f"{ML_API}/questions/search",
                params={"item": item_id, "status": _status, "limit": 30},
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            if _qr.ok:
                for _q in _qr.json().get("questions", []):
                    _txt = (_q.get("text") or "").strip()
                    if _txt and _txt not in own_questions:
                        own_questions.append(_txt)
        time.sleep(0.1)
    except Exception:
        pass
    _c.print(f"[green]{len(own_questions)} preguntas[/green]" if own_questions else "[dim]sin preguntas aún[/dim]")

    # ── Categoría y atributos ─────────────────────────────────────────────────
    _c.print("  [dim]Obteniendo categoría y atributos...[/dim]", end=" ")
    category_name  = _get_category_name(category_id)
    category_attrs = _get_category_attributes(category_id, token)
    _c.print(f"[green]{category_name}[/green]")

    # ── M1 scoring completo ───────────────────────────────────────────────────
    keyword_analysis = score_and_classify_keywords(autosuggest_raw, title, position_data, competitors, as_position_map)
    keyword_clusters = _cluster_keywords(autosuggest_raw, as_position_map)

    # ── Score ML interno ──────────────────────────────────────────────────────
    ml_score = calculate_ml_score(item_data, category_attrs, keyword_analysis)
    _c.print(f"  [dim]Score ML (índice interno): [/dim][{'green' if ml_score['total']>=70 else 'yellow' if ml_score['total']>=45 else 'red'}]{ml_score['total']}/100[/]")

    # ── Score oficial de ML ───────────────────────────────────────────────────
    _c.print("  [dim]Consultando score oficial ML...[/dim]", end=" ")
    ml_quality_oficial = _get_ml_quality_score(item_id, token)
    if ml_quality_oficial.get("score"):
        _lv = ml_quality_oficial["level"] or ""
        _c.print(f"[green]{ml_quality_oficial['score']}/100{' — ' + _lv if _lv else ''} (oficial ML)[/green]")
    else:
        _c.print("[dim]no disponible[/dim]")

    # ── M4: Root Cause Engine ─────────────────────────────────────────────────
    _c.print("  [dim]M4 — Root cause analysis...[/dim]", end=" ")
    root_causes = analyze_root_causes(
        item_data, description, keyword_analysis, position_data,
        competitors, category_attrs, comp_patterns,
    )
    _c.print(f"[green]{len(root_causes)} causas identificadas[/green]")

    # ── M5: Difficulty Score ──────────────────────────────────────────────────
    difficulty = calculate_difficulty(competitors, position_data, autosuggest_raw)
    _c.print(f"  [dim]M5 — Difficulty score: [/dim][yellow]{difficulty['nivel'].upper()}[/yellow]")

    # ── M6: Claude — Análisis (Haiku — validación estructurada) ──────────────
    _c.print("\n  [cyan]M6 — LLAMADA 1/2 → Análisis y validación (Haiku)...[/cyan]")
    # Truncar descripciones de competidores antes de armar el prompt
    comps_trimmed = []
    for c in competitors:
        ct = dict(c)
        if ct.get('description') and len(ct['description']) > 500:
            ct['description'] = ct['description'][:500] + '…'
        comps_trimmed.append(ct)
    prompt_analysis = _build_analysis_prompt(
        item_data, description, keyword_analysis, position_data,
        comps_trimmed, comp_patterns, root_causes, difficulty,
        ml_score, category_attrs, category_name,
        ml_quality_oficial=ml_quality_oficial,
        title_violations=title_violations,
    )
    analysis_text = _call_claude(prompt_analysis, max_tokens=2000, console=_c, fast=True)

    # ── M6: Claude — Síntesis (Opus — escritura creativa final) ──────────────
    _c.print("\n  [cyan]M6 — LLAMADA 2/2 → Optimización final (Opus)...[/cyan]")
    my_photos = len(item_data.get("pictures", []))
    my_price  = float(item_data.get("price", 0))
    prompt_synthesis = _build_synthesis_prompt(
        item_data, description, keyword_analysis, category_attrs,
        category_name, root_causes, comp_patterns, analysis_text,
        gap_keywords=gap_keywords or [],
        catalog_linked=catalog_linked,
        catalog_available=catalog_available,
        my_photos=my_photos,
        my_price=my_price,
        qa_insights=qa_insights,
        own_questions=own_questions,
        keyword_clusters=keyword_clusters,
    )
    synthesis_text = _call_claude(prompt_synthesis, max_tokens=3500, console=_c, fast=False)
    seo_result = _parse_synthesis(synthesis_text)

    # ── M7: Confidence Layer ──────────────────────────────────────────────────
    confidence = build_confidence_layer(keyword_analysis, root_causes, difficulty)

    # ── Score proyectado (estimación de mejora post-optimización) ─────────────
    score_actual = ml_score["total"]
    score_delta  = 0
    if seo_result.get("titles"):          score_delta += 15   # título mejorado
    if seo_result.get("ficha_perfecta"):  score_delta += 20   # atributos completos
    if seo_result.get("descripcion_nueva"): score_delta += 5  # descripción mejorada
    if catalog_available and not catalog_linked: score_delta += 5  # oportunidad catálogo
    score_proyectado = min(score_actual + score_delta, 100)

    # ── Estructura de output (spec) ───────────────────────────────────────────
    return {
        "summary": {
            "item_id":         item_id,
            "title":           title,
            "category":        category_name,
            "ml_score":        score_actual,
            "score_proyectado": score_proyectado,
            "difficulty":      difficulty["nivel"],
            "root_causes_n":   len(root_causes),
            "keywords_n":      len(keyword_analysis),
        },
        "root_causes":         root_causes,
        "keyword_analysis":    keyword_analysis,
        "position_tracking":   position_data,
        "competitor_insights": comp_patterns,
        "difficulty_score":    difficulty,
        "optimization_plan": {
            "titles":                    seo_result.get("titles", []),
            "titulo_recomendado":        seo_result.get("titulo_recomendado", ""),
            "titulo_recomendado_n":      seo_result.get("titulo_recomendado_n", 0),
            "titulo_recomendado_motivo": seo_result.get("titulo_recomendado_motivo", ""),
            "keywords":                  seo_result.get("keywords_section", ""),
            "attributes":                seo_result.get("ficha_perfecta", ""),
            "description":               seo_result.get("descripcion_nueva", ""),
            "correcciones_titulo":       seo_result.get("correcciones_titulo", ""),
            "precio_recomendado":        seo_result.get("precio_recomendado", ""),
            "fotos_recomendadas":        seo_result.get("fotos_recomendadas", ""),
            "alerta_catalogo":           seo_result.get("alerta_catalogo", ""),
            "extra_recommendations":     seo_result.get("recomendaciones", ""),
        },
        "confidence_analysis": confidence,
        # Internos para el display layer
        "_item_data":         item_data,
        "_description":       description,
        "_ml_score":          ml_score,
        "_analysis_text":     analysis_text,
        "_seo_result":        seo_result,
        "_autosuggest_raw":   autosuggest_raw,
        "_catalog_linked":       catalog_linked,
        "_catalog_available":    catalog_available,
        "_qa_insights":          qa_insights,
        "_ml_quality_oficial":   ml_quality_oficial,
        "_title_violations":     title_violations,
    }


def run_new_listing(product_idea: str, client: MLClient, expected_price: float = 0,
                    console=None, competitor_products=None, gap_keywords=None) -> dict:
    """
    Flujo completo para un producto nuevo (sin item_id).
    Acepta competidores pre-seleccionados igual que run_full_optimization().
    """
    from rich.console import Console as RC
    _c = console or RC()

    token = client.account.access_token
    client._ensure_token()

    # M1: autosuggest del producto
    _c.print("  [dim]M1 — Keyword discovery...[/dim]", end=" ")
    autosuggest_raw, as_position_map = get_autosuggest_keywords(product_idea)
    _c.print(f"[green]{len(autosuggest_raw)} keywords[/green]")

    # Detectar categoría
    _c.print("  [dim]Detectando categoría...[/dim]", end=" ")
    category_id, category_name = "", ""
    try:
        r = requests.get(
            f"{ML_API}/sites/{ML_SITE}/domain_discovery/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": product_idea, "limit": 3},
            timeout=8,
        )
        if r.ok and r.json():
            d = r.json()[0]
            category_id   = d.get("category_id", "")
            category_name = d.get("domain_name") or d.get("category_name", "")
    except Exception:
        pass
    _c.print(f"[green]{category_name or 'no detectada'}[/green]")

    # M3: competidores
    if competitor_products:
        _c.print(f"  [dim]M3 — Usando {len(competitor_products)} competidores pre-seleccionados...[/dim]", end=" ")
        competitors = competitor_products
    else:
        main_kw = autosuggest_raw[0] if autosuggest_raw else product_idea
        _c.print(f"  [dim]M3 — Competitor intel para \"{main_kw[:35]}\"...[/dim]", end=" ")
        competitors = fetch_competitors_full(main_kw, token, limit=8)
    comp_patterns = analyze_competitor_patterns(competitors, product_idea)
    _c.print(f"[green]{len(competitors)} competidores analizados[/green]")

    # M1.5: Expandir keywords con vocabulario de competidores (títulos + descripciones)
    _c.print("  [dim]M1.5 — Expandiendo keywords (títulos + descripciones de competidores)...[/dim]", end=" ")
    _comp_titles_raw = [c.get("title", "") for c in competitors if c.get("title")]
    _comp_desc_raw   = [c.get("description", "")[:300] for c in competitors if c.get("description")]
    _source_texts    = _comp_titles_raw + _comp_desc_raw
    _extra_kws = _competitor_seeded_autosuggest_seo(_source_texts, product_idea)
    _seen_raw  = set(autosuggest_raw)
    _added     = 0
    for _kw in _extra_kws:
        if _kw not in _seen_raw:
            _seen_raw.add(_kw)
            autosuggest_raw.append(_kw)
            as_position_map[_kw] = {'best_pos': 5, 'query_count': 0}
            _added += 1
    _c.print(f"[green]+{_added} keywords nuevas ({len(autosuggest_raw)} total)[/green]")

    # M3.5: Q&A y reseñas de competidores
    _c.print("  [dim]M3.5 — Minería de Q&A y reseñas de competidores...[/dim]", end=" ")
    comp_ids = [c["id"] for c in competitors if c.get("id")]
    qa_raw   = fetch_competitor_qa(comp_ids, token)
    if qa_raw:
        total_q = sum(len(c["questions"]) for c in qa_raw)
        total_r = sum(len(c["reviews"])   for c in qa_raw)
        _c.print(f"[green]{total_q} preguntas + {total_r} reseñas[/green]")
        qa_insights = analyze_qa_insights(qa_raw, product_idea, console=_c)
    else:
        _c.print("[dim]sin datos disponibles[/dim]")
        qa_insights = ""

    # Atributos
    _c.print("  [dim]Obteniendo atributos...[/dim]", end=" ")
    category_attrs = _get_category_attributes(category_id, token) if category_id else {"required": [], "optional": []}
    _c.print(f"[green]{len(category_attrs.get('required', []))} requeridos[/green]")

    # Item mock para nueva publicación
    comp_prices  = [c.get("price", 0) for c in competitors if c.get("price")]
    avg_price    = sum(comp_prices) / len(comp_prices) if comp_prices else expected_price
    item_mock = {
        "title": product_idea, "price": avg_price, "sold_quantity": 0,
        "listing_type_id": "gold_special", "shipping": {}, "pictures": [], "attributes": [],
    }

    # M2: no hay item_id para trackear posición
    position_data    = []
    keyword_analysis = score_and_classify_keywords(autosuggest_raw, product_idea, [], competitors, as_position_map)
    keyword_clusters = _cluster_keywords(autosuggest_raw, as_position_map)
    ml_score         = {"total": 0, "kw_in_title": [], "kw_missing": autosuggest_raw[:8],
                        "missing_required": [a["name"] for a in category_attrs.get("required", [])],
                        "missing_optional": [], "attrs_required_pct": 0, "attrs_optional_pct": 0,
                        "photos": 0, "free_shipping": False, "catalog_match": False, "breakdown": {}}
    root_causes      = []
    difficulty       = calculate_difficulty(competitors, [], autosuggest_raw)

    _c.print("\n  [cyan]M6 — LLAMADA 1/2 → Análisis de mercado (Haiku)...[/cyan]")
    comps_trimmed = []
    for c in competitors:
        ct = dict(c)
        if ct.get("description") and len(ct["description"]) > 500:
            ct["description"] = ct["description"][:500] + "…"
        comps_trimmed.append(ct)
    prompt_analysis = _build_analysis_prompt(
        item_mock, "", keyword_analysis, [], comps_trimmed,
        comp_patterns, root_causes, difficulty, ml_score, category_attrs, category_name,
        is_new_listing=True,
    )
    analysis_text = _call_claude(prompt_analysis, max_tokens=2000, console=_c, fast=True)

    _c.print("\n  [cyan]M6 — LLAMADA 2/2 → Generando publicación perfecta (Opus)...[/cyan]")
    prompt_synthesis = _build_synthesis_prompt(
        item_mock, "", keyword_analysis, category_attrs,
        category_name, root_causes, comp_patterns, analysis_text,
        gap_keywords=gap_keywords or [],
        catalog_linked=False,
        catalog_available=False,
        my_photos=0,
        my_price=avg_price,
        qa_insights=qa_insights,
        keyword_clusters=keyword_clusters,
    )
    synthesis_text = _call_claude(prompt_synthesis, max_tokens=3500, console=_c, fast=False)
    seo_result     = _parse_synthesis(synthesis_text)
    confidence     = build_confidence_layer(keyword_analysis, root_causes, difficulty)

    return {
        "summary": {
            "product_idea": product_idea,
            "category":     category_name,
            "difficulty":   difficulty["nivel"],
            "keywords_n":   len(keyword_analysis),
        },
        "root_causes":         root_causes,
        "keyword_analysis":    keyword_analysis,
        "position_tracking":   [],
        "competitor_insights": comp_patterns,
        "difficulty_score":    difficulty,
        "optimization_plan": {
            "titles":                    seo_result.get("titles", []),
            "titulo_recomendado":        seo_result.get("titulo_recomendado", ""),
            "titulo_recomendado_n":      seo_result.get("titulo_recomendado_n", 0),
            "titulo_recomendado_motivo": seo_result.get("titulo_recomendado_motivo", ""),
            "keywords":                  seo_result.get("keywords_section", ""),
            "attributes":                seo_result.get("ficha_perfecta", ""),
            "description":               seo_result.get("descripcion_nueva", ""),
            "correcciones_titulo":       seo_result.get("correcciones_titulo", ""),
            "precio_recomendado":        seo_result.get("precio_recomendado", ""),
            "fotos_recomendadas":        seo_result.get("fotos_recomendadas", ""),
            "alerta_catalogo":           seo_result.get("alerta_catalogo", ""),
            "extra_recommendations":     seo_result.get("recomendaciones", ""),
        },
        "confidence_analysis": confidence,
        "_item_data":       item_mock,
        "_description":     "",
        "_ml_score":        ml_score,
        "_analysis_text":   analysis_text,
        "_seo_result":      seo_result,
        "_autosuggest_raw": autosuggest_raw,
        "_qa_insights":     qa_insights,
    }
