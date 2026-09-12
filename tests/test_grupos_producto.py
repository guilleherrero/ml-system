"""Grupos de producto — un analisis de competencia por producto, no por publicacion.

Los titulos de los fixtures son los reales del catalogo de Novara: seis
publicaciones del mismo cortador de puntas, que hoy obligaban a cargar los
mismos competidores seis veces.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import cerebro                      # noqa: E402
from modules import grupos_producto as gp        # noqa: E402

CORTADORES = [
    {'id': 'MLA1932975847', 'titulo': 'Cortador De Puntas Abriertas Para Cabello Dañado - Ender Pro Negro'},
    {'id': 'MLA1481911017', 'titulo': 'Cortador De Puntas Abiertas Para Cabello Dañado - Ender Pro'},
    {'id': 'MLA3160653670', 'titulo': 'Cortador De Pelo Para Puntas Abiertas | Negro'},
    {'id': 'MLA3160653672', 'titulo': 'Cortador De Pelo Para Puntas Abiertas | Rojo'},
]
OTRO = {'id': 'MLA999', 'titulo': 'Faja Reductora Postparto Cesarea Abdominal Mujer'}


def test_el_color_no_separa_publicaciones_del_mismo_producto():
    assert gp.parecido(CORTADORES[2]['titulo'], CORTADORES[3]['titulo']) == 1.0, \
        'negro y rojo son el mismo producto'


def test_agrupa_los_cortadores_y_deja_afuera_lo_que_no_es():
    grupos = gp.sugerir_grupos(CORTADORES + [OTRO])
    cortador = next(g for g in grupos if 'MLA3160653670' in g['items'])
    assert len(cortador['items']) >= 2
    assert 'MLA999' not in cortador['items'], 'la faja no es un cortador'
    assert any(g['items'] == ['MLA999'] for g in grupos)


def test_el_nombre_del_grupo_es_lo_que_comparten():
    grupos = gp.sugerir_grupos(CORTADORES[2:])
    nombre = grupos[0]['nombre'].lower()
    assert 'cortador' in nombre and 'puntas' in nombre
    assert 'negro' not in nombre and 'rojo' not in nombre, \
        'el color distingue variantes, no nombra el producto'


class _Alias:
    def __init__(self):
        self.alias = f'TestGrupos_{uuid.uuid4().hex[:8]}'


def test_un_item_pertenece_a_un_solo_grupo():
    a = _Alias().alias
    gp.guardar_grupo(a, grupo_id=None, nombre='Cortadores', items=['MLA1', 'MLA2'])
    gp.guardar_grupo(a, grupo_id=None, nombre='Otros', items=['MLA2', 'MLA3'])
    grupos = gp.listar_grupos(a)
    cuantos = sum(1 for g in grupos if 'MLA2' in g['items'])
    assert cuantos == 1, 'estar en dos grupos significaria dos analisis contradictorios'
    assert sorted(g['nombre'] for g in grupos) == ['Cortadores', 'Otros']


def test_sin_grupo_la_publicacion_es_su_propio_grupo():
    a = _Alias().alias
    assert gp.hermanas_de(a, 'MLA7') == ['MLA7'], 'nunca devuelve vacio'


def test_los_competidores_se_cargan_una_vez_y_los_ve_todo_el_grupo():
    a = _Alias().alias
    gp.guardar_grupo(a, grupo_id=None, nombre='Cortadores',
                     items=['MLA1932975847', 'MLA3160653670', 'MLA3160653672'])
    # Se captura contra UNA sola de las hermanas
    cerebro.guardar_competidor(
        a, {'id': 'MLA555', 'title': 'Cortador rival', 'price': 30000},
        item_propio='MLA3160653670', clase=cerebro.CLASE_DIRECTO, puntaje=0.9)

    # ...y lo ven las otras dos
    for hermana in ('MLA1932975847', 'MLA3160653672'):
        vistos = [c['id'] for c in gp.competidores_del_grupo(a, hermana)]
        assert 'MLA555' in vistos, f'{hermana} tendria que ver el competidor del grupo'
    assert [c['id'] for c in gp.directos_del_grupo(a, 'MLA3160653672')] == ['MLA555']


def test_no_se_mezclan_competidores_entre_productos_distintos():
    a = _Alias().alias
    gp.guardar_grupo(a, grupo_id=None, nombre='Cortadores', items=['MLA1'])
    gp.guardar_grupo(a, grupo_id=None, nombre='Fajas', items=['MLA2'])
    cerebro.guardar_competidor(a, {'id': 'MLA555', 'title': 'Rival cortador'},
                               item_propio='MLA1', clase=cerebro.CLASE_DIRECTO)
    assert [c['id'] for c in gp.competidores_del_grupo(a, 'MLA1')] == ['MLA555']
    assert gp.competidores_del_grupo(a, 'MLA2') == []


def test_el_mismo_competidor_en_dos_hermanas_se_cuenta_una_vez():
    a = _Alias().alias
    gp.guardar_grupo(a, grupo_id=None, nombre='Cortadores', items=['MLA1', 'MLA2'])
    cerebro.guardar_competidor(a, {'id': 'MLA555', 'title': 'Rival'},
                               item_propio='MLA1', clase=cerebro.CLASE_DIRECTO, puntaje=0.4)
    cerebro.guardar_competidor(a, {'id': 'MLA555', 'title': 'Rival'},
                               item_propio='MLA2', clase=cerebro.CLASE_DIRECTO, puntaje=0.9)
    comps = gp.competidores_del_grupo(a, 'MLA1')
    assert len(comps) == 1
    assert comps[0]['puntaje'] == 0.9, 'se queda con el mejor puntaje'
