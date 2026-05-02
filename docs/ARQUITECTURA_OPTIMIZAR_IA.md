# Arquitectura — Optimizar IA (`modules/seo_optimizer.py`)

**Tamaño:** 2.893 líneas · **Hash MD5 al momento de escribir:** `1e7272662f0761fba99bdb07f23fa1cc`

Este documento describe la arquitectura interna del motor SEO sin describir
cómo modificarlo. Se actualiza solo cuando se agregan funciones públicas o
cambian constantes. Para tocar el motor, leer primero `docs/GUIA_REGRESION.md`.

> **Regla #1 inmutable:** la lógica interna de este módulo no se modifica
> sin autorización explícita. Todo cambio debe pasar por un wrapper o
> extensión externa, nunca por edición del core.

---

## 1. Vista general

### Propósito

Motor de optimización SEO para publicaciones de MercadoLibre Argentina (`MLA`).
Toma como input un `item_id` (publicación existente) o un `product_idea`
(producto nuevo) y devuelve un plan completo de optimización basado en datos
reales del marketplace: keywords del autosuggest, posiciones actuales,
competidores top, Q&A de compradores y análisis de causa raíz.

### Arquitectura — los 7 sub-módulos (más M0 y M1.5)

| Sub-módulo | Nombre | Qué hace |
|---|---|---|
| **M0** | Competitor Phrase Extraction | Extrae bigramas y trigramas de los títulos de competidores filtrando stopwords y promo words. Alimenta a M1.5 con frases-semilla del lenguaje del comprador. |
| **M1** | Keyword Discovery | Llama al autosuggest público de ML con 4 queries tokenizadas del título. Construye un `position_map` con `best_pos` y `query_count` por keyword (validación cruzada). |
| **M1.5** | Competitor-Seeded Autosuggest | Usa las frases de M0 como semillas para autosuggest adicional. Valida relevancia semántica vs el universo del producto. Descubre keywords que no están en tu vocabulario. |
| **M2** | Position Tracker | Para las 6 keywords propias + 3 mejores gap-keywords, busca en `/sites/MLA/search` y detecta tu posición real en top 48. Mide saturación (total de resultados). |
| **M3** | Competitor Intelligence | Trae los top 8 competidores con datos completos (precio, atributos, descripción, fotos, listing type). Calcula patrones agregados (precio promedio, ratio Premium/Full, etc.). |
| **M3.5** | Q&A Mining | Para los top 5 competidores, baja preguntas respondidas + reseñas. Haiku extrae lenguaje real del comprador, dudas frecuentes y debilidades a explotar. |
| **M3.6** | Own Questions | Trae preguntas reales de compradores del propio item (no del competidor). Solo en flujo de item existente. |
| **M4** | Root Cause Engine | Genera hasta 9 causas de bajo ranking, ordenadas por impacto (alto/medio/bajo). Cada una etiquetada como `dato_real` o `inferencia`. |
| **M5** | Difficulty Score | Calcula nivel de dificultad de posicionamiento (`muy_alta` / `alta` / `media` / `baja`) combinando saturación, ratio Premium/Full y ventas promedio de los competidores. |
| **M6** | Optimization Engine (Claude) | Dos llamadas: Haiku para análisis estructurado, Opus para síntesis creativa (3 títulos + ficha + descripción 9-bloques). |
| **M7** | Confidence Layer | Adjunta nivel de confianza (alta/media/baja) y tipo de evidencia a cada hallazgo principal del output. |

### Flujo de ejecución típico — `run_full_optimization()`

19 pasos secuenciales, en orden:

```
1.  client._ensure_token() + leer item_data
2.  Detección de catálogo (linked? available?)
3.  audit_title() del título actual
4.  M1   — get_autosuggest_keywords(title)
5.  M3   — fetch_competitors_full(main_kw)
6.  M3   — analyze_competitor_patterns(competitors, title)
7.  M1.5 — _competitor_seeded_autosuggest_seo(...)  ← agrega gap_kws
8.  M2   — track_positions(item_id, top_kws + gap_kws)
9.  M3.5 — fetch_competitor_qa() + analyze_qa_insights() (Haiku)
10. M3.6 — fetch propias preguntas del item
11. _get_category_info() + _get_category_attributes()
12. M1   — score_and_classify_keywords() (scoring final con position_data)
13.       _cluster_keywords() (clustering por Jaccard)
14.       calculate_ml_score() (índice interno)
15.       _get_ml_quality_score() (oficial de ML)
16. M4   — analyze_root_causes()
17. M5   — calculate_difficulty()
18. M6   — Llamada 1: análisis estructurado (Haiku)
19. M6   — Llamada 2: síntesis final (Opus) → _parse_synthesis()
20. M7   — build_confidence_layer()
21. Cálculo de score_proyectado
22. Return dict con estructura del spec
```

