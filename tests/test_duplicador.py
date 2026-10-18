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
from unittest import mock

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

    def get_my_listings(self, limit=50, offset=0, status='active'):
        ids = [i for i, it in self.items.items() if it.get('status') == status]
        return {'results': ids[offset:offset + limit], 'paging': {'total': len(ids)}}

    def _get(self, path, params=None):
        if path == f'/users/{SELLER}/items/search':
            ups = params['user_product_id'].split(',')
            return {'results': [i for i, it in self.items.items()
                                if it.get('user_product_id') in ups and it.get('status') == params['status']]}
        if path.startswith('/user-products-families/'):
            fam = path.split('/')[2]
            return {'family_id': fam, 'user_products_ids': list(dict.fromkeys(
                it['user_product_id'] for it in self.items.values()
                if str(it.get('family_id')) == fam and it.get('user_product_id')))}
        if path == '/items':
            return [{'code': 200, 'body': dict(self.items[i])} for i in params['ids'].split(',')]
        if path.startswith('/categories/'):
            return {'settings': {'max_pictures_per_item': 4}}
        if path.endswith('/description'):
            return {'plain_text': self.descripcion}
        raise AssertionError(path)

    diagnostico = {}          # picture_id o 'base64' -> lista de problemas
    puts = None

    def update_item(self, iid, payload):
        self.puts = (iid, payload)
        if 'pictures' in payload:
            self.items[iid]['pictures'] = [{'id': p['id']} for p in payload['pictures']]

    def _post(self, path, payload):
        self.posts.append((path, payload))
        if path == '/moderations/pictures/diagnostic':
            clave = payload.get('picture_id') or 'base64'
            probs = self.diagnostico.get(clave, [])
            return {'diagnostics': [{'picture_type': payload['context']['picture_type'],
                                     'action': 'diagnostic' if probs else 'empty',
                                     'detections': [{'name': 'x', 'wordings': [{'kind': 'REMEDY_SHORT', 'value': t}]} for t in probs]}]}
        if path.startswith('/user-products/'):
            self._n += 1
            nid = f'NUEVO{self._n}'
            self.items[nid] = {'id': nid, 'seller_id': SELLER, 'user_product_id': path.split('/')[2],
                               'price': payload['price'], 'listing_type_id': payload['listing_type_id'],
                               'tags': payload.get('tags', []), 'status': 'active', 'category_id': 'MLA1'}
            return {'id': nid}
        return {}


def _origen(**extra):
    base = {'id': 'PUB1', 'title': 'Cortador', 'seller_id': SELLER, 'user_product_id': UP, 'family_id': 7, 'price': 60000,
            'attributes': [{'id': 'COLOR', 'value_name': 'Rojo'}],
            'listing_type_id': 'gold_special', 'tags': [], 'status': 'active', 'category_id': 'MLA1',
            'domain_id': 'MLA-X', 'currency_id': 'ARS',
            'sale_terms': [{'id': 'WARRANTY_TYPE', 'value_id': '2230280', 'value_name': 'Garantía del vendedor', 'x': 1},
                           {'id': 'INSTALLMENTS_CAMPAIGN', 'value_name': '3x_campaign'}]}
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
        # Solo la garantia: la campaña de cuotas del original no pasa a la copia
        self.assertEqual(payload['sale_terms'], [{'id': 'WARRANTY_TYPE', 'value_id': '2230280',
                                                  'value_name': 'Garantía del vendedor'}])
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


