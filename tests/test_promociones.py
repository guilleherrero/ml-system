"""Promociones: cuerpo de cada POST según el tipo, fechas de campañas propias,
paginación, margen y pausa del repricing.

Los bodies salen de la documentación oficial de /seller-promotions
(revisada el 2026-10-07): cada tipo pide campos distintos y ML responde 400
si sobra o falta uno.
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ml_client import MLApiError  # noqa: E402
from modules import promociones as pm  # noqa: E402
from modules import repricing  # noqa: E402


class FakeClient:
    """Responde por path; registra lo que se mandó."""

    def __init__(self, gets=None, post_errors=None):
        self.gets = gets or {}
        self.post_errors = list(post_errors or [])
        self.posts, self.get_calls = [], []

        class _Acc:
            user_id = 123
        self.account = _Acc()

    def _get(self, path, params=None):
        self.get_calls.append((path, dict(params or {})))
        r = self.gets[path]
        return r(params) if callable(r) else r

    def _post(self, path, body):
        self.posts.append((path, body))
        if self.post_errors:
            raise self.post_errors.pop(0)
        return {'price': body.get('deal_price')}


# ── body_para_sumar ──────────────────────────────────────────────────────────

def test_deal_manda_precio_y_promo():
    assert pm.body_para_sumar('DEAL', 'P-MLA1', deal_price=900) == {
        'promotion_type': 'DEAL', 'promotion_id': 'P-MLA1', 'deal_price': 900.0}


def test_cofondeada_solo_acepta_sin_precio():
    b = pm.body_para_sumar('MARKETPLACE_CAMPAIGN', 'P-MLA2', deal_price=500)
    assert b == {'promotion_type': 'MARKETPLACE_CAMPAIGN', 'promotion_id': 'P-MLA2'}


def test_smart_lleva_offer_id_del_candidato():
    b = pm.body_para_sumar('SMART', 'P-MLA3', offer_id='CANDIDATE-MLA9-1')
    assert b['offer_id'] == 'CANDIDATE-MLA9-1' and 'deal_price' not in b


def test_relampago_exige_stock():
    with pytest.raises(ValueError):
        pm.body_para_sumar('LIGHTNING', 'LGH-1', deal_price=100)
    assert pm.body_para_sumar('LIGHTNING', 'LGH-1', deal_price=100, stock=3)['stock'] == 3
    # El horario que ML asigna al ítem tiene que volver en el POST
    b = pm.body_para_sumar('LIGHTNING', 'LGH-1', deal_price=100, stock=3,
                           start_date='2026-10-10T12:00:00', finish_date='2026-10-10T18:00:00')
    assert (b['start_date'], b['finish_date']) == ('2026-10-10T12:00:00', '2026-10-10T18:00:00')


def test_descuento_individual_sin_promo_id_y_con_fechas():
    b = pm.body_para_sumar('PRICE_DISCOUNT', 'X', deal_price=80, desde='2026-10-08', hasta='2026-10-14')
    assert 'promotion_id' not in b
    assert b['start_date'] == '2026-10-08T00:00:00' and b['finish_date'] == '2026-10-14T00:00:00'


def test_tipo_con_precio_sin_precio_falla_antes_de_llamar_a_ml():
    with pytest.raises(ValueError):
        pm.body_para_sumar('SELLER_CAMPAIGN', 'C-1', deal_price=0)


def test_cupon_no_soportado():
    with pytest.raises(ValueError):
        pm.body_para_sumar('SELLER_COUPON_CAMPAIGN', 'C-2')


# ── fechas ───────────────────────────────────────────────────────────────────

HOY = date(2026, 10, 7)


def test_fechas_ok_14_dias_justos():
    assert pm.validar_fechas('2026-10-07', '2026-10-20', HOY)


@pytest.mark.parametrize('desde,hasta', [
    ('2026-10-06', '2026-10-10'),   # empieza ayer
    ('2026-10-10', '2026-10-09'),   # termina antes de empezar
    ('2026-10-07', '2026-10-21'),   # 15 días
    ('mañana', '2026-10-09'),
])
def test_fechas_invalidas(desde, hasta):
    with pytest.raises(ValueError):
        pm.validar_fechas(desde, hasta, HOY)


# ── paginación y reintento ───────────────────────────────────────────────────

def test_items_pagina_con_search_after():
    paginas = {None: {'results': [{'id': 'A'}], 'search_after': 'x1'},
               'x1': {'results': [{'id': 'B'}], 'search_after': 'x2'},
               'x2': {'results': [{'id': 'C'}]}}
    c = FakeClient({'/seller-promotions/promotions/P1/items':
                    lambda p: paginas[p.get('search_after')]})
    assert [i['id'] for i in pm.items_de_promocion(c, 'P1', 'DEAL')] == ['A', 'B', 'C']


def test_sumar_reintenta_una_vez_si_ml_bloquea_el_item(monkeypatch):
    monkeypatch.setattr(pm.time, 'sleep', lambda s: None)
    c = FakeClient(post_errors=[MLApiError('locked', 423)])
    assert pm.sumar_item(c, 'MLA1', {'deal_price': 10})['price'] == 10
    assert len(c.posts) == 2


def test_sumar_no_reintenta_otros_errores():
    c = FakeClient(post_errors=[MLApiError('bad', 400)])
    with pytest.raises(MLApiError):
        pm.sumar_item(c, 'MLA1', {'deal_price': 10})
    assert len(c.posts) == 1


# ── margen ───────────────────────────────────────────────────────────────────

def test_margen_con_precio_de_la_promo():
    items = [{'id': 'MLA1', 'status': 'candidate', 'price': 0, 'original_price': 10000,
              'suggested_discounted_price': 8000}]
    det = {'MLA1': {'title': 'Faja', 'listing_type_id': 'gold_special', 'available_quantity': 5}}
    out = pm.enriquecer(items, det, {'MLA1': {'costo': 4000}}, lambda lt: 0.30)[0]
    # 10000*0.7-4000 = 3000 → 30%; 8000*0.7-4000 = 1600 → 20%
    assert out['margen_lista_pct'] == 30.0
    assert out['margen_promo_pct'] == 20.0
    assert out['sugerido'] == 8000


def test_sin_costo_no_inventa_margen():
    items = [{'id': 'MLA2', 'status': 'candidate', 'price': 0, 'original_price': 1000}]
    out = pm.enriquecer(items, {}, {}, lambda lt: 0.3)[0]
    assert out['margen_promo_pct'] is None and out['costo'] is None


def test_error_legible_saca_el_mensaje_de_ml():
    e = MLApiError('POST /x failed: {"message":"Errors","cause":[{"error_message":"The discounted price is not credible."}]}', 400)
    assert pm.error_legible(e) == 'The discounted price is not credible.'


# ── repricing ────────────────────────────────────────────────────────────────

def test_repricing_se_frena_si_el_item_esta_en_promo():
    c = FakeClient({'/seller-promotions/items/MLA1': [
        {'type': 'PRICE_DISCOUNT', 'status': 'candidate'},
        {'type': 'DEAL', 'status': 'started', 'name': 'Hot Sale'}]})
    assert repricing._promo_que_lo_bloquea(c, 'MLA1') == 'Hot Sale'


def test_repricing_sigue_si_solo_es_candidato():
    c = FakeClient({'/seller-promotions/items/MLA1': [{'type': 'DEAL', 'status': 'candidate'}]})
    assert repricing._promo_que_lo_bloquea(c, 'MLA1') == ''


def test_repricing_no_se_frena_si_falla_la_consulta():
    class Roto(FakeClient):
        def _get(self, path, params=None):
            raise MLApiError('forbidden', 403)
    assert repricing._promo_que_lo_bloquea(Roto(), 'MLA1') == ''


# ── plantillas ───────────────────────────────────────────────────────────────

def test_plantilla_guarda_porcentaje_y_se_borra(monkeypatch):
    store = {}
    monkeypatch.setattr(pm, 'db_load', lambda p: store.get(p))
    monkeypatch.setattr(pm, 'db_save', lambda p, d: store.__setitem__(p, d))
    p = pm.guardar_plantilla('Cuenta 1', 'Hot Sale', 'DEAL',
                             [{'id': 'MLA1', 'titulo': 'Faja', 'descuento_pct': 12.345},
                              {'titulo': 'sin id'}])
    assert p['items'] == [{'id': 'MLA1', 'titulo': 'Faja', 'descuento_pct': 12.35}]
    assert pm.cargar_plantillas('Cuenta 1')[0]['id'] == p['id']
    assert pm.borrar_plantilla('Cuenta 1', p['id'])
    assert pm.cargar_plantillas('Cuenta 1') == []


def test_cofondeada_el_margen_usa_lo_que_cobra_el_vendedor():
    # Lista 10000, ML pone 5% y el vendedor 15%: el comprador paga 8000 pero
    # el vendedor cobra 8500. 8500*0.7-4000 = 1950 → 22.9%
    items = [{'id': 'MLA1', 'status': 'candidate', 'price': 8000, 'original_price': 10000,
              'meli_percentage': 5, 'seller_percentage': 15}]
    out = pm.enriquecer(items, {}, {'MLA1': {'costo': 4000}}, lambda lt: 0.30)[0]
    assert out['margen_promo_pct'] == 22.9


@pytest.mark.parametrize('malo', ['../../items/MLA1', 'MLA1/../x', 'MLA1?x=1', '', None])
def test_ids_con_path_injection_se_rechazan_antes_de_llamar_a_ml(malo):
    c = FakeClient()
    with pytest.raises(ValueError):
        pm.sumar_item(c, malo, {'deal_price': 1})
    with pytest.raises(ValueError):
        pm.items_de_promocion(c, malo, 'DEAL')
    assert c.posts == [] and c.get_calls == []


def test_completar_limites_toma_tope_y_sugerido_del_item():
    class C:
        def _get(self, path, params=None):
            return [{'type': 'DEAL', 'max_discounted_price': 1},
                    {'type': 'LIGHTNING', 'max_discounted_price': 67500,
                     'suggested_discounted_price': 63750, 'original_price': 75000}]
    crudos = [{'id': 'MLA1', 'status': 'candidate', 'price': 68480, 'original_price': 72000},
              {'id': 'MLA2', 'status': 'pending', 'price': 14250}]
    pm.completar_limites(C(), crudos, 'LIGHTNING')
    assert crudos[0]['max_discounted_price'] == 67500
    assert crudos[0]['original_price'] == 75000
    assert 'max_discounted_price' not in crudos[1]
    # el precio por defecto pasa a ser el sugerido de ML, no el "price" de la lista
    e = pm.enriquecer(crudos[:1], {}, {}, lambda lt: 0.2)[0]
    assert e['sugerido'] == 63750 and e['max_price'] == 67500
