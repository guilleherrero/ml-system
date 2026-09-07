"""
Cerebro — captura del snapshot diario enriquecido (bloques 1.4 y 4.1).

Es la unica pieza de Cerebro que le pega a la API de ML. Corre una vez por dia
por cuenta (job `daily_snapshots`, 04:00 ART) y deja en
`data/cerebro_<Alias>/snapshots.json` una fila por publicacion y por dia:

    visitas del dia, clics de Product Ads, VISITAS ORGANICAS (= totales - ads),
    unidades vendidas del dia, conversion organica y conversion Ads por
    separado, precio, stock, posicion, rating, preguntas sin responder, y por
    cada competidor directo confirmado su precio, cantidad y status.

Por que importa la separacion organico/pago: las visitas de una publicacion
pautada no son organicas. Si no se separan, el sistema le atribuye a una ficha
o a una descripcion lo que en realidad compro la pauta, y aprende una mentira.

Honestidad del dato de Ads: la API de ML NO expone metricas por item, solo por
campania. Cuando la campania tiene un solo item, los clics son exactos
(`ads_scope='item'`). Cuando tiene varios, se prorratean por impresiones
disponibles y queda marcado `ads_scope='campaign'` para que la evaluacion sepa
que ese numero es una estimacion.

El snapshot es SIEMPRE del dia anterior completo (el job corre de madrugada).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

from modules import cerebro

_logger = logging.getLogger(__name__)

_ML = 'https://api.mercadolibre.com'
_TZ = '-03:00'
_TIMEOUT = 10
_LOTE_VISITAS = 50
_PAUSA = 0.12


def _dia_objetivo(dia: str | None = None) -> str:
    if dia:
        return dia
    return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')


def _heads(token: str) -> dict:
    return {'Authorization': f'Bearer {token}'}


# ── Items activos ────────────────────────────────────────────────────────────

def _items_activos(token: str, user_id) -> list[str]:
    """Todos los MLA activos del vendedor, paginado con scan (sin cap)."""
    ids: list[str] = []
    scroll = None
    for _ in range(60):   # techo defensivo: 60 * 100 = 6000 publicaciones
        params = {'search_type': 'scan', 'limit': 100, 'status': 'active'}
        if scroll:
            params['scroll_id'] = scroll
        try:
            r = requests.get(f'{_ML}/users/{user_id}/items/search',
                             headers=_heads(token), params=params, timeout=_TIMEOUT)
            if not r.ok:
                break
            body = r.json()
        except requests.RequestException as e:
            _logger.warning('[cerebro_snap] items/search fallo: %s', e)
            break
        results = body.get('results') or []
        ids.extend(results)
        scroll = body.get('scroll_id')
        if not scroll or not results:
            break
        time.sleep(_PAUSA)
    return ids


def _detalle_items(token: str, item_ids: list[str]) -> dict[str, dict]:
    """Multiget /items?ids= en lotes de 20 (limite de ML)."""
    out: dict[str, dict] = {}
    attrs = ('id,title,price,available_quantity,status,category_id,'
             'catalog_product_id,catalog_listing,sold_quantity,listing_type_id,'
             'shipping,pictures')
    for i in range(0, len(item_ids), 20):
        lote = item_ids[i:i + 20]
        try:
            r = requests.get(f'{_ML}/items', headers=_heads(token),
                             params={'ids': ','.join(lote), 'attributes': attrs},
                             timeout=_TIMEOUT)
            if not r.ok:
                continue
            for row in r.json() or []:
                body = row.get('body') or {}
                if body.get('id'):
                    out[body['id']] = body
        except (requests.RequestException, ValueError) as e:
            _logger.warning('[cerebro_snap] multiget items fallo: %s', e)
        time.sleep(_PAUSA)
    return out


# ── Visitas del dia ──────────────────────────────────────────────────────────

def _visitas_dia(token: str, item_ids: list[str], dia: str) -> dict[str, float]:
    """Visitas por item para UN dia, via multiget /items/visits."""
    desde = f'{dia}T00:00:00.000{_TZ}'
    hasta = f'{dia}T23:59:59.000{_TZ}'
    out: dict[str, float] = {}
    for i in range(0, len(item_ids), _LOTE_VISITAS):
        lote = item_ids[i:i + _LOTE_VISITAS]
        try:
            r = requests.get(f'{_ML}/items/visits', headers=_heads(token),
                             params={'ids': ','.join(lote),
                                     'date_from': desde, 'date_to': hasta},
                             timeout=_TIMEOUT)
            if not r.ok:
                continue
            body = r.json()
            if isinstance(body, dict):
                for iid, val in body.items():
                    if isinstance(val, dict):
                        out[iid] = float(val.get('total_visits') or 0)
                    else:
                        out[iid] = float(val or 0)
            elif isinstance(body, list):
                for row in body:
                    iid = row.get('item_id') or row.get('id')
                    if iid:
                        out[iid] = float(row.get('total_visits') or 0)
        except (requests.RequestException, ValueError, TypeError) as e:
            _logger.warning('[cerebro_snap] visitas lote fallo: %s', e)
        time.sleep(_PAUSA)
    return out


# ── Unidades vendidas del dia ────────────────────────────────────────────────

def _unidades_dia(token: str, user_id, dia: str) -> dict[str, float]:
    """Unidades pagadas por item para UN dia, desde /orders/search."""
    desde = f'{dia}T00:00:00.000{_TZ}'
    hasta = f'{dia}T23:59:59.000{_TZ}'
    out: dict[str, float] = {}
    offset = 0
    for _ in range(20):   # techo: 20 * 50 = 1000 ordenes en un dia
        try:
            r = requests.get(f'{_ML}/orders/search', headers=_heads(token),
                             params={'seller': user_id,
                                     'order.date_created.from': desde,
                                     'order.date_created.to': hasta,
                                     'order.status': 'paid',
                                     'limit': 50, 'offset': offset},
                             timeout=_TIMEOUT)
            if not r.ok:
                break
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            _logger.warning('[cerebro_snap] orders/search fallo: %s', e)
            break
        results = body.get('results') or []
        for orden in results:
            for oi in orden.get('order_items') or []:
                iid = ((oi.get('item') or {}).get('id'))
                if iid:
                    out[iid] = out.get(iid, 0.0) + float(oi.get('quantity') or 0)
        total = (body.get('paging') or {}).get('total', 0)
        offset += 50
        if offset >= total or not results:
            break
        time.sleep(_PAUSA)
    return out


# ── Clics de Product Ads del dia ─────────────────────────────────────────────

def _clics_ads_dia(token: str, user_id, dia: str) -> dict[str, dict]:
    """Clics de Ads por item. Devuelve {item_id: {clics, costo, ventas, scope}}.

    La API de ML da metricas por campania, no por item. Con un solo item en la
    campania el dato es exacto; con varios se prorratea y se marca como tal.
    """
    out: dict[str, dict] = {}
    try:
        from modules import meli_ads_engine as ads
    except Exception as e:
        _logger.warning('[cerebro_snap] meli_ads_engine no disponible: %s', e)
        return out

    try:
        campaign_map = ads._discover_campaign_ids(token, user_id)
    except Exception as e:
        _logger.warning('[cerebro_snap] discover campanias fallo: %s', e)
        return out
    if not campaign_map:
        return out

    for camp_id, item_ids in campaign_map.items():
        item_ids = [i for i in (item_ids or []) if i]
        if not item_ids:
            continue
        try:
            r = ads._ads_get(f'/advertising/product_ads/campaigns/{camp_id}/metrics',
                             token, params={'date_from': dia, 'date_to': dia})
        except Exception as e:
            _logger.warning('[cerebro_snap] metricas campania %s: %s', camp_id, e)
            continue
        if not r.get('ok') or not r.get('data'):
            continue
        body = r['data'] or {}
        clics  = float(body.get('clicks') or 0)
        costo  = float(body.get('cost') or 0)
        ventas = float(body.get('sold_quantity_total') or 0)
        impres = float(body.get('impressions') or 0)

        exacto = len(item_ids) == 1
        n = len(item_ids)
        for iid in item_ids:
            out[iid] = {
                'clics':       clics if exacto else round(clics / n, 2),
                'costo':       costo if exacto else round(costo / n, 2),
                'ventas':      ventas if exacto else round(ventas / n, 3),
                'impresiones': impres if exacto else round(impres / n, 2),
                'ads_scope':   'item' if exacto else 'campaign',
                'campaign_id': camp_id,
            }
        time.sleep(_PAUSA)
    return out


# ── Competidores directos confirmados ────────────────────────────────────────

def _snapshot_competidores(token: str, alias: str, item_id: str) -> list[dict]:
    """Precio, cantidad y status de cada competidor directo confirmado (1.4)."""
    directos = cerebro.competidores_para_precio(alias, item_id)
    filas = []
    for c in directos:
        cid = c.get('id')
        if not cid:
            continue
        fila = {'id': cid, 'seller': c.get('seller'),
                'precio': c.get('price'), 'estado_stock': c.get('estado_stock')}
        try:
            r = requests.get(f'{_ML}/items/{cid}', headers=_heads(token),
                             params={'attributes': 'id,price,available_quantity,status'},
                             timeout=6)
            if r.ok:
                b = r.json()
                fila['precio'] = b.get('price')
                fila['available_quantity'] = b.get('available_quantity')
                fila['status'] = b.get('status')
                # Refrescar el registro persistido (alimenta historial y liquidando)
                cerebro.guardar_competidor(
                    alias,
                    {**{k: c.get(k) for k in ('id', 'title', 'seller', 'seller_id',
                                              'catalog_product_id', 'attributes',
                                              'permalink', 'thumbnail')},
                     'price': b.get('price'),
                     'available_quantity': b.get('available_quantity'),
                     'status': b.get('status')},
                    item_propio=item_id, clase=c.get('clase'),
                    puntaje=c.get('puntaje'), origen=c.get('origen') or 'snapshot')
                fila['estado_stock'] = cerebro._estado_stock(
                    {'status': b.get('status'),
                     'available_quantity': b.get('available_quantity')})
        except (requests.RequestException, ValueError) as e:
            _logger.debug('[cerebro_snap] competidor %s: %s', cid, e)
        filas.append(fila)
        time.sleep(0.08)
    return filas


# ── Captura completa de una cuenta ───────────────────────────────────────────

def capturar_cuenta(client, alias: str, dia: str | None = None,
                    posiciones: dict | None = None,
                    preguntas: dict | None = None,
                    max_items: int | None = None) -> dict:
    """Captura el snapshot diario de TODAS las publicaciones activas de la cuenta.

    `posiciones` y `preguntas` son los JSON locales ya cargados por el llamador
    (evita releerlos por item). Devuelve un resumen para el log del job.
    """
    dia = _dia_objetivo(dia)
    client._ensure_token()
    token = client.account.access_token
    user_id = client.account.user_id

    item_ids = _items_activos(token, user_id)
    if max_items:
        item_ids = item_ids[:max_items]
    if not item_ids:
        return {'alias': alias, 'dia': dia, 'items': 0, 'motivo': 'sin items activos'}

    detalles = _detalle_items(token, item_ids)
    visitas  = _visitas_dia(token, item_ids, dia)
    unidades = _unidades_dia(token, user_id, dia)
    ads_por_item = _clics_ads_dia(token, user_id, dia)

    posiciones = posiciones or {}
    preguntas_por_item: dict[str, int] = {}
    for q in ((preguntas or {}).get('preguntas') or []):
        iid = q.get('item_id')
        if iid:
            preguntas_por_item[iid] = preguntas_por_item.get(iid, 0) + 1

    snaps: dict[str, dict] = {}
    con_ads = 0
    for iid in item_ids:
        d = detalles.get(iid, {})
        ads_row = ads_por_item.get(iid) or {}
        if ads_row:
            con_ads += 1

        posicion = None
        pos_hist = (posiciones.get(iid) or {}).get('history') or {}
        if pos_hist:
            val = pos_hist[max(pos_hist.keys())]
            if val != 999:
                posicion = val

        snap = cerebro.construir_snapshot(
            fecha=dia,
            visitas_totales=visitas.get(iid, 0),
            clics_ads=ads_row.get('clics', 0),
            ads={'impresiones': ads_row.get('impresiones'),
                 'costo': ads_row.get('costo'),
                 'ventas': ads_row.get('ventas')} if ads_row else None,
            unidades=unidades.get(iid, 0),
            precio=d.get('price'),
            stock=d.get('available_quantity'),
            posicion=posicion,
            status=d.get('status'),
            categoria=d.get('category_id'),
            preguntas_sin_responder=preguntas_por_item.get(iid, 0),
            extra={
                'catalog_product_id': d.get('catalog_product_id'),
                'catalog_listing':    d.get('catalog_listing'),
                'listing_type':       d.get('listing_type_id'),
                'fotos':              len(d.get('pictures') or []) or None,
                'ads_scope':          ads_row.get('ads_scope'),
                'ventas_acumuladas':  d.get('sold_quantity'),
            },
        )

        comps = _snapshot_competidores(token, alias, iid)
        if comps:
            snap['competidores'] = comps
        snaps[iid] = snap

    guardados = cerebro.guardar_snapshots_batch(alias, snaps)
    return {
        'alias': alias, 'dia': dia,
        'items': len(item_ids), 'guardados': guardados,
        'con_visitas': sum(1 for v in visitas.values() if v),
        'con_ventas': len(unidades), 'con_ads': con_ads,
    }
