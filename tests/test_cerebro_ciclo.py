"""El ciclo propone -> aplica -> evalua.

Los modulos que proponen dejan la accion en `pendiente`. Si cuando el usuario
aplica ese cambio el sistema registra una accion NUEVA, la propuesta queda
huerfana para siempre y nunca se evalua — que es justo la parte por la que
existe Cerebro.
"""

import os
import shutil
import sys
import tempfile
import uuid
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import cerebro  # noqa: E402


class TestCicloPropuesta(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Un alias distinto por test: el almacenamiento de cerebro es por alias
        # y compartirlo entre tests los hace depender del orden.
        self.alias = f'TestCiclo_{uuid.uuid4().hex[:8]}'
        cerebro._save(self.alias, 'acciones', {'acciones': []})

    def tearDown(self):
        cerebro._save(self.alias, 'acciones', {'acciones': []})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _proponer(self, tipo='ficha', item_id='MLA1'):
        return cerebro.registrar_accion(
            self.alias, tipo=tipo, item_id=item_id,
            origen=cerebro.ORIGEN_PROPUESTO,
            hipotesis='completar la ficha mete la publicacion en mas filtros',
            detalle={'atributos': ['MARCA']})

    def test_aplicar_adopta_la_propuesta_en_vez_de_duplicarla(self):
        prop = self._proponer()
        self.assertEqual(prop['estado'], cerebro.ESTADO_PENDIENTE)

        aplicada = cerebro.aplicar_o_registrar(
            self.alias, tipo='ficha', item_id='MLA1',
            origen=cerebro.ORIGEN_USUARIO,
            hipotesis='aplicado desde el panel',
            detalle={'campos': 3}, ejecutado_por='api:aplicar-todo')

        self.assertEqual(aplicada['id'], prop['id'], 'tiene que ser la MISMA accion')
        self.assertEqual(aplicada['estado'], cerebro.ESTADO_APLICADA)
        self.assertTrue(aplicada['detalle'].get('adoptada_de_propuesta'))
        # La hipotesis original sobrevive: es lo que se va a contrastar
        self.assertIn('filtros', aplicada['hipotesis'])
        self.assertEqual(len(cerebro.listar_acciones(self.alias)), 1,
                         'no se duplica el registro')

    def test_sin_propuesta_pendiente_registra_normal(self):
        acc = cerebro.aplicar_o_registrar(
            self.alias, tipo='precio', item_id='MLA2',
            origen=cerebro.ORIGEN_USUARIO, hipotesis='baje el precio',
            detalle={'precio_antes': 100, 'precio_despues': 90})
        self.assertEqual(acc['estado'], cerebro.ESTADO_APLICADA)
        self.assertFalse(acc['detalle'].get('adoptada_de_propuesta'))

    def test_no_adopta_una_propuesta_de_otro_item_ni_de_otro_tipo(self):
        self._proponer(tipo='ficha', item_id='MLA1')
        otro_item = cerebro.aplicar_o_registrar(
            self.alias, tipo='ficha', item_id='MLA9',
            origen=cerebro.ORIGEN_USUARIO, hipotesis='x', detalle={})
        otro_tipo = cerebro.aplicar_o_registrar(
            self.alias, tipo='precio', item_id='MLA1',
            origen=cerebro.ORIGEN_USUARIO, hipotesis='y', detalle={})
        self.assertNotEqual(otro_item['estado'], cerebro.ESTADO_PENDIENTE)
        self.assertEqual(len(cerebro.listar_acciones(self.alias)), 3,
                         'la propuesta sigue pendiente y hay dos registros nuevos')

    def test_una_propuesta_vieja_no_explica_un_cambio_de_hoy(self):
        prop = self._proponer()
        vieja = '2020-01-01 10:00:00'
        cerebro.actualizar_accion(self.alias, prop['id'], ts=vieja)

        acc = cerebro.aplicar_o_registrar(
            self.alias, tipo='ficha', item_id='MLA1',
            origen=cerebro.ORIGEN_USUARIO, hipotesis='z', detalle={})
        self.assertNotEqual(acc['id'], prop['id'])
        self.assertEqual(len(cerebro.listar_acciones(self.alias)), 2)


if __name__ == '__main__':
    unittest.main()
