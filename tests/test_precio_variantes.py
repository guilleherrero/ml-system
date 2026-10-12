"""Precio y cuotas a TODAS las variantes, aplicado solo al aprobar.

Bug real 2026-10-12 (Cortador Ender Pro, Novara): la calculadora cambio Rojo
(MLA1568967243) y Negro (MLA1481911017) quedo con otro precio y otras cuotas.
En User Products cada color es otro item de la misma family_id.
"""

import os
import shutil
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import cerebro, precio_variantes as pv  # noqa: E402


class FakeML:
    """Lo minimo de MLClient. `ignorar_put` simula ML devolviendo 200 sin
    guardar el cambio — el caso que solo la relectura detecta."""

    def __init__(self, items, ignorar_put=()):
        self.items = items
        self.ignorar_put = set(ignorar_put)
        self.puts = []

    def get_item(self, iid):
        return dict(self.items[iid])

    def _get(self, path, params=None):
        if path == '/items':
            return [{'code': 200, 'body': dict(self.items[i])} for i in params['ids'].split(',')]
        return self.get_item(path.split('/')[-1])

    def get_my_listings(self, limit=50, offset=0, status='active'):
        ids = list(self.items)
        return {'results': ids[offset:offset + limit], 'paging': {'total': len(ids)}}

    def update_listing_type(self, iid, lt):
        self.items[iid]['listing_type_id'] = lt

    def update_item(self, iid, payload):
        self.puts.append(iid)
        if iid not in self.ignorar_put:
            self.items[iid].update(payload)


def _items():
    return {
        'ROJO':  {'id': 'ROJO', 'title': 'Cortador Rojo', 'price': 72000, 'family_id': 7,
                  'listing_type_id': 'gold_special', 'tags': ['good_quality', 'pcj-co-funded'],
                  'attributes': [{'id': 'COLOR', 'value_name': 'Rojo'}]},
        'NEGRO': {'id': 'NEGRO', 'title': 'Cortador Negro', 'price': 48000, 'family_id': 7,
                  'listing_type_id': 'gold_pro', 'tags': ['3x_campaign'],
                  'attributes': [{'id': 'COLOR', 'value_name': 'Negro'}]},
        'CATA':  {'id': 'CATA', 'title': 'Cortador catalogo', 'price': 50000, 'family_id': 7,
                  'listing_type_id': 'gold_pro', 'tags': [], 'catalog_listing': True},
        'TRIO':  {'id': 'TRIO', 'title': 'Cortador 12 cuotas', 'price': 90000, 'family_id': 7,
                  'listing_type_id': 'gold_pro', 'tags': ['12x_campaign']},
        'OTRO':  {'id': 'OTRO', 'title': 'Otro producto', 'price': 1000, 'family_id': 8,
                  'listing_type_id': 'gold_pro', 'tags': []},
    }


class TestVariantes(unittest.TestCase):

    def setUp(self):
        self.alias = f'TestVariantes_{uuid.uuid4().hex[:8]}'
        self.ml = FakeML(_items())
        self.patches = [
            mock.patch('core.account_manager.AccountManager.get_client', return_value=self.ml),
            mock.patch('modules.telegram_bot.notificar_propuesta', return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(cerebro._dir(self.alias), ignore_errors=True)

    def _proponer(self, item_ids, escalon=0):
        return cerebro.registrar_accion(
            self.alias, tipo='precio', item_id='ROJO', origen=cerebro.ORIGEN_PROPUESTO,
            detalle={'variantes': [{'id': 'ROJO', 'color': 'Rojo'}, {'id': 'NEGRO', 'color': 'Negro'}],
                     'ejecutar': {'item_ids': item_ids, 'precio_nuevo': 48000, 'escalon': escalon,
                                  'ganancias': {'ROJO': 9000, 'NEGRO': 11000}, 'experimento': {}}})

    def test_detecta_variantes_sin_catalogo_ni_trio(self):
        vs = pv.variantes(self.ml, 'ROJO', excluir={'TRIO'})
        self.assertEqual([v['id'] for v in vs], ['ROJO', 'NEGRO'])
        self.assertEqual(vs[0]['cuotas'], '3 a 12 con interés bajo')
        self.assertEqual(vs[1]['cuotas'], '3 cuotas sin interés')

    def test_aprobar_aplica_a_todas_y_verifica(self):
        acc = self._proponer(['ROJO', 'NEGRO'])
        with mock.patch('web.db.session_scope', side_effect=Exception('sin db en test')):
            res = cerebro.aprobar_accion(self.alias, acc['id'])
        self.assertEqual(res['estado'], cerebro.ESTADO_APLICADA)
        self.assertEqual(res['detalle']['resultado'], {'ROJO': 'ok', 'NEGRO': 'ok'})
        for iid in ('ROJO', 'NEGRO'):
            it = self.ml.items[iid]
            self.assertEqual(it['price'], 48000)
            self.assertEqual(pv.escalon_de(it['listing_type_id'], it['tags']), 0)
        # los tags que no son de cuotas se conservan
        self.assertIn('good_quality', self.ml.items['ROJO']['tags'])

    def test_relectura_detecta_la_que_no_quedo(self):
        self.ml.ignorar_put = {'NEGRO'}
        acc = self._proponer(['ROJO', 'NEGRO'])
        with mock.patch('web.db.session_scope', side_effect=Exception('sin db en test')):
            res = cerebro.aprobar_accion(self.alias, acc['id'])
        self.assertEqual(res['detalle']['resultado']['ROJO'], 'ok')
        self.assertTrue(res['detalle']['resultado']['NEGRO'].startswith('error'))
        self.assertIn('❌ Negro', pv.texto_resultado(res))

    def test_aprobar_dos_veces_no_aplica_dos_veces(self):
        acc = self._proponer(['ROJO'])
        with mock.patch('web.db.session_scope', side_effect=Exception('sin db en test')):
            cerebro.aprobar_accion(self.alias, acc['id'])
            cerebro.aprobar_accion(self.alias, acc['id'])
        self.assertEqual(self.ml.puts, ['ROJO'])

    def test_rechazada_no_se_aplica(self):
        acc = self._proponer(['ROJO', 'NEGRO'])
        cerebro.rechazar_accion(self.alias, acc['id'])
        cerebro.aprobar_accion(self.alias, acc['id'])
        self.assertEqual(self.ml.puts, [])


if __name__ == '__main__':
    unittest.main()