### Reglas estrictas del sistema

Documentadas en el header del archivo (líneas 14-20):

- ✅ **Solo datos observables reales** (API ML, autosuggest, competidores).
- ✅ **Inferencias claramente marcadas** como tales (campo `tipo: "inferencia"`).
- ✗ **NO inventar volumen de búsqueda** — solo se usa la posición en autosuggest como proxy.
- ✗ **NO asumir pesos del algoritmo interno de ML** — no son públicos.
- ✗ **NO generar atributos falsos** — si un dato no se conoce, escribir `[SUGERIR: descripción del dato]`.

---

## 2. Constantes críticas (NO TOCAR)

### `_ML_SCORE_WEIGHTS` (líneas 79-86)

- **Tipo:** `dict[str, int]`
- **Valor:**
  ```python
  {
      "attrs_required": 35,
      "attrs_optional": 20,
      "title_keywords": 20,
      "photos":         10,
      "free_shipping":  10,
      "catalog_match":   5,
  }
  ```
- **Para qué sirve:** pondera el cálculo del score ML interno (índice diagnóstico, no oficial). Es la base de `calculate_ml_score()`.
- **Suma obligatoria:** 100 — validado por el test de regresión rápido.
- **Si se modifica:** todo el sistema de scoring queda inconsistente. Las pantallas que comparan `score_actual` vs `score_proyectado` muestran números sin sentido. El bonus de `+15/+20/+5/+5` calculado en `run_full_optimization` (líneas 2670-2675) deja de tener relación con el peso real.

### `_CATEGORY_CONTEXT` (líneas 92-152)

- **Tipo:** `list[dict]` con 10 nichos.
- **Estructura por nicho:** `{kws: list[str], faqs: str, objections: str}`
- **Los 10 nichos:**

  | # | Nicho | Kws ancla |
  |---|---|---|
  | 0 | Moda / Indumentaria | moda, ropa, calzado, cartera, billetera, reloj, joyeria, joya, vestido, pantalon, camisa, remera, zapatilla, accesorio moda |
  | 1 | Electrónica | celular, notebook, computadora, tv, audio, gaming, electronica, tecnologia, tablet, auricular, smartwatch, monitor |
  | 2 | Hogar | hogar, mueble, deco, silla, mesa, sofa, colchon, escritorio, estante, armario, cocina, baño |
  | 3 | Deporte | deporte, fitness, running, ciclismo, crossfit, yoga, gimnasio, bicicleta, pelota, natacion |
  | 4 | Belleza | belleza, cosmetico, maquillaje, crema, shampoo, perfume, cuidado personal, serum, aceite capilar, tratamiento |
  | 5 | Bebé / Infantil | bebe, niño, juguete, infantil, pañal, materna, lactancia |
  | 6 | Auto / Moto | auto, moto, vehiculo, repuesto, autoparte, freno, filtro, accesorio vehiculo, neumatico, llanta |
  | 7 | Herramientas | herramienta, construccion, taladro, sierra, soldadora, compresor, amoladora, lijadora |
  | 8 | Salud | salud, medico, ortopedico, farmacia, suplemento, vitamina, rehabilitacion, fisioterapia |
  | 9 | Mascotas | mascota, perro, gato, veterinaria, alimento animal, collar, correa, acuario, ave |

- **Para qué sirve:** fallback cuando M3.5 no devuelve Q&A real. `_get_category_context()` matchea el `category_name` contra los `kws` de cada nicho y retorna FAQs + objeciones para que Claude las neutralice en la descripción.
- **Si se modifica:** se pierde la red de seguridad para productos en categorías sin Q&A real. Caer un nicho hace que toda esa familia de productos pierda contexto de comprador.

### `_STOPWORDS` (líneas 43-47)

- **Tipo:** `set[str]` con 23 elementos.
- **Valor:** preposiciones, artículos, conjunciones y unidades cortas (`de, para, con, y, el, la, x, cm, ml, gr, kg, mm, lt`, etc.).
- **Para qué sirve:** filtrar tokens irrelevantes en `_tokenize()` y `_extract_competitor_phrases_seo()`.
- **Si se modifica:** agregar palabras de contenido (ej. "negro", "grande") las haría desaparecer del análisis. Quitar artículos rompe el filtro semántico de Jaccard.

