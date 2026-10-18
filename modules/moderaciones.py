"""
Prevencion de moderaciones — Parte 1: historial de infracciones.

Pedido de Guille (2026-10-16), a partir de "Detectar Palabras Prohibidas" de
UpSeller. La investigacion mostro que en Novara casi ninguna infraccion es por
palabras: de 139 desde 2025, la mayoria son fotos, originalidad, categoria y
duplicados. Este modulo arranca por ver eso con la fuente oficial:
`GET /moderations/infractions/{user_id}` (doc "Gestionar moderaciones").

Solo lee. Guarda el ultimo resultado por cuenta para no pedirlo en cada carga.
"""
import re
from datetime import datetime

from core.db_storage import db_load, db_save

LIMITE_ML = 20               # la API rechaza limit > 20 (400 "limit max value is 20")
DESDE_DEFAULT = '2025-01-01'

# Clasificacion por el texto del motivo; el filter_subgroup de ML se guarda
# igual pero no alcanza solo (PQT mezcla portadas con otros elementos).
CATEGORIAS = [
    ('contacto',     'Datos de contacto',                       r'datos de contacto'),
    ('fotos',        'Fotos',                                   r'portada|foto|marcas? de agua|fondo blanco|etiquetas o atributos'),
    ('originalidad', 'Originalidad y propiedad intelectual',    r'original|falsific|propiedad intelectual|factura'),
    ('categoria',    'Categoría incorrecta',                    r'categor[ií]a incorrecta'),
    ('duplicado',    'Duplicadas',                              r'igual a otra'),
    ('catalogo',     'Catálogo',                                r'cat[aá]logo'),
    ('precio',       'Precio',                                  r'precio'),
]
POR_SUBGRUPO = {'FOTOS': 'fotos', 'PI_FAKES': 'originalidad', 'DOMAIN': 'categoria', 'DUPLIS': 'duplicado',
                'CATALOG': 'catalogo', 'DP': 'contacto'}
NOMBRES = {k: n for k, n, _ in CATEGORIAS} | {'otros_elementos': 'Otros elementos (ML no da detalle)', 'otras': 'Otras'}


def _safe(alias: str) -> str:
    return (alias or '').replace(' ', '_').replace('/', '-')


def _path(alias: str) -> str:
    return f'data/moderaciones_{_safe(alias)}.json'


def _texto(html: str) -> str:
    t = re.sub(r'<[^>]+>', ' ', html or '')
    return re.sub(r'\s+', ' ', t).strip()


def clasificar(inf: dict) -> str:
    motivo = (inf.get('reason') or '').lower()
    for clave, _, patron in CATEGORIAS:
        if motivo and re.search(patron, motivo):
            return clave
    if inf.get('element_type') not in (None, 'ITM'):
        return 'otros_elementos'
    return POR_SUBGRUPO.get(inf.get('filter_subgroup'), 'otras')


def _seller_id(client):
    if not client.account.user_id:
        client.account.user_id = (client.get_me() or {}).get('id')
    return client.account.user_id


def traer_infracciones(client, desde: str = DESDE_DEFAULT) -> list[dict]:
    uid, out, offset = _seller_id(client), [], 0
    while True:
        r = client._get(f'/moderations/infractions/{uid}', params={
            'date_created_since': desde, 'limit': LIMITE_ML, 'offset': offset, 'language': 'ES'}) or {}
        lote = r.get('infractions') or []
        out += lote
        offset += LIMITE_ML
        if not lote or offset >= (r.get('paging') or {}).get('total', 0):
            return out


def _estado_items(client, ids: list[str]) -> dict:
    """Como esta HOY cada publicacion afectada (puede haberse corregido)."""
    out = {}
    ids = [i for i in ids if re.fullmatch(r'MLA\d+', i or '')]
    for k in range(0, len(ids), 20):
        try:
            entries = client._get('/items', params={'ids': ','.join(ids[k:k + 20]),
                                                    'attributes': 'id,title,status,sub_status,permalink,secure_thumbnail,thumbnail'}) or []
        except Exception:
            continue
        for e in entries:
            b = e.get('body') or {}
            if e.get('code') == 200 and b.get('id'):
                out[b['id']] = {'titulo': b.get('title', ''), 'status': b.get('status'),
                                'sub_status': b.get('sub_status') or [], 'permalink': b.get('permalink'),
                                'thumbnail': (b.get('secure_thumbnail') or b.get('thumbnail') or '').replace('http://', 'https://') or None}
    return out


