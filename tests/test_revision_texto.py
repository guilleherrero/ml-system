"""Revision de texto: bloquea lo que ML rechaza seguro (contacto, emojis,
HTML) y avisa el resto, sin marcar modelos, medidas ni codigos de barras."""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import duplicador as dup  # noqa: E402
from modules import revision_texto as rt  # noqa: E402


def _reglas(texto, campo='descripcion', listas=None):
    return [(h['nivel'], h['regla'], h['texto']) for h in rt.revisar(texto, campo, listas)]


class TestRevisionTexto(unittest.TestCase):

    def test_no_marca_lo_que_no_es_contacto(self):
        for t, campo in (('Cortador De Puntas Abiertas Ender Pro RH-6668 19.5 cm 300 ml', 'titulo'),
                         ('Código de barras 7798123456789, modelo 2024', 'descripcion'),
                         ('GTIN: 7798123456789 | Modelo: RH-6668', 'ficha'),
                         ('Tipo de piel: todo tipo de piel', 'ficha'),
                         ('Batería 1200 mAh, carga en 3 horas, 6V', 'descripcion')):
            self.assertEqual(_reglas(t, campo), [], t)

    def test_bloquea_contacto_una_vez_por_dato(self):
        self.assertEqual(_reglas('Llamanos al 11 4567-8901 o escribinos'),
                         [('bloquea', 'contacto', 'Llamanos al 11 4567-8901')])
        self.assertEqual(_reglas('+54 9 11 4567 8901'), [('bloquea', 'contacto', '+54 9 11 4567 8901')])
        self.assertEqual(_reglas('Mail ventas@novara.com'), [('bloquea', 'contacto', 'ventas@novara.com')])
        self.assertEqual(sorted(_reglas('Seguinos en @novara.oficial y visitá www.novara.com.ar')),
                         [('bloquea', 'contacto', '@novara.oficial'), ('bloquea', 'contacto', 'www.novara.com.ar')])

    def test_emoji_y_html(self):
        self.assertIn(('bloquea', 'emoji', '🚚'), _reglas('Envío gratis 🚚', 'titulo'))
        self.assertTrue(any(r[1] == 'html' for r in _reglas('<p>hola</p>')))
        self.assertFalse(any(r[1] == 'html' for r in _reglas('<p>hola</p>', 'titulo')))

    def test_avisos_no_bloquean(self):
        r = _reglas('Aro de luz ideal para TikTok', 'titulo')
        self.assertEqual(r, [('aviso', 'red_social', 'TikTok')])
        self.assertEqual({x[1] for x in _reglas('Réplica AAA 1:1', 'titulo')}, {'imitacion'})
        self.assertEqual(rt.bloquea(rt.revisar('Réplica AAA', 'titulo')), [])

    def test_lista_propia_y_permitidas(self):
        listas = {'prohibidas': ['dyson'], 'permitidas': ['tiktok']}
        self.assertEqual(_reglas('Secador estilo Dyson para TikTok', 'titulo', listas),
                         [('aviso', 'lista_propia', 'dyson')])
        self.assertEqual(_reglas('Dysonic', 'titulo', listas), [])      # palabra entera, no subcadena


class TestDuplicadorUsaLaRevision(unittest.TestCase):

    def test_descripcion_con_contacto_no_se_guarda(self):
        with self.assertRaises(dup.DuplicadorError) as ctx:
            dup.validar_descripcion('Consultas al WhatsApp 11 4567 8901')
        self.assertIn('teléfono', str(ctx.exception))
        self.assertEqual(dup.validar_descripcion('Ideal para TikTok'), 'Ideal para TikTok')   # aviso, no bloquea
        self.assertEqual([h['regla'] for h in dup.avisos_descripcion('Ideal para TikTok')], ['red_social'])


if __name__ == '__main__':
    unittest.main()