### `_AS_HEADERS` (líneas 49-55)

- **Tipo:** `dict[str, str]` con headers HTTP.
- **Valor:**
  ```python
  {
      "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...",
      "Referer":         "https://www.mercadolibre.com.ar/",
      "Accept":          "application/json, text/javascript, */*; q=0.01",
      "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
      "Origin":          "https://www.mercadolibre.com.ar",
  }
  ```
- **Para qué sirve:** simula un navegador real al consultar el endpoint público de autosuggest (`http2.mlstatic.com/resources/sites/MLA/autosuggest`). Sin estos headers el endpoint puede devolver 403.
- **Si se modifica:** el User-Agent obsoleto puede ser bloqueado. Quitar Referer/Origin causa fallo silencioso → todo M1 y M1.5 vuelve listas vacías y la optimización degrada a fallback de tokenización.

### `_TRANSACTIONAL_SIGNALS` / `_COMPARATIVE_SIGNALS` / `_INFORMATIONAL_SIGNALS` (líneas 57-68)

- **Tipo:** `set[str]` cada uno.
- **Valores actuales:**
  - `_TRANSACTIONAL_SIGNALS` (12): `comprar, precio, barato, economico, oferta, descuento, original, nuevo, envio gratis, cuotas, mercadopago, venta`
  - `_COMPARATIVE_SIGNALS` (8): `mejor, vs, versus, cual, comparar, diferencia, alternativa, recomendado, opinion`
  - `_INFORMATIONAL_SIGNALS` (9): `que es, como, para que, sirve, funciona, significa, beneficios, usos, propiedades`
- **Para qué sirve:** `_classify_intent()` clasifica cada keyword como `transaccional`, `comparativa`, `informativa`, `atributo` o `ambigua`. **Las informacionales NUNCA van al título** — esa exclusión depende de esta clasificación.
- **Si se modifica:** clasificar mal una keyword como informacional la excluye del título cuando podría rankear. Y al revés: si "como" se quita de `_INFORMATIONAL_SIGNALS`, "como usar X" puede entrar al título y desperdiciar caracteres.

### `_ATTRIBUTE_WORDS` (líneas 71-76)

- **Tipo:** `set[str]` con 25 elementos.
- **Valor:** colores, tamaños, géneros, edades (`negro, blanco, grande, chico, talle, mujer, hombre, adulto, niño, bebe`, etc.).
- **Para qué sirve:** marcar keywords como intención `atributo` cuando contienen estas palabras. Cambia cómo Claude las prioriza en el prompt.
- **Si se modifica:** keywords con atributos bien definidos pueden quedar como `ambigua` y bajar de prioridad sin razón.

### `_COMPLEX_CATS` / `_SIMPLE_CATS` (líneas 154-164)

- **Tipo:** `set[str]` cada uno.
- **Valores:**
  - `_COMPLEX_CATS` (24): electrónica, celular, notebook, mueble, auto, moto, herramienta, construccion, salud, medico, ortopedico, electrodomestico, etc.
  - `_SIMPLE_CATS` (6): accesorio, bijouterie, papeleria, bazar, libreria, decoracion.
- **Para qué sirve:** input principal de `_classify_product_complexity()`. Determina si la descripción debe tener 5 bloques (SIMPLE), 9 bloques estándar (INTERMEDIO) o 9 bloques extendidos (COMPLEJO), con longitudes 600-1200 / 1000-1800 / 1200-2500 chars respectivamente.
- **Si se modifica:** un producto correctamente complejo clasificado como SIMPLE recibe descripción de 600 chars que no convierte. Al revés, un accesorio simple con descripción de 2500 chars se ve sobrecargado.

### `_PROMO_WORDS_SEO` (líneas 317-322)

- **Tipo:** `set[str]` con 23 elementos.
- **Valor:** palabras promocionales prohibidas en títulos por ML + ruido común (`oferta, descuento, gratis, cuotas, original, garantia, premium, calidad`, etc.).
- **Para qué sirve:** filtra bigramas/trigramas en `_extract_competitor_phrases_seo()`. Si un competidor tiene "oferta original premium", esa frase no se usa como semilla.
- **Si se modifica:** quitar "oferta" hace que entren frases promocionales como semillas y M1.5 termine generando keywords inútiles.

