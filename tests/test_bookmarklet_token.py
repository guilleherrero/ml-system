"""El capturador de competidores corre en mercadolibre.com.ar sin sesión:
sus endpoints tienen que exigir siempre el token que va dentro del botón."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'web'))
os.environ.pop('CEREBRO_BOOKMARKLET_TOKEN', None)
import app as webapp  # noqa: E402


def test_token_derivado_por_cuenta():
    t = webapp._token_bookmarklet('Novara')
    assert len(t) == 32 and t == webapp._token_bookmarklet('Novara')
    assert webapp._token_bookmarklet_ok('Novara', t)
    assert not webapp._token_bookmarklet_ok('Novara', '')
    assert not webapp._token_bookmarklet_ok('Otra', t)


def test_endpoints_publicos_rechazan_sin_token():
    c = webapp.app.test_client()
    assert c.get('/api/mis-publicaciones-cors/Novara').status_code == 403
    r = c.post('/api/capturar-competidor', json={'alias': 'Novara', 'id': 'MLA1', 'title': 'x'})
    assert r.status_code == 403
