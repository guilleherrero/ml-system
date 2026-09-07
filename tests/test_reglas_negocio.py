"""
Tests de las REGLAS DE NEGOCIO — las que mueven plata.

No prueban que el codigo corra: prueban que los numeros que el sistema le
muestra al usuario sean ciertos. Nacieron de un caso real: el Top 3 prometia
5 millones de pesos por mes por pausar un duplicado con 1.072 visitas. Nadie
lo detecto durante meses porque nada verificaba las formulas, solo la sintaxis.

Regla de oro de este archivo: cada test tiene numeros concretos y un porque.
Si manana alguien cambia una formula y estos tests pasan igual, es que los
tests estan mal escritos.

Correr:  python3 -m pytest tests/test_reglas_negocio.py -v
    o:   python3 tests/test_reglas_negocio.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.detector_duplicados import (
    CONVERSION_MAX_PLAUSIBLE,
    FACTOR_TRANSFERENCIA,
    _calcular_impacto_monetario,
    _margen_unitario,
)


# Datos reales de la cuenta Novara — Cortador Ender Pro (MLA1481911017)
CORTADOR = {'precio': 60578.0, 'costo': 22000.0, 'fee_rate': 0.2849}
MARGEN_CORTADOR = CORTADOR['precio'] * (1 - CORTADOR['fee_rate']) - CORTADOR['costo']  # ~21.319


def _item(id_, ventas, visitas, conv, **extra):
    base = {'id': id_, 'ventas_30d': ventas, 'visitas_30d': visitas,
            'conversion_pct': conv, **CORTADOR}
    base.update(extra)
    return base


class TestMargenUnitario(unittest.TestCase):
    """Lo que queda por unidad no es el precio: es lo que sobra despues de ML."""

    def test_margen_descuenta_comision_y_costo(self):
        m = _margen_unitario(_item('A', 10, 1000, 5))
        self.assertAlmostEqual(m, MARGEN_CORTADOR, places=0)
        # Debe ser bastante menor que el precio: ese fue el error original
        self.assertLess(m, CORTADOR['precio'] * 0.40)

    def test_usa_ganancia_ya_calculada_si_viene(self):
        # stock_rentabilidad ya calcula 'ganancia' con la comision real de ordenes
        self.assertEqual(_margen_unitario(_item('A', 1, 1, 1, ganancia=12345.0)), 12345.0)

    def test_sin_costo_devuelve_none_en_vez_de_inventar(self):
        it = {'id': 'A', 'precio': 50000, 'ventas_30d': 3, 'visitas_30d': 500}
        self.assertIsNone(_margen_unitario(it))


class TestImpactoDuplicados(unittest.TestCase):
    """El numero que ordena el Top 3. Si miente, el usuario prioriza mal."""

    def test_caso_real_no_puede_prometer_millones(self):
        """1.072 visitas perdidas jamas pueden valer 5 millones de margen."""
        ganadora = _item('A', 14, 1500, 7.81)
        dup      = _item('B', 3, 1072, 2.0)
        _, impacto, det = _calcular_impacto_monetario([ganadora, dup], 'A')

        formula_vieja = 1072 * 0.0781 * CORTADOR['precio']   # ~5.071.000
        self.assertGreater(formula_vieja, 5_000_000)
        self.assertLess(impacto, 500_000)
        self.assertLess(impacto, formula_vieja / 10)
        self.assertTrue(det['topeado_por_sensatez'])

    def test_nunca_supera_el_margen_que_el_producto_ya_genera(self):
        """Pausar duplicados no puede mas que duplicar el resultado."""
        ganadora = _item('A', 10, 300, 10.0)
        dup      = _item('B', 0, 9000, 0.5)     # muchisimas visitas, cero ventas
        _, impacto, det = _calcular_impacto_monetario([ganadora, dup], 'A')
        self.assertLessEqual(impacto, det['margen_actual_mes'])
        self.assertTrue(det['topeado_por_sensatez'])

    def test_descuenta_las_ventas_que_el_duplicado_ya_hace(self):
        """Lo que el duplicado ya vende no es ganancia nueva."""
        ganadora = _item('A', 20, 2000, 3.0)
        sin_ventas = _item('B', 0, 1000, 0.0)
        con_ventas = _item('B', 8, 1000, 0.8)
        _, imp_sin, _ = _calcular_impacto_monetario([ganadora, sin_ventas], 'A')
        _, imp_con, d = _calcular_impacto_monetario([ganadora, con_ventas], 'A')
        self.assertLess(imp_con, imp_sin)
        self.assertEqual(d['ventas_duplicados_30d'], 8)

    def test_no_transfiere_el_100_por_ciento_del_trafico(self):
        """Buena parte de esas visitas es la misma persona comparando."""
        self.assertLess(FACTOR_TRANSFERENCIA, 1.0)
        self.assertGreater(FACTOR_TRANSFERENCIA, 0.0)
        ganadora = _item('A', 30, 3000, 3.0)
        dup      = _item('B', 0, 1000, 0.0)
        _, impacto, det = _calcular_impacto_monetario([ganadora, dup], 'A')
        maximo_teorico = 1000 * 0.03 * MARGEN_CORTADOR
        self.assertLess(impacto, maximo_teorico)
        self.assertAlmostEqual(det['unidades_estimadas'],
                               1000 * 0.03 * FACTOR_TRANSFERENCIA, places=1)

    def test_conversion_absurda_se_capea_y_queda_avisado(self):
        """Una conversion de 45% habla de la medicion, no del producto."""
        ganadora = _item('A', 10, 200, 45.0)
        dup      = _item('B', 0, 2000, 1.0)
        _, _, det = _calcular_impacto_monetario([ganadora, dup], 'A')
        self.assertEqual(det['conversion_usada_pct'], CONVERSION_MAX_PLAUSIBLE * 100)
        self.assertTrue(det['conversion_capeada'])

    def test_sin_costo_dice_que_falta_el_dato_en_vez_de_estimar(self):
        ganadora = {'id': 'A', 'precio': 50000, 'ventas_30d': 5,
                    'visitas_30d': 900, 'conversion_pct': 6.0}
        dup      = {'id': 'B', 'precio': 50000, 'ventas_30d': 1,
                    'visitas_30d': 600, 'conversion_pct': 2.0}
        visitas, impacto, det = _calcular_impacto_monetario([ganadora, dup], 'A')
        self.assertEqual(impacto, 0.0)
        self.assertEqual(visitas, 600)          # el diagnostico igual se informa
        self.assertIn('costo', det['motivo'])

    def test_sin_ganadora_no_explota(self):
        _, impacto, det = _calcular_impacto_monetario([_item('A', 1, 10, 1)], 'INEXISTENTE')
        self.assertEqual(impacto, 0.0)
        self.assertIn('motivo', det)

    def test_siempre_explica_de_donde_sale_el_numero(self):
        """Trazabilidad: si no se puede explicar, no se muestra."""
        _, _, det = _calcular_impacto_monetario(
            [_item('A', 14, 1500, 7.81), _item('B', 3, 1072, 2.0)], 'A')
        for campo in ('visitas_perdidas_30d', 'conversion_usada_pct',
                      'factor_transferencia', 'unidades_netas',
                      'margen_unitario', 'formula'):
            self.assertIn(campo, det)


class TestVeredictoCerebro(unittest.TestCase):
    """Un veredicto que se entusiasma con ruido enseña mentiras al sistema."""

    def setUp(self):
        import tempfile
        from modules import cerebro
        self.cerebro = cerebro
        self._dir_previo = cerebro.DATA_DIR
        self.tmp = tempfile.mkdtemp()
        cerebro.DATA_DIR = self.tmp
        self.alias = 'TestReglas'

    def tearDown(self):
        import shutil
        self.cerebro.DATA_DIR = self._dir_previo
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _serie(self, item, dias, visitas, unidades, desde_dias_atras):
        from datetime import datetime, timedelta
        hoy = datetime.now()
        for i in range(dias):
            d = (hoy - timedelta(days=desde_dias_atras - i)).strftime('%Y-%m-%d')
            self.cerebro.guardar_snapshot(self.alias, item, self.cerebro.construir_snapshot(
                fecha=d, visitas_totales=visitas, clics_ads=0,
                unidades=unidades, categoria='MLA-test'))

    def test_una_mejora_clara_se_reconoce(self):
        from datetime import datetime, timedelta
        self._serie('MLA1', 14, 100, 2, 28)      # antes
        self._serie('MLA1', 14, 200, 5, 14)      # despues: el doble
        self._serie('MLA2', 28, 100, 2, 28)      # hermana sin tocar: control
        acc = self.cerebro.registrar_accion(
            self.alias, tipo='ficha', item_id='MLA1',
            origen=self.cerebro.ORIGEN_USUARIO, estado_previo={'categoria': 'MLA-test'})
        self.cerebro.actualizar_accion(
            self.alias, acc['id'],
            aplicada_ts=(datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S'))
        self.cerebro.evaluar_acciones_pendientes(self.alias)
        ev = self.cerebro.get_accion(self.alias, acc['id'])['evaluacion']['7d']
        self.assertEqual(ev['veredicto'], self.cerebro.VEREDICTO_FUNCIONO)

    def test_no_pasa_nada_es_neutra_no_exito(self):
        from datetime import datetime, timedelta
        self._serie('MLA1', 14, 100, 2, 28)
        self._serie('MLA1', 14, 102, 2, 14)      # +2%: ruido, no señal
        self._serie('MLA2', 28, 100, 2, 28)
        acc = self.cerebro.registrar_accion(
            self.alias, tipo='ficha', item_id='MLA1',
            origen=self.cerebro.ORIGEN_USUARIO, estado_previo={'categoria': 'MLA-test'})
        self.cerebro.actualizar_accion(
            self.alias, acc['id'],
            aplicada_ts=(datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S'))
        self.cerebro.evaluar_acciones_pendientes(self.alias)
        ev = self.cerebro.get_accion(self.alias, acc['id'])['evaluacion']['7d']
        self.assertEqual(ev['veredicto'], self.cerebro.VEREDICTO_NEUTRA)

    def test_dos_acciones_encimadas_no_ensenan_nada(self):
        """Si hubo otro cambio en la ventana, el resultado no es atribuible."""
        from datetime import datetime, timedelta
        self._serie('MLA1', 14, 100, 2, 28)
        self._serie('MLA1', 14, 300, 9, 14)
        hace14 = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
        hace10 = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S')
        a1 = self.cerebro.registrar_accion(self.alias, tipo='ficha', item_id='MLA1',
                                           origen=self.cerebro.ORIGEN_USUARIO)
        self.cerebro.actualizar_accion(self.alias, a1['id'], aplicada_ts=hace14)
        a2 = self.cerebro.registrar_accion(self.alias, tipo='precio', item_id='MLA1',
                                           origen=self.cerebro.ORIGEN_USUARIO)
        self.cerebro.actualizar_accion(self.alias, a2['id'], aplicada_ts=hace10)
        self.cerebro.evaluar_acciones_pendientes(self.alias)
        ev = self.cerebro.get_accion(self.alias, a1['id'])['evaluacion']['7d']
        self.assertEqual(ev['veredicto'], self.cerebro.VEREDICTO_CONTAMINADA)

    def test_lo_contaminado_no_entra_en_los_aprendizajes(self):
        self.test_dos_acciones_encimadas_no_ensenan_nada()
        aps = self.cerebro.recalcular_aprendizajes(self.alias)
        self.assertEqual(len(aps), 0)

    def test_competidor_sin_confirmar_nunca_mueve_precio(self):
        """Regla dura del bloque 2.2.6: solo directos confirmados deciden precio."""
        reg = self.cerebro.guardar_competidor(
            self.alias, {'id': 'MLA999', 'title': 'Competidor', 'price': 50000},
            item_propio='MLA1')
        self.assertEqual(len(self.cerebro.competidores_para_precio(self.alias, 'MLA1')), 0)
        self.cerebro.clasificar_competidor(self.alias, reg['clave'],
                                           self.cerebro.CLASE_DIRECTO)
        self.assertEqual(len(self.cerebro.competidores_para_precio(self.alias, 'MLA1')), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
