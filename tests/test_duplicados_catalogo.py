"""El eje de catalogo — el falso positivo mas caro que tenia el detector.

La publicacion de catalogo y la tradicional del mismo producto comparten
titulo, precio y condiciones, asi que el detector las agrupaba como duplicado
y el Top 3 llegaba a recomendar pausar una de las dos por $305.947/mes. No son
duplicados: son dos canales con reglas distintas y ML no los sanciona.
Ver CEREBRO.md 3.6.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.detector_duplicados import (  # noqa: E402
    _clave_duplicacion, _subdividir_cluster, _detectar_ejes_diferentes,
)

# El caso real: Cortador, dos publicaciones del mismo producto
TRADICIONAL = {'id': 'MLA1481911017', 'titulo': 'Cortador de pelo',
               'precio': 45000, 'listing_type': 'gold_special',
               'free_shipping': True, 'ventas_30d': 34, 'visitas_30d': 438}
CATALOGO = {'id': 'MLA1932975847', 'titulo': 'Cortador de pelo',
            'precio': 45000, 'listing_type': 'gold_special',
            'free_shipping': True, 'ventas_30d': 22, 'visitas_30d': 1336}

META_TRAD = {'attributes': [], 'installments': {'quantity': 12, 'rate': 0},
             'shipping': {'logistic_type': 'fulfillment'},
             'catalogo': {'catalog_listing': False, 'catalog_product_id': ''}}
META_CAT = {'attributes': [], 'installments': {'quantity': 12, 'rate': 0},
            'shipping': {'logistic_type': 'fulfillment'},
            'catalogo': {'catalog_listing': True, 'catalog_product_id': 'MLA26...'}}


def test_catalogo_y_tradicional_no_son_la_misma_clave():
    assert _clave_duplicacion(TRADICIONAL, META_TRAD) != _clave_duplicacion(CATALOGO, META_CAT)


def test_el_par_no_se_agrupa_como_duplicado():
    metas = {'MLA1481911017': META_TRAD, 'MLA1932975847': META_CAT}
    subs = _subdividir_cluster([TRADICIONAL, CATALOGO], metas)
    assert len(subs) == 2, 'catalogo y tradicional deben quedar separados'
    assert all(len(s) == 1 for s in subs), 'ninguno es duplicado del otro'


def test_el_eje_se_nombra_en_castellano():
    metas = {'MLA1481911017': META_TRAD, 'MLA1932975847': META_CAT}
    subs = _subdividir_cluster([TRADICIONAL, CATALOGO], metas)
    ejes = _detectar_ejes_diferentes(subs, metas)
    assert any('catálogo' in e for e in ejes), ejes


def test_dos_tradicionales_identicas_siguen_siendo_duplicado():
    a = dict(TRADICIONAL, id='MLA1')
    b = dict(TRADICIONAL, id='MLA2')
    metas = {'MLA1': META_TRAD, 'MLA2': META_TRAD}
    subs = _subdividir_cluster([a, b], metas)
    assert len(subs) == 1 and len(subs[0]) == 2


def test_dos_de_catalogo_identicas_siguen_siendo_duplicado():
    a = dict(CATALOGO, id='MLA3')
    b = dict(CATALOGO, id='MLA4')
    metas = {'MLA3': META_CAT, 'MLA4': META_CAT}
    subs = _subdividir_cluster([a, b], metas)
    assert len(subs) == 1 and len(subs[0]) == 2


def test_sin_metadata_no_rompe():
    # Compat: items sin datos de la API caen todos en catalogo=False
    assert len(_clave_duplicacion(TRADICIONAL, None)) == 8
