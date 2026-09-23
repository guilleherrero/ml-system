"""
Respuestas a preguntas de compradores — generacion y envio, en un solo lugar.

La logica de generar tres opciones con IA vivia dentro de un endpoint de
web/app.py, asi que solo podia usarla la pantalla. Ahora tambien la usa el bot
de Telegram, que es donde mas falta hace: las preguntas llegan a cualquier hora
y responder rapido mueve la conversion, pero casi nunca uno esta frente a la
computadora cuando entran.

Las tres opciones son deliberadamente distintas entre si — directa, completa y
orientada a la venta — para que elegir sea una decision real y no un tramite.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta

import requests

from core.db_storage import db_load, db_save

_logger = logging.getLogger(__name__)

_ML = 'https://api.mercadolibre.com'
MODELO = 'claude-sonnet-4-6'

# Tope de preguntas que se mandan a generar por corrida. Cada una es una llamada
# a la IA: sin tope, una avalancha de preguntas se traduce en una factura.
MAX_POR_CORRIDA = 5

# Tope de llamadas a la IA por dia para el job automatico de preguntas. Pasado
# el tope, la pregunta igual llega al celular, solo que sin sugerencias.
# ~150 llamadas de Sonnet son del orden de US$2.
MAX_LLAMADAS_POR_DIA = 150

# Opciones ya generadas, por question_id. El job corre cada 15 minutos y solo
# marca una pregunta como avisada si Telegram acepto el mensaje: sin este cache,
# un envio que falla regeneraba las respuestas con la IA en cada corrida.
CACHE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data',
                          'respuestas_ia_cache.json')
CACHE_DIAS = 8

_PROMPT = """Sos vendedor experto de MercadoLibre Argentina. Generás 3 respuestas distintas para la misma pregunta de comprador.

Producto: {titulo}{desc}

Pregunta: {pregunta}

GENERÁ EXACTAMENTE en este formato (sin cambiar los encabezados):

OPCIÓN A — DIRECTA:
[1-2 oraciones, respuesta corta y al punto, sin adornos]

OPCIÓN B — COMPLETA:
[3 oraciones, responde + agrega detalle técnico/especificación relevante]

OPCIÓN C — ORIENTADA A LA VENTA:
[2-3 oraciones, responde + destaca un beneficio del producto + invita a comprar con naturalidad]

