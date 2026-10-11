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


def test_competidores_con_campos_vacios_no_rompen_el_optimizador():
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'web'))
    import app as webapp
    c = webapp._normalizar_competidores([{'id': 'MLA1', 'title': None, 'sold_quantity': None, 'price': '',
                                          'photos_count': None, 'attributes': None}, 'basura'])
    assert len(c) == 1 and c[0]['sold_quantity'] == 0 and c[0]['price'] == 0.0 and c[0]['title'] == ''
    assert c[0]['sold_quantity'] > -1   # lo que hacía fallar a seo_optimizer
