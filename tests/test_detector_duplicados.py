"""Tests para modules/detector_duplicados.py.

Cubre la regla del usuario (09/05/2026): dos publicaciones son duplicado
SI Y SOLO SI son idénticas en TODOS estos ejes:
  - precio (con tolerancia ±$50 por bucket de 100)
  - listing_type (Clásica vs Premium)
  - free_shipping (con/sin envío gratis)
  - color (vía attribute oficial COLOR/COLOR_NAME/MAIN_COLOR o token del título)
  - talle (vía attribute oficial SIZE/SIZE_NAME/SIZE_GRID_ID o token del título)

Si difieren en cualquier eje → NO son duplicados, no aparecen en /duplicados.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.detector_duplicados import (  # noqa: E402
    _clave_duplicacion,
    _subdividir_cluster,
    _extraer_token_variante,
    _TOKENS_COLOR,
    _TOKENS_TALLE,
    detectar_duplicados,
)


def _item(id_, titulo, precio=89000, listing_type='gold_special',
          free_shipping=True, ventas_30d=0, visitas_30d=100,
          conversion_pct=5.0):
    """Helper para crear un item de prueba con campos relevantes."""
    return {
        'id': id_,
        'titulo': titulo,
        'precio': precio,
        'listing_type': listing_type,
        'free_shipping': free_shipping,
        'ventas_30d': ventas_30d,
        'visitas_30d': visitas_30d,
        'conversion_pct': conversion_pct,
    }


class TestExtraerTokenVariante(unittest.TestCase):

    def test_color_en_titulo(self):
        self.assertEqual(_extraer_token_variante('Cortador Puntas Rojo', _TOKENS_COLOR), 'rojo')
        self.assertEqual(_extraer_token_variante('Botella Negro Premium', _TOKENS_COLOR), 'negro')

    def test_sin_color_en_titulo_devuelve_vacio(self):
        self.assertEqual(_extraer_token_variante('Cortador Puntas Ender Pro', _TOKENS_COLOR), '')

    def test_talle_l_en_titulo(self):
        self.assertEqual(_extraer_token_variante('Faja Postparto Talle L', _TOKENS_TALLE), 'l')


class TestClaveDuplicacion(unittest.TestCase):

    def test_items_identicos_misma_clave(self):
        a = _item('A', 'Cortador Pro', precio=89000, listing_type='gold_special', free_shipping=True)
        b = _item('B', 'Cortador Pro', precio=89000, listing_type='gold_special', free_shipping=True)
        self.assertEqual(_clave_duplicacion(a, []), _clave_duplicacion(b, []))

    def test_color_distinto_clave_distinta(self):
        a = _item('A', 'Cortador Rojo', precio=89000)
        b = _item('B', 'Cortador Negro', precio=89000)
        self.assertNotEqual(_clave_duplicacion(a, []), _clave_duplicacion(b, []))

    def test_precio_distinto_clave_distinta(self):
        a = _item('A', 'Cortador', precio=74990)
        b = _item('B', 'Cortador', precio=89000)
        self.assertNotEqual(_clave_duplicacion(a, []), _clave_duplicacion(b, []))

    def test_listing_distinto_clave_distinta(self):
        a = _item('A', 'Cortador', precio=89000, listing_type='gold_special')
        b = _item('B', 'Cortador', precio=89000, listing_type='gold_pro')
        self.assertNotEqual(_clave_duplicacion(a, []), _clave_duplicacion(b, []))

    def test_free_shipping_distinto_clave_distinta(self):
        a = _item('A', 'Cortador', precio=89000, free_shipping=True)
        b = _item('B', 'Cortador', precio=89000, free_shipping=False)
        self.assertNotEqual(_clave_duplicacion(a, []), _clave_duplicacion(b, []))

    def test_attribute_oficial_color_diferencia_items(self):
        # Items con título idéntico (sin color) pero attribute COLOR distinto
        a = _item('A', 'Cortador Generico', precio=89000)
        b = _item('B', 'Cortador Generico', precio=89000)
        attrs_a = [{'id': 'COLOR', 'value_name': 'Rojo'}]
        attrs_b = [{'id': 'COLOR', 'value_name': 'Negro'}]
        self.assertNotEqual(_clave_duplicacion(a, attrs_a), _clave_duplicacion(b, attrs_b))

    def test_precio_con_tolerancia_pequena(self):
        # Bucket de $100 → ±$50 deben caer en el mismo bucket
        a = _item('A', 'Cortador', precio=89000)
        b = _item('B', 'Cortador', precio=89049)  # mismo bucket de 89000
        self.assertEqual(_clave_duplicacion(a, [])[0], _clave_duplicacion(b, [])[0])

    def test_precio_diferencia_grande_clave_distinta(self):
        a = _item('A', 'Cortador', precio=89000)
        c = _item('C', 'Cortador', precio=89150)  # bucket de 89200 (distinto)
        self.assertNotEqual(_clave_duplicacion(a, [])[0], _clave_duplicacion(c, [])[0])


class TestSubdividirCluster(unittest.TestCase):

    def test_cortador_puntas_caso_testigo_real(self):
        """Cluster real (7 items) debe subdividirse en 4 sub-clusters:
        2 singletons (color rojo, color negro), 1 grupo de 3 (Clásica Pro $89K),
        1 grupo de 2 (Premium Pro $89K). Pausar resultante: 3 (no 6)."""
        items = [
            _item('A1', 'Cortador Puntas Para El Cabello Rojo', precio=74990, listing_type='gold_special'),
            _item('A2', 'Cortador Puntas Para El Cabello Negro', precio=74990, listing_type='gold_special'),
            _item('B1', 'Cortador Puntas Ender Pro', precio=89000, listing_type='gold_special'),
            _item('B2', 'Cortador Puntas Ender Pro', precio=89000, listing_type='gold_special'),
            _item('B3', 'Cortador Puntas Ender Pro', precio=89000, listing_type='gold_special'),
            _item('C1', 'Cortador Puntas Ender Pro', precio=89000, listing_type='gold_pro'),
            _item('C2', 'Cortador Puntas Ender Pro', precio=89000, listing_type='gold_pro'),
        ]
        sub = _subdividir_cluster(items, {})
        self.assertEqual(len(sub), 4)
        self.assertEqual(sorted(len(s) for s in sub), [1, 1, 2, 3])
        # Total a pausar = sum(len(s) - 1 for s in sub if len(s) >= 2) = 2 + 1 = 3
        total_pausar = sum(len(s) - 1 for s in sub if len(s) >= 2)
        self.assertEqual(total_pausar, 3)

    def test_grupo_homogeneo_se_mantiene_como_un_subcluster(self):
        # Items idénticos en TODO → 1 sub-cluster con todos los items
        items = [
            _item('A', 'Cepillo Secador', precio=45000, listing_type='gold_special'),
            _item('B', 'Cepillo Secador', precio=45000, listing_type='gold_special'),
        ]
        sub = _subdividir_cluster(items, {})
        self.assertEqual(len(sub), 1)
        self.assertEqual(len(sub[0]), 2)

    def test_lista_vacia_devuelve_vacio(self):
        self.assertEqual(_subdividir_cluster([], {}), [])

    def test_un_solo_item_devuelve_singleton(self):
        items = [_item('A', 'Solo')]
        sub = _subdividir_cluster(items, {})
        self.assertEqual(sub, [items])

    def test_attributes_oficiales_dividen_por_color(self):
        # Mismo título y precio, pero attributes oficiales con color distinto
        items = [
            _item('A', 'Cortador Generico', precio=89000),
            _item('B', 'Cortador Generico', precio=89000),
        ]
        attrs = {
            'A': [{'id': 'COLOR', 'value_name': 'Rojo'}],
            'B': [{'id': 'COLOR', 'value_name': 'Negro'}],
        }
        sub = _subdividir_cluster(items, attrs)
        self.assertEqual(len(sub), 2)
        # Cada uno es singleton → ningún duplicado real
        self.assertTrue(all(len(s) == 1 for s in sub))


class TestDetectarDuplicadosEndToEnd(unittest.TestCase):
    """Tests del entry-point completo. Usa tempdir para data/ así
    no toca ignorados ni cache real."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_cortador_puntas_devuelve_2_clusters_no_1(self):
        """De los 7 items reales del Cortador Puntas, deben emerger 2 clusters
        (puro Clásica + puro Premium). Los 2 singletons de color (Rojo/Negro)
        NO deben aparecer."""
        items = [
            _item('A1', 'Cortador Puntas Para El Cabello Rojo',
                  precio=74990, listing_type='gold_special', visitas_30d=200),
            _item('A2', 'Cortador Puntas Para El Cabello Negro',
                  precio=74990, listing_type='gold_special', visitas_30d=180),
            _item('B1', 'Cortador Puntas Ender Pro',
                  precio=89000, listing_type='gold_special', visitas_30d=1024, ventas_30d=10),
            _item('B2', 'Cortador Puntas Ender Pro',
                  precio=89000, listing_type='gold_special', visitas_30d=349),
            _item('B3', 'Cortador Puntas Ender Pro',
                  precio=89000, listing_type='gold_special', visitas_30d=129),
            _item('C1', 'Cortador Puntas Ender Pro',
                  precio=89000, listing_type='gold_pro', visitas_30d=3309, ventas_30d=20),
            _item('C2', 'Cortador Puntas Ender Pro',
                  precio=89000, listing_type='gold_pro', visitas_30d=426),
        ]
        clusters = detectar_duplicados(items, 'TestAccount', self.tmpdir)
        self.assertEqual(len(clusters), 2)
        self.assertTrue(all(c.severidad == 'puro' for c in clusters))
        # Total items en clusters duplicados: 5 (3 + 2)
        self.assertEqual(sum(len(c.items) for c in clusters), 5)
        # Total a pausar: 3 (= total items 5 - ganadoras 2)
        ganadoras = sum(1 for c in clusters for it in c.items if it.es_ganadora)
        self.assertEqual(ganadoras, 2)
        a_pausar = sum(len(c.items) for c in clusters) - ganadoras
        self.assertEqual(a_pausar, 3)

    def test_items_distintos_por_precio_no_son_duplicados(self):
        items = [
            _item('A', 'Producto X', precio=10000),
            _item('B', 'Producto X', precio=15000),
        ]
        clusters = detectar_duplicados(items, 'TestAccount', self.tmpdir)
        self.assertEqual(len(clusters), 0)

    def test_items_distintos_por_listing_no_son_duplicados(self):
        items = [
            _item('A', 'Producto X', precio=10000, listing_type='gold_special'),
            _item('B', 'Producto X', precio=10000, listing_type='gold_pro'),
        ]
        clusters = detectar_duplicados(items, 'TestAccount', self.tmpdir)
        self.assertEqual(len(clusters), 0)

    def test_items_distintos_por_free_shipping_no_son_duplicados(self):
        items = [
            _item('A', 'Producto X', precio=10000, free_shipping=True),
            _item('B', 'Producto X', precio=10000, free_shipping=False),
        ]
        clusters = detectar_duplicados(items, 'TestAccount', self.tmpdir)
        self.assertEqual(len(clusters), 0)

    def test_items_identicos_son_duplicados(self):
        items = [
            _item('A', 'Producto X', precio=10000, listing_type='gold_special', free_shipping=True),
            _item('B', 'Producto X', precio=10000, listing_type='gold_special', free_shipping=True),
        ]
        clusters = detectar_duplicados(items, 'TestAccount', self.tmpdir)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].severidad, 'puro')
        self.assertEqual(len(clusters[0].items), 2)

    def test_impacto_monetario_se_calcula_por_subcluster(self):
        """B1 (ganadora con 5% conv y precio $89K) + B2 + B3 con 478 visitas
        perdidas. Impacto = 478 × 0.05 × 89000 ≈ $2.13M."""
        items = [
            _item('B1', 'Producto X', precio=89000, ventas_30d=10, visitas_30d=1024, conversion_pct=5.0),
            _item('B2', 'Producto X', precio=89000, ventas_30d=0, visitas_30d=349, conversion_pct=0),
            _item('B3', 'Producto X', precio=89000, ventas_30d=0, visitas_30d=129, conversion_pct=0),
        ]
        clusters = detectar_duplicados(items, 'TestAccount', self.tmpdir)
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        # Visitas perdidas = 349 + 129 = 478
        self.assertEqual(c.visitas_perdidas_30d, 478)
        # Impacto = 478 × 0.05 × 89000 = 2,127,100
        expected = 478 * 0.05 * 89000
        self.assertAlmostEqual(c.impacto_monetario_estimado, expected, delta=1)

    def test_color_en_titulo_separa_publicaciones(self):
        """2 publicaciones con color distinto en título no son duplicados."""
        items = [
            _item('A', 'Faja Reductora Postparto Negro', precio=50000),
            _item('B', 'Faja Reductora Postparto Blanco', precio=50000),
        ]
        clusters = detectar_duplicados(items, 'TestAccount', self.tmpdir)
        self.assertEqual(len(clusters), 0)


if __name__ == '__main__':
    unittest.main()
