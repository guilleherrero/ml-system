"""Importador como plantilla: que de la pagina ajena solo se tome lo debido,
y que no se publique nada incompleto, sin fotos propias o debajo del piso."""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import importador as imp  # noqa: E402
from modules import precio_motor as pm  # noqa: E402
from modules import duplicador as dup  # noqa: E402

TASAS = {0: {'comision': 16, 'costo_cuotas': 0}, 4: {'comision': 16, 'costo_cuotas': 5},
         6: {'comision': 16, 'costo_cuotas': 13.4}}
CARGOS = pm.Cargos(iibb=3, percepcion_iva=7)

CAPTURA_UP = {   # pagina /up/ real (2026-10-18): trae en el HTML un catalog_product_id de OTRO producto
    'url': 'https://www.mercadolibre.com.ar/cortadora-puntas-abiertas-recargable-sin-cable/up/MLAU4258780773',
    'titulo': 'Cortadora Puntas Abiertas Recargable Sin Cable', 'precio': '20844.13',
    'specs': [['Marca', 'Nobrand'], ['Modelo', 'AW-888'], ['Color', 'Violeta oscuro'], ['', 'x'], 'mal'],
    'fotos': ['https://http2.mlstatic.com/D_Q_NP_877065-CBT114354627809_072026-R.webp', 'https://otro.com/x.jpg',
              'data:image/png;base64,AAA'],
    'categoria_pagina': 'MLA5411', 'catalog_product_id': 'MLA2080523428',
    'descripcion': 'Cortadora Vivamia recargable. Consultas al 11 4567 8901.'}


class TestCaptura(unittest.TestCase):

    def setUp(self):
        self.alias = f'TestImp_{uuid.uuid4().hex[:8]}'

    def tearDown(self):
        p = imp._path(self.alias)
        if os.path.exists(p):
            os.remove(p)

    def test_limpia_lo_que_viene_de_la_pagina(self):
        c = imp.limpiar_captura(CAPTURA_UP)
        self.assertEqual(c['precio'], 20844.13)
        self.assertEqual(c['fotos'], [CAPTURA_UP['fotos'][0]])                    # solo mlstatic https
        self.assertEqual(c['specs'], [['Marca', 'Nobrand'], ['Modelo', 'AW-888'], ['Color', 'Violeta oscuro']])
        self.assertIsNone(c['catalog_product_id'])          # en /up/ no se confia en el id del HTML
        self.assertEqual(c['user_product_id'], 'MLAU4258780773')
        cat = imp.limpiar_captura({**CAPTURA_UP, 'url': 'https://www.mercadolibre.com.ar/vivamia-cortadora/p/MLA79596492'})
        self.assertEqual(cat['catalog_product_id'], 'MLA79596492')    # en /p/ si: es la pagina del catalogo
        for mala in ({**CAPTURA_UP, 'url': 'https://evil.com/mercadolibre.com.ar/'}, {**CAPTURA_UP, 'titulo': ''}):
            with self.assertRaises(imp.ImportadorError):
                imp.limpiar_captura(mala)

    def test_no_duplica_el_borrador_de_la_misma_pagina(self):
        a = imp.capturar(self.alias, {**CAPTURA_UP, 'descripcion': ''})
        imp.guardar_edicion(self.alias, a['id'], {'family_name': 'Mio', 'escalon': 0})
        b = imp.capturar(self.alias, {**CAPTURA_UP, 'url': CAPTURA_UP['url'] + '#reviews'})
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(len(imp.listar(self.alias)), 1)
        self.assertTrue(b['origen']['descripcion'])                 # se refresca lo leido
        self.assertEqual(b['edicion']['family_name'], 'Mio')         # sin tocar la edicion

    def test_eliminar_la_saca_de_la_lista(self):
        b = imp.capturar(self.alias, CAPTURA_UP)
        imp.eliminar(self.alias, b['id'])
        self.assertEqual(imp.listar(self.alias), [])
        with self.assertRaises(imp.ImportadorError):
            imp.eliminar(self.alias, b['id'])

    def test_editar_valida(self):
        b = imp.capturar(self.alias, CAPTURA_UP)
        e = imp.guardar_edicion(self.alias, b['id'], {'family_name': ' Cortadora  X ', 'category_id': 'MLA5411',
                                                       'atributos': {'BRAND': 'Novara', 'mal id': 'x', 'MODEL': ''},
                                                       'escalon': '6', 'precio': '50000', 'costo': '13000', 'stock': '0'})['edicion']
        self.assertEqual((e['family_name'], e['atributos'], e['escalon'], e['stock']), ('Cortadora X', {'BRAND': 'Novara'}, 6, 1))
        with self.assertRaises(imp.ImportadorError):
            imp.guardar_edicion(self.alias, b['id'], {'escalon': 5})


