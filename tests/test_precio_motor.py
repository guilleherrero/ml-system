"""
Tests del motor unico de precio (bloque 12.2).

Estas reglas deciden a que precio se vende. Un error aca no ensucia un reporte:
cambia lo que el cliente paga y lo que queda de margen. Por eso van con numeros
reales de la cuenta Novara y con el caso concreto que motivo el cambio.

Correr:  python3 tests/test_precio_motor.py
"""

import math
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


class TestPisoDe15(unittest.TestCase):
    """El usuario definio 15% como piso: no se baja de ahi."""

    def test_el_piso_es_15(self):
        self.assertEqual(pm.MARGEN_MINIMO_ACEPTABLE, 0.15)

    def test_una_baja_que_deja_14_por_ciento_se_rechaza(self):
        costo, fee = 8000.0, 0.3259
        precio_14 = costo / (1 - fee - 0.14)
        ev = pm.evaluar_cambio(29000, round(precio_14), costo, fee, unidades_30d=2)
        self.assertFalse(ev['viable'])

    def test_toda_propuesta_muestra_margen_actual_y_nuevo(self):
        ev = pm.evaluar_cambio(29000, 26680, 8000, 0.3259, unidades_30d=1)
        self.assertIsNotNone(ev['margen_actual_pct'])
        self.assertIsNotNone(ev['margen_nuevo_pct'])
        self.assertGreater(ev['margen_actual_pct'], ev['margen_nuevo_pct'])


class TestPalancaCuotas(unittest.TestCase):
    """Reducir cuotas recupera margen sin tocar el precio de lista.

    La cuenta que importa: bajar de 12 a 6 no molesta a quien ya compraba en 6.
    Solo afecta a los que necesitaban 7 a 12.
    """

    POCAS_LARGAS = {'1': 55, '2-3': 25, '4-6': 15, '7-12': 5}
    MUCHAS_LARGAS = {'1': 20, '2-3': 10, '4-6': 20, '7-12': 45, '13+': 5}

    def test_si_casi_nadie_usa_cuotas_largas_reducir_es_de_bajo_riesgo(self):
        pasos = pm.analizar_reduccion_cuotas(29000, 6, self.POCAS_LARGAS)
        paso_12_6 = next(p for p in pasos if p['de_max'] == 12 and p['a_max'] == 6)
        self.assertLessEqual(paso_12_6['pct_afectados'], 8)
        self.assertTrue(paso_12_6['recomendado'])

    def test_si_la_mitad_usa_cuotas_largas_no_se_recomienda(self):
        pasos = pm.analizar_reduccion_cuotas(29000, 6, self.MUCHAS_LARGAS)
        for p in pasos:
            if p['a_max'] <= 6:
                self.assertFalse(p['recomendado'],
                                 f'no deberia recomendar {p["de_max"]}->{p["a_max"]}')

    def test_no_afecta_a_quien_ya_compraba_con_menos_cuotas(self):
        """El 95% que compraba en 6 o menos no se entera del cambio 12->6."""
        pasos = pm.analizar_reduccion_cuotas(29000, 6, self.POCAS_LARGAS)
        paso = next(p for p in pasos if p['de_max'] == 12 and p['a_max'] == 6)
        self.assertAlmostEqual(paso['pct_no_afectados'], 95.0, places=0)

    def test_traduce_el_ahorro_a_descuento_equivalente(self):
        """Para poder compararlo de frente con bajar el precio."""
        pasos = pm.analizar_reduccion_cuotas(29000, 6, self.POCAS_LARGAS)
        self.assertTrue(all('equivale_a_descuento_pct' in p for p in pasos))

    def test_sin_datos_de_cuotas_no_inventa_pasos(self):
        self.assertEqual(pm.analizar_reduccion_cuotas(29000, 6, None), [])
        self.assertEqual(pm.analizar_reduccion_cuotas(29000, 6, {}), [])

    def test_la_alternativa_aparece_al_evaluar_una_baja(self):
        ev = pm.evaluar_cambio(29000, 26680, 8000, 0.3259, unidades_30d=6,
                               cuotas_breakdown=self.POCAS_LARGAS)
        self.assertIn('alternativa_cuotas', ev)
        self.assertTrue(any('cuotas' in a for a in ev['avisos']))

    def test_el_menu_compara_las_dos_palancas(self):
        m = pm.menu_de_palancas(29000, 8000, 0.3259, precio_sugerido=27500,
                                ventas_30d=20, cuotas_breakdown=self.POCAS_LARGAS)
        palancas = {o['palanca'] for o in m['opciones']}
        self.assertIn('bajar_precio', palancas)
        self.assertIn('reducir_cuotas', palancas)
        self.assertIn('recomendada', m)

    def test_bajar_precio_es_publico_y_reducir_cuotas_no(self):
        """Diferencia de fondo: el precio lo ven todos, incluidos los repricers."""
        m = pm.menu_de_palancas(29000, 8000, 0.3259, precio_sugerido=27500,
                                ventas_30d=20, cuotas_breakdown=self.POCAS_LARGAS)
        for o in m['opciones']:
            self.assertEqual(o['publico'], o['palanca'] == 'bajar_precio')


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


