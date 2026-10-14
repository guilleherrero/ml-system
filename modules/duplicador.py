"""
Duplicador de publicaciones — subseccion A: duplicar con calculadora.

Caso de uso (Guille, 2026-10-14): partiendo de la Pub 1 (Clasica sin cuotas),
crear como BORRADOR
  Pub 2 = Premium con cuotas sin interes (escalon 3/6/9/12, lo elige el usuario)
  Pub 3 = Clasica con interes bajo (pcj-co-funded)
con precio calculado por ROI sobre el costo, nunca debajo del 15% de margen,
y que nada se publique hasta que el usuario apruebe cada borrador.

Como se crea en ML (investigado en la doc oficial, ver docs/CEREBRO.md
correccion 56): las cuentas son User Product, y un mismo UP admite hasta 30
"condiciones de venta" (items). La copia se crea con
`POST /user-products/{UP}/items`, que solo recibe precio, tipo y tags: fotos,
ficha, titulo y stock salen del UP. No se copia ni se altera ninguna imagen,
y la copia es el mismo producto con otra condicion de venta, que es lo que ML
permite. La descripcion NO se comparte entre items, se copia aparte.

Modulo dedicado: no modifica nada existente. Lee de `precio_motor`
(calculo), `trio_generador` (tags por escalon y tasas reales),
`precio_variantes` (escalon de un item), `stock_rentabilidad` (costos) y
`PricingConfig` (cargos de la cuenta), sin escribir en ninguno.
"""
import uuid
from datetime import datetime

from core.db_storage import db_load, db_save
from modules import precio_motor as pm
from modules.precio_variantes import escalon_de, etiqueta_escalon
from modules.trio_generador import CUOTAS_A_TAGS

ESCALONES_PUB2 = (3, 6, 9, 12)      # Premium con cuotas sin interes
ESCALON_PUB3 = 4                    # Clasica con interes bajo (pcj-co-funded)
ROI_OBJETIVO_DEFAULT = 120.0
MAX_ITEMS_POR_UP = 30               # limite documentado por ML
MARGEN_MINIMO = pm.MARGEN_MINIMO_ACEPTABLE


class DuplicadorError(Exception):
    """Error con un mensaje que se puede mostrar tal cual al usuario."""


def _safe(alias: str) -> str:
    return (alias or '').replace(' ', '_').replace('/', '-')


def _path(alias: str) -> str:
    return f'data/duplicador_{_safe(alias)}.json'


def _cargar(alias: str) -> dict:
    data = db_load(_path(alias)) or {}
    data.setdefault('borradores', {})
    data.setdefault('relaciones', [])
    return data


def _guardar(alias: str, data: dict):
    db_save(_path(alias), data)


# ── Calculo (puro, testeable sin ML) ─────────────────────────────────────────

def perfiles(escalon_pub2: int) -> list[dict]:
    if escalon_pub2 not in ESCALONES_PUB2:
        raise DuplicadorError('Elegí 3, 6, 9 o 12 cuotas sin interés para la Pub 2.')
    return [
        {'perfil': 'pub2', 'nombre': 'Pub 2 · Premium', 'escalon': escalon_pub2},
        {'perfil': 'pub3', 'nombre': 'Pub 3 · Clásica interés bajo', 'escalon': ESCALON_PUB3},
    ]


def _publicacion(escalon: int, tasas: dict) -> pm.Publicacion:
    t = tasas.get(escalon) or {}
    if t.get('comision') is None or t.get('costo_cuotas') is None:
        raise DuplicadorError(
            f'ML no devolvió la comisión real para "{etiqueta_escalon(escalon)}" en esta categoría. '
            'No se calcula con un número inventado.')
    lt, _ = CUOTAS_A_TAGS[escalon]
    return pm.Publicacion(nombre=etiqueta_escalon(escalon), comision=float(t['comision']),
                          costo_cuotas=float(t['costo_cuotas']), listing_type=lt, cuotas=escalon)


