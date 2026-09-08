"""
Bot de Telegram del Sistema ML — Sprint B, bandeja unica.

La regla que ordena todo esto: Telegram NO es una cola aparte. Es otra puerta a
la misma bandeja de Cerebro. Aprobar una propuesta desde el celular la deja
`aprobada` en `data/cerebro_<Alias>/acciones.json`, que es lo mismo que ve el
panel; y una aprobada desde el panel aparece resuelta si despues se toca el
boton en Telegram. El estado vive en Cerebro, nunca en el canal.

Que llega al telefono en el momento (decision del usuario):
  - lo urgente: buy box perdida, quiebre de stock con trafico, reclamos,
    preguntas sin responder
  - las propuestas de precio, con botones para aprobar o rechazar ahi mismo
Que se junta en el resumen diario:
  - movimientos de competidores, veredictos de Cerebro, Top 3

Configuracion:
  - `TELEGRAM_BOT_TOKEN` como variable de entorno en Render (nunca en el repo).
  - El `chat_id` NO se configura a mano: el bot lo aprende cuando el usuario le
    manda /start, y lo guarda en `data/telegram_config.json`.
  - El webhook se registra desde la pantalla de ajustes.

Nada de esto puede romper el sistema: si Telegram esta caido, mal configurado o
el token vencio, `enviar()` devuelve False y loguea. Un aviso que no sale no
puede tumbar un cron.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import requests

from core.db_storage import db_load, db_save

_logger = logging.getLogger(__name__)

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'telegram_config.json')
_API        = 'https://api.telegram.org/bot'
_TIMEOUT    = 10

# Tipos de aviso y si van al telefono en el momento o al resumen diario
AVISOS_INMEDIATOS = {
    'buybox_perdida':   True,
    'stock_critico':    True,
    'reclamo':          True,
    'preguntas':        True,
    'propuesta_precio': True,
    'competidor':       False,
    'veredicto':        False,
    'top_acciones':     False,
}


# ── Configuracion ────────────────────────────────────────────────────────────

def _config() -> dict:
    cfg = db_load(CONFIG_PATH) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault('chat_ids', [])
    cfg.setdefault('avisos', dict(AVISOS_INMEDIATOS))
    cfg.setdefault('pendientes_resumen', [])
    return cfg


def _guardar(cfg: dict) -> None:
    try:
        db_save(CONFIG_PATH, cfg)
    except Exception as e:
        _logger.error('[telegram] no pude guardar config: %s', e)


def token() -> str:
    return os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()


def configurado() -> bool:
    return bool(token())


def conectado() -> bool:
    """Hay token Y al menos un chat que mando /start."""
    return configurado() and bool(_config().get('chat_ids'))


def estado() -> dict:
    cfg = _config()
    info = {
        'token_configurado': configurado(),
        'chats':             cfg.get('chat_ids', []),
        'conectado':         conectado(),
        'avisos':            cfg.get('avisos', {}),
        'webhook':           cfg.get('webhook'),
        'ultimo_envio':      cfg.get('ultimo_envio'),
        'pendientes_resumen': len(cfg.get('pendientes_resumen', [])),
    }
    if configurado():
        try:
            r = requests.get(f'{_API}{token()}/getMe', timeout=_TIMEOUT)
            if r.ok and r.json().get('ok'):
                info['bot'] = r.json()['result'].get('username')
            else:
                info['error'] = 'El token no fue aceptado por Telegram'
        except requests.RequestException as e:
            info['error'] = f'No se pudo hablar con Telegram: {e}'
    return info


def set_aviso(tipo: str, inmediato: bool) -> dict:
    cfg = _config()
    cfg['avisos'][tipo] = bool(inmediato)
    _guardar(cfg)
    return cfg['avisos']


# ── Envio ────────────────────────────────────────────────────────────────────

def _escape(txt: str) -> str:
    """HTML parse_mode: solo hay que escapar estos tres."""
    return (str(txt or '').replace('&', '&amp;')
            .replace('<', '&lt;').replace('>', '&gt;'))


def enviar(texto: str, botones: list[list[dict]] | None = None,
           chat_id: str | None = None) -> bool:
    """Manda un mensaje. Nunca lanza: un aviso que falla no tumba un cron."""
    if not configurado():
        _logger.info('[telegram] sin TELEGRAM_BOT_TOKEN — no se envia nada')
        return False

    cfg = _config()
    destinos = [chat_id] if chat_id else cfg.get('chat_ids', [])
    if not destinos:
        _logger.info('[telegram] nadie mando /start todavia — no hay a quien escribir')
        return False

    payload_base = {'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if botones:
        payload_base['reply_markup'] = {'inline_keyboard': botones}

    ok_alguno = False
    for cid in destinos:
        try:
            r = requests.post(f'{_API}{token()}/sendMessage',
                              json={**payload_base, 'chat_id': cid, 'text': texto},
                              timeout=_TIMEOUT)
            if r.ok:
                ok_alguno = True
            else:
                _logger.warning('[telegram] sendMessage %s: %s', r.status_code, r.text[:200])
        except requests.RequestException as e:
            _logger.warning('[telegram] error de red enviando: %s', e)

    if ok_alguno:
        cfg['ultimo_envio'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _guardar(cfg)
    return ok_alguno


def _responder_callback(callback_id: str, texto: str) -> None:
    """El toast que ve el usuario al tocar un boton."""
    if not configurado():
        return
    try:
        requests.post(f'{_API}{token()}/answerCallbackQuery',
                      json={'callback_query_id': callback_id, 'text': texto[:200]},
                      timeout=_TIMEOUT)
    except requests.RequestException:
        pass


def _editar(chat_id, message_id, texto: str) -> None:
    """Reemplaza el mensaje original para que quede el resultado, sin botones."""
    if not configurado():
        return
    try:
        requests.post(f'{_API}{token()}/editMessageText',
                      json={'chat_id': chat_id, 'message_id': message_id,
                            'text': texto, 'parse_mode': 'HTML'},
                      timeout=_TIMEOUT)
    except requests.RequestException:
        pass


# ── Webhook ──────────────────────────────────────────────────────────────────

def registrar_webhook(url_base: str, secreto: str) -> dict:
    """Le dice a Telegram a donde mandar los updates."""
    if not configurado():
        return {'ok': False, 'error': 'Falta TELEGRAM_BOT_TOKEN'}
    url = f"{url_base.rstrip('/')}/api/telegram/webhook/{secreto}"
    try:
        r = requests.post(f'{_API}{token()}/setWebhook',
                          json={'url': url,
                                'allowed_updates': ['message', 'callback_query']},
                          timeout=_TIMEOUT)
        data = r.json() if r.ok else {}
        if data.get('ok'):
            cfg = _config()
            cfg['webhook'] = {'url': url,
                              'registrado': datetime.now().strftime('%Y-%m-%d %H:%M')}
            _guardar(cfg)
            return {'ok': True, 'url': url}
        return {'ok': False, 'error': (data or {}).get('description') or r.text[:200]}
    except requests.RequestException as e:
        return {'ok': False, 'error': str(e)}


def _registrar_chat(chat_id) -> bool:
    cfg = _config()
    cid = str(chat_id)
    if cid not in cfg['chat_ids']:
        cfg['chat_ids'].append(cid)
        _guardar(cfg)
        return True
    return False


def procesar_update(update: dict) -> dict:
    """Punto de entrada del webhook. Devuelve un resumen para el log."""
    try:
        if 'callback_query' in update:
            return _procesar_callback(update['callback_query'])
        if 'message' in update:
            return _procesar_mensaje(update['message'])
    except Exception as e:
        _logger.error('[telegram] update fallo: %s', e)
        return {'ok': False, 'error': str(e)}
    return {'ok': True, 'ignorado': True}


def _procesar_mensaje(msg: dict) -> dict:
    chat_id = (msg.get('chat') or {}).get('id')
    texto   = (msg.get('text') or '').strip()
    if not chat_id:
        return {'ok': True, 'ignorado': True}

    comando = texto.split()[0].lower().split('@')[0] if texto else ''

    if comando == '/start':
        nuevo = _registrar_chat(chat_id)
        enviar(
            '<b>Sistema ML conectado.</b>\n\n'
            'Desde aca te voy a avisar lo que no puede esperar: buy box perdida, '
            'quiebre de stock con trafico, reclamos y preguntas sin responder. '
            'Y cada propuesta de precio con botones para aprobarla o rechazarla '
            'sin abrir el panel.\n\n'
            'Lo que aprobes aca queda aprobado en el sistema: es la misma bandeja.\n\n'
            'Comandos:\n'
            '/bandeja — lo que espera tu decision\n'
            '/estado — como viene cada cuenta\n'
            '/resumen — lo que se junto para hoy',
            chat_id=str(chat_id))
        return {'ok': True, 'accion': 'start', 'chat_nuevo': nuevo}

    if comando == '/bandeja':
        _enviar_bandeja(str(chat_id))
        return {'ok': True, 'accion': 'bandeja'}

    # Texto suelto: puede ser la respuesta que el usuario eligio escribir
    res_libre = _texto_libre_para_pregunta(msg)
    if res_libre:
        return res_libre

    if comando == '/estado':
        _enviar_estado(str(chat_id))
        return {'ok': True, 'accion': 'estado'}

    if comando == '/resumen':
        enviar_resumen(chat_id=str(chat_id), vaciar=False)
        return {'ok': True, 'accion': 'resumen'}

    return {'ok': True, 'ignorado': True}


def _procesar_callback(cb: dict) -> dict:
    """Aprobar / rechazar desde el celular. Escribe en la bandeja de Cerebro."""
    data       = cb.get('data') or ''
    cb_id      = cb.get('id')
    msg        = cb.get('message') or {}
    chat_id    = (msg.get('chat') or {}).get('id')
    message_id = msg.get('message_id')

    partes = data.split(':')

    # Preguntas de compradores: qp:<question_id>:<A|B|C|E|X>
    if len(partes) == 3 and partes[0] == 'qp':
        return _procesar_callback_pregunta(cb, partes[1], partes[2])

    if len(partes) < 4 or partes[0] != 'cb':
        _responder_callback(cb_id, 'No entendi ese boton')
        return {'ok': True, 'ignorado': True}

    _, accion_tipo, alias, accion_id = partes[0], partes[1], partes[2], partes[3]

    from modules import cerebro
    actual = cerebro.get_accion(alias, accion_id)
    if not actual:
        _responder_callback(cb_id, 'Esa propuesta ya no existe')
        _editar(chat_id, message_id, 'Esta propuesta ya no esta en la bandeja.')
        return {'ok': False, 'error': 'accion inexistente'}

    # Si ya se resolvio en el panel, se respeta lo que ya pasó: una sola bandeja
    if actual.get('estado') != cerebro.ESTADO_PENDIENTE:
        _responder_callback(cb_id, f'Ya estaba {actual.get("estado")}')
        _editar(chat_id, message_id,
                f'{_resumen_accion(actual)}\n\n'
                f'<i>Ya habia quedado {_escape(actual.get("estado"))} desde el panel.</i>')
        return {'ok': True, 'ya_resuelta': actual.get('estado')}

    if accion_tipo == 'ok':
        res = cerebro.aprobar_accion(alias, accion_id, por='telegram')
        _responder_callback(cb_id, 'Aprobada')
        _editar(chat_id, message_id,
                f'{_resumen_accion(res)}\n\n<b>Aprobada.</b> Se aplica en la '
                f'proxima corrida y queda registrada para evaluarla a 7 y 14 dias.')
        return {'ok': True, 'accion': 'aprobada', 'id': accion_id}

    if accion_tipo == 'no':
        res = cerebro.rechazar_accion(alias, accion_id, por='telegram',
                                      motivo='rechazada desde Telegram')
        _responder_callback(cb_id, 'Rechazada')
        _editar(chat_id, message_id,
                f'{_resumen_accion(res)}\n\n<b>Rechazada.</b> No se aplica.')
        return {'ok': True, 'accion': 'rechazada', 'id': accion_id}

    _responder_callback(cb_id, 'No entendi ese boton')
    return {'ok': True, 'ignorado': True}


# ── Mensajes ─────────────────────────────────────────────────────────────────

def _plata(v) -> str:
    try:
        return f'${float(v):,.0f}'.replace(',', '.')
    except (TypeError, ValueError):
        return '—'


def _resumen_accion(acc: dict) -> str:
    det = acc.get('detalle') or {}
    if acc.get('tipo') == 'precio':
        antes, despues = det.get('precio_antes'), det.get('precio_despues')
        delta = ''
        try:
            delta = f" ({(float(despues)/float(antes)-1)*100:+.1f}%)"
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return (f"<b>{_escape(det.get('titulo') or acc.get('item_id'))}</b>\n"
                f"{_plata(antes)} → {_plata(despues)}{delta}")
    return f"<b>{_escape(det.get('titulo') or acc.get('item_id'))}</b> · {_escape(acc.get('tipo'))}"


def notificar_propuesta(alias: str, accion: dict) -> bool:
    """Una propuesta que espera tu OK, con los botones para resolverla."""
    if not _config()['avisos'].get('propuesta_precio', True):
        return guardar_para_resumen('propuesta_precio',
                                    _resumen_accion(accion), alias)

    det = accion.get('detalle') or {}
    previo = accion.get('estado_previo') or {}
    lineas = [f'💰 <b>Propuesta de precio</b> · {_escape(alias)}', '',
              _resumen_accion(accion)]
    if accion.get('hipotesis'):
        lineas += ['', f'<i>{_escape(accion["hipotesis"])}</i>']

    contexto = []
    if det.get('competidor_precio'):
        contexto.append(f'competidor {_plata(det["competidor_precio"])}')
    if previo.get('visitas_7d'):
        contexto.append(f'{previo["visitas_7d"]:.0f} visitas 7d')
    if previo.get('conversion_7d'):
        contexto.append(f'conv {previo["conversion_7d"]:.1f}%')
    if det.get('margen_nuevo_pct'):
        contexto.append(f'margen queda en {det["margen_nuevo_pct"]:.0f}%')
    if contexto:
        lineas += ['', '· ' + ' · '.join(contexto)]

    botones = [[
        {'text': 'Aprobar',  'callback_data': f'cb:ok:{alias}:{accion.get("id")}'},
        {'text': 'Rechazar', 'callback_data': f'cb:no:{alias}:{accion.get("id")}'},
    ]]
    return enviar('\n'.join(lineas), botones=botones)


def notificar(tipo: str, titulo: str, detalle: str = '', alias: str = '',
              url: str = '') -> bool:
    """Aviso generico. Va al telefono o al resumen segun la configuracion."""
    if not _config()['avisos'].get(tipo, True):
        return guardar_para_resumen(tipo, f'{titulo} — {detalle}'.strip(' —'), alias)

    icono = {'buybox_perdida': '📉', 'stock_critico': '📦', 'reclamo': '⚠️',
             'preguntas': '💬', 'competidor': '👀', 'veredicto': '🧠',
             'top_acciones': '📋'}.get(tipo, '🔔')
    txt = f'{icono} <b>{_escape(titulo)}</b>'
    if alias:
        txt += f' · {_escape(alias)}'
    if detalle:
        txt += f'\n{_escape(detalle)}'
    if url:
        txt += f'\n{url}'
    return enviar(txt)


def guardar_para_resumen(tipo: str, texto: str, alias: str = '') -> bool:
    """Lo que no interrumpe se junta y sale una vez por dia."""
    cfg = _config()
    cfg['pendientes_resumen'].append({
        'tipo': tipo, 'texto': texto, 'alias': alias,
        'ts': datetime.now().strftime('%Y-%m-%d %H:%M')})
    cfg['pendientes_resumen'] = cfg['pendientes_resumen'][-200:]
    _guardar(cfg)
    return True


# ── Preguntas de compradores, respondibles desde el celular ──────────────────
# Las preguntas entran a cualquier hora y responder rapido mueve la conversion,
# pero casi nunca uno esta frente a la computadora cuando llegan. Aca llega la
# pregunta con tres respuestas ya escritas y se contesta con un toque, o se
# escribe una propia respondiendo al mensaje.
#
# El estado vive en data/telegram_preguntas.json, no en el chat: si el bot se
# reinicia entre que manda la pregunta y el usuario toca el boton, la respuesta
# igual se envia.

PREGUNTAS_PATH = os.path.join(DATA_DIR, 'telegram_preguntas.json')

# Cuanto se guarda una pregunta pendiente antes de considerarla vencida
VENCIMIENTO_HORAS = 48


def _preguntas() -> dict:
    d = db_load(PREGUNTAS_PATH) or {}
    return d if isinstance(d, dict) else {}


def _guardar_preguntas(d: dict) -> None:
    try:
        db_save(PREGUNTAS_PATH, d)
    except Exception as e:
        _logger.error('[telegram] no pude guardar preguntas: %s', e)


def _purgar_vencidas(d: dict) -> dict:
    corte = datetime.now() - timedelta(hours=VENCIMIENTO_HORAS)
    return {k: v for k, v in d.items()
            if (v.get('ts') or '') >= corte.strftime('%Y-%m-%d %H:%M:%S')
            or v.get('estado') == 'pendiente'}


def notificar_pregunta(alias: str, question_id, texto_pregunta: str,
                       item_id: str = '', item_titulo: str = '',
                       opciones: list[dict] | None = None) -> bool:
    """Manda una pregunta al celular con sus tres respuestas y los botones."""
    if not conectado():
        return False
    qid = str(question_id)

    data = _purgar_vencidas(_preguntas())
    if qid in data and data[qid].get('estado') != 'pendiente':
        return False   # ya se respondio
    if qid in data and data[qid].get('avisada'):
        return False   # ya se aviso, no repetir

    opciones = opciones or []
    lineas = ['💬 <b>Pregunta nueva</b>' + (f' · {_escape(alias)}' if alias else '')]
    if item_titulo:
        lineas.append(f'<i>{_escape(item_titulo[:70])}</i>')
    lineas += ['', f'<b>"{_escape(texto_pregunta[:400])}"</b>']

    if opciones:
        lineas.append('')
        for o in opciones:
            lineas.append(f'<b>{o["key"]}) {_escape(o["label"])}</b>')
            lineas.append(_escape(o['text'][:400]))
            lineas.append('')
        lineas.append('<i>Tocá una para enviarla, o respondé a este mensaje '
                      'con tu propio texto.</i>')
    else:
        lineas.append('')
        lineas.append('<i>No pude generar sugerencias. Respondé a este mensaje '
                      'con tu texto y la envio.</i>')

    botones = []
    if opciones:
        botones.append([{'text': o['key'], 'callback_data': f'qp:{qid}:{o["key"]}'}
                        for o in opciones])
    botones.append([{'text': 'Escribir otra', 'callback_data': f'qp:{qid}:E'},
                    {'text': 'Después', 'callback_data': f'qp:{qid}:X'}])

    ok = enviar('\n'.join(lineas), botones=botones)
    if ok:
        data[qid] = {
            'alias': alias, 'item_id': item_id, 'item_titulo': item_titulo,
            'pregunta': texto_pregunta, 'opciones': opciones,
            'estado': 'pendiente', 'avisada': True,
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        _guardar_preguntas(data)
    return ok


def _token_de(alias: str) -> str | None:
    try:
        from core.account_manager import AccountManager
        client = AccountManager().get_client(alias)
        client._ensure_token()
        return client.account.access_token
    except Exception as e:
        _logger.error('[telegram] no pude obtener el token de %s: %s', alias, e)
        return None


def _enviar_respuesta_a_ml(qid: str, texto: str) -> dict:
    """Publica la respuesta en ML y la registra en Cerebro."""
    from modules import respuestas_ia

    data = _preguntas()
    entry = data.get(qid)
    if not entry:
        return {'ok': False, 'error': 'esa pregunta ya no esta en la cola'}
    if entry.get('estado') == 'respondida':
        return {'ok': False, 'error': 'ya respondida', 'ya': True}

    alias = entry.get('alias', '')
    token = _token_de(alias)
    if not token:
        return {'ok': False, 'error': 'no pude autenticar la cuenta'}

    res = respuestas_ia.responder_en_ml(qid, texto, token)
    if not res.get('ok'):
        return res

    entry['estado'] = 'respondida'
    entry['respuesta'] = texto
    entry['respondida_ts'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data[qid] = entry
    _guardar_preguntas(data)

    # Cerebro 1.1 — responder rapido deberia mover la conversion; queda medido
    try:
        from modules import cerebro
        if entry.get('item_id'):
            cerebro.registrar_accion(
                alias, tipo='respuesta', item_id=entry['item_id'],
                origen=cerebro.ORIGEN_USUARIO,
                hipotesis='responder la pregunta destraba la compra y mejora la '
                          'conversion de la publicacion',
                estado_previo=cerebro.capturar_estado_previo(alias, entry['item_id']),
                detalle={'question_id': qid, 'largo': len(texto), 'via': 'telegram'},
                ejecutado_por='telegram')
    except Exception as e:
        _logger.warning('[telegram] no pude registrar la respuesta en cerebro: %s', e)

    return {'ok': True}


def _procesar_callback_pregunta(cb: dict, qid: str, opcion: str) -> dict:
    cb_id      = cb.get('id')
    msg        = cb.get('message') or {}
    chat_id    = (msg.get('chat') or {}).get('id')
    message_id = msg.get('message_id')

    data = _preguntas()
    entry = data.get(qid)
    if not entry:
        _responder_callback(cb_id, 'Esa pregunta ya no esta')
        return {'ok': False}

    if opcion == 'X':
        _responder_callback(cb_id, 'Queda pendiente')
        _editar(chat_id, message_id,
                f'💬 <b>Pregunta postergada</b>\n<i>{_escape(entry.get("pregunta", "")[:200])}</i>\n\n'
                f'Sigue sin responder. La vas a ver de nuevo en /bandeja.')
        return {'ok': True, 'accion': 'postergada'}

    if opcion == 'E':
        _responder_callback(cb_id, 'Escribi tu respuesta')
        enviar(f'✏️ Respondé a <b>este</b> mensaje con el texto que querés enviar '
               f'para:\n<i>"{_escape(entry.get("pregunta", "")[:200])}"</i>',
               chat_id=str(chat_id) if chat_id else None)
        # El proximo mensaje que responda a este se toma como la respuesta
        entry['esperando_texto'] = True
        data[qid] = entry
        _guardar_preguntas(data)
        return {'ok': True, 'accion': 'esperando_texto'}

    texto = next((o['text'] for o in (entry.get('opciones') or [])
                  if o.get('key') == opcion), '')
    if not texto:
        _responder_callback(cb_id, 'No encontre esa opcion')
        return {'ok': False}

    res = _enviar_respuesta_a_ml(qid, texto)
    if res.get('ok'):
        _responder_callback(cb_id, 'Respuesta enviada')
        _editar(chat_id, message_id,
                f'✅ <b>Respondida</b>\n<i>"{_escape(entry.get("pregunta", "")[:150])}"</i>\n\n'
                f'{_escape(texto[:400])}')
        return {'ok': True, 'accion': 'respondida', 'qid': qid}

    _responder_callback(cb_id, res.get('error', 'No se pudo enviar')[:180])
    return res


def _texto_libre_para_pregunta(msg: dict) -> dict | None:
    """Si el usuario respondio a un pedido de texto, se envia eso a ML."""
    texto = (msg.get('text') or '').strip()
    if not texto or texto.startswith('/'):
        return None
    data = _preguntas()
    esperando = [q for q, v in data.items()
                 if v.get('esperando_texto') and v.get('estado') == 'pendiente']
    if not esperando:
        return None
    # La mas reciente de las que esperan texto
    qid = sorted(esperando, key=lambda q: data[q].get('ts', ''))[-1]
    res = _enviar_respuesta_a_ml(qid, texto)
    chat_id = str((msg.get('chat') or {}).get('id'))
    if res.get('ok'):
        entry = _preguntas().get(qid, {})
        enviar(f'✅ <b>Respuesta enviada</b>\n'
               f'<i>"{_escape(entry.get("pregunta", "")[:150])}"</i>\n\n{_escape(texto[:400])}',
               chat_id=chat_id)
    else:
        enviar(f'No se pudo enviar: {_escape(res.get("error", ""))[:200]}', chat_id=chat_id)
    return {'ok': res.get('ok'), 'accion': 'respondida_texto_libre', 'qid': qid}


def preguntas_pendientes() -> list[dict]:
    """Las que siguen sin responder, para /bandeja."""
    out = []
    for qid, v in _preguntas().items():
        if v.get('estado') == 'pendiente':
            out.append({**v, 'question_id': qid})
    out.sort(key=lambda x: x.get('ts', ''))
    return out


def _fecha_larga() -> str:
    dias = ['lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo']
    ahora = datetime.now()
    return f'{dias[ahora.weekday()]} {ahora.day}/{ahora.month}'


def _plata_corta(v) -> str:
    """$1.2M, $362k, $6.700 — para que se lea de un vistazo en el celular."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return '—'
    if n >= 1_000_000:
        return f'${n / 1_000_000:.1f}M'.replace('.0M', 'M')
    if n >= 10_000:
        return f'${n / 1000:.0f}k'
    return f'${n:,.0f}'.replace(',', '.')