### `_TITLE_PROHIBITED_WORDS`, `_TITLE_INFRINGEMENT_TERMS`, `_TITLE_SYMBOLS`, `_TITLE_STOPWORDS_START` (líneas 1309-1316)

- **Tipo:** `set[str]` cada uno.
- **Para qué sirven:** input del **auditor de título** (`audit_title()`). Detectan las 8 violaciones que penaliza ML:
  - `_TITLE_PROHIBITED_WORDS`: "envío gratis", "cuotas", "nuevo", "usado", "oferta", "promo", "oficial", "garantía", etc.
  - `_TITLE_INFRINGEMENT_TERMS`: "símil", "tipo", "igual a", "estilo".
  - `_TITLE_SYMBOLS`: `@#*-!_+/|;:.,()[]{}=<>%$&"'\^~``.
  - `_TITLE_STOPWORDS_START`: stopwords que no deben empezar un título.
- **Si se modifica:** el auditor deja pasar violaciones que ML penaliza, o reporta falsos positivos.

### Endpoints y site (líneas 39-41)

```python
ML_SITE   = "MLA"
ML_API    = "https://api.mercadolibre.com"
ML_AS_URL = "https://http2.mlstatic.com/resources/sites/MLA/autosuggest"
```

- **Si se modifica:** `MLA` solo aplica a Argentina. Cambiarlo sin migrar todos los matchers de categoría rompe el sistema. `ML_AS_URL` es el endpoint público de autosuggest — si ML lo cambia, hay que actualizarlo (rara vez ocurre).

---

## 3. Funciones públicas

### M0 — Competitor Phrase Extraction

#### `_extract_competitor_phrases_seo(competitor_titles: list, seed_title: str) → list`
- Extrae bigramas y trigramas de títulos de competidores. Devuelve top 10.
- Llama: `_norm`, `_KW_STOP_SEO`.
- Sin side effects.

#### `_competitor_seeded_autosuggest_seo(competitor_titles: list, seed_title: str) → list`
- Toma las frases de la función anterior y las pasa por autosuggest. Filtra por relevancia semántica con el universo del producto.
- Llama: `_extract_competitor_phrases_seo`, `_ml_autosuggest`, `_norm`.
- **Side effect:** llamadas HTTP al autosuggest (`time.sleep(0.1-0.15)` entre cada una).

### M1 — Keyword Discovery

#### `_ml_autosuggest(query: str, limit: int = 10) → list[str]`
- Consulta `https://http2.mlstatic.com/resources/sites/MLA/autosuggest` con retry+backoff (3 intentos, espera 1s y 2s en 429).
- Sin token, sin auth.

#### `get_autosuggest_keywords(title: str) → tuple[list, dict]`
- Genera 4 queries tokenizadas del título y consulta autosuggest para cada una.
- Devuelve `(keywords_list, position_map)` con `best_pos` y `query_count` por kw.
- Llama: `_ml_autosuggest`.

#### `score_and_classify_keywords(autosuggest_raw, title, position_data, competitors, position_map=None) → list[dict]`
- Para cada keyword: calcula `priority_score` (40% AS pos + 25% comp presence + 15% en título + 10% gap pos + 10% semántica). Clasifica intención y compatibilidad.
- Llama: `_calculate_priority_score`, `_classify_intent`, `_classify_compatibility`, `_jaccard`, `_tokenize`.

### M2 — Position Tracker

#### `track_positions(item_id: str, keywords: list, token: str) → list[dict]`
- Para máximo 8 keywords, busca en `/sites/MLA/search` (limit 48) y detecta posición.
- Calcula gap vs top 3 y nivel de saturación (`alta / media / baja` por total de resultados).
- **Side effect:** ~8 llamadas a la API privada de ML, `time.sleep(0.15)` entre cada una.

### M3 — Competitor Intelligence

#### `fetch_competitors_full(keyword: str, token: str, exclude_id: str = "", limit: int = 8) → list[dict]`
- Top 8 competidores con: título, precio, vendedor, ventas, listing type, shipping, fotos, atributos, descripción.
- **Side effect:** ~17 llamadas (1 search + 8 items + 8 descriptions).

#### `fetch_competitor_qa(competitor_ids: list, token: str, max_q: int = 15, max_r: int = 10) → list[dict]`
- Para los primeros 5 IDs: preguntas respondidas + reseñas.
- **Side effect:** ~10 llamadas (5 questions + 5 reviews).