class TestVariantes(unittest.TestCase):
    """Cada color es otro User Product de la misma familia: cada uno necesita
    su propia Pub 2 y Pub 3, con su propio costo."""

    def setUp(self):
        self.ml = FakeML({
            'PUB1': _origen(),
            'NEGRO1': _origen(id='NEGRO1', user_product_id='MLAU2', attributes=[{'id': 'COLOR', 'value_name': 'Negro'}]),
            'NEGRO6': _origen(id='NEGRO6', user_product_id='MLAU2', listing_type_id='gold_pro', tags=[],
                              attributes=[{'id': 'COLOR', 'value_name': 'Negro'}]),
            'CATA12': _origen(id='CATA12', user_product_id='MLAU2', listing_type_id='gold_pro', tags=['12x_campaign'],
                              catalog_listing=True, attributes=[{'id': 'COLOR', 'value_name': 'Negro'}]),
            'SOLOCAT': _origen(id='SOLOCAT', user_product_id='MLAU3', catalog_listing=True,
                               attributes=[{'id': 'COLOR', 'value_name': 'Azul'}]),
        })

    def test_familia_trae_todas_las_variantes_con_su_pub1(self):
        fam = dup.familia(self.ml, self.ml.get_item('PUB1'))
        por_up = {v['user_product_id']: v for v in fam}
        self.assertEqual(set(por_up), {UP, 'MLAU2', 'MLAU3'})
        self.assertTrue(por_up[UP]['es_la_cargada'])
        self.assertEqual(por_up['MLAU2']['variante'], 'Negro')
        self.assertEqual(por_up['MLAU2']['origen']['id'], 'NEGRO1')     # la sin cuotas, no la de 6
        self.assertIsNone(por_up['MLAU3']['origen'])                    # solo catalogo: no hay de donde duplicar

    def test_calcula_cada_variante_con_su_costo_y_sus_ocupadas(self):
        fam = dup.familia(self.ml, self.ml.get_item('PUB1'))
        grupos = {g['user_product_id']: g for g in dup.calcular_variantes(
            fam, {UP: COSTO, 'MLAU2': COSTO * 2, 'MLAU3': COSTO}, CARGOS, TASAS, 6)}
        rojo, negro, azul = grupos[UP], grupos['MLAU2'], grupos['MLAU3']
        self.assertIsNone(rojo['error'])
        self.assertGreater(negro['filas'][0]['precio'], rojo['filas'][0]['precio'])   # mas costo, mas precio
        self.assertEqual(negro['filas'][0]['ocupada_por'], 'NEGRO6')                    # ya tiene 6 cuotas propia
        self.assertIsNone(rojo['filas'][0]['ocupada_por'])
        self.assertTrue(azul['error'])
        # La de catalogo con 12 cuotas no bloquea una propia con 12 cuotas
        g12 = dup.calcular_variantes([v for v in fam if v['user_product_id'] == 'MLAU2'], {'MLAU2': COSTO}, CARGOS, TASAS, 12)
        self.assertIsNone(g12[0]['filas'][0]['ocupada_por'])

    def test_variante_sin_costo_no_se_calcula_y_las_demas_si(self):
        fam = dup.familia(self.ml, self.ml.get_item('PUB1'))
        grupos = {g['user_product_id']: g for g in dup.calcular_variantes(fam, {UP: COSTO}, CARGOS, TASAS, 6)}
        self.assertIn('costo', grupos['MLAU2']['error'])
        self.assertEqual(len(grupos[UP]['filas']), 2)

    def test_detalle_muestra_lo_que_hereda(self):
        item = _origen(pictures=[{'secure_url': 'https://x/1.jpg'}], shipping={'free_shipping': True, 'logistic_type': 'fulfillment'},
                       attributes=[{'id': 'BRAND', 'name': 'Marca', 'value_name': 'Biobella'}, {'id': 'X', 'name': 'Vacio', 'value_name': None}])
        d = dup.detalle(self.ml, item)
        self.assertEqual(d['fotos'], ['https://x/1.jpg'])
        self.assertEqual(d['ficha'], [{'nombre': 'Marca', 'valor': 'Biobella'}])
        self.assertEqual(d['descripcion'], 'Descripcion original')
        self.assertTrue(d['envio']['gratis'])


