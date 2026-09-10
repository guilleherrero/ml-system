"""El barrido de keywords contra autosuggest — la mitad de Cerebro que sirve el dia 1.

Las sugerencias del fixture son reales, tomadas de autosuggest MLA el 09/09/2026
para las semillas "cortador puntas" y "cortador pelo puntas".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import defensa_publicacion as dp          # noqa: E402
from modules import diagnostico_keywords as dk         # noqa: E402

SUGERENCIAS = {
    'cortador puntas': [
        'Cortador puntas', 'Cortador puntas abiertas', 'Cortador puntas danadas',
        'Cortador puntas abiertas ender pro', 'Cortador puntas cabello',
        'Cortador puntas pelo', 'Cortador puntas papel',
        'Cortador puntas florecidas', 'Cepillo cortador puntas abiertas',
        'Cepillo cortador puntas',
    ],
    'cortador pelo puntas': ['Cortador pelo puntas abiertas'],
}
UNIVERSO = dp.universo_keywords(SUGERENCIAS)


def _item(**kw):
    base = {'id': 'MLA1', 'titulo': 'Cortador De Pelo Para Puntas Abiertas',
            'visitas_30d': 400, 'ventas_30d': 20, 'precio': 75000,
            'margen_pct': 0.44}
    base.update(kw)
    return base


def test_las_semillas_son_frases_cortas_no_el_titulo_entero():
    sem = dk.semillas_de('Cortador De Pelo Para Puntas Abiertas Ender Pro | Negro')
    assert sem, 'tiene que devolver al menos una semilla'
    assert all(len(s.split()) <= 3 for s in sem), sem
    assert 'de' not in ' '.join(sem).split(), 'las stopwords no van a autosuggest'


def test_detecta_el_error_de_escritura_que_apaga_una_busqueda():
    # El caso real: "Abriertas" por "abiertas"
    d = dk.diagnosticar_item(_item(titulo='Cortador Puntas Abriertas Ender Pro'), UNIVERSO)
    errores = d['errores_escritura']
    assert errores, 'tiene que ver el typo'
    assert errores[0]['escrito'] == 'abriertas'
    assert errores[0]['deberia_ser'] == 'abiertas'
    assert errores[0]['cuantas'] >= 3, errores[0]
    tipos = [a['tipo'] for a in d['acciones']]
    assert tipos[0] == 'corregir_error_escritura', 'lo mas barato va primero'


def test_el_titulo_congelado_manda_la_correccion_a_la_descripcion():
    con_ventas = dk.diagnosticar_item(_item(titulo='Cortador Puntas Abriertas', ventas_30d=20), UNIVERSO)
    sin_ventas = dk.diagnosticar_item(_item(titulo='Cortador Puntas Abriertas', ventas_30d=0), UNIVERSO)
    assert con_ventas['titulo_editable'] is False
    assert sin_ventas['titulo_editable'] is True
    assert con_ventas['acciones'][0]['aplicar_en'] == 'descripcion'
    assert sin_ventas['acciones'][0]['aplicar_en'] == 'titulo'


def test_encuentra_las_palabras_que_mas_rinde_sumar():
    d = dk.diagnosticar_item(_item(titulo='Cortador De Pelo Puntas'), UNIVERSO)
    palabras = [p['palabra'] for p in d['palabras_que_faltan']]
    assert 'abiertas' in palabras, palabras
    assert d['cobertura_pct'] < 100


def test_la_ficha_y_la_descripcion_cuentan_pero_menos_que_el_titulo():
    solo_titulo = dk.diagnosticar_item(_item(titulo='Cortador De Pelo Puntas'), UNIVERSO)
    con_desc = dk.diagnosticar_item(_item(titulo='Cortador De Pelo Puntas'), UNIVERSO,
                                    descripcion='puntas abiertas danadas florecidas cabello')
    assert con_desc['cobertura_pct'] > solo_titulo['cobertura_pct']
    assert con_desc['cobertura_pct'] < 100, 'estar en la descripcion no vale como estar en el titulo'


def test_no_pone_plata_cuando_hay_poco_trafico():
    d = dk.diagnosticar_item(_item(titulo='Cortador Puntas', visitas_30d=12, ventas_30d=1), UNIVERSO)
    assert d['impacto_mensual_ars'] is None
    assert 'visitas' in d['no_valuado_porque']


def test_no_pone_plata_sin_costo_cargado():
    d = dk.diagnosticar_item(_item(titulo='Cortador Puntas', margen_pct=None), UNIVERSO)
    assert d['impacto_mensual_ars'] is None
    assert 'costo' in d['no_valuado_porque']


def test_no_pone_plata_sin_ventas_propias():
    d = dk.diagnosticar_item(_item(titulo='Cortador Puntas', ventas_30d=0), UNIVERSO)
    assert d['impacto_mensual_ars'] is None
    assert 'conversion' in d['no_valuado_porque']


def test_valua_cuando_estan_los_tres_datos_y_es_auditable():
    d = dk.diagnosticar_item(_item(titulo='Cortador De Pelo Puntas'), UNIVERSO)
    assert d['impacto_mensual_ars'] > 0
    # Reproducir la cifra a mano: la formula tiene que cerrar
    esperado = (d['visitas_recuperables'] * (20 / 400) * (75000 * 0.44))
    assert abs(d['impacto_mensual_ars'] - esperado) < 1.0, (d['impacto_mensual_ars'], esperado)
    assert 'realismo' in d['formula']


def test_el_tope_de_sensatez_frena_la_extrapolacion_lineal():
    # Cobertura pesima: sin tope, la regla lineal prometeria multiplicar visitas
    d = dk.diagnosticar_item(_item(titulo='Cortador De Pelo Puntas'), UNIVERSO)
    assert d['cobertura_pct'] < 30, d['cobertura_pct']
    assert d['topeado_por_sensatez'] is True
    assert d['visitas_recuperables'] == 400 * dk.TOPE_VISITAS_RECUPERABLES


def test_una_publicacion_bien_cubierta_no_genera_ruido():
    titulo = 'Cortador Puntas Abiertas Danadas Florecidas Cabello Pelo Ender Pro Cepillo Papel'
    d = dk.diagnosticar_item(_item(titulo=titulo), UNIVERSO)
    assert d['cobertura_pct'] >= dk.COBERTURA_SANA_PCT, d['cobertura_pct']
    assert not any(a['tipo'] == 'sumar_keywords' for a in d['acciones']), \
        'con cobertura sana no hay que molestar'


def test_sin_autosuggest_lo_dice_en_vez_de_inventar():
    d = dk.diagnosticar_item(_item(), [])
    assert d['sin_datos'] is True
    assert 'autosuggest' in d['motivo']


def test_la_ficha_vacia_sale_como_accion_gratis():
    d = dk.diagnosticar_item(_item(), UNIVERSO,
                             atributos_vacios=['MARCA', 'MODELO', 'TIPO_DE_CABELLO'])
    ficha = [a for a in d['acciones'] if a['tipo'] == 'completar_ficha']
    assert ficha and ficha[0]['costo'] == 'gratis'
    assert 'MARCA' in ficha[0]['texto']
