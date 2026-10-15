"""
Rutas del Duplicador de publicaciones (Blueprint).

Archivo aparte, igual que contabilidad_routes.py: el registro en app.py son
dos lineas y el `before_request` global protege estas rutas como al resto.

  /duplicar                          pantalla
  /duplicar/api/contexto             Pub 1 + costo + tasas reales + publicaciones del mismo UP
  /duplicar/api/calcular             precios de Pub 2 / Pub 3 (no escribe nada)
  /duplicar/api/borradores           GET lista, POST crea borradores (no escribe en ML)
  /duplicar/api/borradores/<id>/aprobar     crea la copia en ML (exige confirmado)
  /duplicar/api/borradores/<id>/descartar
  /duplicar/api/republicables        pausadas y finalizadas
  /duplicar/api/reactivar, /relist   caso secundario (exigen confirmado)
"""
from flask import Blueprint, jsonify, render_template, request, session

from modules import duplicador as dup
from modules import precio_motor as pm

bp = Blueprint('duplicador', __name__, url_prefix='/duplicar')


def _usuario() -> str:
    return session.get('username') or ''


def _client(alias: str):
    from core.account_manager import AccountManager
    if not alias:
        raise dup.DuplicadorError('Falta elegir la cuenta.')
    try:
        return AccountManager().get_client(alias)
    except Exception as e:
        raise dup.DuplicadorError(f'No se pudo conectar la cuenta {alias}: {e}')


def _cargos(alias: str, item_id: str) -> tuple[pm.Cargos, str]:
    """Los mismos cargos que usa la calculadora: override del item, si no el
    de la cuenta, si no los defaults del motor. Solo lee."""
    from web.db import session_scope
    from web.models_pricing import PricingConfig
    with session_scope() as s:
        row = (s.query(PricingConfig).filter_by(alias=alias, item_id=item_id).first()
               or s.query(PricingConfig).filter_by(alias=alias, item_id=None).first())
        if not row:
            return pm.Cargos(), 'Valores por defecto del motor (no hay cargos guardados para la cuenta)'
        c = pm.Cargos(iibb=float(row.iibb), percepcion_iva=float(row.percepcion_iva), envio=float(row.envio),
                      umbral_envio=float(row.umbral_envio), envio_bajo_umbral=bool(row.envio_bajo_umbral),
                      fijo_menos_15k=float(row.fijo_menos_15k), fijo_15k_25k=float(row.fijo_15k_25k),
                      fijo_25k_umbral=float(row.fijo_25k_umbral), redondeo=bool(row.redondeo))
        return c, ('Cargos guardados para esta publicación' if row.item_id else 'Cargos guardados de la cuenta')


def _cargos_con_ediciones(base: pm.Cargos, ed: dict) -> pm.Cargos:
    """El usuario puede editar IIBB, percepción y envío en pantalla sin guardarlos."""
    c = pm.Cargos(**base.__dict__)
    for k in ('iibb', 'percepcion_iva', 'envio'):
        v = (ed or {}).get(k)
        if v not in (None, ''):
            try:
                setattr(c, k, float(v))
            except (TypeError, ValueError):
                raise dup.DuplicadorError(f'Valor inválido para {k}.')
    return c


def _costo(item_id: str):
    from modules.stock_rentabilidad import _load_costos
    c = (_load_costos().get(item_id) or {}).get('costo')
    return float(c) if c not in (None, '', 0) else None


def _tasas(client, item: dict) -> dict:
    from modules.trio_generador import tasas_reales_por_escalon
    domain_id = item.get('domain_id') or ''
    if not domain_id:
        raise dup.DuplicadorError('La publicación no tiene domain_id: no se pueden traer las comisiones reales.')
    precio = float(item.get('price') or 0)
    tasas = tasas_reales_por_escalon(client, domain_id, precio)
    # /listing_prices falla de forma intermitente por escalon (visto 2026-10-14:
    # faltó "interés bajo" en una corrida y vino en la siguiente). Se reintenta
    # solo lo que falta; si sigue faltando, calcular() lo informa sin inventar.
    def _completa(t):
        return t.get('comision') is not None and t.get('costo_cuotas') is not None
    necesarios = {dup.ESCALON_PUB3, *dup.ESCALONES_PUB2}
    for _ in range(2):
        if all(_completa(tasas.get(e) or {}) for e in necesarios):
            break
        for k, v in tasas_reales_por_escalon(client, domain_id, precio).items():
            if not _completa(tasas.get(k) or {}):
                tasas[k] = v
    if not tasas:
        raise dup.DuplicadorError('ML no devolvió comisiones para esta categoría. Probá de nuevo en un rato.')
    return tasas


def _error(e: Exception, code: int = 400):
    return jsonify({'ok': False, 'error': str(e)}), code


@bp.route('')
def pantalla():
    return render_template('duplicador.html')


