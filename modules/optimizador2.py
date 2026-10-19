"""
Optimizador IA 2.0 — lectura de competidores e informe de competencia.

Definición aprobada por el usuario el 18/10/2026 (doc "Optimizador IA 2.0 —
Definición"). ML cerró la API de publicaciones ajenas: lo que se sabe de un
competidor lo lee el botón en tu navegador, desde su página, y llega acá.

  lista_para_leer   competidores elegidos de un producto y si el botón los puede leer
  limpiar_ficha     valida lo que manda el botón (viene de páginas de terceros)
  mi_publicacion    la publicación propia, por API
  informe           tu publicación contra cada competidor, y lo que te falta
"""
import unicodedata

from modules import cerebro
from modules import grupos_producto as gp

ELEGIDOS = (cerebro.CLASE_DIRECTO, cerebro.CLASE_CANDIDATO)
# El botón corre en www.mercadolibre.com.ar: solo lee páginas del mismo sitio.
# Los resultados pagos linkean a un redireccionador de ML y las publicaciones
# viejas a articulo.mercadolibre.com.ar; esas hay que abrirlas y tocar el botón ahí.
LEIBLE = 'https://www.mercadolibre.com.ar/'
MAX_RESENAS = 40


def elegidos(alias: str, item_id: str) -> list[dict]:
    """Los que el usuario marcó para este producto (cualquier publicación del grupo)."""
    return [c for c in gp.competidores_del_grupo(alias, item_id) if c.get('clase') in ELEGIDOS]


def lista_para_leer(alias: str, item_id: str) -> list[dict]:
    return [{'clave': c['clave'], 'id': c['id'], 'title': c.get('title', ''),
             'permalink': c.get('permalink') or '',
             'leible': (c.get('permalink') or '').startswith(LEIBLE)}
            for c in elegidos(alias, item_id)]


def _txt(v, n: int) -> str:
    return v.strip()[:n] if isinstance(v, str) else ''