class TestDescripcionYFotos(unittest.TestCase):

    def setUp(self):
        self.alias = f'TestDupDF_{uuid.uuid4().hex[:8]}'
        self.ml = FakeML({'PUB1': _origen(pictures=[{'id': 'P1'}, {'id': 'P2'}, {'id': 'P3'}])})
        self.ml.diagnostico = {}

    def tearDown(self):
        p = dup._path(self.alias)
        if os.path.exists(p):
            os.remove(p)

    def test_descripcion_sin_emojis_ni_html(self):
        self.assertEqual(dup.validar_descripcion('  Envío gratis\r\nÑandú 100%  '), 'Envío gratis\nÑandú 100%')
        for mala in ('Oferta 🔥', 'Listo ✅', '<p>hola</p>'):
            with self.assertRaises(dup.DuplicadorError):
                dup.validar_descripcion(mala)

    def test_la_copia_usa_la_descripcion_editada(self):
        filas = dup.calcular(COSTO, CARGOS, TASAS, 12)
        b = dup.crear_borradores(self.alias, self.ml.get_item('PUB1'), filas, COSTO, 120)[0]
        dup.editar_descripcion(self.alias, b['id'], 'Texto propio de la copia')
        r = dup.aprobar(self.alias, b['id'], self.ml, CARGOS, TASAS, COSTO)
        self.assertIn((f'/items/{r["item_creado"]}/description', {'plain_text': 'Texto propio de la copia'}), self.ml.posts)
        with self.assertRaises(dup.DuplicadorError):            # ya aprobado: no se edita
            dup.editar_descripcion(self.alias, b['id'], 'otra')

    def test_restaurar_vuelve_a_la_del_original(self):
        filas = dup.calcular(COSTO, CARGOS, TASAS, 12)
        b = dup.crear_borradores(self.alias, self.ml.get_item('PUB1'), filas, COSTO, 120)[0]
        dup.editar_descripcion(self.alias, b['id'], 'cambiada')
        self.assertIsNone(dup.editar_descripcion(self.alias, b['id'], None)['descripcion'])

    def test_reordenar_fotos_existentes(self):
        r = dup.aplicar_fotos(self.ml, 'PUB1', [{'id': 'P3'}, {'id': 'P1'}])
        self.assertEqual(self.ml.puts, ('PUB1', {'pictures': [{'id': 'P3'}, {'id': 'P1'}]}))
        self.assertEqual(r['avisos'], [])
        # Solo se diagnostica la que pasa a ser principal
        diags = [p for p in self.ml.posts if p[0] == '/moderations/pictures/diagnostic']
        self.assertEqual([(d[1]['picture_id'], d[1]['context']['picture_type']) for d in diags], [('P3', 'thumbnail')])

    def test_foto_nueva_con_problemas_no_sube_nada(self):
        self.ml.diagnostico = {'base64': ['Elimina tus fotos que contienen logos y/o textos.']}
        nueva = {'nuevo': {'nombre': 'a.jpg', 'mime': 'image/jpeg', 'contenido': b'xx'}}
        with mock.patch.object(dup, '_subir_foto') as subir:
            with self.assertRaises(dup.DuplicadorError) as ctx:
                dup.aplicar_fotos(self.ml, 'PUB1', [{'id': 'P1'}, nueva])
            subir.assert_not_called()
        self.assertIn('logos', str(ctx.exception))
        self.assertIsNone(self.ml.puts)

    def test_foto_nueva_ok_se_sube_y_queda_en_su_lugar(self):
        nueva = {'nuevo': {'nombre': 'a.png', 'mime': 'image/png', 'contenido': b'xx'}}
        with mock.patch.object(dup, '_subir_foto', return_value='NUEVA1'):
            dup.aplicar_fotos(self.ml, 'PUB1', [nueva, {'id': 'P2'}])
        self.assertEqual(self.ml.puts[1]['pictures'], [{'id': 'NUEVA1'}, {'id': 'P2'}])

    def test_rechazos_de_fotos(self):
        casos = ([], [{'id': 'AJENA'}], [{'id': 'P1'}] * 5,
                 [{'nuevo': {'nombre': 'a.gif', 'mime': 'image/gif', 'contenido': b'x'}}],
                 [{'nuevo': {'nombre': 'g.jpg', 'mime': 'image/jpeg', 'contenido': b'x' * (dup.MAX_BYTES_FOTO + 1)}}])
        for plan in casos:
            with self.assertRaises(dup.DuplicadorError):
                dup.aplicar_fotos(self.ml, 'PUB1', plan)
        self.assertIsNone(self.ml.puts)

    def test_si_el_diagnostico_falla_avisa_y_sigue(self):
        orig = self.ml._post
        def falla(path, payload):
            if path == '/moderations/pictures/diagnostic':
                raise RuntimeError('500')
            return orig(path, payload)
        self.ml._post = falla
        r = dup.aplicar_fotos(self.ml, 'PUB1', [{'id': 'P2'}, {'id': 'P1'}])
        self.assertEqual(len(r['avisos']), 1)
        self.assertIsNotNone(self.ml.puts)