def _bloque_oportunidades(alias: str) -> list[str]:
    """El Top 3 con lo que hay que saber para decidir: plata, motivo y que hacer."""
    try:
        from modules import top_acciones_diarias as ta
        data = ta.top3(alias)
    except Exception as e:
        _logger.warning('[telegram] no pude leer el top 3: %s', e)
        return []

    top = (data or {}).get('top_3') or []
    if not top:
        return []

    lineas = ['', '💰 <b>Lo que mas plata deja</b>']
    for i, op in enumerate(top[:3], 1):
        snap = op.get('snapshot') or {}
        desc = (op.get('descripcion') or '')

        # Titulo corto: la accion y el producto, sin la parrafada
        titulo = desc.split('—')[-1].strip() if '—' in desc else desc
        accion = 'Pausar duplicado' if op.get('tipo') == 'pausar_duplicados' else (
                 'Revisar precio' if op.get('tipo') == 'repricing_precio' else
                 (op.get('tipo') or '').replace('_', ' ').capitalize())

        lineas.append(f'\n<b>{i}. {_escape(accion)}</b> · '
                      f'{_plata_corta(op.get("impacto_mensual_ars"))}/mes')
        lineas.append(f'{_escape(titulo[:70])}')

        # El POR QUE, en una linea
        porque = []
        if snap.get('visitas_perdidas_30d'):
            porque.append(f'{snap["visitas_perdidas_30d"]} visitas repartidas entre '
                          f'{snap.get("items_count", 2)} publicaciones tuyas')
        if snap.get('conversion_pct') is not None and snap.get('avg_conv_pct'):
            porque.append(f'conversion {snap["conversion_pct"]:.1f}% contra '
                          f'{snap["avg_conv_pct"]:.1f}% de tu catalogo')
        if porque:
            lineas.append(f'<i>{_escape(" · ".join(porque))}</i>')

        # Margen antes y despues, que es lo que el usuario necesita ver
        if snap.get('margen_actual_pct') and snap.get('margen_nuevo_pct'):
            lineas.append(f'margen {snap["margen_actual_pct"]}% → {snap["margen_nuevo_pct"]}%')

        # La advertencia que cambia la decision
        if snap.get('compensacion'):
            lineas.append(f'⚠️ {_escape(snap["compensacion"])}')
        alt = snap.get('alternativa_cuotas')
        if alt and alt.get('resumen'):
            lineas.append(f'💡 {_escape(alt["resumen"][:150])}')

        if op.get('cta_url'):
            lineas.append(f'https://ml-system-rr81.onrender.com{op["cta_url"]}')
    return lineas


