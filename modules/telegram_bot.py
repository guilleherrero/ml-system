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
from datetime import datetime

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


def enviar_resumen(chat_id: str | None = None, vaciar: bool = True) -> bool:
    """Resumen diario: lo que se junto + la bandeja pendiente."""
    cfg = _config()
    pend = cfg.get('pendientes_resumen', [])

    lineas = [f'📋 <b>Resumen — {datetime.now().strftime("%d/%m")}</b>']

    if pend:
        por_tipo: dict[str, list] = {}
        for p in pend:
            por_tipo.setdefault(p['tipo'], []).append(p)
        nombres = {'competidor': 'Competidores', 'veredicto': 'Cerebro aprendio',
                   'top_acciones': 'Oportunidades', 'buybox_perdida': 'Buy box',
                   'stock_critico': 'Stock', 'reclamo': 'Reclamos',
                   'preguntas': 'Preguntas', 'propuesta_precio': 'Precios propuestos'}
        for tipo, items in por_tipo.items():
            lineas += ['', f'<b>{nombres.get(tipo, tipo)}</b>']
            for it in items[:8]:
                lineas.append(f'· {_escape(it["texto"])[:180]}')
            if len(items) > 8:
                lineas.append(f'· y {len(items) - 8} mas')
    else:
        lineas += ['', 'Sin novedades para juntar.']

    total_bandeja = _total_bandeja()
    if total_bandeja:
        lineas += ['', f'<b>{total_bandeja} esperando tu decision</b> — /bandeja']

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