#### `analyze_qa_insights(qa_data: list, product_title: str, console=None) → str`
- Llama a Claude Haiku con max_tokens=800. Devuelve análisis estructurado en 4 secciones (debilidades, dudas, valoraciones, lenguaje).
- **Side effect:** 1 llamada a Anthropic.

#### `analyze_competitor_patterns(competitors: list, title: str) → dict`
- Calcula keywords más frecuentes en títulos, kw_gaps, atributos más completados, ratio Premium/Full, precio promedio, fotos promedio.
- Sin side effects, sin red.

### M4 — Root Cause Engine

#### `analyze_root_causes(item_data, description, keyword_analysis, position_data, competitors, category_attrs, comp_patterns) → list[dict]`
- Genera hasta 9 causas: cobertura SEO, posicionamiento bajo, atributos faltantes, precio fuera de mercado, pocas fotos, shipping inferior, baja autoridad, listing type inferior, saturación.
- Cada causa: `{causa, detalle, impacto, tipo}`.
- Sin side effects.

### M5 — Difficulty Score

#### `calculate_difficulty(competitors: list, position_data: list, keywords: list) → dict`
- Combina premium_ratio, full_ratio, avg_sales y saturación promedio.
- Devuelve `{nivel, factores, detalle}` con nivel `muy_alta / alta / media / baja`.
- Sin side effects.

### Score ML interno

#### `calculate_ml_score(item_data, category_attrs, top_keywords) → dict`
- Aplica `_ML_SCORE_WEIGHTS` para calcular un score 0-100. Devuelve breakdown por componente y listas `missing_required` / `missing_optional` / `kw_in_title` / `kw_missing`.
- Sin side effects.

### Auditor de título

#### `audit_title(title: str) → list[dict]`
- 8 reglas: longitud ≤60, símbolos prohibidos, mayúsculas sostenidas, palabras prohibidas, infracción de marca, repetidas, empieza con stopword, espacios múltiples.
- Devuelve lista de violaciones (puede ser vacía). Cada una: `{nivel, regla, detalle, sugerencia}`.
- Sin side effects, sin red.

### M6 — Optimization Engine

#### `_build_analysis_prompt(...)` — interno, construye el prompt para Haiku.
#### `_build_synthesis_prompt(...)` — interno, construye el prompt para Opus (es el prompt grande de ~250 líneas que contiene las reglas de descripción 9-bloques, política de keywords, distribución, etc.).
#### `_build_faq_addendum(own_questions, qa_insights) → str` — interno, agrega bloque FAQ obligatorio si hay datos.
#### `_call_claude(prompt, max_tokens=3500, console=None, fast=False) → str`
- `fast=True` → Haiku 4.5 (`claude-haiku-4-5-20251001`).
- `fast=False` → Opus 4.6 (`claude-opus-4-6`).
- **Side effect:** llamada streaming a Anthropic.

#### `_parse_synthesis(text: str) → dict`
- Parsea las secciones del output de Opus (`## TÍTULO ALTERNATIVO 1/2/3`, `## TÍTULO RECOMENDADO`, `## FICHA TÉCNICA PERFECTA`, `## DESCRIPCIÓN SUPERADORA`, etc.).
- Tolerante a variaciones de formato (markdown bold, recomendaciones en prosa).
- Trunca títulos a 60 chars.

### M7 — Confidence Layer

#### `build_confidence_layer(keyword_analysis, root_causes, difficulty) → list[dict]`
- Adjunta `confianza` (alta/media/baja) y `tipo` (dato_real/inferencia) a las top 5 keywords + top 4 root causes + 1 entrada de dificultad.

### Orquestadores

#### `run_full_optimization(item_id, client, console=None, competitor_products=None, gap_keywords=None) → dict`
- Pipeline completo para un item existente. 19 pasos.
- Acepta competidores pre-seleccionados (skip M3 fetch) y gap_keywords explícitas.
- **Side effects:** múltiples llamadas a ML API + 2 a Anthropic + 1 al autosuggest público (~30 calls totales).

#### `run_new_listing(product_idea, client, expected_price=0, ...)`
- Variante para producto nuevo (sin item_id). Skip M2 y M3.6, usa item mock.

### Helpers internos relevantes