def evaluar_precio(precio: float, escalon: int, tasas: dict, cargos: pm.Cargos, costo: float) -> dict:
    """Ganancia, margen y ROI de vender a `precio` con ese escalon."""
    pub = _publicacion(escalon, tasas)
    d = pm.desglose_costos(precio, pub, cargos, costo)
    g = d['ganancia']
    return {
        'precio': round(precio, 2),
        'ganancia': g,
        'margen_pct': round(g / precio * 100, 2) if precio else None,
        'roi_pct': round(g / costo * 100, 2) if costo else None,
        'cumple_piso': precio > 0 and g / precio >= MARGEN_MINIMO,
        'desglose': d,
    }


def calcular(costo, cargos: pm.Cargos, tasas: dict, escalon_pub2: int,
             roi_objetivo: float = ROI_OBJETIVO_DEFAULT, precios_editados: dict | None = None) -> list[dict]:
    """Precio sugerido por ROI y, si el usuario lo edito, el precio editado.

    Sin costo no se calcula nada (criterio de CEREBRO: sin costo no hay margen).
    """
    if costo in (None, '', 0) or float(costo) <= 0:
        raise DuplicadorError('Falta el costo del producto. Cargalo en Costos: sin costo no se calcula el precio.')
    costo = float(costo)
    if roi_objetivo is None or float(roi_objetivo) <= 0:
        raise DuplicadorError('El ROI objetivo tiene que ser mayor a 0%.')
    obj = pm.Objetivo(modo='roi', objetivo=float(roi_objetivo), piso=float(roi_objetivo))
    precios_editados = precios_editados or {}

    out = []
    for p in perfiles(escalon_pub2):
        pub = _publicacion(p['escalon'], tasas)
        sugerido = pm.redondear_arriba(pm.precio_objetivo(pub, cargos, costo, obj), cargos)
        editado = precios_editados.get(p['perfil'])
        precio = float(editado) if editado not in (None, '') else sugerido
        if precio <= 0:
            raise DuplicadorError(f'{p["nombre"]}: el precio tiene que ser mayor a $0.')
        ev = evaluar_precio(precio, p['escalon'], tasas, cargos, costo)
        lt, tags = CUOTAS_A_TAGS[p['escalon']]
        out.append({**p, **ev, 'precio_sugerido': sugerido, 'editado': editado not in (None, ''),
                    'cuotas': etiqueta_escalon(p['escalon']), 'listing_type_id': lt, 'tags': tags})
    return out


def condicion_ocupada(existentes: list[dict], escalon: int) -> dict | None:
    """El item del mismo UP que ya tiene esa condicion de venta, si hay uno.
    Mira lo que existe en ML de verdad, asi que tambien detecta copias hechas
    a mano o desde otra herramienta (UpSeller)."""
    return next((e for e in existentes if e.get('escalon') == escalon), None)


# ── Lectura de ML ────────────────────────────────────────────────────────────

_ATRIBUTOS = 'id,title,price,status,user_product_id,family_id,listing_type_id,tags,category_id,domain_id,currency_id,catalog_listing,seller_id,sale_terms'


def items_del_up(client, seller_id, user_product_id: str) -> list[dict]:
    """Items activos y pausados del mismo User Product, en vivo."""
    ids = []
    for status in ('active', 'paused'):
        params = {'user_product_id': user_product_id, 'status': status}
        for intento in (1, 2):   # ML devuelve 500 de forma intermitente en este search (visto 2026-10-14)
            try:
                data = client._get(f'/users/{seller_id}/items/search', params=params) or {}
                break
            except Exception as e:
                if intento == 2:
                    raise DuplicadorError(f'ML no respondió al buscar las publicaciones de este producto ({e}). '
                                          'Sin esa verificación no se sigue: probá de nuevo en un rato.')
        ids += [i for i in data.get('results') or [] if i not in ids]
    out = []
    for i in range(0, len(ids), 20):
        entries = client._get('/items', params={'ids': ','.join(ids[i:i + 20]), 'attributes': _ATRIBUTOS}) or []
        for e in entries:
            b = e.get('body') or {}
            if e.get('code') == 200 and b.get('user_product_id') == user_product_id:
                esc = escalon_de(b.get('listing_type_id'), b.get('tags'))
                out.append({'id': b['id'], 'precio': float(b.get('price') or 0), 'status': b.get('status'),
                            'listing_type_id': b.get('listing_type_id'), 'escalon': esc,
                            'cuotas': etiqueta_escalon(esc), 'catalog_listing': bool(b.get('catalog_listing'))})
    return out


