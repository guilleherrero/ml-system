"""Optimizador IA 2.0: lectura de competidores e informe."""
from modules import cerebro
from modules import optimizador2 as op2


def test_limpiar_ficha_descarta_lo_raro():
    f = op2.limpiar_ficha({
        'title': 'x' * 500, 'price': 'abc', 'cuotas': 99, 'videos': 2, 'photos_count': -1,
        'attributes': [{'name': 'Marca', 'value': 'Biobella'}, {'name': '', 'value': 'y'}, 'basura'],
        'resenas': [{'rating': 9, 'texto': 'ok'}, {'rating': 4, 'texto': ''}] + [{'rating': 5, 'texto': 'z'}] * 100,
        'premium': 'si',
    })
    assert len(f['title']) == 200 and f['price'] is None and f['cuotas'] is None
    assert f['videos'] == 2 and f['photos_count'] is None and f['premium'] is True
    assert f['attributes'] == [{'name': 'Marca', 'value': 'Biobella'}]
    assert f['resenas'][0] == {'rating': None, 'texto': 'ok'} and len(f['resenas']) <= op2.MAX_RESENAS


def test_cuotas_propias():
    assert op2._cuotas_propias({'listing_type_id': 'gold_special', 'tags': ['pcj-co-funded']}) == 0
    assert op2._cuotas_propias({'listing_type_id': 'gold_pro', 'tags': []}) == 6
    assert op2._cuotas_propias({'listing_type_id': 'gold_pro', 'tags': ['12x_campaign']}) == 12


def test_informe_faltantes_y_avisos():
    mio = {'id': 'MLA1', 'title': 'Mio', 'photos_count': 5, 'videos': 0, 'cuotas': 0,
           'attributes': [{'name': 'Batería', 'value': 'Litio'}]}
    comps = [
        {'id': 'MLA2', 'ficha_ts': 'x', 'photos_count': 12, 'videos': 1, 'cuotas': 6,
         'attributes': [{'name': 'bateria', 'value': 'Litio'}, {'name': 'Material', 'value': 'Acero'}]},
        {'id': 'MLA3', 'ficha_ts': 'x', 'photos_count': 8, 'videos': 0, 'publicidad': True,
         'attributes': [{'name': 'Material', 'value': 'Plástico'}]},
        {'id': 'MLA4'},  # sin leer
    ]
    r = op2.informe(mio, comps)
    # "Batería" y "bateria" son el mismo atributo
    assert [f['nombre'] for f in r['faltan_atributos']] == ['Material']
    assert r['faltan_atributos'][0] == {'nombre': 'Material', 'cuantos': 2, 'valores': ['Acero', 'Plástico']}
    assert r['sin_leer'] == 1 and len(r['competidores']) == 3
    texto = ' '.join(r['avisos'])
    assert '1 de 2 competidores tienen video' in texto and 'el que más tiene, 12' in texto
    assert '6 cuotas' in texto and '1 de 3 competidores aparecieron por publicidad' in texto


def test_actualizar_ficha_no_cambia_la_clase(monkeypatch):
    data = {'competidores': {'MLA1::MLA2': {'clave': 'MLA1::MLA2', 'id': 'MLA2',
                                            'clase': cerebro.CLASE_DIRECTO, 'title': 'viejo'}}}
    monkeypatch.setattr(cerebro, '_competidores_raw', lambda alias: data)
    monkeypatch.setattr(cerebro, '_save', lambda *a: True)
    reg = cerebro.actualizar_ficha('x', 'MLA1::MLA2', op2.limpiar_ficha({'title': 'nuevo', 'videos': 0}))
    assert reg['clase'] == cerebro.CLASE_DIRECTO and reg['title'] == 'nuevo'
    assert reg['videos'] == 0 and reg['ficha_ts']
    assert cerebro.actualizar_ficha('x', 'no-existe', {}) is None


def test_informe_no_deja_pasar_texto_en_campos_numericos():
    mio = {'id': 'MLA1', 'attributes': []}
    r = op2.informe(mio, [{'id': 'MLA2', 'reviews_rating': '<img src=x onerror=alert(1)>',
                           'sold_quantity': '<b>', 'price': 'x', 'ficha_ts': 'x'}])
    c = r['competidores'][0]
    assert c['rating'] is None and c['ventas'] is None and c['precio'] is None
