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