def seller_id(client):
    if not client.account.user_id:
        client.account.user_id = (client.get_me() or {}).get('id')
    return client.account.user_id


def leer_origen(client, item_id: str) -> dict:
    item = client.get_item(item_id)
    if not item or 'id' not in item:
        raise DuplicadorError(f'No se encontró la publicación {item_id}.')
    if str(item.get('seller_id')) != str(seller_id(client)):
        raise DuplicadorError(f'{item_id} no es una publicación de esta cuenta.')
    if not item.get('user_product_id'):
        raise DuplicadorError(f'{item_id} no tiene User Product: este duplicador solo trabaja con el modelo User Products de ML.')
    if item.get('catalog_listing'):
        raise DuplicadorError(f'{item_id} es una publicación de catálogo: elegí la publicación propia (no de catálogo).')
    return item


# ── Borradores ───────────────────────────────────────────────────────────────

def crear_borradores(alias: str, item_origen: dict, filas: list[dict], costo: float, roi_objetivo: float,
                     usuario: str = '') -> list[dict]:
    """Guarda un borrador por fila calculada. No escribe nada en ML."""
    for f in filas:
        if not f.get('cumple_piso'):
            raise DuplicadorError(f'{f["nombre"]}: a ${f["precio"]:,.0f} el margen queda en {f["margen_pct"]}%, '
                                  f'debajo del piso de {MARGEN_MINIMO * 100:.0f}%. No se guarda.')
    data = _cargar(alias)
    ahora = datetime.now().isoformat(timespec='seconds')
    nuevos = []
    for f in filas:
        pendiente = next((b for b in data['borradores'].values()
                          if b['item_origen'] == item_origen['id'] and b['escalon'] == f['escalon']
                          and b['estado'] == 'pendiente'), None)
        if pendiente:
            raise DuplicadorError(f'Ya hay un borrador pendiente de {f["cuotas"]} para {item_origen["id"]}. '
                                  'Aprobalo o descartalo antes de crear otro.')
        bid = uuid.uuid4().hex[:10]
        b = {'id': bid, 'estado': 'pendiente', 'creado': ahora, 'creado_por': usuario,
             'item_origen': item_origen['id'], 'titulo_origen': item_origen.get('title', ''),
             'user_product_id': item_origen['user_product_id'],
             'perfil': f['perfil'], 'nombre': f['nombre'], 'escalon': f['escalon'], 'cuotas': f['cuotas'],
             'listing_type_id': f['listing_type_id'], 'tags': f['tags'],
             'precio': f['precio'], 'precio_sugerido': f['precio_sugerido'],
             'costo': costo, 'roi_objetivo': roi_objetivo,
             'margen_pct': f['margen_pct'], 'roi_pct': f['roi_pct'], 'ganancia': f['ganancia']}
        data['borradores'][bid] = b
        nuevos.append(b)
    _guardar(alias, data)
    return nuevos


def listar_borradores(alias: str) -> list[dict]:
    return sorted(_cargar(alias)['borradores'].values(), key=lambda b: b['creado'], reverse=True)


def listar_relaciones(alias: str) -> list[dict]:
    return _cargar(alias)['relaciones']


def descartar(alias: str, bid: str, usuario: str = '') -> dict:
    data = _cargar(alias)
    b = data['borradores'].get(bid)
    if not b:
        raise DuplicadorError('Borrador no encontrado.')
    if b['estado'] != 'pendiente':
        raise DuplicadorError(f'El borrador ya está {b["estado"]}.')
    b.update(estado='descartado', descartado=datetime.now().isoformat(timespec='seconds'), descartado_por=usuario)
    _guardar(alias, data)
    return b