class TestEstrategiaTresPublicaciones(unittest.TestCase):
    """Calculadora de Estrategia de Precios (spec 2026-09-18).

    Configuracion comun: costo 15.000; cargos IIBB 3% + percepcion de IVA 7%
    (ambas se restan siempre — para esta cuenta ninguna se recupera, ver
    docs/CEREBRO.md), envio 5.000, umbral 33.000, fijos 1115/2300/2810,
    redondeo activo. Perfiles Batalla (17%/0%), Medio (17%/5,75%),
    Compensa (17%/10,75%). Situacion hoy: precio 60.578, 17%/10,75%,
    3 ventas/dia. Competidor mas barato: 42.000.

    Los numeros de referencia se recalcularon corriendo este mismo codigo con
    percepcion de IVA incluida — la spec original solo restaba IIBB, asi que
    sus valores de ejemplo no sirven tal cual (motivo: con mas costo, el
    precio objetivo de la batalla ya no le gana solo al competidor en el caso
    margen, así que "comp" difiere de "rent" en ambos casos, no solo en A).
    """

    def _setup(self):
        c = pm.Cargos()
        costo = 15000.0
        pubs = [pm.Publicacion("Batalla", 17.0, 0.0),
                pm.Publicacion("Medio", 17.0, 5.75),
                pm.Publicacion("Compensa", 17.0, 10.75)]
        return c, costo, pubs

    def test_ganancia_hoy(self):
        c, costo, _ = self._setup()
        s = pm.Situacion(precio=60578, comision=17.0, costo_cuotas=10.75,
                         ventas_dia=3, publicidad_dia=0)
        g_venta, g_dia = pm.ganancia_hoy(s, c, costo)
        self.assertAlmostEqual(g_venta, 17709.805, places=2)
        self.assertAlmostEqual(g_dia, 53129.415, places=2)

    def test_caso_a_roi_120_60(self):
        c, costo, pubs = self._setup()
        obj = pm.Objetivo(modo="roi", objetivo=120, piso=60)
        esperado = {
            "rent": ([52999, 56999, 61999], [18689.27, 18331.8275, 18594.3775]),
            "comp": ([40999, 56999, 61999], [9929.27, 18331.8275, 18594.3775]),
            "vel":  ([40999, 44999, 48999], [9929.27, 10261.8275, 10501.8775]),
        }
        for goal, (precios_esp, ganancias_esp) in esperado.items():
            precios, ganancias, _ = pm.estrategia(goal, pubs, c, costo, obj, 42000)
            self.assertEqual([round(p) for p in precios], precios_esp, msg=goal)
            for g, ge in zip(ganancias, ganancias_esp):
                self.assertAlmostEqual(g, ge, places=1, msg=goal)

    def test_caso_b_margen_30_20(self):
        c, costo, pubs = self._setup()
        obj = pm.Objetivo(modo="margen", objetivo=30, piso=20)
        esperado = {
            "rent": ([46999, 53999, 62999], [14309.27, 16314.3275, 19216.8775]),
            "comp": ([40999, 53999, 62999], [9929.27, 16314.3275, 19216.8775]),
            "vel":  ([40999, 44999, 48999], [9929.27, 10261.8275, 10501.8775]),
        }
        for goal, (precios_esp, ganancias_esp) in esperado.items():
            precios, ganancias, _ = pm.estrategia(goal, pubs, c, costo, obj, 42000)
            self.assertEqual([round(p) for p in precios], precios_esp, msg=goal)
            for g, ge in zip(ganancias, ganancias_esp):
                self.assertAlmostEqual(g, ge, places=1, msg=goal)

    def test_caso_c_escalera_meta_y_efecto_umbral(self):
        """Con multiplicador x2 sobre el perfil Batalla: el par 34.999/32.999
        tiene que mostrar que el precio MENOR deja MAS ganancia (no paga
        envio por debajo del umbral de $33.000) — el motor tiene que
        devolverlo asi, no "corregirlo"."""
        c, costo, pubs = self._setup()
        s = pm.Situacion(precio=60578, comision=17.0, costo_cuotas=10.75,
                         ventas_dia=3, publicidad_dia=0)
        _, g_dia_hoy = pm.ganancia_hoy(s, c, costo)
        precios = [60999, 54999, 49999, 44999, 40999, 37999, 34999, 32999]
        pasos = pm.escalera_meta(2.0, g_dia_hoy, pubs[0], c, costo, precios,
                                 stock=10_000, dias_reposicion=0,
                                 piso_precio=0, competidor_min=42000)
        por_precio = {p["precio"]: p for p in pasos}
        # El efecto umbral: 32.999 deja MAS ganancia que 34.999 (no paga envio)

        self.assertGreater(por_precio[32999]["ganancia_venta"],
                           por_precio[34999]["ganancia_venta"])
        self.assertAlmostEqual(por_precio[34999]["ganancia_venta"], 5549.27, places=1)
        self.assertAlmostEqual(por_precio[32999]["ganancia_venta"], 6279.27, places=1)