| Función | Para qué |
|---|---|
| `_smart_truncate(text, max_chars)` | Trunca preservando secciones `##` completas. |
| `_strip_accents(text)` | Normaliza para comparación sin tildes. |
| `_tokenize(text)` | Split + filtro de stopwords + len > 2. |
| `_jaccard(a, b)` | Similitud de Jaccard sobre tokens. |
| `_cluster_keywords(keywords, position_map)` | Agrupa por Jaccard ≥ 0.40, elige representante por mejor `best_pos`. |
| `_classify_intent(keyword)` | Clasifica intención (transaccional/comparativa/informativa/atributo/ambigua). |
| `_classify_compatibility(keyword, title)` | Compatibilidad semántica (alta/media/baja/peligrosa). |
| `_calculate_priority_score(...)` | Score compuesto 40/25/15/10/10. |
| `_classify_product_complexity(...)` | SIMPLE/INTERMEDIO/COMPLEJO según categoría + atributos + precio. |
| `_get_category_context(category_name)` | Devuelve FAQs/objeciones del nicho match. |
| `_get_category_info(category_id)` | (name, path) en una llamada. |
| `_get_category_attributes(category_id, token)` | Required + optional de la categoría. |
| `_get_item(item_id, token)` | Trae datos completos del item. |
| `_get_description(item_id, token)` | Plain text de la descripción. |
| `_get_ml_quality_score(item_id, token)` | Score oficial de ML (puede no estar disponible). |

---

## 4. Dependencias internas

### Grafo de llamadas (top-down)

```
run_full_optimization
├── _ensure_token / _get_item / _get_description
├── audit_title  (auditor)
├── get_autosuggest_keywords ──→ _ml_autosuggest
├── fetch_competitors_full
├── analyze_competitor_patterns ──→ _tokenize
├── _competitor_seeded_autosuggest_seo (M1.5)
│   ├── _extract_competitor_phrases_seo ──→ _norm
│   └── _ml_autosuggest
├── track_positions
├── fetch_competitor_qa
├── analyze_qa_insights ──→ _call_claude (Haiku)
├── _get_category_info / _get_category_attributes
├── score_and_classify_keywords
│   ├── _calculate_priority_score
│   ├── _classify_intent
│   ├── _classify_compatibility ──→ _jaccard ──→ _tokenize
├── _cluster_keywords ──→ _jaccard
├── calculate_ml_score
├── _get_ml_quality_score
├── analyze_root_causes
├── calculate_difficulty
├── _build_analysis_prompt + _call_claude (Haiku)
├── _build_synthesis_prompt
│   ├── _classify_product_complexity
│   ├── _get_category_context
│   └── _build_faq_addendum
├── _call_claude (Opus)
├── _parse_synthesis ──→ _extract_section
└── build_confidence_layer
```

### Funciones "core" (si se rompen, todo se rompe)

- `_ml_autosuggest` — sin esto, M1 y M1.5 quedan vacíos.
- `_tokenize` y `_jaccard` — base de scoring, clustering y análisis semántico.
- `_call_claude` — sin esto, no hay output final.
- `_parse_synthesis` — si no parsea bien, el frontend recibe campos vacíos.
- `score_and_classify_keywords` — input directo de M6.
- `run_full_optimization` — orquestador.

### Funciones "leaf" (riesgo bajo)

- `audit_title` — autocontenida, fácil de testear, fácil de modificar (agregar reglas).
- `_classify_product_complexity` — heurística separada.
- `build_confidence_layer` — solo agrega metadata al output.
- `_smart_truncate` — utilitario de strings.
- Helpers `_get_category_*` — wrappers thin sobre la API.

---

## 5. Puntos de extensión SEGUROS

### Patrón recomendado: wrappers externos

La forma correcta de extender este motor sin modificarlo es escribir **wrappers** en otros archivos que llamen a `run_full_optimization` y enriquezcan el output:

```python
# En modules/optimizador_publicaciones.py o nuevo archivo:
from modules.seo_optimizer import run_full_optimization

def run_full_optimization_with_extras(item_id, client, **kwargs):
    result = run_full_optimization(item_id, client, **kwargs)
    # Enriquecer aquí — agregar campos al dict, no modificarlos
    result['mi_metrica_custom'] = calcular_algo(result)
    return result
```

### Hooks de pre/post procesamiento (UI / web)

Toda la lógica de **mostrar**, **guardar a disco** y **aplicar a ML** vive
fuera de `seo_optimizer.py` (en `web/app.py` y `modules/optimizador_publicaciones.py`).
Se pueden modificar libremente:

- ✅ Cómo se renderizan los resultados en `optimizaciones.html`.
- ✅ Endpoints `/api/optimizar-pub*` en `web/app.py`.
- ✅ Persistencia en `data/optimizaciones_<Alias>.json`.
- ✅ Integración con el Monitor de Evolución.
- ✅ Lógica de "aplicar en ML" — qué se aplica, cómo, cuándo.

### Constantes que SÍ se pueden expandir (con cuidado)

- `_CATEGORY_CONTEXT` — agregar un nicho 11 (ej. "instrumentos musicales"). No tocar los 10 existentes.
- `_TITLE_PROHIBITED_WORDS` — agregar una palabra nueva si ML empieza a penalizarla. No quitar las existentes.
- `_PROMO_WORDS_SEO` — mismo criterio.

Cualquier expansión de constantes requiere actualizar el test de regresión rápido.

### Anti-patrones (NO HACER)

- ✗ Modificar el prompt de `_build_synthesis_prompt` (las reglas de los 9 bloques, distribución de keywords, longitudes objetivo) sin autorización.
- ✗ Cambiar los pesos de `_calculate_priority_score` (40/25/15/10/10).
- ✗ Bajar el threshold de Jaccard en `_cluster_keywords` (0.40) — agruparía keywords no relacionadas.
- ✗ Quitar el retry+backoff de `_ml_autosuggest`.
- ✗ Agregar un fallback "inventado" cuando autosuggest devuelve vacío que no sea tokenización del título.
- ✗ Cambiar de `claude-opus-4-6` a un modelo más chico para "ahorrar costo" — la calidad del output depende del modelo.
- ✗ Quitar `_call_claude` con fast=True para análisis y dejarlo solo con Opus — duplica costo sin ganancia.

---

## 6. Integración con APIs externas

### Anthropic (Claude)

- **Modelo Haiku:** `claude-haiku-4-5-20251001` — usado para el análisis estructurado intermedio (M3.5 Q&A insights y M6 análisis previo). Max tokens: 800-2000.
- **Modelo Opus:** `claude-opus-4-6` — usado para la síntesis final (M6 generación de títulos + ficha + descripción). Max tokens: 3500.
- **Streaming:** sí, ambas llamadas con `stream=True` para mostrar progreso al usuario.
- **Costo aproximado por corrida completa:** ~$0.50-$1.50 USD (1 Haiku + 1 Opus + Q&A insights con Haiku).
- **Side effect en consola:** si `console` se pasa, el output se imprime en tiempo real.

### MercadoLibre API privada (`api.mercadolibre.com`)

Endpoints usados (todos con `Authorization: Bearer <token>`):

| Endpoint | Usado en |
|---|---|
| `GET /items/{id}` | `_get_item`, `fetch_competitors_full` |
| `GET /items/{id}/description` | `_get_description`, `fetch_competitors_full` |
| `GET /items/{id}/quality_score` | `_get_ml_quality_score` |
| `GET /items/{id}/visits/time_window` | (en `web/app.py`, no aquí) |
| `GET /sites/MLA/search` | `track_positions`, `fetch_competitors_full` |
| `GET /products/search` | detección de catálogo |
| `GET /sites/MLA/domain_discovery/search` | `run_new_listing` |
| `GET /categories/{id}` | `_get_category_info` |
| `GET /categories/{id}/attributes` | `_get_category_attributes` |
| `GET /questions/search?item_id=...` | `fetch_competitor_qa`, M3.6 |
| `GET /reviews/item/{id}` | `fetch_competitor_qa` |

**Rate limit conocido:** se inserta `time.sleep(0.1-0.3)` entre llamadas de batch para evitar throttle. ML retorna 401 al expirar el token — el `MLClient` lo refresca automáticamente y reintenta.

### Autosuggest público de ML (`http2.mlstatic.com`)

- Endpoint: `https://http2.mlstatic.com/resources/sites/MLA/autosuggest`
- **Sin auth** — endpoint público, trackea por IP.
- Headers obligatorios: los de `_AS_HEADERS` (User-Agent, Referer, Accept-Language, Origin).
- Parámetros: `q` (query), `limit`, `lang=es_AR`.
- Response: `{"suggested_queries": [{"q": "..."}, ...]}`.
- **Rate limit:** observado ~35-45 calls antes de 429 en una sesión. El retry con backoff (1s, 2s) lo absorbe en la mayoría de los casos. Una corrida completa hace ~14 llamadas, dos seguidas pueden empezar a recibir 429 hacia el final de la segunda.