def aprobar(alias: str, bid: str, client, cargos: pm.Cargos, tasas: dict, costo_actual,
            usuario: str = '') -> dict:
    """Crea la copia en ML. Vuelve a validar TODO con datos en vivo antes de
    escribir, porque entre el borrador y la aprobacion pueden haber cambiado
    el costo, las tasas o las publicaciones del UP."""
    data = _cargar(alias)
    b = data['borradores'].get(bid)
    if not b:
        raise DuplicadorError('Borrador no encontrado.')
    if b['estado'] != 'pendiente':
        raise DuplicadorError(f'El borrador ya está {b["estado"]}: no se crea dos veces.')

    origen = leer_origen(client, b['item_origen'])
    if origen['user_product_id'] != b['user_product_id']:
        raise DuplicadorError('La publicación original cambió de User Product desde que se armó el borrador. Descartalo y armalo de nuevo.')

    existentes = items_del_up(client, seller_id(client), b['user_product_id'])
    ocupada = condicion_ocupada(existentes, b['escalon'])
    if ocupada:
        raise DuplicadorError(f'Ese producto ya tiene una publicación con {b["cuotas"]}: {ocupada["id"]} '
                              f'({ocupada["status"]}). No se duplica dos veces.')
    if len(existentes) >= MAX_ITEMS_POR_UP:
        raise DuplicadorError(f'Ese producto ya tiene {len(existentes)} publicaciones; ML permite hasta {MAX_ITEMS_POR_UP}.')

    if costo_actual in (None, '', 0):
        raise DuplicadorError('El producto ya no tiene costo cargado. No se crea sin poder verificar el margen.')
    ev = evaluar_precio(b['precio'], b['escalon'], tasas, cargos, float(costo_actual))
    if not ev['cumple_piso']:
        raise DuplicadorError(f'Con el costo y las comisiones de hoy, a ${b["precio"]:,.0f} el margen queda en '
                              f'{ev["margen_pct"]}% (piso {MARGEN_MINIMO * 100:.0f}%). No se crea.')

    payload = {'price': b['precio'], 'category_id': origen['category_id'],
               'currency_id': origen.get('currency_id', 'ARS'), 'buying_mode': 'buy_it_now',
               'listing_type_id': b['listing_type_id']}
    if b['tags']:
        payload['tags'] = list(b['tags'])
    if origen.get('sale_terms'):
        payload['sale_terms'] = [{k: v for k, v in st.items() if k in ('id', 'value_id', 'value_name')}
                                 for st in origen['sale_terms']]
    try:
        creado = client._post(f'/user-products/{b["user_product_id"]}/items', payload)
    except Exception as e:
        raise DuplicadorError(f'ML rechazó la creación: {e}')
    nuevo_id = (creado or {}).get('id')
    if not nuevo_id:
        raise DuplicadorError(f'ML no devolvió el id de la publicación nueva. Respuesta: {creado}')

    # Desde aca la publicacion YA existe en ML: cualquier falla se informa
    # explicita y el borrador queda registrado como aplicado igual.
    avisos = []
    try:
        desc = client._get(f'/items/{origen["id"]}/description') or {}
        texto = desc.get('plain_text') or ''
        if texto:
            client._post(f'/items/{nuevo_id}/description', {'plain_text': texto})
        else:
            avisos.append('La publicación original no tiene descripción: la copia quedó sin descripción.')
    except Exception as e:
        avisos.append(f'No se pudo copiar la descripción ({e}). Cargala a mano en {nuevo_id}.')

    try:
        leido = client.get_item(nuevo_id) or {}
        esc_leido = escalon_de(leido.get('listing_type_id'), leido.get('tags'))
        if float(leido.get('price') or 0) != float(b['precio']):
            avisos.append(f'ML muestra precio ${leido.get("price")} en vez de ${b["precio"]}.')
        if esc_leido != b['escalon']:
            avisos.append(f'ML muestra "{etiqueta_escalon(esc_leido)}" en vez de "{b["cuotas"]}".')
        if leido.get('user_product_id') != b['user_product_id']:
            avisos.append('ML no la asoció al mismo User Product del original. Revisala antes de que venda.')
        estado_ml = leido.get('status')
    except Exception as e:
        estado_ml = None
        avisos.append(f'Se creó pero no se pudo releer para verificar ({e}).')

    ahora = datetime.now().isoformat(timespec='seconds')
    b.update(estado='aplicado', item_creado=nuevo_id, aprobado=ahora, aprobado_por=usuario,
             estado_ml=estado_ml, avisos=avisos)
    data['relaciones'].append({'item_origen': b['item_origen'], 'item_copia': nuevo_id,
                               'user_product_id': b['user_product_id'], 'escalon': b['escalon'],
                               'precio': b['precio'], 'creado': ahora, 'por': usuario, 'borrador': bid})
    try:
        _guardar(alias, data)
    except Exception as e:
        avisos.append(f'La publicación {nuevo_id} SÍ se creó en ML, pero no se pudo registrar acá ({e}). Anotala a mano.')
    return b


