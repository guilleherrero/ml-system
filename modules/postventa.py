"""
Postventa — lo que hay que resolver ahora para cuidar la reputación y las ventas.

Junta en un solo lugar:
  - reclamos abiertos (/post-purchase/v1/claims): qué acción te toca, hasta
    cuándo y si afecta la reputación
  - mensajes de compradores sin leer (/messages/unread)
  - preguntas sin responder (/questions/search)
  - envíos por despachar con su fecha límite (/shipments/{id}/sla)
  - reputación con los límites oficiales de ML Argentina

Contratos tomados de developers.mercadolibre.com.ar ("¿Qué es un reclamo?",
"Gestionar mensajes de un reclamo", "Gestionar resolución de reclamos",
"Mensajes pendientes", "Gestión de mensajes", "Envíos") y los límites de
vendedores.mercadolibre.com.ar ("Por qué es importante la reputación"),
revisados el 2026-10-07.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from core.ml_client import MLApiError

CLAIMS = '/post-purchase/v1/claims'

# Límites oficiales MLA (vendedores.mercadolibre.com.ar). Fracción de ventas.
LIMITES = {
    'verde':        {'claims': 0.015, 'cancellations': 0.01,  'delayed_handling_time': 0.10},
    'mercadolider': {'claims': 0.01,  'cancellations': 0.005, 'delayed_handling_time': 0.08},
}
NOMBRE_METRICA = {'claims': 'Reclamos', 'cancellations': 'Cancelaciones por vos',
                  'delayed_handling_time': 'Despachos con demora'}

# Acciones del vendedor que el panel ejecuta. El resto (evidencia de envío,
# tracking, revisión de devolución) se hace en ML con el link del reclamo.
ACCIONES = {
    'send_message_to_complainant': 'Escribirle al comprador',
    'send_message_to_mediator':    'Escribirle al mediador de ML',
    'refund':                      'Devolver el dinero',
    'allow_partial_refund':        'Ofrecer reembolso parcial',
    'allow_return':                'Aceptar la devolución del producto',
    'allow_return_label':          'Aceptar la devolución del producto',
    'open_dispute':                'Pedir mediación de ML',
    'send_potential_shipping':     'Informar fecha de envío',
    'add_shipping_evidence':       'Subir comprobante de envío',
    'send_tracking_number':        'Informar número de seguimiento',
    'return_review':               'Revisar el producto devuelto',
    'send_attachments':            'Enviar adjuntos',
}
EJECUTABLES = {'send_message_to_complainant', 'send_message_to_mediator', 'refund',
               'allow_partial_refund', 'allow_return', 'allow_return_label', 'open_dispute'}

MOTIVO = {'PNR': 'No le llegó el producto', 'PDD': 'Producto diferente o defectuoso',
          'CS': 'Compra cancelada'}

AFECTA = {'affected': 'si', 'not_affected': 'no', 'not_applies': 'no'}

_RE_NUM = re.compile(r'^\d+$')


def _num(v, nombre='ID') -> str:
    """Los IDs llegan del navegador y van en el path de la API."""
    if not _RE_NUM.match(str(v or '')):
        raise ValueError(f'{nombre} inválido: {v!r}')
    return str(v)


def _uid(client) -> str:
    if not client.account.user_id:
        client.account.user_id = client.get_me()['id']
    return str(client.account.user_id)


def _fecha(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=timezone(timedelta(hours=-3)))
    except ValueError:
        return None


def horas_hasta(s, ahora: datetime | None = None) -> float | None:
    d = _fecha(s)
    if not d:
        return None
    ahora = ahora or datetime.now(timezone.utc)
    return round((d - ahora).total_seconds() / 3600, 1)


# ── Reputación ───────────────────────────────────────────────────────────────

def resumen_reputacion(rep: dict) -> dict:
    """Métricas con los límites oficiales y el margen que queda.

    Para cada métrica: tasa actual, límite, cuántos casos más entran antes de
    pasarlo y cuánto suma un caso. Si el vendedor está protegido, ML pone los
    datos reales en 'excluded': se muestran esos, que son los que cuentan
    cuando termina la protección.
    """
    m = rep.get('metrics') or {}
    ventas = int(((m.get('sales') or {}).get('completed')) or 0)
    lider = bool(rep.get('power_seller_status'))
    lim = LIMITES['mercadolider' if lider else 'verde']
    out = []
    for k in ('claims', 'delayed_handling_time', 'cancellations'):
        d = m.get(k) or {}
        exc = d.get('excluded') or {}
        valor = exc.get('real_value', d.get('value')) or 0
        tasa = exc.get('real_rate', d.get('rate')) or 0
        tope = lim[k]
        permitidos = int(tope * ventas) if ventas else None
        out.append({
            'clave': k, 'nombre': NOMBRE_METRICA[k],
            'valor': valor, 'tasa_pct': round(tasa * 100, 2),
            'limite_pct': round(tope * 100, 2),
            'quedan': (permitidos - valor) if permitidos is not None else None,
            'pp_por_caso': round(100 / ventas, 2) if ventas else None,
            # 'justo': un caso más y se pasa, o ya va por el 75% del límite
            'estado': ('mal' if tasa > tope else
                       'justo' if (permitidos is not None and permitidos - valor <= 0) or tasa > tope * 0.75
                       else 'ok'),
            'periodo': d.get('period'),
        })
    return {
        'nivel': rep.get('level_id'), 'lider': rep.get('power_seller_status'),
        'protegido_hasta': rep.get('protection_end_date'), 'nivel_real': rep.get('real_level'),
        'ventas_periodo': ventas, 'metricas': out,
        'referencia': 'MercadoLíder' if lider else 'verde',
    }


def reputacion(client) -> dict:
    u = client._get(f'/users/{_uid(client)}')
    return resumen_reputacion(u.get('seller_reputation') or {})


# ── Reclamos ─────────────────────────────────────────────────────────────────

def _mis_acciones(claim: dict) -> list[dict]:
    for p in claim.get('players') or []:
        if p.get('role') == 'respondent':
            return p.get('available_actions') or []
    return []


def urgencia_reclamo(claim: dict, detalle: dict, afecta: dict) -> dict:
    """Qué te toca, hasta cuándo y si pega en la reputación. Puro: testeable."""
    acciones = _mis_acciones(claim)
    obligatorias = [a for a in acciones if a.get('mandatory')]
    venc = [a.get('due_date') for a in obligatorias if a.get('due_date')]
    if (detalle.get('action_responsible') == 'seller') and detalle.get('due_date'):
        venc.append(detalle['due_date'])
    vence = min(venc, key=lambda s: _fecha(s) or datetime.max.replace(tzinfo=timezone.utc)) if venc else None
    me_toca = bool(obligatorias) or detalle.get('action_responsible') == 'seller'
    af = AFECTA.get(afecta.get('affects_reputation'), '?')
    if af == 'si' and afecta.get('has_incentive'):
        af = '48h'   # si respondés bien dentro de las 48 h, no afecta
    motivo = MOTIVO.get(re.sub(r'\d', '', claim.get('reason_id') or ''), claim.get('reason_id'))
    return {
        'me_toca': me_toca, 'vence': vence or afecta.get('due_date'),
        'afecta': af, 'motivo': motivo,
        'en_mediacion': claim.get('stage') == 'dispute',
        'acciones': [{'action': a.get('action'), 'nombre': ACCIONES.get(a.get('action'), a.get('action')),
                      'obligatoria': bool(a.get('mandatory')), 'vence': a.get('due_date'),
                      'ejecutable': a.get('action') in EJECUTABLES}
                     for a in acciones],
    }


def reclamos_abiertos(client, limite: int = 100) -> list[dict]:
    uid = _uid(client)
    data = client._get(f'{CLAIMS}/search', {
        'players.user_id': uid, 'players.role': 'respondent',
        'status': 'opened', 'limit': min(limite, 100), 'sort': 'date_created:desc'})
    out = []
    for c in data.get('data') or []:
        cid = c.get('id')
        detalle = _get_o_vacio(client, f'{CLAIMS}/{cid}/detail')
        afecta = _get_o_vacio(client, f'{CLAIMS}/{cid}/affects-reputation')
        orden = (_get_o_vacio(client, f'/orders/{c.get("resource_id")}')
                 if c.get('resource') == 'order' else {})
        it = ((orden.get('order_items') or [{}])[0].get('item') or {})
        out.append({
            'tipo': 'reclamo', 'id': str(cid), 'claim_type': c.get('type'),
            'stage': c.get('stage'), 'creado': c.get('date_created'),
            'orden': c.get('resource_id'), 'producto': it.get('title'),
            'item_id': it.get('id'), 'monto': orden.get('total_amount'),
            'comprador': (orden.get('buyer') or {}).get('nickname'),
            'titulo': detalle.get('title') or 'Reclamo abierto',
            'descripcion': detalle.get('description'), 'problema': detalle.get('problem'),
            'responsable': detalle.get('action_responsible'),
            'url': f'https://www.mercadolibre.com.ar/ventas/{c.get("resource_id")}/detalle',
            **urgencia_reclamo(c, detalle, afecta),
        })
        time.sleep(0.05)
    return out


def _get_o_vacio(client, path, params=None):
    try:
        return client._get(path, params) or {}
    except MLApiError:
        return {}


def detalle_reclamo(client, claim_id) -> dict:
    cid = _num(claim_id, 'Reclamo')
    claim = client._get(f'{CLAIMS}/{cid}')
    acciones = {a.get('action') for a in _mis_acciones(claim)}
    out = {
        'claim': claim,
        'mensajes': _get_o_vacio(client, f'{CLAIMS}/{cid}/messages') or [],
        'resoluciones': _get_o_vacio(client, f'{CLAIMS}/{cid}/expected-resolutions') or [],
        'parcial': (_get_o_vacio(client, f'{CLAIMS}/{cid}/partial-refund/available-offers')
                    if 'allow_partial_refund' in acciones else {}),
    }
    return out


def ejecutar_accion(client, claim_id, accion: str, texto: str = '', porcentaje=None):
    """Ejecuta una acción del vendedor sobre un reclamo.

    Antes de llamar se relee el reclamo y se verifica que ML ofrezca esa acción
    AHORA: el panel puede estar abierto hace rato y el reclamo haber cambiado.
    """
    cid = _num(claim_id, 'Reclamo')
    if accion not in EJECUTABLES:
        raise ValueError('Esta acción se hace desde Mercado Libre')
    claim = client._get(f'{CLAIMS}/{cid}')
    disponibles = {a.get('action') for a in _mis_acciones(claim)}
    if claim.get('status') != 'opened' or accion not in disponibles:
        raise ValueError('Mercado Libre ya no ofrece esa acción para este reclamo. Recargá el panel.')
    if accion.startswith('send_message'):
        texto = (texto or '').strip()
        if not texto:
            raise ValueError('El mensaje está vacío')
        receptor = 'complainant' if accion == 'send_message_to_complainant' else 'mediator'
        return client._post(f'{CLAIMS}/{cid}/actions/send-message',
                            {'receiver_role': receptor, 'message': texto[:2000]})
    if accion == 'refund':
        return client._post(f'{CLAIMS}/{cid}/expected-resolutions/refund', {})
    if accion in ('allow_return', 'allow_return_label'):
        return client._post(f'{CLAIMS}/{cid}/expected-resolutions/allow-return', {})
    if accion == 'allow_partial_refund':
        try:
            pct = float(porcentaje)
        except (TypeError, ValueError):
            raise ValueError('Elegí un porcentaje de reembolso')
        return client._post(f'{CLAIMS}/{cid}/expected-resolutions/partial-refund',
                            {'percentage': pct})
    if accion == 'open_dispute':
        return client._post(f'{CLAIMS}/{cid}/actions/open-dispute', {})


# ── Mensajes ─────────────────────────────────────────────────────────────────

_RE_PACK = re.compile(r'/packs/(\d+)/sellers/(\d+)')


def mensajes_sin_leer(client) -> list[dict]:
    data = client._get('/messages/unread', {'role': 'seller', 'tag': 'post_sale'})
    out = []
    for r in (data.get('results') or [])[:40]:
        m = _RE_PACK.search(r.get('resource') or '')
        if not m:
            continue
        pack = m.group(1)
        hilo = _get_o_vacio(client, f'/messages/packs/{pack}/sellers/{m.group(2)}',
                            {'tag': 'post_sale', 'mark_as_read': 'false', 'limit': 5})
        msgs = hilo.get('messages') or []
        ultimo = msgs[0] if msgs else {}
        orden = _get_o_vacio(client, f'/orders/{pack}')
        it = ((orden.get('order_items') or [{}])[0].get('item') or {})
        recibido = (ultimo.get('message_date') or {}).get('received')
        out.append({
            'tipo': 'mensaje', 'id': pack, 'sin_leer': r.get('count'),
            'producto': it.get('title'), 'comprador': (orden.get('buyer') or {}).get('nickname'),
            'texto': _texto(ultimo.get('text'))[:300], 'recibido': recibido,
            'horas_esperando': (-horas_hasta(recibido)) if recibido else None,
            'claim_id': (hilo.get('conversation_status') or {}).get('claim_id'),
            'url': f'https://www.mercadolibre.com.ar/ventas/{pack}/detalle',
        })
        time.sleep(0.05)
    return out


def _texto(t) -> str:
    return (t.get('plain') if isinstance(t, dict) else t) or ''


def hilo_mensajes(client, pack_id) -> list[dict]:
    pack = _num(pack_id, 'Venta')
    hilo = client._get(f'/messages/packs/{pack}/sellers/{_uid(client)}',
                       {'tag': 'post_sale', 'mark_as_read': 'false', 'limit': 30})
    yo = _uid(client)
    return [{'yo': str((m.get('from') or {}).get('user_id')) == yo,
             'texto': _texto(m.get('text')),
             'fecha': (m.get('message_date') or {}).get('received')}
            for m in reversed(hilo.get('messages') or [])]


def responder_mensaje(client, pack_id, texto: str):
    """Responde en el hilo. El destinatario es la contraparte del hilo: el
    comprador o, donde ML ya lo usa, su agente de mensajería."""
    pack = _num(pack_id, 'Venta')
    texto = (texto or '').strip()
    if not texto:
        raise ValueError('El mensaje está vacío')
    if len(texto) > 350:
        raise ValueError('Mercado Libre permite hasta 350 caracteres por mensaje')
    yo = _uid(client)
    hilo = client._get(f'/messages/packs/{pack}/sellers/{yo}',
                       {'tag': 'post_sale', 'mark_as_read': 'false', 'limit': 10})
    otro = next((str((m.get('from') or {}).get('user_id')) for m in hilo.get('messages') or []
                 if str((m.get('from') or {}).get('user_id')) != yo), None)
    if not otro:
        orden = client._get(f'/orders/{pack}')
        otro = str((orden.get('buyer') or {}).get('id') or '')
    if not otro:
        raise ValueError('No se pudo identificar al comprador de esta venta')
    return client._post(f'/messages/packs/{pack}/sellers/{yo}?tag=post_sale',
                        {'from': {'user_id': yo}, 'to': {'user_id': otro}, 'text': texto})


# ── Preguntas ────────────────────────────────────────────────────────────────

def preguntas_sin_responder(client) -> list[dict]:
    data = client._get('/questions/search', {'seller_id': _uid(client), 'status': 'UNANSWERED',
                                             'limit': 50})
    qs = sorted(data.get('questions') or [], key=lambda q: q.get('date_created') or '')
    titulos = {}
    ids = list({q.get('item_id') for q in qs if q.get('item_id')})
    for i in range(0, len(ids), 20):
        for e in _get_o_vacio(client, '/items', {'ids': ','.join(ids[i:i + 20]),
                                                  'attributes': 'id,title'}) or []:
            b = e.get('body') or {}
            titulos[b.get('id')] = b.get('title')
    return [{'tipo': 'pregunta', 'id': str(q.get('id')), 'texto': q.get('text', '').strip(),
             'item_id': q.get('item_id'), 'producto': titulos.get(q.get('item_id')),
             'recibido': q.get('date_created'),
             'horas_esperando': (-horas_hasta(q.get('date_created'))) if q.get('date_created') else None}
            for q in qs]


# ── Envíos ───────────────────────────────────────────────────────────────────

def envios_por_despachar(client, dias: int = 10) -> list[dict]:
    """Ventas pagas que todavía no despachaste, con la fecha límite de ML.

    Despachar después de esa fecha suma a 'despachos con demora'. Full no
    aplica (despacha ML).
    """
    desde = (datetime.now() - timedelta(days=dias)).strftime('%Y-%m-%dT00:00:00.000-03:00')
    data = client._get('/orders/search', {'seller': _uid(client), 'order.status': 'paid',
                                          'order.date_created.from': desde,
                                          'sort': 'date_desc', 'limit': 50})
    out, vistos = [], set()
    for o in data.get('results') or []:
        sid = (o.get('shipping') or {}).get('id')
        if not sid or sid in vistos:
            continue
        vistos.add(sid)
        sh = _get_o_vacio(client, f'/shipments/{sid}')
        if sh.get('status') not in ('ready_to_ship', 'handling') or sh.get('logistic_type') == 'fulfillment':
            continue
        sla = _get_o_vacio(client, f'/shipments/{sid}/sla')
        it = ((o.get('order_items') or [{}])[0].get('item') or {})
        out.append({
            'tipo': 'envio', 'id': str(sid), 'orden': o.get('id'),
            'producto': it.get('title'), 'comprador': (o.get('buyer') or {}).get('nickname'),
            'estado_envio': sh.get('substatus') or sh.get('status'),
            'logistica': sh.get('logistic_type'), 'vence': sla.get('expected_date'),
            'demorado': sla.get('status') == 'delayed',
            'url': f'https://www.mercadolibre.com.ar/ventas/{o.get("id")}/detalle',
        })
        time.sleep(0.05)
    return out


# ── Avisos ───────────────────────────────────────────────────────────────────

def avisos_reclamos(reclamos: list[dict], estado: dict, ahora: datetime | None = None):
    """Qué avisar por Telegram. Un aviso por reclamo nuevo y otro cuando le
    quedan menos de 24 h; nunca dos veces el mismo. Devuelve (avisos, estado).

    estado: {claim_id: {'nuevo': bool, 'vence24': bool}} — lo que ya se avisó.
    Los reclamos que se cerraron salen del estado.
    """
    nuevo_estado, avisos = {}, []
    for r in reclamos:
        if not r.get('me_toca'):
            if r['id'] in estado:
                nuevo_estado[r['id']] = estado[r['id']]
            continue
        st = dict(estado.get(r['id']) or {'nuevo': False, 'vence24': False})
        h = horas_hasta(r.get('vence'), ahora)
        afecta = {'si': 'afecta tu reputación', '48h': 'no afecta si lo resolvés en 48 h',
                  'no': 'no afecta tu reputación'}.get(r.get('afecta'), '')
        detalle = ' · '.join(x for x in [r.get('producto'), afecta,
                                         f'vence en {max(0, round(h))} h' if h is not None else ''] if x)
        if not st['nuevo']:
            avisos.append(('Reclamo nuevo: ' + (r.get('motivo') or r.get('titulo') or ''), detalle, r.get('url') or ''))
            st['nuevo'] = True
        if h is not None and h < 24 and not st['vence24']:
            avisos.append(('Reclamo por vencer: ' + (r.get('motivo') or ''), detalle, r.get('url') or ''))
            st['vence24'] = True
        nuevo_estado[r['id']] = st
    return avisos, nuevo_estado
