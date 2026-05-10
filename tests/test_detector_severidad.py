"""Tests del hotfix #3 (09/05/2026): severidades puro/mixto/legitimo y la
inclusión de cuotas + logistic_type como ejes de subdivisión.

Cubre:
- Cluster idéntico en los 7 ejes → severidad 'puro'
- Cluster con cuotas distintas → cluster 'legitimo'
- Cluster con logistic_type distinto (Full vs no Full) → cluster 'legitimo'
- Cluster mixto: algunas variantes legítimas + algunos duplicados → 'mixto'
- KPIs de resumen_para_alertas: legitimo NO suma a impacto/visitas
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.detector_duplicados import (  # noqa: E402
    detectar_duplicados,
    resumen_para_alertas,
    _clave_duplicacion,
    _detectar_ejes_diferentes,
)


def _item(id_, titulo, precio=89000, listing_type='gold_special',
          free_shipping=True, ventas_30d=0, visitas_30d=100,
          conversion_pct=5.0):
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


def _meta(installments_qty=12, installments_rate=0.0, logistic_type='fulfillment',
          attributes=None):
    """Construye metadata enriquecida (dict shape hotfix #3)."""
    return {
        'attributes': attributes or [],
        'installments': {
            'quantity': installments_qty,
            'rate':     installments_rate,
            'amount':   1000.0,
        },
        'shipping': {
            'free_shipping': True,
            'mode':          'me2',
            'logistic_type': logistic_type,
        },
    }


class TestClavesConCuotasYLogistic(unittest.TestCase):

    def test_cuotas_distintas_clave_distinta(self):
        item = _item('A', 'X')
        clave_12 = _clave_duplicacion(item, _meta(installments_qty=12))
        clave_18 = _clave_duplicacion(item, _meta(installments_qty=18))
        self.assertNotEqual(clave_12, clave_18)

    def test_cuotas_con_vs_sin_interes_clave_distinta(self):
        item = _item('A', 'X')
        clave_si = _clave_duplicacion(item, _meta(installments_rate=0.0))
        clave_no = _clave_duplicacion(item, _meta(installments_rate=5.0))
        self.assertNotEqual(clave_si, clave_no)

    def test_logistic_full_vs_cross_docking_clave_distinta(self):
        item = _item('A', 'X')
        clave_full = _clave_duplicacion(item, _meta(logistic_type='fulfillment'))
        clave_cd   = _clave_duplicacion(item, _meta(logistic_type='cross_docking'))
        self.assertNotEqual(clave_full, clave_cd)

    def test_metadata_dict_y_metadata_list_legacy_compatibles(self):
        # Pasar list (legacy) o dict (nuevo) sin installments/shipping deben
        # devolver la misma clave (los nuevos ejes quedan en bucket default)
        item = _item('A', 'X')
        clave_list = _clave_duplicacion(item, [{'id': 'COLOR', 'value_name': 'Negro'}])
        clave_dict = _clave_duplicacion(item, {'attributes': [{'id': 'COLOR', 'value_name': 'Negro'}]})
        self.assertEqual(clave_list, clave_dict)


class TestSeveridadEndToEnd(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_cluster_identico_emite_puro(self):
        # 2 items idénticos en TODO + sin metadata API (logistic '' / cuotas (0,True))
        items = [
            _item('A', 'Faja Postparto Negro Talle L', precio=50000,
                  listing_type='gold_special', free_shipping=True),
            _item('B', 'Faja Postparto Negro Talle L', precio=50000,
                  listing_type='gold_special', free_shipping=True),
        ]
        clusters = detectar_duplicados(items, 'TestSev', self.tmpdir)
        puros = [c for c in clusters if c.severidad == 'puro']
        self.assertEqual(len(puros), 1, 'Debería emitir 1 cluster puro')
        self.assertEqual(len(puros[0].items), 2)

    def test_cluster_con_color_distinto_emite_legitimo(self):
        items = [
            _item('A', 'Faja Postparto Negro Talle L', precio=50000),
            _item('B', 'Faja Postparto Blanco Talle L', precio=50000),
        ]
        clusters = detectar_duplicados(items, 'TestSev', self.tmpdir)
        # Sin duplicados puro/mixto
        self.assertEqual(len([c for c in clusters if c.severidad in ('puro', 'mixto')]), 0)
        # Pero sí debe haber 1 cluster informativo legitimo
        legit = [c for c in clusters if c.severidad == 'legitimo']
        self.assertEqual(len(legit), 1)
        # La razon debe mencionar 'color'
        self.assertIn('color', legit[0].nota_legitimidad.lower())

    def test_cluster_mixto_cuando_hay_split_y_duplicados(self):
        # Cluster original con 4 items: 2 idénticos + 2 con color distinto
        items = [
            _item('D1', 'Cortador Puntas Pro', precio=89000, listing_type='gold_special'),
            _item('D2', 'Cortador Puntas Pro', precio=89000, listing_type='gold_special'),
            _item('V1', 'Cortador Puntas Rojo', precio=89000, listing_type='gold_special'),
            _item('V2', 'Cortador Puntas Negro', precio=89000, listing_type='gold_special'),
        ]
        clusters = detectar_duplicados(items, 'TestSev', self.tmpdir)
        mixtos = [c for c in clusters if c.severidad == 'mixto']
        # Hay 1 sub-cluster duplicado (D1+D2) emitido como mixto
        self.assertEqual(len(mixtos), 1)
        self.assertEqual(len(mixtos[0].items), 2)
        # NO debe haber 'puro' (porque hubo split)
        self.assertEqual(len([c for c in clusters if c.severidad == 'puro']), 0)

    def test_legitimo_no_suma_a_impacto_monetario(self):
        # 2 items con precios distintos → emiten cluster legitimo
        items = [
            _item('A', 'Producto X', precio=10000, visitas_30d=500),
            _item('B', 'Producto X', precio=15000, visitas_30d=300),
        ]
        clusters = detectar_duplicados(items, 'TestSev', self.tmpdir)
        resumen = resumen_para_alertas(clusters)
        self.assertEqual(resumen['legitimos'], 1)
        # Impacto y visitas perdidas deben ser 0 (legitimo no cuenta)
        self.assertEqual(resumen['impacto_monetario_estimado'], 0.0)
        self.assertEqual(resumen['visitas_perdidas_30d'], 0)

    def test_kpis_resumen_separa_puros_y_legitimos(self):
        # Dos clusters originales DIFERENTES por título — uno puro, uno legitimo
        # Items con prefijos distintos para que _construir_clusters no los junte
        items = [
            # Cluster 1: 2 fajas idénticas → severidad puro
            _item('P1', 'Faja Reductora Postparto Premium', precio=20000, visitas_30d=200),
            _item('P2', 'Faja Reductora Postparto Premium', precio=20000, visitas_30d=100),
            # Cluster 2: 2 cortadores con precio distinto → legitimo
            _item('L1', 'Cortador Pelo Inalambrico Pro', precio=30000),
            _item('L2', 'Cortador Pelo Inalambrico Pro', precio=45000),
        ]
        clusters = detectar_duplicados(items, 'TestSev', self.tmpdir)
        resumen = resumen_para_alertas(clusters)
        self.assertEqual(resumen['puros'], 1, f'Esperaba 1 puro, obtuve {resumen}')
        self.assertEqual(resumen['legitimos'], 1, f'Esperaba 1 legitimo, obtuve {resumen}')
        self.assertEqual(resumen['mixtos'], 0)
        # Impacto > 0 (del puro)
        self.assertGreater(resumen['impacto_monetario_estimado'], 0)


class TestDetectarEjesDiferentes(unittest.TestCase):

    def test_devuelve_color_cuando_difiere(self):
        from modules.detector_duplicados import _subdividir_cluster
        items = [
            _item('A', 'Faja Negro', precio=50000),
            _item('B', 'Faja Blanco', precio=50000),
        ]
        sub = _subdividir_cluster(items, {})
        ejes = _detectar_ejes_diferentes(sub, {})
        self.assertIn('color', ejes)

    def test_devuelve_precio_cuando_difiere(self):
        from modules.detector_duplicados import _subdividir_cluster
        items = [
            _item('A', 'Producto', precio=10000),
            _item('B', 'Producto', precio=20000),
        ]
        sub = _subdividir_cluster(items, {})
        ejes = _detectar_ejes_diferentes(sub, {})
        self.assertIn('precio', ejes)

    def test_lista_vacia_si_un_solo_subgrupo(self):
        from modules.detector_duplicados import _subdividir_cluster
        items = [
            _item('A', 'Producto', precio=50000),
            _item('B', 'Producto', precio=50000),
        ]
        sub = _subdividir_cluster(items, {})
        ejes = _detectar_ejes_diferentes(sub, {})
        self.assertEqual(ejes, [])


if __name__ == '__main__':
    unittest.main()
