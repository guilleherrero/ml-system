"""Historial de infracciones: clasificacion por causa con motivos reales de ML
(Novara, 2025-2026) y paginacion de a 20 (la API rechaza limit > 20)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import moderaciones as mo  # noqa: E402


def _inf(i, reason, sub='PQT', item='MLA1', tipo='ITM', **extra):
    return {'id': str(i), 'date_created': f'2026-0{1 + i % 9}-01T10:00:00.000-0400', 'related_item_id': item,
            'element_type': tipo, 'filter_subgroup': sub, 'reason': reason, 'remedy': '<div><b>Corregí</b> la foto</div>', **extra}


class _Cuenta:
    user_id = 1


class FakeML:
    def __init__(self, infracciones):
        self.account = _Cuenta()
        self.infr = infracciones
        self.limits = []

    def _get(self, path, params=None):
        if path.startswith('/moderations/infractions/'):
            self.limits.append(params['limit'])
            assert params['limit'] <= 20
            o = params['offset']
            return {'infractions': self.infr[o:o + params['limit']], 'paging': {'total': len(self.infr)}}
        if path == '/items':
            return [{'code': 200, 'body': {'id': i, 'title': 'T', 'status': 'active'}} for i in params['ids'].split(',')]
        raise AssertionError(path)


class TestModeraciones(unittest.TestCase):

    def test_clasifica_motivos_reales(self):
        casos = {
            'Para esta categoría es necesario que la portada tenga fondo blanco digitalizado.': 'fotos',
            'La portada tiene logos y/o textos.': 'fotos',
            'Desenfocamos las fotos con datos de contacto.': 'contacto',      # foto, pero la causa es el contacto
            'Detectamos que el producto no era original.': 'originalidad',
            'No subiste la factura para comprobar la originalidad del producto.': 'originalidad',
            'Estaba en una categoría incorrecta.': 'categoria',
            'Era igual a otra de tus opciones de venta.': 'duplicado',
            'Verifica el producto de catálogo que te sugerimos para volver a vender.': 'catalogo',
            'El precio tiene un cambio inusual.': 'precio',
            'Incumplió nuestras políticas.': 'otras',
        }
        for motivo, esperado in casos.items():
            self.assertEqual(mo.clasificar(_inf(0, motivo, sub='AP')), esperado, motivo)
        self.assertEqual(mo.clasificar(_inf(0, '', tipo='ESP')), 'otros_elementos')
        self.assertEqual(mo.clasificar(_inf(0, '', sub='DOMAIN')), 'categoria')   # sin texto: el subgrupo de ML

    def test_pagina_de_a_20_y_agrupa(self):
        infr = [_inf(i, 'La portada tiene logos y/o textos.') for i in range(25)] + \
               [_inf(30 + i, 'Estaba en una categoría incorrecta.', sub='DOMAIN', item='MLA2',
                     suggested={'categories': [{'id': 'MLA47752', 'name': 'Tazas y Vasos Térmicos'}]}) for i in range(3)]
        ml = FakeML(infr)
        traidas = mo.traer_infracciones(ml, '2025-01-01')
        self.assertEqual(len(traidas), 28)
        self.assertEqual(ml.limits, [20, 20])
        r = mo.resumen(traidas, {'MLA1': {'status': 'active'}})
        self.assertEqual([(g['categoria'], g['total']) for g in r['grupos']], [('fotos', 25), ('categoria', 3)])
        self.assertEqual(r['grupos'][0]['motivos'][0]['solucion'], 'Corregí la foto')        # sin HTML
        self.assertEqual(r['grupos'][1]['motivos'][0]['casos'][0]['categoria_sugerida'], ['Tazas y Vasos Térmicos'])
        # 25 infracciones de la MISMA publicacion son 1 publicacion
        self.assertEqual(r['publicaciones_activas_afectadas'], 1)
        fotos = r['grupos'][0]
        self.assertEqual((fotos['publicaciones'], fotos['activas']), (1, 1))
        self.assertEqual(fotos['motivos'][0]['infracciones'], 25)
        self.assertEqual(fotos['motivos'][0]['casos'][0]['veces'], 25)

    def test_ids_que_no_son_publicaciones_no_se_consultan(self):
        r = mo.resumen([_inf(0, '', tipo='ESP', item='86d4389a1b664d6a8c5c3f7345e473fd55993545')], {})
        self.assertIsNone(r['grupos'][0]['motivos'][0]['casos'][0]['item_id'])


class TestCategoria(unittest.TestCase):

    def test_comparar(self):
        sug = [{'id': 'MLA47769', 'nombre': 'Termos'}, {'id': 'MLA47752', 'nombre': 'Tazas y Vasos'}]
        self.assertEqual(mo.comparar_categoria('MLA47769', sug), 'coincide')
        self.assertEqual(mo.comparar_categoria('MLA47752', sug), 'revisar')     # caso real: ML exigio la primera
        self.assertEqual(mo.comparar_categoria('MLA1', sug), 'revisar')
        self.assertEqual(mo.comparar_categoria('MLA1', []), 'sin_dato')         # sin sugerencia no se alerta
        self.assertEqual(mo.comparar_categoria('MLA1', None), 'sin_dato')


if __name__ == '__main__':
    unittest.main()
