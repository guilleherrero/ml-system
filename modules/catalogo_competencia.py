"""
Quien mas esta vendiendo en mis fichas de catalogo.

Una publicacion de catalogo no compite en una busqueda: compite dentro de una
ficha, contra todos los que publicaron sobre esa misma ficha. El que gana la buy
box se lleva casi todo el trafico; los demas quedan detras de un "otras
opciones de compra" que casi nadie abre. Por eso una publicacion de catalogo
puede perder el 80% de las visitas en una semana sin que haya cambiado nada en
ella: cambio quien gana.

El caso que motivo este modulo: una ficha de catalogo de marca propia
—Biobella— con las visitas derrumbadas. En una ficha de marca propia el unico
que deberia estar es el dueño de la marca, asi que si aparece otro vendedor no
es competencia normal, es alguien revendiendo o colgandose de la ficha. Eso hay
que verlo el dia que pasa, no cuando se nota en la facturacion.

El sistema ya consultaba price_to_win, que dice si ganamos y cuanto falta, pero
no quien esta del otro lado. Sin eso no se puede decidir: no es lo mismo perder
contra un vendedor grande con mejor logistica que contra alguien que aparecio
ayer con tres unidades.
"""

from __future__ import annotations

import logging

import requests

_logger = logging.getLogger(__name__)

_ML = 'https://api.mercadolibre.com'

# Cuantas unidades vendidas separan a un vendedor establecido de uno que recien
# aparece. No es una regla de ML, es un corte para poder decir algo util.
VENTAS_VENDEDOR_NUEVO = 50


def _get(url: str, heads: dict, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(url, headers=heads, params=params or {}, timeout=10)
        if r.ok:
            return r.json()
        _logger.warning('[catalogo] %s respondio %s', url, r.status_code)
    except requests.RequestException as e:
        _logger.warning('[catalogo] %s fallo: %s', url, e)
    return None


def competidores_de(item_id: str, heads: dict, mi_user_id=None) -> dict:
    """Todos los que venden sobre la misma ficha de catalogo que mi publicacion."""
    mio = _get(f'{_ML}/items/{item_id}', heads)
    if not mio:
        return {'ok': False, 'error': 'no pude leer la publicacion'}

    cpid = mio.get('catalog_product_id')
    if not cpid:
        return {'ok': False, 'error': 'esta publicacion no es de catalogo',
                'es_catalogo': False}

    prod = _get(f'{_ML}/products/{cpid}', heads) or {}
    listado = _get(f'{_ML}/products/{cpid}/items', heads) or {}

    ganador_id = ((prod.get('buy_box_winner') or {}).get('item_id') or '')
    marca = ''
    for a in (prod.get('attributes') or []):
        if a.get('id') in ('BRAND', 'MANUFACTURER'):
            marca = a.get('value_name') or ''
            break

    vendedores = []
    for r in (listado.get('results') or []):
        sid = r.get('seller_id')
        vendedores.append({
            'item_id':       r.get('item_id'),
            'seller_id':     sid,
            'es_mio':        (str(sid) == str(mi_user_id)) if mi_user_id else None,
            'precio':        r.get('price'),
            'envio_gratis':  bool((r.get('shipping') or {}).get('free_shipping')),
            'logistica':     (r.get('shipping') or {}).get('logistic_type') or '',
            'vendidos':      r.get('sold_quantity'),
            'stock':         r.get('available_quantity'),
            'condicion':     r.get('condition'),
            'gana_buy_box':  r.get('item_id') == ganador_id,
        })
    vendedores.sort(key=lambda v: (not v['gana_buy_box'], v.get('precio') or 0))

    return {
        'ok': True,
        'es_catalogo': True,
        'item_id': item_id,
        'catalog_product_id': cpid,
        'producto': prod.get('name') or '',
        'marca_de_la_ficha': marca,
        'mi_precio': mio.get('price'),
        'mi_stock': mio.get('available_quantity'),
        'mi_status': mio.get('status'),
        'ganador_id': ganador_id,
        'gano_yo': ganador_id == item_id,
        'total_vendedores': (listado.get('paging') or {}).get('total', len(vendedores)),
        'vendedores': vendedores,
    }


def diagnosticar(datos: dict, marca_propia: str = '') -> dict:
    """Por que no gano la ficha, y si hay alguien que no deberia estar ahi."""
    if not datos.get('ok') or not datos.get('es_catalogo'):
        return {'hallazgos': [], 'gravedad': 'ninguna'}

    hallazgos = []
    mios   = [v for v in datos['vendedores'] if v.get('es_mio')]
    ajenos = [v for v in datos['vendedores'] if v.get('es_mio') is False]
    yo = mios[0] if mios else None

    # 1. Ficha de marca propia con vendedores ajenos: no es competencia normal
    marca_ficha = (datos.get('marca_de_la_ficha') or '').strip().lower()
    propia = (marca_propia or '').strip().lower()
    if propia and marca_ficha and propia in marca_ficha and ajenos:
        hallazgos.append({
            'tipo': 'intruso_en_marca_propia',
            'gravedad': 'critica',
            'texto': (f'{len(ajenos)} vendedor(es) publicando sobre tu ficha de '
                      f'{datos["marca_de_la_ficha"]}, que es tu marca. En una ficha de '
                      f'marca propia no deberia haber nadie mas: o estan revendiendo, '
                      f'o se colgaron de la ficha.'),
            'vendedores': [v['seller_id'] for v in ajenos],
        })

    # 2. Perdimos la buy box: decir contra quien y por que
    if yo and not datos.get('gano_yo'):
        ganador = next((v for v in datos['vendedores'] if v['gana_buy_box']), None)
        if ganador:
            motivos = []
            if (ganador.get('precio') or 0) < (yo.get('precio') or 0):
                dif = (yo['precio'] or 0) - (ganador['precio'] or 0)
                motivos.append(f'esta ${dif:,.0f} mas barato'.replace(',', '.'))
            if ganador.get('logistica') == 'fulfillment' and yo.get('logistica') != 'fulfillment':
                motivos.append('esta en Full y vos no')
            if ganador.get('envio_gratis') and not yo.get('envio_gratis'):
                motivos.append('tiene envio gratis y vos no')
            if not motivos:
                motivos.append('no es por precio ni por envio — mira reputacion '
                               'y tiempo de despacho')
            nuevo = (ganador.get('vendidos') or 0) < VENTAS_VENDEDOR_NUEVO
            hallazgos.append({
                'tipo': 'buy_box_perdida',
                'gravedad': 'alta',
                'texto': (f'La gana {ganador["item_id"]}'
                          + (' (vendedor nuevo, pocas ventas)' if nuevo else '')
                          + ': ' + ', '.join(motivos) + '.'),
                'ganador': ganador,
            })
        else:
            hallazgos.append({
                'tipo': 'buy_box_sin_ganador_visible',
                'gravedad': 'media',
                'texto': 'No ganas la ficha y ML no expone quien la gana.',
            })

    # 3. Sin stock no se puede ganar nada
    if yo and not (yo.get('stock') or 0):
        hallazgos.append({
            'tipo': 'sin_stock',
            'gravedad': 'critica',
            'texto': 'Tu publicacion esta sin stock: la buy box no se pierde por '
                     'precio, se pierde porque no hay que vender.',
        })

    orden = {'critica': 0, 'alta': 1, 'media': 2}
    hallazgos.sort(key=lambda h: orden.get(h['gravedad'], 9))
    return {
        'hallazgos': hallazgos,
        'gravedad': hallazgos[0]['gravedad'] if hallazgos else 'ninguna',
    }