def _num(v, lo: float, hi: float, entero: bool = False):
    try:
        n = int(v) if entero else float(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def limpiar_ficha(d: dict) -> dict:
    """Solo tipos y largos esperables: el contenido viene de páginas ajenas."""
    d = d if isinstance(d, dict) else {}
    attrs = [{'name': _txt(a.get('name'), 80), 'value': _txt(a.get('value'), 120)}
             for a in (d.get('attributes') or [])[:60] if isinstance(a, dict)]
    resenas = [{'rating': _num(r.get('rating'), 1, 5, True), 'texto': _txt(r.get('texto'), 600)}
               for r in (d.get('resenas') or [])[:MAX_RESENAS] if isinstance(r, dict)]
    return {
        'title': _txt(d.get('title'), 200),
        'price': _num(d.get('price'), 1, 1e9),
        'description': _txt(d.get('description'), 8000),
        'attributes': [a for a in attrs if a['name'] and a['value']],
        'photos_count': _num(d.get('photos_count'), 0, 100, True),
        'videos': _num(d.get('videos'), 0, 50, True),
        'premium': bool(d.get('premium')),
        'cuotas': _num(d.get('cuotas'), 0, 24, True),
        'full_ship': bool(d.get('full_ship')),
        'free_ship': bool(d.get('free_ship')),
        'sold_quantity': _num(d.get('sold_quantity'), 0, 1e8, True),
        'reviews_rating': _num(d.get('reviews_rating'), 0, 5),
        'reviews_total': _num(d.get('reviews_total'), 0, 1e8, True),
        'resenas': [r for r in resenas if r['texto']],
    }


def _cuotas_propias(item: dict) -> int:
    """Cuotas sin interés de una publicación propia (ver CUOTAS_A_TAGS)."""
    if item.get('listing_type_id') != 'gold_pro':
        return 0
    tags = set(item.get('tags') or [])
    for n in (3, 9, 12):
        if f'{n}x_campaign' in tags:
            return n
    return 6


def mi_publicacion(client, item_id: str) -> dict:
    item = client.get_item(item_id) or {}
    if 'id' not in item:
        raise ValueError(f'No se encontró la publicación {item_id}.')
    envio = item.get('shipping') or {}
    return {
        'id': item['id'], 'title': item.get('title') or item.get('family_name') or '',
        'price': item.get('price'), 'permalink': item.get('permalink') or '',
        'sold_quantity': item.get('sold_quantity'),
        'photos_count': len(item.get('pictures') or []),
        'videos': 1 if item.get('video_id') else 0,
        'cuotas': _cuotas_propias(item),
        'full_ship': envio.get('logistic_type') == 'fulfillment',
        'free_ship': bool(envio.get('free_shipping')),
        'attributes': [{'name': a.get('name') or a.get('id'), 'value': a.get('value_name')}
                       for a in item.get('attributes') or [] if a.get('value_name')],
    }


def _norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', (s or '').lower())
    return ' '.join(''.join(c for c in s if not unicodedata.combining(c)).split())


def _fila(c: dict) -> dict:
    return {
        'id': c.get('id'), 'titulo': c.get('title') or '', 'permalink': c.get('permalink') or '',
        'precio': _num(c.get('price'), 0, 1e9), 'cuotas': _num(c.get('cuotas'), 0, 24, True),
        'full': bool(c.get('full_ship')), 'envio_gratis': bool(c.get('free_ship')),
        # La captura desde la búsqueda guarda estos sin validar: solo números
        'ventas': _num(c.get('sold_quantity'), 0, 1e8, True), 'rating': _num(c.get('reviews_rating'), 0, 5),
        'opiniones': _num(c.get('reviews_total'), 0, 1e8, True), 'resenas_leidas': len(c.get('resenas') or []),
        'descripcion': len(c.get('description') or '') if isinstance(c.get('description'), str) else 0,
        'fotos': _num(c.get('photos_count'), 0, 100, True), 'videos': _num(c.get('videos'), 0, 50, True),
        'publicidad': bool(c.get('publicidad')),
        'atributos': len(c.get('attributes') or []), 'ficha_ts': c.get('ficha_ts'),
    }


def informe(mio: dict, comps: list[dict]) -> dict:
    leidos = [c for c in comps if c.get('ficha_ts')]
    mios = {_norm(a['name']) for a in mio.get('attributes') or []}

    faltan: dict[str, dict] = {}
    for c in leidos:
        for a in c.get('attributes') or []:
            k = _norm(a.get('name'))
            if not k or k in mios:
                continue
            f = faltan.setdefault(k, {'nombre': a['name'], 'cuantos': 0, 'valores': []})
            f['cuantos'] += 1
            if a.get('value') and a['value'] not in f['valores']:
                f['valores'].append(a['value'])

    avisos = []
    n = len(leidos)
    if n:
        con_video = sum(1 for c in leidos if c.get('videos'))
        if con_video and not mio.get('videos'):
            avisos.append(f'{con_video} de {n} competidores tienen video y vos no.')
        max_fotos = max((c.get('photos_count') or 0) for c in leidos)
        if max_fotos > (mio.get('photos_count') or 0):
            avisos.append(f'Tenés {mio.get("photos_count") or 0} fotos; el que más tiene, {max_fotos}.')
        mas_cuotas = max((c.get('cuotas') or 0) for c in leidos)
        if mas_cuotas > (mio.get('cuotas') or 0):
            avisos.append(f'Hay competidores con {mas_cuotas} cuotas sin interés; vos tenés '
                          f'{mio.get("cuotas") or "ninguna"}.')
        if any(c.get('full_ship') for c in leidos) and not mio.get('full_ship'):
            avisos.append('Hay competidores con envío Full y vos no.')
    pagos = sum(1 for c in comps if c.get('publicidad'))
    if pagos:
        avisos.append(f'{pagos} de {len(comps)} competidores aparecieron por publicidad en la búsqueda.')

    return {
        'mio': _fila(mio),
        'competidores': [_fila(c) for c in comps],
        'faltan_atributos': sorted(faltan.values(), key=lambda f: -f['cuantos']),
        'avisos': avisos,
        'sin_leer': len(comps) - n,
    }
