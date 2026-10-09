"""
Promociones de Mercado Libre — recurso /seller-promotions (app_version=v2).

Todo lo que la pantalla /promociones/<alias> necesita:
  - listar las campañas a las que la cuenta está invitada
  - ver los ítems candidatos / adentro de cada una, con margen estimado
  - sumar y quitar ítems
  - crear una campaña propia (SELLER_CAMPAIGN) o descuentos individuales
    (PRICE_DISCOUNT)
  - plantillas: productos + % de descuento guardados para repetir una
    campaña que terminó (las de ML no se pueden clonar: las crea solo ML)

Contratos tomados de la documentación oficial (developers.mercadolibre.com.ar,
"Central de promociones" y una página por tipo, revisadas el 2026-10-07).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from core.db_storage import db_load, db_save
from core.ml_client import MLApiError
from modules.precio_motor import margen_pct

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
V2 = {'app_version': 'v2'}

# modo:
#   precio        → el vendedor define deal_price
#   precio_stock  → deal_price + stock reservado (relámpago)
#   aceptar       → el descuento lo fija ML; solo se acepta (promotion_id [+ offer_id])
#   no_soportado  → existe pero no se gestiona desde acá
TIPOS = {
    'DEAL':                   {'nombre': 'Campaña tradicional',      'modo': 'precio'},
    'MARKETPLACE_CAMPAIGN':   {'nombre': 'Co-fondeada por ML',       'modo': 'aceptar'},
    'VOLUME':                 {'nombre': 'Descuento por cantidad',   'modo': 'aceptar'},
    'PRE_NEGOTIATED':         {'nombre': 'Descuento pre-acordado',   'modo': 'aceptar'},
    'UNHEALTHY_STOCK':        {'nombre': 'Liquidación stock Full',   'modo': 'aceptar'},
    'SMART':                  {'nombre': 'Co-fondeada automatizada', 'modo': 'aceptar'},
    'PRICE_MATCHING':         {'nombre': 'Precios competitivos',     'modo': 'aceptar'},
    'DOD':                    {'nombre': 'Oferta del día',           'modo': 'precio'},
    'LIGHTNING':              {'nombre': 'Oferta relámpago',         'modo': 'precio_stock'},
    'SELLER_CAMPAIGN':        {'nombre': 'Campaña propia',           'modo': 'precio'},
    'PRICE_DISCOUNT':         {'nombre': 'Descuento individual',     'modo': 'precio'},
    'SELLER_COUPON_CAMPAIGN': {'nombre': 'Cupón del vendedor',       'modo': 'no_soportado'},
}

# Tipos que Mercado Libre permite crear al vendedor (y por lo tanto "clonar")
# Tipos que aceptan un precio aparte, más bajo, para compradores Meli+ (top_deal_price)
TIPOS_CON_MELI = ('DEAL', 'SELLER_CAMPAIGN', 'PRICE_DISCOUNT')
DIAS_MAX_PROPIA = 14            # SELLER_CAMPAIGN y PRICE_DISCOUNT: plazo máximo
DESCUENTO_MIN_PCT = 5           # PRICE_DISCOUNT: rango permitido por ML
DESCUENTO_MAX_PCT = 80
ESTADOS_ADENTRO = ('started', 'pending')


# Los IDs llegan del navegador y van dentro del path de la API: sin esto,
# "../../items/MLA1" haría que el panel llame a otro endpoint con el token.
_RE_ITEM = re.compile(r'^ML[A-Z]\d+$')
_RE_PROMO = re.compile(r'^[A-Za-z0-9_-]+$')


def _item(item_id) -> str:
    if not _RE_ITEM.match(str(item_id or '')):
        raise ValueError(f'ID de publicación inválido: {item_id!r}')
    return item_id


def _promo(promo_id) -> str:
    if not _RE_PROMO.match(str(promo_id or '')):
        raise ValueError(f'ID de promoción inválido: {promo_id!r}')
    return promo_id


def tipo_info(tipo: str) -> dict:
    return TIPOS.get(tipo, {'nombre': tipo, 'modo': 'no_soportado'})


def _uid(client) -> str:
    if not client.account.user_id:
        client.account.user_id = client.get_me()['id']
    return str(client.account.user_id)


# ── Lectura ──────────────────────────────────────────────────────────────────

def listar_promociones(client) -> list[dict]:
    """Todas las promociones a las que la cuenta está invitada o que creó."""
    uid = _uid(client)
    out, offset = [], 0
    while True:
        data = client._get(f'/seller-promotions/users/{uid}',
                           {**V2, 'offset': offset, 'limit': 50})
        res = data.get('results') or []
        for p in res:
            info = tipo_info(p.get('type'))
            out.append({**p, 'tipo_nombre': info['nombre'], 'modo': info['modo']})
        total = (data.get('paging') or {}).get('total') or 0
        offset += len(res)
        if not res or offset >= total:
            break
    return out


def items_de_promocion(client, promo_id: str, tipo: str, status: str | None = None,
                       max_items: int = 3000) -> list[dict]:
    """Ítems de una promoción. Pagina con search_after (TTL 5 min en ML)."""
    params = {**V2, 'promotion_type': tipo, 'limit': 50}
    if status:
        params['status'] = status
    out = []
    while len(out) < max_items:
        data = client._get(f'/seller-promotions/promotions/{_promo(promo_id)}/items', params)
        res = data.get('results') or []
        out.extend(res)
        sa = data.get('search_after') or data.get('searchAfter')
        if not res or not sa:
            break
        params['search_after'] = sa
    return out


def contar_items(client, promo_id: str, tipo: str, status: str) -> int | None:
    try:
        data = client._get(f'/seller-promotions/promotions/{_promo(promo_id)}/items',
                           {**V2, 'promotion_type': tipo, 'status': status, 'limit': 1})
    except (MLApiError, ValueError):
        return None
    total = (data.get('paging') or {}).get('total')
    return total if isinstance(total, int) else len(data.get('results') or [])


def detalles_items(client, ids: list[str]) -> dict:
    """{item_id: datos de la publicación} en lotes de 20 (multiget de /items)."""
    out = {}
    attrs = ('id,title,thumbnail,price,available_quantity,listing_type_id,'
             'seller_custom_field,permalink,status,catalog_listing')
    for i in range(0, len(ids), 20):
        lote = ids[i:i + 20]
        for e in client._get('/items', {'ids': ','.join(lote), 'attributes': attrs}) or []:
            b = e.get('body') or {}
            if b.get('id'):
                out[b['id']] = b
    return out


def mis_items_activos(client) -> list[dict]:
    """Publicaciones activas (para armar una campaña propia desde cero)."""
    from modules.monitor_posicionamiento import _get_all_active_items
    return [{'id': it['id'], 'status': 'candidate', 'price': 0,
             'original_price': it.get('price')}
            for it in _get_all_active_items(client)]


def promos_del_item(client, item_id: str) -> list[dict]:
    data = client._get(f'/seller-promotions/items/{_item(item_id)}', V2)
    return data if isinstance(data, list) else (data or {}).get('results') or []


LIMITES = ('min_discounted_price', 'max_discounted_price',
           'suggested_discounted_price', 'original_price')


def completar_limites(client, crudos: list[dict], tipo: str, max_items: int = 60) -> None:
    """Relámpago / oferta del día: la lista de la promo no trae el tope ni el
    sugerido de cada candidato (y su "price" es más alto de lo que ML acepta).
    Los toma de /seller-promotions/items/{id}, que sí los tiene."""
    for it in [c for c in crudos if c.get('status') == 'candidate'][:max_items]:
        try:
            p = next((x for x in promos_del_item(client, it['id']) if x.get('type') == tipo), None)
        except Exception:
            continue
        for k in LIMITES:
            if p and p.get(k):
                it[k] = p[k]


def promo_activa_del_item(client, item_id: str) -> dict | None:
    """La primera promoción en la que el ítem está adentro (activa o programada)."""
    for p in promos_del_item(client, item_id):
        if p.get('status') in ESTADOS_ADENTRO:
            return p
    return None


# ── Margen ───────────────────────────────────────────────────────────────────

def enriquecer(items: list[dict], detalles: dict, costos: dict, fee_rate_de) -> list[dict]:
    """Suma título, stock, SKU, costo y margen estimado con el precio de la promo.

    fee_rate_de(listing_type) -> tasa total de ML (comisión + IVA + envío),
    la misma que usa Mis Publicaciones.
    """
    out = []
    for it in items:
        iid = it.get('id')
        d = detalles.get(iid, {})
        original = float(it.get('original_price') or d.get('price') or 0)
        sugerido = (it.get('suggested_discounted_price')
                    or (it.get('price') if it.get('status') == 'candidate' else None)
                    or it.get('max_discounted_price'))
        precio_promo = float(it.get('price') or 0) or float(sugerido or 0) or None
        costo = (costos.get(iid) or {}).get('costo')
        fee = fee_rate_de(d.get('listing_type_id') or '')
        m_lista = margen_pct(original, costo, fee)
        # Co-fondeada: ML pone su parte; el vendedor cobra lista menos SU %
        if precio_promo and it.get('seller_percentage') is not None and original:
            precio_promo = original * (1 - float(it['seller_percentage']) / 100)
        m_promo = margen_pct(precio_promo, costo, fee) if precio_promo else None
        out.append({
            'id': iid,
            'titulo': d.get('title') or iid,
            'thumbnail': d.get('thumbnail'),
            'permalink': d.get('permalink'),
            'sku': d.get('seller_custom_field') or '',
            'stock': d.get('available_quantity'),
            'catalogo': bool(d.get('catalog_listing')),
            'status': it.get('status'),
            'offer_id': it.get('offer_id') or it.get('ref_id'),
            # Relámpago / oferta del día: ML asigna el horario por ítem y
            # exige que vuelva en el POST (sin esto: START_DATE cannot be null)
            'start_date': it.get('start_date'),
            'finish_date': it.get('finish_date'),
            'original_price': original,
            'price': float(it.get('price') or 0),
            'min_price': it.get('min_discounted_price'),
            'max_price': it.get('max_discounted_price'),
            'sugerido': sugerido,
            'stock_min': (it.get('stock') or {}).get('min'),
            'stock_max': (it.get('stock') or {}).get('max'),
            'meli_pct': it.get('meli_percentage'),
            'seller_pct': it.get('seller_percentage'),
            'costo': costo,
            'fee_rate': fee,
            'margen_lista_pct': _pct(m_lista),
            'margen_promo_pct': _pct(m_promo),
        })
    return out


def _pct(x):
    return None if x is None else round(x * 100, 1)


# ── Escritura ────────────────────────────────────────────────────────────────

def precio_con_descuento(original: float, descuento_pct: float) -> int:
    return int(round(float(original) * (1 - float(descuento_pct) / 100)))


def body_para_sumar(tipo: str, promo_id: str | None, *, deal_price=None,
                    stock=None, offer_id=None, desde=None, hasta=None,
                    start_date=None, finish_date=None, top_deal_price=None) -> dict:
    """Arma el body del POST /seller-promotions/items/{id} según el tipo.

    Lanza ValueError si falta algo que ML va a rechazar igual.
    """
    modo = tipo_info(tipo)['modo']
    if modo == 'no_soportado':
        raise ValueError(f'El tipo {tipo} no se gestiona desde el panel')
    body = {'promotion_type': tipo}
    if promo_id and tipo != 'PRICE_DISCOUNT':
        body['promotion_id'] = promo_id
    if modo == 'aceptar':
        if offer_id:
            body['offer_id'] = offer_id
        return body
    if not deal_price or float(deal_price) <= 0:
        raise ValueError('Falta el precio de la promoción')
    body['deal_price'] = round(float(deal_price), 2)
    if top_deal_price and tipo in TIPOS_CON_MELI:
        if float(top_deal_price) >= body['deal_price']:
            raise ValueError('El precio Meli+ tiene que ser menor que el de la promo')
        body['top_deal_price'] = round(float(top_deal_price), 2)
    if modo == 'precio_stock':
        if not stock or int(stock) <= 0:
            raise ValueError('La oferta relámpago necesita stock reservado')
        body['stock'] = int(stock)
    if start_date and finish_date and tipo != 'PRICE_DISCOUNT':
        body['start_date'], body['finish_date'] = start_date, finish_date
    if tipo == 'PRICE_DISCOUNT':
        if not (desde and hasta):
            raise ValueError('El descuento individual necesita fechas')
        body['start_date'] = f'{desde}T00:00:00'
        body['finish_date'] = f'{hasta}T00:00:00'
    return body


def sumar_item(client, item_id: str, body: dict) -> dict:
    """POST a ML. Reintenta una vez si el ítem está bloqueado (423)."""
    path = f'/seller-promotions/items/{_item(item_id)}?app_version=v2'
    try:
        return client._post(path, body)
    except MLApiError as e:
        if e.status_code != 423:
            raise
        time.sleep(2)
        return client._post(path, body)


def quitar_item(client, item_id: str, tipo: str, promo_id: str | None = None,
                offer_id: str | None = None):
    params = {**V2, 'promotion_type': tipo}
    if promo_id and tipo != 'PRICE_DISCOUNT':
        params['promotion_id'] = promo_id
    if offer_id:
        params['offer_id'] = offer_id
    return client._delete(f'/seller-promotions/items/{_item(item_id)}', params)


def validar_fechas(desde: str, hasta: str, hoy: date | None = None) -> tuple[date, date]:
    """Reglas de ML para campañas propias y descuentos individuales."""
    hoy = hoy or date.today()
    try:
        d = date.fromisoformat(desde)
        h = date.fromisoformat(hasta)
    except (TypeError, ValueError):
        raise ValueError('Fechas inválidas (formato AAAA-MM-DD)')
    if d < hoy:
        raise ValueError('La fecha de inicio no puede ser anterior a hoy')
    if h < d:
        raise ValueError('La fecha de fin no puede ser anterior a la de inicio')
    if (h - d).days + 1 > DIAS_MAX_PROPIA:
        raise ValueError(f'Mercado Libre permite como máximo {DIAS_MAX_PROPIA} días')
    return d, h


def crear_campana_propia(client, nombre: str, desde: str, hasta: str) -> dict:
    validar_fechas(desde, hasta)
    nombre = (nombre or '').strip()
    if not nombre:
        raise ValueError('La campaña necesita un nombre')
    return client._post('/seller-promotions/promotions?app_version=v2', {
        'promotion_type': 'SELLER_CAMPAIGN',
        'name': nombre[:60],
        'sub_type': 'FLEXIBLE_PERCENTAGE',
        'start_date': f'{desde}T00:00:00',
        'finish_date': f'{hasta}T00:00:00',
    })


def error_legible(e: Exception) -> str:
    """El mensaje de ML sin el ruido del wrapper."""
    txt = str(e)
    i = txt.find('{')
    if i >= 0:
        try:
            j = json.loads(txt[i:])
            causas = [c.get('error_message') or c.get('message') for c in (j.get('cause') or [])
                      if isinstance(c, dict)]
            return '; '.join(c for c in causas if c) or j.get('message') or txt
        except ValueError:
            pass
    return txt[:300]


# ── Plantillas ───────────────────────────────────────────────────────────────

def _plantillas_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f"promociones_plantillas_{alias.replace(' ', '_').replace('/', '-')}.json")


def cargar_plantillas(alias: str) -> list[dict]:
    return (db_load(_plantillas_path(alias)) or {}).get('plantillas', [])


def guardar_plantilla(alias: str, nombre: str, tipo_origen: str, items: list[dict],
                      promo_origen: str | None = None) -> dict:
    """items: [{id, titulo, descuento_pct}]. Se guarda el %, no el precio:
    al repetirla se calcula sobre el precio que tenga el producto ese día."""
    limpios = [{'id': i['id'], 'titulo': (i.get('titulo') or '')[:120],
                'descuento_pct': round(float(i.get('descuento_pct') or 0), 2)}
               for i in items if i.get('id')]
    if not limpios:
        raise ValueError('La plantilla no tiene productos')
    p = {'id': uuid.uuid4().hex[:10], 'nombre': (nombre or 'Sin nombre').strip()[:80],
         'tipo_origen': tipo_origen, 'promo_origen': promo_origen,
         'creada': datetime.now().strftime('%Y-%m-%d %H:%M'), 'items': limpios}
    todas = cargar_plantillas(alias)
    todas.insert(0, p)
    db_save(_plantillas_path(alias), {'plantillas': todas[:100]})
    return p


def borrar_plantilla(alias: str, plantilla_id: str) -> bool:
    todas = cargar_plantillas(alias)
    quedan = [p for p in todas if p['id'] != plantilla_id]
    db_save(_plantillas_path(alias), {'plantillas': quedan})
    return len(quedan) != len(todas)

