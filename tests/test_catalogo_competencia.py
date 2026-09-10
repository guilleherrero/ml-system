"""Competencia dentro de una ficha de catalogo.

Los fixtures salen de la respuesta real de ML para MLA1932975847 el 10/09/2026,
que es donde aparecieron los dos errores que estos tests fijan: el stock leido
del lado equivocado y el "perdiste la buy box" sin rival.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import catalogo_competencia as cc  # noqa: E402


def _datos(**kw):
    base = {
        'ok': True, 'es_catalogo': True, 'item_id': 'MLA1932975847',
        'producto': 'Cortador De Puntas', 'marca_de_la_ficha': 'BIOBELLA',
        'mi_precio': 60578, 'mi_stock': 117, 'mi_status': 'active',
        'ganador_id': '', 'gano_yo': False, 'total_vendedores': 1,
        # ML devuelve stock y vendidos en null en /products/{id}/items
        'vendedores': [{'item_id': 'MLA1932975847', 'seller_id': 55993545,
                        'es_mio': True, 'precio': 60578, 'envio_gratis': True,
                        'logistica': 'xd_drop_off', 'vendidos': None,
                        'stock': None, 'gana_buy_box': False}],
    }
    base.update(kw)
    return base


def test_no_dice_sin_stock_cuando_hay_117_unidades():
    # El listado de la ficha devuelve stock=None; leerlo de ahi reportaba
    # "sin stock" en una publicacion con stock de sobra.
    d = cc.diagnosticar(_datos(), 'Biobella')
    assert not any(h['tipo'] == 'sin_stock' for h in d['hallazgos']), d['hallazgos']


def test_sin_stock_de_verdad_si_lo_dice_la_publicacion():
    d = cc.diagnosticar(_datos(mi_stock=0), 'Biobella')
    assert any(h['tipo'] == 'sin_stock' for h in d['hallazgos'])


def test_con_un_solo_vendedor_no_hay_buy_box_que_perder():
    d = cc.diagnosticar(_datos(), 'Biobella')
    tipos = [h['tipo'] for h in d['hallazgos']]
    assert 'unico_vendedor' in tipos
    assert 'buy_box_perdida' not in tipos
    assert 'buy_box_sin_ganador_visible' not in tipos, \
        'no se manda a competir contra un rival que no existe'


def test_un_ajeno_en_ficha_de_marca_propia_es_critico():
    d = cc.diagnosticar(_datos(total_vendedores=2, vendedores=[
        {'item_id': 'MLA1932975847', 'seller_id': 55993545, 'es_mio': True,
         'precio': 60578, 'envio_gratis': True, 'logistica': 'xd_drop_off',
         'vendidos': 300, 'stock': 10, 'gana_buy_box': False},
        {'item_id': 'MLA999', 'seller_id': 111, 'es_mio': False,
         'precio': 55000, 'envio_gratis': True, 'logistica': 'fulfillment',
         'vendidos': 4, 'stock': 3, 'gana_buy_box': True},
    ]), 'Biobella')
    assert d['gravedad'] == 'critica'
    assert d['hallazgos'][0]['tipo'] == 'intruso_en_marca_propia'
    buybox = next(h for h in d['hallazgos'] if h['tipo'] == 'buy_box_perdida')
    assert 'mas barato' in buybox['texto'] and 'Full' in buybox['texto']
    assert 'vendedor nuevo' in buybox['texto'], 'con 4 ventas es alguien que recien aparece'


def test_una_publicacion_que_no_es_de_catalogo_no_genera_hallazgos():
    d = cc.diagnosticar({'ok': True, 'es_catalogo': False}, 'Biobella')
    assert d['hallazgos'] == [] and d['gravedad'] == 'ninguna'