def _bloque_cerebro(alias: str) -> list[str]:
    """Estado de Cerebro en castellano, no en contadores."""
    try:
        from modules import cerebro
        r = cerebro.resumen(alias)
    except Exception as e:
        _logger.warning('[telegram] no pude leer cerebro: %s', e)
        return []

    dias = r.get('dias_serie_max', 0)
    lineas = ['', '🧠 <b>Cerebro</b>']

    if dias < 14:
        faltan = 14 - dias
        primer = (datetime.now() + timedelta(days=faltan)).strftime('%d/%m')
        lineas.append(f'Juntando datos: dia {dias} de 14. '
                      f'El primer veredicto sale alrededor del {primer}.')
    if r.get('items_con_serie'):
        lineas.append(f'{r["items_con_serie"]} publicaciones con seguimiento diario.')

    acciones = r.get('acciones_total', 0)
    if acciones:
        evaluadas = (r.get('acciones_por_estado', {}).get('evaluada_7d', 0)
                     + r.get('acciones_por_estado', {}).get('evaluada_14d', 0))
        lineas.append(f'{acciones} cambios registrados, {evaluadas} ya evaluados.')
    else:
        lineas.append('Todavia no registro ningun cambio tuyo: '
                      'aplica una optimizacion o un precio y empieza a medir.')

    aprendizajes = r.get('aprendizajes', 0)
    if aprendizajes:
        lineas.append(f'{aprendizajes} aprendizajes '
                      f'({r.get("aprendizajes_confiables", 0)} con confianza suficiente).')
    return lineas


