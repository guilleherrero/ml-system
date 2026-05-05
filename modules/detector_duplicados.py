"""
Detector de publicaciones duplicadas / canibalización
======================================================

Identifica clusters de publicaciones similares en el catálogo de un vendedor
y los clasifica en 3 niveles de severidad. Solo expone públicamente los que
requieren acción (puros y subperformantes); los sanos se filtran de la salida
de UI pero quedan disponibles internamente para análisis.

Algoritmo:
  1. Normalizar título — quitar tildes, lowercase, sacar palabras promo,
     tokens >2 caracteres.
  2. Comparar pares con difflib.SequenceMatcher.ratio() >= 0.85.
  3. Filtrar pares marcados manualmente como "ignorados" por el usuario.
  4. Construir clusters por unión de pares (transitividad).
  5. Clasificar severidad con detección de tokens "variante" legítima
     (talles, colores, capacidades) y métricas de venta del cluster.

Sin dependencias nuevas — solo stdlib.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable

# ── Configuración ────────────────────────────────────────────────────────────

THRESHOLD_SIMILARIDAD = 0.85   # ratio mínimo para considerar dos títulos del mismo cluster

# Palabras promocionales/genéricas que se eliminan al normalizar — no aportan
# señal semántica y generan falsos matches.
_PROMO_WORDS = {
    'oferta', 'descuento', 'promo', 'promocion', 'gratis', 'envio',
    'cuotas', 'nuevo', 'usado', 'original', 'garantia', 'mejor', 'barato',
    'economico', 'precio', 'venta', 'pack', 'kit', 'combo', 'super', 'mega',
    'ultra', 'premium', 'calidad', 'oficial',
}

# Tokens "variante" — si dos títulos solo difieren en estos tokens, el
# cluster es de variantes legítimas (talles/colores/medidas) y se clasifica
# como subperformante o sano según métricas, no como duplicado puro.
_TOKENS_TALLE = {
    'xs', 's', 'm', 'l', 'xl', 'xxl', 'xxxl',
    'pequeno', 'chico', 'mediano', 'grande', 'extra',
}
_TOKENS_COLOR = {
    'negro', 'blanco', 'rojo', 'azul', 'verde', 'gris', 'beige', 'rosa',
    'rosado', 'celeste', 'violeta', 'lila', 'marron', 'cafe', 'amarillo',
    'naranja', 'turquesa', 'fucsia', 'dorado', 'plateado', 'palido',
    'bordo', 'mostaza', 'aqua', 'coral',
    # Hotfix v2.2: truncamientos comunes que ML produce por límite de 60 chars
    'neg', 'bla', 'roj', 'azu', 'ver', 'gri', 'bei', 'ros', 'cel', 'vio',
    'mar', 'caf', 'ama', 'nar', 'tur', 'fuc', 'dor', 'pla', 'aqu', 'cor',
    'multicol', 'multic', 'multico', 'animal', 'ani', 'estampa', 'estamp',
    'liso', 'oscuro', 'claro', 'pastel', 'metalico', 'metal',
}
_TOKENS_VARIANTE = _TOKENS_TALLE | _TOKENS_COLOR

_DIGITOS_RE = re.compile(r'^\d+$')


# ── Tipos de salida ──────────────────────────────────────────────────────────

@dataclass
class ItemCluster:
    """Una publicación dentro de un cluster, con sus métricas comparativas."""
    id: str
    titulo: str
    precio: float
    ventas_30d: int
    visitas_30d: int
    conversion_pct: float
    es_ganadora: bool = False
    # Sprint Detector v2 (05/05/2026): campos para distinguir duplicados reales
    # vs combinaciones legítimas según política de ML.
    listing_type: str = ''     # 'gold_special' (Clásica) | 'gold_pro' (Premium)
    free_shipping: bool = False
    es_legitimo: bool = False  # True si en el cluster hay diferenciación válida que cubre a este item


@dataclass
class Recomendacion:
    """Recomendación heurística por item dentro de un cluster.

    accion:
      'mantener'        — la ganadora o un item que vende bien
      'pausar_traf'     — sin ventas pero con visitas (recuperar tráfico)
      'pausar_sin_traf' — sin ventas y sin visitas (limpieza)
      'revisar'         — vende algo pero conversión muy baja
    """
    accion: str
    motivo: str
    pre_seleccionar: bool   # True → aparece marcado por defecto en checkbox de pausa


@dataclass
class Cluster:
    """Un cluster de publicaciones potencialmente duplicadas."""
    cluster_id: str
    severidad: str            # 'puro' | 'subperformante' | 'sano' | 'legitimo' | 'mixto'
    items: list[ItemCluster] = field(default_factory=list)
    titulo_corto: str = ''
    visitas_perdidas_30d: int = 0
    impacto_monetario_estimado: float = 0.0
    recomendaciones: dict = field(default_factory=dict)   # {mla_id: Recomendacion}
    resumen_recomendacion: str = ''
    # Sprint Detector v2: explicación de por qué es legítimo (cuando aplique)
    nota_legitimidad: str = ''


# ── Helpers de normalización ────────────────────────────────────────────────

def _strip_accents(text: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFKD', text or '')
        if unicodedata.category(c) != 'Mn'
    )


def _normalizar_titulo(titulo: str) -> str:
    """Lowercase, sin tildes, sin promo words.

    Hotfix detector v2.1 (05/05/2026): preservar tokens de VARIANTE aunque
    tengan ≤2 chars (talles 's', 'm', 'l', 'xl', etc., y dígitos cortos).
    Antes los filtraba por length>2, lo que hacía invisible el diff de talle
    entre publicaciones y las marcaba como duplicado puro.
    """
    s = _strip_accents((titulo or '').lower())
    tokens = []
    for t in re.findall(r'[a-z0-9]+', s):
        # Caso 1: token significativo (>2 chars) y no es promo word
        if len(t) > 2 and t not in _PROMO_WORDS:
            tokens.append(t)
            continue
        # Caso 2: token corto pero ES variante conocida (talle/color/dígito)
        # → preservar para que el diff entre títulos con distinta variante NO sea vacío
        if t in _TOKENS_VARIANTE or _DIGITOS_RE.match(t):
            tokens.append(t)
    return ' '.join(tokens)


def _tokens_diferencia(titulo_a_norm: str, titulo_b_norm: str) -> set[str]:
    """Tokens que están en uno y no en el otro."""
    sa = set(titulo_a_norm.split())
    sb = set(titulo_b_norm.split())
    return sa.symmetric_difference(sb)


def _diferencias_son_solo_variantes(diff: set[str],
                                    norm_a: str = '', norm_b: str = '') -> bool:
    """True si todos los tokens de diferencia son talles/colores/números.

    Hotfix v2.2 (05/05/2026): si los normalizados se pasan, también detecta
    variantes por ESTRUCTURA del título — caso típico de ML que trunca colores
    a 3-4 chars (gri, neg, aqu, multicol, ani). Si los títulos comparten
    largo prefijo común y solo difieren en últimos tokens cortos → variantes.
    """
    if not diff:
        return False  # idénticos → no es "solo variantes", es duplicado puro

    # Check 1: tokens explícitos en diccionario de variantes
    if all(t in _TOKENS_VARIANTE or _DIGITOS_RE.match(t) for t in diff):
        return True

    # Check 2: heurística por estructura — si los normalizados están dados
    if norm_a and norm_b:
        toks_a = norm_a.split()
        toks_b = norm_b.split()
        # Encontrar el prefijo común
        prefix_len = 0
        for ta, tb in zip(toks_a, toks_b):
            if ta == tb:
                prefix_len += 1
            else:
                break
        # Si comparten 4+ tokens al inicio (= claramente mismo producto base)
        # y los tokens distintos al final son cortos (≤5 chars cada uno)
        # → asumir variantes (probable color/talle truncado por ML)
        if prefix_len >= 4:
            sufijo_a = toks_a[prefix_len:]
            sufijo_b = toks_b[prefix_len:]
            todos_cortos = all(len(t) <= 5 for t in sufijo_a + sufijo_b)
            pocos_tokens = len(sufijo_a) <= 3 and len(sufijo_b) <= 3
            if todos_cortos and pocos_tokens:
                return True

    return False


# ── Pares ignorados ──────────────────────────────────────────────────────────

def _ruta_ignorados(alias: str, data_dir: str) -> str:
    safe_alias = alias.replace(' ', '_').replace('/', '-')
    return os.path.join(data_dir, f'duplicados_ignorados_{safe_alias}.json')


def cargar_pares_ignorados(alias: str, data_dir: str) -> list[dict]:
    """Lee la lista de pares marcados como ignorados por el usuario.

    Estructura del JSON:
      [{"mla_a": "MLA...", "mla_b": "MLA...", "marcado_en": "2026-05-02",
        "razon_opcional": "..."}]
    """
    ruta = _ruta_ignorados(alias, data_dir)
    if not os.path.exists(ruta):
        return []
    try:
        with open(ruta, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def marcar_par_ignorado(alias: str, data_dir: str, mla_a: str, mla_b: str,
                        razon: str = '') -> bool:
    """Agrega un par a la lista de ignorados. Idempotente — si ya existe, no duplica.

    Almacena los MLAs en orden alfabético dentro del par para canonicalizar.
    """
    a, b = sorted([mla_a.strip().upper(), mla_b.strip().upper()])
    if not a or not b or a == b:
        return False

    pares = cargar_pares_ignorados(alias, data_dir)
    for p in pares:
        if {p.get('mla_a'), p.get('mla_b')} == {a, b}:
            return True   # ya está, no duplicamos

    pares.append({
        'mla_a':           a,
        'mla_b':           b,
        'marcado_en':      datetime.now().strftime('%Y-%m-%d'),
        'razon_opcional':  (razon or '').strip(),
    })
    ruta = _ruta_ignorados(alias, data_dir)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(pares, f, ensure_ascii=False, indent=2)
    return True


def _es_par_ignorado(mla_a: str, mla_b: str, ignorados: list[dict]) -> bool:
    a, b = sorted([mla_a, mla_b])
    return any(
        {p.get('mla_a'), p.get('mla_b')} == {a, b}
        for p in ignorados
    )


# ── Trazabilidad de acciones automáticas ────────────────────────────────────

def registrar_accion_automatica(alias: str, data_dir: str, accion: str,
                                datos: dict) -> None:
    """Append-only log de acciones automáticas (pausar duplicados, etc).

    Estructura: [{timestamp, accion, ...datos}]
    """
    safe_alias = alias.replace(' ', '_').replace('/', '-')
    ruta = os.path.join(data_dir, f'acciones_automaticas_{safe_alias}.json')

    log: list = []
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding='utf-8') as f:
                log = json.load(f) or []
                if not isinstance(log, list):
                    log = []
        except Exception:
            log = []

    entry = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'accion':    accion,
        **datos,
    }
    log.append(entry)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


# ── Clusterización ───────────────────────────────────────────────────────────

def _generar_cluster_id(mla_list: Iterable[str]) -> str:
    """Hash determinístico de los MLAs ordenados — estable entre corridas."""
    key = '|'.join(sorted(mla_list))
    return hashlib.md5(key.encode()).hexdigest()[:8]


def _construir_clusters(items: list[dict], ignorados: list[dict]) -> list[list[dict]]:
    """N²/2 SequenceMatcher → unión por transitividad → lista de clusters."""
    norms = [(it.get('id', ''), _normalizar_titulo(it.get('titulo', '')), it)
             for it in items]
    n = len(norms)

    # parent[] para union-find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        id_i, norm_i, _ = norms[i]
        if not norm_i:
            continue
        for j in range(i + 1, n):
            id_j, norm_j, _ = norms[j]
            if not norm_j:
                continue
            if _es_par_ignorado(id_i, id_j, ignorados):
                continue
            ratio = SequenceMatcher(None, norm_i, norm_j).ratio()
            if ratio >= THRESHOLD_SIMILARIDAD:
                union(i, j)

    # agrupar por raíz
    grupos: dict[int, list[dict]] = {}
    for i in range(n):
        r = find(i)
        grupos.setdefault(r, []).append(norms[i][2])

    return [g for g in grupos.values() if len(g) >= 2]


# ── Sprint Detector v2: análisis de legitimidad por política ML ─────────────

# Listing types de ML:
#   'gold_special' → Clásica (comisión menor, sin cuotas SI)
#   'gold_pro'     → Premium (comisión mayor, con cuotas sin interés)
# Tener UNA Clásica + UNA Premium del mismo producto es estrategia LEGÍTIMA.
# Tener DOS Clásicas idénticas es duplicado prohibido.
_LISTING_CLASSIC = {'gold_special', 'free', 'bronze', 'silver'}
_LISTING_PREMIUM = {'gold_pro', 'gold_premium', 'gold'}


def _normalizar_listing(lt: str) -> str:
    """Devuelve 'classic' o 'premium' o '' (desconocido)."""
    lt = (lt or '').lower().strip()
    if lt in _LISTING_PREMIUM:
        return 'premium'
    if lt in _LISTING_CLASSIC:
        return 'classic'
    return ''


def _analizar_legitimidad(cluster_items: list[dict]) -> tuple[bool, str, dict]:
    """Analiza si un cluster representa una combinación legítima según ML.

    Combinaciones legítimas:
      - Mix de Clásica + Premium del mismo producto → estrategia válida (segmenta
        compradores que valoran precio vs cuotas sin interés)
      - Mix de envío gratis + sin envío gratis → distintos modelos de logística

    Returns: (es_completamente_legitimo, nota_explicativa, mapa_por_id)
      mapa_por_id = {mla_id: True/False según si esa publicación está cubierta
      por alguna diferenciación legítima en el cluster}
    """
    n = len(cluster_items)
    if n < 2:
        return False, '', {}

    listings = [_normalizar_listing(it.get('listing_type', '')) for it in cluster_items]
    shippings = [bool(it.get('free_shipping')) for it in cluster_items]
    ids = [it.get('id', '') for it in cluster_items]

    tipos_listing_distintos = len(set(t for t in listings if t)) > 1
    tipos_envio_distintos = len(set(shippings)) > 1

    # Caso 1: hay diferenciación de listing_type Y de shipping
    if tipos_listing_distintos and tipos_envio_distintos:
        nota = 'Combinación legítima: tipos de publicación + métodos de envío diferenciados.'
        # Marcar como legítimas TODAS — están claramente diferenciadas
        mapa = {iid: True for iid in ids}
        return True, nota, mapa

    # Caso 2: solo diferenciación por listing_type
    if tipos_listing_distintos:
        # Contar cuántas hay de cada tipo
        clasicas = [(iid, it) for iid, lt, it in zip(ids, listings, cluster_items) if lt == 'classic']
        premiums = [(iid, it) for iid, lt, it in zip(ids, listings, cluster_items) if lt == 'premium']

        # Si hay UNA Clásica + UNA Premium → estrategia legítima 100%
        if len(clasicas) == 1 and len(premiums) == 1:
            mapa = {iid: True for iid in ids}
            return True, 'Combinación legítima: 1 Clásica + 1 Premium (estrategia válida ML para segmentar compradores precio vs cuotas).', mapa

        # Si hay UNA Premium pero MÚLTIPLES Clásicas → la Premium es legítima,
        # las Clásicas múltiples son duplicados entre ellas
        if len(premiums) == 1 and len(clasicas) > 1:
            mapa = {iid: (iid == premiums[0][0]) for iid in ids}
            return False, (f'Mixto: la Premium ({premiums[0][0]}) es legítima vs las Clásicas, '
                          f'pero las {len(clasicas)} Clásicas son duplicados entre sí.'), mapa

        # Caso simétrico: 1 Clásica + múltiples Premium
        if len(clasicas) == 1 and len(premiums) > 1:
            mapa = {iid: (iid == clasicas[0][0]) for iid in ids}
            return False, (f'Mixto: la Clásica ({clasicas[0][0]}) es legítima vs las Premium, '
                          f'pero las {len(premiums)} Premium son duplicados entre sí.'), mapa

        # Múltiples de ambos lados — la mejor de cada tipo es legítima
        # (estrategia 1 Clásica + 1 Premium es válida ML), las demás son duplicadas
        # dentro de su subgrupo. Identificar la mejor por ventas y luego visitas.
        def _best(grupo):
            return max(grupo, key=lambda it: (
                int((it[1].get('ventas_30d') or 0)),
                int((it[1].get('visitas_30d') or 0)),
            ))
        best_classic = _best(clasicas)
        best_premium = _best(premiums)
        mapa = {iid: False for iid in ids}
        mapa[best_classic[0]] = True
        mapa[best_premium[0]] = True
        nota = (f'Mixto: tenés {len(clasicas)} Clásicas + {len(premiums)} Premium del mismo producto. '
                f'La mejor Clásica ({best_classic[0]}) y la mejor Premium ({best_premium[0]}) '
                f'son la combinación legítima. Las demás ({len(clasicas) + len(premiums) - 2}) son duplicados.')
        return False, nota, mapa

    # Caso 3: solo diferenciación por shipping (ej: 1 con Full + 1 sin Full)
    if tipos_envio_distintos and len(set(shippings)) == 2:
        # Si hay UNA con free_shipping y UNA sin → legítimo
        con_free = [iid for iid, fs in zip(ids, shippings) if fs]
        sin_free = [iid for iid, fs in zip(ids, shippings) if not fs]
        if len(con_free) == 1 and len(sin_free) == 1:
            mapa = {iid: True for iid in ids}
            return True, 'Combinación legítima: 1 con envío gratis + 1 sin envío gratis (modelos de logística distintos).', mapa

    # Caso 4: nada está diferenciado — todos comparten listing y shipping
    return False, '', {iid: False for iid in ids}


# ── Sprint Detector v3: comparación por attributes oficiales ML ──────────────

# Atributos de ML que cuentan como "diferenciador de variante" — si dos items
# difieren en cualquiera de estos, son variantes legítimas, no duplicados.
# Lista basada en el catálogo oficial de attributes de MercadoLibre.
_ATTRS_VARIANTE = {
    'COLOR', 'COLOR_NAME', 'MAIN_COLOR',
    'SIZE', 'SIZE_NAME', 'SIZE_GRID_ID',
    'CAPACITY', 'CAPACITY_VALUE', 'VOLUME_CAPACITY',
    'MODEL', 'MODEL_NAME',
    'MATERIAL', 'MAIN_MATERIAL',
    'GENDER', 'AGE_GROUP',
    'FLAVOR', 'SCENT', 'FRAGRANCE',
    'PACKAGE_LENGTH', 'PACKAGE_WIDTH', 'PACKAGE_HEIGHT', 'PACK_LENGTH',
    'WEIGHT', 'NET_WEIGHT', 'PACKAGE_WEIGHT',
    'PRINT', 'PATTERN', 'STYLE',
    'COMPATIBILITY',
    'UNITS_PER_PACK', 'PACKAGE_QUANTITY', 'PIECES_NUMBER',
    'POWER_SOURCE', 'BATTERIES_INCLUDED',
    'GEMSTONE', 'METAL_TYPE',
    'EDITION', 'FORMAT',
}

# Cache de attributes en disco — TTL 24h para evitar re-fetchear
_ATTRS_CACHE_TTL_HOURS = 24


def _attrs_cache_path(alias: str, data_dir: str) -> str:
    safe = alias.replace(' ', '_').replace('/', '-')
    return os.path.join(data_dir, f'items_attrs_{safe}.json')


def _cargar_cache_attrs(alias: str, data_dir: str) -> dict:
    """Carga el cache de attributes. Devuelve {item_id: {fetched_at, attributes}}."""
    path = _attrs_cache_path(alias, data_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _guardar_cache_attrs(alias: str, data_dir: str, cache: dict) -> None:
    path = _attrs_cache_path(alias, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _attrs_cache_vigente(entry: dict) -> bool:
    """True si la entry del cache está fresca (<TTL)."""
    if not entry or 'fetched_at' not in entry:
        return False
    try:
        ts = datetime.fromisoformat(entry['fetched_at'].replace('Z', '+00:00'))
        if ts.tzinfo:
            ts = ts.replace(tzinfo=None)
        from datetime import timedelta
        return (datetime.now() - ts) < timedelta(hours=_ATTRS_CACHE_TTL_HOURS)
    except Exception:
        return False


def fetch_items_attributes(item_ids: list, alias: str, data_dir: str) -> dict:
    """Trae los attributes de cada item via API pública de ML (/items multiget).

    Cachea localmente con TTL 24h. Solo fetchea los que faltan o están vencidos.

    Returns: {item_id: list[{id, value_name, value_id}]}
    """
    import requests as _rq
    cache = _cargar_cache_attrs(alias, data_dir)

    # Identificar qué items necesitamos fetchear
    a_fetchear = []
    resultado = {}
    for iid in item_ids:
        if iid in cache and _attrs_cache_vigente(cache[iid]):
            resultado[iid] = cache[iid].get('attributes', [])
        else:
            a_fetchear.append(iid)

    if not a_fetchear:
        return resultado

    # Headers tipo browser para evitar bloqueo de CloudFront
    headers = {
        'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
        'Accept': 'application/json',
    }

    # Fetch en chunks de 20 (máx permitido por /items multiget)
    now_iso = datetime.now().astimezone().isoformat(timespec='seconds')
    for i in range(0, len(a_fetchear), 20):
        chunk = a_fetchear[i:i + 20]
        try:
            r = _rq.get(
                'https://api.mercadolibre.com/items',
                params={'ids': ','.join(chunk), 'attributes': 'id,attributes'},
                headers=headers, timeout=10,
            )
            if r.status_code != 200:
                continue
            for entry in (r.json() or []):
                body = entry.get('body') or {}
                iid = body.get('id')
                if not iid:
                    continue
                attrs = body.get('attributes') or []
                # Guardar solo lo esencial
                attrs_compact = [
                    {'id': a.get('id'),
                     'value_name': a.get('value_name'),
                     'value_id': a.get('value_id')}
                    for a in attrs if a.get('id')
                ]
                resultado[iid] = attrs_compact
                cache[iid] = {
                    'fetched_at': now_iso,
                    'attributes': attrs_compact,
                }
        except Exception as e:
            # Si falla la API, los items quedan sin attributes → fallback a v2.2
            continue

    # Persistir cache
    try:
        _guardar_cache_attrs(alias, data_dir, cache)
    except Exception:
        pass

    return resultado


def _attrs_index_por_id(attrs_list: list) -> dict:
    """Convierte lista de attributes en dict {ATTR_ID: value_name}."""
    return {a['id']: (a.get('value_name') or '') for a in (attrs_list or []) if a.get('id')}


def _son_variantes_por_attributes(items_attrs: list) -> tuple:
    """Compara los attributes de N items y decide si son variantes legítimas.

    Args:
        items_attrs: lista de listas de attributes (uno por item).

    Returns:
        (es_variantes, attr_diferenciador)
        es_variantes: True si los items difieren en algún atributo de _ATTRS_VARIANTE
        attr_diferenciador: el ATTR_ID que difiere (ej: 'COLOR'), o '' si no aplica
    """
    if len(items_attrs) < 2:
        return False, ''

    # Indexar attributes de cada item
    indices = [_attrs_index_por_id(attrs) for attrs in items_attrs]

    # Para cada attribute de variante, ver si algún item difiere
    for attr_id in _ATTRS_VARIANTE:
        valores = set()
        for idx in indices:
            v = idx.get(attr_id, '').strip().lower()
            if v:
                valores.add(v)
        # Si tenemos 2+ valores distintos en este atributo → variante
        if len(valores) >= 2:
            return True, attr_id

    return False, ''


# ── Clasificación de severidad ───────────────────────────────────────────────

def _clasificar_cluster(cluster_items: list[dict],
                        attrs_por_item: dict = None) -> tuple[str, str, dict]:
    """Devuelve (severidad, nota_legitimidad, mapa_legitimos_por_id).

    Severidades:
      'legitimo'        — todas las publicaciones están legítimamente diferenciadas
                           (Clásica+Premium, distintos envíos, distintos colores/tamaños).
      'mixto'           — algunas son legítimas, otras son duplicados reales.
      'puro'            — todas idénticas → duplicados prohibidos
      'subperformante'  — variantes con tráfico desperdiciado
      'sano'            — variantes funcionando bien

    Sprint Detector v3 (05/05/2026): si `attrs_por_item` se pasa (dict {item_id:
    list_attrs}), prioriza la comparación por attributes oficiales de ML antes
    de caer al análisis por título (más confiable).
    """
    # ── PASO 0 (v3): si tenemos attributes oficiales, usarlos primero ──────
    if attrs_por_item:
        items_attrs = []
        for it in cluster_items:
            iid = it.get('id', '')
            items_attrs.append(attrs_por_item.get(iid, []))

        # ¿Son variantes según los attributes oficiales?
        es_var_attr, attr_diff = _son_variantes_por_attributes(items_attrs)
        if es_var_attr:
            # Variantes legítimas según ML — clasificar por métricas
            cuenta_con_ventas = sum(1 for it in cluster_items if (it.get('ventas_30d') or 0) > 0)
            total = len(cluster_items)
            visitas_sin_venta = sum(
                (it.get('visitas_30d') or 0)
                for it in cluster_items
                if (it.get('ventas_30d') or 0) == 0
            )
            nota = f'Variantes legítimas según ML — difieren en atributo: {attr_diff}'
            mapa_leg = {it.get('id', ''): True for it in cluster_items}
            if cuenta_con_ventas / total >= 0.60:
                return 'sano', nota, mapa_leg
            if visitas_sin_venta >= 10:
                return 'subperformante', nota, mapa_leg
            return 'sano', nota, mapa_leg

    # 1) Análisis de legitimidad por política ML (Sprint Detector v2)
    es_legitimo, nota_leg, mapa_leg = _analizar_legitimidad(cluster_items)

    # Si es 100% legítimo → no es duplicado en absoluto
    if es_legitimo:
        return 'legitimo', nota_leg, mapa_leg

    # Si es mixto (algunas legítimas + algunas duplicadas) → severidad 'mixto'
    if mapa_leg and any(mapa_leg.values()) and not all(mapa_leg.values()):
        return 'mixto', nota_leg, mapa_leg

    # 2) Análisis tradicional por título y métricas (lógica original + v2.1 fix)
    norms = [_normalizar_titulo(it.get('titulo', '')) for it in cluster_items]

    # ¿Todos los títulos normalizados son IDÉNTICOS? → duplicado puro
    if len(set(norms)) == 1:
        return 'puro', '', mapa_leg

    # Hotfix v2.1 (05/05/2026): la lógica anterior marcaba como 'puro' cuando
    # encontraba UN par con diff vacío (= 1 par de duplicados reales) aunque
    # los OTROS pares fueran variantes legítimas. Eso era falso positivo.
    #
    # Lógica nueva: sumar cada par como (variante, idéntico, no_variante).
    # - Si hay AL MENOS 1 par no_variante → cluster puro (hay diferencia real
    #   de producto que no se explica por variante).
    # - Si todos los pares son variantes O idénticos → es cluster de variantes
    #   legítimas con posibles duplicados internos (clasificar por métricas).
    hay_no_variante = False
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            diff = _tokens_diferencia(norms[i], norms[j])
            if not diff:
                continue   # par idéntico — no cuenta como "no variante"
            # Pasar los normalizados para que la heurística estructural
            # detecte variantes truncadas por ML (gri, neg, aqu, multicol)
            if not _diferencias_son_solo_variantes(diff, norms[i], norms[j]):
                hay_no_variante = True
                break
        if hay_no_variante:
            break

    if hay_no_variante:
        return 'puro', '', mapa_leg

    # Es cluster de variantes legítimas (con posibles duplicados internos)
    # → clasificar por métricas
    cuenta_con_ventas = sum(1 for it in cluster_items if (it.get('ventas_30d') or 0) > 0)
    total = len(cluster_items)
    visitas_sin_venta = sum(
        (it.get('visitas_30d') or 0)
        for it in cluster_items
        if (it.get('ventas_30d') or 0) == 0
    )

    if cuenta_con_ventas / total >= 0.60:
        return 'sano', '', mapa_leg
    if visitas_sin_venta >= 10:
        return 'subperformante', '', mapa_leg
    return 'sano', '', mapa_leg


def _identificar_ganadora(cluster_items: list[dict]) -> str:
    """Devuelve el id de la publicación con mayor ventas_30d (o más visitas si todas en 0)."""
    if not cluster_items:
        return ''
    return max(
        cluster_items,
        key=lambda it: (it.get('ventas_30d') or 0, it.get('visitas_30d') or 0),
    ).get('id', '')


def _calcular_impacto_monetario(cluster_items: list[dict], ganadora_id: str) -> tuple[int, float]:
    """Estima impacto: visitas_perdidas × conv_ganadora × precio_ganadora.

    Returns (visitas_perdidas_30d, impacto_estimado_en_pesos).
    """
    ganadora = next((it for it in cluster_items if it.get('id') == ganadora_id), None)
    if not ganadora:
        return 0, 0.0

    conv_g = float(ganadora.get('conversion_pct') or 0) / 100.0  # 1.5 → 0.015
    precio_g = float(ganadora.get('precio') or 0)

    visitas_perdidas = sum(
        int(it.get('visitas_30d') or 0)
        for it in cluster_items
        if it.get('id') != ganadora_id
    )

    impacto = visitas_perdidas * conv_g * precio_g
    return visitas_perdidas, round(impacto, 0)


def _generar_recomendaciones(cluster_items: list[dict],
                             ganadora_id: str) -> tuple[dict, str]:
    """Heurística determinística que sugiere qué hacer con cada item del cluster.

    Returns:
      (recomendaciones_por_mla, resumen_textual)
    """
    recs: dict = {}
    n_pausar    = 0
    n_revisar   = 0
    n_sin_traf  = 0

    # Conversión histórica de la ganadora — referencia
    ganadora = next((it for it in cluster_items if it.get('id') == ganadora_id), None)
    conv_ganadora = float((ganadora or {}).get('conversion_pct') or 0)

    for it in cluster_items:
        mla = it.get('id', '')
        ventas  = int(it.get('ventas_30d') or 0)
        visitas = int(it.get('visitas_30d') or 0)
        conv    = float(it.get('conversion_pct') or 0)

        if mla == ganadora_id:
            recs[mla] = Recomendacion(
                accion='mantener',
                motivo='Ganadora del cluster — vende mejor que el resto',
                pre_seleccionar=False,
            )
            continue

        if ventas == 0 and visitas >= 10:
            recs[mla] = Recomendacion(
                accion='pausar_traf',
                motivo=f'Sin ventas en 30d pero recibe {visitas} visitas — pausar para que ese tráfico vaya a la ganadora',
                pre_seleccionar=True,
            )
            n_pausar += 1
        elif ventas == 0 and visitas < 10:
            recs[mla] = Recomendacion(
                accion='pausar_sin_traf',
                motivo=f'Sin ventas y casi sin visitas ({visitas}) — limpieza recomendada',
                pre_seleccionar=True,
            )
            n_pausar += 1
            n_sin_traf += 1
        elif ventas > 0 and conv_ganadora > 0 and conv < (conv_ganadora * 0.5):
            recs[mla] = Recomendacion(
                accion='revisar',
                motivo=f'Vende {ventas} con {conv:.2f}% conversión (la ganadora convierte {conv_ganadora:.2f}%) — revisar pricing/foto antes de pausar',
                pre_seleccionar=False,
            )
            n_revisar += 1
        else:
            recs[mla] = Recomendacion(
                accion='mantener',
                motivo=f'Vende {ventas} unid/mes con conversión sana — mantener',
                pre_seleccionar=False,
            )

    # Construir resumen accionable
    parts = []
    if ganadora:
        parts.append(
            f"Mantener {ganadora_id} (vende {int(ganadora.get('ventas_30d') or 0)} unid/mes "
            f"con {conv_ganadora:.2f}% de conversión — ganadora del cluster)"
        )
    if n_pausar:
        sin_traf_extra = f" ({n_sin_traf} sin tráfico, candidatas a cierre manual desde ML si querés limpiar)" if n_sin_traf else ''
        parts.append(f"Pausar {n_pausar} sin ventas en 30d{sin_traf_extra}")
    if n_revisar:
        parts.append(f"Revisar {n_revisar} con conversión muy baja vs la ganadora antes de decidir")

    resumen = ' · '.join(parts) if parts else 'Sin acciones recomendadas para este cluster.'
    return recs, resumen


def _titulo_corto(cluster_items: list[dict]) -> str:
    """Tokens en común entre todos los títulos del cluster — qué representa el cluster."""
    if not cluster_items:
        return ''
    sets = [set(_normalizar_titulo(it.get('titulo', '')).split()) for it in cluster_items]
    comunes = set.intersection(*sets) if sets else set()
    if not comunes:
        # Fallback: primeras 5 palabras del título de la primera
        primer = (cluster_items[0].get('titulo') or '').split()[:5]
        return ' '.join(primer)
    # Mantener orden del primer título
    primer_tokens = _normalizar_titulo(cluster_items[0].get('titulo', '')).split()
    ordenados = [t for t in primer_tokens if t in comunes][:6]
    return ' '.join(ordenados).title()


# ── API pública ──────────────────────────────────────────────────────────────

def detectar_duplicados(stock_items: list[dict], alias: str,
                        data_dir: str,
                        incluir_sanos: bool = False) -> list[Cluster]:
    """Detecta clusters de publicaciones duplicadas/canibalizándose.

    Args:
      stock_items: lista de items con campos id, titulo, precio, ventas_30d,
                   visitas_30d, conversion_pct.
      alias: alias de la cuenta (usado para cargar la lista de pares ignorados).
      data_dir: ruta a /data del proyecto.
      incluir_sanos: si False (default), filtra los clusters clasificados
                     como 'sano' antes de devolver. La UI no los muestra.

    Returns:
      Lista de Cluster ordenada por severidad (puro → subperformante → sano)
      y dentro de cada nivel por impacto_monetario_estimado descendente.
    """
    ignorados = cargar_pares_ignorados(alias, data_dir)
    grupos = _construir_clusters(stock_items, ignorados)

    # Sprint Detector v3: pre-fetch de attributes oficiales para todos los items
    # que están en clusters sospechosos. Esto permite la comparación precisa
    # por attributes oficiales de ML (color, talle, capacidad, etc.) en lugar
    # de inferir desde el título. Tolerante a fallas — si no hay attributes,
    # cae al análisis v2.2 por título.
    attrs_por_item: dict = {}
    try:
        ids_en_clusters = []
        for g in grupos:
            for it in g:
                iid = it.get('id', '')
                if iid:
                    ids_en_clusters.append(iid)
        if ids_en_clusters:
            attrs_por_item = fetch_items_attributes(ids_en_clusters, alias, data_dir)
    except Exception:
        attrs_por_item = {}

    clusters: list[Cluster] = []
    for grupo in grupos:
        severidad, nota_leg, mapa_leg = _clasificar_cluster(grupo, attrs_por_item)

        # Filtros: ocultar 'sano' por default; ocultar 'legitimo' a menos que se pida
        if not incluir_sanos and severidad == 'sano':
            continue

        ganadora_id = _identificar_ganadora(grupo)
        visitas_perdidas, impacto = _calcular_impacto_monetario(grupo, ganadora_id)

        items_cluster = [
            ItemCluster(
                id=it.get('id', ''),
                titulo=it.get('titulo', ''),
                precio=float(it.get('precio') or 0),
                ventas_30d=int(it.get('ventas_30d') or 0),
                visitas_30d=int(it.get('visitas_30d') or 0),
                conversion_pct=float(it.get('conversion_pct') or 0),
                es_ganadora=(it.get('id') == ganadora_id),
                listing_type=it.get('listing_type', ''),
                free_shipping=bool(it.get('free_shipping')),
                es_legitimo=bool(mapa_leg.get(it.get('id', ''), False)),
            )
            for it in grupo
        ]
        # Ordenar dentro del cluster: ganadora primero, luego por ventas desc
        items_cluster.sort(key=lambda x: (not x.es_ganadora, -x.ventas_30d, -x.visitas_30d))

        cluster_id = _generar_cluster_id(it.id for it in items_cluster)
        # En clusters legítimos, no generar recomendaciones de pausar
        if severidad == 'legitimo':
            recomendaciones = {
                it.id: Recomendacion(
                    accion='mantener',
                    motivo='Diferenciación legítima por política ML — no es duplicado.',
                    pre_seleccionar=False,
                ) for it in items_cluster
            }
            resumen_rec = nota_leg or 'Combinación legítima — mantené todas las publicaciones.'
        elif severidad == 'mixto':
            # Para clusters mixtos: en lugar de recomendar pausar TODAS, solo las
            # que NO están cubiertas por diferenciación legítima.
            recomendaciones = {}
            for it in items_cluster:
                if it.id == ganadora_id:
                    recomendaciones[it.id] = Recomendacion(
                        accion='mantener',
                        motivo='Ganadora del cluster',
                        pre_seleccionar=False,
                    )
                elif it.es_legitimo:
                    recomendaciones[it.id] = Recomendacion(
                        accion='mantener',
                        motivo='Diferenciación legítima por política ML',
                        pre_seleccionar=False,
                    )
                else:
                    # Es duplicado real — recomendar pausar (si no tiene ventas)
                    if it.ventas_30d == 0:
                        recomendaciones[it.id] = Recomendacion(
                            accion='pausar_sin_traf' if it.visitas_30d < 10 else 'pausar_traf',
                            motivo='Duplicado real (mismo tipo de listing y envío que la ganadora)',
                            pre_seleccionar=True,
                        )
                    else:
                        recomendaciones[it.id] = Recomendacion(
                            accion='revisar',
                            motivo='Duplicado con ventas — revisá manualmente cuál mantener',
                            pre_seleccionar=False,
                        )
            resumen_rec = nota_leg
        else:
            recomendaciones, resumen_rec = _generar_recomendaciones(grupo, ganadora_id)

        clusters.append(Cluster(
            cluster_id=cluster_id,
            severidad=severidad,
            items=items_cluster,
            titulo_corto=_titulo_corto(grupo),
            visitas_perdidas_30d=visitas_perdidas if severidad not in ('legitimo',) else 0,
            impacto_monetario_estimado=impacto if severidad not in ('legitimo',) else 0.0,
            recomendaciones=recomendaciones,
            resumen_recomendacion=resumen_rec,
            nota_legitimidad=nota_leg,
        ))

    # Ordenar: puro primero (más urgente), luego mixto, sub, sano, legitimo
    orden_severidad = {'puro': 0, 'mixto': 1, 'subperformante': 2, 'sano': 3, 'legitimo': 4}
    clusters.sort(key=lambda c: (orden_severidad.get(c.severidad, 9), -c.impacto_monetario_estimado))
    return clusters


def resumen_para_alertas(clusters: list[Cluster]) -> dict:
    """Devuelve resumen agregable al centro de alertas existente.

    Sprint Detector v2: incluye 'legitimos' y 'mixtos' como categorías separadas
    para que el usuario entienda qué es realmente un problema vs qué está bien.
    """
    puros     = [c for c in clusters if c.severidad == 'puro']
    mixtos    = [c for c in clusters if c.severidad == 'mixto']
    subs      = [c for c in clusters if c.severidad == 'subperformante']
    legitimos = [c for c in clusters if c.severidad == 'legitimo']

    # Solo los puros + subs + parte mixta del cluster representan impacto real
    impacto_total = sum(c.impacto_monetario_estimado for c in puros + subs + mixtos)
    visitas_total = sum(c.visitas_perdidas_30d for c in puros + subs + mixtos)
    return {
        'puros':          len(puros),
        'mixtos':         len(mixtos),
        'subperformantes': len(subs),
        'legitimos':      len(legitimos),
        'visitas_perdidas_30d': visitas_total,
        'impacto_monetario_estimado': impacto_total,
    }