---

## 7. Casos borde conocidos

### Items sin catálogo

- Si `catalog_product_id` está vacío, se busca un producto de catálogo equivalente con `/products/search`. Si existe, `catalog_available = True` → se genera bloque `## ALERTA CATÁLOGO`.
- Si el item ya está vinculado, `catalog_linked = True` → bloque ALERTA CATÁLOGO se omite del prompt y del output.
- El test de regresión completo **debe hacer skip silencioso** del análisis de catálogo cuando no aplica (ya implementado en el plan, no fue creado por costo).

### Items con variantes

- ML expone `variations` en `/items/{id}` pero el motor **no las maneja explícitamente**. Toma los datos del item padre.
- Si un item tiene variantes de color, la regla del prompt prohíbe poner color en el título (línea 2121).
- **Limitación:** no se sugiere optimización por variante, solo a nivel publicación.

### Items pausados

- `_get_item` no filtra por status. Si el item está pausado, igual procesa pero el `track_positions` puede no encontrarlo en search (porque los pausados se ocultan).
- El sistema no tiene un short-circuit para items pausados — termina pero los `position_data` quedan todos en `aparece: False`.

### Categorías "complejas" vs "simples"

- `_classify_product_complexity()` puede caer en fallback (`is_fallback=True`) cuando no hay señales suficientes. En ese caso clasifica como `INTERMEDIO` y loggea warning.
- Items de nicho raro (no entran en `_COMPLEX_CATS` ni `_SIMPLE_CATS`) y con pocos atributos siempre caen en fallback INTERMEDIO.

### Manejo de errores y fallbacks

- **Autosuggest vacío:** `get_autosuggest_keywords` cae a tokenización del título (líneas 504-509). El sistema sigue corriendo con keywords inferidas del propio título.
- **`fetch_competitors_full` vuelve []:** `analyze_competitor_patterns` devuelve `{}`. El prompt funciona pero sin contexto competitivo.
- **`fetch_competitor_qa` vuelve []:** `analyze_qa_insights` devuelve `""`. El prompt cae a `_get_category_context` (FAQs estáticas del nicho).
- **`_call_claude` falla:** propaga la excepción. `run_full_optimization` no la captura, llega al endpoint web que la traduce a error 500.
- **`_get_ml_quality_score` falla:** devuelve `{}`. El bloque oficial no aparece en el prompt, no rompe nada.
- **Token expirado:** el `MLClient` refresca automáticamente — no es un caso borde manejado en este módulo.

---

## 8. Checklist antes de modificar algo

### Pre-cambio

1. ☐ ¿La modificación toca alguna de las **constantes críticas** listadas en sección 2?
   - Si sí → STOP. No modificar sin autorización explícita.
2. ☐ ¿La modificación toca el contenido de `_build_synthesis_prompt` o `_build_analysis_prompt`?
   - Si sí → STOP. Los prompts son parte del core inmutable.
3. ☐ ¿La modificación cambia la firma de una función pública listada en sección 3?
   - Si sí → impacta a `web/app.py` y/o `modules/optimizador_publicaciones.py` y/o `modules/lanzador_productos.py`. Verificar todas las llamadas.
4. ☐ ¿Se puede lograr lo mismo con un **wrapper externo** (sección 5)?
   - Si sí → preferir wrapper antes que modificar el core.
5. ☐ Ejecutar el test rápido **antes** de empezar:
   ```bash
   bash tests/run_regresion.sh
   ```
   Confirmar que pasa con el código actual antes de tocar nada.

### Post-cambio

6. ☐ Ejecutar el test rápido de nuevo:
   ```bash
   bash tests/run_regresion.sh
   ```
7. ☐ Si el test reporta **WARNING de hash**, evaluar si fue intencional. Si sí, regenerar el hash:
   ```bash
   rm tests/.optimizer_hash
   bash tests/run_regresion.sh
   ```
8. ☐ Si se agregó/modificó una función pública, **actualizar la sección 3** de este documento.
9. ☐ Si se agregó una constante, **actualizar la sección 2**.
10. ☐ Cambio significativo (afecta el output del motor) → seguir el protocolo de **validación manual** en `docs/GUIA_REGRESION.md` (correr una optimización en producción y verificar que todos los bloques aparecen).
11. ☐ Antes de mergear a `main`, repasar este checklist completo.