REGLAS obligatorias para las 3:
- Español rioplatense (vos, tus, te)
- Sin emojis
- Sin signos de exclamación múltiples
- Tono profesional y cálido
- No repetir el título del producto completo"""

_DEFS = [
    ('A', 'Directa',            r'OPCIÓN A[^\n]*:\s*\n([\s\S]+?)(?=OPCIÓN B|\Z)'),
    ('B', 'Completa',           r'OPCIÓN B[^\n]*:\s*\n([\s\S]+?)(?=OPCIÓN C|\Z)'),
    ('C', 'Orientada a la venta', r'OPCIÓN C[^\n]*:\s*\n([\s\S]+?)(?=\Z)'),
]


def generar_opciones(pregunta: str, item_titulo: str = '',
                     item_descripcion: str = '',
                     on_tokens=None) -> list[dict]:
    """Tres respuestas distintas para la misma pregunta. [] si la IA no responde.

    `on_tokens(modelo, in, out)` permite al llamador registrar el costo con su
    propio logger sin que este modulo dependa de web/app.py.
    """
    if not (pregunta or '').strip():
        return []
    if not os.environ.get('ANTHROPIC_API_KEY'):
        _logger.warning('[respuestas_ia] sin ANTHROPIC_API_KEY')
        return []

    desc = f'\nDescripción del producto: {item_descripcion[:400]}' if item_descripcion else ''
    prompt = _PROMPT.format(titulo=item_titulo, desc=desc, pregunta=pregunta)

    try:
        import anthropic
        ai = anthropic.Anthropic()
        resp = ai.messages.create(model=MODELO, max_tokens=600,
                                  messages=[{'role': 'user', 'content': prompt}])
        if on_tokens:
            try:
                on_tokens(MODELO, resp.usage.input_tokens, resp.usage.output_tokens)
            except Exception:
                pass
        texto = resp.content[0].text
    except Exception as e:
        _logger.error('[respuestas_ia] fallo la generacion: %s', e)
        return []

    opciones = []
    for key, label, patron in _DEFS:
        m = re.search(patron, texto, re.DOTALL)
        cuerpo = (m.group(1).strip() if m else '')
        if cuerpo:
            opciones.append({'key': key, 'label': label, 'text': cuerpo})
    return opciones


def _cache() -> dict:
    d = db_load(CACHE_PATH) or {}
    limite = (datetime.now() - timedelta(days=CACHE_DIAS)).isoformat(timespec='seconds')
    d['preguntas'] = {qid: v for qid, v in (d.get('preguntas') or {}).items()
                      if (v.get('ts') or '') >= limite}
    return d


def opciones_para_pregunta(question_id, pregunta: str, item_titulo: str = '',
                           item_descripcion: str = '',
                           on_tokens=None) -> list[dict]:
    """Como generar_opciones, pero para el job automatico: una sola llamada a la
    IA por pregunta y como mucho MAX_LLAMADAS_POR_DIA por dia.

    Si la pregunta ya tiene opciones generadas, las devuelve sin llamar a la IA.
    Si se paso el tope diario, devuelve [] y la pregunta sale sin sugerencias.
    """
    qid = str(question_id)
    d = _cache()
    previas = d['preguntas'].get(qid)
    if previas and previas.get('opciones'):
        return previas['opciones']

    hoy = datetime.now().strftime('%Y-%m-%d')
    uso = d.get('uso') or {}
    if uso.get('fecha') != hoy:
        uso = {'fecha': hoy, 'llamadas': 0}
    if uso['llamadas'] >= MAX_LLAMADAS_POR_DIA:
        _logger.warning('[respuestas_ia] tope diario de %s llamadas alcanzado, '
                        'pregunta %s sin sugerencias', MAX_LLAMADAS_POR_DIA, qid)
        return []

    # Se cuenta el intento aunque falle: si la IA falla en cada corrida, el tope
    # tambien corta ese reintento.
    uso['llamadas'] += 1
    d['uso'] = uso
    opciones = generar_opciones(pregunta, item_titulo, item_descripcion, on_tokens)
    if opciones:
        d['preguntas'][qid] = {'opciones': opciones,
                               'ts': datetime.now().isoformat(timespec='seconds')}
    try:
        db_save(CACHE_PATH, d)
    except Exception as e:
        _logger.error('[respuestas_ia] no se pudo guardar el cache: %s', e)
    return opciones


def descripcion_item(item_id: str, token: str) -> str:
    try:
        r = requests.get(f'{_ML}/items/{item_id}/description',
                         headers={'Authorization': f'Bearer {token}'}, timeout=6)
        if r.ok:
            return (r.json() or {}).get('plain_text', '')[:400]
    except requests.RequestException:
        pass
    return ''


def titulo_item(item_id: str, token: str) -> str:
    try:
        r = requests.get(f'{_ML}/items/{item_id}',
                         headers={'Authorization': f'Bearer {token}'},
                         params={'attributes': 'id,title'}, timeout=6)
        if r.ok:
            return (r.json() or {}).get('title', '')
    except requests.RequestException:
        pass
    return ''


def responder_en_ml(question_id, texto: str, token: str) -> dict:
    """Publica la respuesta en MercadoLibre. Devuelve {ok, error}."""
    if not (texto or '').strip():
        return {'ok': False, 'error': 'respuesta vacia'}
    try:
        r = requests.post(f'{_ML}/answers',
                          json={'question_id': question_id, 'text': texto.strip()},
                          headers={'Authorization': f'Bearer {token}',
                                   'Content-Type': 'application/json'},
                          timeout=10)
        if r.ok:
            return {'ok': True}
        return {'ok': False, 'error': f'ML respondio {r.status_code}: {r.text[:200]}'}
    except requests.RequestException as e:
        return {'ok': False, 'error': str(e)}