# ── Caso secundario: republicar una propia pausada o finalizada ─────────────

def republicables(client) -> dict:
    """Pausadas (se reactivan) y cerradas (se republican con relist)."""
    out = {}
    for status in ('paused', 'closed'):
        ids, offset = [], 0
        while True:
            data = client.get_my_listings(limit=50, offset=offset, status=status) or {}
            ids += data.get('results') or []
            offset += 50
            if offset >= (data.get('paging') or {}).get('total', 0) or offset >= 500:
                break
        filas = []
        for i in range(0, len(ids), 20):
            entries = client._get('/items', params={'ids': ','.join(ids[i:i + 20]),
                                                    'attributes': _ATRIBUTOS + ',available_quantity,stop_time,last_updated'}) or []
            for e in entries:
                b = e.get('body') or {}
                if e.get('code') == 200:
                    esc = escalon_de(b.get('listing_type_id'), b.get('tags'))
                    filas.append({'id': b['id'], 'titulo': b.get('title', ''), 'precio': float(b.get('price') or 0),
                                  'stock': b.get('available_quantity'), 'listing_type_id': b.get('listing_type_id'),
                                  'cuotas': etiqueta_escalon(esc), 'actualizado': b.get('last_updated'),
                                  'cerrada': b.get('stop_time')})
        out[status] = filas
    return out


def reactivar(client, item_id: str) -> dict:
    item = client.get_item(item_id) or {}
    if str(item.get('seller_id')) != str(seller_id(client)):
        raise DuplicadorError(f'{item_id} no es una publicación de esta cuenta.')
    if item.get('status') != 'paused':
        raise DuplicadorError(f'{item_id} no está pausada (está {item.get("status")}).')
    client.update_item(item_id, {'status': 'active'})
    leido = client.get_item(item_id) or {}
    return {'item_id': item_id, 'status': leido.get('status')}


def relist(client, item_id: str, precio: float, cantidad: int) -> dict:
    """Republica una finalizada. ML crea un item NUEVO (otro id) y, si pasaron
    menos de 60 dias, le transfiere visitas y ventas."""
    item = client.get_item(item_id) or {}
    if str(item.get('seller_id')) != str(seller_id(client)):
        raise DuplicadorError(f'{item_id} no es una publicación de esta cuenta.')
    if item.get('status') != 'closed':
        raise DuplicadorError(f'{item_id} no está finalizada (está {item.get("status")}). Si está pausada, reactivala.')
    if not precio or precio <= 0 or not cantidad or cantidad <= 0:
        raise DuplicadorError('Precio y cantidad tienen que ser mayores a 0.')
    try:
        nuevo = client._post(f'/items/{item_id}/relist', {'price': precio, 'quantity': int(cantidad),
                                                          'listing_type_id': item.get('listing_type_id')})
    except Exception as e:
        raise DuplicadorError(f'ML rechazó la republicación: {e}')
    return {'item_original': item_id, 'item_nuevo': (nuevo or {}).get('id'), 'status': (nuevo or {}).get('status')}