def _bloque_novedades(pendientes: list[dict]) -> list[str]:
    """Lo que se junto durante el dia y no interrumpio."""
    if not pendientes:
        return []
    # El Top 3 ya se muestra completo arriba: no repetirlo
    utiles = [p for p in pendientes if p.get('tipo') != 'top_acciones']
    if not utiles:
        return []

    nombres = {'competidor': '👀 Competidores', 'veredicto': '🧠 Lo que aprendio',
               'buybox_perdida': '📉 Buy box', 'stock_critico': '📦 Stock',
               'reclamo': '⚠️ Reclamos', 'preguntas': '💬 Preguntas',
               'propuesta_precio': '💰 Precios propuestos'}
    por_tipo: dict[str, list] = {}
    for p in utiles:
        por_tipo.setdefault(p['tipo'], []).append(p)

    lineas = ['', '<b>Novedades del dia</b>']
    for tipo, items in por_tipo.items():
        lineas.append(f'\n{nombres.get(tipo, tipo)}')
        for it in items[:5]:
            lineas.append(f'· {_escape(it["texto"])[:160]}')
        if len(items) > 5:
            lineas.append(f'· y {len(items) - 5} mas')
    return lineas


def enviar_resumen(chat_id: str | None = None, vaciar: bool = True) -> bool:
    """Resumen diario, escrito para decidir en el celular.

    El resumen anterior juntaba lineas sueltas sin decir cuanta plata, por que
    ni que hacer, asi que se leia como una lista ambigua. Este arranca por lo
    que hay que decidir hoy, sigue por lo que mas plata deja —con el motivo, el
    margen antes y despues y la advertencia que cambia la decision— y cierra con
    el estado de Cerebro en castellano en vez de contadores.
    """
    cfg = _config()
    pend = cfg.get('pendientes_resumen', [])
    aliases = _aliases()
    alias = aliases[0] if aliases else ''

    lineas = [f'📋 <b>{_escape(alias) or "Sistema ML"} — {_fecha_larga()}</b>']

    # 1. Lo primero: ¿hay algo que decidir hoy?
    total_bandeja = _total_bandeja()
    if total_bandeja:
        lineas.append(f'\n⚠️ <b>{total_bandeja} '
                      f'{"cosa" if total_bandeja == 1 else "cosas"} esperando tu decision</b> — /bandeja')
    else:
        lineas.append('\nNada esperando tu decision.')

    # 2. Donde esta la plata, con el porque
    for a in aliases[:2]:
        bloque = _bloque_oportunidades(a)
        if bloque and len(aliases) > 1:
            bloque.insert(1, f'<i>{_escape(a)}</i>')
        lineas += bloque

    # 3. Lo que se junto durante el dia
    lineas += _bloque_novedades(pend)

    # 4. Como viene aprendiendo
    for a in aliases[:2]:
        lineas += _bloque_cerebro(a)

    lineas += ['', '<i>/bandeja para decidir · /estado para el detalle</i>']

    ok = enviar('\n'.join(lineas), chat_id=chat_id)
    if ok and vaciar:
        cfg = _config()
        cfg['pendientes_resumen'] = []
        _guardar(cfg)
    return ok

