"""Postventa: límites de reputación, urgencia de reclamos, acciones que mueven
plata y avisos de Telegram.

Las respuestas de ML salen de la documentación oficial de reclamos (revisada
el 2026-10-07). Lo crítico: nunca ejecutar una acción que ML ya no ofrece y
nunca avisar dos veces lo mismo.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import postventa as pv  # noqa: E402

AHORA = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, gets=None):
        self.gets = gets or {}
        self.posts = []

        class _Acc:
            user_id = 99
        self.account = _Acc()

    def _get(self, path, params=None):
        return self.gets[path]

    def _post(self, path, body):
        self.posts.append((path, body))
        return {}


def _claim(acciones, status='opened'):
    return {'id': 5001, 'status': status, 'stage': 'claim', 'reason_id': 'PDD9939',
            'players': [{'role': 'complainant', 'available_actions': []},
                        {'role': 'respondent', 'available_actions': acciones}]}


# ── reputación ───────────────────────────────────────────────────────────────

def _rep(**metricas):
    base = {'sales': {'completed': 200}}
    base.update(metricas)
    return {'level_id': '5_green', 'metrics': base}


def test_limites_oficiales_verde_y_margen():
    r = pv.resumen_reputacion(_rep(claims={'rate': 0.01, 'value': 2}))
    claims = r['metricas'][0]
    assert claims['limite_pct'] == 1.5            # verde: 1,5% de reclamos
    assert claims['quedan'] == 1                  # 1,5% de 200 = 3 → quedan 1
    assert claims['pp_por_caso'] == 0.5           # cada reclamo suma 0,5 pp
    assert claims['estado'] == 'ok'


def test_mercadolider_usa_limites_mas_duros():
    rep = _rep(cancellations={'rate': 0.006, 'value': 1})
    rep['power_seller_status'] = 'gold'
    canc = pv.resumen_reputacion(rep)['metricas'][2]
    assert canc['limite_pct'] == 0.5 and canc['estado'] == 'mal'


def test_protegido_muestra_datos_reales():
    rep = _rep(claims={'rate': 0, 'value': 0, 'excluded': {'real_value': 5, 'real_rate': 0.025}})
    claims = pv.resumen_reputacion(rep)['metricas'][0]
    assert claims['valor'] == 5 and claims['quedan'] == -2 and claims['estado'] == 'mal'


# ── urgencia ─────────────────────────────────────────────────────────────────

def test_accion_obligatoria_marca_vencimiento_y_me_toca():
    c = _claim([{'action': 'send_message_to_complainant', 'mandatory': True,
                 'due_date': '2026-10-08T10:00:00.000-03:00'},
                {'action': 'refund', 'mandatory': False, 'due_date': None}])
    u = pv.urgencia_reclamo(c, {}, {'affects_reputation': 'affected', 'has_incentive': False})
    assert u['me_toca'] and u['vence'] == '2026-10-08T10:00:00.000-03:00'
    assert u['afecta'] == 'si'
    assert u['motivo'] == 'Producto diferente o defectuoso'
    assert [a['ejecutable'] for a in u['acciones']] == [True, True]


def test_incentivo_48h():
    u = pv.urgencia_reclamo(_claim([]), {'action_responsible': 'buyer'},
                            {'affects_reputation': 'affected', 'has_incentive': True})
    assert u['afecta'] == '48h' and not u['me_toca']


def test_detalle_dice_que_le_toca_al_vendedor():
    u = pv.urgencia_reclamo(_claim([]), {'action_responsible': 'seller',
                                         'due_date': '2026-10-09T00:00:00.000-03:00'}, {})
    assert u['me_toca'] and u['vence'].startswith('2026-10-09')


# ── acciones ─────────────────────────────────────────────────────────────────

def test_no_ejecuta_si_ml_ya_no_ofrece_la_accion():
    c = FakeClient({'/post-purchase/v1/claims/5001': _claim([{'action': 'send_message_to_complainant'}])})
    with pytest.raises(ValueError):
        pv.ejecutar_accion(c, '5001', 'refund')
    assert c.posts == []


def test_no_ejecuta_sobre_reclamo_cerrado():
    c = FakeClient({'/post-purchase/v1/claims/5001': _claim([{'action': 'refund'}], status='closed')})
    with pytest.raises(ValueError):
        pv.ejecutar_accion(c, '5001', 'refund')
    assert c.posts == []


@pytest.mark.parametrize('accion,path', [
    ('refund', '/post-purchase/v1/claims/5001/expected-resolutions/refund'),
    ('allow_return', '/post-purchase/v1/claims/5001/expected-resolutions/allow-return'),
    ('open_dispute', '/post-purchase/v1/claims/5001/actions/open-dispute'),
])
def test_cada_accion_va_a_su_endpoint(accion, path):
    c = FakeClient({'/post-purchase/v1/claims/5001': _claim([{'action': accion}])})
    pv.ejecutar_accion(c, '5001', accion)
    assert c.posts == [(path, {})]


def test_mensaje_al_comprador_y_al_mediador():
    c = FakeClient({'/post-purchase/v1/claims/5001': _claim(
        [{'action': 'send_message_to_complainant'}, {'action': 'send_message_to_mediator'}])})
    pv.ejecutar_accion(c, '5001', 'send_message_to_complainant', '  Hola  ')
    pv.ejecutar_accion(c, '5001', 'send_message_to_mediator', 'Adjunto prueba')
    assert c.posts[0][1] == {'receiver_role': 'complainant', 'message': 'Hola'}
    assert c.posts[1][1]['receiver_role'] == 'mediator'
    with pytest.raises(ValueError):
        pv.ejecutar_accion(c, '5001', 'send_message_to_complainant', '   ')


def test_reembolso_parcial_manda_porcentaje():
    c = FakeClient({'/post-purchase/v1/claims/5001': _claim([{'action': 'allow_partial_refund'}])})
    pv.ejecutar_accion(c, '5001', 'allow_partial_refund', porcentaje='40')
    assert c.posts == [('/post-purchase/v1/claims/5001/expected-resolutions/partial-refund',
                        {'percentage': 40.0})]


def test_acciones_de_ml_no_se_ejecutan_desde_el_panel():
    c = FakeClient()
    with pytest.raises(ValueError):
        pv.ejecutar_accion(c, '5001', 'add_shipping_evidence')


@pytest.mark.parametrize('malo', ['../../orders/1', '5001/x', '', None, 'abc'])
def test_ids_invalidos_no_llegan_a_ml(malo):
    c = FakeClient()
    with pytest.raises(ValueError):
        pv.ejecutar_accion(c, malo, 'refund')
    with pytest.raises(ValueError):
        pv.responder_mensaje(c, malo, 'hola')
    assert c.posts == []


# ── mensajes ─────────────────────────────────────────────────────────────────

def test_responde_a_la_contraparte_del_hilo():
    hilo = {'messages': [{'from': {'user_id': 99}}, {'from': {'user_id': 3037674934}}]}
    c = FakeClient({'/messages/packs/123/sellers/99': hilo})
    pv.responder_mensaje(c, '123', 'Sale mañana')
    path, body = c.posts[0]
    assert path == '/messages/packs/123/sellers/99?tag=post_sale'
    assert body == {'from': {'user_id': '99'}, 'to': {'user_id': '3037674934'}, 'text': 'Sale mañana'}


def test_mensaje_de_mas_de_350_caracteres_se_rechaza():
    with pytest.raises(ValueError):
        pv.responder_mensaje(FakeClient(), '123', 'x' * 351)


# ── avisos ───────────────────────────────────────────────────────────────────

def _r(id_, vence, me_toca=True, afecta='si'):
    return {'id': id_, 'me_toca': me_toca, 'vence': vence, 'afecta': afecta,
            'motivo': 'No le llegó el producto', 'producto': 'Faja', 'url': 'https://ml/x'}


def test_avisa_nuevo_una_sola_vez():
    avisos, est = pv.avisos_reclamos([_r('1', '2026-10-10T00:00:00+00:00')], {}, AHORA)
    assert [a[0] for a in avisos] == ['Reclamo nuevo: No le llegó el producto']
    avisos2, _ = pv.avisos_reclamos([_r('1', '2026-10-10T00:00:00+00:00')], est, AHORA)
    assert avisos2 == []


def test_avisa_cuando_quedan_menos_de_24h():
    _, est = pv.avisos_reclamos([_r('1', '2026-10-10T00:00:00+00:00')], {}, AHORA)
    avisos, est2 = pv.avisos_reclamos([_r('1', '2026-10-08T05:00:00+00:00')], est, AHORA)
    assert len(avisos) == 1 and avisos[0][0].startswith('Reclamo por vencer')
    assert 'vence en 14 h' in avisos[0][1]
    assert pv.avisos_reclamos([_r('1', '2026-10-08T05:00:00+00:00')], est2, AHORA)[0] == []


def test_reclamo_cerrado_sale_del_estado_y_esperando_no_avisa():
    _, est = pv.avisos_reclamos([_r('1', None), _r('2', None)], {}, AHORA)
    avisos, est2 = pv.avisos_reclamos([_r('2', None, me_toca=False)], est, AHORA)
    assert avisos == [] and set(est2) == {'2'}


def test_un_caso_mas_y_se_pasa_es_amarillo_aunque_la_tasa_sea_baja():
    # 2 reclamos en 180 ventas = 1,11% (< 75% de 1,5%), pero el tercero da 1,67%
    m = pv.resumen_reputacion({'metrics': {'sales': {'completed': 180},
                                           'claims': {'rate': 0.0111, 'value': 2}}})['metricas'][0]
    assert m['quedan'] == 0 and m['estado'] == 'justo'


def test_envios_ya_entregados_al_correo_no_figuran_por_despachar():
    # Caso real de produccion (2026-10-07): ready_to_ship/in_hub = ya lo dejaste
    class C(FakeClient):
        def _get(self, path, params=None):
            if path == '/orders/search':
                return {'results': [{'id': 1, 'shipping': {'id': 10}, 'order_items': []},
                                    {'id': 2, 'shipping': {'id': 20}, 'order_items': []},
                                    {'id': 3, 'shipping': {'id': 30}, 'order_items': []}]}
            return {'/shipments/10': {'status': 'ready_to_ship', 'substatus': 'in_hub'},
                    '/shipments/20': {'status': 'ready_to_ship', 'substatus': 'ready_to_print'},
                    '/shipments/30': {'status': 'ready_to_ship', 'substatus': 'printed',
                                      'logistic_type': 'fulfillment'},
                    '/shipments/20/sla': {'status': 'on_time', 'expected_date': '2026-10-08T16:00:00-03:00'},
                    }.get(path, {})
    out = pv.envios_por_despachar(C())
    assert [e['id'] for e in out] == ['20']


def test_reclamo_sin_acceso_da_mensaje_claro():
    # Caso real (2026-10-07): ML responde 403 "User does not have access to claim"
    from core.ml_client import MLApiError

    class C(FakeClient):
        def _get(self, path, params=None):
            raise MLApiError('GET failed: {"code":403}', 403)
    with pytest.raises(ValueError, match='Abrilo directamente en Mercado Libre'):
        pv.detalle_reclamo(C(), '5589689450')


# ── proyección (datos reales de Novara, 07/10/2026) ──────────────────────────

NOVARA_AFECTAN = ['2026-08-14', '2026-08-20', '2026-08-26', '2026-08-26', '2026-09-03',
                  '2026-09-17', '2026-09-29', '2026-10-01', '2026-10-06']


def test_base_real_sale_de_valor_sobre_tasa():
    # ML informa 9 reclamos al 1,25% con 673 'completed': la base es 720
    m = pv.resumen_reputacion({'power_seller_status': 'platinum', 'metrics': {
        'sales': {'completed': 673},
        'claims': {'rate': 0, 'value': 0, 'excluded': {'real_value': 9, 'real_rate': 0.0125}}}})['metricas'][0]
    assert m['base'] == 720 and m['permitidos'] == 7 and m['quedan'] == -2


def test_proyeccion_novara_vuelve_al_limite_el_19_10():
    from datetime import date, timedelta
    recl = [{'deja_de_contar': (date.fromisoformat(d) + timedelta(days=60)).isoformat()}
            for d in NOVARA_AFECTAN]
    p = pv.proyeccion(recl, permitidos=7, base=720, hoy='2026-10-07')
    assert p['hoy'] == 9 and not p['ya_dentro']
    assert p['recupera_el'] == '2026-10-19'
    assert [(x['fecha'], x['cuentan']) for x in p['pasos'][:3]] == [
        ('2026-10-13', 8), ('2026-10-19', 7), ('2026-10-25', 5)]
    assert p['pasos'][1]['tasa_pct'] == 0.97


def test_reclamos_ventana_solo_afectados_y_cachea_cerrados(monkeypatch):
    store = {}
    monkeypatch.setattr('core.db_storage.db_load', lambda p: store.get(p))
    monkeypatch.setattr('core.db_storage.db_save', lambda p, d: store.__setitem__(p, d))
    pedidos = []

    class C(FakeClient):
        def _get(self, path, params=None):
            if path.endswith('/search'):
                return {'paging': {'total': 3}, 'data': [
                    {'id': 1, 'type': 'mediations', 'status': 'closed', 'date_created': '2026-08-14T10:00:00.000-04:00', 'reason_id': 'PDD9949'},
                    {'id': 2, 'type': 'cancel_purchase', 'status': 'closed', 'date_created': '2026-08-20T10:00:00.000-04:00'},
                    {'id': 3, 'type': 'mediations', 'status': 'opened', 'date_created': '2026-10-06T10:00:00.000-04:00'}]}
            pedidos.append(path)
            return {'affects_reputation': 'affected' if '/1/' in path else 'not_affected'}

    out = pv.reclamos_ventana(C(), 'Novara', AHORA)
    assert [r['id'] for r in out] == ['1'] and out[0]['deja_de_contar'] == '2026-10-13'
    assert out[0]['motivo'] == 'Producto diferente o defectuoso'
    assert len(pedidos) == 2                      # cancel_purchase no se consulta
    pedidos.clear()
    pv.reclamos_ventana(C(), 'Novara', AHORA)
    assert pedidos == ['/post-purchase/v1/claims/3/affects-reputation']   # el cerrado sale del cache