def _costos_por_up(variantes: list[dict]) -> dict:
    """Costo de cada variante: el de su Pub 1 y, si no, el de cualquier otra
    publicacion propia del mismo UP (es el mismo producto fisico)."""
    from modules.stock_rentabilidad import _load_costos
    costos = _load_costos()
    out = {}
    for v in variantes:
        ids = ([v['origen']['id']] if v.get('origen') else []) + [x['id'] for x in v.get('publicaciones') or []]
        c = next(((costos.get(i) or {}).get('costo') for i in ids if (costos.get(i) or {}).get('costo') not in (None, '', 0)), None)
        out[v['user_product_id']] = float(c) if c else None
    return out


@bp.route('/api/contexto')
def api_contexto():
    alias, item_id = request.args.get('alias', ''), request.args.get('item_id', '').strip().upper()
    try:
        client = _client(alias)
        item = dup.leer_origen(client, item_id)
        tasas = _tasas(client, item)
        cargos, cargos_origen = _cargos(alias, item_id)
        variantes = dup.familia(client, item)
    except dup.DuplicadorError as e:
        return _error(e)
    costos = _costos_por_up(variantes)
    costo = costos.get(item['user_product_id'])
    for v in variantes:
        v['costo'] = costos.get(v['user_product_id'])
    cargada = next(v for v in variantes if v['es_la_cargada'])
    existentes = cargada['publicaciones']

    esc1 = dup.escalon_de(item.get('listing_type_id'), item.get('tags'))
    actual = None
    if costo and esc1 is not None and tasas.get(esc1):
        try:
            actual = dup.evaluar_precio(float(item['price']), esc1, tasas, cargos, costo)
        except dup.DuplicadorError:
            actual = None
    return jsonify({'ok': True,
        'item': {'id': item['id'], 'titulo': item.get('title', ''), 'precio': float(item.get('price') or 0),
                 'cuotas': dup.etiqueta_escalon(esc1), 'escalon': esc1,
                 'user_product_id': item['user_product_id'], 'status': item.get('status'),
                 'variante': cargada.get('variante')},
        'detalle': dup.detalle(client, item),
        'variantes': variantes,
        'costo': costo,
        'falta_costo': costo is None,
        'margen_actual': actual,
        'cargos': cargos.__dict__, 'cargos_origen': cargos_origen,
        'tasas': {str(k): v for k, v in tasas.items()},
        'existentes': existentes,
        'escalones_pub2': [{'escalon': e, 'cuotas': dup.etiqueta_escalon(e)} for e in dup.ESCALONES_PUB2],
        'roi_objetivo': dup.ROI_OBJETIVO_DEFAULT,
        'margen_minimo_pct': dup.MARGEN_MINIMO * 100})


def _calcular_desde_body(body: dict):
    """Calcula para las variantes elegidas (por defecto, solo la cargada).
    El costo escrito en pantalla vale para la variante cargada; las demas
    usan su costo guardado."""
    alias, item_id = body.get('alias', ''), (body.get('item_id') or '').strip().upper()
    client = _client(alias)
    item = dup.leer_origen(client, item_id)
    tasas = _tasas(client, item)
    base, _ = _cargos(alias, item_id)
    cargos = _cargos_con_ediciones(base, body.get('cargos'))
    try:
        escalon = int(body.get('escalon_pub2'))
    except (TypeError, ValueError):
        raise dup.DuplicadorError('Elegí las cuotas de la Pub 2.')
    roi = float(body.get('roi_objetivo') if body.get('roi_objetivo') not in (None, '') else dup.ROI_OBJETIVO_DEFAULT)

    variantes = dup.familia(client, item)
    elegidas = set(body.get('variantes') or [item['user_product_id']])
    variantes = [v for v in variantes if v['user_product_id'] in elegidas]
    costos = _costos_por_up(variantes)
    if body.get('costo') not in (None, ''):
        costos[item['user_product_id']] = float(body['costo'])
    grupos = dup.calcular_variantes(variantes, costos, cargos, tasas, escalon, roi, body.get('precios') or {})

    actual = None
    c1 = costos.get(item['user_product_id'])
    esc1 = dup.escalon_de(item.get('listing_type_id'), item.get('tags'))
    if c1 and esc1 is not None and tasas.get(esc1):
        actual = dup.evaluar_precio(float(item.get('price') or 0), esc1, tasas, cargos, c1)
    return alias, item, grupos, costos, roi, actual


@bp.route('/api/calcular', methods=['POST'])
def api_calcular():
    try:
        _, _, grupos, costos, roi, actual = _calcular_desde_body(request.get_json() or {})
    except dup.DuplicadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'grupos': grupos, 'roi_objetivo': roi, 'margen_actual': actual})


@bp.route('/api/borradores', methods=['GET'])
def api_borradores():
    alias = request.args.get('alias', '')
    return jsonify({'ok': True, 'borradores': dup.listar_borradores(alias), 'relaciones': dup.listar_relaciones(alias)})


