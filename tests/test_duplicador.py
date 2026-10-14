"""Duplicador de publicaciones: reglas de negocio con un ML simulado.

Pub 1 = Clasica sin cuotas. Se duplica como Pub 2 (Premium N cuotas) y Pub 3
(Clasica interes bajo) en el MISMO User Product, via
POST /user-products/{UP}/items. Nada se crea sin aprobar, nunca debajo de 15%
de margen y nunca dos veces la misma condicion de venta.
"""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import duplicador as dup  # noqa: E402
from modules import precio_motor as pm  # noqa: E402

SELLER = 111
UP = 'MLAU1'
# Tasas como las medidas en vivo para Cortadoras (CEREBRO, 2026-09-19)
TASAS = {0: {'comision': 16, 'costo_cuotas': 0}, 4: {'comision': 16, 'costo_cuotas': 5},
         3: {'comision': 16, 'costo_cuotas': 8.9}, 6: {'comision': 16, 'costo_cuotas': 13.4},
         9: {'comision': 16, 'costo_cuotas': 17.8}, 12: {'comision': 16, 'costo_cuotas': 21.6}}
CARGOS = pm.Cargos(iibb=4.19, percepcion_iva=7.08)
COSTO = 15000.0


class _Cuenta:
    user_id = SELLER


class FakeML:
    def __init__(self, items, descripcion='Descripcion original'):
        self.account = _Cuenta()
        self.items = items
        self.descripcion = descripcion
        self.posts = []
        self._n = 0

    def get_item(self, iid):
        return dict(self.items[iid]) if iid in self.items else None

    def _get(self, path, params=None):
        if path == f'/users/{SELLER}/items/search':
            return {'results': [i for i, it in self.items.items()
                                if it.get('user_product_id') == params['user_product_id']
                                and it.get('status') == params['status']]}
        if path == '/items':
            return [{'code': 200, 'body': dict(self.items[i])} for i in params['ids'].split(',')]
        if path.endswith('/description'):
            return {'plain_text': self.descripcion}
        raise AssertionError(path)

    def _post(self, path, payload):
        self.posts.append((path, payload))
        if path.startswith('/user-products/'):
            self._n += 1
            nid = f'NUEVO{self._n}'
            self.items[nid] = {'id': nid, 'seller_id': SELLER, 'user_product_id': path.split('/')[2],
                               'price': payload['price'], 'listing_type_id': payload['listing_type_id'],
                               'tags': payload.get('tags', []), 'status': 'active', 'category_id': 'MLA1'}
            return {'id': nid}
        return {}


def _origen(**extra):
    base = {'id': 'PUB1', 'title': 'Cortador', 'seller_id': SELLER, 'user_product_id': UP, 'price': 60000,
            'listing_type_id': 'gold_special', 'tags': [], 'status': 'active', 'category_id': 'MLA1',
            'domain_id': 'MLA-X', 'currency_id': 'ARS',
            'sale_terms': [{'id': 'WARRANTY_TYPE', 'value_id': '2230280', 'value_name': 'Garantía del vendedor', 'x': 1}]}
    base.update(extra)
    return base


class TestCalculo(unittest.TestCase):

    def test_sin_costo_no_calcula(self):
        for costo in (None, '', 0):
            with self.assertRaises(dup.DuplicadorError):
                dup.calcular(costo, CARGOS, TASAS, 12)

    def test_roi_objetivo_y_piso(self):
        filas = dup.calcular(COSTO, CARGOS, TASAS, 12, roi_objetivo=120)
        self.assertEqual([f['perfil'] for f in filas], ['pub2', 'pub3'])
        self.assertEqual(filas[0]['listing_type_id'], 'gold_pro')
        self.assertEqual(filas[0]['tags'], ['12x_campaign'])
        self.assertEqual(filas[1]['listing_type_id'], 'gold_special')
        self.assertEqual(filas[1]['tags'], ['pcj-co-funded'])
        for f in filas:
            # El redondeo a ...999 es hacia arriba: el ROI nunca queda debajo del objetivo
            self.assertGreaterEqual(f['roi_pct'], 120)
            self.assertTrue(f['cumple_piso'])
        # Pub 2 paga mas cuotas que Pub 3: necesita precio mas alto para el mismo ROI
        self.assertGreater(filas[0]['precio'], filas[1]['precio'])

    def test_precio_editado_debajo_del_piso(self):
        filas = dup.calcular(COSTO, CARGOS, TASAS, 6, precios_editados={'pub3': 20000})
        pub3 = filas[1]
        self.assertTrue(pub3['editado'])
        self.assertEqual(pub3['precio'], 20000)
        self.assertFalse(pub3['cumple_piso'])

    def test_escalon_invalido_y_tasa_faltante(self):
        with self.assertRaises(dup.DuplicadorError):
            dup.calcular(COSTO, CARGOS, TASAS, 4)       # 4 es la Pub 3, no una opcion de Pub 2
        sin_12 = {k: v for k, v in TASAS.items() if k != 12}
        with self.assertRaises(dup.DuplicadorError):
            dup.calcular(COSTO, CARGOS, sin_12, 12)     # nunca un numero inventado