class TestEstrategiaBordes(unittest.TestCase):
    """Los 7 casos borde del §8 de la spec."""

    def _setup(self):
        c = pm.Cargos()
        pub = pm.Publicacion("Batalla", 17.0, 0.0)
        return c, pub

    def test_costo_cuotas_altisimo_da_infinito(self):
        c, _ = self._setup()
        pub = pm.Publicacion("X", 50.0, 60.0)  # comision+cuotas+iibb+percep >= 100
        self.assertTrue(math.isinf(pm.precio_para_ganancia(1000, pub, c, 15000)))

    def test_margen_objetivo_mas_cargos_100_da_infinito(self):
        c, pub = self._setup()
        # rate() de "Batalla" ya es 27% (17+0+3+7); margen 80% + eso >= 100%
        self.assertTrue(math.isinf(pm.precio_para_margen(0.80, pub, c, 15000)))

    def test_sin_competidor_usa_precio_objetivo(self):
        c, pub = self._setup()
        obj = pm.Objetivo(modo="roi", objetivo=120, piso=60)
        safe = pm.precio_objetivo(pub, c, 15000, obj)
        floor = pm.precio_piso_objetivo(pub, c, 15000, obj)
        _, motivo = pm.precio_batalla(safe, floor, 0, c)
        self.assertEqual(motivo, "sin_competidor")

    def test_piso_por_encima_del_competidor(self):
        c, pub = self._setup()
        # Piso carisimo (ROI 500%) que ningun competidor barato puede cumplir
        obj = pm.Objetivo(modo="roi", objetivo=600, piso=500)
        safe = pm.precio_objetivo(pub, c, 15000, obj)
        floor = pm.precio_piso_objetivo(pub, c, 15000, obj)
        _, motivo = pm.precio_batalla(safe, floor, 20000, c)
        self.assertEqual(motivo, "piso_no_permite_ganar")

    def test_redondeo_false_no_redondea(self):
        c = pm.Cargos(redondeo=False)
        pub = pm.Publicacion("Batalla", 17.0, 0.0)
        obj = pm.Objetivo(modo="roi", objetivo=120, piso=60)
        p = pm.precio_objetivo(pub, c, 15000, obj)
        self.assertEqual(p, pm.precio_para_ganancia(15000 * 1.2, pub, c, 15000))
        self.assertNotEqual(str(p)[-3:], "999")

    def test_cargo_fijo_por_tramo(self):
        c = pm.Cargos()
        self.assertEqual(pm.cargo_fijo(10000, c), c.fijo_menos_15k)
        self.assertEqual(pm.cargo_fijo(20000, c), c.fijo_15k_25k)
        self.assertEqual(pm.cargo_fijo(30000, c), c.fijo_25k_umbral)
        # Por encima del umbral: se paga envio, no cargo fijo
        self.assertEqual(pm.cargo_fijo(40000, c), c.envio)

    def test_publicidad_no_afecta_ganancia_por_venta(self):
        c, pub = self._setup()
        pub_con_ads = pm.Publicacion("Batalla", 17.0, 0.0, publicidad_dia=500)
        g1 = pm.ganancia_publicacion(50000, pub, c, 15000)
        g2 = pm.ganancia_publicacion(50000, pub_con_ads, c, 15000)
        self.assertEqual(g1, g2)  # la publicidad no entra en ganancia_publicacion
        # Solo afecta ganancia_hoy / dia, vía Situacion.publicidad_dia
        s_sin = pm.Situacion(precio=50000, comision=17.0, costo_cuotas=0,
                             ventas_dia=2, publicidad_dia=0)
        s_con = pm.Situacion(precio=50000, comision=17.0, costo_cuotas=0,
                             ventas_dia=2, publicidad_dia=500)
        gv_sin, gd_sin = pm.ganancia_hoy(s_sin, c, 15000)
        gv_con, gd_con = pm.ganancia_hoy(s_con, c, 15000)
        self.assertEqual(gv_sin, gv_con)
        self.assertEqual(gd_sin - gd_con, 500)