@bp.route('/api/borradores', methods=['POST'])
def api_crear_borradores():
    """Guarda los borradores elegidos de todas las variantes, o ninguno: todo
    se valida antes de guardar el primero."""
    body = request.get_json() or {}
    try:
        alias, _, grupos, costos, roi, _ = _calcular_desde_body(body)
        incluir = set(body.get('incluir') or [])
        pendientes = {(b['user_product_id'], b['escalon']) for b in dup.listar_borradores(alias) if b['estado'] == 'pendiente'}
        lotes = []
        for g in grupos:
            filas = [f for f in g['filas'] if f['clave'] in incluir]
            for f in filas:
                if f.get('ocupada_por'):
                    raise dup.DuplicadorError(f'{g["variante"]}: ya existe {f["cuotas"]} ({f["ocupada_por"]}).')
                if not f['cumple_piso']:
                    raise dup.DuplicadorError(f'{g["variante"]} · {f["nombre"]}: margen {f["margen_pct"]}%, debajo del piso. No se guarda nada.')
                if (g['user_product_id'], f['escalon']) in pendientes:
                    raise dup.DuplicadorError(f'{g["variante"]}: ya hay un borrador pendiente de {f["cuotas"]}.')
            if filas:
                lotes.append((g, filas))
        if not lotes:
            raise dup.DuplicadorError('Elegí al menos una publicación para guardar.')
        nuevos = []
        for g, filas in lotes:
            origen = {'id': g['origen_id'], 'title': g['titulo'] or g['variante'], 'user_product_id': g['user_product_id']}
            nuevos += dup.crear_borradores(alias, origen, filas, costos[g['user_product_id']], roi, _usuario())
    except dup.DuplicadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'borradores': nuevos})


@bp.route('/api/borradores/<bid>/descartar', methods=['POST'])
def api_descartar(bid):
    body = request.get_json() or {}
    try:
        b = dup.descartar(body.get('alias', ''), bid, _usuario())
    except dup.DuplicadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'borrador': b})


@bp.route('/api/borradores/<bid>/aprobar', methods=['POST'])
def api_aprobar(bid):
    body = request.get_json() or {}
    if body.get('confirmado') is not True:
        return _error('Falta la confirmación explícita (confirmado: true).')
    alias = body.get('alias', '')
    try:
        b = next((x for x in dup.listar_borradores(alias) if x['id'] == bid), None)
        if not b:
            raise dup.DuplicadorError('Borrador no encontrado.')
        client = _client(alias)
        item = dup.leer_origen(client, b['item_origen'])
        cargos, _ = _cargos(alias, b['item_origen'])
        # Costo de Costos si esta cargado (el vigente); si no, el que se escribio
        # en pantalla al armar el borrador.
        costo = _costo(b['item_origen']) or b.get('costo')
        resultado = dup.aprobar(alias, bid, client, cargos, _tasas(client, item), costo, _usuario())
    except dup.DuplicadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'borrador': resultado})


@bp.route('/api/republicables')
def api_republicables():
    try:
        data = dup.republicables(_client(request.args.get('alias', '')))
    except dup.DuplicadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'pausadas': data.get('paused', []), 'finalizadas': data.get('closed', [])})


@bp.route('/api/reactivar', methods=['POST'])
def api_reactivar():
    body = request.get_json() or {}
    if body.get('confirmado') is not True:
        return _error('Falta la confirmación explícita (confirmado: true).')
    try:
        r = dup.reactivar(_client(body.get('alias', '')), (body.get('item_id') or '').strip().upper())
    except dup.DuplicadorError as e:
        return _error(e)
    return jsonify({'ok': True, **r})


@bp.route('/api/relist', methods=['POST'])
def api_relist():
    body = request.get_json() or {}
    if body.get('confirmado') is not True:
        return _error('Falta la confirmación explícita (confirmado: true).')
    alias, item_id = body.get('alias', ''), (body.get('item_id') or '').strip().upper()
    try:
        client = _client(alias)
        precio, cantidad = float(body.get('precio') or 0), int(body.get('cantidad') or 0)
        costo = _costo(item_id)
        aviso = None
        if costo:
            item = client.get_item(item_id) or {}
            esc = dup.escalon_de(item.get('listing_type_id'), item.get('tags'))
            tasas = _tasas(client, item)
            if esc is not None and tasas.get(esc):
                cargos, _ = _cargos(alias, item_id)
                ev = dup.evaluar_precio(precio, esc, tasas, cargos, costo)
                if not ev['cumple_piso']:
                    raise dup.DuplicadorError(f'A ${precio:,.0f} el margen queda en {ev["margen_pct"]}% '
                                              f'(piso {dup.MARGEN_MINIMO * 100:.0f}%). No se republica.')
        else:
            aviso = 'Sin costo cargado: no se pudo verificar el margen de este precio.'
        r = dup.relist(client, item_id, precio, cantidad)
    except (dup.DuplicadorError, ValueError) as e:
        return _error(e)
    return jsonify({'ok': True, 'aviso': aviso, **r})