def _aliases() -> list[str]:
    try:
        from core.account_manager import AccountManager
        return [a.alias for a in AccountManager().list_accounts() if a.active]
    except Exception as e:
        _logger.warning('[telegram] no pude listar cuentas: %s', e)
        return []


def _total_bandeja() -> int:
    from modules import cerebro
    total = 0
    for alias in _aliases():
        try:
            total += len(cerebro.acciones_pendientes(alias))
            total += len(cerebro.candidatos_pendientes(alias))
            total += sum(1 for q in preguntas_pendientes() if q.get('alias') == alias)
        except Exception:
            continue
    return total


def _enviar_bandeja(chat_id: str) -> None:
    """Manda cada propuesta pendiente con sus botones."""
    from modules import cerebro
    enviado = 0
    for alias in _aliases():
        for acc in cerebro.acciones_pendientes(alias)[:10]:
            botones = [[
                {'text': 'Aprobar',  'callback_data': f'cb:ok:{alias}:{acc.get("id")}'},
                {'text': 'Rechazar', 'callback_data': f'cb:no:{alias}:{acc.get("id")}'},
            ]]
            enviar(f'{_resumen_accion(acc)}\n<i>{_escape(acc.get("hipotesis", ""))}</i>',
                   botones=botones, chat_id=chat_id)
            enviado += 1
        for q in preguntas_pendientes():
            if q.get('alias') != alias:
                continue
            botones = [[{'text': o['key'], 'callback_data': f'qp:{q["question_id"]}:{o["key"]}'}
                        for o in (q.get('opciones') or [])]]
            botones.append([{'text': 'Escribir otra',
                             'callback_data': f'qp:{q["question_id"]}:E'}])
            enviar(f'💬 <b>Sin responder</b> · {_escape(q.get("item_titulo", "")[:60])}\n'
                   f'<i>"{_escape(q.get("pregunta", "")[:200])}"</i>',
                   botones=botones, chat_id=chat_id)
            enviado += 1

        candidatos = cerebro.candidatos_pendientes(alias)
        if candidatos:
            enviar(f'👀 <b>{len(candidatos)} competidores por confirmar</b> · {_escape(alias)}\n'
                   f'Se confirman desde el panel, en la publicacion.', chat_id=chat_id)
            enviado += 1
    if not enviado:
        enviar('Bandeja vacia: no hay nada esperando tu decision.', chat_id=chat_id)


