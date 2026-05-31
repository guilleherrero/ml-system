"""
Checkout MercadoPago para Biobella.

Flujo:
1. Cliente completa form en /tienda/checkout
2. Se crea Order(status='pending') en DB
3. Se crea preference en MP con back_urls + notification_url
4. Cliente paga en MP → MP redirige a /tienda/checkout/success|failure|pending
5. MP envía webhook POST a /api/mp/webhook → marcamos Order(status='paid'/'failed')

Credenciales:
- mp_access_token (TEST-... o APP_USR-...) — en app_settings, editable desde /admin/integraciones
- mp_public_key — opcional, solo si usamos el SDK JS embebido
"""
import json
from datetime import datetime
from decimal import Decimal

from web.db import session_scope
from web.models_tienda import Order, OrderItem


def _get_mp_token(session) -> str | None:
    from modules.tienda_sync import get_setting
    return get_setting(session, 'mp_access_token', None) or None


def is_configured() -> bool:
    """¿Está configurado MP?"""
    with session_scope() as s:
        return bool(_get_mp_token(s))


def crear_preferencia(order_id: int, base_url: str) -> dict:
    """
    Crea una preference de MP para la Order dada.
    Devuelve {'ok': True/False, 'init_point': '...', 'preference_id': '...', 'error': ...}

    base_url: URL pública del sitio (ej. 'https://biobella.com.ar') para back_urls.
    """
    import mercadopago

    with session_scope() as s:
        order = s.get(Order, order_id)
        if order is None:
            return {'ok': False, 'error': 'orden no existe'}

        token = _get_mp_token(s)
        if not token:
            return {'ok': False, 'error': 'mp_access_token no configurado en /admin/integraciones'}

        items = [{
            'id':          (it.mla_id or f'p{it.product_id}'),
            'title':       it.titulo[:256],
            'picture_url': it.foto or '',
            'quantity':    int(it.cantidad),
            'unit_price':  float(it.precio_unit),
            'currency_id': 'ARS',
        } for it in order.items]

        # Envío como item separado (si > 0)
        if float(order.envio or 0) > 0:
            items.append({
                'id':         'envio',
                'title':      'Envío',
                'quantity':   1,
                'unit_price': float(order.envio),
                'currency_id': 'ARS',
            })

        preference_data = {
            'items':   items,
            'payer': {
                'name':  order.cliente_nombre,
                'email': order.cliente_email,
                'phone': {'number': order.cliente_telefono or ''},
            },
            'back_urls': {
                'success': f'{base_url}/tienda/checkout/success?order_id={order.id}',
                'failure': f'{base_url}/tienda/checkout/failure?order_id={order.id}',
                'pending': f'{base_url}/tienda/checkout/pending?order_id={order.id}',
            },
            'auto_return':       'approved',
            'notification_url':  f'{base_url}/api/mp/webhook',
            'external_reference': str(order.id),
            'statement_descriptor': 'Biobella',
        }

        sdk = mercadopago.SDK(token)
        try:
            resp = sdk.preference().create(preference_data)
        except Exception as e:
            return {'ok': False, 'error': f'MP SDK error: {e}'}

        if resp.get('status') not in (200, 201):
            return {'ok': False, 'error': f'MP respondió {resp.get("status")}: {resp.get("response")}'}

        body = resp['response']
        preference_id = body.get('id')
        order.mp_preference_id = preference_id

        # init_point apunta a producción; sandbox_init_point para test.
        # Si el token arranca con TEST- usamos sandbox.
        init_point = body.get('sandbox_init_point') if token.startswith('TEST-') else body.get('init_point')

    return {
        'ok': True,
        'preference_id': preference_id,
        'init_point':    init_point or body.get('init_point'),
    }


def procesar_webhook(payload: dict) -> dict:
    """
    Maneja un webhook IPN de MP. Actualiza Order si encuentra match.

    MP manda varios formatos. Soportamos:
    - { type: 'payment', data: { id: '<payment_id>' } }
    - Query string ?topic=payment&id=<payment_id>
    """
    payment_id = (payload.get('data') or {}).get('id') or payload.get('id') or payload.get('payment_id')
    topic = payload.get('type') or payload.get('topic')

    if topic != 'payment' or not payment_id:
        return {'ok': True, 'ignored': True, 'reason': f'topic={topic} sin payment_id'}

    # Consultar el payment a MP para obtener estado + external_reference
    with session_scope() as s:
        token = _get_mp_token(s)

    if not token:
        return {'ok': False, 'error': 'mp_access_token no configurado'}

    import mercadopago
    sdk = mercadopago.SDK(token)
    try:
        resp = sdk.payment().get(payment_id)
    except Exception as e:
        return {'ok': False, 'error': f'MP payment().get falló: {e}'}

    if resp.get('status') not in (200, 201):
        return {'ok': False, 'error': f'MP respondió {resp.get("status")}'}

    pay = resp['response']
    external_ref = pay.get('external_reference')
    mp_status    = pay.get('status')       # approved | pending | rejected | refunded | cancelled | in_process | charged_back
    paid_at_str  = pay.get('date_approved')

    if not external_ref:
        return {'ok': True, 'ignored': True, 'reason': 'sin external_reference'}

    with session_scope() as s:
        try:
            order_id = int(external_ref)
        except (TypeError, ValueError):
            return {'ok': True, 'ignored': True, 'reason': f'external_reference inválido: {external_ref}'}
        order = s.get(Order, order_id)
        if order is None:
            return {'ok': True, 'ignored': True, 'reason': 'orden no encontrada'}

        order.mp_payment_id = str(payment_id)
        order.mp_status     = mp_status
        order.raw_webhook   = pay

        if mp_status == 'approved':
            order.status = 'paid'
            try:
                order.paid_at = datetime.fromisoformat(paid_at_str.replace('Z', '+00:00')) if paid_at_str else datetime.now()
            except Exception:
                order.paid_at = datetime.now()
        elif mp_status in ('rejected', 'cancelled', 'charged_back', 'refunded'):
            order.status = 'failed'
        else:
            order.status = 'pending'

    return {'ok': True, 'order_id': order_id, 'mp_status': mp_status}