class TestFichaYPublicacion(unittest.TestCase):
    CAMPOS = [{'id': 'BRAND', 'nombre': 'Marca', 'obligatorio': True, 'tipo': 'string', 'valores': []},
              {'id': 'MODEL', 'nombre': 'Modelo', 'obligatorio': True, 'tipo': 'string', 'valores': []},
              {'id': 'BATTERY_VOLTAGE', 'nombre': 'Voltaje de la batería', 'obligatorio': False, 'tipo': 'string', 'valores': []}]

    def _b(self, **ed):
        base = {'family_name': 'Cortadora Puntas Abiertas', 'category_id': 'MLA5411', 'atributos': {'BRAND': 'Novara', 'MODEL': 'X1'},
                'escalon': 4, 'precio': 50999, 'costo': 13000, 'stock': 5, 'garantia_tipo': 'Garantía del vendedor',
                'garantia_tiempo': '30 días', 'descripcion': 'Texto propio'}
        base.update(ed)
        return {'origen': imp.limpiar_captura(CAPTURA_UP), 'edicion': base}

    def test_precarga_por_nombre_sin_identidad_ajena(self):
        specs = [['Marca', 'Vivamia'], ['Modelo', 'EA0054'], ['Voltaje de la bateria', '5V']]
        self.assertEqual(imp.precargar_atributos(self.CAMPOS, specs), {'BATTERY_VOLTAGE': '5V'})   # sin tilde tambien matchea
        self.assertEqual(imp.referencia_identidad(self.CAMPOS, specs), {'BRAND': 'Vivamia', 'MODEL': 'EA0054'})

    def test_no_publica_con_la_marca_del_otro_en_el_nombre(self):
        ev = dup.evaluar_precio(50999, 4, TASAS, CARGOS, 13000)
        foto = [{'nombre': 'a.jpg', 'mime': 'image/jpeg', 'contenido': b'x'}]
        b = self._b(family_name='Vivamia Cortadora Puntas Abiertas')
        b['origen']['specs'] = [['Marca', 'Vivamia']]
        self.assertTrue(any('Vivamia' in f for f in imp.validar_propia(b, self.CAMPOS, foto, ev)))
        b['edicion']['atributos']['BRAND'] = 'Vivamia'          # si es SU marca (revende Vivamia) se permite
        self.assertFalse(any('Vivamia' in f for f in imp.validar_propia(b, self.CAMPOS, foto, ev)))

    def test_no_publica_incompleto(self):
        ev = dup.evaluar_precio(50999, 4, TASAS, CARGOS, 13000)
        foto = [{'nombre': 'a.jpg', 'mime': 'image/jpeg', 'contenido': b'x'}]
        self.assertEqual(imp.validar_propia(self._b(), self.CAMPOS, foto, ev), [])
        faltas = imp.validar_propia(self._b(atributos={'BRAND': 'Novara'}, descripcion='Llamanos al 11 4567-8901'), self.CAMPOS, [], ev)
        self.assertTrue(any('Modelo' in f for f in faltas))
        self.assertTrue(any('foto tuya' in f for f in faltas))               # las ajenas no cuentan
        self.assertTrue(any('teléfono' in f for f in faltas))
        bajo = dup.evaluar_precio(20000, 4, TASAS, CARGOS, 13000)
        self.assertTrue(any('piso' in f for f in imp.validar_propia(self._b(precio=20000), self.CAMPOS, foto, bajo)))
        self.assertTrue(any('costo' in f for f in imp.validar_propia(self._b(costo=None), self.CAMPOS, foto, None)))

    def test_payload_user_product_y_catalogo(self):
        b = self._b()
        p = imp.payload_propia(b, es_up=True, foto_ids=['P1', 'P2'])
        self.assertEqual(p['family_name'], 'Cortadora Puntas Abiertas')
        self.assertNotIn('title', p)                                         # cuenta UP: ML arma el titulo
        self.assertEqual((p['listing_type_id'], p['tags']), ('gold_special', ['pcj-co-funded']))
        self.assertEqual(p['pictures'], [{'id': 'P1'}, {'id': 'P2'}])
        self.assertNotIn('description', p)
        self.assertIn('title', imp.payload_propia(b, es_up=False, foto_ids=['P1']))
        b['origen']['catalog_product_id'] = 'MLA79596492'
        c = imp.payload_catalogo(b, 'MLA5411')
        self.assertEqual((c['catalog_product_id'], c['catalog_listing']), ('MLA79596492', True))
        self.assertNotIn('pictures', c)

    def test_categoria_del_vendedor_va_primero(self):
        from unittest import mock
        b = {'origen': imp.limpiar_captura(CAPTURA_UP)}
        ml = [{'id': 'MLA417014', 'nombre': 'Puntas de Destornillador', 'domain_id': 'X'},
              {'id': 'MLA5411', 'nombre': 'Cortadoras de Pelo', 'domain_id': 'Y'}]
        with mock.patch.object(imp, 'sugerir_categorias', return_value=ml):
            sug = imp.sugerencias_categoria(b, lambda c: 'Cortadoras de Pelo')
        self.assertEqual([(s['id'], s['origen']) for s in sug], [('MLA5411', 'vendedor'), ('MLA417014', 'ml')])

    def test_posibles_duplicados(self):
        propios = [{'id': 'A', 'title': 'Cortadora De Puntas Abiertas Recargable', 'category_id': 'MLA5411'},
                   {'id': 'B', 'title': 'Cortadora De Puntas Abiertas Recargable', 'category_id': 'MLA999'},
                   {'id': 'C', 'title': 'Faja Postparto', 'category_id': 'MLA5411'}]
        self.assertEqual([d['id'] for d in imp.posibles_duplicados(propios, 'Cortadora Puntas Abiertas Recargable Usb', 'MLA5411')], ['A'])