class TestListadoYLote(unittest.TestCase):

    def setUp(self):
        self.alias = f'TestDupL_{uuid.uuid4().hex[:8]}'
        self.ml = FakeML({
            'PUB1': _origen(sold_quantity=500),
            'OTRA': _origen(id='OTRA', user_product_id='MLAU9', sold_quantity=10),
            'SINCOSTO': _origen(id='SINCOSTO', user_product_id='MLAU8', sold_quantity=99),
            'CATA': _origen(id='CATA', user_product_id='MLAU7', catalog_listing=True, sold_quantity=999),
        })

    def tearDown(self):
        p = dup._path(self.alias)
        if os.path.exists(p):
            os.remove(p)

    def test_ordena_por_ventas_30_dias_sin_catalogo(self):
        stats = {'OTRA': {'units': 40}, 'PUB1': {'units': 5}}
        with mock.patch('modules.stock_rentabilidad._get_all_orders_30d', return_value=[]), \
             mock.patch('modules.stock_rentabilidad._compute_item_stats', return_value=stats):
            filas = dup.listado(self.ml, {'PUB1': {'costo': 15000}})
        self.assertEqual([f['id'] for f in filas], ['OTRA', 'PUB1', 'SINCOSTO'])   # 30d manda; empate por total
        self.assertEqual(filas[1]['costo'], 15000.0)
        self.assertIsNone(filas[0]['costo'])

    def test_lote_crea_los_validos_y_explica_el_resto(self):
        from flask import Flask
        from web import duplicador_routes as dr
        app = Flask(__name__); app.secret_key = 'x'; app.register_blueprint(dr.bp)
        self.ml.items['PUB1_12'] = _origen(id='PUB1_12', listing_type_id='gold_pro', tags=['12x_campaign'])
        costos = {'PUB1': 15000.0, 'OTRA': 15000.0}
        with mock.patch.object(dr, '_client', return_value=self.ml), \
             mock.patch.object(dr, '_tasas', return_value=TASAS), \
             mock.patch.object(dr, '_cargos', return_value=(CARGOS, 'test')), \
             mock.patch.object(dr, '_costo', side_effect=lambda i: costos.get(i)):
            r = app.test_client().post('/duplicar/api/borradores/lote', json={
                'alias': self.alias, 'item_ids': ['PUB1', 'OTRA', 'SINCOSTO'], 'escalon_pub2': 12, 'roi_objetivo': 120}).get_json()
        por_id = {x['item_id']: x for x in r['resultado']}
        self.assertEqual([c['nombre'] for c in por_id['PUB1']['creados']], ['Pub 3 · Clásica interés bajo'])
        self.assertIn('PUB1_12', por_id['PUB1']['omitidos'][0])           # ya tiene 12 cuotas
        self.assertEqual(len(por_id['OTRA']['creados']), 2)
        self.assertIn('costo', por_id['SINCOSTO']['omitidos'][0])
        self.assertEqual(r['creados'], 3)
        self.assertEqual(self.ml.posts, [])                                # no escribe en ML


if __name__ == '__main__':
    unittest.main()
