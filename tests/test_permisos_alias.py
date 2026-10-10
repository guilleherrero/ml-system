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


def test_mayusculas_no_esquivan_el_permiso(monkeypatch):
    # "AJENA" se resuelve a la cuenta "Ajena": se le pide permiso a esa
    c = _cliente(monkeypatch)
    monkeypatch.setattr(webapp, '_resolve_alias', lambda a: {'ajena': 'Ajena', 'mia': 'Mia'}[a.lower()])
    assert c.get('/api/pending-competidores?alias=AJENA').status_code == 403
    assert c.get('/api/pending-competidores?alias=MIA').status_code != 403