class TestBorradoresYAprobacion(unittest.TestCase):

    def setUp(self):
        self.alias = f'TestDup_{uuid.uuid4().hex[:8]}'
        self.ml = FakeML({'PUB1': _origen()})

    def tearDown(self):
        p = dup._path(self.alias)
        if os.path.exists(p):
            os.remove(p)

    def _borradores(self, escalon=12, **kw):
        filas = dup.calcular(COSTO, CARGOS, TASAS, escalon, **kw)
        return dup.crear_borradores(self.alias, self.ml.get_item('PUB1'), filas, COSTO, 120, 'guille')

    def test_borrador_no_escribe_en_ml_y_no_se_repite(self):
        bs = self._borradores()
        self.assertEqual(len(bs), 2)
        self.assertEqual(self.ml.posts, [])
        with self.assertRaises(dup.DuplicadorError):
            self._borradores()

    def test_no_guarda_debajo_del_piso(self):
        filas = dup.calcular(COSTO, CARGOS, TASAS, 12, precios_editados={'pub2': 18000})
        with self.assertRaises(dup.DuplicadorError):
            dup.crear_borradores(self.alias, self.ml.get_item('PUB1'), filas, COSTO, 120)
        self.assertEqual(dup.listar_borradores(self.alias), [])

    def test_aprobar_crea_en_el_mismo_up_con_payload_minimo(self):
        b = self._borradores()[0]
        r = dup.aprobar(self.alias, b['id'], self.ml, CARGOS, TASAS, COSTO, 'guille')
        self.assertEqual(r['estado'], 'aplicado')
        self.assertEqual(r['avisos'], [])
        path, payload = self.ml.posts[0]
        self.assertEqual(path, f'/user-products/{UP}/items')
        self.assertEqual(payload['listing_type_id'], 'gold_pro')
        self.assertEqual(payload['tags'], ['12x_campaign'])
        # Fotos, ficha, titulo y stock salen del UP: no se mandan ni se tocan
        for prohibido in ('title', 'pictures', 'attributes', 'available_quantity', 'description'):
            self.assertNotIn(prohibido, payload)
        self.assertEqual(payload['sale_terms'][0], {'id': 'WARRANTY_TYPE', 'value_id': '2230280',
                                                    'value_name': 'Garantía del vendedor'})
        # La descripcion no se comparte entre items: se copia aparte
        self.assertEqual(self.ml.posts[1], (f'/items/{r["item_creado"]}/description',
                                            {'plain_text': 'Descripcion original'}))
        rel = dup.listar_relaciones(self.alias)
        self.assertEqual((rel[0]['item_origen'], rel[0]['item_copia']), ('PUB1', r['item_creado']))

    def test_no_se_aprueba_dos_veces(self):
        b = self._borradores()[0]
        dup.aprobar(self.alias, b['id'], self.ml, CARGOS, TASAS, COSTO)
        with self.assertRaises(dup.DuplicadorError):
            dup.aprobar(self.alias, b['id'], self.ml, CARGOS, TASAS, COSTO)
        self.assertEqual(len([p for p in self.ml.posts if p[0].startswith('/user-products/')]), 1)

    def test_bloquea_si_ml_ya_tiene_esa_condicion(self):
        # Copia hecha por fuera (a mano o desde UpSeller) con 12 cuotas en el mismo UP
        self.ml.items['EXTERNA'] = _origen(id='EXTERNA', listing_type_id='gold_pro', tags=['12x_campaign'], status='paused')
        b = self._borradores()[0]
        with self.assertRaises(dup.DuplicadorError) as ctx:
            dup.aprobar(self.alias, b['id'], self.ml, CARGOS, TASAS, COSTO)
        self.assertIn('EXTERNA', str(ctx.exception))
        self.assertEqual(self.ml.posts, [])

    def test_revalida_margen_y_costo_al_aprobar(self):
        b = self._borradores()[0]
        with self.assertRaises(dup.DuplicadorError):
            dup.aprobar(self.alias, b['id'], self.ml, CARGOS, TASAS, None)        # se borro el costo
        with self.assertRaises(dup.DuplicadorError):
            dup.aprobar(self.alias, b['id'], self.ml, CARGOS, TASAS, COSTO * 3)   # el costo subio: perfora el 15%
        self.assertEqual(self.ml.posts, [])

    def test_origen_invalido(self):
        self.ml.items['AJENA'] = _origen(id='AJENA', seller_id=999)
        self.ml.items['VIEJA'] = _origen(id='VIEJA', user_product_id=None)
        self.ml.items['CATA'] = _origen(id='CATA', catalog_listing=True)
        for iid in ('AJENA', 'VIEJA', 'CATA', 'NOEXISTE'):
            with self.assertRaises(dup.DuplicadorError):
                dup.leer_origen(self.ml, iid)


if __name__ == '__main__':
    unittest.main()
