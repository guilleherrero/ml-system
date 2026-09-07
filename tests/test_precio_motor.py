"""
Tests del motor unico de precio (bloque 12.2).

Estas reglas deciden a que precio se vende. Un error aca no ensucia un reporte:
cambia lo que el cliente paga y lo que queda de margen. Por eso van con numeros
reales de la cuenta Novara y con el caso concreto que motivo el cambio.

Correr:  python3 tests/test_precio_motor.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import precio_motor as pm


# Faja Reductora MLA2209044108 — el caso que el Top 3 proponia bajar 8%
FAJA = {'precio': 29000.0, 'costo': 8000.0, 'fee_rate': 0.3259,
        'visitas_30d': 132, 'conv': 0.76, 'avg_conv': 2.32, 'ventas_30d': 1}


class TestMargen(unittest.TestCase):

    def test_margen_descuenta_comision_y_costo(self):
        m = pm.margen_unitario(29000, 8000, 0.3259)
        self.assertAlmostEqual(m, 29000 * (1 - 0.3259) - 8000, places=2)

    def test_sin_costo_no_inventa(self):
        self.assertIsNone(pm.margen_unitario(29000, None, 0.3259))
        self.assertIsNone(pm.precio_piso(None, 0.3259))


class TestPisoDePrecio(unittest.TestCase):
    """El piso viejo de top_acciones permitia margen -10%: vender perdiendo."""

    def test_el_piso_deja_margen_positivo(self):
        piso = pm.precio_piso(8000, 0.3259)
        margen = pm.margen_pct(piso, 8000, 0.3259)
        self.assertAlmostEqual(margen, pm.MARGEN_MINIMO_ACEPTABLE, places=3)
        self.assertGreater(margen, 0)

    def test_el_piso_nuevo_es_mas_alto_que_el_viejo(self):
        viejo = 8000 / (1 - 0.3259 - (-0.10))     # formula anterior
        nuevo = pm.precio_piso(8000, 0.3259)
        self.assertGreater(nuevo, viejo)

    def test_no_se_puede_proponer_vender_a_perdida(self):
        ev = pm.evaluar_cambio(29000, 12000, 8000, 0.3259, unidades_30d=1)
        self.assertFalse(ev['viable'])
        self.assertIn('minimo', ev['motivos'][0])

    def test_margen_cero_tampoco_es_aceptable(self):
        """Vender sin ganar nada no es optimizar."""
        sin_ganancia = 8000 / (1 - 0.3259)
        ev = pm.evaluar_cambio(29000, round(sin_ganancia), 8000, 0.3259)
        self.assertFalse(ev['viable'])


class TestUmbralEnvioGratis(unittest.TestCase):
    """El margen no es lineal: cruzar el umbral lo mueve de golpe."""

    def test_detecta_baja_que_cruza(self):
        self.assertEqual(pm.cruce_umbral_envio(34000, 32000), 'baja_cruzando')

    def test_detecta_suba_que_cruza(self):
        self.assertEqual(pm.cruce_umbral_envio(32000, 34000), 'sube_cruzando')

    def test_no_avisa_cuando_no_corresponde(self):
        self.assertIsNone(pm.cruce_umbral_envio(50000, 45000))
        self.assertIsNone(pm.cruce_umbral_envio(20000, 18000))

    def test_el_aviso_llega_a_la_evaluacion(self):
        ev = pm.evaluar_cambio(34000, 32000, 9000, 0.28, unidades_30d=4)
        self.assertEqual(ev['cruce_umbral_envio'], 'baja_cruzando')
        self.assertTrue(any('envio gratis' in a for a in ev['avisos']))


class TestUnidadesParaCompensar(unittest.TestCase):
    """La cuenta exacta que ningun modulo hacia: cuanto mas hay que vender."""

    def test_caso_real_faja(self):
        c = pm.unidades_para_compensar(29000, 26680, 8000, 0.3259, 1)
        self.assertFalse(c['imposible'])
        # Bajar 8% el precio se come mas del 8% del margen: el margen es la parte
        # chica del precio, asi que el golpe relativo es mayor.
        self.assertGreater(c['aumento_necesario_pct'], 8)
        self.assertLess(c['aumento_necesario_pct'], 30)

    def test_margen_negativo_es_incompensable(self):
        c = pm.unidades_para_compensar(29000, 11000, 8000, 0.3259, 5)
        self.assertTrue(c['imposible'])

    def test_bajar_mas_exige_vender_mas(self):
        poco  = pm.unidades_para_compensar(29000, 27500, 8000, 0.3259, 10)
        mucho = pm.unidades_para_compensar(29000, 24000, 8000, 0.3259, 10)
        self.assertGreater(mucho['aumento_necesario_pct'], poco['aumento_necesario_pct'])


class TestImpactoDeBajar(unittest.TestCase):
    """Antes se asumia que bajar el precio llevaba la conversion al promedio."""

    def test_no_asume_que_la_conversion_salta_al_promedio(self):
        _, det = pm.impacto_estimado_baja(
            FAJA['precio'], 26680, FAJA['costo'], FAJA['fee_rate'],
            FAJA['visitas_30d'], FAJA['conv'], FAJA['avg_conv'], FAJA['ventas_30d'])
        self.assertLess(det['conversion_estimada_pct'], FAJA['avg_conv'])
        self.assertGreater(det['conversion_estimada_pct'], FAJA['conv'])

    def test_es_mucho_menor_que_la_formula_optimista(self):
        nuevo, _ = pm.impacto_estimado_baja(
            FAJA['precio'], 26680, FAJA['costo'], FAJA['fee_rate'],
            FAJA['visitas_30d'], FAJA['conv'], FAJA['avg_conv'], FAJA['ventas_30d'])
        margen_new = pm.margen_unitario(26680, FAJA['costo'], FAJA['fee_rate'])
        viejo = FAJA['visitas_30d'] * (FAJA['avg_conv'] / 100) * margen_new
        self.assertLess(nuevo, viejo / 2)

    def test_descuenta_lo_que_el_item_ya_gana(self):
        con_ventas = pm.impacto_estimado_baja(
            29000, 26680, 8000, 0.3259, 132, 0.76, 2.32, unidades_30d=1)[0]
        sin_ventas = pm.impacto_estimado_baja(
            29000, 26680, 8000, 0.3259, 132, 0.76, 2.32, unidades_30d=0)[0]
        self.assertLess(con_ventas, sin_ventas)

    def test_sin_mejora_posible_no_promete_nada(self):
        impacto, _ = pm.impacto_estimado_baja(
            29000, 26680, 8000, 0.3259, 132, conv_actual=3.0, conv_referencia=2.32)
        self.assertEqual(impacto, 0.0)

    def test_siempre_explica_el_calculo(self):
        _, det = pm.impacto_estimado_baja(29000, 26680, 8000, 0.3259, 132, 0.76, 2.32, 1)
        for k in ('conversion_estimada_pct', 'factor_realismo', 'unidades_estimadas',
                  'ganancia_actual_mes', 'formula'):
            self.assertIn(k, det)


class TestUnaSolaFuente(unittest.TestCase):
    """Las cuatro logicas de precio tienen que dar lo mismo, porque son una."""

    def test_repricing_usa_el_piso_del_motor(self):
        from modules.repricing import _calculate_new_price
        costo, fee = 8000.0, 0.3259
        piso = pm.precio_piso(costo, fee)
        # Un competidor baratisimo no puede arrastrar el precio debajo del piso
        nuevo, _ = _calculate_new_price(
            current_price=29000, competitor_price=9000,
            min_price=1000, max_price=50000, costo=costo, fee_rate=fee)
        self.assertGreaterEqual(nuevo, round(piso, 2) - 1)
        self.assertGreater(pm.margen_pct(nuevo, costo, fee), 0)

    def test_las_constantes_estan_en_un_solo_lugar(self):
        self.assertEqual(pm.UMBRAL_ENVIO_GRATIS_ARS, 33_000)
        self.assertGreater(pm.MARGEN_MINIMO_ACEPTABLE, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
