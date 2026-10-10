"""La descripción y características de un competidor se leen de su página.
Una captura posterior desde la búsqueda (que no las trae) no las tiene que borrar."""
from modules import cerebro


def test_recaptura_sin_ficha_conserva_descripcion(monkeypatch):
    store = {'competidores': {}}
    monkeypatch.setattr(cerebro, '_competidores_raw', lambda alias: store)
    monkeypatch.setattr(cerebro, '_guardar_competidores', lambda alias, d: True, raising=False)
    monkeypatch.setattr(cerebro, '_save', lambda *a, **k: True, raising=False)
    con_ficha = {'id': 'MLA9', 'title': 'Funda camilla', 'description': 'Lycra lavable',
                 'attributes': [{'name': 'Material', 'value': 'Lycra'}], 'photos_count': 6,
                 'premium': True, 'ficha_leida': True}
    r1 = cerebro.guardar_competidor('X', con_ficha, item_propio='MLA1')
    store['competidores'][r1['clave']] = r1
    r2 = cerebro.guardar_competidor('X', {'id': 'MLA9', 'title': 'Funda camilla', 'price': 39000},
                                    item_propio='MLA1')
    assert r2['description'] == 'Lycra lavable' and r2['photos_count'] == 6
    assert r2['premium'] is True and r2['attributes'][0]['value'] == 'Lycra'