def _enviar_estado(chat_id: str) -> None:
    from modules import cerebro
    lineas = ['🧠 <b>Estado</b>']
    for alias in _aliases():
        try:
            r = cerebro.resumen(alias)
            lineas.append(
                f'\n<b>{_escape(alias)}</b>\n'
                f'· {r["acciones_total"]} acciones registradas, {r["pendientes"]} pendientes\n'
                f'· {r["aprendizajes"]} aprendizajes ({r["aprendizajes_confiables"]} con confianza)\n'
                f'· {r["items_con_serie"]} publicaciones con serie, {r["dias_serie_max"]} dias\n'
                f'· {r["competidores"]["total"]} competidores, '
                f'{r["competidores"]["pendientes"]} por confirmar')
        except Exception as e:
            lineas.append(f'\n<b>{_escape(alias)}</b>: no pude leerlo ({e})')
    enviar('\n'.join(lineas), chat_id=chat_id)


def probar() -> dict:
    """Mensaje de prueba desde la pantalla de ajustes."""
    if not configurado():
        return {'ok': False, 'error': 'Falta TELEGRAM_BOT_TOKEN en las variables de entorno'}
    if not _config().get('chat_ids'):
        return {'ok': False, 'error': 'Todavia nadie le mando /start al bot'}
    ok = enviar('✅ <b>Prueba</b>\nEl Sistema ML puede escribirte. Todo listo.')
    return {'ok': ok} if ok else {'ok': False, 'error': 'Telegram rechazo el envio'}
