"""Meli Ads con Product Ads v2 (ML retiró /advertising/product_ads/campaigns).
Formato tomado de las respuestas reales de producción."""
from modules import meli_ads_engine as ads


def test_campanias_desde_v2(monkeypatch):
    def fake_get(path, token, params=None, base=None, api_version=None):
        if path == '/advertising/advertisers':
            return {'ok': True, 'status': 200, 'data': {'advertisers': [{'advertiser_id': 82202, 'site_id': 'MLA'}]}}
        if path.endswith('/campaigns/search'):
            hoy = params.get('metrics') == 'cost'
            return {'ok': True, 'status': 200, 'data': {'paging': {'total': 1}, 'results': [{
                'id': 353548375, 'name': 'IMPULSO', 'status': 'active', 'strategy': 'PROFITABILITY',
                'acos_target': 20.0, 'budget': 4000.0, 'daily_budget': 4122.0,
                'metrics': {'cost': 1000.0} if hoy else {'clicks': 2392, 'prints': 359158, 'cost': 205028.92,
                                                        'total_amount': 1025029.0, 'units_quantity': 31}}]}}
        if path.endswith('/ads/search'):
            return {'ok': True, 'status': 200, 'data': {'paging': {'total': 2}, 'results': [
                {'item_id': 'MLA4035324794', 'campaign_id': 353548375, 'title': 'Dilatador Nasal', 'price': 37000.0},
                {'item_id': 'MLA1', 'campaign_id': 999, 'title': 'Otro', 'price': 1.0}]}}
        raise AssertionError(path)
    monkeypatch.setattr(ads, '_ads_get', fake_get)
    monkeypatch.setattr(ads, '_batch_fetch_items_sales', lambda token, ids: {})
    camps, meta = ads.build_campaigns_from_api('t', '2026-09-10', '2026-10-10')
    assert meta['advertiser_id'] == 82202 and meta['ads_count'] == 2 and len(camps) == 1
    c = camps[0]
    assert c['name'] == 'IMPULSO' and c['item_ids'] == ['MLA4035324794'] and c['budget'] == 4122.0
    assert c['metrics']['clicks'] == 2392 and c['metrics']['spend'] == 205028.92
    assert c['metrics']['acos'] == round(205028.92 / 1025029.0, 4) and c['today_spend'] == 1000.0
    assert ads._anuncios_por_campania('t') == {353548375: ['MLA4035324794'], 999: ['MLA1']}