class TestDescripcion(unittest.TestCase):

    def _b(self, desc, marca='Novara'):
        o = imp.limpiar_captura({**CAPTURA_UP, 'specs': [['Marca', 'Vivamia']]})
        return {'origen': o, 'edicion': {'family_name': 'Cortadora', 'category_id': 'MLA5411', 'atributos': {'BRAND': marca},
                                         'escalon': 0, 'precio': 50000, 'costo': 10000, 'stock': 1, 'descripcion': desc}}

    def test_bloquea_contacto_y_marca_ajena_en_la_descripcion(self):
        foto = [{'nombre': 'a.jpg', 'mime': 'image/jpeg', 'contenido': b'x'}]
        faltas = imp.validar_propia(self._b(CAPTURA_UP['descripcion']), [], foto, None)
        self.assertTrue(any('teléfono' in f for f in faltas))
        self.assertTrue(any('Vivamia' in f and 'descripción' in f for f in faltas))
        self.assertFalse(any('Vivamia' in f for f in imp.validar_propia(self._b('Cortadora Vivamia', marca='Vivamia'), [], foto, None)))

    def test_avisa_si_queda_casi_igual(self):
        b = self._b(CAPTURA_UP['descripcion'])
        self.assertGreaterEqual(imp.parecido_descripcion(b), 0.99)
        self.assertTrue(imp.avisos_propia(b))
        self.assertEqual(imp.avisos_propia(self._b('Cortadora recargable USB de uso personal, liviana y silenciosa.')), [])

    def test_textos_largos_no_cuelgan(self):
        import time
        largo = ' '.join(f'palabra{i % 977} texto{i % 131}' for i in range(25000))   # ~50.000 caracteres o mas
        b = self._b(largo)
        b['origen']['descripcion'] = largo[::-1]
        t0 = time.time()
        imp.parecido_descripcion(b)
        self.assertLess(time.time() - t0, 1.0)


if __name__ == '__main__':
    unittest.main()
