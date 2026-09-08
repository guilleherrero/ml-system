"""
Tests del diagnostico diferencial de competencia.

Lo que se prueba no es que el codigo corra: es que no confunda las causas. Un
diagnostico equivocado hace bajar el precio cuando el problema era el titulo, y
eso es margen regalado sin arreglar nada.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import competencia_diagnostico as cd

MIO = {
    'titulo': 'Cortador De Puntas Abriertas Para Cabello Dañado - Ender Pro',
    'precio': 60578, 'costo': 22000, 'fee_rate': 0.2849, 'fotos': 6,
    'ventas': 14, 'rating': 4.6, 'cuotas_max': 12, 'free_shipping': True,
    'cuotas_breakdown': {'1': 55, '2-3': 25, '4-6': 15, '7-12': 5},
}


def competidor(**kw):
    base = {'titulo': 'Cortador De Puntas Abiertas Cabello Ender Pro',
            'precio': 60000, 'fotos': 6, 'ventas': 14, 'rating': 4.6,
            'cuotas_max': 12, 'free_shipping': True}
    base.update(kw)
    return base


class TestComparacionTitulos(unittest.TestCase):

    def test_encuentra_lo_que_el_dice_y_yo_no(self):
        r = cd.comparar_titulos('cortador de puntas cabello',
                                'cortadora puntas florecidas split ender recargable')
        self.assertIn('florecidas', r['keywords_que_el_tiene_y_yo_no'])
        self.assertIn('recargable', r['keywords_que_el_tiene_y_yo_no'])

    def test_ignora_palabras_que_no_distinguen(self):
        r = cd.comparar_titulos('cortador de puntas', 'cortador de puntas con envio gratis')
        self.assertNotIn('envio', r['keywords_que_el_tiene_y_yo_no'])
        self.assertNotIn('gratis', r['keywords_que_el_tiene_y_yo_no'])

    def test_prioriza_lo_que_la_gente_busca(self):
        r = cd.comparar_titulos(
            'cortador de puntas cabello',
            'cortadora puntas florecidas premium deluxe importado',
            keywords_buscadas=['puntas florecidas'])
        self.assertEqual(r['faltantes_que_la_gente_busca'], ['florecidas'])
        self.assertIn('deluxe', r['keywords_que_el_tiene_y_yo_no'])


class TestDiagnostico(unittest.TestCase):

    def test_perder_visitas_y_posicion_es_keywords(self):
        """No te encuentran: el problema es el posicionamiento, no el precio."""
        d = cd.diagnosticar(
            MIO, competidor(titulo='Cortadora Puntas Florecidas Split Ender Recargable'),
            delta_visitas_pct=-38, delta_conversion_pct=-4,
            delta_posicion=7, delta_demanda_pct=-2)
        self.assertEqual(d['causa_principal'], 'keywords')
        self.assertTrue(any(a['tipo'] == 'ficha' for a in d['acciones']))
        # No debe recomendar tocar el precio
        self.assertFalse(any(a['tipo'] == 'precio' for a in d['acciones']))

    def test_conversion_cae_con_competidor_barato_es_precio(self):
        """Te encuentran igual, pero eligen al mas barato."""
        d = cd.diagnosticar(MIO, competidor(precio=48000),
                            delta_visitas_pct=-3, delta_conversion_pct=-45,
                            delta_posicion=0, delta_demanda_pct=1)
        self.assertEqual(d['causa_principal'], 'precio')
        self.assertEqual(d['confianza'], 'alta')
        self.assertGreater(d['brecha_precio_pct'], 15)

    def test_precio_parecido_pero_pierde_conversion_es_condiciones(self):
        d = cd.diagnosticar(
            MIO, competidor(precio=60000, fotos=10, ventas=40, rating=4.9, fulfillment=True),
            delta_visitas_pct=-5, delta_conversion_pct=-38, delta_posicion=1)
        self.assertEqual(d['causa_principal'], 'condiciones')
        tipos = {a['tipo'] for a in d['acciones']}
        self.assertTrue({'full', 'fotos'} & tipos)

    def test_si_cayo_el_mercado_no_culpa_al_competidor(self):
        d = cd.diagnosticar(MIO, competidor(),
                            delta_visitas_pct=-30, delta_conversion_pct=-2,
                            delta_posicion=0, delta_demanda_pct=-35)
        self.assertEqual(d['causa_principal'], 'mercado')
        self.assertEqual(d['acciones'][0]['tipo'], 'esperar')

    def test_sin_evidencia_no_inventa_una_causa(self):
        d = cd.diagnosticar(MIO, competidor(),
                            delta_visitas_pct=-2, delta_conversion_pct=-3, delta_posicion=0)
        self.assertEqual(d['causa_principal'], 'sin_causa_clara')
        self.assertEqual(d['acciones'], [])

    def test_siempre_muestra_la_evidencia(self):
        d = cd.diagnosticar(MIO, competidor(precio=48000),
                            delta_visitas_pct=-3, delta_conversion_pct=-45)
        self.assertTrue(d['evidencia'])


class TestAcciones(unittest.TestCase):
    """El orden importa: primero lo que no cuesta margen."""

    def test_ante_precio_ofrece_cuotas_antes_que_bajar(self):
        d = cd.diagnosticar(MIO, competidor(precio=48000),
                            delta_visitas_pct=-3, delta_conversion_pct=-45)
        tipos = [a['tipo'] for a in d['acciones']]
        self.assertIn('cuotas', tipos)
        self.assertIn('precio', tipos)
        self.assertLess(tipos.index('cuotas'), tipos.index('precio'))

    def test_la_accion_de_precio_dice_cuanto_hay_que_vender_mas(self):
        d = cd.diagnosticar(MIO, competidor(precio=48000),
                            delta_visitas_pct=-3, delta_conversion_pct=-45)
        precio_acc = next(a for a in d['acciones'] if a['tipo'] == 'precio')
        self.assertIn('compensacion', precio_acc)
        self.assertIn('%', precio_acc['compensacion'] or '')

    def test_si_igualar_rompe_el_piso_no_se_puede(self):
        """Un competidor por debajo del piso de margen no se persigue."""
        d = cd.diagnosticar(MIO, competidor(precio=26000),
                            delta_visitas_pct=-3, delta_conversion_pct=-50)
        precio_acc = next(a for a in d['acciones'] if a['tipo'] == 'precio')
        self.assertFalse(precio_acc['viable'])
        self.assertTrue(any(a['tipo'] == 'esperar' for a in d['acciones']))

    def test_keywords_avisa_que_el_titulo_puede_estar_congelado(self):
        d = cd.diagnosticar(
            MIO, competidor(titulo='Cortadora Puntas Florecidas Split Ender Recargable'),
            delta_visitas_pct=-38, delta_posicion=7)
        ficha = next(a for a in d['acciones'] if a['tipo'] == 'ficha')
        self.assertIn('titulo', (ficha.get('nota') or ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
