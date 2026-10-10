"""Analista de Meli Ads: veredictos y acciones contra el margen real."""
from modules import ads_analista as aa


def _datos(ads, camps=None):
    return {'anuncios': ads, 'campanias': camps or [{'id': 1, 'name': 'C', 'status': 'active', 'daily_budget': 1000,
                                                     'metrics': {'cost': 9000, 'total_amount': 10000}}],
            'gasto_hoy': {1: 100}, 'desde': '2026-10-03', 'hasta': '2026-10-10'}


def test_gasta_sin_vender_pide_sacarlo_y_explica_el_precio():
    ad = {'item_id': 'MLA1', 'campaign_id': 1, 'title': 'Cortador', 'price': 72000,
          'metrics': {'cost': 8447, 'clicks': 17, 'prints': 1289, 'total_amount': 0, 'units_quantity': 0}}
    out = aa.analizar(_datos([ad]), {'MLA1': {'precio': 72000, 'margen_pct': 0.34, 'stock': 102, 'dias_stock': 300,
                                              'conversion_pct': 2, 'free_shipping': True}},
                      competencia=lambda i: 50000, dias=7)
    a = next(x for x in out['acciones'] if x['tipo'] == 'sacar')
    assert '7 días' in a['porque'] and any('44% arriba' in c for c in a['causas'])
    assert out['productos'][0]['veredicto'] == 'pierde'


def test_acos_mayor_al_margen_pierde_y_menor_gana():
    st = {'MLA1': {'precio': 1000, 'margen_pct': 0.30, 'stock': 50, 'dias_stock': 60},
          'MLA2': {'precio': 1000, 'margen_pct': 0.30, 'stock': 50, 'dias_stock': 60}}
    ads = [{'item_id': 'MLA1', 'campaign_id': 1, 'metrics': {'cost': 400, 'total_amount': 1000, 'units_quantity': 1, 'clicks': 5}},
           {'item_id': 'MLA2', 'campaign_id': 1, 'metrics': {'cost': 100, 'total_amount': 1000, 'units_quantity': 1, 'clicks': 5}}]
    out = aa.analizar(_datos(ads), st)
    v = {p['item_id']: p for p in out['productos']}
    assert v['MLA1']['veredicto'] == 'pierde' and v['MLA1']['resultado'] == -100
    assert v['MLA2']['veredicto'] == 'gana' and v['MLA2']['resultado'] == 200


def test_presupuesto_agotado_y_rentable_pide_subirlo():
    st = {'MLA1': {'precio': 1000, 'margen_pct': 0.5, 'stock': 50, 'dias_stock': 60}}
    ads = [{'item_id': 'MLA1', 'campaign_id': 1, 'metrics': {'cost': 100, 'total_amount': 1000, 'units_quantity': 1}}]
    d = _datos(ads)
    d['gasto_hoy'] = {1: 950}
    out = aa.analizar(d, st)
    assert any(a['tipo'] == 'presupuesto' for a in out['acciones'])


def test_alerta_temprana_dejo_de_vender_y_se_dejo_de_mostrar():
    st = {'MLA1': {'precio': 1000, 'margen_pct': 0.4, 'stock': 50, 'dias_stock': 60},
          'MLA2': {'precio': 1000, 'margen_pct': 0.4, 'stock': 50, 'dias_stock': 60}}
    ahora = [{'item_id': 'MLA1', 'campaign_id': 1, 'status': 'active', 'metrics': {'cost': 900, 'clicks': 15, 'prints': 3000}},
             {'item_id': 'MLA2', 'campaign_id': 1, 'status': 'active', 'metrics': {'cost': 50, 'clicks': 2, 'prints': 300, 'total_amount': 1000, 'units_quantity': 1}}]
    antes = {'anuncios': [{'item_id': 'MLA1', 'metrics': {'cost': 800, 'clicks': 14, 'prints': 3000, 'total_amount': 3000, 'units_quantity': 3}},
                          {'item_id': 'MLA2', 'metrics': {'cost': 400, 'clicks': 30, 'prints': 5000, 'total_amount': 4000, 'units_quantity': 4}}]}
    out = aa.analizar(_datos(ahora), st, dias=7, previo=antes)
    tipos = {a.get('item_id'): a['tipo'] for a in out['acciones'] if a.get('item_id')}
    assert tipos == {'MLA1': 'dejo_de_vender', 'MLA2': 'se_dejo_de_mostrar'}


def test_objetivo_de_acos_mayor_al_margen():
    st = {'MLA1': {'precio': 1000, 'margen_pct': 0.15, 'stock': 50, 'dias_stock': 60}}
    ads = [{'item_id': 'MLA1', 'campaign_id': 1, 'metrics': {'cost': 50, 'total_amount': 1000, 'units_quantity': 1}}]
    d = _datos(ads, [{'id': 1, 'name': 'C', 'status': 'active', 'daily_budget': 1000, 'acos_target': 25,
                      'metrics': {'cost': 50, 'total_amount': 1000}}])
    a = next(x for x in aa.analizar(d, st)['acciones'] if x['tipo'] == 'objetivo_alto')
    assert a['acos_sugerido'] == 12


def test_ejecutar_valida_antes_de_tocar_mercado_libre(monkeypatch):
    import pytest
    llamadas = []
    monkeypatch.setattr(aa, '_escribir', lambda m, path, t, body: llamadas.append((m, path, body)) or {'ok': True})
    for mala in ({'tipo': 'anuncio_estado', 'item_id': "MLA1/../../x", 'estado': 'paused'},
                 {'tipo': 'campania', 'campania_id': 5, 'presupuesto': -3},
                 {'tipo': 'campania', 'campania_id': 5, 'acos_objetivo': 500},
                 {'tipo': 'borrar_todo'}):
        with pytest.raises(ValueError):
            aa.ejecutar('t', mala)
    assert llamadas == []
    aa.ejecutar('t', {'tipo': 'anuncio_estado', 'item_id': 'mla1568967243', 'estado': 'paused'})
    aa.ejecutar('t', {'tipo': 'campania', 'campania_id': 357067839, 'presupuesto': 3750})
    assert llamadas == [('PUT', '/marketplace/advertising/MLA/product_ads/ads/MLA1568967243', {'status': 'paused'}),
                        ('PUT', '/marketplace/advertising/MLA/product_ads/campaigns/357067839', {'budget': 3750.0})]
