"""Un usuario sin permiso sobre una cuenta no la puede consultar, venga el alias
por la ruta, por ?alias=, por JSON o con otras mayúsculas."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'web'))
import app as webapp  # noqa: E402
from core import auth  # noqa: E402


def _cliente(monkeypatch):
    os.environ.pop('MCP_API_TOKEN', None)
    monkeypatch.setattr(webapp, 'needs_setup', lambda: False)
    monkeypatch.setattr(webapp, '_auto_update_if_needed', lambda: None)
    monkeypatch.setattr(auth, 'get_current_user', lambda: {'is_admin': False, 'accounts': ['Mia']})
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['is_admin'] = False
    return c


def test_alias_ajeno_se_rechaza_en_cualquier_lugar(monkeypatch):
    c = _cliente(monkeypatch)
    assert c.get('/api/pending-competidores?alias=Ajena').status_code == 403
    assert c.get('/api/salud/Ajena').status_code == 403
    assert c.post('/api/optimizar/sesion', data='{"alias": "Ajena", "item_id": "MLA1"}',
                  content_type='text/plain').status_code == 403


def test_alias_propio_con_otras_mayusculas_pasa_el_permiso():
    import core.auth as a
    orig = a.get_current_user
    a.get_current_user = lambda: {'is_admin': False, 'accounts': ['Mia']}
    try:
        assert a.user_can_access('MIA ') and not a.user_can_access('Ajena')
    finally:
        a.get_current_user = orig