def resumen(infracciones: list[dict], estados: dict) -> dict:
    """Agrupa por categoria y por motivo exacto, mas reciente primero."""
    filas = []
    for i in infracciones:
        item_id = i.get('related_item_id') or ''
        fotos = [pid for p in i.get('pictures') or [] for pid in p.get('picture_ids') or []]
        sug = [c.get('name') or c.get('id') for c in (i.get('suggested') or {}).get('categories') or []]
        filas.append({'id': i.get('id'), 'fecha': (i.get('date_created') or '')[:10],
                      'categoria': clasificar(i), 'motivo': i.get('reason') or '',
                      'solucion': _texto(i.get('remedy')), 'subgrupo': i.get('filter_subgroup'),
                      'tipo_elemento': i.get('element_type'),
                      'item_id': item_id if re.fullmatch(r'MLA\d+', item_id) else None,
                      'item': estados.get(item_id), 'fotos_marcadas': fotos, 'categoria_sugerida': sug})
    filas.sort(key=lambda f: f['fecha'], reverse=True)

    grupos = {}
    for f in filas:
        g = grupos.setdefault(f['categoria'], {'categoria': f['categoria'], 'nombre': NOMBRES[f['categoria']],
                                               'total': 0, 'motivos': {}})
        g['total'] += 1
        m = g['motivos'].setdefault(f['motivo'] or '(sin texto)', {'motivo': f['motivo'] or '(sin texto)',
                                                                   'solucion': f['solucion'], 'casos': []})
        m['casos'].append(f)
    # ML repite la infraccion de la misma publicacion (visto: 19 "fondo blanco"
    # que eran UN microfono marcado a diario). Se cuenta por publicacion.
    def _por_publicacion(casos):
        pubs = {}
        for c in casos:
            clave = c['item_id'] or c['id']
            p = pubs.setdefault(clave, {**c, 'veces': 0, 'primera': c['fecha'], 'fotos_marcadas': []})
            p['veces'] += 1
            p['primera'] = min(p['primera'], c['fecha'])
            p['fotos_marcadas'] = list(dict.fromkeys(p['fotos_marcadas'] + c['fotos_marcadas']))
        orden = {'active': 0, 'paused': 1, 'under_review': 2, 'inactive': 3, 'closed': 4}
        lista = sorted(pubs.values(), key=lambda p: p['fecha'], reverse=True)        # mas recientes primero
        return sorted(lista, key=lambda p: orden.get((p.get('item') or {}).get('status'), 5))   # y las activas arriba

    salida = sorted(grupos.values(), key=lambda g: -g['total'])
    for g in salida:
        motivos = sorted(g['motivos'].values(), key=lambda m: -len(m['casos']))
        for m in motivos:
            m['infracciones'] = len(m['casos'])
            m['casos'] = _por_publicacion(m['casos'])
        g['motivos'] = motivos
        g['publicaciones'] = len({c['item_id'] or c['id'] for m in motivos for c in m['casos']})
        g['activas'] = len({c['item_id'] for m in motivos for c in m['casos']
                            if c['item_id'] and (c.get('item') or {}).get('status') == 'active'})
    activas = len({f['item_id'] for f in filas if f['item_id'] and (f.get('item') or {}).get('status') == 'active'})
    return {'total': len(filas), 'publicaciones': len({f['item_id'] or f['id'] for f in filas}),
            'grupos': salida, 'publicaciones_activas_afectadas': activas,
            'ultima': filas[0]['fecha'] if filas else None}


