"""Conectar una cuenta con la sesion de ML de OTRO usuario no puede pisarla.

Bug real (nov 2026): se apreto "Conectar" en Novara (usuario 55993545) con la
sesion de NEOBUYBOX (363281910) abierta en el navegador; el token de NEOBUYBOX
quedo guardado en Novara y todo el sistema paso a leer otras publicaciones.
"""
import copy
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'web'))
import app as webapp  # noqa: E402

CUENTAS = {'accounts': [{'alias': 'Novara', 'client_id': 'cid', 'client_secret': 'sec',
                         'access_token': 'TOK_NOVARA', 'refresh_token': 'R_NOVARA',
                         'user_id': 55993545, 'nickname': 'NOVARA'}]}


def _resp(data):
    m = mock.Mock()
    m.json.return_value = data
    return m


def _conectar(user_id, nick, guardado):
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['is_admin'] = 1, True
        s['pkce_verifier_Novara'] = 'v'
    tok = {'access_token': f'TOK_{nick}', 'refresh_token': f'R_{nick}',
           'expires_in': 21600, 'user_id': user_id}
    with mock.patch.object(webapp.req_lib, 'post', return_value=_resp(tok)), \
         mock.patch.object(webapp.req_lib, 'get', return_value=_resp({'nickname': nick})):
        r = c.get('/oauth/exchange?code=abc&state=Novara')
    return c, r


def _con_mocks(test):
    def run():
        guardado = {'data': copy.deepcopy(CUENTAS)}
        with mock.patch.object(webapp, '_auto_update_if_needed'), \
             mock.patch.object(webapp, '_audit'), \
             mock.patch.object(webapp, 'load_json', side_effect=lambda p: copy.deepcopy(guardado['data'])), \
             mock.patch.object(webapp, 'save_json', side_effect=lambda p, d: guardado.update(data=copy.deepcopy(d))), \
             mock.patch.object(webapp, 'render_template', return_value='ok'):
            test(guardado)
    return run


@_con_mocks
def test_otro_usuario_no_pisa_y_pide_confirmar(guardado):
    c, r = _conectar(363281910, 'NEOBUYBOX', guardado)
    html = r.get_data(as_text=True)
    assert 'otra cuenta de MercadoLibre' in html and 'NEOBUYBOX' in html
    acc = guardado['data']['accounts'][0]
    assert acc['access_token'] == 'TOK_NOVARA' and acc['user_id'] == 55993545
    # solo con el boton explicito se reemplaza
    c.post('/oauth/confirmar-reemplazo/Novara')
    acc = guardado['data']['accounts'][0]
    assert acc['access_token'] == 'TOK_NEOBUYBOX' and acc['user_id'] == 363281910
    assert acc['nickname'] == 'NEOBUYBOX'


@_con_mocks
def test_mismo_usuario_reconecta_directo(guardado):
    _, r = _conectar(55993545, 'NOVARA', guardado)
    assert r.get_data(as_text=True) == 'ok'
    assert guardado['data']['accounts'][0]['access_token'] == 'TOK_NOVARA'
    assert guardado['data']['accounts'][0]['refresh_token'] == 'R_NOVARA'


@_con_mocks
def test_confirmar_sin_pendiente_no_hace_nada(guardado):
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['is_admin'] = 1, True
    r = c.post('/oauth/confirmar-reemplazo/Novara')
    assert 'No hay nada pendiente' in r.get_data(as_text=True)
    assert guardado['data']['accounts'][0]['access_token'] == 'TOK_NOVARA'
