"""
Rentabilidad real por venta — MUESTRA (solo lectura).

Antes de construir la sección completa, Guille quiere ver los números reales
de algunas ventas: qué le descontó efectivamente ML/MP en cada una, según el
tipo de publicación (clásica/premium) y de envío (Full/Flex/Colecta).

  GET /rentabilidad-real/api/muestra?alias=Novara&n=10

Junta, por orden:
  - la orden de ML (precio, cargo por venta = sale_fee, tipo de publicación)
  - el envío (tipo de logística y lo que pagó el vendedor)
  - el pago de MP (neto acreditado, comisiones y retenciones por pago)
  - lo que ya está en el libro contable para esa orden (cargos y percepciones
    de la facturación de ML, si ya se importó)
No escribe nada en ningún lado.
"""
from flask import Blueprint, jsonify, request

bp = Blueprint('rentabilidad_real', __name__, url_prefix='/rentabilidad-real')


def _f(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _seguro(fn, *a, **k):
    try:
        return fn(*a, **k), None
    except Exception as e:  # cada fuente puede fallar sola sin tirar la muestra
        return None, str(e)[:200]


def _token_mp(alias):
    from web.db import session_scope
    from web.models_contabilidad import CuentaMP
    from modules.contabilidad_import import _token_mp as _tok
    with session_scope() as s:
        c = (s.query(CuentaMP).filter(CuentaMP.ml_alias == alias).first()
             or s.query(CuentaMP).filter(CuentaMP.alias == alias).first())
        if c:
            return _tok(c)
    return None


def _pago_mp(token, pago_id):
    from modules.contabilidad_import import _mp_get
    p = _mp_get(token, f'/v1/payments/{pago_id}', {})
    det = p.get('transaction_details') or {}
    return {
        'bruto': _f(p.get('transaction_amount')),
        'neto_acreditado': _f(det.get('net_received_amount')),
        'cuotas': p.get('installments'),
        'comisiones': [{'tipo': f.get('type'), 'monto': _f(f.get('amount'))}
                       for f in (p.get('fee_details') or [])],
        'impuestos': [{'tipo': t.get('type') or t.get('name') or t.get('id'),
                       'monto': _f(t.get('amount'))}
                      for t in (p.get('taxes_amount_details') or p.get('taxes') or [])],
        'estado': p.get('status'),
    }


def _libro(order_id):
    from web.db import session_scope
    from web.models_contabilidad import Movimiento
    with session_scope() as s:
        filas = s.query(Movimiento).filter(Movimiento.order_id == str(order_id)).all()
        return [{'origen': m.origen, 'subtipo': m.subtipo, 'concepto': m.concepto[:80],
                 'monto': _f(m.monto), 'iva': _f(m.iva), 'percepciones': _f(m.percepciones),
                 'retenciones': _f(m.retenciones), 'periodo': m.periodo} for m in filas]


@bp.route('/api/muestra')
def api_muestra():
    from core.account_manager import AccountManager
    alias = request.args.get('alias', 'Novara')
    try:
        n = max(1, min(15, int(request.args.get('n', 10))))
    except ValueError:
        n = 10
    client = AccountManager().get_client(alias)
    client._ensure_token()
    user_id = client.account.user_id
    data = client._get('/orders/search', {'seller': user_id, 'order.status': 'paid',
                                          'sort': 'date_desc', 'limit': 50})
    ordenes = (data or {}).get('results') or []
    tok_mp, err_tok = _seguro(_token_mp, alias)

    vistos, salida = {}, []
    for o in ordenes:
        if len(salida) >= n:
            break
        items = o.get('order_items') or []
        if not items:
            continue
        it = items[0]
        tipo_pub = (it.get('listing_type_id') or '')
        envio_id = (o.get('shipping') or {}).get('id')
        envio, err_env = _seguro(client._get, f'/shipments/{envio_id}') if envio_id else (None, 'sin envío')
        logistica = (envio or {}).get('logistic_type') or 'sin_envio'
        combo = (tipo_pub, logistica)
        # diversidad: hasta 2 ventas por combinación tipo de publicación + envío
        if vistos.get(combo, 0) >= 2:
            continue
        vistos[combo] = vistos.get(combo, 0) + 1

        costos_env, err_cos = _seguro(client._get, f'/shipments/{envio_id}/costs') if envio_id else (None, None)
        pagos = o.get('payments') or []
        pago_ml = pagos[0] if pagos else {}
        pago_mp, err_mp = (None, err_tok or 'sin token MP')
        if tok_mp and pago_ml.get('id'):
            pago_mp, err_mp = _seguro(_pago_mp, tok_mp, pago_ml.get('id'))
        libro, err_lib = _seguro(_libro, o.get('id'))

        senders = (costos_env or {}).get('senders') or []
        salida.append({
            'order_id': o.get('id'),
            'fecha': o.get('date_created'),
            'producto': (it.get('item') or {}).get('title'),
            'item_id': (it.get('item') or {}).get('id'),
            'cantidad': it.get('quantity'),
            'precio_unitario': _f(it.get('unit_price')),
            'total_venta': _f(o.get('total_amount')),
            'tipo_publicacion': tipo_pub,
            'logistica': logistica,
            'cargo_por_venta_ml': _f(it.get('sale_fee')),
            'sale_fee_details': it.get('sale_fee_details'),
            'pago_en_orden': {'marketplace_fee': _f(pago_ml.get('marketplace_fee')),
                              'costo_envio': _f(pago_ml.get('shipping_cost')),
                              'impuestos': _f(pago_ml.get('taxes_amount')),
                              'cuotas': pago_ml.get('installments')},
            'envio_pagado_por_vendedor': _f(senders[0].get('cost')) if senders else None,
            'envio_costos_raw': costos_env,
            'pago_mp': pago_mp,
            'libro_contable': libro,
            'errores': {k: v for k, v in {'envio': err_env, 'costos_envio': err_cos,
                                          'pago_mp': err_mp, 'libro': err_lib}.items() if v},
        })
    return jsonify({'ok': True, 'alias': alias, 'cantidad': len(salida), 'ventas': salida})