def historial(client, alias: str, desde: str = DESDE_DEFAULT, refrescar: bool = False) -> dict:
    guardado = db_load(_path(alias)) or {}
    if guardado.get('desde') == desde and guardado.get('datos') and not refrescar:
        return {**guardado['datos'], 'actualizado': guardado.get('actualizado'), 'desde': desde, 'desde_cache': True}
    infr = traer_infracciones(client, desde)
    estados = _estado_items(client, list(dict.fromkeys(i.get('related_item_id') for i in infr)))
    datos = resumen(infr, estados)
    ahora = datetime.now().isoformat(timespec='seconds')
    try:
        db_save(_path(alias), {'desde': desde, 'actualizado': ahora, 'datos': datos})
    except Exception:
        pass
    return {**datos, 'actualizado': ahora, 'desde': desde, 'desde_cache': False}


# ── Parte 4: categoria sugerida por ML vs. la actual ─────────────────────────
# Fuente: /sites/MLA/domain_discovery/search (publico, vivo al 2026-10-16).
# Validado contra el historial de Novara: en 15 de las 17 infracciones por
# "categoria incorrecta", su primera sugerencia fue exactamente la categoria que
# ML despues exigio. En el catalogo activo algunas sugerencias son dudosas
# (funda de mesa -> "Fundas para Sillones"), asi que es un AVISO para revisar:
# nunca cambia la categoria.

def sugerir_categorias(titulo: str, limite: int = 3) -> list[dict] | None:
    import requests
    try:
        r = requests.get('https://api.mercadolibre.com/sites/MLA/domain_discovery/search',
                         params={'q': titulo, 'limit': limite}, headers={'Accept': 'application/json'}, timeout=15)
        datos = r.json() if r.ok else None
    except Exception:
        return None
    if not isinstance(datos, list):
        return None
    return [{'id': d.get('category_id'), 'nombre': d.get('category_name'), 'domain_id': d.get('domain_id')}
            for d in datos if d.get('category_id')]


def comparar_categoria(actual: str, sugeridas: list[dict] | None) -> str:
    """'sin_dato' si ML no sugirio nada; 'coincide' si la primera es la actual;
    'revisar' si la actual esta entre las otras sugeridas o no aparece."""
    if not sugeridas:
        return 'sin_dato'
    ids = [s['id'] for s in sugeridas]
    return 'coincide' if ids[0] == actual else 'revisar'


def revisar_categorias(client) -> dict:
    import time
    ids, offset = [], 0
    while True:
        data = client.get_my_listings(limit=50, offset=offset, status='active') or {}
        ids += data.get('results') or []
        offset += 50
        if offset >= (data.get('paging') or {}).get('total', 0):
            break
    nombres, filas = {}, []
    for k in range(0, len(ids), 20):
        entries = client._get('/items', params={'ids': ','.join(ids[k:k + 20]),
                                                'attributes': 'id,title,category_id,catalog_listing,permalink,secure_thumbnail,thumbnail'}) or []
        for e in entries:
            b = e.get('body') or {}
            if e.get('code') != 200 or b.get('catalog_listing'):
                continue      # en catalogo la categoria la define ML
            sug = sugerir_categorias(b.get('title', ''))
            actual = b.get('category_id')
            if actual not in nombres:
                try:
                    nombres[actual] = (client._get(f'/categories/{actual}') or {}).get('name')
                except Exception:
                    nombres[actual] = None
            filas.append({'id': b['id'], 'titulo': b.get('title', ''), 'permalink': b.get('permalink'),
                          'thumbnail': (b.get('secure_thumbnail') or b.get('thumbnail') or '').replace('http://', 'https://') or None,
                          'actual': {'id': actual, 'nombre': nombres[actual]}, 'sugeridas': sug or [],
                          'estado': comparar_categoria(actual, sug)})
            time.sleep(0.15)   # una por una, sin rafagas
    orden = {'revisar': 0, 'sin_dato': 1, 'coincide': 2}
    filas.sort(key=lambda f: orden[f['estado']])
    cuenta = {e: sum(1 for f in filas if f['estado'] == e) for e in orden}
    return {'revisadas': len(filas), **cuenta, 'filas': filas}