class TestResultadoPrueba(unittest.TestCase):
    """Veredicto manual simple (adelanto del sprint 4, pedido por Guille el
    2026-09-18 al comparar contra el artifact original)."""

    def test_conviene_cuando_gana_mas_que_hoy(self):
        r = pm.resultado_prueba([9929.27, 18331.83, 18594.38], g_dia_hoy=53129.42,
                                publicidad_total=0, ventas_dia_prueba=6, mix_batalla_pct=60)
        self.assertTrue(r['viable'])
        self.assertTrue(r['conviene'])
        prom = pm._ganancia_promedio_ponderada([9929.27, 18331.83, 18594.38], 60, 50.0)
        self.assertAlmostEqual(r['ganancia_dia_prueba'], 6 * prom, places=1)

    def test_no_conviene_cuando_gana_menos_que_hoy(self):
        r = pm.resultado_prueba([9929.27, 18331.83, 18594.38], g_dia_hoy=53129.42,
                                publicidad_total=0, ventas_dia_prueba=2, mix_batalla_pct=90)
        self.assertTrue(r['viable'])
        self.assertFalse(r['conviene'])

    def test_sin_mix_promedia_las_viables(self):
        r = pm.resultado_prueba([18689.27, 18331.83, 18594.38], g_dia_hoy=0,
                                publicidad_total=0, ventas_dia_prueba=1)
        prom = (18689.27 + 18331.83 + 18594.38) / 3
        self.assertAlmostEqual(r['ganancia_dia_prueba'], prom, places=1)

    def test_todas_no_viables_no_es_viable(self):
        r = pm.resultado_prueba([float('inf'), float('inf'), float('inf')], g_dia_hoy=0,
                                publicidad_total=0, ventas_dia_prueba=5)
        self.assertFalse(r['viable'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
