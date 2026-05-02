"""
Interfaz web del Sistema ML.
Corré con: python3 web/app.py
Abrí en el navegador: http://localhost:8080
"""

# Timezone Argentina — debe setearse ANTES de los imports de datetime/time
# para que datetime.now() y time.localtime() devuelvan hora local ART.
# Render corre por defecto en UTC; sin esto las pantallas muestran +3h.
import os as _tz_os
_tz_os.environ['TZ'] = 'America/Argentina/Buenos_Aires'
import time as _tz_time
try:
    _tz_time.tzset()  # Unix/Mac/Linux — aplica el cambio al proceso actual
except AttributeError:
    pass  # Windows no tiene tzset (no usado en producción)

import json
import logging
import os
import re
import sys
import glob
import base64
import subprocess
import time as _time_module
from datetime import datetime, timedelta, date

import requests as req_lib
import anthropic
from flask import Flask, render_template, redirect, url_for, jsonify, request, Response, stream_with_context, make_response, session

# Limpiar el API key de caracteres invisibles (newline, espacios) que rompen httpcore
_raw_key = os.environ.get('ANTHROPIC_API_KEY', '')
if _raw_key != _raw_key.strip():
    os.environ['ANTHROPIC_API_KEY'] = _raw_key.strip()

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.fees import get_fee_rates, get_rate
from core.db_storage import db_load, db_save
from core.auth import (
    needs_setup, get_current_user, login_user, logout_user,
    create_user, update_user, delete_user, list_users,
    get_permitted_accounts,
)
from werkzeug.security import check_password_hash

app = Flask(__name__)
_flask_secret = os.environ.get('FLASK_SECRET', '')
if not _flask_secret:
    import warnings
    warnings.warn(
        'FLASK_SECRET no configurada — se usa clave de desarrollo. '
        'Definí la variable de entorno FLASK_SECRET antes de exponer el sistema en red.',
        stacklevel=2,
    )
    _flask_secret = 'ml-sistema-local-secret-2026'
app.secret_key = _flask_secret
app.config['TEMPLATES_AUTO_RELOAD']        = True
app.config['PERMANENT_SESSION_LIFETIME']   = timedelta(days=7)
app.config['SESSION_COOKIE_HTTPONLY']      = True
app.config['SESSION_COOKIE_SAMESITE']      = 'Lax'
app.jinja_env.filters['enumerate'] = enumerate
app.jinja_env.globals['now'] = datetime.now

# ── Logging ───────────────────────────────────────────────────────────────────
_data_dir_log = os.path.join(os.path.dirname(__file__), '..', 'data')
os.makedirs(_data_dir_log, exist_ok=True)
_log_handler = logging.FileHandler(
    os.path.join(_data_dir_log, 'app.log'),
    encoding='utf-8',
)
_log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
app.logger.addHandler(_log_handler)
app.logger.setLevel(logging.INFO)

# ── Audit trail ───────────────────────────────────────────────────────────────
_audit_logger = logging.getLogger('ml.audit')
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
_audit_handler = logging.FileHandler(
    os.path.join(_data_dir_log, 'audit.log'),
    encoding='utf-8',
)
_audit_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
_audit_logger.addHandler(_audit_handler)


def _audit(action: str, **kwargs):
    """Registra una acción crítica en audit.log con usuario y contexto."""
    user    = session.get('username', 'sistema')
    details = ' '.join(f'{k}={v!r}' for k, v in kwargs.items())
    _audit_logger.info('[%s] %s %s', user, action, details)

DATA_DIR        = os.path.join(os.path.dirname(__file__), '..', 'data')
CONFIG_DIR      = os.path.join(os.path.dirname(__file__), '..', 'config')
ROOT_DIR        = os.path.dirname(os.path.dirname(__file__))
TOKEN_LOG_PATH  = os.path.join(DATA_DIR, 'token_log.json')


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path):
    return db_load(path)

def save_json(path, data):
    db_save(path, data)


# ── Token cost logger ─────────────────────────────────────────────────────────

_TOKEN_PRICES = {
    'claude-opus-4-6':            {'input': 15.0,  'output': 75.0},
    'claude-sonnet-4-6':          {'input':  3.0,  'output': 15.0},
    'claude-haiku-4-5-20251001':  {'input':  0.25, 'output':  1.25},
}

def _log_token_usage(funcion: str, modelo: str, input_tokens: int, output_tokens: int):
    """Registra uso y costo de tokens de Claude en token_log.json."""
    rates    = _TOKEN_PRICES.get(modelo, {'input': 3.0, 'output': 15.0})
    costo    = (input_tokens * rates['input'] + output_tokens * rates['output']) / 1_000_000
    entry    = {
        'ts':      datetime.now().isoformat(timespec='seconds'),
        'funcion': funcion,
        'modelo':  modelo,
        'in':      input_tokens,
        'out':     output_tokens,
        'usd':     round(costo, 6),
    }
    try:
        log = load_json(TOKEN_LOG_PATH) or {'entries': []}
        log['entries'].append(entry)
        if len(log['entries']) > 1000:
            log['entries'] = log['entries'][-1000:]
        save_json(TOKEN_LOG_PATH, log)
    except Exception as e:
        app.logger.error('_log_token_usage failed: %s', e)

def safe(alias):
    return alias.replace(' ', '_').replace('/', '-')


def _resolve_alias(alias: str) -> str:
    """Devuelve el alias con el casing exacto guardado (case-insensitive lookup).
    Lanza ValueError si no existe."""
    accounts = (load_json(os.path.join(CONFIG_DIR, 'accounts.json')) or {}).get('accounts', [])
    for a in accounts:
        if a.get('alias', '').lower() == alias.lower():
            return a['alias']
    raise ValueError(f'Alias desconocido: {alias!r}')


def _assert_valid_alias(alias: str):
    """Lanza ValueError si el alias no existe en accounts.json."""
    _resolve_alias(alias)


def _ml_auth(alias: str):
    """Devuelve (token, user_id, heads) con token refrescado automáticamente."""
    alias = _resolve_alias(alias)
    from core.account_manager import AccountManager as _AM
    mgr = _AM()
    client = mgr.get_client(alias)
    client._ensure_token()
    token   = client.account.access_token
    user_id = str(client.account.user_id or '')
    if not user_id:
        user_id = str(client.get_me().get('id', ''))
    heads = {'Authorization': f'Bearer {token}'}
    return token, user_id, heads


def _build_opt_history_block(alias: str) -> str:
    """Lee el historial de optimizaciones con resultados reales y construye un bloque
    de contexto para el prompt del optimizador. Incluye tanto seguimiento manual
    (aplicado con baseline) como resultado_auto (calculado desde posiciones)."""
    opt_data = load_json(os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')) or {}
    entries  = opt_data.get('optimizaciones', [])

    resultados = []
    for opt in entries:
        t_viejo = (opt.get('titulo_actual') or '')[:60]
        t_nuevo = (opt.get('titulo_nuevo')  or '')[:60]
        if not t_viejo or not t_nuevo or t_viejo == t_nuevo:
            continue

        # Preferir seguimiento manual (más preciso) sobre auto
        seg  = opt.get('seguimiento') or {}
        rauto = opt.get('resultado_auto') or {}
        veredicto = seg.get('veredicto') or rauto.get('veredicto')
        delta_p   = seg.get('delta_pos')  # negativo = mejoró en seguimiento manual
        if delta_p is None and rauto.get('delta') is not None:
            # resultado_auto: positivo = mejoró
            delta_p = -rauto['delta']

        if veredicto not in ('mejoro', 'empeoro', 'sin_cambio'):
            continue

        fecha = (opt.get('baseline', {}) or {}).get('fecha_aplicacion', opt.get('fecha', ''))[:10]

        # Describir el cambio: palabras agregadas / quitadas
        words_old = set(t_viejo.lower().split())
        words_new = set(t_nuevo.lower().split())
        added   = [w for w in words_new - words_old if len(w) > 2][:5]
        removed = [w for w in words_old - words_new if len(w) > 2][:3]
        cambio_parts = []
        if added:   cambio_parts.append(f'Agregó: {", ".join(added)}')
        if removed: cambio_parts.append(f'Quitó: {", ".join(removed)}')
        cambio_txt = ' | '.join(cambio_parts) or 'Reformulación'

        if veredicto == 'mejoro':
            pos_txt = f' ({abs(int(delta_p))} posiciones arriba)' if delta_p is not None and delta_p < 0 else ''
            resultado_txt = f'✅ MEJORÓ{pos_txt}'
        elif veredicto == 'empeoro':
            pos_txt = f' ({abs(int(delta_p))} posiciones abajo)' if delta_p is not None and delta_p > 0 else ''
            resultado_txt = f'❌ EMPEORÓ{pos_txt}'
        else:
            resultado_txt = '➡️ SIN CAMBIO significativo'

        resultados.append(
            f'  [{fecha}] "{t_viejo}"\n'
            f'         → "{t_nuevo}"\n'
            f'           Cambio: {cambio_txt}\n'
            f'           Resultado: {resultado_txt}'
        )

    if not resultados:
        return ''

    lines = [
        '═══════════════════════════════════════════════════',
        'HISTORIAL DE OPTIMIZACIONES ANTERIORES EN ESTA CUENTA (resultados reales medidos):',
        '═══════════════════════════════════════════════════',
    ]
    lines.extend(resultados[:8])
    lines += [
        '',
        'INSTRUCCIÓN CRÍTICA BASADA EN HISTORIAL:',
        '· Identificá qué tipo de cambios (agregar especificaciones, keywords long-tail,',
        '  marca, atributos técnicos) correlacionaron con mejoras en esta cuenta.',
        '· Aplicá explícitamente esos patrones ganadores a la optimización actual.',
        '· Si un patrón empeoró → evitalo o justificá por qué sería diferente ahora.',
        '· Al proponer títulos, citá qué patrón del historial estás aplicando.',
        '═══════════════════════════════════════════════════',
    ]
    return '\n'.join(lines)


# ── Keyword discovery expandido con vocabulario de competidores ───────────────

_PROMO_WORDS = {
    'oferta', 'descuento', 'promo', 'promocion', 'gratis', 'envio',
    'cuotas', 'nuevo', 'original', 'garantia', 'mejor', 'barato',
    'economico', 'precio', 'venta', 'pack', 'kit', 'combo', 'super',
    'mega', 'ultra', 'premium', 'calidad',
}

_KW_STOPWORDS = {
    'de', 'para', 'con', 'sin', 'y', 'el', 'la', 'los', 'las', 'un', 'una',
    'en', 'a', 'por', 'al', 'del', 'se', 'su', 'es', 'que', 'o', 'e',
    'x', 'i', 'ii', 'iii',
} | _PROMO_WORDS


def _norm_kw(text: str) -> str:
    """Normaliza texto: minúsculas + quita tildes."""
    import unicodedata
    return unicodedata.normalize('NFKD', text.lower()).encode('ascii', 'ignore').decode()


_COMPLEX_CATS = {
    "electronica", "tecnologia", "celular", "notebook", "computadora",
    "tablet", "tv", "monitor", "audio", "gaming", "mueble", "colchon",
    "sofa", "auto", "moto", "repuesto", "autoparte", "herramienta",
    "construccion", "salud", "medico", "ortopedico", "electrodomestico",
}

_SIMPLE_CATS = {
    "accesorio", "bijouterie", "papeleria", "bazar", "libreria",
    "decoracion",
}


def _extract_competitor_phrases(competitor_titles: list, seed_title: str) -> list:
    """
    Extrae bigramas y trigramas de títulos de competidores.
    · Filtra stopwords y palabras promocionales
    · Descarta frases literalmente presentes en el seed (ya cubiertas)
    · NO filtra frases únicas: son los perfiles de búsqueda alternativos que buscamos
      (maquinita / tijera / clipper para el mismo producto).
      La validación semántica via autosuggest actúa como filtro real.
    · Prioriza frases compartidas; trigrama antes que bigrama de igual peso
    """
    seed_norm = _norm_kw(seed_title)

    token_lists = []
    for t in competitor_titles:
        tokens = [
            w for w in _norm_kw(t).split()
            if w not in _KW_STOPWORDS and not w.isdigit() and len(w) > 2
        ]
        if tokens:
            token_lists.append(tokens)

    if not token_lists:
        return []

    phrase_count = {}

    for tokens in token_lists:
        seen_here = set()
        for i in range(len(tokens)):
            for n in (2, 3):
                if i + n <= len(tokens):
                    phrase = ' '.join(tokens[i:i + n])
                    if phrase not in seen_here:
                        phrase_count[phrase] = phrase_count.get(phrase, 0) + 1
                        seen_here.add(phrase)

    candidates = [
        (phrase, count) for phrase, count in phrase_count.items()
        if phrase not in seed_norm
    ]
    candidates.sort(key=lambda x: (-x[1], -len(x[0].split())))
    return [p for p, _ in candidates[:10]]


def _competitor_seeded_autosuggest(competitor_titles: list, seed_title: str) -> list:
    """
    Genera keywords adicionales validadas usando frases de competidores como semillas.
    Validación doble:
      1. Autosuggest devuelve >= 2 resultados  → la familia léxica existe en el mercado
      2. Al menos 1 resultado comparte palabras con el universo del producto
         (seed + todos los competidores) → evita deriva a otra categoría
    """
    import time as _tc

    phrases = _extract_competitor_phrases(competitor_titles, seed_title)
    if not phrases:
        return []

    _sw2 = {'de','para','con','sin','y','el','la','los','las','un','una','en','a','por','al','del'}
    all_product_text = seed_title + ' ' + ' '.join(competitor_titles)
    product_words = {
        w for w in _norm_kw(all_product_text).split()
        if len(w) > 3 and w not in _sw2 and w not in _KW_STOPWORDS
    }

    new_kws = []
    seen    = set()

    for phrase in phrases:
        suggestions = _ml_autosuggest(phrase, limit=8)

        if len(suggestions) < 2:        # familia sin volumen real
            _tc.sleep(0.1)
            continue

        if product_words:               # check deriva semántica
            relevant = any(
                any(pw in _norm_kw(s) for pw in product_words)
                for s in suggestions
            )
            if not relevant:
                _tc.sleep(0.1)
                continue

        for s in suggestions:
            if s not in seen:
                seen.add(s)
                new_kws.append(s)

        _tc.sleep(0.15)

    return new_kws


def get_accounts():
    all_accs = [a for a in (load_json(os.path.join(CONFIG_DIR, 'accounts.json')) or {}).get('accounts', []) if a.get('active')]
    return get_permitted_accounts(all_accs)


# ── Auth middleware ────────────────────────────────────────────────────────────

_AUTH_EXEMPT = {'/login', '/logout', '/setup',
                '/api/capturar-competidor',
                '/api/pending-competidores',
                '/api/list-aliases',
                '/api/ping'}


def _get_request_alias() -> str | None:
    """Extrae el alias del request: URL primero, luego JSON body (para POST)."""
    alias = (request.view_args or {}).get('alias')
    if alias:
        return alias
    if request.method in ('POST', 'PUT', 'PATCH'):
        try:
            body = request.get_json(silent=True) or {}
            a = body.get('alias', '').strip() if isinstance(body, dict) else ''
            if a:
                return a
        except Exception:
            pass
    return None


@app.before_request
def require_login():
    if request.path.startswith('/static'):
        return
    # Auto-actualizar datos si no corrió hoy (resuelve el problema de Render durmiendo)
    if request.path not in _AUTH_EXEMPT and not request.path.startswith('/static'):
        try:
            _auto_update_if_needed()
        except Exception:
            pass
    if request.path in _AUTH_EXEMPT:
        return
    # Si no existe ningún admin todavía, el sistema funciona sin login
    if needs_setup():
        return
    if not session.get('user_id'):
        return redirect(f'/login?next={request.path}')
    # Verificar acceso al alias si el usuario no es admin
    if not session.get('is_admin'):
        alias = _get_request_alias()
        if alias:
            from core.auth import user_can_access
            if not user_can_access(alias):
                app.logger.warning(
                    'Acceso denegado: user=%s alias=%s path=%s',
                    session.get('username'), alias, request.path,
                )
                if request.path.startswith('/api/'):
                    return jsonify({'ok': False, 'error': 'Sin acceso a esta cuenta'}), 403
                return redirect('/')


# ── Rutas de autenticación ─────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_id'):
        return redirect('/')
    if needs_setup():
        return redirect('/setup')
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        user = login_user(username, request.form.get('password', ''))
        if user:
            _audit('LOGIN', usuario=username)
            next_url = request.form.get('next') or request.args.get('next') or '/'
            return redirect(next_url)
        _audit_logger.warning('[%s] LOGIN_FALLIDO ip=%s', username, request.remote_addr)
        error = 'Usuario o contraseña incorrectos.'
    return render_template('login.html', error=error, next=request.args.get('next', ''))


@app.route('/logout')
def logout():
    _audit('LOGOUT')
    logout_user()
    return redirect('/login')


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if not needs_setup():
        return redirect('/')
    error = None
    if request.method == 'POST':
        username  = request.form.get('username', '').strip()
        password  = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if not username or not password:
            error = 'Completá todos los campos.'
        elif password != password2:
            error = 'Las contraseñas no coinciden.'
        elif len(password) < 6:
            error = 'La contraseña debe tener al menos 6 caracteres.'
        else:
            user = create_user(username, password, is_admin=True)
            login_user(username, password)
            return redirect('/')
    return render_template('setup.html', error=error)


# ── Gestión de usuarios (solo admin) ──────────────────────────────────────────

@app.route('/settings/usuarios')
def settings_usuarios():
    user = get_current_user()
    if not user or not user.get('is_admin'):
        return redirect('/')
    all_accounts = [a for a in (load_json(os.path.join(CONFIG_DIR, 'accounts.json')) or {}).get('accounts', []) if a.get('active')]
    return render_template('settings_usuarios.html',
                           users=list_users(),
                           current_user=user,
                           all_accounts=all_accounts,
                           accounts=get_accounts())


@app.route('/api/usuarios/crear', methods=['POST'])
def api_usuarios_crear():
    if not session.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Sin permisos'}), 403
    body     = request.get_json() or {}
    username = body.get('username', '').strip()
    password = body.get('password', '')
    accounts = body.get('accounts', [])
    if not username or not password:
        return jsonify({'ok': False, 'error': 'Faltan datos'})
    from core.auth import get_user_by_username
    if get_user_by_username(username):
        return jsonify({'ok': False, 'error': 'Ese nombre de usuario ya existe'})
    user = create_user(username, password, is_admin=False, accounts=accounts)
    _audit('CREAR_USUARIO', nuevo_usuario=username, cuentas=','.join(accounts))
    return jsonify({'ok': True, 'id': user['id']})


@app.route('/api/usuarios/actualizar', methods=['POST'])
def api_usuarios_actualizar():
    if not session.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Sin permisos'}), 403
    body     = request.get_json() or {}
    user_id  = body.get('id', '')
    accounts = body.get('accounts', [])
    kwargs   = {'accounts': accounts}
    if body.get('password'):
        kwargs['password'] = body['password']
    ok = update_user(user_id, **kwargs)
    if ok:
        changed = 'password+cuentas' if body.get('password') else 'cuentas'
        _audit('EDITAR_USUARIO', user_id=user_id, cambios=changed, cuentas=','.join(accounts))
    return jsonify({'ok': ok})


@app.route('/api/usuarios/eliminar', methods=['POST'])
def api_usuarios_eliminar():
    if not session.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Sin permisos'}), 403
    body    = request.get_json() or {}
    user_id = body.get('id', '')
    user    = get_current_user()
    if user and user['id'] == user_id:
        return jsonify({'ok': False, 'error': 'No podés eliminarte a vos mismo'})
    ok = delete_user(user_id)
    if ok:
        _audit('ELIMINAR_USUARIO', user_id=user_id)
    return jsonify({'ok': ok})


@app.route('/api/usuarios/cambiar-pass-admin', methods=['POST'])
def api_usuarios_cambiar_pass_admin():
    if not session.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Sin permisos'}), 403
    body   = request.get_json() or {}
    actual = body.get('actual', '')
    nueva  = body.get('nueva', '')
    user   = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'Sesión inválida'})
    from core.auth import load_users
    full_user = next((u for u in load_users().get('users', []) if u['id'] == user['id']), None)
    if not full_user or not check_password_hash(full_user['password_hash'], actual):
        return jsonify({'ok': False, 'error': 'La contraseña actual es incorrecta'})
    update_user(user['id'], password=nueva)
    _audit('CAMBIAR_PASSWORD_ADMIN')
    return jsonify({'ok': True})

def build_calendario():
    from modules.radar_oportunidades import _build_calendario
    today = date.today()
    year  = today.year
    eventos = []
    for ev in _build_calendario(year):
        dias = (ev['fecha'] - today).days
        if dias < -7:
            proximas = [e for e in _build_calendario(year + 1) if e['nombre'] == ev['nombre']]
            if proximas:
                p = proximas[0]
                dias = (p['fecha'] - today).days
                eventos.append({**p, 'dias': dias, 'fecha_str': p['fecha'].strftime('%d/%m/%Y')})
        else:
            eventos.append({**ev, 'dias': dias, 'fecha_str': ev['fecha'].strftime('%d/%m/%Y')})
    return sorted(eventos, key=lambda e: e['dias'])


# ── Helpers de datos ML para optimización ────────────────────────────────────

def _fetch_category_attributes(cat_id: str, headers: dict) -> tuple:
    """Devuelve (required, optional) — listas de {name, id, values} para la categoría."""
    try:
        r = req_lib.get(f'https://api.mercadolibre.com/categories/{cat_id}/attributes',
                        headers=headers, timeout=10)
        if not r.ok:
            return [], []
        required, optional = [], []
        for a in r.json():
            name = a.get('name', '')
            if not name:
                continue
            tags = a.get('tags', {})
            entry = {'name': name, 'id': a.get('id', ''), 'type': a.get('value_type', 'string')}
            raw_vals = a.get('values', [])
            if raw_vals:
                entry['values'] = [v.get('name', '') for v in raw_vals if v.get('name')][:12]
            is_req = isinstance(tags, dict) and ('required' in tags or 'catalog_required' in tags)
            (required if is_req else optional).append(entry)
        return required, optional
    except Exception:
        return [], []


def _fetch_keyword_predictions(keywords: list, headers: dict) -> dict:
    """Para cada keyword, devuelve lista de sugerencias reales de búsqueda de ML ordenadas por popularidad."""
    results = {}
    for kw in keywords[:5]:
        try:
            r = req_lib.get('https://api.mercadolibre.com/sites/MLA/search_predictions',
                            params={'q': kw, 'limit': 8},
                            headers=headers, timeout=6)
            if r.ok:
                preds = [p.get('q', '') for p in r.json() if p.get('q')]
                if preds:
                    results[kw] = preds
        except Exception:
            pass
        import time as _t2; _t2.sleep(0.15)
    return results


# ── Análisis de título basado en atributos reales del ítem ───────────────────

# Atributos cuyo valor debería aparecer en el título para mejorar el ranking
_TITLE_RELEVANT_ATTRS = {
    'BRAND', 'MODEL', 'COLOR', 'SIZE', 'MATERIAL', 'COMPOSITION',
    'CAPACITY', 'POWER', 'WEIGHT', 'LENGTH', 'WIDTH', 'VOLTAGE',
    'CONNECTIVITY', 'COMPATIBLE_WITH', 'GENDER', 'AGE_GROUP',
    'FLAVOR', 'SCENT', 'TYPE', 'SUBTYPE', 'LINE',
}
# Atributos a ignorar siempre (internos, logísticos, fiscales)
_TITLE_IGNORE_ATTRS = {
    'GTIN', 'SKU', 'IVA', 'INTERNAL_TAX', 'PACKAGE_HEIGHT', 'PACKAGE_WIDTH',
    'PACKAGE_LENGTH', 'PACKAGE_WEIGHT', 'SELLER_SKU', 'INVOICE',
    'WARRANTY_TYPE', 'WARRANTY_TIME', 'ITEM_CONDITION', 'IS_DESTACADA',
    'IS_TOM_BRAND', 'IS_HIGHLIGHTED_BRAND',
}

def _detect_listing_problems(title: str, item_attrs: list, cat_attrs_map: dict,
                              free_shipping: bool, price: float,
                              photos: int, sold_qty: int) -> dict:
    """
    Detecta problemas que afectan el posicionamiento en ML, enfocados en lo que
    el vendedor PUEDE cambiar hoy: ficha técnica, fotos y envío.
    Nota: el título NO se puede cambiar si la publicación tuvo ventas.
    """
    # Indexar atributos del ítem: attr_id → value
    item_attr_vals = {}
    for a in item_attrs:
        val = a.get('value_name', '') or a.get('value_id', '')
        if val and val.lower() not in ('no aplica', 'no especificado', 'otro', ''):
            item_attr_vals[a['id']] = val

    problems = []

    # ── 1. Atributos requeridos vacíos ───────────────────────────────────────
    # ML requiere estos para dar visibilidad completa a la publicación.
    for attr_id, attr in cat_attrs_map.items():
        if attr_id in _TITLE_IGNORE_ATTRS:
            continue
        tags = attr.get('tags', {})
        if 'hidden' in tags or 'read_only' in tags:
            continue
        is_required = tags.get('required') or tags.get('catalog_required')
        if is_required and attr_id not in item_attr_vals:
            problems.append({
                'nivel':    'critico',
                'problema': f'Atributo requerido "{attr.get("name","")}" vacío en ficha técnica',
                'accion':   'ML penaliza la visibilidad. Editá la ficha técnica en Mis publicaciones → Editar → Ficha técnica.',
            })

    # ── 2. Atributos de filtro importantes vacíos ────────────────────────────
    # Estos son los filtros que usan los compradores en la barra lateral de ML.
    # Si están vacíos, tu publicación NO aparece cuando el comprador filtra por ellos.
    _FILTER_ATTRS = {
        'COLOR':       'Color',
        'SIZE':        'Talle',
        'MATERIAL':    'Material',
        'COMPOSITION': 'Composición',
        'CAPACITY':    'Capacidad',
        'POWER':       'Potencia',
        'VOLTAGE':     'Voltaje',
        'GENDER':      'Género',
        'AGE_GROUP':   'Edad',
        'CONNECTIVITY': 'Conectividad',
        'COMPATIBLE_WITH': 'Compatible con',
    }
    for attr_id, attr_label in _FILTER_ATTRS.items():
        if attr_id not in cat_attrs_map:
            continue   # no aplica a esta categoría
        if attr_id in _TITLE_IGNORE_ATTRS:
            continue
        attr_meta = cat_attrs_map[attr_id]
        if 'hidden' in attr_meta.get('tags', {}):
            continue
        if attr_id not in item_attr_vals:
            problems.append({
                'nivel':    'mejorar',
                'problema': f'"{attr_label}" vacío en ficha técnica',
                'accion':   f'Los compradores filtran por {attr_label.lower()} en ML. Sin este dato, tu publicación no aparece en esos filtros.',
            })

    # ── 3. Pocas fotos ───────────────────────────────────────────────────────
    if photos < 4:
        problems.append({
            'nivel':    'critico',
            'problema': f'Solo {photos} foto{"" if photos == 1 else "s"} — muy pocas',
            'accion':   'ML prioriza publicaciones con 6 o más fotos. Agregá fotos desde distintos ángulos, detalles y en uso.',
        })
    elif photos < 6:
        problems.append({
            'nivel':    'mejorar',
            'problema': f'{photos} fotos — recomendado tener 6 o más',
            'accion':   'Agregá más fotos: detalles del producto, packaging, etiquetas, en uso. Mejora el ranking y la conversión.',
        })

    # ── 4. Sin envío gratis ──────────────────────────────────────────────────
    if not free_shipping and price and price < 200000:
        problems.append({
            'nivel':    'mejorar',
            'problema': 'Sin envío gratis',
            'accion':   'Las publicaciones con envío gratis aparecen antes en el ordenamiento por defecto. Evaluá incluirlo en el precio.',
        })

    # ── 5. Título (solo informativo si no tuvo ventas — en ese caso sí se puede cambiar) ──
    title_len = len(title)
    if sold_qty == 0 and title_len < 55:
        problems.append({
            'nivel':    'mejorar',
            'problema': f'Título corto ({title_len} chars) — todavía no tuvo ventas, podés cambiarlo',
            'accion':   'Incluí marca, modelo, material, talle/color y uso en el título. Ideal: 60-80 caracteres con las palabras que buscan los compradores.',
        })

    criticos = sum(1 for p in problems if p['nivel'] == 'critico')
    mejoras  = sum(1 for p in problems if p['nivel'] == 'mejorar')
    if criticos >= 1:
        urgencia = 'critico'
    elif mejoras >= 3:
        urgencia = 'mejorar'
    elif mejoras >= 1:
        urgencia = 'revisar'
    else:
        urgencia = 'ok'

    return {
        'problems':  problems,
        'urgencia':  urgencia,
        'title_len': title_len,
        'photos':    photos,
        'score':     criticos * 5 + mejoras,
    }


def _enrich_publications_with_attr_analysis(publications: list, account: dict) -> list:
    """
    Detecta problemas de posicionamiento por publicación.
    Fuentes: atributos reales, fotos, envío gratis y ventas de la API de ML.
    """
    if not publications or not account:
        return publications

    token  = account.get('access_token', '')
    hdrs   = {'Authorization': f'Bearer {token}'}
    ML_API = 'https://api.mercadolibre.com'

    # 1. Batch fetch: atributos, fotos, envío, ventas y categoría de cada ítem
    item_ids     = [p['id'] for p in publications if p.get('id')]
    item_data    = {}   # item_id → {attrs, category_id, free_shipping, photos, sold_qty}

    for b in range(0, len(item_ids), 20):
        batch = item_ids[b:b + 20]
        try:
            r = req_lib.get(f'{ML_API}/items', headers=hdrs,
                            params={'ids': ','.join(batch),
                                    'attributes': 'id,category_id,attributes,shipping,pictures,sold_quantity'},
                            timeout=12)
            if r.ok:
                for entry in r.json():
                    if entry.get('code') == 200:
                        body = entry.get('body', {})
                        iid  = body.get('id', '')
                        item_data[iid] = {
                            'attrs':       body.get('attributes', []),
                            'category_id': body.get('category_id', ''),
                            'free_ship':   bool((body.get('shipping') or {}).get('free_shipping')),
                            'photos':      len(body.get('pictures') or []),
                            'sold_qty':    int(body.get('sold_quantity') or 0),
                        }
        except Exception:
            pass

    # 2. Atributos de categoría — cache para no repetir llamadas
    # Guardamos el mapa completo {attr_id → attr_object} para conocer required + tipo
    cat_attrs_cache = {}   # category_id → {attr_id: attr_obj}
    for iid, idata in item_data.items():
        cat_id = idata.get('category_id', '')
        if not cat_id or cat_id in cat_attrs_cache:
            continue
        try:
            r = req_lib.get(f'{ML_API}/categories/{cat_id}/attributes',
                            headers=hdrs, timeout=8)
            if r.ok:
                cat_attrs_cache[cat_id] = {a['id']: a for a in r.json()}
        except Exception:
            cat_attrs_cache[cat_id] = {}

    # 3. Detectar problemas para cada publicación
    for pub in publications:
        iid    = pub.get('id', '')
        idata  = item_data.get(iid, {})
        cat_id = idata.get('category_id', '')

        result = _detect_listing_problems(
            title        = pub.get('titulo', ''),
            item_attrs   = idata.get('attrs', []),
            cat_attrs_map= cat_attrs_cache.get(cat_id, {}),
            free_shipping= idata.get('free_ship', False),
            price        = float(pub.get('precio', 0) or 0),
            photos       = idata.get('photos', 0),
            sold_qty     = idata.get('sold_qty', 0),
        )
        pub['problems']  = result['problems']
        pub['urgencia']  = result['urgencia']
        pub['title_len'] = result['title_len']
        pub['photos']    = result['photos']
        pub['score']     = result['score']

    return publications


# ── ML Competitors via Highlights API ────────────────────────────────────────

def ml_get_competitors(category_id: str, token: str, limit: int = 8) -> list[dict]:
    """
    Obtiene competidores de una categoría via:
    1. /highlights/MLA/category/{cat_id}  → best sellers
    2. /categories/{cat_id}               → subcategorías para más resultados
    3. /products/{id}                     → nombre + foto
    4. /products/{id}/items               → precio, envío, seller_id
    5. /users/{seller_id}                 → nickname
    """
    headers      = {'Authorization': f'Bearer {token}'}
    seller_cache = {}

    def _fetch_highlights_ids(cat_id):
        """Devuelve lista de product_ids de highlights para una categoría."""
        try:
            r = req_lib.get(
                f'https://api.mercadolibre.com/highlights/MLA/category/{cat_id}',
                headers=headers, timeout=10
            )
            if not r.ok:
                return []
            return [e['id'] for e in r.json().get('content', [])
                    if e.get('type') in ('PRODUCT', 'USER_PRODUCT')]
        except Exception:
            return []

    def _fetch_product(prod_id):
        """Devuelve {title, thumbnail} desde /products/{id}."""
        try:
            pr = req_lib.get(
                f'https://api.mercadolibre.com/products/{prod_id}',
                headers=headers, timeout=8
            )
            if not pr.ok:
                return '', ''
            d    = pr.json()
            name = d.get('name', '')
            pics = d.get('pictures', [])
            # Usar thumbnail de 90x90 si existe, sino la URL base con resize
            thumb = ''
            if pics:
                url = pics[0].get('url', '')
                # ML URLs: reemplazar el tamaño por -S (small ~90px)
                thumb = re.sub(r'-[A-Z]+\.jpg', '-S.jpg', url) if url else ''
            return name, thumb
        except Exception:
            return '', ''

    def _fetch_best_item(prod_id):
        """Devuelve el item más relevante de un producto catálogo."""
        try:
            ir = req_lib.get(
                f'https://api.mercadolibre.com/products/{prod_id}/items',
                headers=headers, timeout=8
            )
            if not ir.ok:
                return None
            items = ir.json().get('results', [])
            if not items:
                return None
            items.sort(key=lambda x: (
                not x.get('shipping', {}).get('free_shipping', False),
                x.get('price', 9999999)
            ))
            return items[0]
        except Exception:
            return None

    def _get_nickname(seller_id):
        if not seller_id:
            return '—'
        if seller_id in seller_cache:
            return seller_cache[seller_id]
        try:
            ur = req_lib.get(
                f'https://api.mercadolibre.com/users/{seller_id}',
                headers=headers, timeout=6
            )
            nick = ur.json().get('nickname', '—') if ur.ok else '—'
            seller_cache[seller_id] = nick
            return nick
        except Exception:
            return '—'

    # ── Reunir IDs: categoría principal + subcategorías (no subir al padre) ──
    all_ids  = []
    seen_ids = set()

    def _add_ids(ids):
        for pid in ids:
            # Solo productos de catálogo (MLA*), los MLAU* no tienen endpoint de producto
            if pid not in seen_ids and pid.startswith('MLA') and not pid.startswith('MLAU'):
                seen_ids.add(pid)
                all_ids.append(pid)

    # Highlights de la categoría principal
    _add_ids(_fetch_highlights_ids(category_id))

    # Subcategorías (hijos) para sumar más resultados del mismo nicho
    try:
        cr = req_lib.get(
            f'https://api.mercadolibre.com/categories/{category_id}',
            headers=headers, timeout=8
        )
        if cr.ok:
            children = cr.json().get('children_categories', [])
            for child in children[:6]:
                if len(all_ids) >= limit:
                    break
                _add_ids(_fetch_highlights_ids(child['id']))
    except Exception:
        pass

    # ── Construir resultados ──────────────────────────────────────────────────
    results = []
    for pos, prod_id in enumerate(all_ids[:limit], 1):
        try:
            name, thumb = _fetch_product(prod_id)
            best        = _fetch_best_item(prod_id)
            if not best:
                continue
            listing_type = best.get('listing_type_id', '')
            nickname     = _get_nickname(best.get('seller_id', ''))
            results.append({
                'id':            prod_id,
                'title':         name,
                'thumbnail':     thumb,
                'price':         best.get('price', 0),
                'sold_quantity': 0,
                'seller':        nickname,
                'free_shipping': best.get('shipping', {}).get('free_shipping', False),
                'premium':       listing_type in ('gold_special', 'gold_pro'),
                'listing_type':  listing_type,
                'position':      pos,
            })
            _time_module.sleep(0.08)
        except Exception:
            continue
    return results


def _fetch_competitors_full(keyword: str, token: str, exclude_id: str = '', limit: int = 5) -> list[dict]:
    """Busca en ML por keyword y devuelve los top N competidores con datos completos
    (título, precio, atributos, descripción) listos para el prompt del optimizador."""
    import time as _t
    headers = {'Authorization': f'Bearer {token}'}
    results = []
    try:
        resp = req_lib.get(
            'https://api.mercadolibre.com/sites/MLA/search',
            headers=headers,
            params={'q': keyword, 'limit': 20},
            timeout=10,
        )
        if not resp.ok:
            return []
        search_items = [it for it in resp.json().get('results', []) if it.get('id') != exclude_id]
        for item in search_items[:limit]:
            item_id = item['id']
            comp = {
                'title':          item.get('title', ''),
                'seller':         item.get('seller', {}).get('nickname', '—'),
                'sold_quantity':  item.get('sold_quantity', 0),
                'price':          item.get('price', 0),
                'free_ship':      item.get('shipping', {}).get('free_shipping', False),
                'premium':        item.get('listing_type_id', '') in ('gold_special', 'gold_pro'),
                'photos_count':   0,
                'reviews_rating': 0,
                'reviews_total':  0,
                'attributes':     [],
                'main_features':  [],
                'description':    '',
                'reviews_sample': [],
            }
            # Fetch full item attributes and photo count
            try:
                ir = req_lib.get(
                    f'https://api.mercadolibre.com/items/{item_id}',
                    headers=headers, timeout=8
                )
                if ir.ok:
                    full = ir.json()
                    comp['photos_count'] = len(full.get('pictures', []))
                    comp['attributes'] = [
                        {'name': a.get('name', ''), 'value': a.get('value_name', '') or str(a.get('value_id', ''))}
                        for a in full.get('attributes', [])
                        if a.get('value_name') or a.get('value_id')
                    ]
            except Exception:
                pass
            # Fetch description
            try:
                dr = req_lib.get(
                    f'https://api.mercadolibre.com/items/{item_id}/description',
                    headers=headers, timeout=8
                )
                if dr.ok:
                    comp['description'] = dr.json().get('plain_text', '')[:1000]
            except Exception:
                pass
            # Fetch answered questions (preguntas reales de compradores)
            try:
                qr = req_lib.get(
                    'https://api.mercadolibre.com/questions/search',
                    headers=headers,
                    params={'item': item_id, 'status': 'ANSWERED', 'limit': 30},
                    timeout=8
                )
                if qr.ok:
                    questions = qr.json().get('questions', [])
                    comp['questions_qa'] = [
                        {'q': q.get('text', ''), 'a': (q.get('answer') or {}).get('text', '')}
                        for q in questions
                        if q.get('text') and (q.get('answer') or {}).get('text')
                    ][:15]
            except Exception:
                pass
            # Fetch reviews
            try:
                rr = req_lib.get(
                    f'https://api.mercadolibre.com/reviews/item/{item_id}',
                    headers=headers,
                    params={'limit': 15},
                    timeout=8
                )
                if rr.ok:
                    rev_data = rr.json()
                    comp['reviews_rating'] = rev_data.get('rating_average', 0)
                    comp['reviews_total']  = rev_data.get('paging', {}).get('total', 0)
                    comp['reviews_sample'] = [
                        r.get('content', '') for r in rev_data.get('reviews', [])
                        if r.get('content')
                    ][:8]
            except Exception:
                pass
            results.append(comp)
            _t.sleep(0.15)
    except Exception:
        pass
    return results


def _fetch_competitor_summary(item_id: str, token: str) -> dict | None:
    """
    Obtiene un resumen eficiente de un competidor:
    título, precio, shipping, tipo, fotos, reputación básica, atributos principales.
    No trae descripción completa para no inflar tokens.
    """
    headers = {'Authorization': f'Bearer {token}'}
    try:
        r = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}',
            headers=headers,
            params={'attributes': 'id,title,price,listing_type_id,shipping,sold_quantity,'
                                  'pictures,attributes,seller_reputation,tags,condition'},
            timeout=8,
        )
        if not r.ok:
            return None
        d = r.json()

        # Atributos: solo los que tienen valor real, máximo 12
        attrs = [
            f"{a.get('name','')}: {a.get('value_name','')}"
            for a in d.get('attributes', [])
            if a.get('value_name') and a.get('value_name') != 'No especificado'
        ][:12]

        # Reputación del vendedor (disponible en listing search, no siempre en /items)
        rep = d.get('seller_reputation', {})
        rep_level   = rep.get('level_id', '')        # e.g. "5_green"
        rep_sales   = rep.get('transactions', {}).get('completed', 0)
        claims_pct  = rep.get('metrics', {}).get('claims', {}).get('rate', None)

        listing_type = d.get('listing_type_id', '')
        is_premium   = listing_type in ('gold_special', 'gold_pro')

        return {
            'id':           d.get('id', item_id),
            'title':        d.get('title', ''),
            'price':        d.get('price', 0),
            'condition':    d.get('condition', ''),
            'free_shipping': d.get('shipping', {}).get('free_shipping', False),
            'listing_type': listing_type,
            'is_premium':   is_premium,
            'photos':       len(d.get('pictures', [])),
            'sold_quantity': d.get('sold_quantity', 0),
            'rep_level':    rep_level,
            'rep_sales':    rep_sales,
            'claims_pct':   claims_pct,
            'attributes':   attrs,
        }
    except Exception:
        return None


def _fetch_top_competitors_summary(keywords: list[str], item_id: str, token: str, top_n: int = 5) -> list[dict]:
    """
    Para la primera keyword con resultados, extrae resúmenes de los top_n competidores
    (excluyendo el propio item_id). Usa _fetch_competitor_summary por cada uno.
    """
    import time as _time
    headers = {'Authorization': f'Bearer {token}'}

    candidate_ids = []
    for kw in keywords[:3]:
        try:
            resp = req_lib.get(
                'https://api.mercadolibre.com/sites/MLA/search',
                headers=headers,
                params={'q': kw, 'limit': 10},
                timeout=10,
            )
            if resp.ok:
                for r in resp.json().get('results', []):
                    rid = r.get('id', '')
                    if rid and rid != item_id and rid not in candidate_ids:
                        candidate_ids.append(rid)
            if len(candidate_ids) >= top_n:
                break
            _time.sleep(0.15)
        except Exception:
            continue

    summaries = []
    for cid in candidate_ids[:top_n]:
        s = _fetch_competitor_summary(cid, token)
        if s:
            summaries.append(s)
        _time.sleep(0.15)

    return summaries


def _diagnose_listing(
    item: dict,
    keywords: list[str],
    position_results: list[dict],
    competitors: list[dict],
) -> list[dict]:
    """
    Analiza causas de bajo rendimiento de una publicación.

    Parámetros:
      item             — datos del item propio (title, price, attributes, shipping, photos, etc.)
      keywords         — keywords extraídas del autosuggest
      position_results — lista de dicts de _check_keyword_position
      competitors      — lista de dicts de _fetch_competitor_summary

    Devuelve lista de dicts ordenada por impacto:
      [{'causa': str, 'detalle': str, 'impacto': 'alto'|'medio'|'bajo'}]
    """
    causes = []

    my_price     = item.get('price', 0) or 0
    my_photos    = item.get('photos', item.get('photos_count', 0)) or 0
    my_attrs     = item.get('attributes', [])
    my_title     = (item.get('title', '') or '').lower()
    my_free_ship = item.get('shipping', {}).get('free_shipping', False) if isinstance(item.get('shipping'), dict) else False

    # ── 1. Posición por keywords ──────────────────────────────────────────────
    ranked    = [r for r in position_results if r.get('position') is not None]
    not_found = [r for r in position_results if r.get('position') is None]

    if not_found and not ranked:
        causes.append({
            'causa':   'Sin posicionamiento detectado',
            'detalle': f'El item no aparece en ninguna de las {len(not_found)} keywords analizadas.',
            'impacto': 'alto',
        })
    elif not_found:
        causes.append({
            'causa':   'Keywords sin ranking',
            'detalle': f'No rankea en {len(not_found)} de {len(position_results)} keywords: '
                       + ', '.join(f'"{r["keyword"]}"' for r in not_found[:3]),
            'impacto': 'medio',
        })

    best_pos = min((r['position'] for r in ranked), default=None)
    if best_pos is not None and best_pos > 20:
        impacto = 'alto' if best_pos > 50 else 'medio'
        causes.append({
            'causa':   'Posición baja en búsquedas',
            'detalle': f'Mejor posición encontrada: #{best_pos}. Compradores ven competidores primero.',
            'impacto': impacto,
        })

    # ── 2. Cobertura de keywords en el título ─────────────────────────────────
    kws_in_title = [kw for kw in keywords if kw.lower() in my_title]
    kws_missing  = [kw for kw in keywords if kw.lower() not in my_title]
    coverage_pct = len(kws_in_title) / len(keywords) * 100 if keywords else 100

    if coverage_pct < 30:
        causes.append({
            'causa':   'Título con baja cobertura de keywords reales',
            'detalle': f'Solo {len(kws_in_title)}/{len(keywords)} keywords del autosuggest aparecen en el título. '
                       f'Faltantes: {", ".join(kws_missing[:4])}',
            'impacto': 'alto',
        })
    elif coverage_pct < 60:
        causes.append({
            'causa':   'Título con cobertura parcial de keywords',
            'detalle': f'{len(kws_missing)} keywords relevantes ausentes del título: '
                       + ', '.join(f'"{k}"' for k in kws_missing[:3]),
            'impacto': 'medio',
        })

    # ── 3. Precio relativo a competidores ─────────────────────────────────────
    comp_prices = [c['price'] for c in competitors if c.get('price', 0) > 0]
    if comp_prices and my_price > 0:
        avg_comp = sum(comp_prices) / len(comp_prices)
        min_comp = min(comp_prices)
        pct_vs_avg = (my_price - avg_comp) / avg_comp * 100

        if pct_vs_avg > 20:
            causes.append({
                'causa':   'Precio significativamente mayor al promedio',
                'detalle': f'Tu precio ${my_price:,.0f} está {pct_vs_avg:.0f}% sobre el promedio '
                           f'de competidores (${avg_comp:,.0f}). Mínimo competidor: ${min_comp:,.0f}.',
                'impacto': 'alto',
            })
        elif pct_vs_avg > 8:
            causes.append({
                'causa':   'Precio levemente por encima del promedio',
                'detalle': f'Tu precio ${my_price:,.0f} está {pct_vs_avg:.0f}% sobre el promedio '
                           f'de competidores (${avg_comp:,.0f}).',
                'impacto': 'medio',
            })

    # ── 4. Shipping gratuito ──────────────────────────────────────────────────
    comp_free = [c for c in competitors if c.get('free_shipping')]
    if not my_free_ship and comp_free:
        causes.append({
            'causa':   'Sin envío gratis frente a competidores que sí lo ofrecen',
            'detalle': f'{len(comp_free)} de {len(competitors)} competidores tienen envío gratis.',
            'impacto': 'alto' if len(comp_free) >= len(competitors) // 2 + 1 else 'medio',
        })

    # ── 5. Fotos ──────────────────────────────────────────────────────────────
    comp_photos = [c['photos'] for c in competitors if c.get('photos', 0) > 0]
    avg_photos  = sum(comp_photos) / len(comp_photos) if comp_photos else 0

    if my_photos < 4:
        causes.append({
            'causa':   'Pocas fotos',
            'detalle': f'La publicación tiene {my_photos} foto(s). '
                       + (f'Competidores tienen en promedio {avg_photos:.0f}.' if avg_photos else ''),
            'impacto': 'alto' if my_photos <= 1 else 'medio',
        })
    elif avg_photos > 0 and my_photos < avg_photos * 0.6:
        causes.append({
            'causa':   'Menos fotos que los competidores',
            'detalle': f'Tenés {my_photos} fotos vs. promedio competidores {avg_photos:.0f}.',
            'impacto': 'bajo',
        })

    # ── 6. Atributos ──────────────────────────────────────────────────────────
    comp_attr_counts = [len(c.get('attributes', [])) for c in competitors]
    avg_comp_attrs   = sum(comp_attr_counts) / len(comp_attr_counts) if comp_attr_counts else 0
    my_attr_count    = len(my_attrs)

    if my_attr_count == 0:
        causes.append({
            'causa':   'Sin atributos completados',
            'detalle': 'La ficha técnica está vacía. ML penaliza el ranking de publicaciones sin atributos.',
            'impacto': 'alto',
        })
    elif avg_comp_attrs > 0 and my_attr_count < avg_comp_attrs * 0.5:
        causes.append({
            'causa':   'Ficha técnica incompleta frente a competidores',
            'detalle': f'Tenés {my_attr_count} atributos vs. promedio competidores {avg_comp_attrs:.0f}.',
            'impacto': 'medio',
        })

    # ── 7. Tipo de publicación ────────────────────────────────────────────────
    comp_premium = [c for c in competitors if c.get('is_premium')]
    my_premium   = item.get('listing_type_id', '') in ('gold_special', 'gold_pro')
    if not my_premium and len(comp_premium) >= 2:
        causes.append({
            'causa':   'Publicación clásica frente a competidores premium',
            'detalle': f'{len(comp_premium)} competidores tienen publicación Oro/Premium con mayor exposición.',
            'impacto': 'bajo',
        })

    # Ordenar: alto → medio → bajo
    order = {'alto': 0, 'medio': 1, 'bajo': 2}
    causes.sort(key=lambda c: order.get(c['impacto'], 9))

    return causes


def _build_optimization_result(
    item: dict,
    keywords: list[str],
    position_results: list[dict],
    competitors: list[dict],
    causes: list[dict],
) -> dict:
    """
    Llama a Claude para generar 3 títulos, atributos sugeridos y descripción optimizada.
    Devuelve un dict con la salida final estructurada de Optimización IA.

    Parámetros:
      item             — datos del item propio
      keywords         — keywords del autosuggest (priorizadas)
      position_results — resultado de _check_positions_for_keywords
      competitors      — resultado de _fetch_top_competitors_summary
      causes           — resultado de _diagnose_listing
    """
    title       = item.get('title', '')
    price       = item.get('price', 0)
    my_attrs    = item.get('attributes', [])
    cat_name    = item.get('category_name', '')
    free_ship   = item.get('shipping', {}).get('free_shipping', False) if isinstance(item.get('shipping'), dict) else False

    # Resumen de posiciones
    ranked     = [(r['keyword'], r['position']) for r in position_results if r.get('position')]
    not_ranked = [r['keyword'] for r in position_results if not r.get('position')]
    best_pos   = min((p for _, p in ranked), default=None)

    # Keywords priorizadas: rankeadas (mejor pos) → sin ranking → resto del autosuggest
    ranked_kws   = [kw for kw, _ in sorted(ranked, key=lambda x: x[1])]
    seen_kws     = set(ranked_kws) | set(not_ranked)
    extra_kws    = [kw for kw in keywords if kw not in seen_kws]
    kws_sorted   = ranked_kws + not_ranked + extra_kws

    # Atributos propios
    my_attr_names = set()
    my_attrs_text = ''
    if my_attrs:
        if isinstance(my_attrs[0], dict):
            my_attr_names = {a.get('name', '') for a in my_attrs}
            my_attrs_text = '\n'.join(f"  {a.get('name','')}: {a.get('value','') or a.get('value_name','')}" for a in my_attrs[:15])
        else:
            my_attrs_text = '\n'.join(f"  {a}" for a in my_attrs[:15])
            for a in my_attrs[:15]:
                my_attr_names.add(a.split(':')[0].strip() if ':' in a else a)

    # Atributos de competidores no presentes en los propios
    comp_attr_pool: dict[str, list[str]] = {}
    for c in competitors:
        for a in c.get('attributes', []):
            if ':' in a:
                name, val = a.split(':', 1)
                name = name.strip(); val = val.strip()
                if name not in my_attr_names:
                    comp_attr_pool.setdefault(name, [])
                    if val not in comp_attr_pool[name]:
                        comp_attr_pool[name].append(val)
    suggested_attrs = [
        f"{name} (ej: {', '.join(vals[:2])})"
        for name, vals in list(comp_attr_pool.items())[:8]
        if vals
    ]

    # Resumen ejecutivo
    exec_summary_parts = []
    if best_pos:
        exec_summary_parts.append(f"mejor posición #{best_pos}")
    elif not ranked:
        exec_summary_parts.append("sin posicionamiento detectado")
    high_causes = [c['causa'] for c in causes if c['impacto'] == 'alto']
    if high_causes:
        exec_summary_parts.append("causas críticas: " + "; ".join(high_causes[:2]))
    exec_summary = ". ".join(exec_summary_parts).capitalize() if exec_summary_parts else "Análisis completado."

    # Resumen de competidores para el prompt
    comp_lines = []
    for c in competitors[:4]:
        attrs_s = '; '.join(c.get('attributes', [])[:5])
        comp_lines.append(
            f"  - {c['title'][:80]} | ${c['price']:,.0f}"
            f"{' | envío gratis' if c.get('free_shipping') else ''}"
            f"{' | premium' if c.get('is_premium') else ''}"
            f" | fotos: {c.get('photos', 0)}"
            + (f"\n    atributos: {attrs_s}" if attrs_s else '')
        )

    causes_lines = '\n'.join(
        f"  [{c['impacto'].upper()}] {c['causa']}: {c['detalle']}"
        for c in causes[:6]
    )

    prompt = f"""Sos un experto en optimización de publicaciones de MercadoLibre Argentina.
Analizá los datos reales y generá exactamente lo que se pide, sin relleno.

═══ DATOS DE LA PUBLICACIÓN ═══
Título actual: {title}
Precio: ${price:,.0f}{' | Envío gratis' if free_ship else ''}
Categoría: {cat_name}
Atributos actuales:
{my_attrs_text or '  (ninguno)'}

═══ KEYWORDS REALES (autosuggest ML, por relevancia) ═══
{chr(10).join(f'  {i+1}. {kw}' for i, kw in enumerate(kws_sorted[:10]))}

═══ POSICIONAMIENTO ═══
{chr(10).join(f'  "{kw}" → #{pos}' for kw, pos in ranked[:5]) or '  Sin posicionamiento detectado'}
{f"  Sin ranking: {', '.join(not_ranked[:5])}" if not_ranked else ''}

═══ COMPETIDORES RELEVANTES ═══
{chr(10).join(comp_lines) or '  (no disponible)'}

═══ CAUSAS PRINCIPALES DE BAJO RENDIMIENTO ═══
{causes_lines or '  (ninguna detectada)'}

═══ ATRIBUTOS QUE TIENEN COMPETIDORES Y VOS NO ═══
{chr(10).join(f'  - {a}' for a in suggested_attrs) or '  (ninguno adicional detectado)'}

═══ LO QUE NECESITO ═══

**TITULO_1_SEO**: título maximizando keywords reales del autosuggest, 55-60 caracteres, sin inventar.
**ESTRATEGIA_1**: una línea explicando el enfoque SEO usado.

**TITULO_2_BALANCEADO**: título que balancea keywords con claridad y conversión, 50-58 caracteres.
**ESTRATEGIA_2**: una línea explicando el balance.

**TITULO_3_DIFERENCIADOR**: título que destaca un diferenciador real (precio, envío, atributos), 48-58 caracteres.
**ESTRATEGIA_3**: una línea explicando el diferenciador.

**ATRIBUTOS_SUGERIDOS**: lista de atributos que faltan y son verificables (solo los que tienen evidencia en competidores). Formato: "Nombre: valor sugerido". Máximo 6.

**DESCRIPCION**: descripción de 300-450 palabras. Sin relleno, sin frases genéricas. Estructura:
- párrafo de apertura con el beneficio principal y 2-3 keywords naturales
- características técnicas verificables (bullet points concisos)
- por qué elegir este producto (basado en datos reales, no inventar)
- cierre con llamada a la acción natural

Respondé únicamente con las secciones marcadas con **, sin texto adicional."""

    ai   = anthropic.Anthropic()
    resp = ai.messages.create(
        model='claude-opus-4-6',
        max_tokens=1800,
        messages=[{'role': 'user', 'content': prompt}],
    )
    _log_token_usage('Optimizar IA — Títulos/Descripción', 'claude-opus-4-6', resp.usage.input_tokens, resp.usage.output_tokens)
    raw = resp.content[0].text if resp.content else ''

    def _extract(tag: str) -> str:
        import re as _re
        m = _re.search(rf'\*\*{tag}\*\*:?\s*(.*?)(?=\n\*\*|\Z)', raw, _re.DOTALL)
        return m.group(1).strip() if m else ''

    titulos = [
        {'titulo': _extract('TITULO_1_SEO'),         'estrategia': _extract('ESTRATEGIA_1')},
        {'titulo': _extract('TITULO_2_BALANCEADO'),   'estrategia': _extract('ESTRATEGIA_2')},
        {'titulo': _extract('TITULO_3_DIFERENCIADOR'),'estrategia': _extract('ESTRATEGIA_3')},
    ]
    titulos = [t for t in titulos if t['titulo']]

    attrs_raw  = _extract('ATRIBUTOS_SUGERIDOS')
    descripcion = _extract('DESCRIPCION')

    return {
        'resumen_ejecutivo':    exec_summary,
        'keywords':             kws_sorted[:10],
        'posiciones':           [{'keyword': kw, 'posicion': pos} for kw, pos in ranked],
        'keywords_sin_ranking': not_ranked,
        'competidores':         competitors,
        'causas':               causes,
        'titulos_alt':          titulos,
        'atributos_sugeridos':  attrs_raw,
        'descripcion_nueva':    descripcion,
    }


def ml_search_web(query: str, limit: int = 20) -> list[dict]:
    """Stub — scraping bloqueado por ML. Usar ml_get_competitors() con category_id."""
    return []


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    accounts = get_accounts()
    rows = []
    today     = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    for acc in accounts:
        alias = acc['alias']
        s     = safe(alias)

        rep_data   = load_json(os.path.join(DATA_DIR, f'reputacion_{s}.json'))
        stock_data = load_json(os.path.join(DATA_DIR, f'stock_{s}.json'))
        pos_data   = load_json(os.path.join(DATA_DIR, f'posiciones_{s}.json'))

        rep_latest = rep_data[-1] if rep_data else None
        rep_ok = None
        if rep_latest:
            rep_ok = (
                rep_latest.get('reclamos_pct', 0) <= 2.0 and
                rep_latest.get('demoras_pct', 0) <= 15.0 and
                rep_latest.get('cancelaciones_pct', 0) <= 5.0
            )

        stock_items = (stock_data or {}).get('items', [])
        sin_stock   = sum(1 for i in stock_items if i.get('alerta_stock') == 'SIN_STOCK')
        criticos    = sum(1 for i in stock_items if i.get('alerta_stock') == 'CRITICO')
        margen_neg  = sum(1 for i in stock_items if i.get('alerta_margen') == 'NEGATIVO')

        bajaron = total_pos = 0
        if pos_data:
            for item_data in pos_data.values():
                hist     = item_data.get('history', {})
                pos_hoy  = hist.get(today)
                pos_ayer = hist.get(yesterday)
                if pos_hoy is not None:
                    total_pos += 1
                if pos_hoy and pos_ayer and pos_hoy != 999 and (pos_hoy - pos_ayer) >= 3:
                    bajaron += 1

        repricing_cfg = load_json(os.path.join(CONFIG_DIR, 'repricing.json')) or {}
        repricing_count = len([
            iid for iid, cfg in repricing_cfg.get('items', {}).items()
            if cfg.get('alias', alias) == alias and cfg.get('activo', True)
        ])

        # Publicaciones pausadas — consulta rápida a ML
        pausadas_count = 0
        try:
            from core.account_manager import AccountManager as _AM2
            _mgr2    = _AM2()
            _client2 = _mgr2.get_client(alias)
            _client2._ensure_token()
            _heads2  = {'Authorization': f'Bearer {_client2.account.access_token}'}
            _uid2    = str(_client2.account.user_id)
            _rp = req_lib.get(
                f'https://api.mercadolibre.com/users/{_uid2}/items/search',
                headers=_heads2, params={'status': 'paused', 'limit': 1}, timeout=6)
            if _rp.ok:
                pausadas_count = _rp.json().get('paging', {}).get('total', 0)
        except Exception:
            pass

        rows.append({
            'alias':           alias,
            'nickname':        acc.get('nickname', ''),
            'rep_ok':          rep_ok,
            'rep_latest':      rep_latest,
            'sin_stock':       sin_stock,
            'criticos':        criticos,
            'margen_neg':      margen_neg,
            'total_pubs':      len(stock_items) or total_pos,
            'bajaron':         bajaron,
            'total_pos':       total_pos,
            'stock_fecha':     (stock_data or {}).get('fecha'),
            'rep_fecha':       rep_latest.get('fecha') if rep_latest else None,
            'repricing_count': repricing_count,
            'pausadas':        pausadas_count,
        })

    calendario = build_calendario()[:3]
    return render_template('dashboard.html', rows=rows, calendario=calendario,
                           accounts=accounts)


# ── Monitoreo ─────────────────────────────────────────────────────────────────

@app.route('/stock/<alias>')
def stock(alias):
    data        = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json'))
    costos_data = load_json(os.path.join(CONFIG_DIR, 'costos.json')) or {}

    fees = get_fee_rates()  # lee de config/fees.json (sin refrescar)

    items = []
    if data:
        for raw in data.get('items', []):
            item_id = raw.get('id', '')
            # Costo: preferir costos.json (siempre el más reciente)
            costo_entry = costos_data.get(item_id, {})
            costo = costo_entry.get('costo') if costo_entry else raw.get('costo')
            capital = round(costo * raw.get('stock', 0), 0) if costo else None

            # Fee rate: usar el real del JSON (calculado de órdenes históricas)
            # o la tasa actualizada de la API de ML
            precio    = float(raw.get('precio', 0))
            fee_rate  = raw.get('fee_rate') or get_rate(raw.get('listing_type', ''), fees)
            fee_source = raw.get('fee_source', 'estimado')
            total_fee = round(precio * fee_rate, 2)
            neto      = precio - total_fee

            if costo and costo > 0:
                ganancia   = neto - costo
                margen_pct = ganancia / precio if precio > 0 else 0
            else:
                ganancia   = raw.get('ganancia')
                margen_pct = raw.get('margen_pct')

            alerta_margen = None
            if margen_pct is not None:
                if margen_pct < 0:
                    alerta_margen = 'NEGATIVO'
                elif margen_pct < 0.10:
                    alerta_margen = 'BAJO'

            ventas_30d    = raw.get('ventas_30d') or round((raw.get('velocidad') or 0) * 30)
            visitas_30d   = raw.get('visitas_30d')
            conversion_pct = raw.get('conversion_pct')

            items.append({**raw,
                'costo':          costo,
                'capital':        capital,
                'fee_rate':       fee_rate,
                'fee_source':     fee_source,
                'neto':           neto,
                'ganancia':       ganancia,
                'margen_pct':     margen_pct,
                'alerta_margen':  alerta_margen,
                'ventas_30d':     ventas_30d,
                'visitas_30d':    visitas_30d,
                'conversion_pct': conversion_pct,
            })
        items = sorted(items, key=lambda x: (
            x.get('alerta_stock') != 'SIN_STOCK',
            x.get('alerta_stock') != 'CRITICO',
            x.get('alerta_stock') != 'ADVERTENCIA',
            x.get('alerta_margen') != 'NEGATIVO',
            -(x.get('velocidad') or 0),
        ))

    # ── Enriquecer con posiciones, competencia y reputación ──────────────────
    pos_data  = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
    comp_data = load_json(os.path.join(DATA_DIR, f'competencia_{safe(alias)}.json')) or {}
    rep_data  = load_json(os.path.join(DATA_DIR, f'reputacion_{safe(alias)}.json')) or []

    # Reputación — último snapshot
    rep_latest = rep_data[-1] if rep_data else {}
    rep_reclamos = rep_latest.get('reclamos_pct', 0) or 0
    rep_demoras  = rep_latest.get('demoras_pct', 0) or 0
    rep_nivel    = rep_latest.get('nivel', '')

    # Keywords faltantes por item desde competencia
    kw_faltantes_map = {}
    for cat_val in comp_data.get('categorias', {}).values():
        for pub in cat_val.get('mis_publicaciones', []):
            pid = pub.get('id', '')
            if pid and pub.get('keywords_faltantes'):
                kw_faltantes_map[pid] = pub['keywords_faltantes']

    for item in items:
        iid = item.get('id', '')

        # Posición actual + tendencia + contexto histórico
        pos_item    = pos_data.get(iid, {})
        pos_history = pos_item.get('history', {})
        pos_sorted  = sorted(pos_history.items())
        pos_current = pos_sorted[-1][1] if pos_sorted else None
        pos_trend   = ''
        if len(pos_sorted) >= 2:
            prev, curr = pos_sorted[-2][1], pos_sorted[-1][1]
            if curr < prev:   pos_trend = 'subiendo'
            elif curr > prev: pos_trend = 'bajando'
            else:             pos_trend = 'estable'
        # Best real position in history (excluding 999)
        real_positions = [v for _, v in pos_sorted if v != 999]
        pos_best = min(real_positions) if real_positions else None
        # Best position date
        pos_best_date = next((d for d, v in pos_sorted if v == pos_best), None) if pos_best else None
        # Usar la keyword real guardada por el monitor (autosuggest-based)
        pos_query = pos_item.get('keyword', '')
        item['posicion']      = pos_current
        item['pos_trend']     = pos_trend
        item['pos_best']      = pos_best
        item['pos_best_date'] = pos_best_date
        item['pos_query']     = pos_query
        item['kw_faltantes']  = kw_faltantes_map.get(iid, [])
        item['rep_reclamos']  = rep_reclamos
        item['rep_demoras']   = rep_demoras
        item['rep_nivel']     = rep_nivel

    sin_costo = sum(1 for i in items if not i.get('costo'))
    capital_total = sum(i.get('capital') or 0 for i in items)

    # Métricas de experiencia de compra a nivel cuenta (se muestran una sola vez)
    seller_metrics = None
    try:
        from core.account_manager import AccountManager
        mgr = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        heads = {'Authorization': f'Bearer {client.account.access_token}'}
        r_rep = req_lib.get(
            f'https://api.mercadolibre.com/users/{client.account.user_id}',
            headers=heads, params={'attributes': 'seller_reputation'}, timeout=6)
        if r_rep.ok:
            rep = r_rep.json().get('seller_reputation', {})
            m = rep.get('metrics', {})
            seller_metrics = {
                'level':        rep.get('level_id', ''),
                'claims_rate':  round(float(m.get('claims', {}).get('rate', 0)) * 100, 2),
                'claims_value': int(m.get('claims', {}).get('value', 0)),
                'delays_rate':  round(float(m.get('delayed_handling_time', {}).get('rate', 0)) * 100, 2),
                'delays_value': int(m.get('delayed_handling_time', {}).get('value', 0)),
                'cancel_rate':  round(float(m.get('cancellations', {}).get('rate', 0)) * 100, 2),
                'cancel_value': int(m.get('cancellations', {}).get('value', 0)),
                'period':       m.get('claims', {}).get('period', '60 días'),
            }
    except Exception:
        pass

    return render_template('stock.html', alias=alias, items=items,
                           fecha=(data or {}).get('fecha'),
                           sin_costo=sin_costo, capital_total=capital_total,
                           seller_metrics=seller_metrics,
                           accounts=get_accounts())


@app.route('/salud/<alias>')
def salud(alias):
    """Publicaciones en catálogo ML: precio propio vs precio del buy box."""
    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return render_template('salud.html', alias=alias, items=[], resumen={},
                               accounts=get_accounts())

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception:
        return render_template('salud.html', alias=alias, items=[], resumen={},
                               accounts=get_accounts())
    ML = 'https://api.mercadolibre.com'

    # 1 — Recolectar todos los IDs (activos + pausados, catálogo puede quedar paused)
    all_ids = []
    for status in ('active', 'paused'):
        offset = 0
        while True:
            r = req_lib.get(f'{ML}/users/{user_id}/items/search', headers=heads,
                            params={'status': status, 'limit': 100, 'offset': offset},
                            timeout=12)
            if not r.ok:
                break
            ids   = r.json().get('results', [])
            total = r.json().get('paging', {}).get('total', 0)
            all_ids.extend(ids)
            offset += len(ids)
            if not ids or offset >= total or offset >= 400:
                break
            _time_module.sleep(0.1)

    # 2 — Fetch en lotes, quedarse solo con los que tienen catalog_product_id
    seen = set()
    unique_ids = [i for i in all_ids if not (i in seen or seen.add(i))]

    catalog_items = []
    for b in range(0, len(unique_ids), 20):
        batch = unique_ids[b:b+20]
        try:
            r = req_lib.get(f'{ML}/items', headers=heads,
                            params={'ids': ','.join(batch),
                                    'attributes': 'id,title,price,permalink,status,catalog_product_id'},
                            timeout=12)
            if r.ok:
                for e in r.json():
                    if e.get('code') != 200:
                        continue
                    body = e.get('body', {})
                    cpid = body.get('catalog_product_id')
                    if not cpid:
                        continue
                    catalog_items.append({
                        'id':                 body.get('id', ''),
                        'titulo':             body.get('title', '')[:70],
                        'precio':             float(body.get('price', 0) or 0),
                        'permalink':          body.get('permalink', ''),
                        'status':             body.get('status', ''),
                        'catalog_product_id': cpid,
                        'buy_box_price':      None,
                        'buy_box_winner_id':  None,
                        'we_win':             None,
                        'competidores':       0,
                        'diferencia_pct':     None,
                        'precio_ideal':       None,
                        'winner_stock':       None,
                        'segundo_precio':     None,   # precio del 2do competidor
                    })
        except Exception:
            pass
        _time_module.sleep(0.1)

    # 3 — Para cada item de catálogo, obtener el buy box actual + stock del ganador
    for it in catalog_items:
        try:
            r = req_lib.get(f'{ML}/products/{it["catalog_product_id"]}/items',
                            headers=heads, params={'limit': 10}, timeout=8)
            if r.ok:
                sellers = r.json().get('results', [])
                it['competidores'] = len(sellers)
                if sellers:
                    winner    = sellers[0]
                    winner_id = winner.get('id') or winner.get('item_id', '')
                    bb_price  = float(winner.get('price') or 0)
                    it['buy_box_price']     = bb_price
                    it['buy_box_winner_id'] = winner_id
                    it['we_win']            = (winner_id == it['id'])
                    # diferencia_pct: si ganamos → margen sobre 2do competidor
                    #                 si perdemos → cuánto más caro estamos vs el buy box

                    # 2do competidor: si ganamos es sellers[1], si perdemos también hay uno detrás
                    if it['we_win'] and len(sellers) >= 2:
                        it['segundo_precio'] = float(sellers[1].get('price') or 0)
                    elif not it['we_win'] and len(sellers) >= 2:
                        for s in sellers[1:]:
                            sid = s.get('id') or s.get('item_id', '')
                            if sid != winner_id:
                                it['segundo_precio'] = float(s.get('price') or 0)
                                break

                    # diferencia_pct: referencia según si ganamos o perdemos
                    if it['we_win']:
                        if it['segundo_precio'] and it['segundo_precio'] > 0:
                            it['diferencia_pct'] = round((it['precio'] - it['segundo_precio']) / it['segundo_precio'] * 100, 1)
                        # si no hay 2do competidor, no hay diferencia relevante
                    elif bb_price > 0:
                        it['diferencia_pct'] = round((it['precio'] - bb_price) / bb_price * 100, 1)

                    # Precio ideal:
                    # - Perdiendo → buy_box_price - 1
                    # - Ganando con 2do competidor → segundo_precio - 1 (solo si es mayor al precio actual)
                    # - Ganando sin 2do → precio actual está bien
                    if not it['we_win'] and bb_price > 0:
                        it['precio_ideal'] = max(1.0, bb_price - 1)
                    elif it['we_win'] and it['segundo_precio'] and it['segundo_precio'] - 1 > it['precio']:
                        it['precio_ideal'] = max(1.0, it['segundo_precio'] - 1)
                    else:
                        it['precio_ideal'] = it['precio']
        except Exception:
            pass
        _time_module.sleep(0.1)

        # Stock del ganador (solo si no somos nosotros)
        winner_id = it.get('buy_box_winner_id')
        if winner_id and not it.get('we_win'):
            try:
                rw = req_lib.get(f'{ML}/items/{winner_id}',
                                 headers=heads,
                                 params={'attributes': 'available_quantity'},
                                 timeout=6)
                if rw.ok:
                    it['winner_stock'] = rw.json().get('available_quantity')
            except Exception:
                pass
            _time_module.sleep(0.1)

    # 4 — Ordenar: ganando primero, luego perdiendo, luego pausadas/sin datos
    def _sort_key(x):
        paused = x.get('status') == 'paused'
        if x['we_win'] is True and not paused:
            return (0, -(x['diferencia_pct'] or 0))   # ganando: mayor margen primero
        if x['we_win'] is False and not paused:
            return (1, x['diferencia_pct'] or 0)       # perdiendo: más caro primero
        if paused:
            return (3, 0)
        return (2, 0)                                   # sin datos
    catalog_items.sort(key=_sort_key)

    resumen = {
        'total':     len(catalog_items),
        'ganando':   sum(1 for i in catalog_items if i['we_win'] is True),
        'perdiendo': sum(1 for i in catalog_items if i['we_win'] is False),
        'sin_datos': sum(1 for i in catalog_items if i['we_win'] is None),
    }

    # 5 — Incorporar rangos de repricing configurados
    repricing_cfg = load_json(os.path.join(CONFIG_DIR, 'repricing.json')) or {}
    items_cfg = repricing_cfg.get('items', {})
    for it in catalog_items:
        rc = items_cfg.get(it['id'], {})
        it['precio_min'] = rc.get('precio_min')
        it['precio_max'] = rc.get('precio_max')
    resumen['configurados'] = sum(1 for i in catalog_items if i.get('precio_min') is not None)

    return render_template('salud.html', alias=alias, items=catalog_items,
                           resumen=resumen, accounts=get_accounts())


@app.route('/api/aplicar-precio/<alias>', methods=['POST'])
def api_aplicar_precio(alias):
    """Actualiza el precio de una publicación directamente via ML API."""
    data    = request.get_json(silent=True) or {}
    item_id = str(data.get('item_id', '')).strip()
    precio  = data.get('precio')

    if not item_id or precio is None:
        return jsonify({'ok': False, 'error': 'item_id y precio requeridos'}), 400
    try:
        precio = float(precio)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'precio inválido'}), 400
    if precio <= 0:
        return jsonify({'ok': False, 'error': 'precio debe ser mayor a 0'}), 400

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    heads_json = {**heads, 'Content-Type': 'application/json'}
    r = req_lib.put(
        f'https://api.mercadolibre.com/items/{item_id}',
        headers=heads_json,
        json={'price': precio},
        timeout=10,
    )
    if r.ok:
        nuevo = r.json().get('price', precio)
        _audit('CAMBIAR_PRECIO', alias=alias, item_id=item_id, precio_nuevo=nuevo)
        return jsonify({'ok': True, 'precio_nuevo': nuevo})
    return jsonify({'ok': False, 'error': r.text[:200]}), r.status_code


@app.route('/api/salud-config/<alias>', methods=['POST', 'DELETE'])
def api_salud_config(alias):
    """Guarda, actualiza o elimina precio_min / precio_max de una publicación de catálogo."""
    data    = request.get_json(silent=True) or {}
    item_id = str(data.get('item_id', '')).strip()
    if not item_id:
        return jsonify({'ok': False, 'error': 'item_id requerido'}), 400

    cfg_path = os.path.join(CONFIG_DIR, 'repricing.json')
    cfg = load_json(cfg_path) or {'items': {}}
    cfg.setdefault('items', {})

    # DELETE — eliminar rango
    if request.method == 'DELETE' or data.get('delete'):
        cfg['items'].pop(item_id, None)
        save_json(cfg_path, cfg)
        return jsonify({'ok': True})

    titulo    = str(data.get('titulo', ''))[:70]
    precio_min = data.get('precio_min')
    precio_max = data.get('precio_max')

    if precio_min is None or precio_max is None:
        return jsonify({'ok': False, 'error': 'precio_min y precio_max requeridos'}), 400
    try:
        precio_min = float(precio_min)
        precio_max = float(precio_max)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'precios inválidos'}), 400
    if precio_min <= 0 or precio_max <= 0 or precio_min >= precio_max:
        return jsonify({'ok': False, 'error': 'precio_min debe ser menor que precio_max y mayor a 0'}), 400

    cfg_path = os.path.join(CONFIG_DIR, 'repricing.json')
    cfg = load_json(cfg_path) or {'items': {}}
    cfg.setdefault('items', {})
    existing = cfg['items'].get(item_id, {})
    existing.update({
        'titulo':      titulo,
        'precio_min':  round(precio_min, 2),
        'precio_max':  round(precio_max, 2),
        'alias':       alias,
    })
    cfg['items'][item_id] = existing
    save_json(cfg_path, cfg)
    return jsonify({'ok': True})


@app.route('/api/salud-repricing/<alias>', methods=['POST'])
def api_salud_repricing(alias):
    """Aplica repricing automático a los ítems enviados (ya calculados en el frontend)."""
    data    = request.get_json(silent=True) or {}
    cambios = data.get('cambios', [])   # [{item_id, precio_nuevo}]

    if not cambios:
        return jsonify({'ok': True, 'aplicados': 0, 'errores': []})

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    heads_json = {**heads, 'Content-Type': 'application/json'}
    aplicados  = 0
    errores    = []

    for c in cambios:
        item_id    = str(c.get('item_id', '')).strip()
        precio_nuevo = c.get('precio_nuevo')
        if not item_id or precio_nuevo is None:
            continue
        try:
            precio_nuevo = float(precio_nuevo)
            r = req_lib.put(
                f'https://api.mercadolibre.com/items/{item_id}',
                headers=heads_json,
                json={'price': precio_nuevo},
                timeout=10,
            )
            if r.ok:
                aplicados += 1
                _audit('REPRICING_AUTO', alias=alias, item_id=item_id, precio_nuevo=precio_nuevo)
            else:
                errores.append({'item_id': item_id, 'error': r.text[:100]})
        except Exception as ex:
            errores.append({'item_id': item_id, 'error': str(ex)})
        _time_module.sleep(0.15)

    return jsonify({'ok': True, 'aplicados': aplicados, 'errores': errores})


@app.route('/api/costos-items/<alias>')
def api_costos_items(alias):
    """Items activos con sus costos actuales para el modal de edición."""
    try:
        costos_data = load_json(os.path.join(CONFIG_DIR, 'costos.json')) or {}
        stock_data  = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json'))

        items = []
        fees = get_fee_rates()  # lee de config/fees.json (sin refrescar)
        if stock_data:
            for it in stock_data.get('items', []):
                item_id = it.get('id', '')
                ce = costos_data.get(item_id, {})
                fee_rate = it.get('fee_rate') or get_rate(it.get('listing_type', ''), fees)
                items.append({
                    'id':        item_id,
                    'titulo':    it.get('titulo', ''),
                    'precio':    it.get('precio', 0),
                    'stock':     it.get('stock', 0),
                    'costo':     ce.get('costo') if ce else None,
                    'updated':   ce.get('updated') if ce else None,
                    'fee_rate':  fee_rate,
                    'fee_source': it.get('fee_source', 'estimado'),
                })
        else:
            # Sin stock JSON: leer directo de ML
            from core.account_manager import AccountManager
            mgr = AccountManager()
            client = mgr.get_client(alias)
            client._ensure_token()
            token = client.account.access_token
            heads = {'Authorization': f'Bearer {token}'}
            user_data = req_lib.get('https://api.mercadolibre.com/users/me',
                                    headers=heads, timeout=10).json()
            user_id = user_data.get('id')
            offset = 0
            while True:
                r = req_lib.get(f'https://api.mercadolibre.com/users/{user_id}/items/search',
                                headers=heads,
                                params={'status': 'active', 'limit': 50, 'offset': offset},
                                timeout=10)
                if not r.ok:
                    break
                data = r.json()
                ids = data.get('results', [])
                if not ids:
                    break
                ids_str = ','.join(ids[:20])
                det = req_lib.get('https://api.mercadolibre.com/items',
                                  headers=heads,
                                  params={'ids': ids_str, 'attributes': 'id,title,price,available_quantity'},
                                  timeout=10)
                if det.ok:
                    for d in det.json():
                        body = d.get('body', {})
                        item_id = body.get('id', '')
                        ce = costos_data.get(item_id, {})
                        items.append({
                            'id': item_id,
                            'titulo': body.get('title', '')[:60],
                            'precio': float(body.get('price', 0)),
                            'stock': body.get('available_quantity', 0),
                            'costo': ce.get('costo') if ce else None,
                            'updated': ce.get('updated') if ce else None,
                        })
                offset += len(ids)
                total = data.get('paging', {}).get('total', 0)
                if offset >= total or offset >= 200:
                    break
                _time_module.sleep(0.1)

        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/costos-save', methods=['POST'])
def api_costos_save():
    """Guarda costos de productos desde la web."""
    try:
        payload = request.get_json()
        alias   = payload.get('alias', '')
        costos_in = payload.get('costos', [])  # [{id, titulo, precio, costo}]

        costos_data = load_json(os.path.join(CONFIG_DIR, 'costos.json')) or {}
        today = datetime.now().strftime('%Y-%m-%d')
        saved = 0
        deleted = 0

        for entry in costos_in:
            item_id = entry['id']
            costo   = entry.get('costo')
            if costo is not None and float(costo) > 0:
                costos_data[item_id] = {
                    'alias':   alias,
                    'titulo':  entry.get('titulo', ''),
                    'costo':   float(costo),
                    'updated': today,
                }
                saved += 1
            elif costo == 0 or costo is None:
                # Si se envía 0 o vacío, borrar el costo
                if item_id in costos_data:
                    del costos_data[item_id]
                    deleted += 1

        save_json(os.path.join(CONFIG_DIR, 'costos.json'), costos_data)

        return jsonify({'ok': True, 'saved': saved, 'deleted': deleted})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/item-ai-rec/<alias>/<item_id>', methods=['POST'])
def api_item_ai_recommendation(alias, item_id):
    """
    Analiza los datos de diagnóstico de una publicación con Claude y devuelve
    una recomendación concreta: qué hacer, cómo mejorarlo, o si conviene eliminarlo.
    Responde en streaming para mostrar el texto progresivamente.
    """
    try:
        payload = request.get_json() or {}
        diag = payload.get('diagnostico', {})

        # Construir prompt con todos los datos del diagnóstico
        titulo      = diag.get('titulo', item_id)
        health      = diag.get('health_score')
        pics        = diag.get('pic_count', 0)
        free_ship   = diag.get('free_shipping', False)
        has_video   = diag.get('has_video', False)
        desc_len    = diag.get('desc_len', 0)
        empty_attrs = diag.get('empty_attrs_count', 0)
        listing_type = diag.get('listing_type', '')
        rating_avg  = diag.get('rating_avg')
        rating_total = diag.get('rating_total', 0)
        neg_reviews = diag.get('neg_reviews', [])
        unanswered  = diag.get('unanswered_questions', 0)
        warranty    = diag.get('warranty', '')
        ventas_30d  = diag.get('ventas_30d', 0)
        visitas_30d = diag.get('visitas_30d', 0)
        conversion  = diag.get('conversion_pct')
        margen_pct  = diag.get('margen_pct')
        alerta_stock = diag.get('alerta_stock', '')
        alerta_margen = diag.get('alerta_margen', '')
        velocidad   = diag.get('velocidad', 0)
        pos_current = diag.get('pos_current')
        pos_trend   = diag.get('pos_trend')
        variants_total = diag.get('variants_total', 0)
        variants_dead  = diag.get('variants_dead', 0)
        visits_w1   = diag.get('visits_w1', 0)
        visits_w2   = diag.get('visits_w2', 0)
        visits_delta_pct = diag.get('visits_delta_pct')

        conv_str = f"{conversion:.1f}%" if conversion is not None else "sin dato"
        margen_str = f"{margen_pct*100:.1f}%" if margen_pct is not None else "sin dato"
        health_str = f"{round(health*100)}%" if health is not None else "sin dato"
        rating_str = f"{rating_avg:.1f}★ ({rating_total} reseñas)" if rating_avg else "sin reseñas"
        neg_str = ""
        if neg_reviews:
            neg_str = "\n".join(f"  - {r['rate']}★: {r['title']}" for r in neg_reviews[:3])

        prompt = f"""Sos un experto en MercadoLibre Argentina con 10 años de experiencia optimizando cuentas de vendedores profesionales.

Analizá esta publicación y dá una recomendación clara y accionable. Si la publicación está dañando la cuenta, decilo sin rodeos. Si vale la pena salvarla, explicá exactamente cómo.

## Publicación: {titulo}
ID: {item_id}

## Métricas de rendimiento
- Visitas (30 días): {visitas_30d}
- Ventas (30 días): {ventas_30d}
- Conversión: {conv_str}
- Velocidad de ventas: {velocidad:.2f} unidades/día
- Margen: {margen_str}
- Alerta stock: {alerta_stock or 'sin alerta'}
- Alerta margen: {alerta_margen or 'sin alerta'}

## Posición en búsqueda ML
- Posición actual: {"#" + str(pos_current) if pos_current else "sin datos"}
- Tendencia: {"subió " + str(abs(pos_trend)) + " lugares" if pos_trend and pos_trend < 0 else ("bajó " + str(pos_trend) + " lugares" if pos_trend and pos_trend > 0 else "estable")}

## Tendencia de visitas
- Últimos 7 días: {visits_w1} visitas
- 7 días anteriores: {visits_w2} visitas
- Variación: {(f"+{visits_delta_pct}%" if visits_delta_pct and visits_delta_pct > 0 else f"{visits_delta_pct}%") if visits_delta_pct is not None else "sin dato"}

## Variantes
- Total variantes: {variants_total}
- Variantes sin stock: {variants_dead}

## Calidad de la publicación (evaluación ML)
- Score de calidad ML: {health_str}
- Fotos: {pics}
- Envío gratis: {'sí' if free_ship else 'no'}
- Video: {'sí' if has_video else 'no'}
- Descripción: {desc_len} caracteres
- Atributos vacíos en ficha técnica: {empty_attrs}
- Tipo de publicación: {listing_type}
- Garantía: {warranty or 'no declarada'}

## Reseñas
- Rating: {rating_str}
- Reseñas negativas recientes:
{neg_str if neg_str else '  (ninguna)'}

## Preguntas
- Sin responder: {unanswered}

---

Respondé en español con este formato exacto:

**VEREDICTO:** [SALVAR / OPTIMIZAR / PAUSAR / ELIMINAR]

**POR QUÉ:**
[2-3 oraciones explicando el diagnóstico principal. Sé directo.]

**ACCIONES CONCRETAS:**
1. [acción específica]
2. [acción específica]
3. [acción específica si aplica]

**IMPACTO ESPERADO:**
[Una oración sobre qué mejora si se aplica el plan, o qué pasa si no se actúa.]

No uses frases genéricas. Basate estrictamente en los números de esta publicación."""

        def generate():
            ai = anthropic.Anthropic()
            with ai.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    yield text

        return Response(stream_with_context(generate()), mimetype='text/plain; charset=utf-8')

    except Exception as e:
        return Response(f"Error: {str(e)}", mimetype='text/plain', status=500)


@app.route('/api/item-health/<alias>/<item_id>')
def api_item_health(alias, item_id):
    """
    Devuelve calidad de publicación + métricas de experiencia de compra evaluadas por ML.
    - health score: campo 'health' del item (0-1)
    - tags de calidad: good_quality_thumbnail, poor_quality_thumbnail, etc.
    - atributos vacíos: cuántos atributos requeridos están sin completar
    - métricas de vendedor: reclamos, demoras, cancelaciones (60 días)
    """
    try:
        from core.account_manager import AccountManager
        mgr = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token = client.account.access_token
        heads = {'Authorization': f'Bearer {token}'}
        user_id = client.account.user_id

        import concurrent.futures
        from datetime import datetime, timedelta

        # Fetch item con todos los campos de calidad (incluye variantes)
        r_item = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}',
            headers=heads,
            params={'attributes': 'id,health,tags,warnings,attributes,pictures,shipping,'
                                  'sold_quantity,status,video_id,listing_type_id,'
                                  'catalog_listing,warranty,descriptions,variations'},
            timeout=8)
        if not r_item.ok:
            return jsonify({'ok': False, 'error': f'HTTP {r_item.status_code}'})

        item = r_item.json()

        # ── Datos base del item ───────────────────────────────
        health_score  = item.get('health')
        tags          = item.get('tags') or []
        attrs         = item.get('attributes') or []
        pics          = item.get('pictures') or []
        shipping      = item.get('shipping') or {}
        free_shipping = shipping.get('free_shipping', False)
        has_video     = bool(item.get('video_id'))
        listing_type  = item.get('listing_type_id', '')
        warranty      = item.get('warranty') or ''

        quality_tags = {
            'good_quality_thumbnail': ('ok',      'Foto principal de buena calidad'),
            'poor_quality_thumbnail': ('problem', 'Foto principal de baja calidad — ML penaliza ranking'),
            'good_quality_picture':   ('ok',      'Fotos de buena calidad'),
            'poor_quality_picture':   ('problem', 'Fotos de baja calidad'),
            'incomplete_technical_specs': ('problem', 'Ficha técnica incompleta'),
            'missing_description':    ('problem', 'Sin descripción del producto'),
        }
        tag_results = [
            {'tag': t, 'status': s, 'label': l}
            for t in tags if t in quality_tags
            for s, l in [quality_tags[t]]
        ]
        empty_attrs = [a for a in attrs if not a.get('value_id') and not a.get('value_name')]

        # ── Variantes ─────────────────────────────────────────
        variations = item.get('variations') or []
        variants_data = []
        for v in variations:
            combis = [c.get('value_name', '') for c in (v.get('attribute_combinations') or [])]
            stock_v = v.get('available_quantity', 0)
            variants_data.append({
                'name':  ' / '.join(filter(None, combis)) or f"Variante {v.get('id','')}",
                'stock': stock_v,
                'dead':  stock_v == 0,
            })
        dead_variants   = [v for v in variants_data if v['dead']]
        active_variants = [v for v in variants_data if not v['dead']]

        # ── Posición en búsqueda (del snapshot local) ─────────
        pos_data    = {}
        pos_history = []
        pos_current = None
        pos_trend   = None
        try:
            pos_file = os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')
            if os.path.exists(pos_file):
                with open(pos_file, encoding='utf-8') as pf:
                    all_pos = json.load(pf)
                if item_id in all_pos:
                    pos_data = all_pos[item_id]
                    history  = pos_data.get('history', {})
                    # Ordenar por fecha y tomar últimas 7 entradas
                    sorted_dates = sorted(history.keys())[-7:]
                    pos_history  = [
                        {'fecha': d, 'pos': history[d]}
                        for d in sorted_dates
                        if history[d] != 999  # 999 = no encontrado
                    ]
                    # Posición actual = última entrada válida
                    valid = [h for h in pos_history if h['pos'] < 999]
                    if valid:
                        pos_current = valid[-1]['pos']
                        if len(valid) >= 2:
                            delta = valid[-1]['pos'] - valid[-2]['pos']
                            pos_trend = delta  # negativo = mejoró (subió)
        except Exception:
            pass

        # ── Llamadas paralelas ────────────────────────────────
        hoy   = datetime.now()
        d7    = (hoy - timedelta(days=7)).strftime('%Y-%m-%d')
        d14   = (hoy - timedelta(days=14)).strftime('%Y-%m-%d')
        hoy_s = hoy.strftime('%Y-%m-%d')

        def get_reviews():
            r = req_lib.get(f'https://api.mercadolibre.com/reviews/item/{item_id}',
                            headers=heads, params={'limit': 5}, timeout=6)
            return r.json() if r.ok else {}

        def get_questions():
            r = req_lib.get('https://api.mercadolibre.com/questions/search',
                            headers=heads,
                            params={'item': item_id, 'status': 'UNANSWERED', 'limit': 1},
                            timeout=6)
            return r.json() if r.ok else {}

        def get_description():
            r = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}/description',
                            headers=heads, timeout=6)
            return r.json() if r.ok else {}

        def get_visits_14d():
            # time_window devuelve visitas diarias para los últimos N días
            r = req_lib.get(
                f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
                headers=heads, params={'last': 14, 'unit': 'day'}, timeout=6)
            return r.json() if r.ok else {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            f_rev  = ex.submit(get_reviews)
            f_q    = ex.submit(get_questions)
            f_desc = ex.submit(get_description)
            f_v14  = ex.submit(get_visits_14d)
            rev_data  = f_rev.result()
            q_data    = f_q.result()
            desc_data = f_desc.result()
            v14_data  = f_v14.result()

        # ── Reviews ───────────────────────────────────────────
        rating_avg    = rev_data.get('rating_average')
        rating_total  = (rev_data.get('paging') or {}).get('total', 0)
        rating_levels = rev_data.get('rating_levels') or {}
        neg_reviews   = [
            {'rate': r.get('rate'), 'title': (r.get('title') or '')[:80]}
            for r in (rev_data.get('reviews') or []) if r.get('rate', 5) <= 2
        ]

        # ── Preguntas sin responder ───────────────────────────
        unanswered_questions = q_data.get('total', 0)

        # ── Descripción ───────────────────────────────────────
        desc_text = desc_data.get('plain_text') or desc_data.get('text') or ''
        desc_len  = len(desc_text.strip())

        # ── Tendencia de visitas: semana 1 vs semana 2 ────────
        # time_window devuelve resultados diarios ordenados cronológicamente
        daily = v14_data.get('results') or []
        total_14d = int(v14_data.get('total_visits') or 0)
        # Los primeros 7 días = semana anterior, los últimos 7 = esta semana
        if len(daily) >= 7:
            visits_w1 = sum(int(d.get('total', 0)) for d in daily[-7:])
            visits_w2 = sum(int(d.get('total', 0)) for d in daily[:7])
        else:
            # Fallback: total 14d, mitad para cada semana (impreciso pero evita crashear)
            visits_w1 = total_14d // 2
            visits_w2 = total_14d - visits_w1
        visits_delta = visits_w1 - visits_w2
        visits_delta_pct = round((visits_delta / visits_w2 * 100), 1) if visits_w2 > 0 else None

        return jsonify({
            'ok': True,
            'item_id':           item_id,
            'health_score':      health_score,
            'tag_results':       tag_results,
            'empty_attrs_count': len(empty_attrs),
            'pic_count':         len(pics),
            'free_shipping':     free_shipping,
            'has_video':         has_video,
            'listing_type':      listing_type,
            'warranty':          warranty,
            'rating_avg':        rating_avg,
            'rating_total':      rating_total,
            'rating_levels':     rating_levels,
            'neg_reviews':       neg_reviews,
            'unanswered_questions': unanswered_questions,
            'desc_len':          desc_len,
            # Variantes
            'variants_total':    len(variations),
            'variants_dead':     len(dead_variants),
            'variants_active':   len(active_variants),
            'dead_variants':     dead_variants[:8],
            'active_variants':   active_variants[:5],
            # Posición en búsqueda
            'pos_current':       pos_current,
            'pos_trend':         pos_trend,
            'pos_history':       pos_history,
            # Tendencia de visitas
            'visits_w1':         visits_w1,
            'visits_w2':         visits_w2,
            'visits_delta':      visits_delta,
            'visits_delta_pct':  visits_delta_pct,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/visitas/<alias>')
def api_visitas(alias):
    """Trae visitas en vivo (30d) para todos los items del stock JSON."""
    try:
        stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json'))
        if not stock_data:
            return jsonify({'ok': False, 'error': 'Sin datos de stock. Corré el análisis primero.'})

        from core.account_manager import AccountManager
        mgr = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token = client.account.access_token
        heads = {'Authorization': f'Bearer {token}'}

        items = stock_data.get('items', [])
        results = []
        for it in items:
            item_id = it.get('id', '')
            ventas_30d = it.get('ventas_30d') or round((it.get('velocidad') or 0) * 30)
            try:
                r = req_lib.get(
                    f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
                    headers=heads,
                    params={'last': 30, 'unit': 'day'},
                    timeout=8,
                )
                visitas = r.json().get('total_visits', 0) if r.ok else 0
            except Exception:
                visitas = 0
            conversion = round(ventas_30d / visitas * 100, 2) if visitas > 0 else None
            results.append({
                'id':             item_id,
                'visitas_30d':    visitas,
                'ventas_30d':     ventas_30d,
                'conversion_pct': conversion,
            })
            _time_module.sleep(0.1)

        return jsonify({'ok': True, 'items': results})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ── Refresh stock en background ───────────────────────────────────────────────

import threading as _threading

_refresh_status: dict = {}   # alias → {'running': bool, 'finished': str, 'error': str}

def _run_stock_background(alias: str):
    """Corre el análisis de stock en un thread separado para no bloquear la web."""
    try:
        from core.account_manager import AccountManager
        from modules.stock_rentabilidad import run as run_stock
        from io import StringIO
        import sys as _sys

        mgr    = AccountManager()
        client = mgr.get_client(alias)

        # Silenciar output de rich (va a la terminal del servidor, no al browser)
        _refresh_status[alias] = {'running': True, 'finished': None, 'error': None}
        run_stock(client, alias, mostrar_todos=True)
        from datetime import datetime as _dt
        _refresh_status[alias] = {'running': False, 'finished': _dt.now().strftime('%H:%M'), 'error': None}
    except Exception as e:
        _refresh_status[alias] = {'running': False, 'finished': None, 'error': str(e)}


@app.route('/api/refresh-stock/<alias>', methods=['POST'])
def api_refresh_stock(alias):
    """Dispara el análisis de stock en background. Retorna inmediatamente."""
    status = _refresh_status.get(alias, {})
    if status.get('running'):
        return jsonify({'ok': False, 'error': 'El análisis ya está corriendo, esperá que termine.'})
    t = _threading.Thread(target=_run_stock_background, args=(alias,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'message': 'Análisis iniciado. Puede tardar 2-3 minutos.'})


@app.route('/api/refresh-stock-status/<alias>')
def api_refresh_stock_status(alias):
    """Devuelve el estado del análisis en background."""
    status = _refresh_status.get(alias, {})
    return jsonify({
        'running':  status.get('running', False),
        'finished': status.get('finished'),
        'error':    status.get('error'),
    })


@app.route('/posiciones/<alias>')
def posiciones(alias):
    from datetime import datetime as _dt, timedelta as _td
    pos_data   = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
    stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    opt_data   = load_json(os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')) or {}

    # Índice rápido de ventas/visitas desde stock
    stock_idx = {i['id']: i for i in stock_data.get('items', []) if i.get('id')}

    # Índice de optimizaciones por item_id → lista ordenada por fecha
    opt_idx = {}
    for opt in opt_data.get('optimizaciones', []):
        iid = opt.get('item_id', '')
        if iid:
            opt_idx.setdefault(iid, []).append(opt)
    for iid in opt_idx:
        opt_idx[iid].sort(key=lambda x: x.get('fecha', ''), reverse=True)

    dates = []
    items = []
    all_search_blocked = False

    if pos_data:
        all_dates = set()
        for d in pos_data.values():
            all_dates.update(d.get('history', {}).keys())
        dates = sorted(all_dates)[-7:]

        all_999 = all(
            all(v == 999 for v in item_data.get('history', {}).values())
            for item_data in pos_data.values()
        )
        all_search_blocked = all_999

        for item_id, item_data in pos_data.items():
            hist      = item_data.get('history', {})
            positions = [hist.get(d) for d in dates]
            real      = [p for p in positions if p is not None and p != 999]
            latest    = real[-1] if real else 999
            has_pos   = bool(real)
            si = stock_idx.get(item_id, {})

            # ── Datos de evolución post-optimización ─────────────────────
            opt_entries = opt_idx.get(item_id, [])
            opt_evolution = []
            for opt in opt_entries:
                fecha_opt = opt.get('fecha', '')[:10]  # YYYY-MM-DD
                if not fecha_opt:
                    continue
                try:
                    d_opt = _dt.strptime(fecha_opt, '%Y-%m-%d')
                except ValueError:
                    continue

                # Posición baseline: día antes del análisis
                d_antes = (d_opt - _td(days=1)).strftime('%Y-%m-%d')
                pos_antes = hist.get(d_antes) or hist.get(fecha_opt)

                # Posiciones en puntos clave post-optimización
                checkpoints = {}
                for days in [3, 7, 14, 30]:
                    d_check = (d_opt + _td(days=days)).strftime('%Y-%m-%d')
                    v = hist.get(d_check)
                    if v is not None:
                        checkpoints[days] = v

                # Delta total
                last_known = list(checkpoints.values())[-1] if checkpoints else None
                delta = None
                if pos_antes and pos_antes != 999 and last_known and last_known != 999:
                    delta = pos_antes - last_known  # positivo = mejoró

                opt_evolution.append({
                    'fecha':      fecha_opt,
                    'pos_antes':  pos_antes,
                    'checkpoints': checkpoints,
                    'delta':      delta,
                    'titulos':    opt.get('titulos_alt', []),
                })

            # ── Tendencia y veredicto ─────────────────────────────────
            real_positions = [p for p in positions if p is not None and p != 999]
            trend_7d   = None   # positivo = mejoró (bajó el número)
            caida_rapida = False
            if len(real_positions) >= 2:
                trend_7d = real_positions[0] - real_positions[-1]  # primero - último
                caida_rapida = trend_7d < -5  # cayó >5 posiciones

            # Veredicto legible
            if not has_pos:
                veredicto = 'Sin posición rastreable (fuera del top 200)'
                veredicto_tipo = 'sin_dato'
            elif trend_7d is None:
                veredicto = f'Posición actual: {latest}'
                veredicto_tipo = 'estable'
            elif trend_7d >= 10:
                veredicto = f'Subió {trend_7d} posiciones esta semana 🚀'
                veredicto_tipo = 'sube_fuerte'
            elif trend_7d >= 3:
                veredicto = f'Mejorando — subió {trend_7d} lugares'
                veredicto_tipo = 'sube'
            elif trend_7d <= -10:
                veredicto = f'Cayó {abs(trend_7d)} posiciones — atención urgente ⚠️'
                veredicto_tipo = 'cae_fuerte'
            elif trend_7d <= -3:
                veredicto = f'Perdiendo terreno — bajó {abs(trend_7d)} lugares'
                veredicto_tipo = 'cae'
            else:
                veredicto = f'Estable en posición {latest}'
                veredicto_tipo = 'estable'

            # Veredicto post-optimización (si hay resultado reciente)
            opt_veredicto = None
            if opt_evolution:
                last_evo = opt_evolution[0]  # más reciente
                if last_evo.get('delta') is not None:
                    delta_opt = last_evo['delta']
                    dias_opt  = (datetime.now() - _dt.strptime(last_evo['fecha'], '%Y-%m-%d')).days
                    if delta_opt >= 5:
                        opt_veredicto = f'+{delta_opt} posiciones en {dias_opt}d post-IA ✅'
                    elif delta_opt >= 1:
                        opt_veredicto = f'+{delta_opt} posiciones post-IA'
                    elif delta_opt <= -3:
                        opt_veredicto = f'{delta_opt} posiciones post-IA — revisar'
                    else:
                        opt_veredicto = f'Sin cambio significativo post-IA ({dias_opt}d)'

            items.append({
                'id':             item_id,
                'title':          item_data.get('title', item_id)[:70],
                'positions':      positions,
                'latest':         latest,
                'has_pos':        has_pos,
                'ventas_30d':     si.get('ventas_30d', 0),
                'precio':         si.get('precio') or si.get('precio_actual', 0),
                'stock':          si.get('stock') if si.get('stock') is not None else '—',
                'alerta':         si.get('alerta_stock', ''),
                'visitas_30d':    si.get('visitas_30d'),
                'conversion_pct': si.get('conversion_pct'),
                'optimizado':     bool(opt_entries),
                'opt_evolution':  opt_evolution,
                'trend_7d':       trend_7d,
                'caida_rapida':   caida_rapida,
                'veredicto':      veredicto,
                'veredicto_tipo': veredicto_tipo,
                'opt_veredicto':  opt_veredicto,
            })
        items.sort(key=lambda x: (0 if x['has_pos'] else 1, x['latest']))

    # ── Auto-persistir resultado_auto en optimizaciones JSON ─────────────────
    # Construye el mapa item_id → {fecha_opt → delta} desde los items calculados
    _auto_results = {}
    for it in items:
        for evo in it.get('opt_evolution', []):
            if evo.get('delta') is not None:
                _auto_results.setdefault(it['id'], {})[evo['fecha']] = {
                    'delta':      evo['delta'],      # positivo = mejoró
                    'pos_antes':  evo.get('pos_antes'),
                    'veredicto':  ('mejoro'    if evo['delta'] >= 3  else
                                   'empeoro'   if evo['delta'] <= -3 else
                                   'sin_cambio'),
                    'actualizado': datetime.now().strftime('%Y-%m-%d'),
                }

    if _auto_results:
        _opt_path = os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')
        _opt_file = load_json(_opt_path) or {'optimizaciones': []}
        _changed  = False
        for opt_entry in _opt_file.get('optimizaciones', []):
            iid      = opt_entry.get('item_id', '')
            fecha_op = opt_entry.get('fecha', '')[:10]
            if iid in _auto_results and fecha_op in _auto_results[iid]:
                upd = _auto_results[iid][fecha_op]
                # No sobreescribir seguimiento manual con baseline real
                existing = opt_entry.get('resultado_auto', {})
                if existing.get('delta') != upd['delta']:
                    opt_entry['resultado_auto'] = upd
                    _changed = True
        if _changed:
            try:
                save_json(_opt_path, _opt_file)
            except Exception:
                pass

    # Datos completos para Chart.js (últimos 30 días por ítem)
    chart_data = {}
    if pos_data:
        all_d_30 = sorted({
            d for item_data in pos_data.values()
            for d in item_data.get('history', {}).keys()
        })[-30:]
        for item_id, item_data in pos_data.items():
            hist = item_data.get('history', {})
            chart_data[item_id] = {
                'labels':  [d[5:] for d in all_d_30],
                'data':    [hist.get(d) if hist.get(d) not in (None, 999) else None for d in all_d_30],
                'opt_dates': [
                    opt.get('fecha', '')[:10][5:]
                    for opt in opt_idx.get(item_id, [])
                    if opt.get('fecha', '')[:10] in all_d_30
                ],
            }

    # Total de ventas 30d de TODA la cuenta (incluye publicaciones activas
    # aunque no estén en seguimiento de posiciones). Permite mostrar el KPI
    # como "rastreadas / total" y detectar gap de tracking.
    ventas_total_cuenta = sum(
        int(i.get('ventas_30d') or 0) for i in stock_data.get('items', [])
    )

    return render_template('posiciones.html', alias=alias, items=items,
                           dates=[d[5:] for d in dates],
                           all_search_blocked=all_search_blocked,
                           chart_data=chart_data,
                           ventas_total_cuenta=ventas_total_cuenta,
                           accounts=get_accounts())


@app.route('/reputacion/<alias>')
def reputacion(alias):
    from datetime import datetime as _dt, timedelta as _td

    snapshots = load_json(os.path.join(DATA_DIR, f'reputacion_{safe(alias)}.json')) or []

    all_accs  = get_accounts()
    account   = next((a for a in all_accs if a.get('alias') == alias), None)

    rep_live     = {}
    claims_list  = []
    dashboard    = {}
    top_products = []
    shipping_metrics = {}

    if account:
        try:
            token, user_id, heads = _ml_auth(alias)
        except Exception:
            token, user_id, heads = '', '', {}

        # 1 — Reputación en vivo
        if user_id:
            try:
                r = req_lib.get(f'https://api.mercadolibre.com/users/{user_id}/seller_reputation',
                                headers=heads, timeout=8)
                if r.ok:
                    rep_live = r.json()
            except Exception:
                pass

        # 2 — Mediaciones pendientes (órdenes activas con mediación abierta)
        # Solo buscamos las NO canceladas → son reclamos aún sin resolver
        if user_id:
            try:
                date_60d = (_dt.now() - _td(days=60)).strftime('%Y-%m-%dT00:00:00.000-03:00')
                r = req_lib.get('https://api.mercadolibre.com/orders/search',
                                headers=heads,
                                params={'seller': user_id,
                                        'order.date_created.from': date_60d,
                                        'limit': 50, 'sort': 'date_desc'},
                                timeout=12)
                if r.ok:
                    for order in r.json().get('results', []):
                        if order.get('status') == 'cancelled':
                            continue
                        if not order.get('mediations'):
                            continue
                        items   = order.get('order_items', [])
                        title   = items[0].get('item', {}).get('title', '—') if items else '—'
                        item_id = items[0].get('item', {}).get('id', '')     if items else ''
                        claims_list.append({
                            'order_id':     order.get('id', ''),
                            'item_title':   title,
                            'item_id':      item_id,
                            'date_created': (order.get('date_created') or '')[:10],
                        })
            except Exception:
                pass

        # 3 — Órdenes 60 días → dashboard + top 10
        # Todo alineado al período de cálculo de ML (60 días)
        if user_id:
            try:
                now      = _dt.now()
                date_60d = (now - _td(days=60)).strftime('%Y-%m-%dT00:00:00.000-03:00')
                date_30d = (now - _td(days=30)).strftime('%Y-%m-%dT00:00:00.000-03:00')
                date_7d  = (now - _td(days=7)).strftime('%Y-%m-%dT00:00:00.000-03:00')
                date_hoy = now.strftime('%Y-%m-%dT00:00:00.000-03:00')

                def _fetch_all_orders(date_from):
                    """Pagina completamente las órdenes pagadas desde date_from."""
                    orders = []
                    offset = 0
                    while True:
                        r = req_lib.get('https://api.mercadolibre.com/orders/search',
                            params={
                                'seller': user_id,
                                'order.status': 'paid',
                                'order.date_created.from': date_from,
                                'limit': 50, 'offset': offset,
                                'sort': 'date_desc',
                            }, headers=heads, timeout=15)
                        if not r.ok:
                            break
                        data_o  = r.json()
                        results = data_o.get('results', [])
                        orders.extend(results)
                        total   = data_o.get('paging', {}).get('total', 0)
                        offset += len(results)
                        if not results or offset >= total:
                            break
                    return orders

                orders_60d    = _fetch_all_orders(date_60d)
                orders_30d    = _fetch_all_orders(date_30d)
                orders_7d     = _fetch_all_orders(date_7d)
                orders_hoy    = _fetch_all_orders(date_hoy)

                def _sum_orders(order_list):
                    amt = 0.0
                    unt = 0
                    for o in order_list:
                        amt += o.get('total_amount', 0) or 0
                        unt += sum(i.get('quantity', 1) for i in o.get('order_items', []))
                    return amt, unt

                dashboard = {
                    'hoy':    {'amount': 0.0, 'units': 0},
                    'semana': {'amount': 0.0, 'units': 0},
                    'mes':    {'amount': 0.0, 'units': 0},
                    'sesenta':{'amount': 0.0, 'units': 0},
                }
                dashboard['hoy']['amount'],     dashboard['hoy']['units']     = _sum_orders(orders_hoy)
                dashboard['semana']['amount'],  dashboard['semana']['units']  = _sum_orders(orders_7d)
                dashboard['mes']['amount'],     dashboard['mes']['units']     = _sum_orders(orders_30d)
                dashboard['sesenta']['amount'], dashboard['sesenta']['units'] = _sum_orders(orders_60d)

                # Top 10 productos — últimos 60 días (alineado con métricas de reputación)
                items_agg = {}
                for order in orders_60d:
                    for oi in order.get('order_items', []):
                        iid   = oi.get('item', {}).get('id', '')
                        title = oi.get('item', {}).get('title', '')
                        qty   = oi.get('quantity', 1)
                        price = oi.get('unit_price', 0) or 0
                        if iid:
                            if iid not in items_agg:
                                items_agg[iid] = {'id': iid, 'title': title, 'units': 0, 'revenue': 0.0}
                            items_agg[iid]['units']   += qty
                            items_agg[iid]['revenue'] += qty * price

                top_products = sorted(items_agg.values(), key=lambda x: -x['units'])[:10]
            except Exception:
                pass

        # 4 — Métricas de envío / Flex desde reputación
        if rep_live:
            m = rep_live.get('metrics', {})
            shipping_metrics = {
                'delayed': m.get('delayed_handling_time', {}),
                'cancels': m.get('cancellations', {}),
                'claims':  m.get('claims', {}),
            }

    # Totales de stock desde snapshot guardado (data/stock_{alias}.json)
    # Totales de stock con deduplicación por family_id (grupo de AppSeller).
    # Si el snapshot no tiene family_id (generado antes del cambio), lo busca
    # en la API en tiempo real usando los item_ids del snapshot.
    stock_snap      = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    stock_items_raw = stock_snap.get('items', [])

    # ¿El snapshot ya tiene family_id guardado?
    _has_family = any(i.get('family_id') is not None for i in stock_items_raw)

    if not _has_family and stock_items_raw and account:
        # Buscar family_id en la API para todos los items del snapshot
        _item_ids  = [i['id'] for i in stock_items_raw if i.get('id')]
        _fid_map   = {}  # item_id → family_id
        try:
            _, __, _heads = _ml_auth(alias)
        except Exception:
            _heads = {}
        for _b in range(0, len(_item_ids), 20):
            _batch = _item_ids[_b:_b + 20]
            try:
                _r = req_lib.get('https://api.mercadolibre.com/items',
                                 headers=_heads,
                                 params={'ids': ','.join(_batch),
                                         'attributes': 'id,family_id'},
                                 timeout=10)
                if _r.ok:
                    for _entry in _r.json():
                        _body = _entry.get('body', {})
                        if _body.get('id'):
                            _fid_map[_body['id']] = _body.get('family_id')
            except Exception:
                pass
        # Inyectar family_id en cada item para el cálculo
        for _si in stock_items_raw:
            if _si.get('id') in _fid_map:
                _si['family_id'] = _fid_map[_si['id']]

    # Agrupar por family_id: por grupo tomar el MAX stock (1 conteo por grupo)
    _family_groups = {}
    _no_family     = []
    for _si in stock_items_raw:
        _fid = _si.get('family_id')
        if _fid:
            _family_groups.setdefault(_fid, []).append(_si)
        else:
            _no_family.append(_si)

    total_stock_units = 0
    total_stock_value = 0

    for _grp in _family_groups.values():
        # Dentro del grupo, sub-agrupar por stock exacto:
        #   - Mismo stock → comparten pool → contar una sola vez
        #   - Stock distinto → variante real (talle/color) → contar por separado
        _seen_stocks = {}   # stock_qty → precio representativo
        for _si in _grp:
            _s = int(_si.get('stock', 0) or 0)
            if _s not in _seen_stocks:
                _seen_stocks[_s] = float(_si.get('precio', 0) or 0)
        for _s, _p in _seen_stocks.items():
            total_stock_units += _s
            total_stock_value += _s * _p

    for _si in _no_family:
        _s = int(_si.get('stock', 0) or 0)
        total_stock_units += _s
        total_stock_value += _s * float(_si.get('precio', 0) or 0)

    # Nivel de reputación
    LEVEL_MAP = {
        '5_green':       ('MercadoLíder Platinum', 'platinum', '#7c3aed'),
        '4_light_green': ('MercadoLíder Gold',     'gold',     '#d97706'),
        '3_yellow':      ('MercadoLíder',          'leader',   '#2563eb'),
        '2_orange':      ('Bueno',                 'good',     '#16a34a'),
        '1_red':         ('Nuevo',                 'new',      '#64748b'),
    }
    nivel_id    = rep_live.get('level_id', '')
    nivel_info  = LEVEL_MAP.get(nivel_id, (nivel_id or 'Sin datos', 'nd', '#94a3b8'))
    power_seller = rep_live.get('power_seller_status', '')
    transactions = rep_live.get('transactions', {})
    metrics_raw  = rep_live.get('metrics', {})

    return render_template('reputacion.html',
                           alias=alias,
                           snapshots=snapshots,
                           rep_live=rep_live,
                           nivel_label=nivel_info[0],
                           nivel_slug=nivel_info[1],
                           nivel_color=nivel_info[2],
                           power_seller=power_seller,
                           transactions=transactions,
                           metrics_raw=metrics_raw,
                           shipping_metrics=shipping_metrics,
                           claims=claims_list,
                           dashboard=dashboard,
                           top_products=top_products,
                           total_stock_units=total_stock_units,
                           total_stock_value=total_stock_value,
                           accounts=all_accs)


# ── Preguntas ─────────────────────────────────────────────────────────────────

@app.route('/preguntas/<alias>')
def preguntas(alias):
    import time as _t
    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)

    questions = []
    fetch_error = None

    if account:
        try:
            token, user_id, heads = _ml_auth(alias)
        except Exception:
            token, user_id, heads = '', '', {}

        try:
            # Preguntas sin responder
            r = req_lib.get('https://api.mercadolibre.com/questions/search',
                params={'seller_id': user_id, 'status': 'UNANSWERED', 'limit': 50},
                headers=heads, timeout=10)
            raw_qs = r.json().get('questions', []) if r.ok else []

            # Obtener títulos de items únicos
            item_ids = list({q.get('item_id', '') for q in raw_qs if q.get('item_id')})
            item_map = {}
            for iid in item_ids[:20]:
                ir = req_lib.get(f'https://api.mercadolibre.com/items/{iid}',
                    params={'attributes': 'id,title'}, headers=heads, timeout=6)
                if ir.ok:
                    item_map[iid] = ir.json().get('title', iid)
                _t.sleep(0.05)

            for q in raw_qs:
                iid = q.get('item_id', '')
                # Calcular horas pendiente
                _horas_pendiente = None
                try:
                    from datetime import timezone
                    _dc = q.get('date_created', '')
                    if _dc:
                        _dt_q = datetime.fromisoformat(_dc.replace('Z', '+00:00'))
                        _dt_now = datetime.now(timezone.utc)
                        _horas_pendiente = round((_dt_now - _dt_q).total_seconds() / 3600, 1)
                except Exception:
                    pass
                questions.append({
                    'id':              q.get('id'),
                    'text':            q.get('text', '').strip(),
                    'date':            (q.get('date_created', '') or '')[:16].replace('T', ' '),
                    'buyer':           (q.get('from') or {}).get('nickname', 'Comprador'),
                    'item_id':         iid,
                    'item_title':      item_map.get(iid, iid),
                    'horas_pendiente': _horas_pendiente,
                })
        except Exception as e:
            fetch_error = str(e)

    return render_template('preguntas.html', alias=alias, questions=questions,
                           fetch_error=fetch_error, accounts=all_accs)


@app.route('/api/generar-respuestas', methods=['POST'])
def api_generar_respuestas():
    import re as _re
    body        = request.get_json() or {}
    alias       = body.get('alias', '')
    question    = body.get('question', '').strip()
    item_title  = body.get('item_title', '')
    item_desc   = body.get('item_description', '')

    if not question:
        return jsonify({'ok': False, 'error': 'Falta la pregunta'}), 400

    # Obtener descripción si no viene en el body
    if not item_desc and body.get('item_id'):
        all_accs = get_accounts()
        account  = next((a for a in all_accs if a.get('alias') == alias), None)
        if account:
            heads = {'Authorization': f'Bearer {account.get("access_token", "")}'}
            dr = req_lib.get(f'https://api.mercadolibre.com/items/{body["item_id"]}/description',
                             headers=heads, timeout=6)
            if dr.ok:
                item_desc = dr.json().get('plain_text', '')[:400]

    desc_ctx = f"\nDescripción del producto: {item_desc}" if item_desc else ""

    prompt = f"""Sos vendedor experto de MercadoLibre Argentina. Generás 3 respuestas distintas para la misma pregunta de comprador.

Producto: {item_title}{desc_ctx}

Pregunta: {question}

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

    try:
        ai   = anthropic.Anthropic()
        resp = ai.messages.create(
            model='claude-sonnet-4-6', max_tokens=600,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_token_usage('Repricing — Respuesta IA', 'claude-sonnet-4-6', resp.usage.input_tokens, resp.usage.output_tokens)
        text = resp.content[0].text

        opciones = []
        defs = [
            ('A', 'Directa y concisa',       r'OPCIÓN A[^\n]*:\n([\s\S]+?)(?=OPCIÓN B|\Z)'),
            ('B', 'Completa con detalles',    r'OPCIÓN B[^\n]*:\n([\s\S]+?)(?=OPCIÓN C|\Z)'),
            ('C', 'Orientada a la venta',     r'OPCIÓN C[^\n]*:\n([\s\S]+?)(?=\Z)'),
        ]
        for key, label, pat in defs:
            m = _re.search(pat, text, _re.DOTALL)
            opciones.append({
                'key':   key,
                'label': label,
                'text':  m.group(1).strip() if m else '',
            })

        return jsonify({'ok': True, 'opciones': opciones})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/responder-pregunta', methods=['POST'])
def api_responder_pregunta():
    body        = request.get_json() or {}
    alias       = body.get('alias', '')
    question_id = body.get('question_id')
    respuesta   = body.get('respuesta', '').strip()

    if not question_id or not respuesta:
        return jsonify({'ok': False, 'error': 'Datos incompletos'}), 400

    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return jsonify({'ok': False, 'error': 'Cuenta no encontrada'}), 404

    try:
        token, _, heads_base = _ml_auth(alias)
        heads = {**heads_base, 'Content-Type': 'application/json'}
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    try:
        r = req_lib.post('https://api.mercadolibre.com/answers',
            json={'question_id': question_id, 'text': respuesta},
            headers=heads, timeout=10)
        if r.ok:
            return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': r.text}), r.status_code
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/preguntas-count/<alias>')
def api_preguntas_count(alias):
    """Endpoint liviano para el dashboard — solo devuelve el conteo."""
    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return jsonify({'count': 0})
    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception:
        return jsonify({'count': 0})
    try:
        r = req_lib.get('https://api.mercadolibre.com/questions/search',
            params={'seller_id': user_id, 'status': 'UNANSWERED', 'limit': 1},
            headers=heads, timeout=6)
        count = r.json().get('total', 0) if r.ok else 0
        return jsonify({'count': count})
    except Exception:
        return jsonify({'count': 0})


@app.route('/funnel/<alias>')
def funnel(alias):
    """Página de funnel de conversión por publicación."""
    return render_template('funnel.html', alias=alias, accounts=get_accounts())


@app.route('/api/mis-preguntas-analisis/<alias>')
def api_mis_preguntas_analisis(alias):
    """
    Obtiene todas las preguntas respondidas recibidas por el vendedor,
    las agrupa por publicación y ejecuta un análisis Claude (haiku) por ítem
    para detectar qué falta en la descripción/ficha.
    """
    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return jsonify({'ok': False, 'error': 'cuenta no encontrada'}), 404

    try:
        token, uid, _ = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401
    if not uid:
        return jsonify({'ok': False, 'error': 'user_id no encontrado'}), 400

    ML    = 'https://api.mercadolibre.com'
    heads = {'Authorization': f'Bearer {token}'}

    # 1. Traer preguntas respondidas (máx 200)
    preguntas_raw = []
    try:
        params = {'seller_id': uid, 'status': 'ANSWERED', 'limit': 50}
        while len(preguntas_raw) < 200:
            r = req_lib.get(f'{ML}/my/received_questions/search', headers=heads, params=params, timeout=10)
            if not r.ok:
                break
            data = r.json()
            batch = data.get('questions', [])
            if not batch:
                break
            preguntas_raw.extend(batch)
            total = data.get('paging', {}).get('total', 0)
            if len(preguntas_raw) >= total:
                break
            params['offset'] = len(preguntas_raw)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

    if not preguntas_raw:
        return jsonify({'ok': True, 'items': [], 'total_preguntas': 0})

    # 2. Agrupar por item_id
    by_item = {}
    for q in preguntas_raw:
        iid = q.get('item_id', '')
        if not iid:
            continue
        by_item.setdefault(iid, []).append({
            'q': q.get('text', ''),
            'a': (q.get('answer') or {}).get('text', ''),
        })

    # 3. Para cada ítem con ≥3 preguntas, traer título y analizar con Claude Haiku
    results = []
    # Ordenar por cantidad de preguntas (más preguntas = más señal)
    items_sorted = sorted(by_item.items(), key=lambda x: len(x[1]), reverse=True)[:15]

    for item_id, qas in items_sorted:
        titulo = item_id
        try:
            ir = req_lib.get(f'{ML}/items/{item_id}', headers=heads,
                             params={'attributes': 'id,title'}, timeout=6)
            if ir.ok:
                titulo = ir.json().get('title', item_id)
        except Exception:
            pass

        # Armar prompt con las preguntas reales
        qa_text = '\n'.join(
            f'P: {qa["q"]}\nR: {qa["a"][:300]}' if qa.get('a') else f'P: {qa["q"]}'
            for qa in qas[:20]
        )
        prompt = f"""Sos un experto en optimización de publicaciones de MercadoLibre Argentina.
Analizás las preguntas reales que los compradores le hicieron a este vendedor sobre esta publicación.

PUBLICACIÓN: {titulo}
TOTAL DE PREGUNTAS: {len(qas)}

PREGUNTAS DE COMPRADORES:
{qa_text}

Basándote SOLO en estas preguntas reales, respondé con un JSON exactamente así (sin texto adicional):
{{
  "patron_principal": "la duda más repetida en una frase corta",
  "falta_en_descripcion": ["dato 1 que falta y genera muchas preguntas", "dato 2", "dato 3"],
  "falta_en_ficha": ["atributo 1 que deberían completar", "atributo 2"],
  "keywords_long_tail": ["frase exacta que usan los compradores 1", "frase 2", "frase 3"],
  "accion_prioritaria": "qué agregar primero a la publicación para reducir preguntas"
}}"""

        try:
            ai  = anthropic.Anthropic()
            r_ai = ai.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=500,
                messages=[{'role': 'user', 'content': prompt}]
            )
            _log_token_usage('Mis Preguntas — Análisis IA', 'claude-haiku-4-5-20251001',
                             r_ai.usage.input_tokens, r_ai.usage.output_tokens)
            raw_json = r_ai.content[0].text.strip()
            raw_json = re.sub(r'^```[a-z]*\n?', '', raw_json)
            raw_json = re.sub(r'\n?```$', '', raw_json).strip()
            analisis = json.loads(raw_json)
        except Exception:
            analisis = {}

        results.append({
            'item_id':   item_id,
            'titulo':    titulo,
            'n_preguntas': len(qas),
            'analisis':  analisis,
            'preguntas_muestra': [qa['q'] for qa in qas[:5]],
        })

    return jsonify({
        'ok':             True,
        'total_preguntas': len(preguntas_raw),
        'items':          results,
    })


@app.route('/api/funnel/<alias>')
def api_funnel(alias):
    """Funnel visitas → preguntas → ventas por publicación (últimos 30 días)."""
    from datetime import datetime as _dt, timedelta as _td

    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return jsonify({'ok': False, 'error': 'cuenta no encontrada'}), 404

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401
    now     = _dt.now()
    ML      = 'https://api.mercadolibre.com'

    # ── 1. Obtener todas las publicaciones activas ─────────────────────────────
    all_ids = []
    offset  = 0
    while len(all_ids) < 200:
        r = req_lib.get(f'{ML}/users/{user_id}/items/search', headers=heads,
                        params={'status': 'active', 'limit': 100, 'offset': offset}, timeout=12)
        if not r.ok:
            break
        batch = r.json().get('results', [])
        all_ids.extend(batch)
        total  = r.json().get('paging', {}).get('total', 0)
        offset += len(batch)
        if not batch or offset >= total:
            break

    if not all_ids:
        return jsonify({'ok': True, 'items': []})

    # ── 2. Datos base: título, precio, visitas de los últimos 30d ──────────────
    items_map = {}
    stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    stock_map  = {s.get('id',''): s for s in stock_data.get('items', [])}

    for b in range(0, min(len(all_ids), 100), 20):
        batch = all_ids[b:b+20]
        try:
            r = req_lib.get(f'{ML}/items', headers=heads,
                            params={'ids': ','.join(batch),
                                    'attributes': 'id,title,price,sold_quantity,listing_type_id'},
                            timeout=12)
            if r.ok:
                for e in r.json():
                    if e.get('code') == 200:
                        bd = e.get('body', {})
                        iid = bd.get('id', '')
                        st  = stock_map.get(iid, {})
                        precio_actual = float(bd.get('price', 0) or 0)
                        sq = int(bd.get('sold_quantity', 0) or 0)
                        items_map[iid] = {
                            'item_id':   iid,
                            'titulo':    bd.get('title', '')[:70],
                            'precio':    precio_actual,
                            'vendidos_total': sq,
                            'ventas_30d': int(st.get('ventas_30d') or round((st.get('velocidad') or 0) * 30)),
                            'visitas_30d': st.get('visitas_30d'),
                            'preguntas_30d': None,
                            'conversion_pct': st.get('conversion_pct'),
                        }
        except Exception:
            pass
        _time_module.sleep(0.1)

    # ── 3. Visitas en vivo para los que no tienen o tienen 0 en snapshot ─────────
    # 0 en snapshot puede significar "estaba sin stock y pausado" — verificar en vivo
    sin_visitas = [iid for iid, d in items_map.items() if not d['visitas_30d']]
    for iid in sin_visitas[:50]:
        try:
            r = req_lib.get(f'{ML}/items/{iid}/visits/time_window',
                            headers=heads, params={'last': 30, 'unit': 'day'}, timeout=6)
            if r.ok:
                items_map[iid]['visitas_30d'] = r.json().get('total_visits', 0)
        except Exception:
            items_map[iid]['visitas_30d'] = 0
        _time_module.sleep(0.08)

    # ── 3b. Ventas en vivo para ítems con visitas pero ventas=0 en snapshot ─────
    # Snapshot puede estar desactualizado si el ítem estuvo sin stock cuando se grabó
    date_from_v = (now - _td(days=30)).strftime('%Y-%m-%dT00:00:00.000-03:00')
    sin_ventas_con_visitas = [
        iid for iid, d in items_map.items()
        if d['ventas_30d'] == 0 and (d['visitas_30d'] or 0) > 0
    ]
    for iid in sin_ventas_con_visitas[:40]:
        try:
            r = req_lib.get(f'{ML}/orders/search', headers=heads,
                            params={'seller': user_id, 'item': iid,
                                    'order.status': 'paid',
                                    'order.date_created.from': date_from_v,
                                    'limit': 50},
                            timeout=8)
            if r.ok:
                orders = r.json().get('results', [])
                units = sum(
                    sum(oi.get('quantity', 0) for oi in o.get('order_items', [])
                        if oi.get('item', {}).get('id') == iid)
                    for o in orders
                )
                if units > 0:
                    items_map[iid]['ventas_30d'] = units
        except Exception:
            pass
        _time_module.sleep(0.1)

    # ── 4. Preguntas recibidas en los últimos 30 días por item ─────────────────
    try:
        date_from = (now - _td(days=30)).strftime('%Y-%m-%dT00:00:00.000-03:00')
        offset_q  = 0
        while True:
            r = req_lib.get(f'{ML}/questions/search', headers=heads,
                            params={'seller_id': user_id,
                                    'date_created_from': date_from,
                                    'limit': 50, 'offset': offset_q},
                            timeout=10)
            if not r.ok:
                break
            batch_q = r.json().get('questions', [])
            for q in batch_q:
                iid = q.get('item_id', '')
                if iid in items_map:
                    items_map[iid]['preguntas_30d'] = (items_map[iid]['preguntas_30d'] or 0) + 1
            total_q  = r.json().get('total', {}).get('value', 0) if isinstance(r.json().get('total'), dict) else r.json().get('total', 0)
            offset_q += len(batch_q)
            if not batch_q or offset_q >= total_q or offset_q >= 500:
                break
    except Exception:
        pass

    # ── 5. Calcular métricas del funnel y diagnóstico ──────────────────────────
    result = []
    for item in items_map.values():
        vis   = item['visitas_30d']  or 0
        pregs = item['preguntas_30d'] or 0
        vtas  = item['ventas_30d']   or 0

        # Tasas de conversión
        vis_to_preg = round(pregs / vis * 100, 1)  if vis  > 0 else 0
        vis_to_vta  = round(vtas  / vis * 100, 1)  if vis  > 0 else 0
        preg_to_vta = round(vtas  / pregs * 100, 1) if pregs > 0 else 0

        # Diagnóstico automático
        if vis == 0:
            diagnostico = 'sin_trafico'
            accion = 'Sin visitas. Revisá el título y la categoría. La publicación no aparece en búsquedas.'
        elif vtas == 0 and vis >= 50:
            diagnostico = 'trafico_sin_venta'
            accion = f'{vis} visitas pero 0 ventas. Problema de precio, fotos o descripción. Optimizá con IA.'
        elif vis_to_vta < 1 and vis >= 100:
            diagnostico = 'conversion_baja'
            accion = f'Conversión {vis_to_vta}% — por debajo del 1% mínimo. Revisá precio vs. competencia.'
        elif pregs > 0 and preg_to_vta < 20:
            diagnostico = 'preguntas_sin_venta'
            accion = f'{pregs} preguntas pero solo {preg_to_vta}% cierran en venta. La descripción no resuelve las dudas.'
        elif vis_to_vta >= 3:
            diagnostico = 'excelente'
            accion = f'Conversión {vis_to_vta}% — excelente. Escalá con más stock y ads.'
        elif vis_to_vta >= 1:
            diagnostico = 'normal'
            accion = f'Conversión {vis_to_vta}% — dentro de lo normal para ML.'
        else:
            diagnostico = 'revisar'
            accion = 'Pocos datos para diagnosticar. Esperá más visitas.'

        result.append({
            **item,
            'visitas_30d':    vis,
            'preguntas_30d':  pregs,
            'ventas_30d':     vtas,
            'vis_to_preg':    vis_to_preg,
            'vis_to_vta':     vis_to_vta,
            'preg_to_vta':    preg_to_vta,
            'diagnostico':    diagnostico,
            'accion':         accion,
        })

    # Ordenar: problemas críticos primero, luego por visitas desc
    _orden_diag = {'trafico_sin_venta': 0, 'conversion_baja': 1, 'preguntas_sin_venta': 2,
                   'sin_trafico': 3, 'revisar': 4, 'normal': 5, 'excelente': 6}
    result.sort(key=lambda x: (_orden_diag.get(x['diagnostico'], 9), -(x['visitas_30d'] or 0)))

    return jsonify({'ok': True, 'items': result, 'total': len(result)})


@app.route('/ventas/<alias>')
def ventas_por_producto(alias):
    """Página de ranking de ventas por producto."""
    return render_template('ventas.html', alias=alias, accounts=get_accounts())


@app.route('/api/ventas-producto/<alias>')
def api_ventas_producto(alias):
    """Ranking de ventas agrupadas por item para los últimos 7, 30 y 60 días."""
    from datetime import datetime as _dt, timedelta as _td
    import time as _t
    from core.account_manager import AccountManager as _AM

    try:
        _mgr    = _AM()
        _client = _mgr.get_client(alias)
        _client._ensure_token()
        token   = _client.account.access_token
        user_id = str(_client.account.user_id or '')
        if not user_id:
            user_id = str(_client.get_me().get('id', ''))
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Token inválido: {e}'}), 401

    heads   = {'Authorization': f'Bearer {token}'}
    now     = _dt.now()
    ML      = 'https://api.mercadolibre.com'

    # Cargar costos para calcular margen
    costos_data = load_json(os.path.join(CONFIG_DIR, 'costos.json')) or {}
    fees_cache  = get_fee_rates()

    def fetch_orders(days):
        date_from = (now - _td(days=days)).strftime('%Y-%m-%dT00:00:00.000-03:00')
        orders = []
        offset = 0
        while True:
            r = req_lib.get(f'{ML}/orders/search', headers=heads,
                params={'seller': user_id, 'order.status': 'paid',
                        'order.date_created.from': date_from,
                        'limit': 50, 'offset': offset, 'sort': 'date_desc'},
                timeout=15)
            if not r.ok:
                break
            d       = r.json()
            batch   = d.get('results', [])
            orders.extend(batch)
            total   = d.get('paging', {}).get('total', 0)
            offset += len(batch)
            if not batch or offset >= total or offset >= 500:
                break
        return orders

    try:
        orders_30 = fetch_orders(30)
        orders_7  = [o for o in orders_30
                     if o.get('date_created','') >= (now - _td(days=7)).strftime('%Y-%m-%d')]

        def aggregate(orders):
            items_map = {}
            for order in orders:
                for oi in order.get('order_items', []):
                    it  = oi.get('item', {})
                    iid = it.get('id', '')
                    if not iid:
                        continue
                    if iid not in items_map:
                        items_map[iid] = {
                            'item_id':  iid,
                            'titulo':   it.get('title', '')[:70],
                            'ingresos': 0.0,
                            'unidades': 0,
                            'ordenes':  0,
                        }
                    qty       = oi.get('quantity', 1) or 1
                    unit_p    = float(oi.get('unit_price', 0) or 0)
                    items_map[iid]['ingresos'] += unit_p * qty
                    items_map[iid]['unidades'] += qty
                    items_map[iid]['ordenes']  += 1
            return list(items_map.values())

        items_30 = aggregate(orders_30)
        items_7  = aggregate(orders_7)

        # Mapa rápido 7d para merge
        map_7 = {i['item_id']: i for i in items_7}

        # Batch-fetch precio actual y sold_quantity histórico
        sold_quantity_map: dict = {}
        all_item_ids = [i['item_id'] for i in items_30]
        for b in range(0, len(all_item_ids), 20):
            batch = all_item_ids[b:b+20]
            try:
                r = req_lib.get(f'https://api.mercadolibre.com/items',
                                headers=heads,
                                params={'ids': ','.join(batch),
                                        'attributes': 'id,price,sold_quantity'},
                                timeout=10)
                if r.ok:
                    for e in r.json():
                        if e.get('code') == 200:
                            bd = e.get('body', {})
                            iid2 = bd.get('id', '')
                            sold_quantity_map[iid2] = {
                                'precio_actual':  float(bd.get('price', 0) or 0),
                                'sold_quantity':  int(bd.get('sold_quantity', 0) or 0),
                            }
            except Exception:
                pass
            _t.sleep(0.1)

        # Enriquecer con fee, costo y margen
        for item in items_30:
            iid    = item['item_id']
            precio = item['ingresos'] / item['unidades'] if item['unidades'] else 0
            ce     = costos_data.get(iid, {})
            costo  = ce.get('costo') if ce else None

            # Fee rate desde stock JSON si está disponible
            stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
            stock_item = next((s for s in stock_data.get('items', []) if s.get('id') == iid), {})
            fee_rate = stock_item.get('fee_rate') or get_rate(stock_item.get('listing_type', ''), fees_cache)
            neto     = precio * (1 - fee_rate)

            if costo and precio > 0:
                margen_pct = (neto - costo) / precio * 100
            else:
                margen_pct = None

            sq_data = sold_quantity_map.get(iid, {})
            item['precio_promedio']  = round(precio, 2)
            item['precio_actual']    = sq_data.get('precio_actual', round(precio, 2))
            item['fee_rate']         = fee_rate
            item['neto_unitario']    = round(neto, 2)
            item['costo']            = costo
            item['margen_pct']       = round(margen_pct, 1) if margen_pct is not None else None
            item['ingresos_7d']      = round(map_7.get(iid, {}).get('ingresos', 0), 2)
            item['unidades_7d']      = map_7.get(iid, {}).get('unidades', 0)
            item['sold_quantity']    = sq_data.get('sold_quantity', 0)
            item['ingresos_total']   = round(sq_data.get('sold_quantity', 0) * sq_data.get('precio_actual', precio), 2)
            item['pct_del_total']    = 0  # se calcula abajo

        # Ordenar por ingresos 30d desc
        items_30.sort(key=lambda x: x['ingresos'], reverse=True)

        # Calcular % del total
        total_ingresos = sum(i['ingresos'] for i in items_30)
        for item in items_30:
            item['ingresos']      = round(item['ingresos'], 2)
            item['pct_del_total'] = round(item['ingresos'] / total_ingresos * 100, 1) if total_ingresos > 0 else 0

        return jsonify({
            'ok':            True,
            'items':         items_30,
            'total_ingresos': round(total_ingresos, 2),
            'total_unidades': sum(i['unidades'] for i in items_30),
            'total_ordenes':  sum(i['ordenes']  for i in items_30),
        })

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# Cache en memoria para revenue (evita re-fetch en cada refresh)
_revenue_cache: dict = {}
_REVENUE_TTL = 300  # segundos

@app.route('/api/urgente/<alias>')
def api_urgente(alias):
    """Devuelve items de atención urgente para el dashboard: mediaciones pendientes + preguntas."""
    from datetime import datetime as _dt, timedelta as _td
    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return jsonify({'ok': False}), 404

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception:
        return jsonify({'ok': False}), 401
    date_60d = (_dt.now() - _td(days=60)).strftime('%Y-%m-%dT00:00:00.000-03:00')

    mediaciones_pendientes = []
    preguntas_count = 0

    # 1 — Mediaciones pendientes: órdenes activas (no canceladas) que tienen mediaciones abiertas
    try:
        r = req_lib.get('https://api.mercadolibre.com/orders/search', headers=heads,
            params={'seller': user_id, 'order.date_created.from': date_60d,
                    'limit': 50, 'sort': 'date_desc'}, timeout=12)
        if r.ok:
            for order in r.json().get('results', []):
                meds = order.get('mediations') or []
                if not meds:
                    continue
                if order.get('status') == 'cancelled':
                    continue   # ya resuelta
                order_id = order.get('id', '')
                items    = order.get('order_items', [])
                title    = items[0].get('item', {}).get('title', '—') if items else '—'
                item_id  = items[0].get('item', {}).get('id', '')     if items else ''
                fecha    = (order.get('date_created') or '')[:10]

                # Buscar el claim_id real via la API de claims de ML
                claim_id = ''
                med_url  = ''
                try:
                    rc = req_lib.get('https://api.mercadolibre.com/v1/claims',
                        headers=heads,
                        params={'resource_id': order_id, 'role': 'respondent'},
                        timeout=8)
                    if rc.ok:
                        claims = rc.json().get('data', [])
                        if claims:
                            claim_id = str(claims[0].get('id', ''))
                except Exception:
                    pass

                if claim_id:
                    med_url = f'https://www.mercadolibre.com.ar/reclamospanel/detalle/{claim_id}'
                elif order_id:
                    med_url = f'https://www.mercadolibre.com.ar/reclamospanel/detalle/{order_id}'

                mediaciones_pendientes.append({
                    'order_id':   order_id,
                    'claim_id':   claim_id,
                    'item_title': title,
                    'item_id':    item_id,
                    'fecha':      fecha,
                    'med_url':    med_url,
                })
    except Exception:
        pass

    # 2 — Preguntas sin responder
    try:
        r2 = req_lib.get('https://api.mercadolibre.com/questions/search',
            headers=heads,
            params={'seller_id': user_id, 'status': 'UNANSWERED', 'limit': 1},
            timeout=8)
        if r2.ok:
            preguntas_count = r2.json().get('total', 0)
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'mediaciones_pendientes': mediaciones_pendientes,
        'preguntas_count':        preguntas_count,
    })


@app.route('/api/revenue-quick/<alias>')
def api_revenue_quick(alias):
    """Devuelve ventas hoy / semana / mes con paginación completa. Cache 5 min."""
    import time as _t
    from datetime import datetime as _dt, timedelta as _td

    now_ts = _t.time()
    if alias in _revenue_cache and now_ts - _revenue_cache[alias]['ts'] < _REVENUE_TTL:
        return jsonify(_revenue_cache[alias]['data'])

    from core.account_manager import AccountManager as _AM
    try:
        _mgr    = _AM()
        _client = _mgr.get_client(alias)
        _client._ensure_token()
        token   = _client.account.access_token
        user_id = str(_client.account.user_id or '')
        if not user_id:
            user_id = str(_client.get_me().get('id', ''))
    except Exception:
        return jsonify({'ok': False})

    heads   = {'Authorization': f'Bearer {token}'}
    # Usar hora de Argentina (UTC-3) para calcular fechas correctamente
    now     = _dt.utcnow() - _td(hours=3)

    def fetch_period(date_from):
        orders = []
        offset = 0
        while True:
            r = req_lib.get('https://api.mercadolibre.com/orders/search',
                params={'seller': user_id, 'order.status': 'paid',
                        'order.date_created.from': date_from,
                        'limit': 50, 'offset': offset, 'sort': 'date_desc'},
                headers=heads, timeout=12)
            if not r.ok:
                break
            d       = r.json()
            results = d.get('results', [])
            orders.extend(results)
            total   = d.get('paging', {}).get('total', 0)
            offset += len(results)
            if not results or offset >= total:
                break
        amount = sum(o.get('total_amount', 0) or 0 for o in orders)
        units  = sum(sum(i.get('quantity', 1) for i in o.get('order_items', [])) for o in orders)
        return {'amount': round(amount, 2), 'units': units, 'orders': len(orders)}

    def fetch_period_range(date_from, date_to=None):
        orders = []
        offset = 0
        params_base = {'seller': user_id, 'order.status': 'paid',
                       'order.date_created.from': date_from,
                       'limit': 50, 'sort': 'date_desc'}
        if date_to:
            params_base['order.date_created.to'] = date_to
        while True:
            r = req_lib.get('https://api.mercadolibre.com/orders/search',
                params={**params_base, 'offset': offset},
                headers=heads, timeout=12)
            if not r.ok:
                break
            d       = r.json()
            results = d.get('results', [])
            orders.extend(results)
            total   = d.get('paging', {}).get('total', 0)
            offset += len(results)
            if not results or offset >= total:
                break
        amount = sum(o.get('total_amount', 0) or 0 for o in orders)
        units  = sum(sum(i.get('quantity', 1) for i in o.get('order_items', [])) for o in orders)
        return {'amount': round(amount, 2), 'units': units, 'orders': len(orders)}

    def pct_change(current, previous):
        if not previous:
            return None
        return round((current - previous) / previous * 100, 1)

    try:
        # Períodos actuales
        hoy        = fetch_period(now.strftime('%Y-%m-%dT00:00:00.000-03:00'))
        semana     = fetch_period((now - _td(days=7)).strftime('%Y-%m-%dT00:00:00.000-03:00'))
        mes        = fetch_period((now - _td(days=30)).strftime('%Y-%m-%dT00:00:00.000-03:00'))

        # Períodos anteriores para comparar
        sem_ant_to   = (now - _td(days=7)).strftime('%Y-%m-%dT%H:%M:%S.000-03:00')
        sem_ant_from = (now - _td(days=14)).strftime('%Y-%m-%dT00:00:00.000-03:00')
        mes_ant_to   = (now - _td(days=30)).strftime('%Y-%m-%dT%H:%M:%S.000-03:00')
        mes_ant_from = (now - _td(days=60)).strftime('%Y-%m-%dT00:00:00.000-03:00')

        semana_ant = fetch_period_range(sem_ant_from, sem_ant_to)
        mes_ant    = fetch_period_range(mes_ant_from, mes_ant_to)

        result = {
            'ok':     True,
            'hoy':    hoy,
            'semana': {**semana,
                       'prev_amount': semana_ant['amount'],
                       'pct': pct_change(semana['amount'], semana_ant['amount'])},
            'mes':    {**mes,
                       'prev_amount': mes_ant['amount'],
                       'pct': pct_change(mes['amount'], mes_ant['amount'])},
        }
        _revenue_cache[alias] = {'ts': now_ts, 'data': result}
        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/historial/<alias>')
def historial(alias):
    rep_data = load_json(os.path.join(DATA_DIR, f'reputacion_{safe(alias)}.json')) or []

    pos_data     = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json'))
    pos_items    = []
    pos_dates    = []
    pos_total    = 0
    pos_con_dato = 0
    pos_top10    = 0
    if pos_data:
        all_dates = set()
        for d in pos_data.values():
            all_dates.update(d.get('history', {}).keys())
        pos_dates = sorted(all_dates)[-14:]
        pos_total = len(pos_data)
        for item_id, item_data in pos_data.items():
            hist      = item_data.get('history', {})
            positions = [hist.get(d) for d in pos_dates]
            real      = [p for p in positions if p is not None and p != 999]
            best      = min(real) if real else None
            if best is not None:
                pos_con_dato += 1
                if best <= 10:
                    pos_top10 += 1
            first    = real[0]  if real else None
            last     = real[-1] if real else None
            tendencia = (last - first) if (first is not None and last is not None and first != last) else 0
            pos_items.append({
                'id':        item_id,
                'title':     item_data.get('title', item_id)[:60],
                'positions': positions,
                'tendencia': tendencia,
                'latest':    last if last is not None else 999,
                'best':      best,
                'has_data':  best is not None,
            })
        # Ordenar: primero los que tienen dato real, luego por posición
        pos_items.sort(key=lambda x: (0 if x['has_data'] else 1, x['latest']))

    return render_template('historial.html', alias=alias,
                           rep_snapshots=rep_data,
                           pos_items=pos_items,
                           pos_dates=[d[5:] for d in pos_dates],
                           pos_total=pos_total,
                           pos_con_dato=pos_con_dato,
                           pos_top10=pos_top10,
                           accounts=get_accounts())


@app.route('/api/rep-items/<alias>')
def api_rep_items(alias):
    """Devuelve publicaciones afectadas por cancelaciones y reclamos (últimos 60 días)."""
    from datetime import datetime as _dt, timedelta as _td
    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return jsonify({'error': 'cuenta no encontrada'}), 404

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'error': str(e)}), 401
    date_from = (_dt.now() - _td(days=60)).strftime('%Y-%m-%dT00:00:00.000-03:00')

    # Lógica basada en la estructura real de la API de ML:
    # cancel_detail.group:
    #   "mediations" → reclamo que escaló a mediación (ML intervino) — afecta reputación
    #   "buyer"      → comprador canceló — no afecta reputación del vendedor
    #   "seller"     → vendedor canceló — afecta reputación
    #   "shipment"   → ML canceló por problema de envío
    #   "internal"   → ML canceló internamente
    #   "fraud"      → cancelado por fraude

    mediaciones_map  = {}   # reclamos reales que escalaron a ML
    vend_cancel_map  = {}   # canceladas por el vendedor (afectan rep)
    comp_cancel_map  = {}   # canceladas por el comprador
    envio_cancel_map = {}   # canceladas por problema de envío (shipment/internal)

    try:
        offset = 0
        while True:
            r = req_lib.get('https://api.mercadolibre.com/orders/search',
                params={
                    'seller': user_id,
                    'order.status': 'cancelled',
                    'order.date_created.from': date_from,
                    'limit': 50, 'offset': offset, 'sort': 'date_desc',
                }, headers=heads, timeout=15)
            if not r.ok:
                break
            data_o  = r.json()
            results = data_o.get('results', [])
            for order in results:
                cd    = order.get('cancel_detail') or {}
                group = (cd.get('group') or '').lower()
                req   = (cd.get('requested_by') or '').lower()

                if group == 'mediations':
                    target = mediaciones_map
                elif req == 'seller' or group == 'seller':
                    target = vend_cancel_map
                elif req == 'buyer' or group == 'buyer':
                    target = comp_cancel_map
                elif group in ('shipment', 'internal', 'fraud'):
                    target = envio_cancel_map
                else:
                    target = comp_cancel_map  # desconocido → comprador por defecto

                for oi in order.get('order_items', []):
                    iid   = oi.get('item', {}).get('id', '')
                    title = oi.get('item', {}).get('title', '')
                    if iid:
                        if iid not in target:
                            target[iid] = {'id': iid, 'title': title, 'count': 0}
                        target[iid]['count'] += 1

            total  = data_o.get('paging', {}).get('total', 0)
            offset += len(results)
            if not results or offset >= total or offset >= 200:
                break
    except Exception:
        pass

    mediaciones  = sorted(mediaciones_map.values(),  key=lambda x: -x['count'])[:10]
    vend_cancels = sorted(vend_cancel_map.values(),  key=lambda x: -x['count'])[:10]
    comp_cancels = sorted(comp_cancel_map.values(),  key=lambda x: -x['count'])[:10]
    envio_cancels= sorted(envio_cancel_map.values(), key=lambda x: -x['count'])[:10]

    return jsonify({
        'mediaciones':   mediaciones,    # reclamos escalados a ML (afectan rep)
        'vend_cancels':  vend_cancels,   # canceladas por el vendedor
        'comp_cancels':  comp_cancels,   # canceladas por el comprador
        'envio_cancels': envio_cancels,  # canceladas por problema de envío
    })


# ── Análisis ──────────────────────────────────────────────────────────────────

@app.route('/competencia/<alias>')
def competencia(alias):
    data  = load_json(os.path.join(DATA_DIR, f'competencia_{safe(alias)}.json'))
    stock = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json'))
    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)

    # Construir lista plana de publicaciones (deduplicada)
    publications = []
    seen_ids = set()
    if data:
        for cat_id, cat in data.get('categorias', {}).items():
            for pub in cat.get('mis_publicaciones', []):
                iid = pub.get('id', '')
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                publications.append({
                    'id':       iid,
                    'titulo':   pub.get('titulo', ''),
                    'precio':   pub.get('precio', 0),
                    'categoria': cat.get('nombre', cat_id),
                    'problems': [], 'urgencia': 'ok', 'title_len': len(pub.get('titulo', '')), 'score': 0,
                })
    elif stock:
        for item in stock.get('items', []):
            iid = item.get('id', '')
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            publications.append({
                'id':       iid,
                'titulo':   item.get('titulo', ''),
                'precio':   item.get('precio', 0),
                'categoria': '',
                'problems': [], 'urgencia': 'ok', 'title_len': len(item.get('titulo', '')), 'score': 0,
            })

    # Detectar problemas reales con atributos de ML API
    if publications and account:
        publications = _enrich_publications_with_attr_analysis(publications, account)

    publications.sort(key=lambda x: -x.get('score', 0))

    # Cargar análisis guardados (últimos 2 días)
    from datetime import datetime as _dt
    saved_analyses = []
    cutoff = _dt.now().timestamp() - 2 * 86400  # 48 horas
    for path in sorted(glob.glob(os.path.join(DATA_DIR, 'analisis_pub_*.json')), reverse=True):
        if os.path.getmtime(path) < cutoff:
            continue
        d = load_json(path)
        if d and d.get('alias') == alias:
            saved_analyses.append(d)

    return render_template('competencia.html', alias=alias,
                           publications=publications,
                           saved_analyses=saved_analyses,
                           fecha=(data or {}).get('fecha'),
                           accounts=get_accounts())


@app.route('/api/opt-seguimiento/<alias>/<item_id>')
def api_opt_seguimiento(alias, item_id):
    """Compara métricas actuales vs. baseline capturado al aplicar la optimización."""
    from datetime import datetime as _dt, timedelta as _td

    all_accs = get_accounts()
    account  = next((a for a in all_accs if a.get('alias') == alias), None)
    if not account:
        return jsonify({'ok': False, 'error': 'cuenta no encontrada'}), 404

    opt_path = os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')
    opt_data = load_json(opt_path) or {}
    opt_item = next((o for o in opt_data.get('optimizaciones', [])
                     if o.get('item_id') == item_id), None)

    if not opt_item or not opt_item.get('baseline'):
        return jsonify({'ok': False, 'error': 'No hay baseline para este item'})

    baseline = opt_item['baseline']
    try:
        token, _, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401
    ML       = 'https://api.mercadolibre.com'

    ahora = {}
    try:
        # Visitas actuales (30d)
        r_vis = req_lib.get(f'{ML}/items/{item_id}/visits/time_window',
                            headers=heads, params={'last': 30, 'unit': 'day'}, timeout=8)
        if r_vis.ok:
            ahora['visitas'] = r_vis.json().get('total_visits', 0)
    except Exception:
        pass

    # Posición actual desde JSON de posiciones
    pos_data = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
    if item_id in pos_data:
        hist = pos_data[item_id].get('history', {})
        if hist:
            last_date = max(hist.keys())
            pos_val   = hist[last_date]
            if pos_val != 999:
                ahora['posicion'] = pos_val
                ahora['posicion_fecha'] = last_date

    # Calcular deltas
    delta_vis = None
    delta_pos = None
    dias_desde = None

    if 'fecha_aplicacion' in baseline:
        try:
            fa = _dt.strptime(baseline['fecha_aplicacion'][:10], '%Y-%m-%d')
            dias_desde = (datetime.now() - fa).days
        except Exception:
            pass

    if 'visitas_antes' in baseline and 'visitas' in ahora:
        delta_vis = ahora['visitas'] - baseline['visitas_antes']

    if 'posicion_antes' in baseline and 'posicion' in ahora:
        # Negativo = mejoró (bajó el número de posición)
        delta_pos = ahora['posicion'] - baseline['posicion_antes']

    # Veredicto
    if dias_desde is not None and dias_desde < 3:
        veredicto = 'muy_reciente'
        veredicto_txt = f'Optimización aplicada hace {dias_desde} día(s). Esperá al menos 7 días para ver resultados.'
    elif delta_pos is not None and delta_pos < -2:
        veredicto = 'mejoro'
        veredicto_txt = f'Subió {abs(delta_pos)} posiciones. La optimización funcionó.'
    elif delta_vis is not None and delta_vis > 50:
        veredicto = 'mejoro'
        veredicto_txt = f'+{delta_vis} visitas más que antes. Más tráfico entrante.'
    elif delta_pos is not None and delta_pos > 2:
        veredicto = 'empeoro'
        veredicto_txt = f'Bajó {delta_pos} posiciones. Revisá si el cambio de título fue correcto.'
    elif delta_pos is not None or delta_vis is not None:
        veredicto = 'sin_cambio'
        veredicto_txt = 'Sin cambio significativo aún. Puede tardar más días en reflejarse.'
    else:
        veredicto = 'sin_dato'
        veredicto_txt = 'Sin datos suficientes para comparar.'

    result = {
        'ok':          True,
        'baseline':    baseline,
        'ahora':       ahora,
        'delta_vis':   delta_vis,
        'delta_pos':   delta_pos,
        'dias_desde':  dias_desde,
        'veredicto':   veredicto,
        'veredicto_txt': veredicto_txt,
    }

    # Persistir en el JSON para no recalcular siempre
    for o in opt_data.get('optimizaciones', []):
        if o.get('item_id') == item_id:
            o['seguimiento'] = result
            break
    save_json(opt_path, opt_data)

    return jsonify(result)


@app.route('/api/ml-quality-scores/<alias>')
def api_ml_quality_scores(alias):
    """Devuelve el score de calidad ML para todas las publicaciones del alias."""
    from core.account_manager import AccountManager
    from modules.seo_optimizer import _get_ml_quality_score

    try:
        mgr    = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token  = client.account.access_token

        comp  = load_json(os.path.join(DATA_DIR, f'competencia_{safe(alias)}.json'))
        stock = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json'))
        pubs  = []
        if comp:
            for cat in comp.get('categorias', {}).values():
                for p in cat.get('mis_publicaciones', []):
                    pid = p.get('id', '') or p.get('item_id', '')
                    if pid and not any(x['id'] == pid for x in pubs):
                        pubs.append({'id': pid, 'titulo': p.get('titulo', '')})
        if not pubs and stock:
            for p in stock.get('items', []):
                pid = p.get('id', '') or p.get('item_id', '')
                if pid and not any(x['id'] == pid for x in pubs):
                    pubs.append({'id': pid, 'titulo': p.get('titulo', '')})

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_score(pub):
            score_data = _get_ml_quality_score(pub['id'], token)
            return {
                'id':      pub['id'],
                'titulo':  pub['titulo'],
                'score':   score_data.get('score', 0),
                'level':   score_data.get('level', ''),
                'reasons': score_data.get('reasons', [])[:3],
            }

        results = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_score, pub): pub for pub in pubs[:40]}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception:
                    pass

        results.sort(key=lambda x: x['score'])
        return jsonify({'ok': True, 'items': results})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/borrar-optimizacion', methods=['POST'])
def api_borrar_optimizacion():
    body    = request.get_json() or {}
    alias   = body.get('alias', '')
    item_id = body.get('item_id', '').strip().upper()
    if not alias or not item_id:
        return jsonify({'ok': False}), 400
    path = os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')
    data = load_json(path) or {'optimizaciones': []}
    data['optimizaciones'] = [o for o in data.get('optimizaciones', []) if o.get('item_id') != item_id]
    save_json(path, data)
    return jsonify({'ok': True})


@app.route('/api/aplicar-optimizacion', methods=['POST'])
def api_aplicar_optimizacion():
    from core.account_manager import AccountManager
    import unicodedata as _ud

    body        = request.get_json() or {}
    alias       = body.get('alias', '')
    item_id     = body.get('item_id', '').strip().upper()
    titulo      = body.get('titulo', '').strip()
    descripcion = body.get('descripcion', '').strip()
    # [{name: "Marca", value: "Kamel"}, ...] — nombres como los generó Claude
    attrs_raw   = body.get('attributes', [])

    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Falta alias o item_id'}), 400
    if not titulo and not descripcion and not attrs_raw:
        return jsonify({'ok': False, 'error': 'Nada para aplicar'}), 400

    def _norm(s):
        """Normaliza para comparación: minúsculas, sin tildes, sin espacios extra."""
        s = s.lower().strip()
        s = _ud.normalize('NFD', s)
        s = ''.join(c for c in s if _ud.category(c) != 'Mn')
        return s

    try:
        client = AccountManager().get_client(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    errors  = []
    applied = []

    # ── Capturar estado ANTES de aplicar cambios (para monitor de evolución) ──
    titulo_antes   = ''
    visitas_antes      = 0
    ventas_antes       = 0
    ventas_total_antes = 0
    conv_antes         = 0.0
    posicion_antes     = None
    posicion_kw        = ''
    try:
        client._ensure_token()
        _h_pre = {'Authorization': f'Bearer {client.account.access_token}'}
        _ir = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}', headers=_h_pre, timeout=8)
        if _ir.ok:
            _ir_json       = _ir.json()
            titulo_antes   = _ir_json.get('title', '')
            ventas_total_antes = _ir_json.get('sold_quantity', 0)
        _vr = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
            headers=_h_pre, params={'last': 7, 'unit': 'day'}, timeout=6)
        if _vr.ok:
            visitas_antes = _vr.json().get('total_visits', 0)
        # Posición y conversión desde datos locales
        _pos_data  = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
        _stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
        if item_id in _pos_data:
            _ph = _pos_data[item_id].get('history', {})
            if _ph:
                _last_date = max(_ph.keys())
                _pv = _ph[_last_date]
                if _pv != 999:
                    posicion_antes = _pv
        _kws = _pos_data.get(item_id, {}).get('keywords', [])
        posicion_kw = _kws[0] if _kws else ''
        for _si in (_stock_data.get('items') or []):
            if _si.get('id') == item_id:
                ventas_antes = _si.get('ventas_30d') or 0
                conv_antes   = _si.get('conversion_pct') or 0.0
                break
    except Exception:
        pass

    # ── Título ────────────────────────────────────────────────────────────────
    if titulo:
        try:
            client._put(f'/items/{item_id}', {'title': titulo})
            applied.append('titulo')
        except Exception as e:
            errors.append(f'título: {e}')

    # ── Descripción ───────────────────────────────────────────────────────────
    if descripcion:
        try:
            # Strip internal validation checklist — no debe publicarse en ML
            cl_idx = descripcion.find('### CHECKLIST DE VALIDACIÓN INTERNA')
            desc_clean = descripcion[:cl_idx].strip() if cl_idx > 0 else descripcion
            client._put(f'/items/{item_id}/description', {'plain_text': desc_clean})
            applied.append('descripcion')
        except Exception as e:
            errors.append(f'descripción: {e}')

    # ── Atributos (ficha técnica) ─────────────────────────────────────────────
    attrs_applied = 0
    attrs_errors  = []
    if attrs_raw:
        try:
            # 1. Obtener item para conocer la categoría
            client._ensure_token()
            ml_headers = {'Authorization': f'Bearer {client.account.access_token}'}
            item_r = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}',
                                  headers=ml_headers, timeout=8)
            if not item_r.ok:
                errors.append('atributos: no se pudo obtener el item')
            else:
                cat_id = item_r.json().get('category_id', '')
                req_attrs, opt_attrs = _fetch_category_attributes(cat_id, ml_headers)
                all_cat_attrs = req_attrs + opt_attrs

                # 2. Construir mapa nombre_normalizado → {id, values}
                attr_map = {}
                for a in all_cat_attrs:
                    attr_map[_norm(a['name'])] = {
                        'id':     a['id'],
                        'values': a.get('values', []),
                        'type':   a.get('type', 'string'),
                    }

                # 3. Mapear los atributos de Claude a IDs de ML
                payload_attrs = []
                for ar in attrs_raw:
                    name_raw  = (ar.get('name') or '').strip()
                    value_raw = (ar.get('value') or '').strip()
                    if not name_raw or not value_raw:
                        continue

                    name_n = _norm(name_raw)
                    cat_attr = attr_map.get(name_n)

                    # Búsqueda parcial si no hay match exacto
                    if not cat_attr:
                        for k, v in attr_map.items():
                            if name_n in k or k in name_n:
                                cat_attr = v
                                break

                    if not cat_attr:
                        attrs_errors.append(f'"{name_raw}" no encontrado en categoría')
                        continue

                    # Si el atributo tiene valores aceptados, buscar el más cercano
                    accepted = cat_attr.get('values', [])
                    if accepted:
                        value_n = _norm(value_raw)
                        best_val = next(
                            (v for v in accepted if _norm(v) == value_n),
                            next((v for v in accepted if value_n in _norm(v) or _norm(v) in value_n), None)
                        )
                        if best_val:
                            payload_attrs.append({'id': cat_attr['id'], 'value_name': best_val})
                        else:
                            # Valor libre (no está en la lista pero es un campo de texto)
                            payload_attrs.append({'id': cat_attr['id'], 'value_name': value_raw})
                    else:
                        payload_attrs.append({'id': cat_attr['id'], 'value_name': value_raw})

                # 4. Aplicar vía PUT /items/{id}
                if payload_attrs:
                    try:
                        client._put(f'/items/{item_id}', {'attributes': payload_attrs})
                        attrs_applied = len(payload_attrs)
                        applied.append(f'atributos ({attrs_applied})')
                    except Exception as e:
                        errors.append(f'atributos: {e}')

        except Exception as e:
            errors.append(f'atributos: {e}')

    # ── Persistir en optimizaciones_Alias.json ───────────────────────────────
    json_updated = False
    if applied:
        opt_path = os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')
        data = load_json(opt_path) or {'optimizaciones': []}
        for o in data.get('optimizaciones', []):
            if o.get('item_id') == item_id:
                if 'descripcion' in applied:   o['descripcion_aplicada'] = True
                if 'titulo'      in applied:   o['titulo_aplicado']      = True
                if attrs_applied:              o['ficha_aplicada']       = True
                o['aplicado'] = True
                json_updated  = True
                break
        if json_updated:
            save_json(opt_path, data)

    # ── Guardar en Monitor de Evolución ──────────────────────────────────────
    if applied:
        _now       = datetime.now().strftime('%Y-%m-%d %H:%M')
        _mon_path  = os.path.join(DATA_DIR, 'monitor_evolucion.json')
        _mon       = load_json(_mon_path) or {'items': []}
        if not isinstance(_mon, dict):
            _mon = {'items': []}
        # Purgar ítems con optimización de más de 90 días
        _cutoff = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        _mon['items'] = [
            it for it in _mon.get('items', [])
            if (it.get('fecha_opt') or '')[:10] >= _cutoff
        ]

        # Buscar entrada existente para este item (actualizar si ya está)
        _existing = next((x for x in _mon['items'] if x.get('item_id') == item_id and x.get('alias') == alias), None)
        _baseline = {
            'fecha':        _now,
            'visitas_7d':   visitas_antes,
            'ventas_30d':   ventas_antes,
            'ventas_total': ventas_total_antes,
            'conv_pct':     conv_antes,
            'posicion':     posicion_antes,
            'posicion_kw': posicion_kw,
        }
        # Título del producto (nombre amigable)
        _opt_data  = load_json(os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')) or {}
        _opt_match = next((o for o in _opt_data.get('optimizaciones', []) if o.get('item_id') == item_id), {})
        _titulo_producto = _opt_match.get('titulo_actual') or titulo_antes or item_id

        if _existing:
            # Re-optimización: actualizar baseline y resetear snapshots
            _existing.update({
                'fecha_opt':      _now,
                'titulo_antes':   titulo_antes   if 'titulo' in applied else _existing.get('titulo_antes', ''),
                'titulo_despues': titulo         if 'titulo' in applied else _existing.get('titulo_despues', ''),
                'baseline':       _baseline,
                'snapshots':      [],
                'ultimo_snapshot': None,
                'applied':        applied,
            })
        else:
            _mon['items'].append({
                'item_id':         item_id,
                'alias':           alias,
                'titulo_producto': _titulo_producto,
                'fecha_opt':       _now,
                'titulo_antes':    titulo_antes,
                'titulo_despues':  titulo if 'titulo' in applied else '',
                'baseline':        _baseline,
                'snapshots':       [],
                'ultimo_snapshot': None,
                'applied':         applied,
            })
        save_json(_mon_path, _mon)

        # ── Sprint 3.1: lanzar captura async del baseline COMPLETO ──────────
        # El baseline plano de arriba (visitas_antes, ventas, etc.) queda como
        # placeholder mientras el thread corre. Cuando termine reemplaza la
        # entry con la estructura v2 de 14 métricas en 5 secciones.
        try:
            from modules.baseline_capture import (
                capturar_baseline_async, marcar_capturing,
            )
            # Top 3 keywords desde la optimización para tracking de posiciones
            _top_kws = []
            try:
                _opt_d = load_json(os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')) or {}
                _opt_match = next((o for o in _opt_d.get('optimizaciones', [])
                                   if o.get('item_id') == item_id), None)
                if _opt_match and _opt_match.get('keywords_faltantes'):
                    _top_kws = [k.strip() for k in
                                (_opt_match.get('keywords_faltantes') or '').split(',')
                                if k.strip()][:3]
            except Exception:
                pass

            marcar_capturing(DATA_DIR, item_id, alias)
            capturar_baseline_async(item_id, alias, client, DATA_DIR,
                                    top_keywords=_top_kws or None)
        except Exception as e:
            app.logger.warning('[baseline_capture] no se pudo lanzar async: %s', e)

    if applied:
        _audit('APLICAR_ML', alias=alias, item_id=item_id, campos=','.join(applied))

    result = {'ok': True, 'applied': applied, 'json_updated': json_updated}
    if attrs_errors:
        result['attrs_warnings'] = attrs_errors
    if errors:
        return jsonify({'ok': False, 'error': '; '.join(errors), **result})
    return jsonify(result)


@app.route('/api/baseline-recapturar/<alias>/<item_id>', methods=['POST'])
def api_baseline_recapturar(alias, item_id):
    """Recaptura el baseline completo de un item ya monitoreado.

    Útil para baselines viejos (version != 2) que solo tienen 5-6 campos planos.
    El usuario clickea "Re-capturar baseline ahora" en una card del Monitor.

    ⚠️ La captura es del estado ACTUAL del item, no del momento original de la
    optimización. La UI debe avisar al usuario antes de confirmar.
    """
    item_id = (item_id or '').strip().upper()
    alias   = (alias or '').strip()
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Faltan alias o item_id'}), 400

    try:
        from core.account_manager import AccountManager as _AM_rec
        _client_rec = _AM_rec().get_client(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Auth: {e}'}), 401

    # Verificar que la entry existe en monitor
    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon = load_json(_mon_path) or {'items': []}
    entry = next((it for it in _mon.get('items', [])
                  if it.get('item_id') == item_id and it.get('alias') == alias), None)
    if not entry:
        return jsonify({'ok': False, 'error': 'Item no está en el monitor'}), 404

    # Top keywords desde la optimización original si existe
    top_kws = []
    try:
        opt_d = load_json(os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')) or {}
        opt_match = next((o for o in opt_d.get('optimizaciones', [])
                          if o.get('item_id') == item_id), None)
        if opt_match and opt_match.get('keywords_faltantes'):
            top_kws = [k.strip() for k in
                       (opt_match.get('keywords_faltantes') or '').split(',')
                       if k.strip()][:3]
    except Exception:
        pass

    from modules.baseline_capture import (
        capturar_baseline_async, marcar_capturing,
    )
    marcar_capturing(DATA_DIR, item_id, alias)
    capturar_baseline_async(item_id, alias, _client_rec, DATA_DIR,
                            top_keywords=top_kws or None)

    return jsonify({
        'ok':       True,
        'mensaje':  'Captura iniciada en background. Recargá en 10-15 segundos.',
        'item_id':  item_id,
    })


@app.route('/api/republicacion-revincular/<alias>/<item_id>', methods=['POST'])
def api_republicacion_revincular(alias, item_id):
    """Re-vincula una republicación existente capturando snapshot retroactivo
    de MLA-X (la original).

    Útil cuando una entry tiene origen='nueva' pero NO tiene
    publicacion_original.snapshot_al_republicar (porque la vinculación se
    hizo antes del Sprint 3).

    El snapshot que se captura es del estado ACTUAL de MLA-X — la UI debe
    avisar que NO refleja el momento real de la republicación.
    """
    item_id = (item_id or '').strip().upper()
    alias   = (alias or '').strip()
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Faltan alias o item_id'}), 400

    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon = load_json(_mon_path) or {'items': []}
    entry = next((it for it in _mon.get('items', [])
                  if it.get('item_id') == item_id and it.get('alias') == alias), None)
    if not entry:
        return jsonify({'ok': False, 'error': 'Item no está en el monitor'}), 404

    mla_orig = entry.get('item_id_original')
    if not mla_orig:
        return jsonify({'ok': False, 'error': 'Esta entry no es una republicación'}), 400

    # Capturar baseline completo de la original
    try:
        from core.account_manager import AccountManager as _AM_rev
        from modules.baseline_capture import capturar_baseline_completo
        _client_rev = _AM_rev().get_client(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Auth: {e}'}), 401

    try:
        snap_orig = capturar_baseline_completo(mla_orig, alias, _client_rev,
                                                top_keywords=None, data_dir=DATA_DIR)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Captura falló: {e}'}), 500

    # Marcar como retroactivo + guardar contexto temporal
    fecha_revinc = datetime.now().strftime('%Y-%m-%d %H:%M')
    entry['publicacion_original'] = {
        'mla':                              mla_orig,
        'snapshot_al_republicar':           snap_orig,
        'snapshot_al_republicar_es_retroactivo': True,
        'fecha_snapshot_capturado':         fecha_revinc,
        'fecha_republicacion_original':     entry.get('fecha_opt', 'desconocida'),
        'advertencia':                      'Snapshot capturado a posteriori — NO refleja el estado al momento de la republicación',
        'snapshots':                        [],
        'ultimo_snapshot':                  None,
        'estado_ml':                        'active',
        'fecha_estado':                     fecha_revinc,
    }
    save_json(_mon_path, _mon)

    return jsonify({
        'ok':         True,
        'mensaje':    'Snapshot retroactivo de la publicación original capturado.',
        'mla_orig':   mla_orig,
        'es_retroactivo': True,
    })


@app.route('/api/test-claude')
def api_test_claude():
    """Endpoint de diagnóstico: testea la conexión con la API de Anthropic."""
    import os
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return jsonify({'ok': False, 'error': 'ANTHROPIC_API_KEY no configurada'})
    try:
        ai = anthropic.Anthropic()
        msg = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=10,
            messages=[{'role': 'user', 'content': 'di ok'}]
        )
        return jsonify({'ok': True, 'response': msg.content[0].text, 'key_prefix': key[:20] + '...'})
    except Exception as e:
        import traceback
        return jsonify({'ok': False, 'error_type': type(e).__name__, 'error': str(e), 'traceback': traceback.format_exc()})


@app.route('/monitor/<alias>')
def monitor_evolucion(alias):
    return render_template('monitor_evolucion.html', alias=alias, accounts=get_accounts())


@app.route('/api/monitor-evolucion/<alias>')
def api_monitor_evolucion(alias):
    """Devuelve todos los items monitoreados del alias con deltas calculados."""
    from core.account_manager import AccountManager as _AM
    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    items     = [x for x in _mon.get('items', []) if x.get('alias') == alias]

    result = []
    for it in items:
        baseline = it.get('baseline') or {}
        ultimo   = it.get('ultimo_snapshot') or {}
        snapshots = it.get('snapshots') or []

        # Calcular deltas baseline → último snapshot
        def _delta(key):
            b = baseline.get(key)
            u = ultimo.get(key)
            if b is None or u is None:
                return None
            return round(u - b, 2)

        def _delta_pct(key):
            b = baseline.get(key) or 0
            u = ultimo.get(key)
            if u is None or b == 0:
                return None
            return round((u - b) / b * 100, 1)

        # Días transcurridos desde la optimización
        try:
            from datetime import datetime as _dtt
            _fd  = it.get('fecha_opt', '')[:10]
            dias = (_dtt.now() - _dtt.strptime(_fd, '%Y-%m-%d')).days
        except Exception:
            dias = 0

        result.append({
            'item_id':          it.get('item_id'),
            'alias':            it.get('alias'),
            'titulo_producto':  it.get('titulo_producto', it.get('item_id', '')),
            'fecha_opt':        it.get('fecha_opt', ''),
            'dias_elapsed':     dias,
            'titulo_antes':     it.get('titulo_antes', ''),
            'titulo_despues':   it.get('titulo_despues', ''),
            'origen':           it.get('origen', ''),
            'item_id_original': it.get('item_id_original', ''),
            'applied':          it.get('applied', []),
            'baseline':         baseline,
            'ultimo_snapshot':  ultimo,
            'snapshots':        snapshots,
            'snapshots_n':      len(snapshots),
            'visitas_ayer':     ultimo.get('visitas_ayer'),
            # ── Sprint 3.1: flags + datos de captura completa ──
            '_capturing':       it.get('_capturing', False),
            '_capture_error':   it.get('_capture_error'),
            'baseline_version': baseline.get('version', 1),
            'publicacion_original': it.get('publicacion_original'),
            'deltas': {
                'visitas_7d':    _delta('visitas_7d'),
                'visitas_pct':   _delta_pct('visitas_7d'),
                'ventas_30d':    _delta('ventas_30d'),
                'ventas_pct':    _delta_pct('ventas_30d'),
                'ventas_total':  _delta('ventas_total'),
                'conv_pct':      _delta('conv_pct'),
                'posicion':      _delta('posicion'),
            },
        })

    return jsonify({'ok': True, 'items': result})


@app.route('/api/monitor-refresh', methods=['POST'])
def api_monitor_refresh():
    """Actualiza las métricas actuales de un item monitoreado."""
    from core.account_manager import AccountManager as _AM
    body    = request.get_json() or {}
    alias   = body.get('alias', '').strip()
    item_id = body.get('item_id', '').strip().upper()
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Falta alias o item_id'}), 400

    try:
        client = _AM().get_client(alias)
        client._ensure_token()
        _h = {'Authorization': f'Bearer {client.account.access_token}'}
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    snap = {'fecha': datetime.now().strftime('%Y-%m-%d %H:%M')}

    # Visitas 7d + desglose diario (hoy y ayer)
    try:
        _vr = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
            headers=_h, params={'last': 7, 'unit': 'day'}, timeout=6)
        if _vr.ok:
            snap['visitas_7d'] = _vr.json().get('total_visits', 0)
    except Exception:
        pass
    try:
        from datetime import datetime as _dtn, timedelta as _tdd
        _ayer = (_dtn.now() - _tdd(days=1)).strftime('%Y-%m-%d')
        _vd = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
            headers=_h,
            params={'date_from': f'{_ayer}T00:00:00.000-03:00',
                    'date_to':   f'{_ayer}T23:59:59.000-03:00'},
            timeout=6)
        if _vd.ok:
            snap['visitas_ayer'] = _vd.json().get('total_visits', 0)
    except Exception:
        pass
    try:
        _ir = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}', headers=_h, timeout=6)
        if _ir.ok:
            snap['ventas_total'] = _ir.json().get('sold_quantity', 0)
    except Exception:
        pass

    # Posición + conversión desde datos locales
    _pos_data   = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
    _stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    if item_id in _pos_data:
        _ph = _pos_data[item_id].get('history', {})
        if _ph:
            _pv = _ph[max(_ph.keys())]
            if _pv != 999:
                snap['posicion'] = _pv
    for _si in (_stock_data.get('items') or []):
        if _si.get('id') == item_id:
            snap['ventas_30d'] = _si.get('ventas_30d') or 0
            snap['conv_pct']   = _si.get('conversion_pct') or 0.0
            break

    # Guardar snapshot
    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    snap_orig = None   # snapshot de la publicación original si hay republicación
    for it in _mon.get('items', []):
        if it.get('item_id') == item_id and it.get('alias') == alias:
            it.setdefault('snapshots', []).append(snap)
            it['snapshots']      = it['snapshots'][-30:]   # máx 30 snapshots
            it['ultimo_snapshot'] = snap

            # ── Sprint 3.1 ext: refrescar también la publicación original ──
            # Si esta entry tiene publicacion_original (caso republicación),
            # capturar también snapshot de MLA-X y verificar su status.
            pub_orig = it.get('publicacion_original') or {}
            mla_orig = pub_orig.get('mla')
            if mla_orig:
                snap_orig = _capturar_snapshot_simple(mla_orig, _h, alias)
                pub_orig.setdefault('snapshots', []).append(snap_orig)
                pub_orig['snapshots']       = pub_orig['snapshots'][-30:]
                pub_orig['ultimo_snapshot'] = snap_orig
                # Actualizar estado_ml si cambió (active/paused/closed)
                try:
                    _r_st = req_lib.get(
                        f'https://api.mercadolibre.com/items/{mla_orig}',
                        headers=_h, params={'attributes': 'status'}, timeout=6)
                    if _r_st.ok:
                        nuevo_estado = _r_st.json().get('status', 'unknown')
                        if nuevo_estado != pub_orig.get('estado_ml'):
                            pub_orig['estado_ml']    = nuevo_estado
                            pub_orig['fecha_estado'] = datetime.now().strftime('%Y-%m-%d %H:%M')
                except Exception:
                    pass
                it['publicacion_original'] = pub_orig
            break
    save_json(_mon_path, _mon)
    return jsonify({'ok': True, 'snapshot': snap, 'snapshot_original': snap_orig})


def _capturar_snapshot_simple(item_id: str, headers: dict, alias: str) -> dict:
    """Snapshot ligero de un item — usado para refrescar la publicación original
    en caso de republicación. Reusa la misma estructura que el snapshot de la
    nueva (visitas_7d, ventas_total, posición, conv_pct).
    """
    snap = {'fecha': datetime.now().strftime('%Y-%m-%d %H:%M')}
    try:
        r = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
                        headers=headers, params={'last': 7, 'unit': 'day'}, timeout=6)
        if r.ok:
            snap['visitas_7d'] = r.json().get('total_visits', 0)
    except Exception:
        pass
    try:
        r = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}', headers=headers, timeout=6)
        if r.ok:
            snap['ventas_total'] = r.json().get('sold_quantity', 0)
    except Exception:
        pass
    # Posición + ventas_30d desde JSON local (ya cargado por el cron diario)
    try:
        _pos = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
        if item_id in _pos:
            _ph = _pos[item_id].get('history', {})
            if _ph:
                _pv = _ph[max(_ph.keys())]
                if _pv != 999:
                    snap['posicion'] = _pv
        _stk = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
        for _si in (_stk.get('items') or []):
            if _si.get('id') == item_id:
                snap['ventas_30d'] = _si.get('ventas_30d') or 0
                snap['conv_pct']   = _si.get('conversion_pct') or 0.0
                break
    except Exception:
        pass
    return snap


@app.route('/api/monitor-alertas-count/<alias>')
def api_monitor_alertas_count(alias):
    """Devuelve cantidad de alertas no leídas para el badge del nav."""
    _mon  = load_json(os.path.join(DATA_DIR, 'monitor_evolucion.json')) or {'items': []}
    count = sum(
        1 for it in _mon.get('items', [])
        if it.get('alias') == alias
        for a in (it.get('alertas') or [])
        if not a.get('leida') and a.get('nivel') in ('warning', 'bueno')
    )
    return jsonify({'count': count})


@app.route('/api/monitor-marcar-leidas', methods=['POST'])
def api_monitor_marcar_leidas():
    """Marca todas las alertas del alias como leídas."""
    body  = request.get_json() or {}
    alias = body.get('alias', '').strip()
    if not alias:
        return jsonify({'ok': False, 'error': 'Falta alias'}), 400
    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    for it in _mon.get('items', []):
        if it.get('alias') == alias:
            for a in (it.get('alertas') or []):
                a['leida'] = True
    save_json(_mon_path, _mon)
    return jsonify({'ok': True})


@app.route('/api/monitor-delete', methods=['POST'])
def api_monitor_delete():
    """Elimina un item del monitor de evolución."""
    body    = request.get_json() or {}
    alias   = body.get('alias', '').strip()
    item_id = body.get('item_id', '').strip().upper()
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Falta alias o item_id'}), 400

    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    before    = len(_mon.get('items', []))
    _mon['items'] = [x for x in _mon.get('items', [])
                     if not (x.get('item_id') == item_id and x.get('alias') == alias)]
    save_json(_mon_path, _mon)
    return jsonify({'ok': True, 'removed': before - len(_mon['items'])})


@app.route('/api/generar-faq', methods=['POST'])
def api_generar_faq():
    """Genera 5 preguntas frecuentes usando preguntas reales de compradores del item + Claude."""
    from core.account_manager import AccountManager
    body    = request.get_json() or {}
    alias   = body.get('alias', '').strip()
    item_id = body.get('item_id', '').strip().upper()
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Faltan alias o item_id'}), 400

    try:
        client = AccountManager().get_client(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    client._ensure_token()
    ml_headers = {'Authorization': f'Bearer {client.account.access_token}'}

    # 1. Buscar preguntas reales del item (respondidas y sin responder)
    preguntas_reales = []
    for status in ('ANSWERED', 'UNANSWERED'):
        try:
            r = req_lib.get(
                'https://api.mercadolibre.com/questions/search',
                params={'item': item_id, 'status': status, 'limit': 30},
                headers=ml_headers, timeout=8
            )
            if r.ok:
                for q in r.json().get('questions', []):
                    texto = q.get('text', '').strip()
                    respuesta = (q.get('answer') or {}).get('text', '').strip()
                    if texto:
                        preguntas_reales.append({'pregunta': texto, 'respuesta': respuesta})
        except Exception:
            pass

    # 2. Obtener título del item para contexto
    titulo_item = item_id
    try:
        ir = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}',
                         headers=ml_headers, timeout=6)
        if ir.ok:
            titulo_item = ir.json().get('title', item_id)
    except Exception:
        pass

    if not preguntas_reales:
        return jsonify({'ok': False, 'error': 'No se encontraron preguntas de compradores para este item. Publicá primero y volvé cuando haya preguntas.'}), 404

    # 3. Llamar a Claude para generar FAQ
    preguntas_txt = '\n'.join(
        f'- {p["pregunta"]}' + (f'\n  Respuesta del vendedor: {p["respuesta"]}' if p['respuesta'] else '')
        for p in preguntas_reales[:40]
    )

    prompt = f"""Sos experto en ecommerce de MercadoLibre Argentina. Analizás las preguntas reales de compradores de esta publicación y generás un bloque FAQ para la descripción del producto.

PRODUCTO: {titulo_item}

PREGUNTAS REALES DE COMPRADORES:
{preguntas_txt}

TAREA: Generá exactamente 5 preguntas frecuentes y sus respuestas en base a los patrones reales que ves arriba.

REGLAS:
- Español rioplatense (vos, tus)
- Respuestas directas, máximo 2 oraciones
- Sin markdown, sin bullets, solo texto plano
- No mencionar precios ni garantías ML (ML los muestra por separado)
- Elegí las 5 preguntas más frecuentes o importantes

FORMATO EXACTO (respetá los separadores):
PREGUNTAS FRECUENTES

P: [pregunta 1]
R: [respuesta 1]

P: [pregunta 2]
R: [respuesta 2]

P: [pregunta 3]
R: [respuesta 3]

P: [pregunta 4]
R: [respuesta 4]

P: [pregunta 5]
R: [respuesta 5]"""

    try:
        ai = anthropic.Anthropic()
        msg = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}]
        )
        faq_text = msg.content[0].text.strip()
        return jsonify({'ok': True, 'faq': faq_text, 'total_preguntas': len(preguntas_reales)})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Error Claude: {e}'}), 500


@app.route('/api/opt-marcar-aplicado', methods=['POST'])
def api_opt_marcar_aplicado():
    """Marca una optimización como aplicada y captura el baseline actual para seguimiento.
    Útil cuando el usuario aplicó los cambios manualmente en ML sin usar 'Aplicar en ML'."""
    body    = request.get_json() or {}
    alias   = body.get('alias', '').strip()
    item_id = body.get('item_id', '').strip()
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Faltan alias o item_id'}), 400

    opt_path = os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')
    data     = load_json(opt_path) or {'optimizaciones': []}
    opt_item = next((o for o in data.get('optimizaciones', []) if o.get('item_id') == item_id), None)
    if not opt_item:
        return jsonify({'ok': False, 'error': 'No hay optimización guardada para este item'}), 404

    try:
        token, _, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    baseline = {'fecha_aplicacion': datetime.now().strftime('%Y-%m-%d %H:%M')}
    try:
        r_vis = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
            headers=heads, params={'last': 30, 'unit': 'day'}, timeout=6)
        if r_vis.ok:
            baseline['visitas_antes'] = r_vis.json().get('total_visits', 0)
    except Exception:
        pass

    pos_data = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
    if item_id in pos_data:
        hist = pos_data[item_id].get('history', {})
        if hist:
            last_date = max(hist.keys())
            pos_val   = hist[last_date]
            if pos_val != 999:
                baseline['posicion_antes'] = pos_val
                baseline['posicion_fecha'] = last_date

    opt_item['aplicado']   = True
    opt_item['baseline']   = baseline
    opt_item['seguimiento'] = None
    save_json(opt_path, data)

    # Registrar en Monitor de Evolución para tracking con línea de tiempo
    _now_mon  = datetime.now().strftime('%Y-%m-%d %H:%M')
    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    if not isinstance(_mon, dict):
        _mon = {'items': []}

    # Leer ventas_30d, conv_pct y ventas_total del stock JSON (mismo flujo que
    # api_aplicar_optimizacion). Antes estaban hardcodeados en cero, lo que
    # generaba "Antes: —" en Monitor de Evolución.
    _ventas_30d_b   = 0
    _conv_pct_b     = 0.0
    _ventas_total_b = 0
    try:
        _stock_data_b = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
        for _si_b in (_stock_data_b.get('items') or []):
            if _si_b.get('id') == item_id:
                _ventas_30d_b = int(_si_b.get('ventas_30d') or 0)
                _conv_pct_b   = float(_si_b.get('conversion_pct') or 0.0)
                break
        _ir_b = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}',
                            headers=heads, timeout=6)
        if _ir_b.ok:
            _ventas_total_b = int(_ir_b.json().get('sold_quantity') or 0)
    except Exception:
        pass

    _mon_baseline = {
        'fecha':        _now_mon,
        'visitas_7d':   baseline.get('visitas_antes', 0),
        'ventas_30d':   _ventas_30d_b,
        'ventas_total': _ventas_total_b,
        'conv_pct':     _conv_pct_b,
        'posicion':     baseline.get('posicion_antes'),
        'posicion_kw':  None,
    }
    _titulo_prod   = opt_item.get('titulo_actual', item_id)
    _titulo_nuevo  = opt_item.get('titulo_nuevo', '')
    _existing_mon  = next((x for x in _mon.get('items', [])
                           if x.get('item_id') == item_id and x.get('alias') == alias), None)
    if _existing_mon:
        _existing_mon.update({
            'fecha_opt':      _now_mon,
            'titulo_antes':   _titulo_prod,
            'titulo_despues': _titulo_nuevo,
            'baseline':       _mon_baseline,
            'snapshots':      [],
            'ultimo_snapshot': None,
            'applied':        ['manual'],
        })
    else:
        _mon['items'].append({
            'item_id':         item_id,
            'alias':           alias,
            'titulo_producto': _titulo_prod,
            'fecha_opt':       _now_mon,
            'titulo_antes':    _titulo_prod,
            'titulo_despues':  _titulo_nuevo,
            'baseline':        _mon_baseline,
            'snapshots':       [],
            'ultimo_snapshot': None,
            'applied':         ['manual'],
        })
    save_json(_mon_path, _mon)

    # ── Sprint 3.1: lanzar captura async del baseline COMPLETO ──────────
    try:
        from modules.baseline_capture import (
            capturar_baseline_async, marcar_capturing,
        )
        from core.account_manager import AccountManager as _AM_bc
        _client_bc = _AM_bc().get_client(alias)
        _top_kws_bc = []
        if opt_item.get('keywords_faltantes'):
            _top_kws_bc = [k.strip() for k in
                           (opt_item.get('keywords_faltantes') or '').split(',')
                           if k.strip()][:3]
        marcar_capturing(DATA_DIR, item_id, alias)
        capturar_baseline_async(item_id, alias, _client_bc, DATA_DIR,
                                top_keywords=_top_kws_bc or None)
    except Exception as e:
        app.logger.warning('[baseline_capture] async marcar_aplicado falló: %s', e)

    return jsonify({'ok': True, 'baseline': baseline})


@app.route('/api/monitor-nueva-publicacion', methods=['POST'])
def api_monitor_nueva_publicacion():
    """
    Vincula una publicación NUEVA (creada manualmente en ML) al Monitor de Evolución,
    recuperando los competidores del análisis de optimización original.
    Caso de uso: título no modificable (tiene ventas) → se crea nueva publicación.
    """
    body          = request.get_json() or {}
    alias         = body.get('alias', '').strip()
    item_id_nuevo = body.get('item_id_nuevo', '').strip().upper()
    item_id_orig  = body.get('item_id_original', '').strip().upper()

    if not alias or not item_id_nuevo:
        return jsonify({'ok': False, 'error': 'Faltan alias o item_id_nuevo'}), 400

    # Recuperar competidores del análisis original
    comps = []
    if item_id_orig:
        opt_path = os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')
        opt_data = load_json(opt_path) or {}
        orig_opt = next((o for o in opt_data.get('optimizaciones', []) if o.get('item_id') == item_id_orig), None)
        if orig_opt:
            comps = orig_opt.get('competidores_ids', [])

    try:
        token, _, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    # Obtener título de la nueva publicación desde la API de ML
    titulo_nuevo = ''
    try:
        r_item = req_lib.get(f'https://api.mercadolibre.com/items/{item_id_nuevo}',
                             headers=heads, timeout=6)
        if r_item.ok:
            titulo_nuevo = r_item.json().get('title', '')
    except Exception:
        pass

    # Capturar baseline de la nueva publicación (todo en 0 por ser nueva)
    _now = datetime.now().strftime('%Y-%m-%d %H:%M')
    baseline = {'fecha': _now, 'visitas_7d': 0, 'ventas_30d': 0, 'ventas_total': 0, 'conv_pct': 0.0, 'posicion': None}
    try:
        r_vis = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id_nuevo}/visits/time_window',
            headers=heads, params={'last': 7, 'unit': 'day'}, timeout=6)
        if r_vis.ok:
            baseline['visitas_7d'] = r_vis.json().get('total_visits', 0)
    except Exception:
        pass

    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    if not isinstance(_mon, dict):
        _mon = {'items': []}

    _cutoff = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    _mon['items'] = [it for it in _mon.get('items', []) if (it.get('fecha_opt') or '')[:10] >= _cutoff]

    existing = next((x for x in _mon['items'] if x.get('item_id') == item_id_nuevo and x.get('alias') == alias), None)
    entry = {
        'item_id':          item_id_nuevo,
        'alias':            alias,
        'titulo_producto':  titulo_nuevo or item_id_nuevo,
        'fecha_opt':        _now,
        'titulo_antes':     '',
        'titulo_despues':   titulo_nuevo,
        'origen':           'nueva',
        'item_id_original': item_id_orig,
        'baseline':         baseline,
        'snapshots':        [],
        'ultimo_snapshot':  None,
        'applied':          ['titulo', 'descripcion', 'ficha'],
        'competidores':     comps,
    }
    if existing:
        existing.update(entry)
    else:
        _mon['items'].append(entry)

    save_json(_mon_path, _mon)

    # ── Sprint 3.1 ext republicación: capturar baseline DUAL ────────────────
    # Captura async del baseline completo de MLA-Y (la nueva) Y de MLA-X (la
    # original) en el mismo thread. La función baseline_capture toma
    # publicacion_original_mla y guarda automáticamente en
    # entry.publicacion_original.snapshot_al_republicar.
    try:
        from modules.baseline_capture import (
            capturar_baseline_async, marcar_capturing,
        )
        from core.account_manager import AccountManager as _AM_rep
        _client_rep = _AM_rep().get_client(alias)

        # Top 3 keywords del análisis original (de MLA-X) si existe
        _top_kws_rep = []
        if item_id_orig:
            try:
                _opt_d_rep = load_json(os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')) or {}
                _opt_match_rep = next((o for o in _opt_d_rep.get('optimizaciones', [])
                                       if o.get('item_id') == item_id_orig), None)
                if _opt_match_rep and _opt_match_rep.get('keywords_faltantes'):
                    _top_kws_rep = [k.strip() for k in
                                    (_opt_match_rep.get('keywords_faltantes') or '').split(',')
                                    if k.strip()][:3]
            except Exception:
                pass

        marcar_capturing(DATA_DIR, item_id_nuevo, alias)
        capturar_baseline_async(
            item_id_nuevo, alias, _client_rep, DATA_DIR,
            top_keywords=_top_kws_rep or None,
            publicacion_original_mla=item_id_orig or None,
        )
    except Exception as e:
        app.logger.warning('[baseline_capture] async republicación falló: %s', e)

    return jsonify({
        'ok':            True,
        'titulo':        titulo_nuevo,
        'competidores_n': len(comps),
        'baseline':      baseline,
    })


@app.route('/api/monitor-iniciar', methods=['POST'])
def api_monitor_iniciar():
    """Registra un ítem en Monitor de Evolución para seguimiento, sin requerir que esté aplicado."""
    body    = request.get_json() or {}
    alias   = body.get('alias', '').strip()
    item_id = body.get('item_id', '').strip().upper()
    titulo  = body.get('titulo_actual', '').strip()
    comps   = body.get('competidores', [])

    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Faltan alias o item_id'}), 400

    try:
        token, _, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    _now = datetime.now().strftime('%Y-%m-%d %H:%M')
    baseline = {'fecha': _now}

    # Visitas 7d (igual que el resto del monitor)
    try:
        r_vis = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
            headers=heads, params={'last': 7, 'unit': 'day'}, timeout=6)
        if r_vis.ok:
            baseline['visitas_7d'] = r_vis.json().get('total_visits', 0)
    except Exception:
        pass

    # Ventas acumuladas
    try:
        r_item = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}',
                             headers=heads, timeout=6)
        if r_item.ok:
            baseline['ventas_total'] = r_item.json().get('sold_quantity', 0)
    except Exception:
        pass

    # Ventas 30d y conversión desde stock local
    _stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    for _si in (_stock_data.get('items') or []):
        if _si.get('id') == item_id:
            baseline['ventas_30d'] = _si.get('ventas_30d') or 0
            baseline['conv_pct']   = _si.get('conversion_pct') or 0.0
            break

    # Posición
    pos_data = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
    if item_id in pos_data:
        hist = pos_data[item_id].get('history', {})
        if hist:
            last_date = max(hist.keys())
            pos_val   = hist[last_date]
            if pos_val != 999:
                baseline['posicion'] = pos_val

    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    if not isinstance(_mon, dict):
        _mon = {'items': []}

    _cutoff = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    _mon['items'] = [it for it in _mon.get('items', []) if (it.get('fecha_opt') or '')[:10] >= _cutoff]

    existing = next((x for x in _mon['items'] if x.get('item_id') == item_id and x.get('alias') == alias), None)

    if existing:
        # Re-registro: actualizar baseline y datos pero preservar historial de snapshots
        existing.update({
            'titulo_producto': titulo or item_id,
            'fecha_opt':       _now,
            'titulo_antes':    titulo,
            'titulo_despues':  titulo,
            'origen':          'directo',
            'baseline':        baseline,
            'competidores':    comps,
        })
    else:
        _mon['items'].append({
            'item_id':         item_id,
            'alias':           alias,
            'titulo_producto': titulo or item_id,
            'fecha_opt':       _now,
            'titulo_antes':    titulo,
            'titulo_despues':  titulo,
            'origen':          'directo',
            'baseline':        baseline,
            'snapshots':       [],
            'ultimo_snapshot': None,
            'applied':         [],
            'competidores':    comps,
        })

    save_json(_mon_path, _mon)
    return jsonify({'ok': True, 'baseline': baseline})


_STOP_WORDS = {
    'de', 'del', 'la', 'el', 'los', 'las', 'un', 'una', 'unos', 'unas',
    'con', 'para', 'por', 'en', 'y', 'o', 'a', 'al', 'sin', 'mas', 'más',
    'alta', 'alto', 'nuevo', 'nueva', 'original', 'generico', 'genérico',
}

def _seeds_from_title(title: str) -> list[str]:
    """Genera 3-5 variantes semánticas cortas del título para consultar autosuggest."""
    import re
    title = title.strip()
    # Normalizar: quitar paréntesis, barras, contenido entre corchetes
    clean = re.sub(r'[\(\[\{][^\)\]\}]{0,30}[\)\]\}]', ' ', title)
    clean = re.sub(r'\s+', ' ', clean).strip()

    words = clean.split()

    seeds = []

    # Seed 1: primeras 2 palabras significativas
    core = [w for w in words if w.lower() not in _STOP_WORDS]
    if len(core) >= 2:
        seeds.append(' '.join(core[:2]).lower())

    # Seed 2: primeras 3 palabras significativas
    if len(core) >= 3:
        seeds.append(' '.join(core[:3]).lower())

    # Seed 3: primeras 4 palabras del título original (sin filtrar stop words)
    if len(words) >= 4:
        seeds.append(' '.join(words[:4]).lower())

    # Seed 4: primeras 5 palabras del título original
    if len(words) >= 6:
        seeds.append(' '.join(words[:5]).lower())

    # Seed 5: título completo hasta 40 chars (para long-tail)
    if len(title) > 20:
        seeds.append(title[:40].rsplit(' ', 1)[0].lower())

    # Deduplicar preservando orden
    seen = set()
    result = []
    for s in seeds:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            result.append(s)

    return result[:5]


_AUTOSUGGEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Referer': 'https://www.mercadolibre.com.ar/',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'es-AR,es;q=0.9,en;q=0.8',
    'Origin': 'https://www.mercadolibre.com.ar',
}

def _ml_autosuggest(query: str, limit: int = 8) -> list[str]:
    """Devuelve sugerencias reales de búsqueda de ML para una query, ordenadas por popularidad.
    Incluye retry con backoff en caso de rate limit (429).
    """
    import time as _t_as
    for _attempt in range(3):
        try:
            r = req_lib.get(
                'https://http2.mlstatic.com/resources/sites/MLA/autosuggest',
                params={'q': query, 'limit': limit, 'lang': 'es_AR'},
                headers=_AUTOSUGGEST_HEADERS,
                timeout=6
            )
            if r.ok:
                return [s['q'] for s in r.json().get('suggested_queries', []) if s.get('q')]
            if r.status_code == 429:
                app.logger.warning('[autosuggest] rate limit (429) intento %d para query=%r — esperando %ds',
                                   _attempt + 1, query[:40], _attempt + 1)
                _t_as.sleep(_attempt + 1)
                continue
            app.logger.warning('[autosuggest] HTTP %s para query=%r', r.status_code, query[:40])
            break
        except Exception as _as_err:
            app.logger.warning('[autosuggest] excepción intento %d para query=%r: %s', _attempt + 1, query[:40], _as_err)
            if _attempt < 2:
                _t_as.sleep(0.5)
    return []


def _keywords_from_seeds(seeds: list[str], per_seed: int = 8) -> list[str]:
    """Consulta autosuggest de ML para cada seed y devuelve keywords limpias y deduplicadas."""
    import time as _time
    seen = set()
    result = []
    for seed in seeds:
        suggestions = _ml_autosuggest(seed, limit=per_seed)
        for kw in suggestions:
            kw_norm = kw.lower().strip()
            if kw_norm and kw_norm not in seen:
                seen.add(kw_norm)
                result.append(kw_norm)
        _time.sleep(0.15)
    return result


def _check_keyword_position(item_id: str, keyword: str, token: str, max_results: int = 100) -> dict:
    """
    Busca el item_id en los resultados de ML para una keyword.
    Devuelve:
      {
        'keyword':    str,
        'position':   int | None,   # None si no aparece
        'top_competitors': [{'id', 'title', 'price', 'sold_quantity'}],  # top 5
      }
    """
    import time as _time
    headers    = {'Authorization': f'Bearer {token}'}
    PAGE_SIZE  = 50
    pages      = (max_results + PAGE_SIZE - 1) // PAGE_SIZE
    position   = None
    top_items  = []

    for page in range(pages):
        offset = page * PAGE_SIZE
        try:
            resp = req_lib.get(
                'https://api.mercadolibre.com/sites/MLA/search',
                headers=headers,
                params={'q': keyword, 'limit': PAGE_SIZE, 'offset': offset},
                timeout=10,
            )
            if not resp.ok:
                break
            body    = resp.json()
            results = body.get('results', [])

            if page == 0:
                top_items = [
                    {
                        'id':            r.get('id', ''),
                        'title':         r.get('title', ''),
                        'price':         r.get('price', 0),
                        'sold_quantity': r.get('sold_quantity', 0),
                    }
                    for r in results[:5]
                ]

            for i, r in enumerate(results):
                if r.get('id') == item_id:
                    position = offset + i + 1
                    break

            if position is not None or len(results) < PAGE_SIZE:
                break
            _time.sleep(0.15)
        except Exception:
            break

    return {
        'keyword':        keyword,
        'position':       position,
        'top_competitors': top_items,
    }


def _check_positions_for_keywords(item_id: str, keywords: list[str], token: str) -> list[dict]:
    """Llama _check_keyword_position para cada keyword y devuelve los resultados."""
    import time as _time
    results = []
    for kw in keywords:
        results.append(_check_keyword_position(item_id, kw, token))
        _time.sleep(0.2)
    return results


@app.route('/api/buscar-posicion', methods=['POST'])
def api_buscar_posicion():
    """
    Busca en qué posición aparece un item para una keyword dada usando la API oficial de ML.
    Body: {item_id, keyword, alias}
    Returns: {posicion, keyword, total}
    """
    data    = request.get_json() or {}
    item_id = data.get('item_id', '').strip()
    keyword = data.get('keyword', '').strip()
    alias   = data.get('alias', '').strip()
    if not item_id or not keyword:
        return jsonify({'ok': False, 'error': 'item_id y keyword requeridos'}), 400

    # Obtener token de acceso
    token = ''
    try:
        from core.account_manager import AccountManager
        mgr    = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token  = client.account.access_token
    except Exception:
        pass

    if not token:
        return jsonify({'ok': False, 'error': 'No se pudo obtener token para esta cuenta'}), 400

    NOT_FOUND  = 999
    SEARCH_PAGES = 6   # 6 × 50 = 300 resultados
    PAGE_SIZE    = 50
    headers      = {'Authorization': f'Bearer {token}'}
    posicion     = NOT_FOUND
    total        = 0

    for page in range(SEARCH_PAGES):
        offset = page * PAGE_SIZE
        try:
            resp = req_lib.get(
                'https://api.mercadolibre.com/sites/MLA/search',
                headers=headers,
                params={'q': keyword, 'limit': PAGE_SIZE, 'offset': offset},
                timeout=10
            )
            if not resp.ok:
                break
            body    = resp.json()
            results = body.get('results', [])
            if page == 0:
                total = body.get('paging', {}).get('total', 0)
            for i, item in enumerate(results):
                if item.get('id') == item_id:
                    posicion = offset + i + 1
                    break
            if posicion != NOT_FOUND:
                break
            if len(results) < PAGE_SIZE:
                break
            _time_module.sleep(0.2)
        except Exception:
            break

    return jsonify({
        'ok':      True,
        'item_id': item_id,
        'keyword': keyword,
        'posicion': posicion,
        'total':   total,
    })


@app.route('/api/diagnostico-item', methods=['POST'])
def api_diagnostico_item():
    """
    Diagnóstico profundo con IA de una publicación específica.
    Usa todos los datos reales disponibles: stock, posiciones, reputación, competencia + autosuggest ML.
    """
    body    = request.get_json() or {}
    item_id = body.get('item_id', '').strip().upper()
    alias   = body.get('alias', '').strip()
    if not item_id or not alias:
        return jsonify({'ok': False, 'error': 'Falta item_id o alias'}), 400

    try:
        # ── Cargar todos los datos locales ────────────────────────────────────
        stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
        pos_data   = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
        rep_data   = load_json(os.path.join(DATA_DIR, f'reputacion_{safe(alias)}.json')) or []
        comp_data  = load_json(os.path.join(DATA_DIR, f'competencia_{safe(alias)}.json')) or {}
        costos_data= load_json(os.path.join(CONFIG_DIR, 'costos.json')) or {}

        # Stock metrics
        my_stock = next((i for i in stock_data.get('items', []) if i.get('id') == item_id), {})
        if not my_stock:
            return jsonify({'ok': False, 'error': 'Publicación no encontrada en datos de stock. Actualizá primero.'}), 404

        titulo       = my_stock.get('titulo', '')
        precio       = my_stock.get('precio', 0) or 0
        visitas_30d  = my_stock.get('visitas_30d', 0) or 0
        ventas_30d   = my_stock.get('ventas_30d', 0) or 0
        conv_pct     = my_stock.get('conversion_pct', 0) or 0
        velocidad    = my_stock.get('velocidad', 0) or 0
        dias_stock   = my_stock.get('dias_stock', 0) or 0
        stock_qty    = my_stock.get('stock', 0) or 0
        listing_type = my_stock.get('listing_type', '')
        free_ship    = my_stock.get('free_shipping', False)
        alerta_stock = my_stock.get('alerta_stock', '')
        fee_rate     = my_stock.get('fee_rate', 0) or 0
        neto         = precio * (1 - fee_rate)
        costo_entry  = costos_data.get(item_id, {})
        costo        = costo_entry.get('costo') if costo_entry else my_stock.get('costo')
        margen_pct   = ((neto - costo) / precio * 100) if costo and precio > 0 else None
        is_premium   = listing_type in ('gold_special', 'gold_pro')

        # Posición
        pos_item    = pos_data.get(item_id, {})
        pos_history = pos_item.get('history', {})
        pos_sorted  = sorted(pos_history.items())
        pos_current = pos_sorted[-1][1] if pos_sorted else None
        pos_trend   = None
        if len(pos_sorted) >= 2:
            prev, curr = pos_sorted[-2][1], pos_sorted[-1][1]
            pos_trend = 'subiendo' if curr < prev else ('bajando' if curr > prev else 'estable')

        # Reputación
        rep_latest      = rep_data[-1] if rep_data else {}
        rep_reclamos    = rep_latest.get('reclamos_pct', 0) or 0
        rep_demoras     = rep_latest.get('demoras_pct', 0) or 0
        rep_nivel       = rep_latest.get('nivel', '')
        rep_ps          = rep_latest.get('power_seller', '')

        # Competidores y keywords faltantes
        comp_titles      = []
        kw_faltantes     = []
        for cat_val in comp_data.get('categorias', {}).values():
            for pub in cat_val.get('mis_publicaciones', []):
                if pub.get('id') == item_id:
                    kw_faltantes = pub.get('keywords_faltantes', [])
                    comp_titles  = cat_val.get('competidores', [])[:5]
                    break

        # Autosuggest real de ML para este producto
        stopwords = {'de','para','con','sin','y','el','la','los','las','un','una','por','al','del'}
        title_words = [w for w in titulo.lower().split() if len(w) > 3 and w not in stopwords]
        queries_as = list({
            ' '.join(title_words[:2]) if len(title_words) >= 2 else '',
            title_words[0] if title_words else '',
        } - {''})

        import time as _tia
        ml_keywords = []
        seen_as = set()
        for q in queries_as[:2]:
            for s in _ml_autosuggest(q, limit=10):
                if s not in seen_as:
                    seen_as.add(s)
                    ml_keywords.append(s)
            _tia.sleep(0.1)

        # ── Detectar bottleneck principal ──────────────────────────────────
        if visitas_30d == 0:
            bottleneck = 'SIN DATOS DE VISITAS'
        elif visitas_30d < 40 and conv_pct >= 4:
            bottleneck = f'TRÁFICO — solo {visitas_30d} visitas/mes, conversión buena ({conv_pct:.1f}%)'
        elif visitas_30d >= 40 and conv_pct < 2:
            bottleneck = f'CONVERSIÓN — {visitas_30d} visitas/mes pero solo {conv_pct:.1f}% compran'
        elif visitas_30d < 40 and conv_pct < 2:
            bottleneck = f'CRÍTICO — pocas visitas ({visitas_30d}/mes) Y baja conversión ({conv_pct:.1f}%)'
        else:
            bottleneck = f'ESCALADO — {visitas_30d} visitas/mes con {conv_pct:.1f}% conversión. Escalar tráfico.'

        # ── Armar prompt para Claude ───────────────────────────────────────
        pos_str = f'#{pos_current}' if pos_current and pos_current != 999 else 'No aparece (>50)'
        if pos_trend:
            pos_str += f' ({pos_trend})'

        prompt = f"""Sos el mejor experto en MercadoLibre Argentina. Analizá estos datos REALES de una publicación y generá un diagnóstico específico, conciso y con acciones concretas.

PUBLICACIÓN: {titulo}
ID: {item_id}

MÉTRICAS REALES (últimos 30 días):
- Visitas: {visitas_30d:,}/mes ({(visitas_30d/30):.1f}/día)
- Ventas: {ventas_30d:,}/mes
- Conversión: {conv_pct:.1f}%
- Velocidad: {velocidad:.2f} u/día
- Stock: {stock_qty} unidades (~{int(dias_stock)} días)
- Precio: ${precio:,.0f}
- Margen neto: {f'{margen_pct:.1f}%' if margen_pct is not None else 'sin datos de costo'}
- Tipo publicación: {'PREMIUM' if is_premium else 'CLÁSICA'}
- Envío gratis: {'Sí' if free_ship else 'No'}
- Alerta stock: {alerta_stock or 'ninguna'}

POSICIÓN EN BÚSQUEDAS: {pos_str}

REPUTACIÓN DEL VENDEDOR:
- Reclamos: {rep_reclamos:.1f}% (límite ML: 2%)
- Demoras: {rep_demoras:.1f}%
- Nivel: {rep_nivel} | Power Seller: {rep_ps or 'no'}

BOTTLENECK DETECTADO: {bottleneck}

KEYWORDS REALES DEL MOTOR ML (autosuggest — ordenadas por popularidad):
{chr(10).join(f'  {i+1}. "{s}"' for i, s in enumerate(ml_keywords)) or '  (no disponible)'}

KEYWORDS QUE USAN COMPETIDORES Y VOS NO TENÉS:
{chr(10).join(f'  · "{k}"' for k in kw_faltantes[:6]) or '  (sin datos — correr módulo Competencia)'}

COMPETIDORES EN LA CATEGORÍA:
{chr(10).join(f'  {i+1}. {t}' for i, t in enumerate(comp_titles[:4])) or '  (sin datos)'}

TÍTULO ACTUAL ({len(titulo)} chars de 60):
{titulo}

---

Generá un diagnóstico en este formato EXACTO (sin introducción, directo al contenido):

🔴 PROBLEMA PRINCIPAL
[1 oración con el bottleneck y los números exactos]

📊 SITUACIÓN ACTUAL
· [métrica 1 con número real y veredicto]
· [métrica 2 con número real y veredicto]
· [métrica 3 con número real y veredicto]
(máximo 5 bullets, solo los más relevantes)

🎯 ACCIONES PRIORITARIAS (ordenadas por impacto)
1. [acción específica con números — ej: "Agregar 'postparto' al título — es la 2da búsqueda más popular en ML para este producto"]
2. [acción específica con números]
3. [acción específica con números]
(máximo 4 acciones, específicas, accionables hoy)

💡 OPORTUNIDAD DETECTADA
[1 oportunidad concreta que no es obvia — basada en los datos]"""

        ai_client = anthropic.Anthropic()
        msg = ai_client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=900,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_token_usage('Salud Catálogo — Diagnóstico IA', 'claude-sonnet-4-6', msg.usage.input_tokens, msg.usage.output_tokens)
        return jsonify({'ok': True, 'diagnostico': msg.content[0].text.strip()})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/ml-autosuggest')
def api_ml_autosuggest():
    """Proxy para el autosuggest de ML — usado desde el frontend."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'ok': False, 'suggestions': []})
    suggestions = _ml_autosuggest(q, limit=10)
    return jsonify({'ok': True, 'suggestions': suggestions})


@app.route('/api/keywords-research', methods=['POST'])
def api_keywords_research():
    """
    1. Llama al autosuggest REAL de ML para obtener sugerencias ordenadas por popularidad.
    2. Llama a Claude para clasificarlas y agregar análisis de uso en título/descripción.
    3. Si se provee item_id + alias, llama a track_positions() para saber dónde rankea el item.
    Devuelve JSON con: titulo[], descripcion[], long_tail[], ml_suggestions[], consejo, positions{}.
    """
    body     = request.get_json() or {}
    producto = body.get('producto', '').strip()
    alias    = body.get('alias', '').strip()
    item_id  = body.get('item_id', '').strip()
    if not producto:
        return jsonify({'ok': False, 'error': 'Falta nombre de producto'}), 400

    # ── 1. Datos reales de ML autosuggest ─────────────────────────────────────
    # Lanzamos 3 queries: la original + variantes cortas para mayor cobertura
    import time as _t
    queries_to_try = [producto]
    words = producto.split()
    if len(words) > 2:
        queries_to_try.append(' '.join(words[:2]))
    if len(words) > 1:
        queries_to_try.append(words[0])

    seen = set()
    ml_suggestions = []
    for q in queries_to_try:
        for s in _ml_autosuggest(q, limit=10):
            if s not in seen:
                seen.add(s)
                ml_suggestions.append(s)
        _t.sleep(0.1)

    # ── 1b. Posicionamiento real del item en esas búsquedas ──────────────────
    positions: dict = {}
    if item_id and alias:
        try:
            from modules.seo_optimizer import track_positions as _track_pos
            _tok, _, __ = _ml_auth(alias)
            pos_results = _track_pos(item_id, ml_suggestions[:8], _tok)
            positions   = {r['keyword']: r for r in pos_results}
        except Exception:
            pass  # posiciones no disponibles — no bloquea el resto

    # ── 2. Claude clasifica y analiza las sugerencias reales ──────────────────
    ml_block = '\n'.join(f'  {i+1}. "{s}"' for i, s in enumerate(ml_suggestions)) or '  (no disponible)'

    prompt = f"""Sos un experto en optimización de publicaciones en MercadoLibre Argentina.

Producto analizado: "{producto}"

SUGERENCIAS REALES DE BÚSQUEDA DE ML (ordenadas por popularidad — dato real del motor de ML):
{ml_block}

Estas son las frases exactas que los compradores escriben en MercadoLibre. Basándote en ellas, respondé ÚNICAMENTE con un JSON válido:

{{
  "titulo": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"],
  "descripcion": ["variante1", "variante2", "variante3", "variante4", "variante5"],
  "long_tail": ["frase larga 1", "frase larga 2", "frase larga 3"],
  "consejo": "Consejo concreto de 1-2 oraciones para posicionar este producto en ML Argentina."
}}

Reglas estrictas:
- "titulo": las 5 palabras/frases MÁS BUSCADAS (priorizá las de mayor popularidad según el orden de las sugerencias de ML). Máximo 3 palabras cada una. Sin artículos.
- "descripcion": variantes semánticas y sinónimos que aparecen en las sugerencias + términos complementarios para usar en el cuerpo de la descripción.
- "long_tail": las 3 frases más largas y específicas de la lista ML (alta intención de compra). Si no hay suficientes en la lista, agregá 1-2 basadas en tu conocimiento del mercado.
- "consejo": mencioná específicamente qué sugerencia de ML tiene mayor potencial y por qué.
- Respondé SOLO con el JSON. Sin texto adicional. Sin markdown."""

    try:
        ai_client = anthropic.Anthropic()
        msg = ai_client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_token_usage('Lanzar Producto — Keywords', 'claude-sonnet-4-6', msg.usage.input_tokens, msg.usage.output_tokens)
        raw = msg.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        parsed = json.loads(raw)
        parsed['ok'] = True
        parsed['ml_suggestions'] = ml_suggestions
        parsed['positions'] = positions
        return jsonify(parsed)
    except json.JSONDecodeError as e:
        # Si Claude falla, al menos devolvemos las sugerencias crudas de ML
        return jsonify({
            'ok': True,
            'titulo': ml_suggestions[:5],
            'descripcion': ml_suggestions[5:10],
            'long_tail': [s for s in ml_suggestions if len(s.split()) >= 3][:3],
            'ml_suggestions': ml_suggestions,
            'consejo': 'Sugerencias obtenidas directamente del motor de búsqueda de ML.',
            'positions': positions,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/optimizaciones/<alias>')
def optimizaciones(alias):
    data  = load_json(os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json'))
    stock = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json'))
    items = (data or {}).get('optimizaciones', [])

    # Publicaciones disponibles para optimizar (de stock o de competencia)
    comp  = load_json(os.path.join(DATA_DIR, f'competencia_{safe(alias)}.json'))
    pubs_disponibles = []
    if comp:
        for cat in comp.get('categorias', {}).values():
            for p in cat.get('mis_publicaciones', []):
                pid = p.get('id','') or p.get('item_id','')
                if pid:
                    pubs_disponibles.append({'id': pid, 'titulo': p.get('titulo','')})
    elif stock:
        for p in stock.get('items', []):
            pid = p.get('id','') or p.get('item_id','')
            if pid:
                pubs_disponibles.append({'id': pid, 'titulo': p.get('titulo','')})

    return render_template('optimizaciones.html', alias=alias, items=items,
                           pubs_disponibles=pubs_disponibles,
                           fecha=(data or {}).get('fecha'), accounts=get_accounts())


def _eval_scoring(kws: list, competitors: list) -> dict:
    """
    Scoring heurístico para evaluar oportunidad de lanzamiento.
    Usa solo señales observables: autosuggest y datos de competidores.
    Todos los scores son 0-100.
    """
    n_kws   = len(kws)
    n_comps = len(competitors)

    # ── demand_score — cuánta demanda real hay ───────────────────────────────
    # Heurística top-heavy: primeras sugerencias valen más (ML las ordena por popularidad)
    # + bonus por diversidad semántica (palabras únicas entre keywords)
    if n_kws == 0:
        demand_score = 0
    else:
        # Peso decreciente: pos 1=10, 2=9, ..., 10=1, resto=0.5
        position_score = sum(max(10 - i, 0.5) for i in range(n_kws))
        max_possible   = sum(max(10 - i, 0.5) for i in range(20))  # normalizar sobre 20 kws
        base = (position_score / max_possible) * 70  # base máx 70 pts

        # Bonus diversidad: palabras únicas en el conjunto de keywords
        all_words = set(w for kw in kws for w in kw.lower().split() if len(w) > 3)
        diversity   = min(30, len(all_words) * 1.5)  # máx 30 pts

        demand_score = min(100, int(base + diversity))

    # ── saturation_score — qué tan saturado está el mercado ──────────────────
    # Señal: % de competidores con envío gratis + listing premium + ventas altas
    if n_comps == 0:
        saturation_score = 10  # sin competidores visibles = mercado muy nuevo
    else:
        premium_count  = sum(1 for c in competitors if c.get('listing_type_id') in ('gold_special', 'gold_pro'))
        freeship_count = sum(1 for c in competitors if c.get('free_shipping'))
        high_sales     = sum(1 for c in competitors if c.get('sold_quantity', 0) > 500)
        ratio = (premium_count + freeship_count + high_sales) / (n_comps * 3)
        saturation_score = min(100, int(ratio * 100))

    # ── price_competitiveness_score — dispersión de precios ──────────────────
    # Alta dispersión = hay espacio para posicionarse en precio
    prices = [c['price'] for c in competitors if c.get('price', 0) > 0]
    if len(prices) < 2:
        price_competitiveness_score = 50
    else:
        avg   = sum(prices) / len(prices)
        spread = (max(prices) - min(prices)) / avg if avg > 0 else 0
        # spread > 0.5 = mucha dispersión = oportunidad
        price_competitiveness_score = min(100, int(spread * 100))

    # ── differentiation_score — espacio para diferenciarse ───────────────────
    # Señal inversa a saturación + dispersión de precios
    differentiation_score = min(100, max(0, 100 - saturation_score + int(price_competitiveness_score * 0.3)))

    # ── difficulty_score — dificultad general de entrada ─────────────────────
    # Combina saturación y fuerza de competidores
    avg_sold = (sum(c.get('sold_quantity', 0) for c in competitors) / n_comps) if n_comps else 0
    sold_penalty = min(70, int(avg_sold / 80))
    difficulty_score = min(100, int(saturation_score * 0.6 + sold_penalty))

    # ── decision_preliminar ───────────────────────────────────────────────────
    # Lanzar si: demand alta, dificultad manejable, algún espacio de diferenciación
    if demand_score >= 60 and difficulty_score <= 55 and differentiation_score >= 35:
        decision = 'LANZAR'
    elif demand_score >= 40 and difficulty_score <= 75:
        decision = 'LANZAR CON CAUTELA'
    else:
        decision = 'NO LANZAR'

    return {
        'demand_score':               demand_score,
        'saturation_score':           saturation_score,
        'price_competitiveness_score': price_competitiveness_score,
        'differentiation_score':      differentiation_score,
        'difficulty_score':           difficulty_score,
        'decision_preliminar':        decision,
    }


def _analyze_campaign_with_claude(campaign: dict) -> str:
    """Llama a Claude para analizar una campaña y devolver recomendación concreta."""
    metrics    = campaign.get('metrics', {})
    acos       = metrics.get('acos')
    acos_target = campaign.get('acos_target', 10.0)
    budget     = campaign.get('budget', 0.0)
    spend      = metrics.get('spend', 0.0)
    revenue    = metrics.get('revenue_ads', 0.0)
    conversions = metrics.get('conversions', 0)
    clicks     = metrics.get('clicks', 0)
    impressions = metrics.get('impressions', 0)
    ctr        = metrics.get('ctr', 0.0)
    roas       = metrics.get('roas')
    status     = campaign.get('status', '')

    if not metrics:
        return 'Sin métricas disponibles para analizar.'

    acos_str = f'{acos:.1%}' if acos is not None else 'sin ventas registradas'
    roas_str = f'{roas:.1f}x' if roas is not None else 'sin dato'

    prompt = f"""Sos un experto en MercadoLibre Product Ads Argentina. Analizá esta campaña y dá una recomendación concreta.

CAMPAÑA: {campaign.get('name', campaign.get('id'))}
Estado: {status}
Presupuesto diario: ${budget:,.0f} ARS
Estrategia: {campaign.get('strategy', 'N/A')} | Objetivo ACoS: {acos_target}%
Items en campaña: {campaign.get('items_count', 0)}

MÉTRICAS (últimos 30 días):
- Impresiones: {impressions:,}
- Clicks: {clicks:,}  |  CTR: {ctr:.2%}
- Gasto total: ${spend:,.0f} ARS
- Revenue generado por ads: ${revenue:,.0f} ARS
- Conversiones: {conversions}
- ACoS actual: {acos_str}
- ROAS: {roas_str}

Respondé en español. Sé directo y específico. Incluí:
1. Evaluación en 1 línea (¿la campaña está bien o mal?)
2. Acción concreta recomendada con números exactos (ej: "Subir presupuesto a $12.000/día" o "Pausar — ACoS 45% supera target")
3. Motivo en 1-2 líneas

Máximo 120 palabras. Sin asteriscos ni markdown."""

    try:
        client   = anthropic.Anthropic()
        response = client.messages.create(
            model='claude-opus-4-6',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}],
        )
        _log_token_usage('Meli ADS — Análisis campaña', 'claude-opus-4-6', response.usage.input_tokens, response.usage.output_tokens)
        return response.content[0].text.strip()
    except Exception as e:
        return f'Error al conectar con Claude: {e}'


@app.route('/meli-ads')
def meli_ads():
    from modules.meli_ads_engine import build_campaigns_from_api
    from datetime import date, timedelta

    api_status = {
        'api_connected':   False,
        'advertiser_id':   None,
        'campaigns_count': 0,
        'ads_count':       0,
        'warnings':        [],
        'auth_error':      False,
    }
    campaigns = []

    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr      = AccountManager()
        _accounts = _mgr.list_accounts()
        if _accounts:
            _client = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
            _client._ensure_token()          # auto-refresh si está vencido
            _token     = _client.account.access_token
            _date_to   = date.today().strftime('%Y-%m-%d')
            _date_from = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')

            campaigns, _meta = build_campaigns_from_api(_token, _date_from, _date_to)
            api_status['api_connected']   = True
            api_status['advertiser_id']   = _meta.get('advertiser_id')
            api_status['campaigns_count'] = _meta.get('campaigns_count', 0)
            api_status['ads_count']       = _meta.get('ads_count', 0)
            api_status['warnings']        = _meta.get('warnings', [])
            api_status['auth_error']      = _meta.get('auth_error', False)

            # Análisis Claude se carga lazily por cada campaña desde el frontend

            account_alias = _accounts[0].alias
        else:
            api_status['warnings'].append('No hay cuentas de MercadoLibre conectadas.')
            account_alias = None
    except Exception as _e:
        api_status['warnings'].append(f'Error al cargar campañas: {_e}')
        account_alias = None

    return render_template('meli_ads.html', campaigns=campaigns, api_status=api_status,
                           account_alias=account_alias, accounts=get_accounts())


@app.route('/api/meli-ads/campaign/<int:camp_id>/analysis')
def api_meli_ads_campaign_analysis(camp_id: int):
    """Genera análisis Claude para una campaña (llamada lazy desde el frontend)."""
    from modules.meli_ads_engine import build_campaigns_from_api
    from datetime import date, timedelta
    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr      = AccountManager()
        _accounts = _mgr.list_accounts()
        if not _accounts:
            return jsonify({'ok': False, 'analysis': 'Sin cuentas conectadas.'})
        _client = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
        _client._ensure_token()
        _token     = _client.account.access_token
        _date_to   = date.today().strftime('%Y-%m-%d')
        _date_from = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
        campaigns, _ = build_campaigns_from_api(_token, _date_from, _date_to)
        camp = next((c for c in campaigns if c['id'] == camp_id), None)
        if not camp:
            return jsonify({'ok': False, 'analysis': 'Campaña no encontrada.'})
        analysis = _analyze_campaign_with_claude(camp)
        return jsonify({'ok': True, 'analysis': analysis})
    except Exception as e:
        return jsonify({'ok': False, 'analysis': f'Error: {e}'})


@app.route('/api/meli-ads/campaign/update-budget', methods=['POST'])
def api_meli_ads_update_budget():
    """Actualiza el presupuesto diario de una campaña vía ML API."""
    from modules.meli_ads_engine import update_campaign_budget
    payload     = request.get_json(silent=True) or {}
    campaign_id = payload.get('campaign_id')
    new_budget  = payload.get('budget')

    if not campaign_id or not new_budget:
        return jsonify({'ok': False, 'mensaje': 'Parámetros inválidos (campaign_id y budget requeridos).'})
    try:
        campaign_id = int(campaign_id)
        new_budget  = float(new_budget)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'mensaje': 'campaign_id y budget deben ser numéricos.'})

    if new_budget <= 0:
        return jsonify({'ok': False, 'mensaje': 'El presupuesto debe ser mayor a 0.'})

    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr = AccountManager()
        _accounts = _mgr.list_accounts()
        if not _accounts:
            return jsonify({'ok': False, 'mensaje': 'Sin cuenta de MercadoLibre conectada.'})
        _client = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
        _client._ensure_token()
        result = update_campaign_budget(_client.account.access_token, campaign_id, new_budget)
        return jsonify({'ok': result['ok'], 'mensaje': result['message']})
    except Exception as e:
        return jsonify({'ok': False, 'mensaje': str(e)})


@app.route('/api/meli-ads/campaign/update-status', methods=['POST'])
def api_meli_ads_update_status():
    """Activa o pausa una campaña vía ML API."""
    from modules.meli_ads_engine import update_campaign_status
    payload     = request.get_json(silent=True) or {}
    campaign_id = payload.get('campaign_id')
    status      = str(payload.get('status', '')).strip().lower()

    if not campaign_id or status not in ('active', 'paused'):
        return jsonify({'ok': False, 'mensaje': 'Parámetros inválidos. status: "active" | "paused".'})
    try:
        campaign_id = int(campaign_id)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'mensaje': 'campaign_id debe ser numérico.'})

    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr = AccountManager()
        _accounts = _mgr.list_accounts()
        if not _accounts:
            return jsonify({'ok': False, 'mensaje': 'Sin cuenta de MercadoLibre conectada.'})
        _client = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
        _client._ensure_token()
        result = update_campaign_status(_client.account.access_token, campaign_id, status)
        return jsonify({'ok': result['ok'], 'mensaje': result['message']})
    except Exception as e:
        return jsonify({'ok': False, 'mensaje': str(e)})


@app.route('/api/meli-ads/campaign/<int:camp_id>/items')
def api_meli_ads_campaign_items(camp_id: int):
    """Devuelve señales de calidad + recomendación de movimiento para cada ítem."""
    from modules.meli_ads_engine import get_campaign_items_detail, _get_user_id, _discover_campaign_ids
    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr     = AccountManager()
        _accounts = _mgr.list_accounts()
        if not _accounts:
            return jsonify({'ok': False, 'mensaje': 'Sin cuenta conectada.'})
        _client  = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
        _client._ensure_token()
        token    = _client.account.access_token

        user_id, _, _ = _get_user_id(token)
        if not user_id:
            return jsonify({'ok': False, 'mensaje': 'No se pudo obtener user_id.'})

        campaign_map = _discover_campaign_ids(token, user_id)
        item_ids     = campaign_map.get(camp_id, [])
        if not item_ids:
            return jsonify({'ok': True, 'items': [], 'total': 0})

        items = get_campaign_items_detail(token, item_ids)

        # ── Ventas 30d desde snapshot (más preciso que sold_quantity histórico) ─
        alias      = _accounts[0].alias
        stock_snap = db_load(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
        stock_map  = {s.get('id', ''): s for s in stock_snap.get('items', [])}

        def _ventas30(iid: str) -> float:
            s = stock_map.get(iid, {})
            v = s.get('ventas_30d')
            if v is not None:
                return float(v)
            vel = s.get('velocidad')
            if vel is not None:
                return round(float(vel) * 30, 1)
            return 0.0

        # ── Jerarquía de campañas por nombre ────────────────────────────────
        def _camp_tier(name: str) -> int:
            n = name.lower()
            if 'top 1' in n or 'top1' in n:   return 1
            if 'top' in n:                      return 2
            if 'impulso' in n or 'boost' in n:  return 3
            return 4

        all_camp_ids = list(campaign_map.keys())
        from modules.meli_ads_engine import _get_campaign_detail
        camp_meta: dict[int, dict] = {}
        for cid in all_camp_ids:
            try:
                camp_meta[cid] = _get_campaign_detail(token, cid)
                _time_module.sleep(0.05)
            except Exception:
                camp_meta[cid] = {'name': str(cid)}

        camps_ranked    = sorted(all_camp_ids,
                                 key=lambda c: _camp_tier(camp_meta.get(c, {}).get('name', '')))
        current_rank    = camps_ranked.index(camp_id) if camp_id in camps_ranked else 0
        camp_above      = camps_ranked[current_rank - 1] if current_rank > 0 else None
        camp_below      = camps_ranked[current_rank + 1] if current_rank < len(camps_ranked) - 1 else None
        camp_above_name = camp_meta.get(camp_above, {}).get('name', '') if camp_above else ''
        camp_below_name = camp_meta.get(camp_below, {}).get('name', '') if camp_below else ''

        # ── Eficiencia publicitaria: conversion_pct × precio ────────────────
        # Proxy de ingreso estimado por click. Es la métrica más honesta que
        # podemos calcular sin acceso a datos por ítem de la API de ads.
        price_map = {it.get('id', ''): float(it.get('price', 0) or 0) for it in items}

        def _eff_weight(iid: str) -> float:
            s     = stock_map.get(iid, {})
            conv  = s.get('conversion_pct')          # almacenado como %, ej: 5.2
            price = float(s.get('precio') or 0) or price_map.get(iid, 0)
            if not conv or not price:
                return 0.0
            return round((float(conv) / 100.0) * price, 2)

        camp_weights     = [_eff_weight(iid) for iid in item_ids]
        camp_avg_weight  = (sum(camp_weights) / len(camp_weights)) if camp_weights else 0
        total_weight     = sum(camp_weights) or 1.0  # evitar /0

        def _fmt_w(w: float) -> str:
            return f'${int(w):,}'.replace(',', '.') if w > 0 else '?'

        # ── Generar recomendación por ítem ──────────────────────────────────
        # Fusión de dos señales:
        #   1. Color ML (verde/amarillo/rojo) — dato real de ads, indica visibilidad
        #   2. Conversión × precio           — nuestra estimación de valor por click
        def _recommend(item: dict) -> dict:
            iid     = item.get('id', '')
            level   = item.get('level', '')
            bb      = item.get('buy_box', False)
            no_snap = iid not in stock_map
            s       = stock_map.get(iid, {})
            conv    = s.get('conversion_pct')
            w       = _eff_weight(iid)

            # ── Señal 1: color ML (rojo = ML frena el anuncio) ───────────────
            if level == 'red' and not bb:
                return {
                    'tipo': 'quitar', 'color': '#dc2626', 'bg': '#fee2e2',
                    'texto': 'ML califica ROJO y sin Buy Box — el anuncio no se está mostrando y hay un competidor adelante. Pausar hasta resolver ficha y precio.',
                    'accion': 'Quitar de campaña',
                }
            if level == 'red':
                return {
                    'tipo': 'revisar', 'color': '#dc2626', 'bg': '#fee2e2',
                    'texto': 'ML califica ROJO — el algoritmo de ads está limitando la visibilidad de este anuncio. Mejorar fotos, título y descripción antes de seguir invirtiendo.',
                    'accion': None,
                }

            # ── Señal 1: sin Buy Box (competidor adelante) ───────────────────
            if not bb:
                return {
                    'tipo': 'revisar', 'color': '#d97706', 'bg': '#fef3c7',
                    'texto': 'Sin Buy Box — hay un competidor con mejor precio o reputación. Cada click del anuncio termina viendo al competidor primero. Recuperar Buy Box antes de invertir más.',
                    'accion': None,
                }

            # ── Sin datos de conversión — no se puede aplicar señal 2 ────────
            if no_snap or conv is None:
                nivel_str = 'VERDE' if level == 'green' else 'AMARILLO' if level == 'yellow' else level.upper()
                return {
                    'tipo': 'ok', 'color': '#64748b', 'bg': '#f8fafc',
                    'texto': f'ML califica {nivel_str}, Buy Box ✓. Sin datos de conversión para estimar eficiencia — el sistema actualizará mañana a las 7 AM.',
                    'accion': None,
                }

            conv_f  = float(conv)
            w_str   = _fmt_w(w)
            avg_str = _fmt_w(camp_avg_weight)

            # ── Señal 1 AMARILLO + señal 2 baja → doble problema ─────────────
            if level == 'yellow' and camp_avg_weight > 0 and w < camp_avg_weight * 0.4:
                texto = (f'ML califica AMARILLO (visibilidad limitada) y la conversión {conv_f:.1f}% '
                         f'→ {w_str}/click está por debajo del promedio de la campaña ({avg_str}/click). '
                         f'Doble señal negativa: ML lo penaliza y convierte poco.')
                if camp_below:
                    return {
                        'tipo': 'bajar', 'color': '#2563eb', 'bg': '#eff6ff',
                        'texto': texto + f' Mover a {camp_below_name} para reducir la inversión mientras se corrige la ficha.',
                        'accion': f'Mover a {camp_below_name}',
                        'camp_id': camp_below, 'camp_name': camp_below_name,
                    }
                return {'tipo': 'revisar', 'color': '#d97706', 'bg': '#fef3c7',
                        'texto': texto, 'accion': None}

            # ── Señal 1 AMARILLO + señal 2 ok → mejorar ficha primero ────────
            if level == 'yellow':
                return {
                    'tipo': 'revisar', 'color': '#d97706', 'bg': '#fef3c7',
                    'texto': f'ML califica AMARILLO — el algoritmo de ads está limitando la visibilidad. Conversión {conv_f:.1f}% ({w_str}/click) es razonable, pero no se aprovecha porque ML no muestra el anuncio con frecuencia. Mejorar ficha para pasar a VERDE.',
                    'accion': None,
                }

            # ── A partir de acá: VERDE + Buy Box (señal 1 OK) ────────────────
            # La decisión la toma únicamente la señal 2: conversión × precio

            # Verde + eficiencia alta → subir campaña
            if camp_above and camp_avg_weight > 0 and w >= camp_avg_weight * 2:
                return {
                    'tipo': 'subir', 'color': '#15803d', 'bg': '#dcfce7',
                    'texto': (f'ML califica VERDE ✓ y conversión {conv_f:.1f}% → {w_str}/click '
                              f'({round(w / camp_avg_weight, 1)}× el promedio de la campaña). '
                              f'Ambas señales positivas: candidato para subir a {camp_above_name} y darle más presupuesto.'),
                    'accion': f'Mover a {camp_above_name}',
                    'camp_id': camp_above, 'camp_name': camp_above_name,
                }

            # Verde + eficiencia baja → bajar campaña
            if camp_below and camp_avg_weight > 0 and w < camp_avg_weight * 0.35:
                return {
                    'tipo': 'bajar', 'color': '#2563eb', 'bg': '#eff6ff',
                    'texto': (f'ML califica VERDE pero la conversión {conv_f:.1f}% → {w_str}/click '
                              f'está muy por debajo del promedio ({avg_str}/click). '
                              f'El listing está bien pero cada click genera poco ingreso. Mover a {camp_below_name} para reducir inversión.'),
                    'accion': f'Mover a {camp_below_name}',
                    'camp_id': camp_below, 'camp_name': camp_below_name,
                }

            # Verde + eficiencia normal → sin acción
            return {
                'tipo': 'ok', 'color': '#64748b', 'bg': '#f8fafc',
                'texto': (f'ML califica VERDE ✓, Buy Box ✓, conversión {conv_f:.1f}% → {w_str}/click '
                          f'(promedio campaña: {avg_str}/click). Sin acciones urgentes.'),
                'accion': None,
            }

        # ── Métricas de campaña recibidas del frontend ──────────────────────
        camp_spend   = float(request.args.get('spend',       0) or 0)
        camp_revenue = float(request.args.get('revenue',     0) or 0)
        camp_convs   = float(request.args.get('conversions', 0) or 0)
        acos_target  = float(request.args.get('acos_target', 10) or 10) / 100

        for item in items:
            iid   = item.get('id', '')
            v30   = _ventas30(iid)
            w     = _eff_weight(iid)
            s     = stock_map.get(iid, {})
            conv  = s.get('conversion_pct')

            item['ventas_30d']    = v30
            item['sold_quantity'] = s.get('sold_quantity', None)
            item['rec']           = _recommend(item)

            # Proporción basada en eficiencia (conv × precio), no en unidades
            pct         = w / total_weight if total_weight > 0 else 0
            est_spend   = round(camp_spend   * pct, 0)
            est_revenue = round(camp_revenue * pct, 0)
            est_convs   = round(camp_convs   * pct, 1)
            est_roas    = round(est_revenue / est_spend, 2) if est_spend > 0 else None
            est_acos    = round(est_spend / est_revenue, 3) if est_revenue > 0 else None

            # Motor / Freno / Neutro — fusión de ambas señales
            level = item.get('level', '')
            bb    = item.get('buy_box', False)
            if level == 'green' and bb and w >= camp_avg_weight * 1.5:
                rol = 'motor'   # Verde ML + alta eficiencia por click
            elif level == 'red' or not bb or w == 0 or (camp_avg_weight > 0 and w <= camp_avg_weight * 0.4):
                rol = 'freno'   # Rojo ML, sin BB, sin conversión o muy baja eficiencia
            else:
                rol = 'neutro'

            item['contrib'] = {
                'pct':             round(pct * 100, 1),
                'est_spend':       est_spend,
                'est_revenue':     est_revenue,
                'est_convs':       est_convs,
                'est_roas':        est_roas,
                'est_acos_pct':    round(est_acos * 100, 1) if est_acos else None,
                'acos_ok':         (est_acos <= acos_target) if est_acos else None,
                'rol':             rol,
                'conv_pct':        round(float(conv), 1) if conv is not None else None,
                'eff_weight':      round(w, 0),
                'camp_avg_weight': round(camp_avg_weight, 0),
            }

        return jsonify({'ok': True, 'items': items, 'total': len(items),
                        'camp_avg_weight': round(camp_avg_weight, 0)})
    except Exception as e:
        return jsonify({'ok': False, 'mensaje': str(e)})


@app.route('/api/meli-ads/item/move', methods=['POST'])
def api_meli_ads_item_move():
    """Mueve un ítem a otra campaña."""
    from modules.meli_ads_engine import move_item_to_campaign
    payload     = request.get_json(silent=True) or {}
    item_id     = str(payload.get('item_id', '')).strip()
    new_camp_id = payload.get('campaign_id')

    if not item_id or not new_camp_id:
        return jsonify({'ok': False, 'mensaje': 'Parámetros inválidos (item_id y campaign_id requeridos).'})
    try:
        new_camp_id = int(new_camp_id)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'mensaje': 'campaign_id debe ser numérico.'})

    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr     = AccountManager()
        _accounts = _mgr.list_accounts()
        if not _accounts:
            return jsonify({'ok': False, 'mensaje': 'Sin cuenta conectada.'})
        _client  = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
        _client._ensure_token()
        result = move_item_to_campaign(_client.account.access_token, item_id, new_camp_id)
        return jsonify({'ok': result['ok'], 'mensaje': result['message'],
                        'needs_reauth': result.get('needs_reauth', False)})
    except Exception as e:
        return jsonify({'ok': False, 'mensaje': str(e)})


@app.route('/api/meli-ads/item/remove', methods=['POST'])
def api_meli_ads_item_remove():
    """Elimina un ítem de su campaña de Product Ads."""
    from modules.meli_ads_engine import remove_item_from_campaign
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get('item_id', '')).strip()

    if not item_id:
        return jsonify({'ok': False, 'mensaje': 'Falta item_id.'})

    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr     = AccountManager()
        _accounts = _mgr.list_accounts()
        if not _accounts:
            return jsonify({'ok': False, 'mensaje': 'Sin cuenta conectada.'})
        _client  = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
        _client._ensure_token()
        result = remove_item_from_campaign(_client.account.access_token, item_id)
        return jsonify({'ok': result['ok'], 'mensaje': result['message'],
                        'needs_reauth': result.get('needs_reauth', False)})
    except Exception as e:
        return jsonify({'ok': False, 'mensaje': str(e)})


@app.route('/api/meli-ads/distribution-analysis')
def api_meli_ads_distribution():
    """
    Analiza si cada producto está en la campaña correcta en base a su volumen de ventas.
    Tarda ~30-60s en ejecutarse (muchas llamadas a API). Se llama solo cuando el usuario
    presiona el botón de análisis.
    """
    from modules.meli_ads_engine import (analyze_distribution, _get_user_id,
                                         _discover_campaign_ids, _get_campaign_detail)
    try:
        from core.account_manager import AccountManager
        from core.ml_client import MLClient
        _mgr      = AccountManager()
        _accounts = _mgr.list_accounts()
        if not _accounts:
            return jsonify({'ok': False, 'mensaje': 'Sin cuenta conectada.'})
        _client   = MLClient(_accounts[0], on_token_refresh=_mgr._on_token_refresh)
        _client._ensure_token()
        token     = _client.account.access_token

        user_id, _, _ = _get_user_id(token)
        if not user_id:
            return jsonify({'ok': False, 'mensaje': 'No se pudo obtener user_id.'})

        campaign_map = _discover_campaign_ids(token, user_id)
        if not campaign_map:
            return jsonify({'ok': False, 'mensaje': 'No se encontraron campañas.'})

        # Detalles de campañas (nombre, budget, etc.)
        camp_details = {cid: _get_campaign_detail(token, cid) for cid in campaign_map}

        result = analyze_distribution(token, campaign_map, camp_details)
        return jsonify({'ok': True, **result})

    except Exception as e:
        return jsonify({'ok': False, 'mensaje': str(e)})


@app.route('/evaluar-producto')
def evaluar_producto():
    pattern  = os.path.join(DATA_DIR, 'evaluacion_*.json')
    archivos = sorted(glob.glob(pattern), reverse=True)
    historial = []
    for path in archivos[:10]:
        data = load_json(path)
        if data:
            historial.append(data)
    return render_template('evaluar_producto.html', historial=historial, accounts=get_accounts())


@app.route('/api/evaluar-producto', methods=['POST'])
def api_evaluar_producto():
    from modules.lanzador_productos import _gather_market_data

    body  = request.get_json() or {}
    query = body.get('query', '').strip()
    if not query:
        return jsonify({'ok': False, 'error': 'Falta query'}), 400

    seeds = _seeds_from_title(query)
    kws   = _keywords_from_seeds(seeds, per_seed=10)[:20]

    # Detectar categoría y competidores usando lógica existente
    category_id   = ''
    category_name = ''
    competitors   = []
    try:
        accounts = get_accounts()
        token    = accounts[0]['access_token'] if accounts else ''
        if token:
            market_data   = _gather_market_data(query, token)
            category_id   = market_data.get('suggested_category_id', '')
            category_name = market_data.get('suggested_category_name', '')

            hdrs = {'Authorization': f'Bearer {token}'}
            sr = req_lib.get(
                'https://api.mercadolibre.com/sites/MLA/search',
                headers=hdrs,
                params={'q': query, 'limit': 5, 'sort': 'sold_quantity_desc'},
                timeout=10,
            )
            if sr.ok:
                for r in sr.json().get('results', []):
                    competitors.append({
                        'title':           r.get('title', ''),
                        'price':           r.get('price', 0),
                        'sold_quantity':   r.get('sold_quantity', 0),
                        'free_shipping':   bool((r.get('shipping') or {}).get('free_shipping', False)),
                        'listing_type_id': r.get('listing_type_id', ''),
                    })
    except Exception as e:
        app.logger.warning(f'[evaluar-producto] Error obteniendo datos de ML: {e}')

    # ── Scoring heurístico (sin IA) ──────────────────────────────────────────
    scores = _eval_scoring(kws, competitors)

    # ── Dictamen IA ──────────────────────────────────────────────────────────
    ai_eval = {'decision': '', 'why': [], 'main_risks': [], 'recommended_angle': ''}
    try:
        comps_str = '\n'.join(
            f'- "{c["title"]}" | ${c["price"]:,.0f} | ventas: {c.get("sold_quantity", 0)} | '
            f'envío gratis: {"Sí" if c.get("free_shipping") else "No"} | tipo: {c.get("listing_type_id", "")}'
            for c in competitors
        ) or '(sin competidores detectados)'

        prompt = f"""Evaluá la oportunidad de lanzar este producto en Mercado Libre Argentina.

Producto: "{query}"
Categoría: {category_name or '(desconocida)'}

Keywords del autosuggest ({len(kws)} encontradas):
{', '.join(kws[:12])}

Competidores top por ventas:
{comps_str}

Señales de mercado (0-100):
- Demanda estimada: {scores['demand_score']}
- Saturación actual: {scores['saturation_score']}
- Dispersión de precios: {scores['price_competitiveness_score']}
- Espacio de diferenciación: {scores['differentiation_score']}
- Dificultad de entrada: {scores['difficulty_score']}

Respondé SOLO con este JSON, sin texto adicional:
{{
  "decision": "LANZAR" | "LANZAR CON CAUTELA" | "NO LANZAR",
  "why": ["razón 1", "razón 2", "razón 3"],
  "main_risks": ["riesgo 1", "riesgo 2"],
  "recommended_angle": "ángulo de diferenciación concreto en una oración"
}}"""

        ai   = anthropic.Anthropic()
        resp = ai.messages.create(
            model='claude-opus-4-6',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        _log_token_usage('Evaluar Producto — Dictamen IA', 'claude-opus-4-6', resp.usage.input_tokens, resp.usage.output_tokens)
        import re as _re, json as _json
        raw = next((b.text for b in resp.content if hasattr(b, 'text')), '')
        m   = _re.search(r'\{[\s\S]+\}', raw)
        if m:
            parsed = _json.loads(m.group(0))
            # Validar estructura antes de usar
            valid = (
                isinstance(parsed.get('decision'), str) and
                isinstance(parsed.get('why'), list) and
                isinstance(parsed.get('main_risks'), list) and
                isinstance(parsed.get('recommended_angle'), str)
            )
            if valid:
                ai_eval = parsed
            else:
                app.logger.warning(f'[evaluar-producto] JSON de Claude con estructura inválida: {list(parsed.keys())}')
                # Fallback parcial: rescatar lo que sea válido
                ai_eval = {
                    'decision':          parsed.get('decision', '') if isinstance(parsed.get('decision'), str) else '',
                    'why':               parsed.get('why', [])       if isinstance(parsed.get('why'), list)   else [],
                    'main_risks':        parsed.get('main_risks', []) if isinstance(parsed.get('main_risks'), list) else [],
                    'recommended_angle': parsed.get('recommended_angle', '') if isinstance(parsed.get('recommended_angle'), str) else '',
                }
    except Exception as e:
        app.logger.warning(f'[evaluar-producto] Error en dictamen IA: {e}')

    # ── Persistir evaluación ──────────────────────────────────────────────────
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        safe_q = query[:30].replace(' ', '_').replace('/', '-')
        ts     = datetime.now().strftime('%Y%m%d_%H%M')
        path   = os.path.join(DATA_DIR, f'evaluacion_{safe_q}_{ts}.json')
        save_json(path, {
                'fecha':               datetime.now().strftime('%Y-%m-%d %H:%M'),
                'query':               query,
                'category_id':         category_id,
                'category':            category_name,
                'autosuggest_kws':     kws,
                'competitors':         competitors,
                'scores':              scores,
                'ai_eval':             ai_eval,
            })
    except Exception as e:
        app.logger.warning(f'[evaluar-producto] Error guardando evaluación: {e}')

    return jsonify({'ok': True, 'query': query, 'autosuggest_kws': kws,
                    'category_id': category_id, 'category': category_name,
                    'competitors': competitors, 'scores': scores,
                    'decision_preliminar': scores['decision_preliminar'],
                    'ai_eval': ai_eval})


@app.route('/lanzamientos')
def lanzamientos():
    pattern = os.path.join(DATA_DIR, 'lanzamiento_*.json')
    archivos = sorted(glob.glob(pattern), reverse=True)
    lanzamientos_list = []
    for path in archivos[:10]:
        data = load_json(path)
        if data:
            mercado = data.get('mercado', {}) or data.get('market_data', {})
            lanzamientos_list.append({
                'archivo':              os.path.basename(path),
                'producto':             data.get('producto', ''),
                'fecha':                data.get('fecha', ''),
                'analisis':             data.get('analisis_claude', '') or data.get('analisis', ''),
                'analisis_competencia': data.get('analisis_competencia', ''),
                'mercado':              mercado,
                'titulos':              data.get('titulos_alt', []),
                'ficha':                data.get('ficha_perfecta', ''),
                'descripcion':          data.get('descripcion_nueva', ''),
                'proyeccion':           data.get('proyeccion', ''),
                'comps_count':          len(data.get('competidores_reales', [])),
                'cat_name':             mercado.get('suggested_category_name', ''),
            })
    return render_template('lanzamientos.html', lanzamientos=lanzamientos_list,
                           accounts=get_accounts())


@app.route('/repricing/<alias>')
def repricing(alias):
    data  = load_json(os.path.join(CONFIG_DIR, 'repricing.json'))
    items = []
    if data:
        for item_id, cfg in data.get('items', {}).items():
            if cfg.get('alias') == alias:
                margen = cfg.get('margen_min_pct', 0) * 100
                items.append({
                    'id':         item_id,
                    'titulo':     cfg.get('titulo', '')[:55],
                    'precio':     cfg.get('precio_actual', 0),
                    'precio_min': cfg.get('precio_min', 0),
                    'precio_max': cfg.get('precio_max', 0),
                    'margen':     margen,
                })
    return render_template('repricing.html', alias=alias, items=items,
                           accounts=get_accounts())


@app.route('/api/debug-items/<alias>')
def api_debug_items(alias):
    """Diagnóstico temporal: muestra todos los items con sus status y catalog_product_id."""
    try:
        from core.account_manager import AccountManager
        mgr = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token = client.account.access_token
        heads = {'Authorization': f'Bearer {token}'}
        user_data = req_lib.get('https://api.mercadolibre.com/users/me', headers=heads, timeout=10).json()
        user_id = user_data.get('id')

        summary = {}
        all_ids_by_status = {}

        for status in ('active', 'paused', 'under_review', 'inactive', 'closed'):
            r = req_lib.get(f'https://api.mercadolibre.com/users/{user_id}/items/search',
                            headers=heads,
                            params={'status': status, 'limit': 10, 'offset': 0},
                            timeout=10)
            if r.ok:
                data = r.json()
                total = data.get('paging', {}).get('total', 0)
                ids = data.get('results', [])
                summary[status] = total
                all_ids_by_status[status] = ids[:5]  # first 5 ids as sample

        # Fetch details of first 20 across all statuses to see catalog_product_id
        sample_ids = []
        for ids in all_ids_by_status.values():
            sample_ids.extend(ids)
        sample_ids = sample_ids[:20]

        catalog_items = []
        if sample_ids:
            det = req_lib.get('https://api.mercadolibre.com/items', headers=heads,
                              params={'ids': ','.join(sample_ids),
                                      'attributes': 'id,title,status,catalog_product_id'},
                              timeout=10)
            if det.ok:
                for d in det.json():
                    body = d.get('body', {})
                    if body.get('catalog_product_id'):
                        catalog_items.append({
                            'id': body['id'],
                            'title': body.get('title','')[:50],
                            'status': body.get('status'),
                            'catalog_product_id': body.get('catalog_product_id'),
                        })

        return jsonify({'summary_by_status': summary, 'sample_ids': all_ids_by_status,
                        'catalog_items_in_sample': catalog_items})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/repricing-items/<alias>')
def api_repricing_items(alias):
    """Devuelve publicaciones activas de la cuenta para configurar repricing."""
    try:
        from core.account_manager import AccountManager
        mgr = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token = client.account.access_token
        heads = {'Authorization': f'Bearer {token}'}
        user_data = req_lib.get('https://api.mercadolibre.com/users/me',
                                headers=heads, timeout=10).json()
        user_id = user_data.get('id')

        # Cargar config existente
        cfg = load_json(os.path.join(CONFIG_DIR, 'repricing.json')) or {'items': {}}

        items = []
        all_ids = []

        # Recolectar IDs activos + pausados (catálogo puede quedar paused)
        for status in ('active', 'paused'):  # paused incluido porque catálogo a veces queda paused
            offset = 0
            while True:
                r = req_lib.get(f'https://api.mercadolibre.com/users/{user_id}/items/search',
                                headers=heads,
                                params={'status': status, 'limit': 50, 'offset': offset},
                                timeout=10)
                if not r.ok:
                    break
                data = r.json()
                ids = data.get('results', [])
                if not ids:
                    break
                all_ids.extend(ids)
                offset += len(ids)
                total = data.get('paging', {}).get('total', 0)
                if offset >= total or offset >= 300:
                    break
                _time_module.sleep(0.1)

        # Deduplicar y fetch en lotes de 20
        seen_ids = set()
        unique_ids = [i for i in all_ids if not (i in seen_ids or seen_ids.add(i))]

        for batch_start in range(0, len(unique_ids), 20):
            batch = unique_ids[batch_start:batch_start + 20]
            ids_str = ','.join(batch)
            det = req_lib.get('https://api.mercadolibre.com/items',
                              headers=heads,
                              params={'ids': ids_str,
                                      'attributes': 'id,title,price,category_id,catalog_product_id,status'},
                              timeout=10)
            if det.ok:
                for d in det.json():
                    body = d.get('body', {})
                    item_id = body.get('id', '')
                    if not item_id:
                        continue
                    existing = cfg['items'].get(item_id, {})
                    price = float(body.get('price', 0))
                    items.append({
                        'id': item_id,
                        'titulo': body.get('title', '')[:60],
                        'precio': price,
                        'precio_min': existing.get('precio_min', round(price * 0.85, 0)),
                        'precio_max': existing.get('precio_max', round(price * 1.20, 0)),
                        'configurado': item_id in cfg['items'],
                        'is_catalog': bool(body.get('catalog_product_id')),
                        'catalog_product_id': body.get('catalog_product_id'),
                        'status': body.get('status', 'active'),
                        'buy_box_price': None,
                        'buy_box_winners': 0,
                        'we_win_buy_box': False,
                    })
            _time_module.sleep(0.1)

        # Para items de catálogo, obtener el precio del buy box actual
        for it in items:
            cpid = it.get('catalog_product_id')
            if not cpid:
                continue
            try:
                r_pi = req_lib.get(
                    f'https://api.mercadolibre.com/products/{cpid}/items',
                    headers=heads, params={'limit': 10}, timeout=8)
                if r_pi.ok:
                    catalog_sellers = r_pi.json().get('results', [])
                    it['buy_box_winners'] = len(catalog_sellers)
                    if catalog_sellers:
                        # El primero en la lista es el ganador del buy box
                        winner = catalog_sellers[0]
                        winner_id = winner.get('id') or winner.get('item_id', '')
                        it['buy_box_price'] = float(winner.get('price') or 0)
                        it['we_win_buy_box'] = (winner_id == it['id'])
                        # Precio mínimo sugerido: buy_box_price * 0.90 si no hay config previa
                        if not it['configurado']:
                            it['precio_min'] = round(it['buy_box_price'] * 0.90, 0)
                            it['precio_max'] = round(it['buy_box_price'] * 1.15, 0)
            except Exception:
                pass
            _time_module.sleep(0.2)

        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/repricing-config', methods=['POST'])
def api_repricing_config():
    """Guarda configuración de min/max por item desde la web."""
    try:
        payload = request.get_json()
        alias = payload.get('alias', '')
        items_data = payload.get('items', [])  # [{id, titulo, precio, precio_min, precio_max}]

        cfg = load_json(os.path.join(CONFIG_DIR, 'repricing.json')) or {'items': {}}

        for it in items_data:
            item_id = it['id']
            existing = cfg['items'].get(item_id, {})
            cfg['items'][item_id] = {
                **existing,
                'titulo': it.get('titulo', ''),
                'precio_actual': it.get('precio', existing.get('precio_actual', 0)),
                'precio_min': float(it.get('precio_min', 0)),
                'precio_max': float(it.get('precio_max', 0)),
                'margen_min_pct': 0,
                'alias': alias,
            }

        save_json(os.path.join(CONFIG_DIR, 'repricing.json'), cfg)

        return jsonify({'ok': True, 'saved': len(items_data)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/repricing-simulate/<alias>')
def api_repricing_simulate(alias):
    """Simula repricing sin aplicar cambios. Devuelve resultados por item."""
    try:
        from core.account_manager import AccountManager
        mgr = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token = client.account.access_token
        heads = {'Authorization': f'Bearer {token}'}

        cfg = load_json(os.path.join(CONFIG_DIR, 'repricing.json')) or {'items': {}}
        items_cfg = {k: v for k, v in cfg['items'].items() if v.get('alias') == alias}

        if not items_cfg:
            return jsonify({'ok': False, 'error': 'No hay items configurados para esta cuenta.'})

        fees = get_fee_rates(client)  # refresca si tiene >7 días
        results = []

        for item_id, item_cfg in items_cfg.items():
            titulo = item_cfg.get('titulo', item_id)
            min_p  = float(item_cfg.get('precio_min', 0))
            max_p  = float(item_cfg.get('precio_max', 999999))
            costo  = item_cfg.get('costo')

            # Precio actual desde ML + catalog_product_id
            catalog_product_id = None
            cat_id = ''
            try:
                r_item = req_lib.get(
                    f'https://api.mercadolibre.com/items/{item_id}',
                    headers=heads,
                    params={'attributes': 'id,price,category_id,catalog_product_id'},
                    timeout=8)
                item_data = r_item.json()
                current_price = float(item_data.get('price', item_cfg.get('precio_actual', 0)))
                cat_id = item_data.get('category_id', '')
                catalog_product_id = item_data.get('catalog_product_id')
            except Exception:
                current_price = float(item_cfg.get('precio_actual', 0))

            # Precio del competidor
            competitor_price = None
            is_catalog = False
            we_win_buy_box = False
            total_sellers = 0
            match_quality = 'none'

            if catalog_product_id:
                # Publicación de catálogo → comparación exacta del mismo producto
                try:
                    r_pi = req_lib.get(
                        f'https://api.mercadolibre.com/products/{catalog_product_id}/items',
                        headers=heads, params={'limit': 10}, timeout=8)
                    if r_pi.ok:
                        catalog_items = r_pi.json().get('results', [])
                        is_catalog = True
                        total_sellers = len(catalog_items)
                        match_quality = 'exact'
                        buy_box_winner_id = None
                        min_comp_price = None
                        for ci in catalog_items:
                            ci_id = ci.get('id') or ci.get('item_id', '')
                            ci_price = float(ci.get('price') or 0)
                            if not buy_box_winner_id:
                                buy_box_winner_id = ci_id  # primero = ganador buy box (ordenados por ML)
                            if ci_id and ci_id != item_id and ci_price > 0:
                                if min_comp_price is None or ci_price < min_comp_price:
                                    min_comp_price = ci_price
                        we_win_buy_box = (buy_box_winner_id == item_id)
                        competitor_price = min_comp_price
                except Exception:
                    pass

            if not is_catalog and cat_id:
                # Publicación fuera de catálogo → búsqueda por categoría (imprecisa)
                try:
                    sr = req_lib.get('https://api.mercadolibre.com/sites/MLA/search',
                                     headers=heads,
                                     params={'category': cat_id, 'sort': 'price_asc', 'limit': 5},
                                     timeout=8)
                    if sr.ok:
                        comps = [r2 for r2 in sr.json().get('results', []) if r2.get('id') != item_id]
                        if comps:
                            competitor_price = float(comps[0]['price'])
                            match_quality = 'approximate'
                            total_sellers = len(comps)
                except Exception:
                    pass

            # Calcular nuevo precio usando comisión real de la API de ML
            listing_type = item_data.get('listing_type_id', 'gold_special') if item_data else 'gold_special'
            fee_rate = get_rate(listing_type, fees)
            if costo:
                min_p = max(min_p, round(costo / (1 - fee_rate), 2))

            if competitor_price is None:
                new_price = min(round(current_price * 1.02, 2), max_p)
                razon = 'Sin competidor → +2%' if new_price != current_price else 'Sin competidor, ya en máximo'
            elif competitor_price < current_price:
                target = round(competitor_price * 0.99, 2)
                new_price = max(target, min_p)
                razon = f'Competidor ${competitor_price:,.0f} → bajamos -1%' if new_price > min_p else f'Comp ${competitor_price:,.0f} → precio mínimo'
            elif competitor_price > current_price * 1.05:
                new_price = min(round(competitor_price * 0.98, 2), max_p)
                razon = f'Competidor ${competitor_price:,.0f} más caro → subimos'
                if new_price <= current_price:
                    new_price = current_price
                    razon = f'Precio óptimo (comp: ${competitor_price:,.0f})'
            else:
                new_price = current_price
                razon = f'Precio óptimo (comp: ${competitor_price:,.0f})'

            delta = new_price - current_price
            results.append({
                'id': item_id,
                'titulo': titulo,
                'precio_actual': current_price,
                'precio_nuevo': new_price,
                'precio_min': min_p,
                'precio_max': max_p,
                'competidor': competitor_price,
                'razon': razon,
                'delta': round(delta, 2),
                'cambia': abs(delta) > 0.5,
                'is_catalog': is_catalog,
                'catalog_product_id': catalog_product_id,
                'we_win_buy_box': we_win_buy_box,
                'total_sellers': total_sellers,
                'match_quality': match_quality,
            })
            _time_module.sleep(0.15)

        return jsonify({'ok': True, 'results': results,
                        'cambios': sum(1 for r in results if r['cambia'])})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/repricing-apply', methods=['POST'])
def api_repricing_apply():
    """Aplica cambios de precio directamente en MercadoLibre."""
    try:
        payload = request.get_json()
        alias = payload.get('alias', '')
        changes = payload.get('changes', [])  # [{id, precio_nuevo}]

        from core.account_manager import AccountManager
        mgr = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token = client.account.access_token
        heads = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        cfg = load_json(os.path.join(CONFIG_DIR, 'repricing.json')) or {'items': {}}

        applied = []
        errors = []
        for ch in changes:
            item_id = ch['id']
            new_price = float(ch['precio_nuevo'])
            try:
                r = req_lib.put(f'https://api.mercadolibre.com/items/{item_id}',
                                headers=heads,
                                json={'price': new_price},
                                timeout=10)
                if r.ok:
                    applied.append(item_id)
                    if item_id in cfg['items']:
                        cfg['items'][item_id]['precio_actual'] = new_price
                else:
                    errors.append({'id': item_id, 'error': r.json().get('message', r.status_code)})
            except Exception as e:
                errors.append({'id': item_id, 'error': str(e)})
            _time_module.sleep(0.2)

        save_json(os.path.join(CONFIG_DIR, 'repricing.json'), cfg)

        if applied:
            _audit('REPRICING_APPLY', alias=alias, items_actualizados=len(applied), errores=len(errors))
        return jsonify({'ok': True, 'applied': len(applied), 'errors': errors})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ── Herramientas ──────────────────────────────────────────────────────────────

@app.route('/calendario')
def calendario():
    eventos = build_calendario()
    return render_template('calendario.html', eventos=eventos,
                           accounts=get_accounts())


@app.route('/alertas')
def alertas():
    return render_template('alertas.html', accounts=get_accounts())


@app.route('/duplicados/<alias>')
def duplicados(alias):
    """Detector de publicaciones duplicadas / canibalización por cuenta."""
    from modules.detector_duplicados import (
        detectar_duplicados, resumen_para_alertas,
    )
    stock_data  = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    stock_items = stock_data.get('items', [])

    clusters = detectar_duplicados(stock_items, alias, DATA_DIR)
    # Las dataclasses se pasan directo al template — Jinja accede por atributo.
    # NO convertir a dict porque c.items chocaría con el método dict.items() de Jinja.

    resumen = resumen_para_alertas(clusters)
    fecha_stock = (stock_data or {}).get('fecha', '')

    return render_template('duplicados.html',
                           alias=alias,
                           clusters=clusters,
                           resumen=resumen,
                           fecha_stock=fecha_stock,
                           accounts=get_accounts())


@app.route('/api/duplicados/pausar-batch', methods=['POST'])
def api_duplicados_pausar_batch():
    """Pausa en ML las publicaciones del cluster que no tengan ventas en 30 días.

    Protección dura: si una MLA tiene ventas_30d > 0, NO se pausa aunque venga
    en la lista del request. Es un proxy conservador para "ventas en los últimos
    7 días" (no tenemos ventas_7d en stock JSON; ventas_30d > 0 cubre el caso).
    """
    from modules.detector_duplicados import registrar_accion_automatica

    body       = request.get_json(silent=True) or {}
    alias      = (body.get('alias') or '').strip()
    cluster_id = (body.get('cluster_id') or '').strip()
    mlas_in    = body.get('mlas') or []

    if not alias or not isinstance(mlas_in, list) or not mlas_in:
        return jsonify({'ok': False, 'error': 'alias y mlas son requeridos'}), 400

    # Cargar stock para validar protecciones
    stock_data  = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    stock_idx   = {i.get('id'): i for i in stock_data.get('items', []) if i.get('id')}

    # La "ganadora" del cluster se calcula más abajo sobre stock_data completo,
    # no solo sobre mlas_in (las del request son las candidatas a pausar, no
    # necesariamente incluyen la ganadora).

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Auth: {e}'}), 401

    pausadas: list = []
    errores:  list = []
    protegidas: list = []
    heads_json = {**heads, 'Content-Type': 'application/json'}

    for mla in mlas_in:
        mla = (mla or '').strip().upper()
        if not mla:
            continue
        item_info = stock_idx.get(mla, {})
        ventas    = int(item_info.get('ventas_30d') or 0)

        # Protección dura: ventas_30d > 0 ⇒ no pausar
        if ventas > 0:
            protegidas.append({'mla': mla, 'ventas_30d': ventas})
            continue

        try:
            r = req_lib.put(
                f'https://api.mercadolibre.com/items/{mla}',
                headers=heads_json,
                json={'status': 'paused'},
                timeout=10,
            )
            if r.ok:
                pausadas.append(mla)
                # Registrar trazabilidad
                registrar_accion_automatica(alias, DATA_DIR, 'pausar_duplicado', {
                    'mla':         mla,
                    'cluster_id':  cluster_id,
                    'ventas_30d':  ventas,
                    'visitas_30d': int(item_info.get('visitas_30d') or 0),
                    'titulo':      (item_info.get('titulo') or '')[:120],
                    'razon':       'sin_ventas_30d_en_cluster_canibal',
                    'ejecutado_por': 'ui:/duplicados',
                })
            else:
                errores.append({'mla': mla, 'status': r.status_code,
                                'msg': (r.text or '')[:200]})
        except Exception as e:
            errores.append({'mla': mla, 'error': str(e)})
        _time_module.sleep(0.15)

    # Capturar baseline de la ganadora del cluster en monitor_evolucion (si la
    # encontramos en stock) — permite medir si la pausa beneficia su tracking.
    try:
        ganadora = max(
            (it for it in stock_data.get('items', []) if it.get('id') and int(it.get('ventas_30d') or 0) > 0),
            key=lambda x: (int(x.get('ventas_30d') or 0), int(x.get('visitas_30d') or 0)),
            default=None,
        )
        if ganadora and pausadas:
            _now_mon  = datetime.now().strftime('%Y-%m-%d %H:%M')
            _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
            _mon = load_json(_mon_path) or {'items': []}
            if not isinstance(_mon, dict):
                _mon = {'items': []}
            _baseline = {
                'fecha':       _now_mon,
                'visitas_7d':  0,
                'ventas_30d':  int(ganadora.get('ventas_30d') or 0),
                'ventas_total': 0,
                'conv_pct':    float(ganadora.get('conversion_pct') or 0),
                'posicion':    None,
                'posicion_kw': None,
            }
            gana_id = ganadora['id']
            existing = next((x for x in _mon.get('items', [])
                             if x.get('item_id') == gana_id and x.get('alias') == alias), None)
            entry_data = {
                'fecha_opt':       _now_mon,
                'titulo_antes':    ganadora.get('titulo', '')[:120],
                'titulo_despues':  ganadora.get('titulo', '')[:120],
                'baseline':        _baseline,
                'snapshots':       [],
                'ultimo_snapshot': None,
                'applied':         [f'pausa_duplicado_cluster_{cluster_id}'],
                'origen':          'pausa_duplicado',
                'cluster_id':      cluster_id,
                'mlas_pausadas':   pausadas,
            }
            if existing:
                existing.update(entry_data)
            else:
                _mon['items'].append({
                    'item_id':         gana_id,
                    'alias':           alias,
                    'titulo_producto': ganadora.get('titulo', '')[:120],
                    **entry_data,
                })
            save_json(_mon_path, _mon)
    except Exception:
        pass

    return jsonify({
        'ok':         True,
        'pausadas':   len(pausadas),
        'protegidas': len(protegidas),
        'errores':    len(errores),
        'detalle': {
            'pausadas':   pausadas,
            'protegidas': protegidas,
            'errores':    errores,
        },
    })


@app.route('/api/duplicados/ignorar-cluster', methods=['POST'])
def api_duplicados_ignorar_cluster():
    """Marca todos los pares del cluster como 'ignorados' por el usuario.

    Granularidad por par (no por cluster): si aparece un MLA nuevo en una
    futura corrida, sigue evaluándose contra los demás.
    """
    from modules.detector_duplicados import marcar_par_ignorado
    body  = request.get_json(silent=True) or {}
    alias = (body.get('alias') or '').strip()
    mlas  = body.get('mlas') or []
    razon = (body.get('razon') or '').strip()

    if not alias or not isinstance(mlas, list) or len(mlas) < 2:
        return jsonify({'ok': False, 'error': 'alias y al menos 2 mlas requeridos'}), 400

    pares_marcados = 0
    for i in range(len(mlas)):
        for j in range(i + 1, len(mlas)):
            if marcar_par_ignorado(alias, DATA_DIR, mlas[i], mlas[j], razon):
                pares_marcados += 1

    return jsonify({'ok': True, 'pares_marcados': pares_marcados})


@app.route('/api/alertas')
def api_alertas():
    """Construye todas las alertas de todas las cuentas.
    Visitas se buscan en vivo desde ML si no están en el JSON de stock.
    Posiciones compara con la fecha anterior disponible más cercana (no fija 'ayer').
    """
    accounts    = get_accounts()
    today       = datetime.now().strftime('%Y-%m-%d')
    all_alerts  = []
    U, I = 'urgente', 'importante'

    def add(nivel, alias, cat, icono, titulo, detalle, link, link_txt='Ver', item_id=None):
        all_alerts.append({'nivel': nivel, 'alias': alias, 'categoria': cat,
                           'icono': icono, 'titulo': titulo, 'detalle': detalle,
                           'link': link, 'link_txt': link_txt, 'item_id': item_id})

    fees_cache = get_fee_rates()  # lee de config/fees.json (sin refrescar)

    for acc in accounts:
        alias = acc['alias']
        s     = safe(alias)

        # ── Cargar datos ───────────────────────────────────────────────────
        stock_data  = load_json(os.path.join(DATA_DIR, f'stock_{s}.json')) or {}
        costos_data = load_json(os.path.join(CONFIG_DIR, 'costos.json')) or {}
        stock_items = stock_data.get('items', [])

        # ── Visitas en vivo si el JSON no las tiene ────────────────────────
        sin_visitas = [it for it in stock_items if it.get('visitas_30d') is None]
        visits_live = {}
        if sin_visitas:
            try:
                from core.account_manager import AccountManager
                mgr    = AccountManager()
                client = mgr.get_client(alias)
                client._ensure_token()
                token  = client.account.access_token
                heads  = {'Authorization': f'Bearer {token}'}
                for it in sin_visitas:
                    iid = it.get('id', '')
                    try:
                        r = req_lib.get(
                            f'https://api.mercadolibre.com/items/{iid}/visits/time_window',
                            headers=heads, params={'last': 30, 'unit': 'day'}, timeout=6)
                        visits_live[iid] = r.json().get('total_visits', 0) if r.ok else 0
                    except Exception:
                        visits_live[iid] = 0
                    _time_module.sleep(0.08)
            except Exception:
                pass
            # Persistir en el stock JSON para la próxima vez
            if visits_live:
                for it in stock_items:
                    iid = it.get('id', '')
                    if iid in visits_live:
                        ventas = it.get('ventas_30d') or round((it.get('velocidad') or 0) * 30)
                        vis    = visits_live[iid]
                        it['visitas_30d']   = vis
                        it['ventas_30d']    = ventas
                        it['conversion_pct'] = round(ventas / vis * 100, 2) if vis > 0 else None
                stock_path = os.path.join(DATA_DIR, f'stock_{s}.json')
                save_json(stock_path, stock_data)

        # ── Alertas de stock y margen ──────────────────────────────────────
        trafico_bajo_items = []   # agrupar en una sola alerta al final
        for it in stock_items:
            item_id = it.get('id', '')
            titulo  = it.get('titulo', '')[:55]
            precio  = float(it.get('precio', 0))
            ce      = costos_data.get(item_id, {})
            costo   = ce.get('costo') if ce else it.get('costo')
            fee     = it.get('fee_rate') or get_rate(it.get('listing_type', ''), fees_cache)
            neto    = precio * (1 - fee)
            lk      = f'/stock/{alias}'

            if costo:
                mpct = (neto - costo) / precio * 100 if precio else 0
                am   = 'NEGATIVO' if mpct < 0 else ('BAJO' if mpct < 10 else None)
            else:
                mpct = None
                am   = it.get('alerta_margen')

            ast  = it.get('alerta_stock')
            dias = it.get('dias_stock')

            if ast == 'SIN_STOCK':
                add(U, alias, 'Stock', 'bi-box-seam',
                    f'Sin stock: {titulo}',
                    'La publicación está pausada en ML. Reponé urgente.', lk, 'Ver stock', item_id)
            elif ast == 'CRITICO':
                add(U, alias, 'Stock', 'bi-exclamation-triangle',
                    f'Stock crítico: {titulo}',
                    f'Quedan {dias:.0f} días de stock. Iniciá la reposición hoy.' if dias else 'Menos de 7 días de stock.',
                    lk, 'Ver stock', item_id)
            elif ast == 'ADVERTENCIA':
                add(I, alias, 'Stock', 'bi-clock-history',
                    f'Stock bajo: {titulo}',
                    f'{dias:.0f} días de stock restantes.' if dias else 'Menos de 15 días de stock.',
                    lk, 'Ver stock', item_id)

            if am == 'NEGATIVO' and mpct is not None:
                add(U, alias, 'Margen', 'bi-currency-dollar',
                    f'Margen negativo: {titulo}',
                    f'Margen: {mpct:.1f}% — perdés dinero en cada venta.',
                    lk, 'Ver stock', item_id)
            elif am == 'BAJO' and mpct is not None:
                add(I, alias, 'Margen', 'bi-graph-down',
                    f'Margen bajo: {titulo}',
                    f'Margen: {mpct:.1f}% — por debajo del 10% recomendado.',
                    lk, 'Ver stock', item_id)

            # ── Visitas y conversión ───────────────────────────────────────
            vis  = it.get('visitas_30d')   # ya actualizado si se buscó en vivo
            conv = it.get('conversion_pct')
            vtas = it.get('ventas_30d') or round((it.get('velocidad') or 0) * 30)

            if vis is not None:
                if vis == 0:
                    add(I, alias, 'Conversión', 'bi-eye-slash',
                        f'Sin exposición: {titulo}',
                        'La publicación no recibió ninguna visita en 30 días. Revisá título y categoría.',
                        lk, 'Ver stock', item_id)
                elif vis < 100:
                    trafico_bajo_items.append(vis)   # acumular, no generar una por una
                elif vtas == 0 and vis >= 100:
                    add(U, alias, 'Conversión', 'bi-funnel',
                        f'Sin ventas con visitas: {titulo}',
                        f'{vis} visitas pero 0 ventas. Revisá precio vs competencia y foto principal.',
                        lk, 'Ver stock', item_id)
                elif conv is not None and conv < 1:
                    add(I, alias, 'Conversión', 'bi-funnel',
                        f'Conversión muy baja: {titulo}',
                        f'{conv:.1f}% de conversión con {vis} visitas. Revisá precio, fotos y descripción.',
                        lk, 'Ver stock', item_id)

        # Alerta agrupada de tráfico bajo
        if trafico_bajo_items:
            cnt = len(trafico_bajo_items)
            avg = round(sum(trafico_bajo_items) / cnt)
            add(I, alias, 'Conversión', 'bi-eye',
                f'{cnt} publicaciones con tráfico bajo — {alias}',
                f'Promedio {avg} visitas/mes (mínimo saludable: 100). Optimizá títulos, fotos y atributos desde Competencia.',
                f'/stock/{alias}', 'Ver stock')

        # ── Posiciones: comparar con fecha anterior disponible más cercana ─
        pos_data = load_json(os.path.join(DATA_DIR, f'posiciones_{s}.json')) or {}
        for item_id, d in pos_data.items():
            hist      = d.get('history', {})
            pos_hoy   = hist.get(today)
            if not pos_hoy or pos_hoy == 999:
                continue
            # Buscar la fecha previa más reciente con dato válido
            fechas_prev = sorted([f for f in hist if f < today], reverse=True)
            pos_prev = None
            fecha_prev = None
            for f in fechas_prev:
                v = hist.get(f)
                if v and v != 999:
                    pos_prev  = v
                    fecha_prev = f
                    break
            if pos_prev is None:
                continue
            delta = pos_hoy - pos_prev
            dias_entre = (datetime.strptime(today, '%Y-%m-%d') -
                          datetime.strptime(fecha_prev, '%Y-%m-%d')).days
            detalle_fecha = f'(vs {fecha_prev}, {dias_entre}d atrás)' if dias_entre > 1 else ''
            if delta >= 5:
                add(U, alias, 'Posiciones', 'bi-graph-down-arrow',
                    f'Caída fuerte de posición: {d.get("title","")[:48]}',
                    f'Bajó {delta} posiciones: {pos_prev} → {pos_hoy} {detalle_fecha}',
                    f'/posiciones/{alias}', 'Ver posiciones', item_id)
            elif delta >= 3:
                add(I, alias, 'Posiciones', 'bi-arrow-down',
                    f'Bajó posición: {d.get("title","")[:50]}',
                    f'Bajó {delta} posiciones: {pos_prev} → {pos_hoy} {detalle_fecha}',
                    f'/posiciones/{alias}', 'Ver posiciones', item_id)

        # ── Reputación ─────────────────────────────────────────────────────
        rep_data = load_json(os.path.join(DATA_DIR, f'reputacion_{s}.json'))
        rep = rep_data[-1] if rep_data else None
        if rep:
            reclamos = rep.get('reclamos_pct', 0)
            demoras  = rep.get('demoras_pct', 0)
            nivel    = rep.get('nivel', '')
            lkr      = f'/reputacion/{alias}'
            if reclamos > 2:
                add(U, alias, 'Reputación', 'bi-shield-exclamation',
                    f'Reclamos críticos — {alias}',
                    f'{reclamos:.1f}% de reclamos. Supera el 2% máximo — reputación en riesgo.',
                    lkr, 'Ver reputación')
            elif reclamos > 1:
                add(I, alias, 'Reputación', 'bi-shield-half',
                    f'Reclamos en alerta — {alias}',
                    f'{reclamos:.1f}% de reclamos. Por encima del 2% baja tu nivel.',
                    lkr, 'Ver reputación')
            if demoras > 15:
                add(I, alias, 'Reputación', 'bi-clock',
                    f'Demoras de envío — {alias}',
                    f'{demoras:.1f}% de envíos demorados (límite: 15%).',
                    lkr, 'Ver reputación')
            if nivel in ('4_rojo', '3_naranja', '2_amarillo'):
                add(I, alias, 'Reputación', 'bi-star-half',
                    f'Nivel de reputación bajo — {alias}',
                    f'Nivel: {nivel.replace("_"," ").title()}. Reducí reclamos y demoras.',
                    lkr, 'Ver reputación')

        # ── Publicaciones pausadas por ML ──────────────────────────────────
        try:
            from core.account_manager import AccountManager as _AM
            _mgr    = _AM()
            _client = _mgr.get_client(alias)
            _client._ensure_token()
            _heads  = {'Authorization': f'Bearer {_client.account.access_token}'}
            _uid    = str(_client.account.user_id)
            _r = req_lib.get(
                f'https://api.mercadolibre.com/users/{_uid}/items/search',
                headers=_heads,
                params={'status': 'paused', 'limit': 50},
                timeout=10)
            if _r.ok:
                _paused_ids = _r.json().get('results', [])
                if _paused_ids:
                    # Obtener títulos en batch
                    _batch = ','.join(_paused_ids[:20])
                    _ri = req_lib.get(
                        'https://api.mercadolibre.com/items',
                        headers=_heads,
                        params={'ids': _batch, 'attributes': 'id,title,status'},
                        timeout=10)
                    _titles = {}
                    if _ri.ok:
                        for _e in _ri.json():
                            if _e.get('code') == 200:
                                _b = _e.get('body', {})
                                _titles[_b.get('id','')] = _b.get('title','')
                    for _pid in _paused_ids[:10]:
                        _t = _titles.get(_pid, _pid)[:55]
                        add(U, alias, 'Publicaciones', 'bi-pause-circle',
                            f'Publicación pausada: {_t}',
                            'ML pausó esta publicación. Revisá stock, precio o estado en Mis publicaciones.',
                            f'https://www.mercadolibre.com.ar/anuncios/{_pid}/editar', 'Ver en ML', _pid)
                    if len(_paused_ids) > 10:
                        add(I, alias, 'Publicaciones', 'bi-pause-circle',
                            f'{len(_paused_ids) - 10} publicaciones pausadas más',
                            'Revisá todas en Mis publicaciones de ML.',
                            'https://www.mercadolibre.com.ar/vendedores/publicaciones', 'Ver todas')
        except Exception:
            pass

        # ── Detector de duplicados / canibalización ───────────────────
        try:
            from modules.detector_duplicados import detectar_duplicados
            _dup_clusters = detectar_duplicados(stock_items, alias, DATA_DIR)
            for _c in _dup_clusters:
                _nivel = U if _c.severidad == 'puro' else I
                _icono = 'bi-files' if _c.severidad == 'puro' else 'bi-shuffle'
                _impacto_txt = (
                    f' ≈ ${_c.impacto_monetario_estimado:,.0f}/mes en ventas potencialmente perdidas'
                    if _c.impacto_monetario_estimado > 0 else ''
                )
                add(_nivel, alias, 'Canibalización', _icono,
                    f"Cluster '{_c.titulo_corto[:60]}' canibalizando {_c.visitas_perdidas_30d} visitas/mes{_impacto_txt}",
                    f"{len(_c.items)} publicaciones del mismo producto compitiendo entre sí. "
                    f"Severidad: {_c.severidad}. Ver detalle para pausar las que no venden.",
                    f'/duplicados/{alias}#cluster-{_c.cluster_id}', 'Ver detalle')
        except Exception:
            pass

    orden = {'urgente': 0, 'importante': 1, 'info': 2}
    all_alerts.sort(key=lambda a: orden.get(a['nivel'], 9))
    return jsonify({
        'ok': True,
        'total': len(all_alerts),
        'urgentes':   sum(1 for a in all_alerts if a['nivel'] == 'urgente'),
        'importantes': sum(1 for a in all_alerts if a['nivel'] == 'importante'),
        'alerts': all_alerts,
    })


@app.route('/api/radar-search', methods=['POST'])
def api_radar_search():
    """
    Flujo real de búsqueda de oportunidades:
      1. domain_discovery → category_id
      2. Subir árbol de categorías hasta encontrar highlights
      3. highlights → catalog product IDs
      4. products/{id}/items → item IDs del marketplace
      5. items multiget → datos reales: título, precio, ventas, vendedor, envío
      Fallback: catalog si no hay highlights/items disponibles
    """
    body    = request.get_json(force=True)
    keyword = body.get('keyword', '').strip()
    alias   = body.get('alias', '').strip()

    if not keyword:
        return jsonify(ok=False, error='Ingresá un término de búsqueda.')

    try:
        from core.account_manager import AccountManager
        mgr    = AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token  = client.account.access_token
    except Exception as e:
        return jsonify(ok=False, error=f'No se pudo conectar con la cuenta: {e}')

    ML_BASE = 'https://api.mercadolibre.com'
    hdrs    = {'Authorization': f'Bearer {token}'}
    rich_data = False
    products  = []
    metrics   = {}

    # ── 1. domain_discovery → categorías candidatas ────────────────────────────
    r_dom = req_lib.get(f'{ML_BASE}/sites/MLA/domain_discovery/search', headers=hdrs,
                        params={'q': keyword, 'limit': 5}, timeout=8)
    dom_cats = []
    if r_dom.ok:
        for d in r_dom.json():
            cid = d.get('category_id')
            if cid and cid not in dom_cats:
                dom_cats.append(cid)

    # ── 2. Buscar highlights en la categoría exacta del keyword ──────────────
    highlight_cat = None
    hl_content    = []
    for cat_id in dom_cats[:4]:
        r_hl = req_lib.get(f'{ML_BASE}/highlights/MLA/category/{cat_id}',
                           headers=hdrs, timeout=6)
        if not (r_hl.ok and r_hl.json().get('content')):
            continue
        # Verificar que al menos un producto de esta categoría tiene items reales
        content = r_hl.json()['content']
        for test_item in content[:3]:
            r_test = req_lib.get(f'{ML_BASE}/products/{test_item["id"]}/items',
                                 headers=hdrs, params={'limit': 1}, timeout=5)
            if r_test.ok and r_test.json().get('results'):
                highlight_cat = cat_id
                hl_content    = content
                break
        if highlight_cat:
            break

    # ── 3. highlights → datos reales via products/{id} + products/{id}/items ────
    # El items multiget devuelve 403 para catalog-winner items. En cambio:
    #   products/{id}       → name, pictures (thumbnail)
    #   products/{id}/items → price, seller_id, shipping, official_store_id
    if highlight_cat:
        prod_ids = [i['id'] for i in hl_content[:12]]
        sellers = {}; prices = []; all_items_raw = []
        free_ship_cnt = full_cnt = official_cnt = 0

        for prod_id in prod_ids:
            try:
                # Datos del producto: nombre + thumbnail
                r_prod = req_lib.get(f'{ML_BASE}/products/{prod_id}',
                                     headers=hdrs,
                                     params={'attributes': 'id,name,pictures,catalog_product_id'},
                                     timeout=6)
                prod_name  = ''
                prod_thumb = ''
                prod_link  = f'https://www.mercadolibre.com.ar/p/{prod_id}'
                if r_prod.ok:
                    pd = r_prod.json()
                    prod_name  = (pd.get('name') or '')[:80]
                    pics = pd.get('pictures') or []
                    if pics:
                        prod_thumb = (pics[0].get('url') or '').replace('http://', 'https://')

                # Items del producto: precios y vendedores reales
                r_pi = req_lib.get(f'{ML_BASE}/products/{prod_id}/items',
                                   headers=hdrs, params={'limit': 3}, timeout=6)
                if r_pi.ok:
                    for it in r_pi.json().get('results', []):
                        price   = float(it.get('price') or 0)
                        sid     = it.get('seller_id')
                        ship    = it.get('shipping') or {}
                        free_sh = bool(ship.get('free_shipping'))
                        is_full = ship.get('logistic_type') == 'fulfillment'
                        is_off  = bool(it.get('official_store_id'))

                        if price > 0: prices.append(price)
                        if sid: sellers[sid] = sellers.get(sid, 0) + 1
                        if free_sh: free_ship_cnt += 1
                        if is_full: full_cnt      += 1
                        if is_off:  official_cnt  += 1

                        all_items_raw.append({
                            'id':        it.get('item_id', ''),
                            'title':     prod_name,
                            'price':     price if price > 0 else None,
                            'sold':      None,
                            'seller':    str(sid or '?'),
                            'free_ship': free_sh,
                            'is_full':   is_full,
                            'is_official': is_off,
                            'thumbnail': prod_thumb,
                            'link':      prod_link,
                            'condition': it.get('condition', 'new'),
                        })
            except Exception:
                pass
            _time_module.sleep(0.1)

        # Deduplicar: un registro por producto (el de menor precio)
        seen_titles = set()
        for it in sorted(all_items_raw, key=lambda x: x['price'] or 999999):
            title_key = it['title'][:50]
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                products.append(it)

        if products:
            rich_data = True
            n      = len(products) or 1
            n_sell = len(sellers) or 1
            ds = (2 if n_sell < 5 else 1 if n_sell < 15 else 0) + \
                 (1 if official_cnt / max(len(all_items_raw), 1) > 0.3 else 0)

            titles     = [p['title'] for p in products]
            title_lens = [len(t) for t in titles]
            short_pct  = round(sum(1 for l in title_lens if l < 45) / n * 100)
            no_fs_pct  = round((n - free_ship_cnt) / n * 100)

            # Sellers con nickname vía multiget users
            seller_ids = list(sellers.keys())[:10]
            if seller_ids:
                r_sell = req_lib.get(f'{ML_BASE}/users', headers=hdrs,
                                     params={'ids': ','.join(str(s) for s in seller_ids)},
                                     timeout=8)
                if r_sell.ok:
                    sell_resp = r_sell.json()
                    # Users multiget returns [{code, body}] or [user_obj] depending on endpoint
                    nick_map = {}
                    for entry in sell_resp:
                        if isinstance(entry, dict):
                            u = entry.get('body', entry)
                            uid = u.get('id')
                            if uid:
                                nick_map[uid] = u.get('nickname', '?')
                    for p in products:
                        try:
                            sid_int = int(p['seller'])
                            p['seller'] = nick_map.get(sid_int, p['seller'])
                        except (ValueError, TypeError):
                            pass

            metrics = {
                'total_results': len(products),
                'unique_sellers': n_sell,
                'avg_price':  round(sum(prices)/len(prices)) if prices else 0,
                'min_price':  round(min(prices)) if prices else 0,
                'max_price':  round(max(prices)) if prices else 0,
                'free_ship_pct':  round(free_ship_cnt / max(len(all_items_raw), 1) * 100),
                'full_pct':       round(full_cnt / max(len(all_items_raw), 1) * 100),
                'official_pct':   round(official_cnt / max(len(all_items_raw), 1) * 100),
                'concentration':  0,
                'has_sold_data':  False,
                'avg_sold':       None,
                'total_sold':     None,
                'difficulty': 'Alta' if ds >= 3 else ('Media' if ds >= 1 else 'Baja'),
                'data_source':    'rich',
                'category_id':    highlight_cat,
                'listing_quality': {
                    'avg_title_len':   round(sum(title_lens)/len(title_lens)) if title_lens else 0,
                    'short_title_pct': short_pct,
                    'has_photo_pct':   round(sum(1 for p in products if p['thumbnail']) / n * 100),
                    'no_freeship_pct': no_fs_pct,
                    'titles_sample':   titles[:15],
                },
                'subniche_map': [],
            }

    # ── 4. Fallback catálogo si no hay datos reales ────────────────────────────
    if not rich_data:
        # Productos del catálogo (siempre disponible)
        r_cat = req_lib.get(f'{ML_BASE}/products/search', headers=hdrs,
                            params={'site_id': 'MLA', 'q': keyword, 'limit': 12},
                            timeout=10)
        cat_results = r_cat.json().get('results', []) if r_cat.ok else []

        for item in cat_results:
            pics  = item.get('pictures', [])
            thumb = pics[0].get('url', '').replace('http://', 'https://') if pics else ''
            name  = (item.get('name') or '')[:80]
            slug  = name.lower().replace(' ', '-')
            products.append({
                'id': item.get('catalog_product_id', item.get('id', '')),
                'title': name,
                'price': None, 'sold': None, 'seller': None,
                'free_ship': None, 'is_full': None, 'is_official': None,
                'thumbnail': thumb,
                'link': f'https://listado.mercadolibre.com.ar/{slug}',
                'condition': 'new',
                'pic_count': len(pics),
            })

        # Dominios/categorías relacionados
        r_dom = req_lib.get(f'{ML_BASE}/sites/MLA/domain_discovery/search', headers=hdrs,
                            params={'q': keyword, 'limit': 5}, timeout=8)
        domains = []
        if r_dom.ok:
            for d in r_dom.json():
                dname = d.get('domain_name', '')
                if dname:
                    domains.append(dname)

        # ¿Está en trends?
        r_tr = req_lib.get(f'{ML_BASE}/trends/MLA', headers=hdrs, timeout=8)
        kw_lower  = keyword.lower()
        trending  = False
        if r_tr.ok:
            for t in r_tr.json():
                if kw_lower in t.get('keyword', '').lower():
                    trending = True
                    break

        # Análisis de calidad de publicaciones del catálogo
        cat_titles = [p['title'] for p in products]
        cat_lens   = [len(t) for t in cat_titles]
        cat_short_pct = round(sum(1 for l in cat_lens if l < 45) / len(cat_lens) * 100) if cat_lens else 0
        cat_photo_pct = round(sum(1 for p in products if p['thumbnail']) / len(products) * 100) if products else 0

        # ── Escáner de sub-nichos: calidad de competencia por variante ──────────
        # Criterio: títulos cortos = competencia mal optimizada = oportunidad real
        import re as _re
        _stop = {'para','con','sin','los','las','del','una','por','que','como',
                 'este','marca','modelo','talle','color','tipo','este','cada'}
        kw_words = set(keyword.lower().split())
        _candidates = {}

        # 1. Palabras significativas de los títulos encontrados
        for title in cat_titles[:12]:
            words = _re.findall(r'\b[a-zA-ZáéíóúÁÉÍÓÚñÑ]{5,}\b', title)
            for w in words:
                wl = w.lower()
                if wl not in kw_words and wl not in _stop:
                    q = f'{keyword} {wl}'
                    _candidates[q] = True

        # 2. Palabras de dominios encontrados
        for dom in domains:
            for w in dom.split():
                wl = w.lower()
                if wl not in kw_words and wl not in _stop and len(wl) > 4:
                    _candidates[f'{keyword} {wl}'] = True

        # Analizar calidad de la competencia en cada sub-nicho
        subniche_map = []
        for q in list(_candidates.keys())[:10]:
            try:
                r_sub = req_lib.get(f'{ML_BASE}/products/search', headers=hdrs,
                                    params={'site_id': 'MLA', 'q': q, 'limit': 10},
                                    timeout=5)
                if not r_sub.ok:
                    continue
                sub_items = r_sub.json().get('results', [])
                if not sub_items:
                    continue

                # Calidad de títulos: longitud promedio y % cortos
                sub_titles = [(item.get('name') or '') for item in sub_items]
                sub_lens   = [len(t) for t in sub_titles if t]
                avg_len    = round(sum(sub_lens) / len(sub_lens)) if sub_lens else 0
                short_pct_sub = round(sum(1 for l in sub_lens if l < 45) / len(sub_lens) * 100) if sub_lens else 0

                # Oportunidad = títulos cortos (mal optimizados) → fácil de superar
                # Score: 0 (competencia fuerte) → 100 (competencia débil)
                opp_score = short_pct_sub  # % de títulos mal optimizados

                signal = 'alta' if opp_score >= 60 else ('media' if opp_score >= 30 else 'baja')
                subniche_map.append({
                    'query': q,
                    'avg_title_len': avg_len,
                    'short_pct': short_pct_sub,
                    'opp_score': opp_score,
                    'signal': signal,
                    'results_count': len(sub_items),
                })
            except Exception:
                pass

        # Ordenar: mayor oportunidad primero (títulos más cortos = competencia más débil)
        subniche_map.sort(key=lambda x: -x['opp_score'])

        metrics = {
            'total_results': len(cat_results),
            'unique_sellers': None,
            'avg_price': None, 'min_price': None, 'max_price': None,
            'free_ship_pct': None, 'full_pct': None, 'official_pct': None,
            'concentration': None, 'has_sold_data': False, 'avg_sold': None,
            'difficulty': None,
            'data_source': 'catalog',
            'domains': domains,
            'trending': trending,
            'catalog_count': len(cat_results),
            'subniche_map': subniche_map,
            'listing_quality': {
                'avg_title_len': round(sum(cat_lens)/len(cat_lens)) if cat_lens else 0,
                'short_title_pct': cat_short_pct,
                'has_photo_pct': cat_photo_pct,
                'no_freeship_pct': None,
                'titles_sample': cat_titles[:15],
            },
        }

    return jsonify(ok=True, keyword=keyword, products=products[:12],
                   metrics=metrics, rich_data=rich_data)


@app.route('/api/radar-analizar-keyword', methods=['POST'])
def api_radar_analizar_keyword():
    """SSE: análisis con Claude de una oportunidad de nicho."""
    body     = request.get_json(force=True)
    keyword  = body.get('keyword', '')
    metrics  = body.get('metrics', {})
    products = body.get('products', [])

    def generate():
        is_catalog = metrics.get('data_source') == 'catalog'
        lq         = metrics.get('listing_quality', {})
        total_r    = metrics.get('total_results', 0)
        domains    = metrics.get('domains', [])
        trending   = metrics.get('trending', False)

        # Títulos formateados para análisis
        titles_sample = lq.get('titles_sample', [p['title'] for p in products[:15]])
        titles_block  = '\n'.join(f'  {i+1}. "{t}"' for i, t in enumerate(titles_sample))

        def prod_line(p):
            price_str = f'${p["price"]:,.0f}' if p.get('price') is not None else ''
            parts = [f'  - "{p["title"]}"']
            if price_str: parts.append(price_str)
            if p.get('sold'):      parts.append(f'{p["sold"]} vendidos')
            if p.get('free_ship'): parts.append('envío gratis')
            if p.get('is_full'):   parts.append('Full')
            if p.get('seller'):    parts.append(f'[{p["seller"]}]')
            return ' | '.join(parts)
        prod_block = '\n'.join(prod_line(p) for p in products[:12])

        avg_tlen   = lq.get('avg_title_len', 0)
        short_pct  = lq.get('short_title_pct', 0)
        photo_pct  = lq.get('has_photo_pct', 100)
        noship_pct = lq.get('no_freeship_pct')

        subniche_map = metrics.get('subniche_map', [])

        if is_catalog:
            domains_str  = ', '.join(domains) if domains else 'sin identificar'
            trending_str = 'SÍ está en trending de ML' if trending else 'no está en trending'

            # Bloque de sub-nichos con datos reales
            if subniche_map:
                sub_lines = '\n'.join(
                    f'  {"🟢" if s["signal"]=="alta" else ("🟡" if s["signal"]=="media" else "🔴")} '
                    f'"{s["query"]}" → títulos prom. {s["avg_title_len"]} chars, '
                    f'{s["short_pct"]}% mal optimizados → '
                    f'oportunidad {"ALTA" if s["signal"]=="alta" else ("media" if s["signal"]=="media" else "baja")}'
                    for s in subniche_map
                )
                sub_block = (
                    f'\n\n═══ MAPA DE SUB-NICHOS (búsquedas reales + análisis de calidad) ═══\n'
                    f'{sub_lines}\n'
                    f'Criterio: % de publicaciones con título < 45 chars = competencia mal optimizada = fácil de superar\n'
                    f'🟢=60%+ mal optimizados (entrá ya) 🟡=30–59% 🔴=<30% (competencia sólida)'
                )
            else:
                sub_block = ''

            market_block = f"""- Productos en catálogo ML: {total_r}
- Categorías identificadas: {domains_str}
- Trending ahora: {trending_str}
- Longitud promedio de títulos en catálogo: {avg_tlen} caracteres {'(CORTOS — mal posicionados)' if avg_tlen < 50 else '(aceptables)'}
- Títulos con menos de 45 caracteres (faltan keywords): {short_pct}%{sub_block}"""
        else:
            diff   = metrics.get('difficulty', '?')
            n_sell = metrics.get('unique_sellers', 0)
            avg_p  = metrics.get('avg_price', 0)
            min_p  = metrics.get('min_price', 0)
            max_p  = metrics.get('max_price', 0)
            fs_pct = metrics.get('free_ship_pct', 0)
            full_p = metrics.get('full_pct', 0)
            off_p  = metrics.get('official_pct', 0)
            market_block = f"""- Productos destacados analizados: {total_r} (los best-sellers de ML en esta categoría)
- Vendedores únicos activos: {n_sell} {'→ mercado muy concentrado' if n_sell < 5 else ('→ pocos jugadores' if n_sell < 15 else '→ mercado competitivo')}
- Precio promedio: ${avg_p:,} | Rango: ${min_p:,} – ${max_p:,}
- Con envío gratis: {fs_pct}% | Con logística Full de ML: {full_p}%
- Tiendas oficiales: {off_p}% {'→ hay marcas grandes' if off_p > 30 else '→ principalmente vendedores independientes'}
- Longitud promedio de títulos: {avg_tlen} chars {'⚠ títulos muy cortos' if avg_tlen < 50 else '(OK)'}
- Títulos cortos (< 45 chars, faltan keywords): {short_pct}%
- Publicaciones sin foto propia: {100-photo_pct}%
- Dificultad estimada para entrar: {diff}
Nota: estos son los productos actualmente destacados por ML en esta categoría (best-sellers del catálogo oficial)."""

        prompt = f"""Sos un consultor experto en negocios de marketplace, especializado en MercadoLibre Argentina (MLA).
Tu trabajo es detectar oportunidades REALES que otros vendedores están perdiendo.

Un vendedor está evaluando entrar al mercado de: **"{keyword}"**

═══ DATOS DEL MERCADO ═══
{market_block}

═══ TÍTULOS DE LAS PUBLICACIONES ACTUALES ═══
(analizá si están bien optimizados para búsqueda)
{titles_block}

═══ TOP PUBLICACIONES ═══
{prod_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tu análisis debe responder: **¿cuál es la oportunidad real que nadie está aprovechando?**

Mirá específicamente:
1. ¿Los títulos tienen las keywords que usa el comprador cuando busca? ¿O son genéricos?
2. ¿Hay variantes mal cubiertas (talla, color, modelo, uso específico)?
3. ¿Falta envío gratis donde el comprador lo espera?
4. ¿Los top vendedores tienen debilidades explotables (precio alto, sin Full, mala descripción)?
5. ¿Hay un ángulo de diferenciación que nadie está usando?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Respondé con este formato (## para headers, • para bullets):

## La oportunidad real
[1-2 oraciones: la brecha concreta que existe ahora mismo en este mercado]

## Por qué las publicaciones actuales están fallando
• [debilidad específica que ves en los títulos/datos — citá ejemplos concretos]
• [otra debilidad: precio, envío, variantes, posicionamiento]
• [si aplica: segmento de compradores que nadie está atendiendo bien]

## Así ganarías
• Título ideal: "[ejemplo de título optimizado de 60-80 caracteres con las keywords correctas]"
• Precio de entrada: [rango concreto y por qué]
• Envío: [gratis / Full / cuándo y por qué es decisivo acá]
• El diferencial clave: [qué hacer distinto que les gane a los actuales top sellers]

## Señales de alerta
• [riesgo 1 concreto de este mercado específico]
• [riesgo 2]

## Acción inmediata
1. [primer paso concreto para validar antes de invertir]
2. [segundo paso]
3. [tercer paso]

## 🏆 3 productos ganadores para entrar ahora
[Basate en los datos reales: analizá precios, envíos, tiendas oficiales y calidad de títulos. Detectá variantes, segmentos de precio o ángulos que los actuales best-sellers están descuidando. Para cada uno explicá el criterio con datos concretos del mercado.]

**1. [Nombre concreto del producto/variante]**
Oportunidad: [por qué nadie lo está cubriendo bien]
Precio sugerido: $X.XXX — $X.XXX ARS
Título sugerido: "[título optimizado listo para usar]"
Buscar en ML: [2 o 3 palabras clave para buscar este producto en ML, sin tildes, minúsculas]

**2. [Nombre concreto del producto/variante]**
Oportunidad: [la brecha que existe]
Precio sugerido: $X.XXX — $X.XXX ARS
Título sugerido: "[título optimizado listo para usar]"
Buscar en ML: [2 o 3 palabras clave para buscar este producto en ML, sin tildes, minúsculas]

**3. [Nombre concreto del producto/variante]**
Oportunidad: [la brecha que existe]
Precio sugerido: $X.XXX — $X.XXX ARS
Título sugerido: "[título optimizado listo para usar]"
Buscar en ML: [2 o 3 palabras clave para buscar este producto en ML, sin tildes, minúsculas]

Sé brutalmente honesto y específico. Citá los títulos reales cuando detectes problemas. Los 3 productos ganadores deben ser CONCRETOS y accionables, no genéricos. Máximo 550 palabras."""

        try:
            ai = anthropic.Anthropic()
            with ai.messages.stream(
                model='claude-opus-4-6',
                max_tokens=1200,
                messages=[{'role': 'user', 'content': prompt}],
            ) as stream:
                for text in stream.text_stream:
                    yield _sse({'type': 'token', 'text': text})
                _pq_fm = stream.get_final_message()
                _log_token_usage('Preguntas IA — Respuesta', 'claude-opus-4-6', _pq_fm.usage.input_tokens, _pq_fm.usage.output_tokens)
            yield _sse({'type': 'done'})
        except Exception as e:
            yield _sse({'type': 'error', 'msg': str(e)})

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control']     = 'no-cache'
    return resp


@app.route('/radar')
def radar():
    eventos = build_calendario()
    accounts = get_accounts()

    # Cargar datos de radar si existen
    radar_data = []
    radar_fecha = None
    radar_alias = None
    for path in sorted(glob.glob(os.path.join(DATA_DIR, 'radar_*.json')), reverse=True):
        d = load_json(path)
        if d:
            # Soporta formato lista de nichos o dict con clave 'nichos'
            nichos = d if isinstance(d, list) else d.get('nichos', [])
            if nichos:
                radar_data = nichos
                radar_fecha = d.get('fecha') if isinstance(d, dict) else None
                # Extraer alias del nombre del archivo
                base = os.path.basename(path)  # radar_Alias.json
                radar_alias = base[6:-5].replace('_', ' ')
                break

    return render_template('radar.html', eventos=eventos, accounts=accounts,
                           radar_data=radar_data, radar_fecha=radar_fecha,
                           radar_alias=radar_alias)


@app.route('/multicuenta')
def multicuenta():
    accounts  = get_accounts()
    today     = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    rows = []
    for acc in accounts:
        s          = safe(acc['alias'])
        stock_data = load_json(os.path.join(DATA_DIR, f'stock_{s}.json'))
        rep_data   = load_json(os.path.join(DATA_DIR, f'reputacion_{s}.json'))
        pos_data   = load_json(os.path.join(DATA_DIR, f'posiciones_{s}.json'))

        stock_items = (stock_data or {}).get('items', [])
        rep_latest  = rep_data[-1] if rep_data else None
        rep_ok = None
        if rep_latest:
            rep_ok = (rep_latest.get('reclamos_pct', 0) <= 2.0 and
                      rep_latest.get('demoras_pct', 0) <= 15.0 and
                      rep_latest.get('cancelaciones_pct', 0) <= 5.0)

        bajaron = total_pos = 0
        if pos_data:
            for item_data in pos_data.values():
                hist = item_data.get('history', {})
                pos_hoy  = hist.get(today)
                pos_ayer = hist.get(yesterday)
                if pos_hoy is not None:
                    total_pos += 1
                if pos_hoy and pos_ayer and pos_hoy != 999 and (pos_hoy - pos_ayer) >= 3:
                    bajaron += 1

        margen_neg  = sum(1 for i in stock_items if i.get('alerta_margen') == 'NEGATIVO')
        margen_bajo = sum(1 for i in stock_items if i.get('alerta_margen') == 'BAJO')
        nivel_id    = rep_latest.get('nivel', '') if rep_latest else ''

        rows.append({
            'alias':       acc['alias'],
            'nickname':    acc.get('nickname', ''),
            'total':       len(stock_items) or total_pos,
            'sin_stock':   sum(1 for i in stock_items if i.get('alerta_stock') == 'SIN_STOCK'),
            'criticos':    sum(1 for i in stock_items if i.get('alerta_stock') == 'CRITICO'),
            'bajaron':     bajaron,
            'total_pos':   total_pos,
            'rep_ok':      rep_ok,
            'rep_latest':  rep_latest,
            'nivel_id':    nivel_id,
            'margen_neg':  margen_neg,
            'margen_bajo': margen_bajo,
            'stock_fecha': (stock_data or {}).get('fecha'),
        })

    return render_template('multicuenta.html', rows=rows, accounts=accounts,
                           ml_redirect_uri=ML_REDIRECT_URI)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/run', methods=['POST'])
def api_run():
    """Ejecuta un comando CLI en background."""
    body    = request.get_json() or {}
    comando = body.get('comando', '')
    alias   = body.get('alias', '')

    allowed = [
        'posiciones', 'reputacion', 'stock-rentabilidad',
        'competencia', 'optimizar', 'repricing', 'preguntas',
        'todo', 'radar', 'full', 'reposicion', 'multicuenta',
    ]
    if comando not in allowed:
        return jsonify({'ok': False, 'error': 'Comando no permitido'})

    cmd = ['python3', 'main.py', comando]
    if alias:
        cmd.append(alias)

    try:
        subprocess.Popen(cmd, cwd=ROOT_DIR)
        return jsonify({'ok': True, 'msg': f'Corriendo: {" ".join(cmd[2:])}'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ── Optimizador web ──────────────────────────────────────────────────────────

# Cola en memoria: {alias: [competidor, ...]}
_pending_competitors: dict = {}

@app.route('/api/capturar-competidor', methods=['POST', 'OPTIONS'])
def api_capturar_competidor():
    """Recibe datos de un competidor enviados desde el bookmarklet del navegador."""
    # CORS — el bookmarklet corre en mercadolibre.com.ar, dominio distinto
    if request.method == 'OPTIONS':
        resp = make_response('', 204)
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    body  = request.get_json(force=True) or {}
    alias = body.get('alias', '').strip()
    if not alias:
        resp = jsonify({'ok': False, 'error': 'Falta alias'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 400

    comp = {
        'id':           body.get('id', ''),
        'title':        body.get('title', ''),
        'description':  body.get('description', ''),
        'price':        body.get('price', 0),
        'thumbnail':    body.get('thumbnail', ''),
        'pictures':     body.get('pictures', []),
        'photos_count': body.get('photos_count', 0),
        'attributes':   body.get('attributes', []),
        'sold_quantity':body.get('sold_quantity', 0),
        'free_ship':    body.get('free_ship', False),
        'premium':      body.get('premium', False),
        'condition':    body.get('condition', 'new'),
        'seller':       body.get('seller', '—'),
        'main_features':[],
        'reviews_rating': 0.0,
        'reviews_total':  0,
        'reviews_sample': [],
        '_fromBookmarklet': True,
    }

    if not comp['id'] or not comp['title']:
        resp = jsonify({'ok': False, 'error': 'Falta id o title'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 400

    _pending_competitors.setdefault(alias, [])
    # Evitar duplicados
    existing_ids = {c['id'] for c in _pending_competitors[alias]}
    if comp['id'] not in existing_ids:
        _pending_competitors[alias].append(comp)

    resp = jsonify({'ok': True, 'title': comp['title']})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/api/pending-competidores', methods=['GET'])
def api_pending_competidores():
    """Devuelve y vacía la cola de competidores pendientes para un alias."""
    alias   = request.args.get('alias', '').strip()
    pending = _pending_competitors.pop(alias, [])
    return jsonify({'ok': True, 'competitors': pending})


@app.route('/api/detalle-competidor', methods=['POST'])
def api_detalle_competidor():
    """
    Devuelve el detalle completo de un competidor de ML.
    Intenta primero como producto de catálogo (/products/{id}).
    Si falla, usa directamente /items/{id} para publicaciones independientes.
    """
    from core.account_manager import AccountManager
    body       = request.get_json() or {}
    alias      = body.get('alias', '')
    product_id = body.get('product_id', '').strip()
    slug_title = body.get('slug_title', '').strip()   # título extraído del slug de la URL

    if not alias or not product_id:
        return jsonify({'ok': False, 'error': 'Falta alias o product_id'}), 400

    def _fetch_seller(sid, headers):
        """Devuelve (nickname, ventas_completadas)."""
        if not sid:
            return '—', 0
        try:
            ur = req_lib.get(f'https://api.mercadolibre.com/users/{sid}',
                             headers=headers, timeout=6)
            if ur.ok:
                ud = ur.json()
                return (ud.get('nickname', '—'),
                        ud.get('seller_reputation', {}).get('transactions', {}).get('completed', 0))
        except Exception:
            pass
        return '—', 0

    def _fetch_description(item_id, headers):
        try:
            # Intentar sin auth primero (API pública)
            r = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}/description', timeout=6)
            if not r.ok:
                r = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}/description',
                                headers=headers, timeout=6)
            if r.ok:
                d = r.json()
                return d.get('plain_text', '') or d.get('text', '')
        except Exception:
            pass
        return ''

    def _fetch_reviews(item_id, headers):
        try:
            r = req_lib.get(f'https://api.mercadolibre.com/reviews/item/{item_id}',
                            headers=headers, timeout=6)
            if r.ok:
                rd = r.json()
                rating  = round(rd.get('rating_average', 0), 1)
                total   = rd.get('paging', {}).get('total', 0)
                sample  = [rv.get('content', '')[:250]
                           for rv in rd.get('reviews', [])[:4]
                           if rv.get('content', '').strip()]
                return rating, total, sample
        except Exception:
            pass
        return 0.0, 0, []

    try:
        manager = AccountManager()
        client  = manager.get_client(alias)
        client._ensure_token()
        token   = client.account.access_token
        headers = {'Authorization': f'Bearer {token}'}

        # ── Intento 1: producto de catálogo (/products/{id}) ─────────────
        pr = req_lib.get(f'https://api.mercadolibre.com/products/{product_id}',
                         headers=headers, timeout=10)

        if pr.ok:
            pd = pr.json()
            title         = pd.get('name', '')
            short_desc    = pd.get('short_description', {})
            base_desc     = short_desc.get('content', '') if isinstance(short_desc, dict) else ''
            main_features = [f.get('text', '') for f in pd.get('main_features', []) if f.get('text')]
            pictures      = [p.get('url', '') for p in pd.get('pictures', [])[:5] if p.get('url')]
            thumbnail     = re.sub(r'-[A-Z]+\.jpg$', '-S.jpg', pictures[0]) if pictures else ''
            photos_count  = len(pictures)
            raw_attrs     = pd.get('attributes', [])
            attributes    = [
                {'name': a.get('name', ''), 'value': a.get('value_name', '')}
                for a in raw_attrs
                if a.get('name') and a.get('value_name') and a.get('value_name') != 'No especificado'
            ]

            price = free_ship = False
            premium = False
            seller = '—'; seller_sales = 0
            sold_quantity = 0; listing_type = ''; condition = 'new'
            item_id_best = ''
            price = 0

            ir = req_lib.get(f'https://api.mercadolibre.com/products/{product_id}/items',
                             headers=headers, timeout=8)
            if ir.ok:
                items_list = ir.json().get('results', [])
                if items_list:
                    items_list.sort(key=lambda x: (
                        not x.get('shipping', {}).get('free_shipping', False),
                        x.get('price', 9999999)
                    ))
                    best          = items_list[0]
                    price         = best.get('price', 0)
                    free_ship     = best.get('shipping', {}).get('free_shipping', False)
                    lt            = best.get('listing_type_id', '')
                    listing_type  = lt
                    premium       = lt in ('gold_special', 'gold_pro')
                    sold_quantity = best.get('sold_quantity', 0)
                    condition     = best.get('condition', 'new')
                    item_id_best  = best.get('id', '')
                    seller, seller_sales = _fetch_seller(best.get('seller_id', ''), headers)

            full_description = base_desc
            if item_id_best:
                d = _fetch_description(item_id_best, headers)
                if d and len(d) > len(full_description):
                    full_description = d

            reviews_rating, reviews_total, reviews_sample = (
                _fetch_reviews(item_id_best, headers) if item_id_best else (0.0, 0, [])
            )

        else:
            # ── Intento 2: publicación independiente (/items/{id}) ────────
            ir = req_lib.get(f'https://api.mercadolibre.com/items/{product_id}', timeout=10)
            if not ir.ok:
                ir = req_lib.get(f'https://api.mercadolibre.com/items/{product_id}',
                                 headers=headers, timeout=10)

            if ir.ok:
                it            = ir.json()
                title         = it.get('title', '')
                pictures      = [p.get('url', '') for p in it.get('pictures', [])[:5] if p.get('url')]
                thumbnail     = re.sub(r'-[A-Z]+\.jpg$', '-S.jpg', pictures[0]) if pictures else ''
                photos_count  = len(pictures)
                main_features = []

                raw_attrs  = it.get('attributes', [])
                attributes = [
                    {'name': a.get('name', ''), 'value': a.get('value_name', '')}
                    for a in raw_attrs
                    if a.get('name') and a.get('value_name') and a.get('value_name') != 'No especificado'
                ]

                price         = it.get('price', 0)
                free_ship     = it.get('shipping', {}).get('free_shipping', False)
                lt            = it.get('listing_type_id', '')
                listing_type  = lt
                premium       = lt in ('gold_special', 'gold_pro')
                sold_quantity = it.get('sold_quantity', 0)
                condition     = it.get('condition', 'new')
                seller, seller_sales = _fetch_seller(it.get('seller_id', ''), headers)
                full_description              = _fetch_description(product_id, headers)
                reviews_rating, reviews_total, reviews_sample = _fetch_reviews(product_id, headers)

            else:
                # ── Intento 3: buscar producto equivalente en catálogo por título ──
                # Cuando /items/{id} da 403, usamos el título del slug para encontrar
                # el producto de catálogo equivalente y así obtener descripción y atributos.
                search_q = slug_title or product_id
                cr = req_lib.get('https://api.mercadolibre.com/products/search',
                                 params={'site_id': 'MLA', 'q': search_q, 'limit': 3},
                                 headers=headers, timeout=10)

                catalog_found = False
                if cr.ok:
                    results = cr.json().get('results', [])
                    if results:
                        best_cat = results[0]
                        cat_id   = best_cat.get('id', '')
                        if cat_id:
                            cpr = req_lib.get(f'https://api.mercadolibre.com/products/{cat_id}',
                                              headers=headers, timeout=10)
                            if cpr.ok:
                                pd = cpr.json()
                                title         = slug_title or pd.get('name', product_id)
                                short_desc    = pd.get('short_description', {})
                                base_desc     = short_desc.get('content', '') if isinstance(short_desc, dict) else ''
                                main_features = [f.get('text', '') for f in pd.get('main_features', []) if f.get('text')]
                                pictures      = [p.get('url', '') for p in pd.get('pictures', [])[:5] if p.get('url')]
                                thumbnail     = re.sub(r'-[A-Z]+\.jpg$', '-S.jpg', pictures[0]) if pictures else ''
                                photos_count  = len(pictures)
                                raw_attrs     = pd.get('attributes', [])
                                attributes    = [
                                    {'name': a.get('name', ''), 'value': a.get('value_name', '')}
                                    for a in raw_attrs
                                    if a.get('name') and a.get('value_name') and a.get('value_name') != 'No especificado'
                                ]
                                # Métricas del mejor ítem del catálogo
                                price = free_ship = False; premium = False
                                sold_quantity = 0; listing_type = ''; condition = 'new'
                                seller = '—'; seller_sales = 0; price = 0
                                iir = req_lib.get(f'https://api.mercadolibre.com/products/{cat_id}/items',
                                                  headers=headers, timeout=8)
                                if iir.ok:
                                    items_list = iir.json().get('results', [])
                                    if items_list:
                                        items_list.sort(key=lambda x: (
                                            not x.get('shipping', {}).get('free_shipping', False),
                                            x.get('price', 9999999)
                                        ))
                                        best       = items_list[0]
                                        price      = best.get('price', 0)
                                        free_ship  = best.get('shipping', {}).get('free_shipping', False)
                                        lt         = best.get('listing_type_id', '')
                                        listing_type = lt
                                        premium    = lt in ('gold_special', 'gold_pro')
                                        sold_quantity = best.get('sold_quantity', 0)
                                        condition  = best.get('condition', 'new')
                                        seller, seller_sales = _fetch_seller(best.get('seller_id', ''), headers)
                                        item_id_for_desc = best.get('id', '')
                                        if item_id_for_desc and len(base_desc) < 50:
                                            d = _fetch_description(item_id_for_desc, headers)
                                            if d: base_desc = d
                                full_description = base_desc
                                reviews_rating, reviews_total, reviews_sample = 0.0, 0, []
                                catalog_found = True

                if not catalog_found:
                    # Sin ningún dato de API — devolver lo que tenemos del slug
                    title         = slug_title or product_id
                    thumbnail     = ''
                    pictures      = []
                    photos_count  = 0
                    main_features = []
                    attributes    = []
                    price         = 0
                    free_ship     = False
                    listing_type  = ''
                    premium       = False
                    sold_quantity = 0
                    condition     = 'new'
                    seller        = '—'
                    seller_sales  = 0
                    full_description              = ''
                    reviews_rating, reviews_total, reviews_sample = 0.0, 0, []

        return jsonify({
            'ok':             True,
            'id':             product_id,
            'title':          title,
            'thumbnail':      thumbnail,
            'pictures':       pictures,
            'photos_count':   photos_count,
            'price':          price,
            'free_ship':      free_ship,
            'premium':        premium,
            'listing_type':   listing_type,
            'condition':      condition,
            'seller':         seller,
            'seller_sales':   seller_sales,
            'sold_quantity':  sold_quantity,
            'attributes':     attributes,
            'description':    full_description,
            'main_features':  main_features,
            'reviews_rating': reviews_rating,
            'reviews_total':  reviews_total,
            'reviews_sample': reviews_sample,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/item-info', methods=['POST'])
def api_item_info():
    """
    Dado un item_id o URL de ML, devuelve {title, thumbnail, price}.
    Usado cuando el usuario agrega un competidor desde el iframe de ML.
    """
    from core.account_manager import AccountManager
    body    = request.get_json() or {}
    alias   = body.get('alias', '')
    item_id = body.get('item_id', '').strip().upper()
    url     = body.get('url', '')

    # Extraer item_id de la URL si no se pasó directamente
    if not item_id and url:
        m = re.search(r'MLA-?(\d{7,12})', url, re.IGNORECASE)
        if m:
            item_id = 'MLA' + m.group(1)

    if not item_id:
        return jsonify({'ok': False, 'error': 'No se pudo determinar el ID del producto'}), 400

    try:
        manager = AccountManager()
        client  = manager.get_client(alias)
        client._ensure_token()
        token   = client.account.access_token
        headers = {'Authorization': f'Bearer {token}'}

        title = ''
        thumb = ''
        price = 0

        # Intentar como catalog product primero
        pr = req_lib.get(f'https://api.mercadolibre.com/products/{item_id}',
                         headers=headers, timeout=8)
        if pr.ok:
            pd = pr.json()
            title = pd.get('name', '')
            pics  = pd.get('pictures', [])
            if pics:
                url_img = pics[0].get('url', '')
                thumb   = re.sub(r'-[A-Z]+\.jpg$', '-S.jpg', url_img)
            # Intentar precio desde items
            ir = req_lib.get(f'https://api.mercadolibre.com/products/{item_id}/items',
                             headers=headers, timeout=6)
            if ir.ok:
                items = ir.json().get('results', [])
                if items:
                    price = min(x.get('price', 0) for x in items if x.get('price'))

        # Fallback: intentar como item directo (propio)
        if not title:
            try:
                item = client.get_item(item_id)
                title = item.get('title', item_id)
                price = item.get('price', 0)
                pics  = item.get('pictures', [])
                if pics:
                    url_img = pics[0].get('url', '')
                    thumb   = re.sub(r'-[A-Z]+\.jpg$', '-S.jpg', url_img)
            except Exception:
                title = item_id

        return jsonify({'ok': True, 'id': item_id, 'title': title,
                        'thumbnail': thumb, 'price': price})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'title': item_id}), 200


@app.route('/api/buscar-competidores', methods=['POST'])
def api_buscar_competidores():
    """
    Busca competidores en ML por query libre.
    Devuelve resultados con score de utilidad como competidor basado en:
      - Posición en resultados (proxy de ranking para esa keyword)
      - Ventas (validación real del mercado)
      - Tipo de publicación (Premium > Clásica)
      - Envío gratis
      - Cantidad de fotos

    Body: {alias, query}
    """
    import time as _time
    from core.account_manager import AccountManager

    body  = request.get_json() or {}
    alias = body.get('alias', '')
    query = body.get('query', '').strip()

    if not alias or not query:
        return jsonify({'ok': False, 'error': 'Falta alias o query'}), 400

    def _competitor_score(position, sold, is_premium, free_ship, photos):
        """Score 0-100 que indica qué tan útil es este competidor para analizar."""
        # Posición en resultados (40pts) — más arriba = rankea mejor para esa keyword
        pos_score = max(0, 40 - (position - 1) * 4)

        # Ventas (35pts) — valida que el producto realmente vende
        if   sold >= 500: sale_score = 35
        elif sold >= 100: sale_score = 25
        elif sold >= 20:  sale_score = 15
        elif sold >= 1:   sale_score = 7
        else:             sale_score = 0

        # Premium (15pts) — inversión en visibilidad
        prem_score = 15 if is_premium else 0

        # Envío gratis (7pts) — factor de ranking ML
        ship_score = 7 if free_ship else 0

        # Fotos (3pts) — indicador de listing cuidado
        photo_score = 3 if photos >= 6 else (1 if photos >= 3 else 0)

        return min(100, pos_score + sale_score + prem_score + ship_score + photo_score)

    def _score_verdict(score):
        if score >= 65:
            return {'label': 'Vale analizarlo', 'color': '#16a34a', 'icon': '✅'}
        elif score >= 35:
            return {'label': 'Ranking moderado', 'color': '#d97706', 'icon': '⚠️'}
        else:
            return {'label': 'Poca relevancia', 'color': '#dc2626', 'icon': '❌'}

    try:
        manager = AccountManager()
        client  = manager.get_client(alias)
        client._ensure_token()
        token   = client.account.access_token
        headers = {'Authorization': f'Bearer {token}'}

        # ── 1. Buscar en catálogo ML ──────────────────────────────────────────
        sr = req_lib.get(
            'https://api.mercadolibre.com/products/search',
            headers=headers,
            params={'site_id': 'MLA', 'q': query, 'limit': 20},
            timeout=10
        )
        if not sr.ok:
            return jsonify({'ok': False, 'error': f'No se pudo buscar en ML ({sr.status_code})'}), 500

        search_results = sr.json().get('results', [])
        if not search_results:
            return jsonify({'ok': True, 'results': []})

        # ── 2. Enriquecer con métricas del mejor listing ──────────────────────
        results = []
        for position, prod in enumerate(search_results[:16], 1):
            prod_id = prod.get('catalog_product_id') or prod.get('id', '')
            name    = prod.get('name', '')
            if not prod_id or not name:
                continue
            try:
                # Thumbnail del catálogo
                thumb = ''
                pr = req_lib.get(f'https://api.mercadolibre.com/products/{prod_id}',
                                  headers=headers, timeout=6)
                if pr.ok:
                    pics = pr.json().get('pictures', [])
                    if pics:
                        thumb = re.sub(r'-[A-Z]+\.jpg$', '-I.jpg', pics[0].get('url', ''))

                # Mejor listing del producto para métricas
                sold, price, is_premium, free_ship, photos = 0, 0, False, False, 0
                ir = req_lib.get(f'https://api.mercadolibre.com/products/{prod_id}/items',
                                  headers=headers, params={'limit': 3}, timeout=6)
                if ir.ok:
                    items = ir.json().get('results', [])
                    if items:
                        # Tomar el de mayor ventas (el más relevante)
                        best = max(items, key=lambda x: x.get('sold_quantity', 0))
                        sold      = int(best.get('sold_quantity', 0))
                        price     = float(best.get('price', 0))
                        is_premium= best.get('listing_type_id', '') in ('gold_special', 'gold_pro')
                        free_ship = best.get('shipping', {}).get('free_shipping', False)
                        # Fotos del item
                        item_r = req_lib.get(f'https://api.mercadolibre.com/items/{best["id"]}',
                                              headers=headers, timeout=5)
                        if item_r.ok:
                            photos = len(item_r.json().get('pictures', []))
                            if not thumb:
                                item_pics = item_r.json().get('pictures', [])
                                if item_pics:
                                    thumb = re.sub(r'-[A-Z]+\.jpg$', '-I.jpg',
                                                   item_pics[0].get('secure_url') or item_pics[0].get('url', ''))

                score   = _competitor_score(position, sold, is_premium, free_ship, photos)
                verdict = _score_verdict(score)

                results.append({
                    'id':          prod_id,
                    'title':       name,
                    'thumbnail':   thumb,
                    'position':    position,
                    'price':       round(price),
                    'sold':        sold,
                    'is_premium':  is_premium,
                    'free_ship':   free_ship,
                    'photos':      photos,
                    'score':       score,
                    'verdict':     verdict,
                })
                _time.sleep(0.08)
            except Exception:
                continue

        # Ordenar por score descendente (los mejores competidores arriba)
        results.sort(key=lambda x: -x['score'])

        return jsonify({'ok': True, 'results': results})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/analizar-producto', methods=['POST'])
def api_analizar_producto():
    """
    Analiza un producto con Claude para identificar:
    - Concepto exacto del producto
    - Diferenciador clave dentro del rubro
    - Términos de búsqueda recomendados y no recomendados

    Body: {alias, item_id}
    """
    import json as _json
    from core.account_manager import AccountManager

    body         = request.get_json() or {}
    alias        = body.get('alias', '')
    item_id      = body.get('item_id', '').strip().upper()
    product_text = body.get('product_text', '').strip()   # para productos nuevos sin item_id

    if not alias or (not item_id and not product_text):
        return jsonify({'ok': False, 'error': 'Falta alias e item_id o descripción del producto'}), 400

    try:
        manager = AccountManager()
        client  = manager.get_client(alias)
        client._ensure_token()
        token   = client.account.access_token
        headers = {'Authorization': f'Bearer {token}'}

        if item_id:
            # ── 1. Datos del item ────────────────────────────────────────────────
            ir = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}',
                             headers=headers, timeout=8)
            if not ir.ok:
                return jsonify({'ok': False, 'error': 'No se pudo obtener el item de ML'}), 400

            item_data   = ir.json()
            title       = item_data.get('title', '')
            category_id = item_data.get('category_id', '')
            attributes  = item_data.get('attributes', [])

            if not title:
                return jsonify({'ok': False, 'error': 'El item no tiene título'}), 400

            # ── 2. Nombre de la categoría ────────────────────────────────────────
            category_name = category_id
            try:
                cr = req_lib.get(f'https://api.mercadolibre.com/categories/{category_id}',
                                 headers=headers, timeout=6)
                if cr.ok:
                    category_name = cr.json().get('name', category_id)
            except Exception:
                pass

            # ── 3. Atributos relevantes (excluye condición/marca/SKU) ────────────
            skip_attrs = {'ITEM_CONDITION', 'BRAND', 'MODEL', 'SELLER_SKU'}
            relevant_attrs = []
            for attr in attributes[:15]:
                if attr.get('id', '') in skip_attrs:
                    continue
                name  = attr.get('name', '')
                value = attr.get('value_name', '')
                if name and value:
                    relevant_attrs.append(f'{name}: {value}')
            attrs_summary = ', '.join(relevant_attrs[:8]) or 'No disponibles'
        else:
            # Modo producto nuevo: usar texto libre, sin consultar ML
            title         = product_text
            category_name = 'A determinar'
            attrs_summary = 'No disponibles (producto nuevo, sin publicación aún)'

        # ── 4. Claude analiza el producto ────────────────────────────────────
        prompt = (
            f'Analizá este producto de MercadoLibre Argentina y ayudame a identificar '
            f'los mejores términos de búsqueda para encontrar competidores directos.\n\n'
            f'Producto: "{title}"\n'
            f'Categoría ML: {category_name}\n'
            f'Atributos: {attrs_summary}\n\n'
            f'Tu tarea:\n'
            f'1. Identificá qué es EXACTAMENTE este producto (no el rubro, el producto específico)\n'
            f'2. Identificá el atributo DIFERENCIADOR que lo distingue de otros productos similares del mismo rubro\n'
            f'3. Generá entre 5 y 7 términos de búsqueda\n\n'
            f'Para los términos:\n'
            f'- recomendado=true: el término incluye el diferenciador y es específico a este producto exacto\n'
            f'- recomendado=false: demasiado genérico, podría confundirse con otro producto del rubro\n'
            f'- ambiguo=true: el término existe en ML pero aplica a varios productos distintos\n'
            f'- Usá vocabulario de compradores argentinos en ML\n\n'
            f'Respondé ÚNICAMENTE con este JSON (sin texto adicional, sin bloques de código markdown):\n'
            f'{{"concepto":"descripción exacta en 1 línea","diferenciador":"atributo clave que lo distingue",'
            f'"terminos":[{{"termino":"término de búsqueda","razon":"por qué incluir o excluir","recomendado":true,"ambiguo":false}}]}}'
        )

        ai   = anthropic.Anthropic()
        resp = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=700,
            messages=[{'role': 'user', 'content': prompt}],
        )
        _log_token_usage('Lanzar Producto — Análisis concepto', 'claude-haiku-4-5-20251001', resp.usage.input_tokens, resp.usage.output_tokens)
        raw = (resp.content[0].text or '').strip()

        # Limpiar posibles bloques markdown
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw).strip()

        analysis = _json.loads(raw)

        return jsonify({
            'ok':            True,
            'item_title':    title,
            'category_name': category_name,
            'concepto':      analysis.get('concepto', ''),
            'diferenciador': analysis.get('diferenciador', ''),
            'terminos':      analysis.get('terminos', []),
        })

    except _json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'Error parseando respuesta de Claude: {e}'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/descubrir-competidores', methods=['POST'])
def api_descubrir_competidores():
    """
    Descubre los competidores reales para un producto.

    Lógica:
      1. Obtiene el título y category_id del item desde ML
      2. Corre autosuggest para obtener las keywords más buscadas (ordenadas por popularidad)
      3. Obtiene los best sellers de la misma categoría vía highlights API
         (también busca en subcategorías para mayor cobertura)
      4. Puntúa cada competidor por relevancia semántica con las keywords del autosuggest:
           - Cuántas keywords aparecen en el nombre del producto = score de relevancia
           - Peso por posición del autosuggest (keyword más buscada = mayor peso)
      5. Enriquece los top 8 con datos del mejor listing (precio, envío, fotos)
      6. Devuelve los top 10 rankeados por score

    Body: {alias, item_id}
    """
    import time as _time
    from core.account_manager import AccountManager

    body               = request.get_json() or {}
    alias              = body.get('alias', '')
    item_id            = body.get('item_id', '').strip().upper()
    product_text       = body.get('product_text', '').strip()  # para productos nuevos sin item_id
    confirmed_keywords = body.get('confirmed_keywords', [])    # lista de términos validados por el usuario
    diferenciador      = body.get('diferenciador', '')         # atributo clave del producto (ej: "puntas")

    if not alias or (not item_id and not product_text and not confirmed_keywords):
        return jsonify({'ok': False, 'error': 'Falta alias e item_id o descripción del producto'}), 400

    try:
        manager = AccountManager()
        client  = manager.get_client(alias)
        client._ensure_token()
        token   = client.account.access_token
        headers = {'Authorization': f'Bearer {token}'}

        title      = ''
        item_price = 0.0
        category_id = ''

        if item_id:
            # ── 1a. Modo publicación existente: obtener datos del item ────────────
            ir = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}',
                             headers=headers, timeout=8)
            if not ir.ok:
                return jsonify({'ok': False, 'error': 'No se pudo obtener el item de ML'}), 400

            item_data   = ir.json()
            title       = item_data.get('title', '')
            item_price  = float(item_data.get('price', 0))
            category_id = item_data.get('category_id', '')

            if not title:
                return jsonify({'ok': False, 'error': 'El item no tiene título'}), 400
            if not category_id:
                return jsonify({'ok': False, 'error': 'El item no tiene categoría asignada'}), 400
        else:
            # ── 1b. Modo producto nuevo: usar texto o confirmed keywords ──────────
            title = product_text or (confirmed_keywords[0] if confirmed_keywords else '')

        # ── 2. Keywords del autosuggest ───────────────────────────────────────
        _stopwords = {'de','para','con','sin','y','el','la','los','las','un','una',
                      'en','a','por','al','del','se','su','es','que','o','e','x'}

        # Si el usuario confirmó términos específicos, usarlos como queries de autosuggest
        # Así las keywords resultantes son precisas para ese producto exacto
        if confirmed_keywords:
            queries = [t.lower().strip() for t in confirmed_keywords[:5] if t.strip()]
        else:
            # Fallback: derivar queries del título
            words = [w for w in title.lower().split() if len(w) >= 3 and w not in _stopwords]
            q1 = title.lower()
            q2 = ' '.join(words[:3]) if len(words) >= 3 else ' '.join(words)
            q3 = ' '.join(words[:2]) if len(words) >= 2 else ''
            queries = [q for q in [q1, q2, q3] if q]

        seen_kw      = set()
        raw_keywords = []

        for q in queries:
            suggestions = _ml_autosuggest(q, limit=8)
            for pos, kw in enumerate(suggestions, 1):
                kw_norm = kw.lower().strip()
                if kw_norm and kw_norm not in seen_kw:
                    seen_kw.add(kw_norm)
                    raw_keywords.append({'keyword': kw_norm, 'autosuggest_position': pos})
            _time.sleep(0.1)

        # Fallback: si autosuggest devuelve vacío, usar los términos confirmados directamente
        if not raw_keywords:
            if confirmed_keywords:
                for pos, term in enumerate(confirmed_keywords[:6], 1):
                    raw_keywords.append({'keyword': term.lower().strip(), 'autosuggest_position': pos})
            else:
                words = [w for w in title.lower().split() if len(w) >= 3 and w not in _stopwords]
                for pos, w in enumerate(words[:6], 1):
                    raw_keywords.append({'keyword': w, 'autosuggest_position': pos})

        keywords_to_use = raw_keywords[:6]

        if not keywords_to_use:
            return jsonify({'ok': False, 'error': 'No se pudieron extraer keywords del producto'}), 400

        # ── Tokens del diferenciador para filtro duro ────────────────────────
        # Solo pasan competidores cuyo nombre contiene al menos uno de estos tokens.
        # Si diferenciador viene vacío, lo derivamos de la intersección de los términos confirmados.
        diff_filter_tokens: set = set()
        if diferenciador:
            diff_filter_tokens = set(re.findall(r'[a-záéíóúüñ0-9]+', diferenciador.lower())) - _stopwords
        elif confirmed_keywords and len(confirmed_keywords) >= 2:
            # Tokens que aparecen en AL MENOS la mitad de los términos confirmados
            token_counts: dict = {}
            for ck in confirmed_keywords:
                for t in set(re.findall(r'[a-záéíóúüñ0-9]+', ck.lower())) - _stopwords:
                    token_counts[t] = token_counts.get(t, 0) + 1
            threshold = max(2, len(confirmed_keywords) // 2)
            diff_filter_tokens = {t for t, c in token_counts.items() if c >= threshold}

        # Tokenset de keywords para scoring semántico
        kw_tokens_weighted = []  # [(token_set, weight), ...]
        total_kws = len(keywords_to_use)
        for kw_idx, kw_data in enumerate(keywords_to_use):
            kw_weight = (total_kws - kw_idx) / total_kws
            tokens    = set(re.findall(r'[a-záéíóúüñ0-9]+', kw_data['keyword'].lower()))
            tokens   -= _stopwords
            kw_tokens_weighted.append((tokens, kw_weight, kw_data['keyword']))

        keywords_info = [
            {
                'keyword':     kd['keyword'],
                'as_position': kd['autosuggest_position'],
                'weight':      round(((total_kws - i) / total_kws) * 100),
                'total_results': 0,
            }
            for i, kd in enumerate(keywords_to_use)
        ]

        # ── 3. Obtener competidores ───────────────────────────────────────────
        # Modo A (publicación existente): Highlights API por categoría
        # Modo B (producto nuevo, sin item_id): búsqueda directa por keywords en ML
        product_ids   = []
        seen_pids     = set()
        # item_ids_mode: cuando usamos search en vez de highlights, los IDs son items, no productos
        item_ids_mode = False

        if category_id:
            def _get_highlights(cat_id):
                try:
                    r = req_lib.get(
                        f'https://api.mercadolibre.com/highlights/MLA/category/{cat_id}',
                        headers=headers, timeout=10
                    )
                    if not r.ok:
                        return []
                    return [e['id'] for e in r.json().get('content', [])
                            if e.get('type') in ('PRODUCT', 'USER_PRODUCT')
                            and e.get('id', '').startswith('MLA')
                            and not e.get('id', '').startswith('MLAU')]
                except Exception:
                    return []

            for pid in _get_highlights(category_id):
                if pid not in seen_pids:
                    seen_pids.add(pid)
                    product_ids.append(pid)

            # Subcategorías para mayor cobertura
            if len(product_ids) < 8:
                try:
                    cr = req_lib.get(f'https://api.mercadolibre.com/categories/{category_id}',
                                     headers=headers, timeout=8)
                    if cr.ok:
                        for child in cr.json().get('children_categories', [])[:4]:
                            for pid in _get_highlights(child['id']):
                                if pid not in seen_pids:
                                    seen_pids.add(pid)
                                    product_ids.append(pid)
                            if len(product_ids) >= 15:
                                break
                            _time.sleep(0.1)
                except Exception:
                    pass
        else:
            # Modo búsqueda directa: usar las keywords para buscar en ML
            item_ids_mode = True
            for kd in keywords_to_use[:4]:
                try:
                    sr = req_lib.get(
                        'https://api.mercadolibre.com/sites/MLA/search',
                        headers=headers,
                        params={'q': kd['keyword'], 'limit': 10, 'sort': 'relevance'},
                        timeout=10
                    )
                    if sr.ok:
                        for res in sr.json().get('results', []):
                            pid = res.get('id', '')
                            if pid and pid not in seen_pids:
                                seen_pids.add(pid)
                                product_ids.append(pid)
                    _time.sleep(0.15)
                except Exception:
                    pass

        if not product_ids:
            return jsonify({'ok': False, 'error': 'No se encontraron competidores para ese producto'}), 400

        # ── 4. Enriquecer products y puntuar por relevancia con keywords ──────
        competitor_map = {}

        for prod_id in product_ids[:15]:
            if prod_id == item_id:
                continue
            try:
                name  = ''
                thumb = ''

                if item_ids_mode:
                    # Modo búsqueda directa: los IDs son items (MLA...), no catálogo
                    ir2 = req_lib.get(f'https://api.mercadolibre.com/items/{prod_id}',
                                      headers=headers, timeout=6)
                    if not ir2.ok:
                        _time.sleep(0.08)
                        continue
                    id2  = ir2.json()
                    name = id2.get('title', '')
                    pics = id2.get('pictures', [])
                    if pics:
                        url   = pics[0].get('secure_url') or pics[0].get('url', '')
                        thumb = re.sub(r'-[A-Z]+\.jpg$', '-I.jpg', url) if url else ''
                    best_item = id2
                else:
                    # Modo highlights: los IDs son productos catálogo
                    pr = req_lib.get(f'https://api.mercadolibre.com/products/{prod_id}',
                                     headers=headers, timeout=6)
                    if not pr.ok:
                        _time.sleep(0.08)
                        continue

                    pd    = pr.json()
                    name  = pd.get('name', '')
                    pics  = pd.get('pictures', [])
                    if pics:
                        url   = pics[0].get('url', '')
                        thumb = re.sub(r'-[A-Z]+\.jpg$', '-I.jpg', url) if url else ''

                    # Mejor listing del producto para precio y datos de venta
                    best_item = None
                    try:
                        items_r = req_lib.get(f'https://api.mercadolibre.com/products/{prod_id}/items',
                                              headers=headers, params={'limit': 3}, timeout=6)
                        if items_r.ok:
                            items_list = items_r.json().get('results', [])
                            if items_list:
                                items_list.sort(key=lambda x: (
                                    not x.get('shipping', {}).get('free_shipping', False),
                                    x.get('price', 9999999)
                                ))
                                best_item = items_list[0]
                    except Exception:
                        pass

                    if best_item and not thumb:
                        try:
                            item_r = req_lib.get(f'https://api.mercadolibre.com/items/{best_item["id"]}',
                                                  headers=headers, timeout=6)
                            if item_r.ok:
                                item_pics = item_r.json().get('pictures', [])
                                if item_pics:
                                    url   = item_pics[0].get('secure_url') or item_pics[0].get('url', '')
                                    thumb = re.sub(r'-[A-Z]+\.jpg$', '-I.jpg', url)
                        except Exception:
                            pass

                if not name:
                    _time.sleep(0.08)
                    continue

                # Score semántico: cuántas keywords del autosuggest coinciden con el nombre
                name_tokens = set(re.findall(r'[a-záéíóúüñ0-9]+', name.lower()))
                name_tokens -= _stopwords
                score       = 0.0
                kw_matches  = []

                for (kw_tokens, kw_weight, kw_str) in kw_tokens_weighted:
                    overlap = len(kw_tokens & name_tokens)
                    if overlap > 0:
                        contribution = kw_weight * (overlap / max(len(kw_tokens), 1)) * 100
                        score       += contribution
                        kw_matches.append(kw_str)

                price        = float(best_item.get('price', 0)) if best_item else 0
                sold_qty     = int(best_item.get('sold_quantity', 0)) if best_item else 0
                free_ship    = best_item.get('shipping', {}).get('free_shipping', False) if best_item else False
                listing_type = best_item.get('listing_type_id', '') if best_item else ''
                is_premium   = listing_type in ('gold_special', 'gold_pro')
                photos_count = len(best_item.get('pictures', [])) if (best_item and item_ids_mode) else 0
                if best_item and not item_ids_mode:
                    try:
                        item_r = req_lib.get(f'https://api.mercadolibre.com/items/{best_item["id"]}',
                                              headers=headers, timeout=6)
                        if item_r.ok:
                            photos_count = len(item_r.json().get('pictures', []))
                    except Exception:
                        pass

                # Filtro duro: el nombre debe contener al menos un token del diferenciador
                if diff_filter_tokens and not (name_tokens & diff_filter_tokens):
                    _time.sleep(0.08)
                    continue

                competitor_map[prod_id] = {
                    'id':           prod_id,
                    'title':        name,
                    'price':        price,
                    'sold_quantity': sold_qty,
                    'is_premium':   is_premium,
                    'free_shipping': free_ship,
                    'thumbnail':    thumb,
                    'photos_count': photos_count,
                    'score':        round(score, 1),
                    'appearances':  len(kw_matches),
                    'kw_details':   [{'keyword': k, 'position': 1, 'weight': 100} for k in kw_matches],
                }

            except Exception:
                pass
            _time.sleep(0.1)

        if not competitor_map:
            return jsonify({'ok': False, 'error': 'No se pudieron obtener datos de competidores'}), 400

        # ── 5. Rankear por score semántico ────────────────────────────────────
        ranked = sorted(competitor_map.values(), key=lambda x: x['score'], reverse=True)[:10]

        return jsonify({
            'ok':          True,
            'item_title':  title,
            'item_price':  item_price,
            'keywords':    keywords_info,
            'competitors': ranked,
            'total_found': len(competitor_map),
        })

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/reverse-keywords', methods=['POST'])
def api_reverse_keywords():
    """
    Reverse engineering de keywords de los competidores seleccionados.

    Para cada competidor, corre autosuggest con su título y recolecta las keywords
    que ML asocia a ese producto. Cruza con las keywords propias del usuario para
    identificar gaps (keywords que los competidores rankean y el usuario no tiene).

    Body: {alias, competitors: [{id, title, description}], our_keywords: ['kw1', 'kw2', ...]}
    """
    import time as _time
    from collections import Counter
    from core.account_manager import AccountManager

    body         = request.get_json() or {}
    alias        = body.get('alias', '')
    competitors  = body.get('competitors', [])   # [{id, title, description}, ...]
    our_keywords = [k.lower().strip() for k in body.get('our_keywords', [])]

    if not alias:
        return jsonify({'ok': False, 'error': 'Falta alias'}), 400
    if not competitors:
        return jsonify({'ok': False, 'error': 'No hay competidores para analizar'}), 400

    _stopwords = {'de','para','con','sin','y','el','la','los','las','un','una',
                  'en','a','por','al','del','se','su','es','que','o','e','x',
                  'negro','blanco','azul','rojo','verde','gris','rosa','dorado',
                  'este','esta','estos','estas','muy','más','mas','también',
                  'tambien','si','no','su','sus','hay','tiene','tienen','puede',
                  'pueden','ideal','perfecta','perfecto','todo','todos','hacer'}

    def _extract_desc_phrases(description: str, title_words: set, n: int = 3) -> list:
        """
        Extrae frases de 2-3 palabras de la descripción que sean relevantes
        para el producto (excluye stopwords, incluye palabras del título como ancla).
        Devuelve hasta n frases únicas ordenadas por frecuencia.
        """
        if not description:
            return []
        # Limpiar HTML básico
        clean = re.sub(r'<[^>]+>', ' ', description)
        clean = re.sub(r'[^\w\sáéíóúüñ]', ' ', clean.lower())
        words = [w for w in clean.split() if len(w) >= 3 and w not in _stopwords]

        # Bigramas y trigramas
        phrases = []
        for i in range(len(words)):
            if i + 1 < len(words):
                phrases.append(f'{words[i]} {words[i+1]}')
            if i + 2 < len(words):
                phrases.append(f'{words[i]} {words[i+1]} {words[i+2]}')

        # Contar frecuencia y priorizar frases que contienen palabras del título
        phrase_counts = Counter(phrases)
        scored = []
        for ph, cnt in phrase_counts.items():
            ph_words = set(ph.split())
            title_overlap = len(ph_words & title_words)
            scored.append((ph, cnt + title_overlap * 2))

        scored.sort(key=lambda x: -x[1])
        seen = set()
        result = []
        for ph, _ in scored:
            # Evitar frases redundantes (que sean subconjunto de otra ya incluida)
            if ph not in seen:
                seen.add(ph)
                result.append(ph)
            if len(result) >= n:
                break
        return result

    try:
        # ── Recolectar keywords por competidor ────────────────────────────────
        # kw_data[keyword] = {keyword, competitors: [title,...], positions: [int,...], sources: set}
        kw_data: dict = {}

        for comp in competitors[:8]:   # máximo 8 competidores
            comp_title = (comp.get('title') or '').strip()
            if not comp_title or comp_title == comp.get('id', ''):
                continue

            comp_desc = (comp.get('description') or '').strip()

            # ── Queries desde el TÍTULO ───────────────────────────────────────
            title_words = {w for w in comp_title.lower().split()
                           if len(w) >= 3 and w not in _stopwords}
            q1 = comp_title.lower()
            q2 = ' '.join(list(title_words)[:4])
            title_queries = list(dict.fromkeys([q for q in [q1, q2] if q]))

            # ── Queries desde la DESCRIPCIÓN ─────────────────────────────────
            desc_phrases = _extract_desc_phrases(comp_desc, title_words, n=3)

            all_queries = title_queries + desc_phrases

            seen_for_comp = set()
            for q_idx, q in enumerate(all_queries):
                source = 'titulo' if q_idx < len(title_queries) else 'descripcion'
                suggestions = _ml_autosuggest(q, limit=10)
                for pos, kw in enumerate(suggestions, 1):
                    kw_norm = kw.lower().strip()
                    if not kw_norm or kw_norm in seen_for_comp:
                        continue
                    seen_for_comp.add(kw_norm)
                    if kw_norm not in kw_data:
                        kw_data[kw_norm] = {
                            'keyword':     kw_norm,
                            'competitors': [],
                            'positions':   [],
                            'sources':     set(),
                            'in_ours':     False,
                        }
                    kw_data[kw_norm]['competitors'].append(comp_title[:50])
                    kw_data[kw_norm]['positions'].append(pos)
                    kw_data[kw_norm]['sources'].add(source)
                _time.sleep(0.1)

        # ── Fallback: extraer keywords directamente de los títulos si autosuggest falló ──
        if not kw_data:
            app.logger.warning('[reverse-keywords] autosuggest vacío para %d competidores — usando fallback por tokenización', len(competitors))
            for comp in competitors[:8]:
                comp_title = (comp.get('title') or '').strip()
                if not comp_title or comp_title == comp.get('id', ''):
                    continue
                title_words_fb = [
                    w for w in re.findall(r'[a-záéíóúüñ0-9]+', comp_title.lower())
                    if len(w) >= 3 and w not in _stopwords
                ]
                # Bigramas del título como keywords
                seen_for_comp = set()
                for i in range(len(title_words_fb)):
                    # palabra suelta
                    kw_norm = title_words_fb[i]
                    if kw_norm not in seen_for_comp:
                        seen_for_comp.add(kw_norm)
                        if kw_norm not in kw_data:
                            kw_data[kw_norm] = {'keyword': kw_norm, 'competitors': [], 'positions': [], 'sources': set(), 'in_ours': False}
                        kw_data[kw_norm]['competitors'].append(comp_title[:50])
                        kw_data[kw_norm]['positions'].append(i + 1)
                        kw_data[kw_norm]['sources'].add('titulo_fallback')
                    # bigrama
                    if i + 1 < len(title_words_fb):
                        bi = f'{title_words_fb[i]} {title_words_fb[i+1]}'
                        if bi not in seen_for_comp:
                            seen_for_comp.add(bi)
                            if bi not in kw_data:
                                kw_data[bi] = {'keyword': bi, 'competitors': [], 'positions': [], 'sources': set(), 'in_ours': False}
                            kw_data[bi]['competitors'].append(comp_title[:50])
                            kw_data[bi]['positions'].append(i + 1)
                            kw_data[bi]['sources'].add('titulo_fallback')

        if not kw_data:
            return jsonify({'ok': False, 'error': 'No se pudieron obtener keywords de los competidores. Los competidores no tienen títulos válidos.'}), 400

        # ── Marcar cuáles ya tiene el usuario ────────────────────────────────
        our_token_sets = [
            set(re.findall(r'[a-záéíóúüñ0-9]+', kw)) - _stopwords
            for kw in our_keywords
        ]

        for kw_norm, entry in kw_data.items():
            kw_tokens = set(re.findall(r'[a-záéíóúüñ0-9]+', kw_norm)) - _stopwords
            # Consideramos "match" si hay ≥60% de overlap con alguna keyword propia
            for our_tokens in our_token_sets:
                if not our_tokens:
                    continue
                overlap = len(kw_tokens & our_tokens) / max(len(kw_tokens), len(our_tokens))
                if overlap >= 0.6:
                    entry['in_ours'] = True
                    break

        # ── Calcular métricas y ordenar ───────────────────────────────────────
        total_comps = len(competitors)
        result_list = []
        for entry in kw_data.values():
            freq      = len(set(entry['competitors']))   # competidores únicos que la tienen
            avg_pos   = sum(entry['positions']) / len(entry['positions'])
            sources   = list(entry.get('sources', set()))
            result_list.append({
                'keyword':      entry['keyword'],
                'frequency':    freq,
                'avg_position': round(avg_pos, 1),
                'competitors':  list(set(entry['competitors']))[:4],
                'in_ours':      entry['in_ours'],
                'is_gap':       not entry['in_ours'] and freq >= max(1, total_comps // 3),
                'from_desc':    'descripcion' in sources and 'titulo' not in sources,
            })

        # Ordenar: primero gaps por frecuencia, luego el resto
        result_list.sort(key=lambda x: (-int(x['is_gap']), -x['frequency'], x['avg_position']))

        gaps   = [k for k in result_list if k['is_gap']]
        shared = [k for k in result_list if not k['is_gap']]

        return jsonify({
            'ok':           True,
            'total_comps':  total_comps,
            'total_kws':    len(result_list),
            'gaps':         gaps[:15],
            'shared':       shared[:15],
            'all_keywords': result_list[:30],
        })

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ⚠️ LEGACY — NO USAR EN PRODUCCIÓN
# Endpoint obsoleto reemplazado por api_optimizar_pub_v2 (línea 9937).
# La UI usa /api/optimizar-pub-v2 desde abril 2026 (commit f00982d).
# Mantenido temporalmente como referencia. Pendiente eliminación.
# Si pensás aplicar cambios acá, primero verificá si vale la pena
# o si conviene deprecarlo de una vez.
@app.route('/api/optimizar-pub', methods=['POST'])
def api_optimizar_pub():
    """SSE: genera título y descripción optimizados para una publicación."""
    from core.account_manager import AccountManager
    import time as _time

    body    = request.get_json() or {}
    alias   = body.get('alias', '')
    item_id = body.get('item_id', '').strip().upper()
    contexto = body.get('contexto', '').strip()
    # IDs de competidores elegidos manualmente por el usuario (lista de product IDs)
    manual_competitor_ids      = body.get('competitor_ids', [])
    manual_competitor_titles   = body.get('competitor_titles', [])
    manual_competitor_products = body.get('competitor_products', [])  # [{id,title,attributes,description,main_features}]
    kw_research                = body.get('kw_research') or {}        # {titulo:[], descripcion:[], long_tail:[]}

    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Falta alias o item_id'}), 400

    def generate():
        try:
            manager = AccountManager()
            client  = manager.get_client(alias)

            # ── 1. Datos de la publicación desde ML API ───────────────────────
            yield _sse({'type': 'step', 'msg': f'Leyendo publicación {item_id}...'})
            item            = client.get_item(item_id)
            title           = item.get('title', '')
            price           = item.get('price', 0)
            cat_id          = item.get('category_id', '')
            my_listing_type = item.get('listing_type_id', '')
            my_free_ship    = item.get('shipping', {}).get('free_shipping', False)
            my_sold         = item.get('sold_quantity', 0)
            my_photos       = len(item.get('pictures', []))
            my_attrs = [
                f"{a['name']}: {a['value_name']}"
                for a in item.get('attributes', [])
                if a.get('value_name') and a.get('name') and a.get('value_name') != 'No especificado'
            ]

            my_description = ''
            try:
                dd = client._get(f'/items/{item_id}/description')
                my_description = dd.get('plain_text', '') or dd.get('text', '')
            except Exception:
                pass

            cat_name = cat_id
            try:
                cat_name = client._get(f'/categories/{cat_id}').get('name', cat_id)
            except Exception:
                pass

            my_reviews_rating = 0.0
            my_reviews_total  = 0
            my_reviews_text   = []
            try:
                rr = client._get(f'/reviews/item/{item_id}')
                my_reviews_rating = round(rr.get('rating_average', 0), 1)
                my_reviews_total  = rr.get('paging', {}).get('total', 0)
                my_reviews_text   = [
                    r.get('content', '') for r in rr.get('reviews', [])
                    if r.get('content', '').strip()
                ][:6]
            except Exception:
                pass

            # ── Q&A de mi propia publicación ──────────────────────────────────
            my_questions_unanswered = []
            my_questions_sample     = []
            try:
                import requests as _req_lib
                _q_headers = {'Authorization': f'Bearer {client.account.access_token}'}
                _qu = _req_lib.get(
                    'https://api.mercadolibre.com/questions/search',
                    headers=_q_headers,
                    params={'item': item_id, 'status': 'UNANSWERED', 'limit': 20},
                    timeout=8
                )
                if _qu.ok:
                    my_questions_unanswered = [
                        q.get('text', '').strip() for q in _qu.json().get('questions', [])
                        if q.get('text', '').strip()
                    ][:8]
                _qa = _req_lib.get(
                    'https://api.mercadolibre.com/questions/search',
                    headers=_q_headers,
                    params={'item': item_id, 'status': 'ANSWERED', 'limit': 20},
                    timeout=8
                )
                if _qa.ok:
                    my_questions_sample = [
                        {
                            'q': q.get('text', '').strip(),
                            'a': (q.get('answer') or {}).get('text', '')[:150].strip(),
                        }
                        for q in _qa.json().get('questions', [])
                        if q.get('text', '').strip() and (q.get('answer') or {}).get('text', '').strip()
                    ][:8]
            except Exception:
                pass

            # ── 2. Performance real desde archivos locales ────────────────────
            yield _sse({'type': 'step', 'msg': 'Cargando métricas reales de performance...'})

            stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json'))
            pos_data   = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json'))
            rep_data   = load_json(os.path.join(DATA_DIR, f'reputacion_{safe(alias)}.json'))
            comp_data  = load_json(os.path.join(DATA_DIR, f'competencia_{safe(alias)}.json'))

            # Stock metrics
            my_stock_item = next(
                (i for i in (stock_data or {}).get('items', []) if i.get('id') == item_id), {}
            )
            visitas_30d = my_stock_item.get('visitas_30d') or 0
            ventas_30d  = my_stock_item.get('ventas_30d')  or 0
            conv_pct    = my_stock_item.get('conversion_pct') or 0.0
            velocidad   = my_stock_item.get('velocidad') or 0.0
            dias_stock  = my_stock_item.get('dias_stock') or 0
            margen_pct  = my_stock_item.get('margen_pct')
            stock_qty   = my_stock_item.get('stock') or 0

            # Posición en búsquedas
            pos_item    = (pos_data or {}).get(item_id, {})
            pos_history = pos_item.get('history', {})
            pos_sorted  = sorted(pos_history.items())
            pos_current = pos_sorted[-1][1] if pos_sorted else None
            pos_trend   = None
            if len(pos_sorted) >= 2:
                prev = pos_sorted[-2][1]
                curr = pos_sorted[-1][1]
                if prev == 999 and curr == 999:
                    pos_trend = 'sin_ranking'
                elif curr < prev:
                    pos_trend = 'subiendo'
                elif curr > prev:
                    pos_trend = 'bajando'
                else:
                    pos_trend = 'estable'

            # Reputación
            rep_latest       = ((rep_data or []) or [{}])[-1]
            rep_nivel        = rep_latest.get('nivel', '')
            rep_reclamos     = rep_latest.get('reclamos_pct', 0) or 0
            rep_demoras      = rep_latest.get('demoras_pct', 0) or 0
            rep_cancelaciones= rep_latest.get('cancelaciones_pct', 0) or 0
            rep_ps           = rep_latest.get('power_seller', '')

            # Competidores de la misma categoría desde datos locales
            comp_titles_local = []
            comp_kw_faltantes = []
            if comp_data:
                for cat_key, cat_val in comp_data.get('categorias', {}).items():
                    for pub in cat_val.get('mis_publicaciones', []):
                        if pub.get('id') == item_id:
                            comp_titles_local = cat_val.get('competidores', [])[:8]
                            comp_kw_faltantes = pub.get('keywords_faltantes', [])
                            break

            # ── Historial de optimizaciones con resultados reales ─────────────
            opt_historial_block = _build_opt_history_block(alias)

            # ── Diagnóstico automático de bottleneck ──────────────────────────
            if visitas_30d == 0:
                bottleneck = 'SIN DATOS — actualizá el módulo de stock para obtener métricas.'
                bottleneck_prioridad = 'datos'
            elif visitas_30d < 40 and conv_pct >= 4:
                bottleneck = (f'TRÁFICO — solo {visitas_30d} visitas/mes pero convertís bien ({conv_pct:.1f}%). '
                              f'El título/keywords no atraen suficiente tráfico. '
                              f'ACCIÓN: mejorar keywords del título para aumentar impresiones.')
                bottleneck_prioridad = 'titulo_keywords'
            elif visitas_30d >= 40 and conv_pct < 2.0:
                bottleneck = (f'CONVERSIÓN — {visitas_30d} visitas/mes pero solo {conv_pct:.1f}% compran. '
                              f'El listing no convence una vez que el comprador entra. '
                              f'ACCIÓN: mejorar descripción, fotos y precio vs competencia.')
                bottleneck_prioridad = 'descripcion_precio'
            elif visitas_30d < 40 and conv_pct < 2.0:
                bottleneck = (f'CRÍTICO — {visitas_30d} visitas/mes Y {conv_pct:.1f}% conversión. '
                              f'Problema doble: ni trae tráfico ni convierte. '
                              f'ACCIÓN: reescribir título completo + descripción + revisar precio.')
                bottleneck_prioridad = 'todo'
            else:
                bottleneck = (f'ESCALADO — {visitas_30d} visitas/mes con {conv_pct:.1f}% conversión ({ventas_30d} ventas/mes). '
                              f'Base sólida. ACCIÓN: escalar tráfico con long-tail keywords y ganar posición.')
                bottleneck_prioridad = 'escalar'

            # Posición en string legible
            if pos_current is None or pos_current == 999:
                pos_str = 'No aparece en búsquedas monitoreadas (posición >50 o sin datos)'
            elif pos_current <= 5:
                pos_str = f'Top {pos_current} — excelente visibilidad'
            elif pos_current <= 15:
                pos_str = f'Posición #{pos_current} — buena, mejorable'
            else:
                pos_str = f'Posición #{pos_current} — baja visibilidad'
            trend_map = {'subiendo': '📈 mejorando', 'bajando': '📉 empeorando',
                         'estable': '➡️ estable', 'sin_ranking': '❌ sin ranking'}
            if pos_trend:
                pos_str += f' | Tendencia: {trend_map.get(pos_trend, "")}'
            if pos_sorted:
                pos_str += f' | Última medición: {pos_sorted[-1][0]}'

            # Reputación en string
            if rep_reclamos > 2:
                rep_str = (f'⚠️ CRÍTICO: {rep_reclamos:.1f}% reclamos (límite ML=2%). '
                           f'El algoritmo penaliza tu visibilidad aunque el listing sea perfecto. '
                           f'Resolver reclamos es PRIORITARIO sobre cualquier optimización de texto.')
            elif rep_reclamos > 1:
                rep_str = f'ADVERTENCIA: {rep_reclamos:.1f}% reclamos — en zona de riesgo (límite 2%).'
            else:
                rep_str = f'OK — {rep_reclamos:.1f}% reclamos.'
            if rep_demoras > 10:
                rep_str += f' Demoras: {rep_demoras:.1f}% (alto — afecta ranking).'
            if rep_cancelaciones > 1.5:
                rep_str += f' Cancelaciones: {rep_cancelaciones:.1f}% (alto).'
            if rep_ps:
                rep_str += f' Power Seller: {rep_ps.upper()}.'

            # ── 3. Bloque de competidores ─────────────────────────────────────
            comp_blocks = []

            # Determinar lista efectiva de competidores con datos completos
            if manual_competitor_products:
                yield _sse({'type': 'step', 'msg': f'Procesando {len(manual_competitor_products)} competidores seleccionados...'})
                effective_comps = manual_competitor_products
            else:
                # Auto-fetch: buscar top competidores por keyword con datos completos
                yield _sse({'type': 'step', 'msg': 'Buscando competidores con datos completos en ML...'})
                client._ensure_token()
                _sw = {'de','para','con','sin','y','el','la','los','las','un','una','por','al','del'}
                _tw = [w for w in title.lower().split() if len(w) > 3 and w not in _sw]
                _kw_seed = ' '.join(_tw[:4]) if _tw else title
                _as = _ml_autosuggest(_kw_seed, limit=1)
                _kw_comps = _as[0] if _as else _kw_seed
                effective_comps = _fetch_competitors_full(
                    _kw_comps, client.account.access_token, exclude_id=item_id, limit=5
                )

            if effective_comps:
                for i, cp in enumerate(effective_comps[:6], 1):
                    lines_c = [f'━━━ COMPETIDOR {i}: {cp.get("title", "")} ━━━']
                    meta = []
                    if cp.get('seller'):       meta.append(f'Vendedor: {cp["seller"]}')
                    if cp.get('sold_quantity'): meta.append(f'Ventas: {cp["sold_quantity"]:,}')
                    if cp.get('price'):         meta.append(f'Precio: ${cp["price"]:,.0f}')
                    meta.append('Envío gratis: Sí' if cp.get('free_ship') else 'Envío gratis: No')
                    meta.append('PREMIUM' if cp.get('premium') else 'CLÁSICA')
                    if cp.get('photos_count'):  meta.append(f'Fotos: {cp["photos_count"]}')
                    if cp.get('reviews_rating'): meta.append(f'Rating: {cp["reviews_rating"]}/5 ({cp.get("reviews_total",0)} reseñas)')
                    lines_c.append(' | '.join(meta))
                    attrs = cp.get('attributes', [])
                    if attrs:
                        lines_c.append('\nFICHA TÉCNICA:')
                        for a in attrs[:20]:
                            lines_c.append(f'  - {a["name"]}: {a["value"]}')
                    feats = cp.get('main_features', [])
                    if feats:
                        lines_c.append('\nCARACTERÍSTICAS DESTACADAS:')
                        for f in feats[:6]:
                            lines_c.append(f'  • {f}')
                    desc = cp.get('description', '')
                    if desc:
                        lines_c.append(f'\nDESCRIPCIÓN:\n{desc[:800]}')
                    reviews = cp.get('reviews_sample', [])
                    if reviews:
                        lines_c.append('\nRESEÑAS DE COMPRADORES (feedback real):')
                        for rv in reviews[:6]:
                            lines_c.append(f'  "{rv}"')
                    questions_qa = cp.get('questions_qa', [])
                    if questions_qa:
                        lines_c.append('\nPREGUNTAS REALES DE COMPRADORES (keywords long-tail + objeciones):')
                        for qa in questions_qa[:10]:
                            lines_c.append(f'  P: {qa["q"]}')
                            if qa.get('a'):
                                lines_c.append(f'  R: {qa["a"][:200]}')
                    comp_blocks.append('\n'.join(lines_c))
            elif comp_titles_local:
                # Fallback: solo títulos del análisis guardado
                for i, t in enumerate(comp_titles_local[:5], 1):
                    comp_blocks.append(f'━━━ COMPETIDOR {i}: {t} ━━━')
            else:
                # Último recurso: búsqueda por categoría
                client._ensure_token()
                comps = ml_get_competitors(cat_id, client.account.access_token, limit=6)
                comps = [c for c in comps if c.get('id') != item_id][:5]
                for i, c in enumerate(comps, 1):
                    line = f'━━━ COMPETIDOR {i}: {c.get("title","")} ━━━'
                    if c.get('price'): line += f'\nPrecio: ${c["price"]:,.0f}'
                    comp_blocks.append(line)

            comp_section = '\n\n'.join(comp_blocks) or 'Sin competidores disponibles.'

            # ── Análisis de precio vs mercado ─────────────────────────────────
            price_analysis_str = ''
            if price > 0:
                _comp_prices = sorted([
                    cp.get('price', 0) for cp in effective_comps
                    if cp.get('price', 0) > 0
                ])
                if _comp_prices:
                    import statistics as _stats
                    _median = _stats.median(_comp_prices)
                    _min    = _comp_prices[0]
                    _max    = _comp_prices[-1]
                    _below  = sum(1 for p in _comp_prices if p < price)
                    _pct    = round((_below / len(_comp_prices)) * 100)
                    _diff_median = round(((price - _median) / _median) * 100, 1)
                    _diff_min    = round(((price - _min) / _min) * 100, 1)
                    if _pct >= 75:
                        _pos_label = f'más caro que el {_pct}% del mercado ⚠️'
                    elif _pct >= 50:
                        _pos_label = f'percentil {_pct} — por encima de la mediana'
                    elif _pct >= 25:
                        _pos_label = f'percentil {_pct} — por debajo de la mediana ✅'
                    else:
                        _pos_label = f'más barato que el {100-_pct}% del mercado ✅'
                    price_analysis_str = (
                        f'Tu precio: ${price:,.0f} — {_pos_label}\n'
                        f'Rango mercado: ${_min:,.0f} – ${_max:,.0f} | Mediana: ${_median:,.0f}\n'
                        f'Diferencia vs mediana: {_diff_median:+.1f}% | Diferencia vs más barato: {_diff_min:+.1f}%'
                    )

            # ── 4. Atributos de categoría (API oficial ML) ────────────────────
            yield _sse({'type': 'step', 'msg': 'Obteniendo atributos oficiales de la categoría...'})
            client._ensure_token()
            ml_headers = {'Authorization': f'Bearer {client.account.access_token}'}
            req_attrs, opt_attrs = _fetch_category_attributes(cat_id, ml_headers)

            def _fmt_attr(a):
                line = f"  {a['name']}"
                if a.get('values'):
                    line += f" → valores: {', '.join(a['values'])}"
                return line

            if req_attrs or opt_attrs:
                parts = []
                if req_attrs:
                    parts.append('OBLIGATORIOS:')
                    parts.extend(_fmt_attr(a) for a in req_attrs)
                if opt_attrs:
                    parts.append(f'OPCIONALES ({len(opt_attrs)}):')
                    parts.extend(_fmt_attr(a) for a in opt_attrs[:25])
                cat_attrs_block = '\n'.join(parts)
            else:
                cat_attrs_block = '(no disponible)'

            # ── 5. Keywords reales del autosuggest de ML — con scoring por jerarquía ──
            yield _sse({'type': 'step', 'msg': 'Obteniendo keywords reales del motor de búsqueda de ML...'})
            import time as _t2

            # Prefijos que identifican búsquedas informacionales (no son compradores)
            _INFO_PREFIXES = (
                'como ', 'cómo ', 'que es ', 'qué es ', 'por que ', 'por qué ',
                'donde ', 'dónde ', 'cuando ', 'cuándo ', 'cual es ', 'cuál es ',
                'para que ', 'para qué ', 'tutorial ', 'aprende ', 'tips ',
                'que significa ', 'qué significa ', 'diferencia entre ',
            )

            def _is_informational(kw: str) -> bool:
                kw_l = kw.lower().strip()
                return any(kw_l.startswith(p) for p in _INFO_PREFIXES)

            stopwords = {'de','para','con','sin','y','el','la','los','las','un','una','por','al','del'}
            title_words = [w for w in title.lower().split() if len(w) > 3 and w not in stopwords]
            queries_as = list({
                title.lower(),
                ' '.join(title_words[:3]) if len(title_words) >= 3 else ' '.join(title_words),
                ' '.join(title_words[:2]) if len(title_words) >= 2 else '',
            } - {''})

            # kw_scores[keyword] = {'score': int, 'queries': int, 'informational': bool}
            # Score base: posición en autosuggest → pos 1 = 10 pts, pos 2 = 9 pts, ..., pos 10 = 1 pt
            # Bonus: +5 si aparece en 2+ queries distintas (validación cruzada de volumen)
            kw_scores   = {}
            seen_as     = set()
            all_suggestions = []

            for q in queries_as[:3]:
                for pos, s in enumerate(_ml_autosuggest(q, limit=10), 1):
                    score = max(0, 11 - pos)
                    if s not in kw_scores:
                        kw_scores[s] = {'score': 0, 'queries': 0, 'informational': _is_informational(s)}
                    kw_scores[s]['score']   += score
                    kw_scores[s]['queries'] += 1
                    if s not in seen_as:
                        seen_as.add(s)
                        all_suggestions.append(s)
                _t2.sleep(0.1)

            # Bonus por validación cruzada: keyword que aparece en 2+ queries distintas
            for _kd in kw_scores.values():
                if _kd['queries'] >= 2:
                    _kd['score'] += 5

            # ── Expandir con vocabulario real de competidores ─────────────────
            _comp_kws_nuevas = []
            if effective_comps:
                yield _sse({'type': 'step', 'msg': 'Expandiendo keywords con vocabulario de competidores...'})
                _comp_titles_list = [cp.get('title', '') for cp in effective_comps if cp.get('title')]
                for _kw in _competitor_seeded_autosuggest(_comp_titles_list, title):
                    if _kw not in seen_as:
                        seen_as.add(_kw)
                        all_suggestions.append(_kw)
                        _comp_kws_nuevas.append(_kw)
                    # Score 6 para keywords de competidores que no aparecieron en autosuggest propio
                    if _kw not in kw_scores:
                        kw_scores[_kw] = {'score': 6, 'queries': 0, 'informational': _is_informational(_kw)}

            # ── Re-ordenar por score real (mayor volumen primero) ─────────────
            all_suggestions.sort(key=lambda s: -kw_scores.get(s, {}).get('score', 0))

            # ── Separar en tiers de jerarquía ─────────────────────────────────
            _comp_set = set(_comp_kws_nuevas)
            kw_tier1        = []   # score >= 10, no informacional → van al TÍTULO (posición 1-2)
            kw_tier2        = []   # score 5-9, no informacional  → complementan título o descripción
            kw_tier3        = []   # perfiles alternativos de competidores → título alt 3 o descripción
            kw_info         = []   # informacionales → solo descripción, nunca título

            for s in all_suggestions:
                _d  = kw_scores.get(s, {})
                _sc = _d.get('score', 0)
                _is_info = _d.get('informational', False)
                _is_comp_only = (s in _comp_set) and (_d.get('queries', 0) == 0)
                if _is_info:
                    kw_info.append(s)
                elif _is_comp_only:
                    kw_tier3.append(s)
                elif _sc >= 10:
                    kw_tier1.append(s)
                else:
                    kw_tier2.append(s)

            # ── Construir kw_block jerarquizado para el prompt de Claude ───────
            def _kw_label(s):
                return ' ★ ya en tu título' if s.lower() in title.lower() else ''

            kw_block_parts = []
            if kw_tier1:
                kw_block_parts.append('TIER 1 — MÁXIMO VOLUMEN DE BÚSQUEDA (estos van en posición 1-2 del título):')
                for i, s in enumerate(kw_tier1[:8], 1):
                    kw_block_parts.append(f'  {i}. "{s}"{_kw_label(s)}')
            if kw_tier2:
                kw_block_parts.append('TIER 2 — VOLUMEN ALTO (complementar título o primer párrafo de descripción):')
                for i, s in enumerate(kw_tier2[:10], 1):
                    kw_block_parts.append(f'  {i}. "{s}"{_kw_label(s)}')
            if kw_tier3:
                kw_block_parts.append('TIER 3 — PERFILES ALTERNATIVOS validados (para título alternativo 3 o descripción):')
                for i, s in enumerate(kw_tier3[:10], 1):
                    kw_block_parts.append(f'  {i}. "{s}"{_kw_label(s)}')
            if kw_info:
                kw_block_parts.append('INFORMACIONALES — solo descripción, NUNCA en título:')
                for s in kw_info[:6]:
                    kw_block_parts.append(f'  · "{s}"')
            if comp_kw_faltantes:
                kw_block_parts.append('KEYWORDS FALTANTES detectadas en análisis de competencia:')
                kw_block_parts.extend(f'  · "{k}"' for k in comp_kw_faltantes[:8])

            kw_block = '\n'.join(kw_block_parts) if kw_block_parts else '(no disponible)'

            # ── Research previo del usuario ───────────────────────────────────
            kw_research_block = ''
            if kw_research:
                parts_kw = []
                if kw_research.get('titulo'):
                    parts_kw.append('Para TÍTULO: ' + ', '.join(f'"{k}"' for k in kw_research['titulo']))
                if kw_research.get('descripcion'):
                    parts_kw.append('Variantes semánticas: ' + ', '.join(f'"{k}"' for k in kw_research['descripcion']))
                if kw_research.get('long_tail'):
                    parts_kw.append('Long-tail: ' + ', '.join(f'"{k}"' for k in kw_research['long_tail']))
                if parts_kw:
                    kw_research_block = '\nKEYWORDS INVESTIGADAS PREVIAMENTE:\n' + '\n'.join(parts_kw)

            # ── Emitir sección visible de keywords antes del análisis ──────────
            kw_section_lines = [f'## KEYWORDS DEL MERCADO — {title[:40]}{"…" if len(title)>40 else ""}']
            kw_section_lines.append('Keywords ordenadas por volumen real de búsqueda en ML:')
            kw_section_lines.append('')
            if kw_tier1:
                kw_section_lines.append('▌TIER 1 — MÁXIMO VOLUMEN (ir al título):')
                for s in kw_tier1[:8]:
                    tag = ' ✓' if s.lower() in title.lower() else ' ←'
                    kw_section_lines.append(f'  ★ {s}{tag}')
            if kw_tier2:
                kw_section_lines.append('')
                kw_section_lines.append('▌TIER 2 — VOLUMEN ALTO (complementar título o descripción):')
                for s in kw_tier2[:10]:
                    tag = ' ✓' if s.lower() in title.lower() else ''
                    kw_section_lines.append(f'  ▸ {s}{tag}')
            if kw_tier3:
                kw_section_lines.append('')
                kw_section_lines.append('▌TIER 3 — PERFILES ALTERNATIVOS (título alt o descripción):')
                for s in kw_tier3[:10]:
                    kw_section_lines.append(f'  ◆ {s}')
            if kw_info:
                kw_section_lines.append('')
                kw_section_lines.append('▌INFORMACIONALES — solo descripción:')
                for s in kw_info[:5]:
                    kw_section_lines.append(f'  · {s}')
            if comp_kw_faltantes:
                kw_section_lines.append('')
                kw_section_lines.append('▌FALTANTES detectadas en análisis de competencia:')
                for k in comp_kw_faltantes[:8]:
                    kw_section_lines.append(f'  ▸ {k}')
            if not kw_tier1 and not kw_tier2 and not comp_kw_faltantes:
                kw_section_lines.append('(sin datos disponibles)')
            yield _sse({'type': 'token', 'text': '\n'.join(kw_section_lines) + '\n\n'})

            # ── 6. Construir bloques del prompt ───────────────────────────────
            yield _sse({'type': 'step', 'msg': 'Iniciando análisis con IA...'})

            tipo_pub = 'PREMIUM (Gold Special/Pro)' if my_listing_type in ('gold_special','gold_pro') else 'CLÁSICA'
            diferencial_txt = f'\n\nDIFERENCIAL DEL VENDEDOR:\n{contexto}' if contexto else ''

            # ── Bloque de performance real ────────────────────────────────────
            perf_block = f"""MÉTRICAS REALES DE PERFORMANCE (últimos 30 días):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Visitas:       {visitas_30d:,}/mes
Ventas:        {ventas_30d:,}/mes
Conversión:    {conv_pct:.1f}% {'✅ buena' if conv_pct >= 3 else '⚠️ baja' if conv_pct >= 1 else '🔴 crítica'}
Vel. de venta: {velocidad:.2f} unidades/día
Stock:         {stock_qty} unidades ({int(dias_stock)} días restantes)
Margen:        {f'{margen_pct:.1f}%' if margen_pct else 'sin datos (cargá costos)'}

POSICIÓN EN BÚSQUEDAS:
{pos_str}

REPUTACIÓN DEL VENDEDOR:
{rep_str}

DIAGNÓSTICO DE BOTTLENECK:
{bottleneck}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

            # ── Bloque de publicación ─────────────────────────────────────────
            _reviews_block = ''
            if my_reviews_text:
                _reviews_block = (
                    f'\nMIS RESEÑAS DE COMPRADORES ({my_reviews_total} en total — {my_reviews_rating}/5):\n'
                    + '\n'.join(f'  "{r}"' for r in my_reviews_text)
                )
            _unanswered_block = ''
            if my_questions_unanswered:
                _unanswered_block = (
                    f'\nPREGUNTAS SIN RESPONDER ({len(my_questions_unanswered)} — CONVERSIÓN EN RIESGO):\n'
                    + '\n'.join(f'  ❌ {q}' for q in my_questions_unanswered)
                )
            _qa_block = ''
            if my_questions_sample:
                _qa_block = (
                    '\nPREGUNTAS FRECUENTES DE MIS COMPRADORES (temas a reforzar en descripción):\n'
                    + '\n'.join(
                        f'  P: {qa["q"]}\n  R: {qa["a"]}' for qa in my_questions_sample
                    )
                )
            _price_block = f'\n\nANÁLISIS DE PRECIO VS MERCADO:\n{price_analysis_str}' if price_analysis_str else ''
            _desc_truncated = len(my_description) > 1500
            pub_block = f"""MI PUBLICACIÓN:
Título actual ({len(title)} chars): {title}
Categoría: {cat_name} | Precio: ${price:,.0f} | Tipo: {tipo_pub}
Envío gratis: {'Sí' if my_free_ship else 'No'} | Fotos: {my_photos} | Ventas totales: {my_sold:,}
Rating: {my_reviews_rating}/5 ({my_reviews_total} reseñas){_price_block}

FICHA TÉCNICA ACTUAL ({len(my_attrs)} campos completados):
{chr(10).join(f'  · {a}' for a in my_attrs) or '  (sin atributos completados)'}

DESCRIPCIÓN ACTUAL ({len(my_description)} caracteres{' — mostrando primeros 1500' if _desc_truncated else ''}):
{my_description[:1500] or '(sin descripción)'}{_reviews_block}{_unanswered_block}{_qa_block}"""

            algo_block = """REGLAS DEL ALGORITMO ML:
TÍTULO: keyword principal al inicio · formato Producto+Marca+Atributo · ≤60 chars · sin artículos · sin símbolos · sin repetición
FICHA: cada campo vacío penaliza visibilidad · campos filtro (marca/modelo/talle/material) peso doble · campos requeridos vacíos = penalización directa
DESCRIPCIÓN: 600-800 palabras · keyword_principal: 2–3 apariciones en total — densidad objetivo ~1.5% · variantes semánticas · estructura: beneficio→spec→diferencial→uso→CTA
OTROS: Premium +40% exposición · envío gratis mejora ranking · mínimo 6 fotos · foto principal fondo blanco"""

            # ── Bloque de datos ML ────────────────────────────────────────────
            ml_data_block = f"""DATOS REALES DE ML
════════════════════════════
KEYWORDS REALES (autosuggest ML — ordenadas por popularidad de búsqueda):
{kw_block}{kw_research_block}

ATRIBUTOS OFICIALES DE LA CATEGORÍA "{cat_name}":
{cat_attrs_block}
════════════════════════════"""

            ai_client = anthropic.Anthropic()

            # ══════════════════════════════════════════════════════════════════
            # LLAMADA 1 — DIAGNÓSTICO + ANÁLISIS DE MERCADO
            # ══════════════════════════════════════════════════════════════════
            n_comp = len(manual_competitor_products) if manual_competitor_products else len(comp_blocks)
            yield _sse({'type': 'step', 'msg': f'Analizando performance + mercado...'})

            historial_section = f'\n\n{opt_historial_block}' if opt_historial_block else ''

            prompt_analisis = f"""Sos el mejor experto en MercadoLibre Argentina. Tenés acceso a las métricas REALES de performance de esta publicación. Usá esos datos para hacer un diagnóstico preciso y accionable.
FORMATO: bullet points concisos. Máximo 1 línea por ítem. Sin párrafos. Solo datos concretos y acciones claras.{diferencial_txt}{historial_section}

{perf_block}

{pub_block}

{algo_block}

{ml_data_block}

COMPETIDORES EN LA CATEGORÍA:
{comp_section}

## DIAGNÓSTICO DE PERFORMANCE
Basado en las métricas reales (visitas, conversión, posición), identificá:
· Bottleneck principal: [tráfico / conversión / precio / reputación] — [evidencia con números]
· Impacto estimado si se resuelve: [+X visitas/mes o +X% conversión]
· Prioridad de acción: [qué cambiar primero, segundo y tercero]
· Alerta de reputación: [si afecta el ranking, qué hacer]

## FORTALEZAS DE CADA COMPETIDOR
Por cada competidor disponible (máximo 4 bullets):
· C[N] — keywords en su título que yo no tengo: "[kw1]", "[kw2]"
· C[N] — atributos completados que yo tengo vacíos: [campo1], [campo2]
· C[N] — argumento de venta fuerte: [cita literal si disponible]
· C[N] — ventaja comercial: [precio/envío/tipo/ventas]

## PUNTAJE DE CALIDAD ACTUAL
TÍTULO: [X/10] — [razón ≤10 palabras] — {'⚠️ NO modificable (tuvo ventas)' if my_sold > 0 else '✅ modificable (sin ventas)'}
FICHA TÉCNICA: [X/10] — [razón ≤10 palabras]
DESCRIPCIÓN: [X/10] — [{len(my_description)} chars — razón ≤10 palabras]
FOTOS: [X/10] — [{my_photos} fotos — veredicto]
TIPO PUB: [X/10] — [{tipo_pub}]
ENVÍO: [X/10] — [{'gratis ✅' if my_free_ship else 'pago ⚠️'}]
PRECIO: [X/10] — [vs competidores en ≤10 palabras]
PUNTAJE TOTAL: [X/70] → [BAJO / MEDIO / BUENO / EXCELENTE]

## KEYWORDS FALTANTES (ordenadas por impacto)
REGLA: las keywords están clasificadas por volumen real. TIER 1 = máximo volumen. TIER 2 = alto. TIER 3 = perfiles alternativos. Informacionales = solo descripción.
► "[keyword]" — tier: [1/2/3] — aparece en: [autosuggest / competidor C[N]] — impacto en posicionamiento: [en 8 palabras]

## ANÁLISIS DE FICHA TÉCNICA
[Campo vacío] → valor correcto: [X] — fuente: [categoría ML / competidor C[N]]

## ANÁLISIS DE DESCRIPCIÓN
· Longitud: {len(my_description)} chars — [veredicto]
· Estructura: [problema principal]
· Keywords ausentes en descripción: [lista]
· Argumento de conversión faltante: [el más importante]"""

            analysis_text = '\n'.join(kw_section_lines) + '\n\n'
            with ai_client.messages.stream(
                model='claude-sonnet-4-6',
                max_tokens=3500,
                messages=[{'role': 'user', 'content': prompt_analisis}],
            ) as stream:
                for text in stream.text_stream:
                    analysis_text += text
                    yield _sse({'type': 'token', 'text': text})
                _fm = stream.get_final_message()
                _log_token_usage('Optimizar IA — Análisis mercado', 'claude-sonnet-4-6', _fm.usage.input_tokens, _fm.usage.output_tokens)

            # ══════════════════════════════════════════════════════════════════
            # LLAMADA 2 — OUTPUT FINAL: TÍTULOS + FICHA + DESCRIPCIÓN
            # ══════════════════════════════════════════════════════════════════
            yield _sse({'type': 'step', 'msg': '⚡ Construyendo output final optimizado...'})
            yield _sse({'type': 'token', 'text': '\n\n---\n\n'})

            sold_note = f'IMPORTANTE: Esta publicación tuvo {my_sold} ventas — el título NO se puede cambiar en ML. Generá los títulos alternativos de todas formas para cuando se cree una nueva publicación.' if my_sold > 0 else 'La publicación NO tuvo ventas aún — el título SÍ se puede cambiar.'

            # ── Clasificación ternaria de complejidad del producto ──
            _cat_norm = _norm_kw(cat_name)
            _cx, _sx  = 0, 0
            _cx_r, _sx_r = [], []
            if any(kw in _cat_norm for kw in _COMPLEX_CATS):
                _cx += 2; _cx_r.append("cat_compleja")
            if any(kw in _cat_norm for kw in _SIMPLE_CATS):
                _sx += 2; _sx_r.append("cat_simple")
            if len(my_attrs) >= 10:
                _cx += 1; _cx_r.append("muchos_attrs")
            elif 0 < len(my_attrs) <= 3:
                _sx += 1; _sx_r.append("pocos_attrs")
            if len(kw_info) >= 3:
                _cx += 1; _cx_r.append("kw_informacionales")
            if price > 0 and _comp_prices:
                _pct_v = sum(1 for p in _comp_prices if p < price) / len(_comp_prices)
                if _pct_v >= 0.75:
                    _cx += 1; _cx_r.append(f"precio_pct{round(_pct_v*100)}")
                elif _pct_v <= 0.25:
                    _sx += 1; _sx_r.append(f"precio_pct{round(_pct_v*100)}")
            _justif_cls      = ",".join(_cx_r + _sx_r) or "sin_señales"
            _is_fallback_cls = max(_cx, _sx) < 2
            if _cx >= 2 and _cx > _sx:
                _product_type = "COMPLEJO"
            elif _sx >= 2 and _sx > _cx:
                _product_type = "SIMPLE"
            else:
                _product_type = "INTERMEDIO"
            _log_cls = (
                f"[clasificacion] {_product_type} | fallback={_is_fallback_cls} | "
                f"titulo='{title[:40]}' | señales={_cx}c/{_sx}s | razones={_justif_cls}"
            )
            if _is_fallback_cls:
                app.logger.warning(_log_cls)
            else:
                app.logger.info(_log_cls)
            _type_lengths_a = {"SIMPLE": "600–1200", "INTERMEDIO": "1000–1800", "COMPLEJO": "1200–2500"}
            _type_structs_a = {"SIMPLE": "5 bloques", "INTERMEDIO": "9 bloques", "COMPLEJO": "9 bloques"}
            _type_block     = (
                f"═══ TIPO DE PRODUCTO (clasificado por Python — no reclasificar) ═══\n"
                f"Clasificación: {_product_type}\n"
                f"Justificación: {_justif_cls}\n"
                f"Estructura a usar: {_type_structs_a[_product_type]}\n"
                f"Longitud objetivo: {_type_lengths_a[_product_type]} chars\n"
                f"════════════════════════════════════════════════════"
            )

            # ── Instrucción de uso de reseñas (mejora: decirle a Claude CÓMO usarlas) ──
            if my_reviews_text:
                _sample_reviews_fmt = '\n'.join(f'  • "{r}"' for r in my_reviews_text[:4])
                _reviews_block_instruction = (
                    f'\nINSTRUCCIÓN PARA RESEÑAS — usá estos testimonios reales como prueba social:\n'
                    f'{_sample_reviews_fmt}\n'
                    f'CÓMO incorporarlos: extraé el resultado concreto o emocional de cada reseña (no copies verbatim). '
                    f'Reescribilo en segunda persona: "vas a notar...", "sentís que...", "el cambio es visible desde...". '
                    f'Integrá 2–3 en la prosa como experiencias de compradores reales, sin comillas ni atribución.'
                )
            else:
                _reviews_block_instruction = ''

            # ── Construir bloque de estructura adaptativo en Python (no en el f-string) ──
            _qa_note        = ' (fuente: Q&A real de tu publicación disponible arriba)' if my_questions_sample else ''
            _unanswered_note = (f' · Las {len(my_questions_unanswered)} preguntas sin responder '
                                f'indican objeciones activas — priorizarlas') if my_questions_unanswered else ''
            _sold_cred      = (f'{my_sold} ventas reales — usar como dato de credibilidad. '
                               if my_sold > 0 else 'garantía propia, respaldo, experiencia de uso — solo datos reales. ')
            _longitud_line  = (
                f"- Producto {_product_type.lower()} detectado → apuntá a "
                f"{_type_lengths_a[_product_type]} caracteres"
                + (" (no rellenar con genéricos para alcanzar un largo mayor)"
                   if _product_type == "SIMPLE" else "")
            )

            if _product_type != "SIMPLE":  # COMPLEJO o INTERMEDIO → 9 bloques
                _bloques_estructura = f"""ESTRUCTURA DE 9 BLOQUES (párrafos separados por línea en blanco, sin títulos ni bullets):
1. APERTURA SEO [CRÍTICO — dentro de los primeros 300 chars]: TIER 1 keyword #1 + problema real que resuelve + beneficio concreto
2. QUÉ ES + PARA QUIÉN: descripción del producto + perfil del comprador ideal
3. CÓMO FUNCIONA: mecanismo real de uso + por qué es efectivo
4. BENEFICIOS COMPROBABLES: concretos y verificables — sin generalidades
5. DIFERENCIACIÓN REAL: comparar contra los competidores analizados usando solo datos reales provistos
6. DUDAS Y OBJECIONES:
   PARTE A — Responder dudas frecuentes: integrar en prosa natural las preguntas frecuentes de compradores{_qa_note}
   PARTE B — Neutralizar objeciones: para cada miedo del nicho, una frase que lo desarme con dato concreto (no con promesa vacía){_unanswered_note}
7. ESPECIFICACIONES TÉCNICAS: en lenguaje del comprador, no del fabricante
8. PRUEBA SOCIAL + CONFIANZA: {_sold_cred}Nunca mencionar garantía ML (ya la muestra arriba){_reviews_block_instruction}
9. CIERRE Y CTA: refuerzo del beneficio principal + llamada a la acción + TIER 1 keyword #1 repetida"""
                _checklist_bloques = '□ 9 bloques presentes y diferenciados'
            else:
                _bloques_estructura = f"""ESTRUCTURA DE 5 BLOQUES (párrafos separados por línea en blanco, sin títulos ni bullets):
1. APERTURA SEO [CRÍTICO — dentro de los primeros 300 chars]: TIER 1 keyword #1 + problema real que resuelve + beneficio concreto
2. QUÉ ES + PARA QUIÉN: descripción del producto + perfil del comprador ideal en un párrafo directo
3. BENEFICIOS + DIFERENCIACIÓN: 3–4 beneficios concretos y verificables + comparación con competidores usando solo datos reales
4. DUDAS Y OBJECIONES: integrar en prosa las dudas frecuentes{_qa_note}{_unanswered_note}
5. PRUEBA SOCIAL + CIERRE Y CTA: {_sold_cred}Nunca mencionar garantía ML. Refuerzo del beneficio principal + llamada a la acción + TIER 1 keyword #1 repetida.{_reviews_block_instruction}"""
                _checklist_bloques = '□ 5 bloques presentes y diferenciados'

            prompt_sintesis = f"""Sos el mejor especialista en MercadoLibre Argentina. Tenés métricas reales, análisis de mercado y datos de la API de ML. Construí el output final perfecto.

{sold_note}{historial_section}

{_type_block}

{algo_block}

{ml_data_block}

{perf_block}

{pub_block}

ANÁLISIS DE MERCADO:
{analysis_text}

Generá SOLO las secciones de output. Sin repetir el análisis. Sin introducción. Directo al output.

REGLA DE ORO PARA TÍTULOS:
Las keywords están clasificadas por volumen REAL de búsqueda en ML.
TIER 1 = las más buscadas → SIEMPRE van en los primeros tokens del título.
TIER 2 = alto volumen → complementan el título.
TIER 3 = perfiles alternativos → para el título alternativo 3.
INFORMACIONALES → NUNCA en título, solo en descripción.

## TÍTULO ALTERNATIVO 1
TÍTULO: [TIER 1 keyword #1 como primer token obligatorio — completar con TIER 1 #2 o TIER 2 #1 — ≤60 chars — sin artículos]
Estrategia: [qué TIER 1 keywords captura + cómo resuelve el bottleneck {bottleneck_prioridad}]
Caracteres: [N/60]

## TÍTULO ALTERNATIVO 2
TÍTULO: [TIER 1 keyword #2 como primer token — completar con atributo diferencial o marca — ≤60 chars]
Estrategia: [qué búsquedas de TIER 1 y TIER 2 captura adicionalmente]
Caracteres: [N/60]

## TÍTULO ALTERNATIVO 3
TÍTULO: [TIER 3 keyword de perfil alternativo como primer token — ≤60 chars — captura segmento distinto]
Estrategia: [qué perfil de comprador alternativo captura que los otros dos títulos no cubren]
Caracteres: [N/60]

## FICHA TÉCNICA PERFECTA
INSTRUCCIÓN: Usá EXACTAMENTE los nombres de campo de "ATRIBUTOS OFICIALES DE LA CATEGORÍA". Completá TODOS los obligatorios y todos los opcionales aplicables.
REGLA CRÍTICA: Los valores de atributo deben ser semánticamente correctos y limpios. PROHIBIDO meter frases SEO o keywords dentro de valores de atributo.
  ✅ Correcto — Color: Negro
  ❌ Incorrecto — Color: Negro profesional para puntas abiertas
Si hay valores aceptados, usá uno de esos exactos. Si no podés inferir el valor con certeza → escribí [SUGERIR: descripción de qué dato va aquí] para que el vendedor lo complete.
Las gap keywords van SOLO en el título y en la descripción, nunca en los valores de la ficha.
Formato: un campo por línea sin viñetas:
[Nombre exacto del campo]: [valor exacto]

## DESCRIPCIÓN SUPERADORA

REGLAS TÉCNICAS DE ML:
- NUNCA repetir información que ya está en la ficha técnica — el comprador ya la leyó arriba
- NUNCA repetir datos que ML ya muestra: envío, cuotas, devolución estándar, garantía ML
- NUNCA inventar características técnicas no confirmadas en los datos provistos
- Texto plano únicamente — sin HTML, markdown, bullets con *, links, teléfonos, emails, URLs
- Español rioplatense (vos, tus, tu)
- PROHIBIDO: frases genéricas ("alta calidad", "excelente producto", "no te arrepentirás", "el mejor del mercado")

ZONA CRÍTICA — PRIMEROS 300 CARACTERES:
El algoritmo de ML decide aquí si el contenido es relevante o genérico.
DEBEN contener: TIER 1 keyword #1 + qué es el producto + para quién es + beneficio concreto.

LONGITUD:
{_longitud_line}
- Por encima de 3000: retornos decrecientes — evitar

DISTRIBUCIÓN DE KEYWORDS:
- TIER 1: 2–3 apariciones distribuidas (densidad ~1.5% del texto — contar)
- TIER 2: 2–3 apariciones cada una
- TIER 3: 1–2 apariciones (perfiles alternativos)
- INFORMACIONALES: usarlas como preguntas retóricas integradas en prosa — nunca en título

{_bloques_estructura}

TONO según categoría (detectar y aplicar):
- Belleza/cuidado personal → resultado visual, sensación, experiencia
- Salud/ortopedia/bienestar → dolor, alivio, funcionalidad real
- Electrónica/gadgets → facilidad de uso, resultado concreto, compatibilidad
- Moda/indumentaria → estilo, comodidad, ocasión de uso
- Hogar/muebles/herramientas → practicidad, medidas reales, integración

CHECKLIST DE VALIDACIÓN INTERNA (verificar antes de entregar):
□ TIER 1 keyword aparece 2–3 veces — contadas
□ 5+ keywords de TIER 2/3 distribuidas naturalmente
□ Bloque 1 completo dentro de los primeros 300 chars
{_checklist_bloques}
□ Sin información de ficha técnica repetida
□ Sin datos que ML ya muestra al comprador
□ Sin características inventadas
□ Sin frases genéricas
□ Bottleneck resuelto: {bottleneck_prioridad}"""

            synthesis_text = ''
            with ai_client.messages.stream(
                model='claude-sonnet-4-6',
                max_tokens=5500,
                messages=[{'role': 'user', 'content': prompt_sintesis}],
            ) as stream:
                for text in stream.text_stream:
                    synthesis_text += text
                    yield _sse({'type': 'token', 'text': text})
                _fm2 = stream.get_final_message()
                _log_token_usage('Optimizar IA — Output final', 'claude-sonnet-4-6', _fm2.usage.input_tokens, _fm2.usage.output_tokens)

            full_text = analysis_text + '\n\n' + synthesis_text

            # ── 4. Parsear secciones ──────────────────────────────────────────
            def extract_section(text, header):
                """Extrae el contenido entre ## HEADER y el siguiente ##"""
                pattern = rf'## {re.escape(header)}[ \t]*\n(.*?)(?=\n## |\Z)'
                m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
                return m.group(1).strip() if m else ''

            titulos_alt = []
            for n in ['1', '2', '3']:
                bloque = extract_section(full_text, f'TÍTULO ALTERNATIVO {n}')
                if bloque:
                    lines_t = [l.strip() for l in bloque.split('\n') if l.strip()]
                    # Buscar línea con prefijo "TÍTULO:" primero
                    titulo_linea = next(
                        (l.replace('TÍTULO:', '').strip() for l in lines_t
                         if l.upper().startswith('TÍTULO:')),
                        ''
                    )
                    # Fallback: primera línea que no sea estrategia/caracteres
                    if not titulo_linea:
                        titulo_linea = next(
                            (l for l in lines_t
                             if not l.lower().startswith('estrategia')
                             and not l.lower().startswith('caracter')
                             and not l.lower().startswith('título:')),
                            ''
                        )
                    estrategia = next(
                        (l.replace('Estrategia:', '').strip() for l in lines_t
                         if l.lower().startswith('estrategia')),
                        ''
                    )
                    if titulo_linea:
                        _t_chars = len(titulo_linea)
                        _t_warning = f' ⚠️ {_t_chars} chars — EXCEDE límite ML de 60' if _t_chars > 60 else ''
                        titulos_alt.append({
                            'titulo':    titulo_linea,
                            'estrategia': estrategia,
                            'chars':     _t_chars,
                            'warning':   _t_warning,
                        })

            titulo_nuevo       = titulos_alt[0]['titulo'] if titulos_alt else ''
            desc_nueva         = extract_section(full_text, 'DESCRIPCIÓN SUPERADORA')
            keywords_sec       = extract_section(full_text, 'PALABRAS CLAVE FALTANTES')
            ficha_sec          = extract_section(full_text, 'ANÁLISIS DE FICHA TÉCNICA')
            ficha_perfecta_sec = extract_section(full_text, 'FICHA TÉCNICA PERFECTA')
            desc_analisis      = extract_section(full_text, 'ANÁLISIS DE DESCRIPCIÓN')
            fortalezas_sec     = extract_section(full_text, 'FORTALEZAS DE CADA COMPETIDOR')
            puntaje_sec        = extract_section(full_text, 'PUNTAJE DE CALIDAD ACTUAL')

            # ── Validación post-generación de descripción ─────────────────────
            try:
                if desc_nueva:
                    _d_chars   = len(desc_nueva)
                    _d_preview = desc_nueva[:300]

                    # Longitud
                    if _d_chars < 600:
                        _d_len_status = f'⚠️ MUY CORTA ({_d_chars} chars) — ML puede considerarla insuficiente'
                    elif _d_chars > 3000:
                        _d_len_status = f'⚠️ MUY LARGA ({_d_chars} chars) — retornos decrecientes, considerar recortar'
                    elif _d_chars > 2500:
                        _d_len_status = f'✅ {_d_chars} chars — larga pero aceptable para producto complejo'
                    else:
                        _d_len_status = f'✅ {_d_chars} chars — longitud óptima'

                    # Keyword principal (TIER 1 #1) en los primeros 300 chars
                    _main_kw     = kw_tier1[0] if kw_tier1 else (kw_tier2[0] if kw_tier2 else '')
                    _kw_in_open  = _main_kw and _main_kw.lower() in _d_preview.lower()
                    _open_status = f'✅ keyword principal en apertura' if _kw_in_open else f'⚠️ keyword principal ausente en primeros 300 chars'

                    # Densidad keyword principal en el texto completo
                    _kw_count    = desc_nueva.lower().count(_main_kw.lower()) if _main_kw else 0
                    if _main_kw:
                        if _kw_count < 3:
                            _density_status = f'⚠️ "{_main_kw}" aparece {_kw_count}x — recomendado 3-5x'
                        elif _kw_count > 6:
                            _density_status = f'⚠️ "{_main_kw}" aparece {_kw_count}x — posible over-optimization'
                        else:
                            _density_status = f'✅ "{_main_kw}" aparece {_kw_count}x — densidad correcta'
                    else:
                        _density_status = '(sin keyword TIER 1 disponible)'

                    _val_lines = [
                        '\n\n---\n\n## VALIDACIÓN DE DESCRIPCIÓN GENERADA',
                        f'Longitud:   {_d_len_status}',
                        f'Apertura:   {_open_status}',
                        f'Densidad:   {_density_status}',
                    ]
                    yield _sse({'type': 'token', 'text': '\n'.join(_val_lines) + '\n'})
            except Exception:
                pass

            # ── 5. Persistir ──────────────────────────────────────────────────
            s = alias.replace(' ', '_').replace('/', '-')
            opt_path = os.path.join(DATA_DIR, f'optimizaciones_{s}.json')
            existing = load_json(opt_path) or {'optimizaciones': []}
            opts = existing.get('optimizaciones', [])
            opts = [o for o in opts if o.get('item_id') != item_id]
            opts.insert(0, {
                'item_id':           item_id,
                'titulo_actual':     title,
                'titulo_nuevo':      titulo_nuevo or title,
                'titulos_alt':       titulos_alt,
                'descripcion_nueva': desc_nueva,
                'keywords_faltantes': keywords_sec,
                'analisis_ficha':    ficha_sec,
                'ficha_perfecta':    ficha_perfecta_sec,
                'analisis_desc':     desc_analisis,
                'resumen_mercado':   fortalezas_sec,
                'puntaje_calidad':   puntaje_sec,
                'competidores_n':    len(manual_competitor_products),
                'competidores_ids':  [c.get('id','') for c in manual_competitor_products if c.get('id')],
                'fecha':             datetime.now().strftime('%Y-%m-%d %H:%M'),
                'aplicado':          False,
                'sold_quantity':     my_sold,
            })
            existing['optimizaciones'] = opts[:20]
            existing['fecha'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            save_json(opt_path, existing)

            yield _sse({'type': 'done'})

        except Exception as e:
            yield _sse({'type': 'error', 'msg': str(e)})

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control']     = 'no-cache'
    return resp


@app.route('/api/optimizar-pub-v2', methods=['POST'])
def api_optimizar_pub_v2():
    """
    Optimización IA usando run_full_optimization() de seo_optimizer.
    Mismo protocolo SSE que api_optimizar_pub. Guarda en el mismo JSON.
    """
    from core.account_manager import AccountManager
    from modules.seo_optimizer import run_full_optimization

    body         = request.get_json() or {}
    alias        = body.get('alias', '')
    item_id      = body.get('item_id', '').strip().upper()
    gap_keywords = body.get('gap_keywords', [])          # keywords gap del reverse analysis
    comp_prods   = body.get('competitor_products', [])   # competidores seleccionados
    kw_research  = body.get('kw_research') or {}

    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Falta alias o item_id'}), 400

    def generate():
        try:
            yield _sse({'type': 'step', 'msg': f'Conectando con MercadoLibre ({item_id})...'})
            manager = AccountManager()
            client  = manager.get_client(alias)

            # Inyectar gap keywords al contexto antes del análisis
            if gap_keywords:
                gap_block = (
                    '\n## GAP KEYWORDS — LENGUAJE DEL MERCADO\n'
                    'Estas keywords las usan tus competidores y vos NO las tenés. '
                    'Incorporalas en el TÍTULO (al menos la más importante) y en la DESCRIPCIÓN. '
                    'PROHIBIDO meterlas en valores de atributos de la ficha técnica:\n' +
                    '\n'.join(f'  ▸ {kw}' for kw in gap_keywords[:10])
                )
                yield _sse({'type': 'token', 'text': gap_block + '\n\n'})

            yield _sse({'type': 'step', 'msg': 'Ejecutando análisis completo: keywords + posiciones + competidores + IA...'})
            import sys
            try:
                result = run_full_optimization(item_id, client,
                                               competitor_products=comp_prods or None,
                                               gap_keywords=gap_keywords or None)
            except ValueError as ve:
                # Error esperado: item no encontrado o ID incorrecto
                yield _sse({'type': 'error', 'msg': str(ve)})
                return
            except Exception as opt_err:
                import traceback, sys as _sys
                tb = traceback.format_exc()
                print(f'[ERROR run_full_optimization] item={item_id}: {type(opt_err).__name__}: {opt_err}\n{tb}', file=_sys.stderr, flush=True)
                yield _sse({'type': 'error', 'msg': f'[{type(opt_err).__name__}] {opt_err}'})
                return

            if not result:
                yield _sse({'type': 'error', 'msg': f'No se pudo obtener la publicación {item_id}. Verificá que el ID sea correcto y pertenezca a la cuenta seleccionada.'})
                return

            # ── Mapear resultado al formato que espera la UI ──────────────────
            opt_plan  = result.get('optimization_plan', {})
            item_data = result.get('_item_data', {})
            analysis  = result.get('_analysis_text', '')

            def _sec(text, header):
                m = re.search(
                    rf'## {re.escape(header)}[ \t]*\n(.*?)(?=\n## |\Z)',
                    text, re.DOTALL | re.IGNORECASE
                )
                return m.group(1).strip() if m else ''

            # titulos_alt: normalizar de {tipo, titulo, estrategia} → {titulo, estrategia}
            titulos_alt = [
                {'titulo': t.get('titulo', ''), 'estrategia': t.get('estrategia', '')}
                for t in opt_plan.get('titles', [])
                if t.get('titulo')
            ]

            # titulo_nuevo: titles[0] → seo_result titulo_principal → ''
            seo_result  = result.get('_seo_result', {})
            titulo_nuevo = (
                titulos_alt[0]['titulo'] if titulos_alt
                else seo_result.get('titulo_principal', '')
            )

            # keywords_faltantes: solo las del autosuggest que no están en el título actual
            kw_analysis = result.get('keyword_analysis', [])
            kw_faltantes = [
                k['keyword'] for k in kw_analysis
                if not k.get('en_titulo_actual')
                and k.get('compatibilidad') in ('alta', 'media')
            ]
            keywords_faltantes_str = ', '.join(kw_faltantes[:10]) if kw_faltantes else ''

            summary = result.get('summary', {})
            tr_n      = opt_plan.get('titulo_recomendado_n', 0)
            tr_titulo = opt_plan.get('titulo_recomendado', '')
            tr_motivo = opt_plan.get('titulo_recomendado_motivo', '')

            record = {
                'item_id':                   item_id,
                'titulo_actual':             item_data.get('title', ''),
                'titulo_nuevo':              titulo_nuevo,
                'titulos_alt':               titulos_alt,
                'titulo_recomendado_n':      tr_n,
                'titulo_recomendado':        tr_titulo,
                'titulo_recomendado_motivo': tr_motivo,
                'descripcion_nueva':         opt_plan.get('description', ''),
                'ficha_perfecta':            opt_plan.get('attributes', ''),
                'keywords_faltantes':        keywords_faltantes_str,
                'correcciones_titulo':       opt_plan.get('correcciones_titulo', ''),
                'precio_recomendado':        opt_plan.get('precio_recomendado', ''),
                'fotos_recomendadas':        opt_plan.get('fotos_recomendadas', ''),
                'alerta_catalogo':           opt_plan.get('alerta_catalogo', ''),
                'category_path':             summary.get('category_path', ''),
                'score_actual':              summary.get('ml_score', 0),
                'score_proyectado':          summary.get('score_proyectado', 0),
                'score_oficial_ml':          (result.get('_ml_quality_oficial') or {}).get('score', 0),
                'score_oficial_nivel':       (result.get('_ml_quality_oficial') or {}).get('level', ''),
                'score_oficial_razones':     (result.get('_ml_quality_oficial') or {}).get('reasons', []),
                'title_violations':          result.get('_title_violations', []),
                'qa_insights':          result.get('_qa_insights', ''),
                'resumen_mercado':      _sec(analysis, 'ANÁLISIS DE COMPETIDORES'),
                'puntaje_calidad':      _sec(analysis, 'PUNTAJE DE CALIDAD ACTUAL'),
                'analisis_ficha':       _sec(analysis, 'ANÁLISIS DE FICHA TÉCNICA'),
                'analisis_desc':        _sec(analysis, 'ANÁLISIS DE DESCRIPCIÓN'),
                'competidores_n':       len(comp_prods),
                'competidores_ids':     [c.get('id','') for c in comp_prods if c.get('id')],
                'fecha':                datetime.now().strftime('%Y-%m-%d %H:%M'),
                'aplicado':             False,
                'sold_quantity':        item_data.get('sold_quantity', 0),
            }

            # ── Persistir ─────────────────────────────────────────────────────
            yield _sse({'type': 'step', 'msg': 'Guardando resultado...'})
            opt_path = os.path.join(DATA_DIR, f'optimizaciones_{safe(alias)}.json')
            existing = load_json(opt_path) or {'optimizaciones': []}
            opts = existing.get('optimizaciones', [])
            opts = [o for o in opts if o.get('item_id') != item_id]
            opts.insert(0, record)
            existing['optimizaciones'] = opts[:20]
            existing['fecha'] = record['fecha']
            save_json(opt_path, existing)

            yield _sse({'type': 'done'})

        except Exception as e:
            yield _sse({'type': 'error', 'msg': str(e)})

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control']     = 'no-cache'
    return resp


# ── Lanzar producto nuevo (v2 — mismo pipeline que Optimizaciones IA) ────────

@app.route('/lanzar-nuevo')
def lanzar_nuevo():
    accounts = get_accounts()
    alias    = request.args.get('alias', accounts[0]['alias'] if accounts else '')
    data_path = os.path.join(DATA_DIR, f'lanzamientos_v2_{safe(alias)}.json')
    saved     = load_json(data_path) or {}
    items     = saved.get('lanzamientos', [])
    pubs_disponibles = []
    try:
        mgr = __import__('core.account_manager', fromlist=['AccountManager']).AccountManager()
        client = mgr.get_client(alias)
        client._ensure_token()
        token = client.account.access_token
        heads = {'Authorization': f'Bearer {token}'}
        uid   = req_lib.get('https://api.mercadolibre.com/users/me', headers=heads, timeout=8).json().get('id')
        r     = req_lib.get(f'https://api.mercadolibre.com/users/{uid}/items/search',
                            headers=heads, params={'status': 'active', 'limit': 50}, timeout=10)
        if r.ok:
            ids = r.json().get('results', [])
            if ids:
                det = req_lib.get('https://api.mercadolibre.com/items',
                                  headers=heads, params={'ids': ','.join(ids[:20])}, timeout=10)
                if det.ok:
                    pubs_disponibles = [{'id': i['body']['id'], 'titulo': i['body'].get('title', '')}
                                        for i in det.json() if i.get('code') == 200]
    except Exception:
        pass
    return render_template('lanzar_nuevo.html', alias=alias, accounts=accounts,
                           items=items, pubs_disponibles=pubs_disponibles,
                           fecha=saved.get('fecha', ''))


@app.route('/api/lanzar-nuevo-v2', methods=['POST'])
def api_lanzar_nuevo_v2():
    """
    Pipeline completo para producto nuevo: mismo motor que Optimizaciones IA
    pero sin item_id existente. Usa run_new_listing() con competidores manuales.
    """
    from core.account_manager import AccountManager
    from modules.seo_optimizer import run_new_listing

    body         = request.get_json() or {}
    alias        = body.get('alias', '')
    product_idea = body.get('product_idea', '').strip()
    comp_prods   = body.get('competitor_products', [])
    gap_keywords = body.get('gap_keywords', [])

    if not alias or not product_idea:
        return jsonify({'ok': False, 'error': 'Falta alias o descripción del producto'}), 400

    def generate():
        try:
            yield _sse({'type': 'step', 'msg': f'Analizando mercado para: "{product_idea[:50]}"...'})
            manager = AccountManager()
            client  = manager.get_client(alias)

            if gap_keywords:
                gap_block = (
                    '\n## GAP KEYWORDS — LENGUAJE DEL MERCADO\n'
                    'Incorporalas en el TÍTULO y DESCRIPCIÓN:\n' +
                    '\n'.join(f'  ▸ {kw}' for kw in gap_keywords[:10])
                )
                yield _sse({'type': 'token', 'text': gap_block + '\n\n'})

            yield _sse({'type': 'step', 'msg': 'Ejecutando análisis: keywords + competidores + Q&A + IA...'})
            import sys
            try:
                result = run_new_listing(
                    product_idea, client,
                    competitor_products=comp_prods or None,
                    gap_keywords=gap_keywords or None,
                )
            except Exception as err:
                import traceback
                tb = traceback.format_exc()
                print(f'[ERROR run_new_listing] {err}\n{tb}', file=sys.stderr, flush=True)
                yield _sse({'type': 'error', 'msg': f'Error en análisis: {err}'})
                return

            if not result:
                yield _sse({'type': 'error', 'msg': 'No se pudo completar el análisis.'})
                return

            opt_plan = result.get('optimization_plan', {})
            analysis = result.get('_analysis_text', '')

            def _sec(text, header):
                m = re.search(
                    rf'## {re.escape(header)}[ \t]*\n(.*?)(?=\n## |\Z)',
                    text, re.DOTALL | re.IGNORECASE
                )
                return m.group(1).strip() if m else ''

            titulos_alt = [
                {'titulo': t.get('titulo', ''), 'estrategia': t.get('estrategia', '')}
                for t in opt_plan.get('titles', []) if t.get('titulo')
            ]

            record = {
                'product_idea':              product_idea,
                'titulo_nuevo':              titulos_alt[0]['titulo'] if titulos_alt else '',
                'titulos_alt':               titulos_alt,
                'titulo_recomendado_n':      opt_plan.get('titulo_recomendado_n', 0),
                'titulo_recomendado':        opt_plan.get('titulo_recomendado', ''),
                'titulo_recomendado_motivo': opt_plan.get('titulo_recomendado_motivo', ''),
                'descripcion_nueva':         opt_plan.get('description', ''),
                'ficha_perfecta':            opt_plan.get('attributes', ''),
                'keywords_faltantes':        ', '.join(
                    k['keyword'] for k in result.get('keyword_analysis', [])[:8]
                    if k.get('compatibilidad') in ('alta', 'media')
                ),
                'precio_recomendado':        opt_plan.get('precio_recomendado', ''),
                'fotos_recomendadas':        opt_plan.get('fotos_recomendadas', ''),
                'alerta_catalogo':           opt_plan.get('alerta_catalogo', ''),
                'qa_insights':               result.get('_qa_insights', ''),
                'resumen_mercado':           _sec(analysis, 'ANÁLISIS DE COMPETIDORES'),
                'puntaje_calidad':           _sec(analysis, 'PUNTAJE DE CALIDAD ACTUAL'),
                'analisis_ficha':            _sec(analysis, 'ANÁLISIS DE FICHA TÉCNICA'),
                'dificultad':                result.get('summary', {}).get('difficulty', ''),
                'categoria':                 result.get('summary', {}).get('category', ''),
                'category_path':             result.get('summary', {}).get('category_path', ''),
                'competidores_n':            len(comp_prods),
                'fecha':                     datetime.now().strftime('%Y-%m-%d %H:%M'),
            }

            yield _sse({'type': 'step', 'msg': 'Guardando resultado...'})
            data_path = os.path.join(DATA_DIR, f'lanzamientos_v2_{safe(alias)}.json')
            existing  = load_json(data_path) or {'lanzamientos': []}
            lanzs     = existing.get('lanzamientos', [])
            lanzs.insert(0, record)
            existing['lanzamientos'] = lanzs[:20]
            existing['fecha'] = record['fecha']
            save_json(data_path, existing)

            yield _sse({'type': 'done'})

        except Exception as e:
            yield _sse({'type': 'error', 'msg': str(e)})

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control']     = 'no-cache'
    return resp


@app.route('/api/borrar-lanzamiento', methods=['POST'])
def api_borrar_lanzamiento():
    """Elimina un lanzamiento del historial por índice."""
    body  = request.get_json() or {}
    alias = body.get('alias', '')
    idx   = body.get('idx', -1)

    if not alias:
        return jsonify({'ok': False, 'error': 'Falta alias'}), 400

    data_path = os.path.join(DATA_DIR, f'lanzamientos_v2_{safe(alias)}.json')
    existing  = load_json(data_path) or {'lanzamientos': []}
    lanzs     = existing.get('lanzamientos', [])

    if idx < 0 or idx >= len(lanzs):
        return jsonify({'ok': False, 'error': 'Índice fuera de rango'}), 400

    lanzs.pop(idx)
    existing['lanzamientos'] = lanzs
    save_json(data_path, existing)

    return jsonify({'ok': True})


# ── Lanzador web ─────────────────────────────────────────────────────────────

@app.route('/api/lanzar', methods=['POST'])
def api_lanzar():
    """Analiza un producto nuevo: competidores reales + atributos ML + 2 llamadas AI → publicación perfecta."""
    from modules.lanzador_productos import _gather_market_data
    import time as _t, re as _re

    producto    = request.form.get('producto', '').strip()
    imagen      = request.files.get('imagen')
    imagen_b64  = None
    imagen_mime = 'image/jpeg'

    if not producto:
        return jsonify({'ok': False, 'error': 'Falta el nombre del producto'}), 400

    if imagen and imagen.filename:
        raw = imagen.read()
        imagen_b64 = base64.b64encode(raw).decode()
        ct = imagen.content_type or ''
        if 'png' in ct:   imagen_mime = 'image/png'
        elif 'webp' in ct: imagen_mime = 'image/webp'

    accounts   = get_accounts()
    token      = accounts[0]['access_token'] if accounts else ''
    ml_headers = {'Authorization': f'Bearer {token}'}

    def generate():
        try:
            # ── Paso 1: Categoría y mercado ──────────────────────────────
            yield _sse({'type': 'step', 'msg': 'Detectando categoría y mercado en ML...'})
            market_data = _gather_market_data(producto, token)
            cat_id   = market_data.get('suggested_category_id', '')
            cat_name = market_data.get('suggested_category_name', '')

            yield _sse({'type': 'market',
                        'cat':   cat_name,
                        'total': market_data.get('catalog_count', 0),
                        'comps': market_data.get('competitor_names', [])[:4]})

            # ── Paso 2: Listings reales top por ventas ───────────────────
            yield _sse({'type': 'step', 'msg': 'Buscando publicaciones top en ventas...'})

            search_r = req_lib.get('https://api.mercadolibre.com/sites/MLA/search',
                params={'q': producto, 'sort': 'sold_quantity_desc', 'limit': 6},
                headers=ml_headers, timeout=12)

            real_comps = []
            if search_r.ok:
                for idx, r in enumerate(search_r.json().get('results', [])[:5], 1):
                    item_id = r.get('id', '')
                    if not item_id:
                        continue

                    det_r  = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}',
                                         headers=ml_headers, timeout=8)
                    item_d = det_r.json() if det_r.ok else {}

                    desc_r = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}/description',
                                         headers=ml_headers, timeout=6)
                    desc_t = desc_r.json().get('plain_text', '') if desc_r.ok else ''

                    rev_r  = req_lib.get(f'https://api.mercadolibre.com/reviews/item/{item_id}',
                                         params={'limit': 3}, headers=ml_headers, timeout=6)
                    rev_d  = rev_r.json() if rev_r.ok else {}

                    seller_id    = item_d.get('seller_id') or r.get('seller', {}).get('id')
                    seller_sales = 0
                    if seller_id:
                        sel_r = req_lib.get(f'https://api.mercadolibre.com/users/{seller_id}',
                                            headers=ml_headers, timeout=5)
                        if sel_r.ok:
                            seller_sales = sel_r.json().get('seller_reputation', {}).get('transactions', {}).get('completed', 0)

                    attrs     = item_d.get('attributes', r.get('attributes', []))
                    attr_dict = {a.get('name', ''): a.get('value_name', '')
                                 for a in attrs if a.get('name') and a.get('value_name')}

                    real_comps.append({
                        'n':             idx,
                        'id':            item_id,
                        'title':         item_d.get('title', r.get('title', '')),
                        'price':         item_d.get('price', r.get('price', 0)),
                        'sold':          item_d.get('sold_quantity', r.get('sold_quantity', 0)),
                        'shipping_free': (item_d.get('shipping') or r.get('shipping', {})).get('free_shipping', False),
                        'photos':        len(item_d.get('pictures', [])),
                        'listing_type':  item_d.get('listing_type_id', ''),
                        'attrs':         attr_dict,
                        'description':   desc_t[:400],
                        'reviews_rating': rev_d.get('rating_average', 0),
                        'reviews_count':  rev_d.get('paging', {}).get('total', 0),
                        'seller_sales':   seller_sales,
                    })
                    _t.sleep(0.2)

            yield _sse({'type': 'comps_detail', 'comps': [
                {'n': c['n'], 'title': c['title'][:55], 'price': c['price'],
                 'sold': c['sold'], 'shipping_free': c['shipping_free']}
                for c in real_comps
            ]})

            # ── Paso 3: Atributos oficiales + keywords reales ────────────
            yield _sse({'type': 'step', 'msg': 'Obteniendo atributos oficiales y keywords de ML...'})

            req_attrs, opt_attrs = _fetch_category_attributes(cat_id, ml_headers) if cat_id else ([], [])

            kw_candidates = set()
            for w in producto.lower().split():
                if len(w) > 3:
                    kw_candidates.add(w)
            for c in real_comps:
                for w in c['title'].lower().split():
                    if len(w) > 3:
                        kw_candidates.add(w)

            kw_predictions = _fetch_keyword_predictions(list(kw_candidates)[:5], ml_headers)

            ml_data_block = "DATOS REALES DE ML — FUENTE: API OFICIAL\n" + "=" * 40
            if req_attrs:
                ml_data_block += f"\n\nATRIBUTOS REQUERIDOS DE LA CATEGORÍA '{cat_name}':"
                for a in req_attrs[:15]:
                    line = f"  • {a['name']}"
                    if a.get('values'):
                        line += f" → valores aceptados: {', '.join(a['values'][:8])}"
                    ml_data_block += f"\n{line}"
            if opt_attrs:
                ml_data_block += "\n\nATRIBUTOS OPCIONALES (mejoran el puntaje de ML):"
                for a in opt_attrs[:10]:
                    line = f"  • {a['name']}"
                    if a.get('values'):
                        line += f" → valores: {', '.join(a['values'][:6])}"
                    ml_data_block += f"\n{line}"
            if kw_predictions:
                ml_data_block += "\n\nKEYWORDS VALIDADAS — BÚSQUEDAS REALES EN ML (mayor a menor popularidad):"
                for base_kw, preds in kw_predictions.items():
                    ml_data_block += f"\n  '{base_kw}' → " + " | ".join(preds[:6])

            yield _sse({'type': 'ml_data',
                        'attrs_req': len(req_attrs),
                        'attrs_opt': len(opt_attrs),
                        'keywords':  len(kw_predictions)})

            # ── Paso 4: Análisis competitivo — Call 1 ───────────────────
            yield _sse({'type': 'step', 'msg': 'Analizando competencia con IA...'})

            comp_section = ""
            for c in real_comps:
                comp_section += f"""
== COMPETIDOR {c['n']}: {c['title']} ==
- Precio: ${c['price']:,.0f} ARS | Ventas: {c['sold']} unid | Envío gratis: {'Sí' if c['shipping_free'] else 'No'}
- Fotos: {c['photos']} | Tipo: {c['listing_type']} | Rating: {c['reviews_rating']:.1f}/5 ({c['reviews_count']} opiniones)
- Vendedor: {c['seller_sales']} ventas históricas
- Ficha técnica: {', '.join(f"{k}: {v}" for k,v in list(c['attrs'].items())[:8]) or 'Sin datos'}
- Descripción (extracto): {c['description'][:300] or 'Sin descripción'}
"""

            prompt_analisis = f"""Sos experto en MercadoLibre Argentina. Analizás la competencia para un PRODUCTO NUEVO a lanzar.

PRODUCTO A LANZAR: {producto}
CATEGORÍA DETECTADA: {cat_name}

{comp_section if comp_section else 'No se encontraron competidores directos con datos completos en ML.'}

ANÁLISIS REQUERIDO — sé conciso y directo:

## FORTALEZAS DE LA COMPETENCIA
¿Qué hacen bien los competidores top? Citá por número de competidor.

## DEBILIDADES Y HUECOS DEL MERCADO
¿Qué no hacen bien? ¿Qué oportunidad existe para diferenciarse?

## KEYWORDS QUE DOMINAN LOS TÍTULOS GANADORES
Palabras clave de los mejores competidores con contexto de uso.

## PRECIO DE ENTRADA
Rango recomendado en ARS con estrategia. Considerá comisión ML 16.5% + envío $700.

## DIFERENCIADOR GANADOR
La ventaja principal que debe comunicar tu publicación para ser superadora.

## ESTRATEGIA DE FOTOS
Lista de 6 fotos clave basándote en los competidores top.

Respondé en menos de 600 palabras."""

            ai_client    = anthropic.Anthropic()
            analisis_text = ''
            with ai_client.messages.stream(
                model='claude-sonnet-4-6',
                max_tokens=2500,
                messages=[{'role': 'user', 'content': prompt_analisis}],
            ) as stream:
                for text in stream.text_stream:
                    analisis_text += text
                    yield _sse({'type': 'token_analisis', 'text': text})
                _lz_fm1 = stream.get_final_message()
                _log_token_usage('Lanzar Producto — Análisis competencia', 'claude-sonnet-4-6', _lz_fm1.usage.input_tokens, _lz_fm1.usage.output_tokens)

            _t.sleep(0.3)

            # ── Paso 5: Síntesis — publicación perfecta — Call 2 ────────
            yield _sse({'type': 'step', 'msg': 'Generando publicación perfecta lista para publicar...'})

            img_nota = ""
            if imagen_b64:
                img_nota = "\n\nNOTA: El vendedor adjuntó una foto del producto. Tené en cuenta su presentación y características visuales."

            prompt_sintesis = f"""Actuá como experto en lanzamiento de productos en MercadoLibre Argentina.
Este producto aún no está publicado. Construí una publicación desde cero con máxima probabilidad de posicionamiento y conversión.

PRODUCTO: {producto}
CATEGORÍA: {cat_name}

ANÁLISIS DE COMPETENCIA:
{analisis_text}

{ml_data_block}
{img_nota}

═══ PASO 1 — SELECCIÓN DE KEYWORDS ═══
Seleccioná del autosuggest real disponible arriba:
  - 1 keyword principal (mayor intención de compra)
  - 3 keywords secundarias
  - 5 long tail relevantes
Priorizar autosuggest. Evitar keywords genéricas sin intención.

═══ PASO 2 — TÍTULOS ═══
Reglas:
  - máximo 60 caracteres (contá antes de escribir)
  - sin palabras vacías (de, para, con, etc.)
  - keyword principal al inicio en TÍTULO 1
  - los 3 títulos deben ser estructuralmente distintos

═══ PASO 3 — FICHA TÉCNICA ═══
  - nombres EXACTOS de atributos oficiales de la categoría listados arriba
  - completar todos los obligatorios
  - opcionales relevantes según competidores
  - si no podés inferir el valor → escribí [SUGERIR: descripción de qué dato va aquí]

═══ PASO 4 — DESCRIPCIÓN ═══
  - 500–700 palabras reales útiles
  - español rioplatense (vos, tus)
  - sin markdown, párrafos separados por línea en blanco
  - detectar tipo de producto y ajustar tono
  - distribución de keywords:
      · keyword principal: 4–6 veces (inicio obligatorio + desarrollo + cierre)
      · secundarias: 2–3 veces cada una
      · long tail: 1–2 veces, sin forzar
  - estructura 8 párrafos:
      1. problema + beneficio inmediato (keyword principal aquí)
      2. qué es el producto + para quién
      3. cómo funciona + por qué es efectivo
      4. beneficios concretos y verificables
      5. diferenciación vs competidores analizados
      6. características técnicas en lenguaje del comprador
      7. confianza / garantía / uso
      8. cierre con intención de compra + keyword principal
  - PROHIBIDO: relleno, frases genéricas, exageraciones, repetir con otras palabras

VALIDACIÓN INTERNA antes de entregar:
  - keyword principal ≥ 4 veces en descripción
  - 3 títulos distintos entre sí
  - todos los atributos obligatorios completados

═══ FORMATO DE ENTREGA — SOLO ESTAS SECCIONES ═══

## KEYWORDS
- principal: [keyword]
- secundarias: [kw1], [kw2], [kw3]
- long tail: [lt1], [lt2], [lt3], [lt4], [lt5]

## TÍTULOS
1. [SEO máximo — keyword principal primero — ≤60 chars]
2. [balance — keyword + atributo diferencial — ≤60 chars]
3. [alta conversión — long tail de compra — ≤60 chars]

## FICHA TÉCNICA
[atributo]: [valor]

## DESCRIPCIÓN
[texto final — sin títulos internos, sin markdown]"""

            if imagen_b64:
                content_sint = [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': imagen_mime, 'data': imagen_b64}},
                    {'type': 'text', 'text': prompt_sintesis},
                ]
            else:
                content_sint = prompt_sintesis

            sintesis_text = ''
            with ai_client.messages.stream(
                model='claude-sonnet-4-6',
                max_tokens=3500,
                messages=[{'role': 'user', 'content': content_sint}],
            ) as stream:
                for text in stream.text_stream:
                    sintesis_text += text
                    yield _sse({'type': 'token_sintesis', 'text': text})
                _lz_fm2 = stream.get_final_message()
                _log_token_usage('Lanzar Producto — Síntesis publicación', 'claude-sonnet-4-6', _lz_fm2.usage.input_tokens, _lz_fm2.usage.output_tokens)

            # ── Parsear resultado estructurado ───────────────────────────
            def _sec_lanzar(text, header):
                m = _re.search(
                    rf'## {_re.escape(header)}[ \t]*\n([\s\S]+?)(?=\n## |\Z)',
                    text, _re.IGNORECASE
                )
                return m.group(1).strip() if m else ''

            titulos_raw = _sec_lanzar(sintesis_text, 'TÍTULOS')
            titulos = []
            for line in titulos_raw.splitlines():
                line = line.strip()
                m = _re.match(r'^[1-3]\.\s*(.+)', line)
                if m:
                    titulos.append(m.group(1).strip().strip('[]'))

            keywords_raw = _sec_lanzar(sintesis_text, 'KEYWORDS')
            ficha        = _sec_lanzar(sintesis_text, 'FICHA TÉCNICA')
            descripcion  = _sec_lanzar(sintesis_text, 'DESCRIPCIÓN')
            proyeccion   = ''  # eliminado del nuevo formato

            # ── Guardar JSON ─────────────────────────────────────────────
            os.makedirs(DATA_DIR, exist_ok=True)
            safe_p = producto[:30].replace(' ', '_').replace('/', '-')
            ts     = datetime.now().strftime('%Y%m%d_%H%M')
            path   = os.path.join(DATA_DIR, f'lanzamiento_{safe_p}_{ts}.json')
            save_json(path, {
                    'fecha':                ts,
                    'producto':             producto,
                    'market_data':          market_data,
                    'competidores_reales':  real_comps,
                    'analisis_competencia': analisis_text,
                    'sintesis':             sintesis_text,
                    'keywords':             keywords_raw,
                    'titulos_alt':          titulos,
                    'ficha_perfecta':       ficha,
                    'descripcion_nueva':    descripcion,
                    'proyeccion':           proyeccion,
                })

            yield _sse({
                'type':       'result',
                'titulos':    titulos,
                'ficha':      ficha,
                'descripcion': descripcion,
                'proyeccion': proyeccion,
            })
            yield _sse({'type': 'done'})

        except anthropic.BadRequestError as e:
            yield _sse({'type': 'error', 'msg': f'Error Anthropic: {e}'})
        except Exception as e:
            yield _sse({'type': 'error', 'msg': str(e)})

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/api/lanzar-producto', methods=['POST'])
def api_lanzar_producto():
    from core.account_manager import AccountManager
    import time as _time

    body  = request.get_json() or {}
    query = body.get('query', '').strip()
    if not query:
        return jsonify({'ok': False, 'error': 'Falta query'}), 400

    seeds = _seeds_from_title(query)
    kws   = _keywords_from_seeds(seeds, per_seed=10)[:20]

    # Competidores + categoría + atributos
    competidores = []
    category     = ''
    attributes   = []
    try:
        manager = AccountManager()
        accounts = manager.list_accounts()
        if accounts:
            alias0  = accounts[0]['alias']
            client0 = manager.get_client(alias0)
            token   = client0.account.access_token

            headers = {'Authorization': f'Bearer {token}'}

            # Buscar top 5 competidores (ordenados por ventas)
            resp = req_lib.get(
                'https://api.mercadolibre.com/sites/MLA/search',
                headers=headers,
                params={'q': query, 'limit': 5, 'sort': 'sold_quantity_desc'},
                timeout=10,
            )
            if resp.ok:
                results = resp.json().get('results', [])
                stop = {'de', 'del', 'la', 'el', 'los', 'las', 'con', 'para', 'por', 'en', 'un', 'una', 'y', 'a'}
                for r in results:
                    title        = r.get('title', '')
                    price        = r.get('price', 0)
                    sold         = r.get('sold_quantity', 0)
                    free_ship    = bool((r.get('shipping') or {}).get('free_shipping', False))
                    kw_list      = [w.lower() for w in title.split() if len(w) > 3 and w.lower() not in stop]
                    competidores.append({'title': title, 'price': price, 'sold_quantity': sold,
                                         'free_shipping': free_ship, 'keywords': kw_list})

                # Detectar categoría desde el primer resultado
                if results:
                    category = results[0].get('category_id', '')

            # Si no hay category_id del search, intentar domain_discovery
            if not category:
                r_dom = req_lib.get(
                    'https://api.mercadolibre.com/sites/MLA/domain_discovery/search',
                    headers=headers,
                    params={'q': query, 'limit': 1},
                    timeout=8,
                )
                if r_dom.ok:
                    doms = r_dom.json()
                    if doms:
                        category = doms[0].get('category_id', '')

            # Obtener atributos de la categoría
            if category:
                req_attrs, opt_attrs = _fetch_category_attributes(category, headers)
                attributes = [{'name': a['name'], 'id': a['id'], 'required': True,  **({'values': a['values']} if 'values' in a else {})} for a in req_attrs] + \
                             [{'name': a['name'], 'id': a['id'], 'required': False, **({'values': a['values']} if 'values' in a else {})} for a in opt_attrs]

    except Exception as e:
        app.logger.warning(f'[lanzar-producto] Error obteniendo datos de ML: {e}')

    # Generación IA — una sola llamada a Claude
    launch_result = {'keywords': {}, 'titulos_alt': [], 'ficha_perfecta': '', 'descripcion_nueva': ''}
    try:
        kws_str   = '\n'.join(f'- {k}' for k in kws[:15])
        comps_str = '\n'.join(
            f'- "{c["title"]}" | ${c["price"]:,.0f} | ventas: {c.get("sold_quantity", 0)} | '
            f'envío gratis: {"Sí" if c.get("free_shipping") else "No"} | kws: {", ".join(c["keywords"][:6])}'
            for c in competidores
        ) or '(sin datos)'
        req_names = [a['name'] for a in attributes if a.get('required')]
        opt_names = [a['name'] for a in attributes if not a.get('required')]
        attrs_str = (
            ('Obligatorios: ' + ', '.join(req_names) if req_names else '') +
            ('\nOpcionales: '  + ', '.join(opt_names[:10]) if opt_names else '')
        ).strip() or '(sin datos)'

        prompt = f"""Sos experto en posicionamiento y conversión en Mercado Libre Argentina.
Producto a lanzar: "{query}"
Categoría ML: {category or '(desconocida)'}

KEYWORDS DEL AUTOSUGGEST (ordenadas por popularidad):
{kws_str}

COMPETIDORES TOP 5:
{comps_str}

ATRIBUTOS DE LA CATEGORÍA:
{attrs_str}

Generá el siguiente contenido optimizado para ML Argentina. Seguí exactamente los encabezados.

## KEYWORDS
Principal: <keyword más buscada y relevante>
Secundarias: <3-5 keywords separadas por coma>
Long tail: <3-5 frases largas separadas por coma>

## TÍTULO ALTERNATIVO 1
<60-80 chars, keyword principal al inicio, modelo/especificación, diferenciador>

## TÍTULO ALTERNATIVO 2
<60-80 chars, segunda keyword fuerte, ventaja competitiva>

## TÍTULO ALTERNATIVO 3
<60-80 chars, variante con long tail o uso específico>

## FICHA TÉCNICA PERFECTA
<Lista de atributos obligatorios y opcionales con valores reales. Formato: Atributo: Valor. Un atributo por línea.>

## DESCRIPCIÓN SUPERADORA
<650-800 palabras. Estructura: párrafo de apertura con keyword principal → beneficios concretos → especificaciones técnicas → casos de uso → por qué elegirnos → llamada a la acción. Sin bullets genéricos. Tono natural, persuasivo.>

Respondé solo con las secciones indicadas, sin texto adicional."""

        ai   = anthropic.Anthropic()
        resp = ai.messages.create(
            model='claude-opus-4-6',
            max_tokens=4000,
            messages=[{'role': 'user', 'content': prompt}],
        )
        _log_token_usage('Lanzar Producto — Análisis completo', 'claude-opus-4-6', resp.usage.input_tokens, resp.usage.output_tokens)
        text = next((b.text for b in resp.content if hasattr(b, 'text')), '')

        def _sec(label: str) -> str:
            import re
            m = re.search(rf'## {re.escape(label)}\s*\n(.*?)(?=\n## |\Z)', text, re.S)
            return m.group(1).strip() if m else ''

        # Keywords
        kw_block = _sec('KEYWORDS')
        def _kw_line(prefix):
            import re
            m = re.search(rf'{prefix}:\s*(.+)', kw_block)
            return [x.strip() for x in m.group(1).split(',')] if m else []
        import re as _re
        m_principal = _re.search(r'Principal:\s*(.+)', kw_block)
        launch_result['keywords'] = {
            'principal':  m_principal.group(1).strip() if m_principal else '',
            'secundarias': _kw_line('Secundarias'),
            'long_tail':   _kw_line('Long tail'),
        }

        # Títulos
        for n in ['1', '2', '3']:
            t = _sec(f'TÍTULO ALTERNATIVO {n}')
            if t:
                launch_result['titulos_alt'].append({'titulo': t, 'estrategia': f'Variante {n}'})

        launch_result['ficha_perfecta']   = _sec('FICHA TÉCNICA PERFECTA')
        launch_result['descripcion_nueva'] = _sec('DESCRIPCIÓN SUPERADORA')

    except Exception as e:
        app.logger.warning(f'[lanzar-producto] Error en generación IA: {e}')

    return jsonify({'ok': True, 'query': query, 'autosuggest_kws': kws,
                    'competidores': competidores, 'category': category,
                    'attributes': attributes, 'launch_result': launch_result})


@app.route('/api/analizar-pub', methods=['POST'])
def api_analizar_pub():
    """SSE: análisis profundo de una publicación vs competencia real en ML + Claude."""
    from core.account_manager import AccountManager
    import time as _time

    body     = request.get_json() or {}
    alias    = body.get('alias', '')
    item_id  = body.get('item_id', '').strip().upper()
    contexto = body.get('contexto', '').strip()

    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Falta alias o item_id'}), 400

    def generate():
        try:
            manager = AccountManager()
            client  = manager.get_client(alias)

            # ── 1. Tu publicación completa ────────────────────────────────────
            yield _sse({'type': 'step', 'msg': f'Leyendo tu publicación {item_id}...'})
            item        = client.get_item(item_id)
            title       = item.get('title', '')
            price       = item.get('price', 0)
            category_id = item.get('category_id', '')
            pics_count  = len(item.get('pictures', []))
            free_ship   = item.get('shipping', {}).get('free_shipping', False)
            my_attrs    = {a['id']: a.get('value_name','') for a in item.get('attributes',[]) if a.get('value_name')}
            my_attrs_named = [f"{a.get('name')}: {a.get('value_name')}"
                              for a in item.get('attributes', [])
                              if a.get('value_name') and a.get('name')]
            description = ''
            try:
                dd = client._get(f'/items/{item_id}/description')
                description = (dd.get('plain_text','') or dd.get('text',''))[:800]
            except Exception:
                pass
            cat_name = category_id
            try:
                cat_name = client._get(f'/categories/{category_id}').get('name', category_id)
            except Exception:
                pass

            # Tipo de publicación y cuotas
            listing_type  = item.get('listing_type_id', '')
            listing_label = {'gold_special': 'Premium', 'gold_pro': 'Premium Pro',
                             'gold': 'Oro', 'silver': 'Plata', 'bronze': 'Bronce',
                             'free': 'Gratuita'}.get(listing_type, listing_type)
            # Cuotas sin interés
            installments = ''
            try:
                pay = client._get(f'/items/{item_id}/mercadopago_installments')
                best = max((p for p in pay.get('payer_costs', []) if p.get('installments', 1) > 1),
                           key=lambda x: x.get('installments', 0), default=None)
                if best:
                    installments = f"{best['installments']}x de ${best['installment_amount']:,.0f}"
            except Exception:
                pass

            # Atributos requeridos por ML para esta categoría
            cat_required_attrs = []
            cat_recommended_attrs = []
            try:
                cat_attrs_data = client._get(f'/categories/{category_id}/attributes')
                for a in cat_attrs_data:
                    tags = a.get('tags', {})
                    aid  = a.get('id', '')
                    name = a.get('name', '')
                    if not name:
                        continue
                    if tags.get('required') or tags.get('buy_required'):
                        cat_required_attrs.append({'id': aid, 'name': name,
                                                   'filled': aid in my_attrs})
                    elif tags.get('catalog_required') or (not tags.get('hidden') and not tags.get('read_only')):
                        cat_recommended_attrs.append({'id': aid, 'name': name,
                                                      'filled': aid in my_attrs})
            except Exception:
                pass

            missing_required = [a for a in cat_required_attrs if not a['filled']]
            missing_recommended = [a for a in cat_recommended_attrs if not a['filled']][:10]

            # Posición real: usar rank en highlights como proxy
            my_position = None

            yield _sse({'type': 'item_ok', 'title': title, 'price': price,
                        'category': cat_name, 'attrs_count': len(my_attrs),
                        'pics': pics_count, 'free_ship': free_ship,
                        'listing_type': listing_label,
                        'missing_required': len(missing_required),
                        'position': my_position})

            # ── 2. Top vendedores reales del mercado (via Highlights API) ──────
            yield _sse({'type': 'step', 'msg': 'Analizando top vendedores del mercado real...'})
            from modules.analisis_competencia import _tokenize
            words = [w for w in _tokenize(title) if len(w) > 3][:4]
            query = ' '.join(words)
            top_items   = []
            data_source = 'sin_datos'
            comp_cache_names = []

            client._ensure_token()
            top_items = ml_get_competitors(category_id, client.account.access_token, limit=8)
            top_items = [c for c in top_items if c.get('id') != item_id]
            if top_items:
                data_source = 'search_real'

            # Fallback: usar competencia_Alias.json si existe
            if not top_items:
                s = alias.replace(' ', '_').replace('/', '-')
                comp_cache = load_json(os.path.join(DATA_DIR, f'competencia_{s}.json'))
                if comp_cache:
                    for cat_data in comp_cache.get('categorias', {}).values():
                        if category_id in str(cat_data):
                            comp_cache_names = cat_data.get('competidores', [])[:8]
                            break
                    if not comp_cache_names:
                        for cat_data in list(comp_cache.get('categorias', {}).values())[:3]:
                            comp_cache_names += cat_data.get('competidores', [])[:4]
                    if comp_cache_names:
                        data_source = 'cache_catalogo'

            comp_rows = [{'id': c.get('id',''), 'title': c.get('title',''),
                          'price': c.get('price', 0), 'sold': c.get('sold_quantity', 0),
                          'free_ship': c.get('free_shipping', False),
                          'seller': c.get('seller', '—')}
                         for c in top_items]
            yield _sse({'type': 'comps_ok', 'comps': comp_rows,
                        'data_source': data_source,
                        'cache_names': comp_cache_names})

            # ── 3. Ficha de los top 5 (datos del scraping) ───────────────────
            yield _sse({'type': 'step', 'msg': f'Preparando análisis de los top {min(5,len(top_items))} vendedores...'})
            comp_details = []
            for c in top_items[:5]:
                if not c.get('id'):
                    continue
                comp_details.append({
                    'title':        c.get('title', ''),
                    'price':        c.get('price', 0),
                    'sold':         c.get('sold_quantity', 0),
                    'pics':         0,
                    'free_ship':    c.get('free_shipping', False),
                    'listing_type': 'Premium' if c.get('premium') else '',
                    'seller_level': '',
                    'seller':       c.get('seller', '—'),
                    'reviews':      c.get('reviews', 0),
                    'installments': '',
                    'attrs_named':  [],
                    'attr_ids':     set(),
                    'desc':         '',
                })

            # ── 4. Keyword intelligence ───────────────────────────────────────
            from modules.analisis_competencia import _tokenize
            from collections import Counter

            # Keywords ponderadas por ventas (cuánto vende cada palabra en el mercado)
            kw_weights = Counter()
            for cd in comp_details:
                sales_weight = max(cd['sold'], 1)
                words = [w for w in _tokenize(cd['title']) if len(w) > 2]
                for w in set(words):
                    kw_weights[w] += sales_weight

            # Fallback: extraer keywords de nombres del catálogo si no hay comp_details reales
            if not kw_weights and comp_cache_names:
                for name in comp_cache_names:
                    words = [w for w in _tokenize(name) if len(w) > 2]
                    for w in set(words):
                        kw_weights[w] += 1

            top_kws = kw_weights.most_common(30)

            # Keywords que YO tengo
            my_kw_set = set(_tokenize(title))

            # Keywords del mercado que me faltan, ordenadas por peso de ventas
            missing_kws = [(w, v) for w, v in top_kws if w not in my_kw_set]

            # Sugerencias de búsqueda (intenta varios endpoints)
            search_suggestions = []
            for sug_endpoint, sug_params in [
                ('/sites/MLA/autosuggest', {'q': ' '.join([w for w in _tokenize(title) if len(w)>3][:3]), 'limit': 8, 'lang': 'es_AR'}),
                ('/sites/MLA/autosuggest', {'q': ' '.join([w for w in _tokenize(title) if len(w)>3][:3]), 'limit': 8}),
            ]:
                try:
                    sug_r = client._get(sug_endpoint, sug_params)
                    suggestions = [s.get('q','') for s in sug_r.get('suggested_queries',[])]
                    if suggestions:
                        search_suggestions = suggestions
                        break
                except Exception:
                    pass

            # Trending en la categoría
            trending_kws = []
            try:
                tr = client._get('/sites/MLA/search_trends',
                                 {'category': category_id, 'limit': 10})
                trending_kws = [t.get('keyword','') for t in (tr if isinstance(tr,list) else [])]
            except Exception:
                pass

            # Detección de variantes en competidores
            has_variants = any(cd.get('has_variants', False) for cd in comp_details)
            # Verificar en raw top_items
            for ti in top_items[:3]:
                if ti.get('attributes'):
                    for a in ti.get('attributes', []):
                        if a.get('id') in ('COLOR', 'SIZE', 'GENDER', 'MODEL', 'FLAVOR'):
                            has_variants = True
                            break

            # ── 5. Calcular métricas de mercado ──────────────────────────────
            attr_freq = {}
            for cd in comp_details:
                for aid in cd['attr_ids']:
                    attr_freq[aid] = attr_freq.get(aid, 0) + 1
            missing_attrs_count = sum(1 for k,v in attr_freq.items() if k not in my_attrs and v >= 2)

            prices       = [cd['price'] for cd in comp_details if cd['price'] > 0]
            avg_price    = sum(prices)/len(prices) if prices else 0
            min_price    = min(prices) if prices else 0
            max_price    = max(prices) if prices else 0
            avg_pics     = sum(cd['pics'] for cd in comp_details)/len(comp_details) if comp_details else 0
            free_pct     = int(sum(1 for cd in comp_details if cd['free_ship'])/len(comp_details)*100) if comp_details else 0
            price_diff   = f"{((price/avg_price-1)*100):+.1f}%" if avg_price else "n/d"

            # ── 6. Construir bloque de competidores ───────────────────────────
            yield _sse({'type': 'step', 'msg': 'Generando análisis de mercado con Claude Sonnet...'})
            comps_block = ''
            for i, cd in enumerate(comp_details, 1):
                comps_block += (
                    f"\n{'─'*55}\n"
                    f"#{i}  {cd['title'][:70]}\n"
                    f"    Precio: ${cd['price']:,.0f}  ·  Ventas: {cd['sold']}  ·  Fotos: {cd['pics']}\n"
                    f"    Tipo pub.: {cd['listing_type'] or '?'}  ·  Envío gratis: {'SÍ' if cd['free_ship'] else 'NO'}  ·  "
                    f"Cuotas: {cd['installments'] or 'no info'}  ·  Reseñas: {cd['reviews']}\n"
                    f"    Vendedor nivel: {cd['seller_level'] or 'no info'}\n"
                    f"    Atributos: {' | '.join(cd['attrs_named'][:8]) or '(ninguno)'}\n"
                )
                if cd['desc']:
                    comps_block += f"    Descripción: {cd['desc'][:300]}…\n"
            if not comps_block:
                comps_block = '\n    (No se pudo obtener detalle de competidores)\n'

            # Resumen de niveles y tipos de publicación de la competencia
            comp_levels   = [cd['seller_level'] for cd in comp_details if cd['seller_level']]
            comp_types    = [cd['listing_type'] for cd in comp_details if cd['listing_type']]
            premium_count = sum(1 for t in comp_types if 'Premium' in t or 'gold' in t.lower())

            diferencial_block = ''
            if contexto:
                diferencial_block = f"""
╔══════════════════════════════════════════╗
  MI DIFERENCIAL (NO LO TIENEN MIS COMPETIDORES)
╚══════════════════════════════════════════╝
{contexto}
→ Este diferencial DEBE ser el eje del título y la descripción.
"""

            # Bloque de atributos requeridos faltantes
            req_block = ''
            if missing_required:
                req_block = '\n    REQUERIDOS POR ML QUE ME FALTAN: ' + \
                            ', '.join(a['name'] for a in missing_required[:8])
            if missing_recommended:
                req_block += '\n    RECOMENDADOS QUE ME FALTAN: ' + \
                             ', '.join(a['name'] for a in missing_recommended[:8])

            # Alerta de calidad de datos
            data_warning = ''
            if data_source == 'sin_datos' and not comp_cache_names:
                data_warning = """
⛔ ADVERTENCIA CRÍTICA PARA LA IA:
No hay datos de competidores disponibles (API de búsqueda restringida).
NO INVENTES competidores, precios ni ventas. El análisis de título, atributos y descripción SÍ puede hacerse con los datos de la publicación propia y el conocimiento general del mercado ML Argentina.
Aclaralo en el diagnóstico.
"""
            elif data_source == 'cache_catalogo':
                data_warning = f"""
⚠️ NOTA DE CALIDAD DE DATOS:
Los competidores son del catálogo de ML (productos registrados), no listings activos con precios/ventas reales.
Úsalos solo para análisis de keywords y estructura de títulos. NO inventes precios ni ventas.
Para precio y posición vs competencia usá tu conocimiento del mercado ML Argentina.
Nombres encontrados en el catálogo:
{chr(10).join('  - ' + n for n in comp_cache_names[:8])}
"""

            # Keyword blocks for enhanced prompt
            kw_market_block = '\n'.join(
                f"  {w:15s} → peso {v:,} {'✓ tengo' if w in my_kw_set else '✗ ME FALTA'}"
                for w, v in top_kws[:20]
            ) if top_kws else '  (sin datos de mercado — activar permiso "Ítems y búsqueda")'
            sug_block = '\n'.join(f"  · {s}" for s in search_suggestions[:6]) or '  (no disponible)'
            trending_block = ', '.join(trending_kws[:8]) or 'no disponible'

            variant_note = ''
            if has_variants:
                variant_note = '\n⚠️ VARIANTES: Los competidores tienen variantes (color/talle/modelo). ML agrega automáticamente la variante al final del título. Dejá espacio para eso al diseñar el título.'

            prompt = f"""Sos el mejor especialista en posicionamiento orgánico y conversión en MercadoLibre Argentina.
Tu misión: crear la publicación PERFECTA que el algoritmo de ML priorice por sobre todos los competidores y que convierta al máximo.
{data_warning}{diferencial_block}{variant_note}

╔══════════════════════════════════════════════════════════╗
  MI PUBLICACIÓN ACTUAL
╚══════════════════════════════════════════════════════════╝
Título actual:       {title}
Precio:              ${price:,.0f}
Tipo publicación:    {listing_label or '?'}
Categoría:           {cat_name}
Fotos:               {pics_count}
Envío gratis:        {'SÍ' if free_ship else 'NO'}
Cuotas:              {installments or 'no configuradas'}
Posición actual:     {f'#{my_position}' if my_position else 'fuera del top 50 ⚠️'}
Atributos cargados: {' | '.join(my_attrs_named[:10]) or 'NINGUNO'}
Descripción actual:
{description[:600] or '(SIN DESCRIPCIÓN)'}

╔══════════════════════════════════════════════════════════╗
  INTELIGENCIA DE KEYWORDS DEL MERCADO
╚══════════════════════════════════════════════════════════╝
Keywords rankeadas por volumen de ventas que generan (peso = ventas acumuladas):
{kw_market_block}

Lo que los compradores escriben en el buscador de ML:
{sug_block}

Tendencias en esta categoría:
{trending_block}

Atributos que ML exige/recomienda y me faltan:{req_block or ' ninguno ✓'}

╔══════════════════════════════════════════════════════════╗
  TOP {len(comp_details)} VENDEDORES LÍDERES
╚══════════════════════════════════════════════════════════╝{comps_block}
╔══════════════════════════════════════════════════════════╗
  RADIOGRAFÍA DEL MERCADO
╚══════════════════════════════════════════════════════════╝
Precio promedio:     ${avg_price:,.0f}  (rango ${min_price:,.0f}–${max_price:,.0f})
Mi precio vs mercado: {price_diff}
Fotos mercado:       {avg_pics:.0f} promedio  |  Mis fotos: {pics_count}
Envío gratis mercado: {free_pct}%  |  El mío: {'SÍ' if free_ship else 'NO'}
Publicaciones Premium en top: {premium_count}/{len(comp_details) if comp_details else '?'}
Niveles vendedores competencia: {', '.join(set(comp_levels)) if comp_levels else 'no disponible'}

╔══════════════════════════════════════════════════════════╗
  ANÁLISIS Y CREACIÓN DE PUBLICACIÓN PERFECTA
╚══════════════════════════════════════════════════════════╝

## 1. DIAGNÓSTICO — POR QUÉ NO ESTOY GANANDO
Las 3 causas principales con datos concretos del análisis.
Puntuación actual vs mercado (1-10): título / precio / fotos / atributos / descripción

## 2. KEYWORD STRATEGY
¿Cuáles son las 8 keywords más valiosas de este mercado y por qué?
¿Cuáles NO debo incluir (ruido, poca relevancia)?
¿Cómo afectan las variantes de ML al título? (si aplica)

## 3. TRES TÍTULOS OPTIMIZADOS
Generá exactamente 3 títulos. Cada uno con su estrategia:

**TÍTULO A — SEO MÁXIMO** (máx 60 caracteres, sin variante):
> [título]
Estrategia: [2 líneas explicando keywords priorizadas]

**TÍTULO B — CONVERSIÓN** (máx 60 caracteres, sin variante):
> [título]
Estrategia: [2 líneas explicando el enfoque en beneficio/diferencial]

**TÍTULO C — ALGORITMO BALANCEADO** (máx 60 caracteres, sin variante):
> [título]
Estrategia: [2 líneas explicando el balance keyword+confianza]

¿Cuál de los 3 recomendás y por qué?

## 4. DESCRIPCIÓN PERFECTA (lista para copiar y pegar en ML)
Estructura que el algoritmo de ML valora más:
- Párrafo 1 (apertura poderosa, keyword-rich, 80 palabras)
- Especificaciones técnicas (lista con bullets)
- Beneficios y diferencial (si lo hay)
- Preguntas frecuentes anticipadas (2-3)
- Cierre con CTA

Escribí la descripción completa a continuación (400-600 palabras, español rioplatense, sin markdown):

---INICIO DESCRIPCIÓN---
[descripción completa acá]
---FIN DESCRIPCIÓN---

## 5. ATRIBUTOS — LISTA COMPLETA PARA CARGAR
Para cada atributo faltante importante: qué valor cargar exactamente.

## 6. PRECIO, FOTOS Y TIPO DE PUBLICACIÓN
Recomendaciones concretas con números.

## 7. PLAN DE ACCIÓN (8 pasos ordenados por impacto)
🔴 1. [acción + impacto esperado]
🔴 2.
🔴 3.
🟡 4.
🟡 5.
🟡 6.
🟢 7.
🟢 8."""

            ai_client = anthropic.Anthropic()
            full_text = ''
            with ai_client.messages.stream(
                model='claude-sonnet-4-6',
                max_tokens=8000,
                messages=[{'role': 'user', 'content': prompt}],
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    yield _sse({'type': 'token', 'text': text})
                _mc_fm = stream.get_final_message()
                _log_token_usage('Multicuenta — Análisis cruzado', 'claude-sonnet-4-6', _mc_fm.usage.input_tokens, _mc_fm.usage.output_tokens)

            # ── 6. Guardar ────────────────────────────────────────────────────
            out_path = os.path.join(DATA_DIR, f'analisis_pub_{item_id}.json')
            save_json(out_path, {
                    'item_id': item_id, 'titulo': title, 'alias': alias,
                    'fecha': datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'market': {'avg_price': avg_price, 'min_price': min_price,
                               'max_price': max_price, 'avg_pics': avg_pics,
                               'free_ship_pct': free_pct},
                    'competidores': comp_rows,
                    'analisis': full_text,
                })

            yield _sse({'type': 'done', 'item_id': item_id})

        except Exception as e:
            yield _sse({'type': 'error', 'msg': str(e)})

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control']     = 'no-cache'
    return resp


def _sse(data: dict) -> str:
    return f'data: {json.dumps(data, ensure_ascii=False)}\n\n'


# ── OAuth re-autorización ─────────────────────────────────────────────────────

ML_REDIRECT_URI = os.environ.get('ML_REDIRECT_URI', 'http://localhost:8080/oauth/exchange')


@app.route('/api/cuenta-nueva', methods=['POST'])
def api_cuenta_nueva():
    """Crea una nueva cuenta en accounts.json con las credenciales ingresadas."""
    body         = request.get_json(force=True)
    alias        = body.get('alias', '').strip()
    client_id    = body.get('client_id', '').strip()
    client_secret= body.get('client_secret', '').strip()

    if not alias or not client_id or not client_secret:
        return jsonify(ok=False, error='Alias, Client ID y Client Secret son requeridos.')

    accounts_path = os.path.join(CONFIG_DIR, 'accounts.json')
    data = load_json(accounts_path) or {'accounts': []}
    if not isinstance(data, dict):
        data = {'accounts': data}

    # Verificar que el alias no exista ya
    if any(a['alias'] == alias for a in data['accounts']):
        return jsonify(ok=False, error=f'Ya existe una cuenta con el alias "{alias}".')

    data['accounts'].append({
        'alias':          alias,
        'client_id':      client_id,
        'client_secret':  client_secret,
        'access_token':   '',
        'refresh_token':  '',
        'token_expires_at': '',
        'user_id':        '',
        'nickname':       '',
        'active':         True,
    })
    save_json(accounts_path, data)

    return jsonify(ok=True, alias=alias)

def _pkce_pair():
    """Genera code_verifier y code_challenge para PKCE."""
    import hashlib, secrets
    from flask import session
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return verifier, challenge


@app.route('/oauth/connect/<alias>')
def oauth_connect(alias):
    """Redirige directo a ML para autorizar. Guarda el verifier en sesión."""
    import urllib.parse
    accounts_path = os.path.join(CONFIG_DIR, 'accounts.json')
    data = load_json(accounts_path) or {'accounts': []}
    if not isinstance(data, dict):
        data = {'accounts': data}
    acc = next((a for a in data['accounts'] if a['alias'] == alias), None)
    if not acc:
        return 'Cuenta no encontrada', 404

    verifier, challenge = _pkce_pair()
    from flask import session
    session[f'pkce_verifier_{alias}'] = verifier

    params = {
        'response_type':         'code',
        'client_id':             acc['client_id'],
        'redirect_uri':          ML_REDIRECT_URI,
        'state':                 alias,
        'code_challenge':        challenge,
        'code_challenge_method': 'S256',
        'scope':                 'offline_access read write',
    }
    auth_url = 'https://auth.mercadolibre.com.ar/authorization?' + urllib.parse.urlencode(params)
    return redirect(auth_url)


@app.route('/oauth/exchange', methods=['GET', 'POST'])
def oauth_exchange():
    """Callback OAuth — acepta GET (redirect de ML) y POST (form manual)."""
    from flask import session

    if request.method == 'GET':
        code  = request.args.get('code',  '').strip()
        alias = request.args.get('state', '').strip()
        verifier = session.pop(f'pkce_verifier_{alias}', '')
    else:
        code     = request.form.get('code',     '').strip()
        alias    = request.form.get('alias',    '').strip()
        verifier = request.form.get('verifier', '').strip()

    if not code:
        return '<h2>Falta el código</h2><a href="/">Volver</a>'
    if not verifier:
        return (f'<h2>Sesión inválida — volvé a iniciar el proceso</h2>'
                f'<a href="/oauth/connect/{alias}">Reintentar</a>')

    accounts_path = os.path.join(CONFIG_DIR, 'accounts.json')
    data = load_json(accounts_path) or {'accounts': []}
    if not isinstance(data, dict):
        data = {'accounts': data}
    acc  = next((a for a in data['accounts'] if a['alias'] == alias), None)
    if not acc:
        return '<h2>Cuenta no encontrada</h2><a href="/">Volver</a>'

    try:
        r = req_lib.post('https://api.mercadolibre.com/oauth/token',
                         data={
                             'grant_type':    'authorization_code',
                             'client_id':     acc['client_id'],
                             'client_secret': acc['client_secret'],
                             'code':          code,
                             'redirect_uri':  ML_REDIRECT_URI,
                             'code_verifier': verifier,
                         }, timeout=10)
        tok = r.json()
        if 'access_token' not in tok:
            return (f'<h2>❌ Error: {tok.get("message","?")}</h2>'
                    f'<p><a href="/oauth/connect/{alias}">Intentá de nuevo</a> '
                    f'(el código dura solo 10 minutos)</p>')
        expires_at = (datetime.now() + timedelta(seconds=tok.get('expires_in', 21600))).isoformat()
        acc['access_token']     = tok['access_token']
        acc['refresh_token']    = tok['refresh_token']
        acc['token_expires_at'] = expires_at
        save_json(accounts_path, data)
        return render_template('oauth_success.html', alias=alias, scope=tok.get('scope',''))
    except Exception as e:
        return f'<h2>Error: {e}</h2><a href="/">Volver</a>'


# ── Notificaciones por email ──────────────────────────────────────────────────

_NOTIF_CONFIG_PATH = os.path.join(CONFIG_DIR, 'notificaciones.json')


def _load_notif_config():
    return load_json(_NOTIF_CONFIG_PATH) or {}


def _save_notif_config(cfg):
    save_json(_NOTIF_CONFIG_PATH, cfg)


def _send_email_alert(subject: str, body_html: str, cfg: dict):
    """Envía un email de alerta usando SMTP configurado."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_host  = cfg.get('smtp_host', 'smtp.gmail.com')
    smtp_port  = int(cfg.get('smtp_port', 587))
    smtp_user  = cfg.get('smtp_user', '')
    smtp_pass  = cfg.get('smtp_pass', '')
    email_to   = cfg.get('email_to', '')

    if not smtp_user or not smtp_pass or not email_to:
        return False, 'Configuración incompleta'

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'Sistema ML <{smtp_user}>'
        msg['To']      = email_to
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, [email_to], msg.as_string())
        return True, 'OK'
    except Exception as e:
        return False, str(e)


def _build_alert_email(alertas: list) -> str:
    """Genera el HTML del email de alertas diarias."""
    urgentes   = [a for a in alertas if a['nivel'] == 'urgente']
    importantes = [a for a in alertas if a['nivel'] == 'importante']

    def _row_color(nivel):
        return '#fee2e2' if nivel == 'urgente' else '#fef3c7'

    rows = ''
    for a in urgentes + importantes:
        rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
          <td style="padding:10px 14px;background:{_row_color(a['nivel'])};width:80px;text-align:center;font-size:.75rem;font-weight:700;color:{'#dc2626' if a['nivel'] == 'urgente' else '#d97706'}">
            {'🔴 URGENTE' if a['nivel'] == 'urgente' else '🟡 IMPORTANTE'}
          </td>
          <td style="padding:10px 14px">
            <div style="font-weight:600;font-size:.85rem;color:#1e293b">{a['titulo']}</div>
            <div style="font-size:.78rem;color:#64748b;margin-top:2px">{a['detalle']}</div>
          </td>
          <td style="padding:10px 14px;white-space:nowrap">
            <a href="http://localhost:8080{a['link']}" style="font-size:.75rem;color:#2563eb">Ver →</a>
          </td>
        </tr>"""

    fecha = datetime.now().strftime('%A %d de %B, %H:%M')
    return f"""
    <html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f7;margin:0;padding:20px">
      <div style="max-width:620px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">
        <div style="background:linear-gradient(135deg,#1e3a8a,#1d4ed8);padding:20px 24px">
          <div style="color:#fff;font-size:1.1rem;font-weight:800">Sistema ML — Resumen diario</div>
          <div style="color:#93c5fd;font-size:.78rem;margin-top:4px">{fecha} | {len(urgentes)} urgentes · {len(importantes)} importantes</div>
        </div>
        <table style="width:100%;border-collapse:collapse">
          {rows}
        </table>
        <div style="padding:16px 24px;background:#f8fafc;border-top:1px solid #e2e8f0;text-align:center">
          <a href="http://localhost:8080" style="background:#2563eb;color:#fff;padding:8px 20px;border-radius:8px;text-decoration:none;font-size:.8rem;font-weight:700">
            Abrir Sistema ML →
          </a>
          <div style="font-size:.68rem;color:#94a3b8;margin-top:10px">Sistema ML · Actualización automática diaria</div>
        </div>
      </div>
    </body></html>"""


@app.route('/api/token-costs')
def api_token_costs():
    """Devuelve el historial de uso de tokens Claude con costos."""
    log   = load_json(TOKEN_LOG_PATH) or {'entries': []}
    entries = log.get('entries', [])

    # Acumulados por función
    by_func = {}
    for e in entries:
        fn = e.get('funcion', '?')
        if fn not in by_func:
            by_func[fn] = {'funcion': fn, 'modelo': e.get('modelo',''), 'llamadas': 0,
                           'in': 0, 'out': 0, 'usd': 0.0}
        by_func[fn]['llamadas'] += 1
        by_func[fn]['in']       += e.get('in', 0)
        by_func[fn]['out']      += e.get('out', 0)
        by_func[fn]['usd']      += e.get('usd', 0.0)

    # Totales del mes actual
    mes_actual = datetime.now().strftime('%Y-%m')
    entries_mes = [e for e in entries if e.get('ts', '').startswith(mes_actual)]
    total_mes   = sum(e.get('usd', 0) for e in entries_mes)
    total_all   = sum(e.get('usd', 0) for e in entries)

    # Últimas 50 entradas para la tabla detallada (más recientes primero)
    recientes = list(reversed(entries[-50:]))

    return jsonify({
        'ok':         True,
        'total_mes':  round(total_mes, 4),
        'total_all':  round(total_all, 4),
        'por_funcion': sorted(by_func.values(), key=lambda x: x['usd'], reverse=True),
        'recientes':  recientes,
    })


@app.route('/api/token-costs-clear', methods=['POST'])
def api_token_costs_clear():
    try:
        save_json(TOKEN_LOG_PATH, {'entries': []})
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/settings')
def settings():
    return render_template('settings.html', accounts=get_accounts())


@app.route('/api/notificaciones-config', methods=['GET', 'POST'])
def api_notificaciones_config():
    """Obtiene o guarda la configuración de notificaciones."""
    if request.method == 'GET':
        cfg = _load_notif_config()
        # No exponer la contraseña
        safe_cfg = {k: v for k, v in cfg.items() if k != 'smtp_pass'}
        safe_cfg['tiene_pass'] = bool(cfg.get('smtp_pass'))
        return jsonify({'ok': True, 'config': safe_cfg})

    body = request.get_json(force=True)
    cfg  = _load_notif_config()
    for key in ('smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass', 'email_to', 'activo'):
        if key in body:
            cfg[key] = body[key]
    _save_notif_config(cfg)
    return jsonify({'ok': True})


@app.route('/api/notificaciones-test', methods=['POST'])
def api_notificaciones_test():
    """Envía un email de prueba."""
    cfg = _load_notif_config()
    ok, msg = _send_email_alert(
        subject='✅ Sistema ML — Email de prueba',
        body_html='<html><body style="font-family:Arial,sans-serif;padding:30px"><h2 style="color:#2563eb">¡Funciona!</h2><p>Tu configuración de email está correcta. Vas a recibir alertas diarias de tu cuenta de MercadoLibre.</p></body></html>',
        cfg=cfg,
    )
    return jsonify({'ok': ok, 'mensaje': msg if not ok else 'Email de prueba enviado correctamente.'})


# ── Ads summary para el scheduler ────────────────────────────────────────────

def _collect_ads_summary(alias: str, token: str):
    """Recopila métricas de Meli Ads (últimos 7d + presupuesto de hoy) y las cachea."""
    try:
        from modules.meli_ads_engine import (
            _discover_campaign_ids,
            _ads_get,
            _get_campaign_detail,
            _float as _af,
            _int  as _ai,
        )
    except ImportError:
        return

    r_me = req_lib.get('https://api.mercadolibre.com/users/me',
        headers={'Authorization': f'Bearer {token}'}, timeout=8)
    if not r_me.ok:
        return
    uid = r_me.json().get('id')
    if not uid:
        return

    now_art  = datetime.utcnow() - timedelta(hours=3)
    d7_to    = now_art.strftime('%Y-%m-%d')
    d7_from  = (now_art - timedelta(days=7)).strftime('%Y-%m-%d')

    camp_map = _discover_campaign_ids(token, uid)
    if not camp_map:
        db_save(os.path.join(DATA_DIR, f'ads_summary_{safe(alias)}.json'), {
            'alias': alias, 'fecha': now_art.strftime('%d/%m/%Y %H:%M'),
            'sin_campanas': True,
        })
        return

    total_spend = total_rev = total_conv = total_impr = total_clicks = 0.0
    budget_warnings = []

    for cid in camp_map:
        _r7 = _ads_get(f'/advertising/product_ads/campaigns/{cid}/metrics', token,
                       params={'date_from': d7_from, 'date_to': d7_to})
        if _r7['ok'] and _r7['data']:
            _m = _r7['data']
            total_spend  += _af(_m.get('cost', 0))
            total_rev    += _af(_m.get('amount_total', 0))
            total_conv   += _ai(_m.get('sold_quantity_total', 0))
            total_impr   += _ai(_m.get('impressions', 0))
            total_clicks += _ai(_m.get('clicks', 0))

        det    = _get_campaign_detail(token, cid)
        budget = _af(det.get('budget', 0))
        if budget > 0:
            _rt = _ads_get(f'/advertising/product_ads/campaigns/{cid}/metrics', token,
                           params={'date_from': d7_to, 'date_to': d7_to})
            today_sp  = _af((_rt.get('data') or {}).get('cost', 0)) if _rt['ok'] else 0.0
            today_pct = round(today_sp / budget * 100, 1)
            if today_pct >= 75:
                budget_warnings.append({
                    'nombre':      det.get('name', f'Campaña {cid}'),
                    'pct':         today_pct,
                    'presupuesto': round(budget),
                    'gastado_hoy': round(today_sp),
                })

    db_save(os.path.join(DATA_DIR, f'ads_summary_{safe(alias)}.json'), {
        'alias':       alias,
        'fecha':       now_art.strftime('%d/%m/%Y %H:%M'),
        'periodo':     f'{d7_from} → {d7_to}',
        'campanas':    len(camp_map),
        'spend_7d':    round(total_spend),
        'revenue_7d':  round(total_rev),
        'conversiones': int(total_conv),
        'roas':        round(total_rev / total_spend, 2) if total_spend > 0 else None,
        'acos':        round(total_spend / total_rev, 4) if total_rev > 0 else None,
        'ctr':         round(total_clicks / total_impr * 100, 2) if total_impr > 0 else None,
        'budget_warnings': budget_warnings,
        'sin_campanas': False,
    })


# ── Scheduler automático ──────────────────────────────────────────────────────

def _scheduler_run_all():
    """Actualiza datos de todas las cuentas una vez por día: reputación, stock y posiciones."""
    global _scheduler_running
    if _scheduler_running:
        print('[scheduler] Ya hay una actualización en curso, saltando.')
        return
    _scheduler_running = True
    try:
        _scheduler_run_all_inner()
    finally:
        _scheduler_running = False


def _scheduler_run_all_inner():
    from core.account_manager import AccountManager
    from modules.preguntas_reputacion import run_reputacion
    from modules.stock_rentabilidad import run as run_stock
    from modules.monitor_posicionamiento import run as run_pos

    mgr = AccountManager()
    accounts = mgr.list_accounts()
    if not accounts:
        print('[scheduler] Sin cuentas activas, nada que actualizar.')
        return

    print(f'\n[scheduler] Iniciando actualización diaria — {datetime.now().strftime("%Y-%m-%d %H:%M")}')

    for acc in accounts:
        alias = acc.alias
        print(f'[scheduler] Actualizando {alias}...')
        try:
            client = mgr.get_client(alias)
        except Exception as e:
            app.logger.error('[scheduler] %s — no se pudo obtener cliente: %s', alias, e)
            print(f'[scheduler] {alias} — no se pudo obtener cliente: {e}')
            continue

        try:
            run_reputacion(client, alias)
            print(f'[scheduler] {alias} — reputación OK')
        except Exception as e:
            app.logger.error('[scheduler] %s — reputación ERROR: %s', alias, e)
            print(f'[scheduler] {alias} — reputación ERROR: {e}')

        try:
            run_stock(client, alias)
            print(f'[scheduler] {alias} — stock OK')
        except Exception as e:
            app.logger.error('[scheduler] %s — stock ERROR: %s', alias, e)
            print(f'[scheduler] {alias} — stock ERROR: {e}')

        try:
            run_pos(client, alias)
            print(f'[scheduler] {alias} — posiciones OK')
        except Exception as e:
            app.logger.error('[scheduler] %s — posiciones ERROR: %s', alias, e)
            print(f'[scheduler] {alias} — posiciones ERROR: {e}')

        try:
            _sync_full_inventory(alias)
            print(f'[scheduler] {alias} — inventario Full OK')
        except Exception as e:
            app.logger.error('[scheduler] %s — inventario Full ERROR: %s', alias, e)
            print(f'[scheduler] {alias} — inventario Full ERROR: {e}')

        try:
            client._ensure_token()
            _collect_ads_summary(alias, client.account.access_token)
            print(f'[scheduler] {alias} — Meli Ads OK')
        except Exception as e:
            app.logger.error('[scheduler] %s — Meli Ads ERROR: %s', alias, e)
            print(f'[scheduler] {alias} — Meli Ads ERROR: {e}')

    # ── Monitor de Evolución: evaluar alertas post-optimización ───────────────
    try:
        _scheduler_check_monitor()
        print('[scheduler] Monitor de evolución — OK')
    except Exception as e:
        app.logger.error('[scheduler] Monitor de evolución ERROR: %s', e)
        print(f'[scheduler] Monitor de evolución ERROR: {e}')

    global _scheduler_last_run
    _scheduler_last_run = datetime.now()
    # Persistir en disco para sobrevivir reinicios del servidor
    try:
        from core.db_storage import db_save as _dbs
        _dbs(os.path.join(DATA_DIR, '_scheduler_last_run.json'),
             {'last_run': _scheduler_last_run.strftime('%Y-%m-%d %H:%M')})
    except Exception:
        pass
    print(f'[scheduler] Actualización diaria completada — {_scheduler_last_run.strftime("%Y-%m-%d %H:%M")}')

    # ── Chequear preguntas antiguas sin responder ─────────────────────────────
    try:
        from core.account_manager import AccountManager as _AM_q
        from datetime import timezone as _tz
        _mgr_q   = _AM_q()
        _pregs_viejas = []
        for _acc_q in _mgr_q.list_accounts():
            try:
                _cl_q = _mgr_q.get_client(_acc_q.alias)
                _cl_q._ensure_token()
                _h_q = {'Authorization': f'Bearer {_cl_q.account.access_token}'}
                _qr = req_lib.get(
                    'https://api.mercadolibre.com/questions/search',
                    headers=_h_q,
                    params={'seller_id': _cl_q.account.user_id, 'status': 'UNANSWERED', 'limit': 50},
                    timeout=8)
                if _qr.ok:
                    for _q in _qr.json().get('questions', []):
                        _dc = _q.get('date_created', '')
                        if _dc:
                            _dt_q = datetime.fromisoformat(_dc.replace('Z', '+00:00'))
                            _horas = (datetime.now(_tz.utc) - _dt_q).total_seconds() / 3600
                            if _horas >= 4:
                                _pregs_viejas.append({
                                    'alias':  _acc_q.alias,
                                    'horas':  round(_horas, 1),
                                    'texto':  _q.get('text', '')[:80],
                                    'item_id': _q.get('item_id', ''),
                                })
            except Exception:
                pass
        if _pregs_viejas:
            print(f'[scheduler] {len(_pregs_viejas)} pregunta(s) sin responder hace 4+ horas')
    except Exception as e:
        print(f'[scheduler] Chequeo preguntas antiguas ERROR: {e}')
        _pregs_viejas = []

    # ── Enviar resumen por email si está configurado ────────────────────────
    notif_cfg = _load_notif_config()
    if notif_cfg.get('activo') and notif_cfg.get('email_to'):
        try:
            # Obtener alertas actuales
            with app.test_request_context():
                alertas_resp = api_alertas()
                alertas_data = alertas_resp.get_json()
            all_alerts = alertas_data.get('alerts', [])
            # Agregar alertas de preguntas antiguas
            for _pv in (_pregs_viejas or []):
                all_alerts.append({
                    'nivel':   'urgente' if _pv['horas'] >= 8 else 'importante',
                    'cuenta':  _pv['alias'],
                    'mensaje': f"Pregunta sin responder hace {_pv['horas']:.0f}h: \"{_pv['texto']}\" (item {_pv['item_id']})",
                })
            urgentes   = sum(1 for a in all_alerts if a.get('nivel') == 'urgente')
            importantes = sum(1 for a in all_alerts if a.get('nivel') == 'importante')

            if urgentes > 0 or importantes > 0:
                subject = f'⚠️ Sistema ML — {urgentes} urgente{"s" if urgentes!=1 else ""} · {importantes} importante{"s" if importantes!=1 else ""}'
                html = _build_alert_email(all_alerts)
                ok, msg = _send_email_alert(subject, html, notif_cfg)
                print(f'[scheduler] Email enviado: {"OK" if ok else f"ERROR — {msg}"}')
            else:
                # Sin alertas — enviar resumen positivo (solo si está configurado para ello)
                if notif_cfg.get('siempre_enviar'):
                    subject = '✅ Sistema ML — Todo en orden'
                    html = _build_alert_email([])
                    _send_email_alert(subject, html, notif_cfg)
                print('[scheduler] Sin alertas críticas — no se envió email')
        except Exception as e:
            print(f'[scheduler] Error al enviar email: {e}')


def _scheduler_check_monitor():
    """
    Toma snapshot diario de TODOS los ítems monitoreados y genera alertas en hitos.
    - Snapshots: todos los días, para todos los ítems (independientemente de días elapsed)
    - Alertas: solo en hitos (7d, 30d) si no fueron generadas ya
    """
    from core.account_manager import AccountManager as _AM
    _mon_path = os.path.join(DATA_DIR, 'monitor_evolucion.json')
    _mon      = load_json(_mon_path) or {'items': []}
    if not _mon.get('items'):
        return

    _now     = datetime.now()
    _today   = _now.strftime('%Y-%m-%d')
    changed  = False
    HITOS    = [7, 30, 60, 90]

    for it in _mon['items']:
        alias   = it.get('alias', '')
        item_id = it.get('item_id', '')
        if not alias or not item_id:
            continue

        try:
            _fd  = it.get('fecha_opt', '')[:10]
            dias = (_now - datetime.strptime(_fd, '%Y-%m-%d')).days
        except Exception:
            continue

        # ── Snapshot diario: siempre, para todos los ítems ───────────────
        # Evitar duplicado si ya hay un snapshot de hoy
        snaps_hoy = [s for s in (it.get('snapshots') or []) if (s.get('fecha') or '')[:10] == _today]
        if snaps_hoy:
            continue  # ya se tomó snapshot hoy

        snap = {'fecha': _now.strftime('%Y-%m-%d %H:%M')}
        try:
            mgr    = _AM()
            client = mgr.get_client(alias)
            client._ensure_token()
            _h = {'Authorization': f'Bearer {client.account.access_token}'}
            # Visitas 7d
            _vr = req_lib.get(
                f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
                headers=_h, params={'last': 7, 'unit': 'day'}, timeout=6)
            if _vr.ok:
                snap['visitas_7d'] = _vr.json().get('total_visits', 0)
            # Visitas ayer (fecha exacta, más confiable que last=2)
            from datetime import datetime as _dtn2, timedelta as _tdd2
            _ayer_s = (_dtn2.now() - _tdd2(days=1)).strftime('%Y-%m-%d')
            _vd = req_lib.get(
                f'https://api.mercadolibre.com/items/{item_id}/visits/time_window',
                headers=_h,
                params={'date_from': f'{_ayer_s}T00:00:00.000-03:00',
                        'date_to':   f'{_ayer_s}T23:59:59.000-03:00'},
                timeout=6)
            if _vd.ok:
                snap['visitas_ayer'] = _vd.json().get('total_visits', 0)
                snap['visitas_hoy']  = snap.get('visitas_hoy', 0)  # placeholder
            # Ventas acumuladas
            _ir = req_lib.get(f'https://api.mercadolibre.com/items/{item_id}', headers=_h, timeout=6)
            if _ir.ok:
                snap['ventas_total'] = _ir.json().get('sold_quantity', 0)
        except Exception:
            pass

        # Posición y conversión desde datos locales (ya actualizados por el scheduler)
        _pos_data   = load_json(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
        _stock_data = load_json(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
        if item_id in _pos_data:
            _ph = _pos_data[item_id].get('history', {})
            if _ph:
                _pv = _ph[max(_ph.keys())]
                if _pv != 999:
                    snap['posicion'] = _pv
        for _si in (_stock_data.get('items') or []):
            if _si.get('id') == item_id:
                snap['ventas_30d'] = _si.get('ventas_30d') or 0
                snap['conv_pct']   = _si.get('conversion_pct') or 0.0
                break

        it.setdefault('snapshots', []).append(snap)
        it['snapshots']       = it['snapshots'][-60:]   # guardar hasta 60 días
        it['ultimo_snapshot'] = snap
        changed = True
        print(f'[monitor] {alias}/{item_id} — snapshot día {dias}')

        # ── Alertas en hitos (solo si no generadas ya para ese hito) ────
        alertas_existentes = it.get('alertas', [])
        hitos_ya = {a.get('hito') for a in alertas_existentes}
        baseline = it.get('baseline') or {}

        for hito in HITOS:
            hito_key = f'{hito}d'
            if dias < hito or hito_key in hitos_ya:
                continue

            nuevas = []
            b_vis = baseline.get('visitas_7d') or 0
            b_pos = baseline.get('posicion')

            d_pos = None
            if b_pos is not None and snap.get('posicion') is not None:
                d_pos = round(snap['posicion'] - b_pos, 1)

            d_vis = None
            if b_vis and snap.get('visitas_7d') is not None:
                d_vis = round(snap['visitas_7d'] - b_vis, 1)

            if d_pos is not None:
                if d_pos <= -3:
                    nuevas.append({'hito': hito_key, 'nivel': 'bueno',   'tipo': 'posicion_sube',
                        'mensaje': f'Subiste {abs(int(d_pos))} posiciones ({b_pos}° → {snap["posicion"]}°) en {dias} días.',
                        'fecha': _today, 'leida': False})
                elif d_pos >= 3:
                    nuevas.append({'hito': hito_key, 'nivel': 'warning', 'tipo': 'posicion_baja',
                        'mensaje': f'⚠ Posición bajó {int(d_pos)} lugares ({b_pos}° → {snap["posicion"]}°) en {dias} días.',
                        'fecha': _today, 'leida': False})

            if d_vis is not None and b_vis >= 5:
                pct = round(d_vis / b_vis * 100, 1)
                if pct >= 20:
                    nuevas.append({'hito': hito_key, 'nivel': 'bueno',   'tipo': 'visitas_suben',
                        'mensaje': f'Visitas 7d subieron {pct}% ({int(b_vis)} → {int(snap["visitas_7d"])}) en {dias} días.',
                        'fecha': _today, 'leida': False})
                elif pct <= -20:
                    nuevas.append({'hito': hito_key, 'nivel': 'warning', 'tipo': 'visitas_bajan',
                        'mensaje': f'⚠ Visitas 7d bajaron {abs(pct)}% ({int(b_vis)} → {int(snap["visitas_7d"])}) en {dias} días.',
                        'fecha': _today, 'leida': False})

            if nuevas:
                it['alertas'] = alertas_existentes + nuevas
                print(f'[monitor] {alias}/{item_id} — {len(nuevas)} alerta(s) hito {hito_key}')

    if not changed:
        return

    # Re-cargar datos frescos del DB antes de guardar para no pisar ítems
    # agregados concurrentemente (race condition con api_monitor_iniciar).
    # Solo transferimos snapshots/alertas; el resto del item queda intacto.
    _mon_fresh = load_json(_mon_path) or {'items': []}
    _processed = {(x.get('item_id'), x.get('alias')): x for x in _mon['items']}
    for _fi in _mon_fresh.get('items', []):
        _fkey = (_fi.get('item_id'), _fi.get('alias'))
        if _fkey not in _processed:
            continue
        _proc = _processed[_fkey]
        _fi['snapshots']       = _proc.get('snapshots', _fi.get('snapshots', []))
        _fi['ultimo_snapshot'] = _proc.get('ultimo_snapshot', _fi.get('ultimo_snapshot'))
        if 'alertas' in _proc:
            _fi['alertas'] = _proc['alertas']

    # Purgar ítems con optimización de más de 90 días
    _cutoff = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    _mon_fresh['items'] = [it for it in _mon_fresh.get('items', []) if (it.get('fecha_opt') or '')[:10] >= _cutoff]
    save_json(_mon_path, _mon_fresh)


def _job_repricing_hourly():
    """Job 2 — Buy Box check + repricing en horario comercial.

    Skip silencioso si config/repricing.json está vacío (sin reglas activas).
    Solo procesa cuentas con active=True.
    """
    repricing_cfg = load_json(os.path.join(CONFIG_DIR, 'repricing.json')) or {}
    if not repricing_cfg.get('items'):
        print('[job_repricing] Sin reglas de repricing activas — skip silencioso.')
        return

    from core.account_manager import AccountManager
    from modules.repricing import run as run_repricing
    mgr = AccountManager()
    accounts = [a for a in mgr.list_accounts() if a.active]
    if not accounts:
        print('[job_repricing] Sin cuentas activas.')
        return

    for acc in accounts:
        try:
            client = mgr.get_client(acc.alias)
            run_repricing(client, acc.alias, dry_run=False)
            print(f'[job_repricing] {acc.alias} OK')
        except Exception as e:
            app.logger.error('[job_repricing] %s — error: %s', acc.alias, e)
            raise   # propagar para que el retry del JobManager lo capture


def _job_questions_15min():
    """Job 3 — Refrescar preguntas pendientes en horario comercial.

    Solo procesa cuentas con active=True. Idempotente.
    """
    from core.account_manager import AccountManager
    mgr = AccountManager()
    accounts = [a for a in mgr.list_accounts() if a.active]
    if not accounts:
        return

    for acc in accounts:
        try:
            client = mgr.get_client(acc.alias)
            client._ensure_token()
            heads = {'Authorization': f'Bearer {client.account.access_token}'}
            r = req_lib.get(
                'https://api.mercadolibre.com/questions/search',
                headers=heads,
                params={'seller_id': client.account.user_id,
                        'status': 'UNANSWERED', 'limit': 50},
                timeout=8,
            )
            if r.ok:
                qs = r.json().get('questions', [])
                # Persistir count para que la UI lo muestre — NO responder automáticamente
                preg_path = os.path.join(DATA_DIR, f'preguntas_pendientes_{safe(acc.alias)}.json')
                save_json(preg_path, {
                    'fecha':         datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'count':         len(qs),
                    'preguntas':     qs[:50],
                })
        except Exception as e:
            app.logger.warning('[job_questions] %s — error: %s', acc.alias, e)
            raise


def _job_buybox_check():
    """Job 5 — Verificar Buy Box en publicaciones de catálogo cada 6h.

    Compara contra el último snapshot guardado. Si Buy Box se perdió,
    persiste alerta para que aparezca en /alertas en la próxima carga.
    Solo procesa cuentas con active=True.
    """
    from core.account_manager import AccountManager
    mgr = AccountManager()
    accounts = [a for a in mgr.list_accounts() if a.active]
    if not accounts:
        return

    snap_path = os.path.join(DATA_DIR, 'buybox_snapshots.json')
    snapshots = load_json(snap_path) or {}
    nuevas_perdidas: list = []

    for acc in accounts:
        try:
            client = mgr.get_client(acc.alias)
            client._ensure_token()
            token = client.account.access_token
            heads = {'Authorization': f'Bearer {token}'}

            # Cargar lista de publicaciones de catálogo (ya está en stock JSON)
            stock = load_json(os.path.join(DATA_DIR, f'stock_{safe(acc.alias)}.json')) or {}
            for it in stock.get('items', [])[:50]:   # cap a 50 por cuenta para no saturar
                item_id = it.get('id', '')
                if not item_id:
                    continue
                # Verificar status del catálogo via API
                try:
                    r_item = req_lib.get(
                        f'https://api.mercadolibre.com/items/{item_id}',
                        headers=heads,
                        params={'attributes': 'id,catalog_product_id,catalog_listing'},
                        timeout=6,
                    )
                    if not r_item.ok:
                        continue
                    body = r_item.json()
                    cpid = body.get('catalog_product_id')
                    if not cpid:
                        continue
                    # Buy box winner
                    rp = req_lib.get(
                        f'https://api.mercadolibre.com/products/{cpid}/items',
                        headers=heads, params={'limit': 5}, timeout=6,
                    )
                    if not rp.ok:
                        continue
                    sellers = rp.json().get('results', [])
                    we_win = bool(sellers) and (sellers[0].get('id') == item_id or
                                                 sellers[0].get('item_id') == item_id)

                    prev = snapshots.get(f'{acc.alias}::{item_id}', {})
                    if prev.get('we_win') and not we_win:
                        nuevas_perdidas.append({
                            'alias':   acc.alias,
                            'item_id': item_id,
                            'titulo':  it.get('titulo', '')[:80],
                            'fecha':   datetime.now().strftime('%Y-%m-%d %H:%M'),
                        })
                    snapshots[f'{acc.alias}::{item_id}'] = {
                        'we_win': we_win,
                        'fecha':  datetime.now().strftime('%Y-%m-%d %H:%M'),
                    }
                except Exception:
                    continue
                _time_module.sleep(0.1)
        except Exception as e:
            app.logger.warning('[job_buybox] %s — error: %s', acc.alias, e)
            raise

    save_json(snap_path, snapshots)
    if nuevas_perdidas:
        # Persistir lista de Buy Box perdidos para que /alertas las muestre
        save_json(os.path.join(DATA_DIR, 'buybox_perdidos_recientes.json'), {
            'fecha':    datetime.now().strftime('%Y-%m-%d %H:%M'),
            'items':    nuevas_perdidas,
        })


def _job_weekly_reopt():
    """Job 4 — RESERVADO. Re-optimización IA de publicaciones que perdieron
    posiciones esta semana.

    NO se ejecuta por cron (slot reservado). Disparable manualmente desde
    /settings con el botón "Re-optimizar publicaciones que perdieron posiciones".

    Filtro inteligente: solo publicaciones con caída >5 posiciones esta semana
    (no Top X ciego — re-optimizar lo que no necesita es destructivo).
    """
    from core.account_manager import AccountManager
    from modules.seo_optimizer import run_full_optimization

    mgr = AccountManager()
    accounts = [a for a in mgr.list_accounts() if a.active]
    procesadas = 0

    for acc in accounts:
        pos_path = os.path.join(DATA_DIR, f'posiciones_{safe(acc.alias)}.json')
        pos_data = load_json(pos_path) or {}

        # Identificar items con caída >5 posiciones en últimos 7 días
        candidatos: list[str] = []
        for item_id, item_data in pos_data.items():
            hist = item_data.get('history', {})
            if not hist:
                continue
            fechas = sorted(hist.keys())[-8:]
            valid = [(f, hist[f]) for f in fechas if hist.get(f) and hist[f] != 999]
            if len(valid) < 2:
                continue
            primera_pos = valid[0][1]
            ultima_pos  = valid[-1][1]
            caida = ultima_pos - primera_pos   # positivo = empeoró
            if caida > 5:
                candidatos.append(item_id)

        if not candidatos:
            print(f'[job_weekly_reopt] {acc.alias}: sin candidatos (ningún item perdió >5 posiciones).')
            continue

        client = mgr.get_client(acc.alias)
        for item_id in candidatos[:5]:   # cap a 5 por cuenta para limitar costo Claude
            try:
                run_full_optimization(item_id, client)
                procesadas += 1
            except Exception as e:
                app.logger.warning('[job_weekly_reopt] %s/%s — error: %s', acc.alias, item_id, e)

    print(f'[job_weekly_reopt] Re-optimización manual completada — {procesadas} publicaciones procesadas.')


def _start_scheduler():
    """Inicia APScheduler con los 5 jobs del Sprint 2 vía JobManager."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from core.scheduler_manager import JobManager

        scheduler = BackgroundScheduler(timezone='America/Argentina/Buenos_Aires')
        scheduler.start()

        jm = JobManager(scheduler, data_dir=DATA_DIR, config_dir=CONFIG_DIR)

        # Job 1 — Daily 7:00 AM ART (ya existía, ahora con retry + histórico)
        jm.register_job(
            'daily_update', _scheduler_run_all,
            CronTrigger(hour=7, minute=0, timezone='America/Argentina/Buenos_Aires'),
            name='Actualización diaria',
            description='Refresh principal de data: reputación, stock, posiciones, Full, Meli Ads, monitor',
        )

        # Job 2 — Repricing horario comercial (8-22 ART) cada 1h
        jm.register_job(
            'repricing_hourly', _job_repricing_hourly,
            CronTrigger(hour='8-22', minute=0, timezone='America/Argentina/Buenos_Aires'),
            name='Repricing horario',
            description='Buy Box + repricing automático en horario comercial. Skip si no hay reglas activas.',
        )

        # Job 3 — Preguntas cada 15 min (8-22 ART)
        jm.register_job(
            'questions_15min', _job_questions_15min,
            CronTrigger(hour='8-22', minute='*/15', timezone='America/Argentina/Buenos_Aires'),
            name='Preguntas pendientes',
            description='Refrescar preguntas sin responder cada 15 min en horario comercial',
        )

        # Job 4 — RESERVADO (re-optimización semanal). Disparable manual.
        jm.register_job(
            'weekly_reopt', _job_weekly_reopt,
            None,   # trigger ignorado para reservados
            name='Re-optimización semanal IA',
            description='Re-optimizar publicaciones que perdieron >5 posiciones esta semana. Solo disparo manual.',
            reserved=True,
        )

        # Job 5 — Buy Box check cada 6 horas
        jm.register_job(
            'buybox_6h', _job_buybox_check,
            IntervalTrigger(hours=6, timezone='America/Argentina/Buenos_Aires'),
            name='Buy Box check',
            description='Verificar Buy Box en publicaciones de catálogo cada 6h. Alerta si se perdió.',
        )

        global _job_manager
        _job_manager = jm

        print(f'[scheduler] Activo — 5 jobs registrados (1 reservado para activación manual)')
        return scheduler
    except Exception as e:
        print(f'[scheduler] No se pudo iniciar: {e}')
        return None


# Ruta para ver estado y forzar actualización manual
@app.route('/api/debug-data/<alias>')
def api_debug_data(alias):
    """Diagnóstico: muestra qué datos hay en PostgreSQL para una cuenta."""
    s = safe(alias)
    stock_path = os.path.join(DATA_DIR, f'stock_{s}.json')
    rep_path   = os.path.join(DATA_DIR, f'reputacion_{s}.json')
    pos_path   = os.path.join(DATA_DIR, f'posiciones_{s}.json')
    from core.db_storage import _key
    stock = load_json(stock_path)
    rep   = load_json(rep_path)
    pos   = load_json(pos_path)
    return jsonify({
        'stock_key':   _key(stock_path),
        'rep_key':     _key(rep_path),
        'pos_key':     _key(pos_path),
        'stock_found': stock is not None,
        'stock_fecha': (stock or {}).get('fecha'),
        'stock_items': len((stock or {}).get('items', [])),
        'rep_found':   rep is not None,
        'rep_entries': len(rep) if isinstance(rep, list) else 0,
        'pos_found':   pos is not None,
        'pos_items':   len(pos) if isinstance(pos, dict) else 0,
    })


@app.route('/api/ping')
def api_ping():
    """Health check público — usado por servicios externos para mantener el servidor despierto."""
    updated_today = bool(_scheduler_last_run and _scheduler_last_run.date() >= date.today())
    return jsonify({'ok': True, 'updated_today': updated_today,
                    'last_run': _scheduler_last_run.strftime('%Y-%m-%d %H:%M') if _scheduler_last_run else None})


@app.route('/api/scheduler-status')
def api_scheduler_status():
    next_run = None
    if _app_scheduler and _app_scheduler.get_jobs():
        nr = _app_scheduler.get_jobs()[0].next_run_time
        # Convertir a hora Argentina para mostrar
        try:
            import pytz
            art = pytz.timezone('America/Argentina/Buenos_Aires')
            nr_art = nr.astimezone(art)
            next_run = nr_art.strftime('%Y-%m-%d %H:%M (ART)')
        except Exception:
            next_run = str(nr)
    return jsonify({
        'ok': True,
        'scheduler_active': _app_scheduler is not None and _app_scheduler.running,
        'next_run': next_run,
        'last_run': _scheduler_last_run.strftime('%Y-%m-%d %H:%M (ART)') if _scheduler_last_run else 'Nunca (desde el último reinicio del servidor)',
    })


@app.route('/api/scheduler-run-now', methods=['POST'])
def api_scheduler_run_now():
    """Fuerza una actualización inmediata (manual). Mantenido para retrocompat."""
    import threading
    t = threading.Thread(target=_scheduler_run_all, daemon=True)
    t.start()
    return jsonify({'ok': True, 'mensaje': 'Actualización iniciada en background.'})


# ── Cron Jobs — gestión vía JobManager ──────────────────────────────────────

@app.route('/api/scheduler-jobs')
def api_scheduler_jobs():
    """Lista de jobs registrados con metadata + estado (next_run, last_run, etc.)."""
    if _job_manager is None:
        return jsonify({'ok': False, 'error': 'Scheduler no inicializado'}), 503
    return jsonify({'ok': True, 'jobs': _job_manager.list_jobs()})


@app.route('/api/scheduler-toggle/<job_id>', methods=['POST'])
def api_scheduler_toggle(job_id):
    """Pausa o reanuda un job. Body: {action: 'pause'|'resume'}."""
    if _job_manager is None:
        return jsonify({'ok': False, 'error': 'Scheduler no inicializado'}), 503
    body = request.get_json(silent=True) or {}
    action = (body.get('action') or '').strip().lower()
    if action == 'pause':
        _job_manager.pause_job(job_id)
        return jsonify({'ok': True, 'job_id': job_id, 'paused': True})
    if action == 'resume':
        _job_manager.resume_job(job_id)
        return jsonify({'ok': True, 'job_id': job_id, 'paused': False})
    return jsonify({'ok': False, 'error': "action debe ser 'pause' o 'resume'"}), 400


@app.route('/api/scheduler-history/<job_id>')
def api_scheduler_history(job_id):
    """Últimas N corridas del job (más reciente primero). Default: 20."""
    if _job_manager is None:
        return jsonify({'ok': False, 'error': 'Scheduler no inicializado'}), 503
    try:
        limit = int(request.args.get('limit', 20))
    except (TypeError, ValueError):
        limit = 20
    return jsonify({
        'ok':      True,
        'job_id':  job_id,
        'history': _job_manager.get_history(job_id, limit=limit),
    })


@app.route('/api/scheduler-run-job/<job_id>', methods=['POST'])
def api_scheduler_run_job(job_id):
    """Dispara un job específico manualmente (incluido los reservados)."""
    if _job_manager is None:
        return jsonify({'ok': False, 'error': 'Scheduler no inicializado'}), 503
    ok = _job_manager.run_now(job_id)
    if not ok:
        return jsonify({'ok': False, 'error': f'Job no encontrado: {job_id}'}), 404
    return jsonify({'ok': True, 'mensaje': f'Job {job_id} disparado en background.'})


@app.route('/api/reporte-semanal-download')
def api_reporte_semanal_download():
    """Genera reporte semanal como JSON descargable.

    Cuando se configure SMTP, el envío automático por email se va a
    apoyar en este mismo generador. Por ahora solo descarga.
    """
    from core.account_manager import AccountManager
    from datetime import datetime as _dt, timedelta as _td

    mgr = AccountManager()
    accounts = [a for a in mgr.list_accounts() if a.active]
    fecha_desde = (_dt.now() - _td(days=7)).strftime('%Y-%m-%d')
    fecha_hasta = _dt.now().strftime('%Y-%m-%d')

    reporte: dict = {
        'fecha_generado':  _dt.now().strftime('%Y-%m-%d %H:%M (ART)'),
        'periodo':         f'{fecha_desde} al {fecha_hasta}',
        'cuentas':         [],
    }

    for acc in accounts:
        s = safe(acc.alias)
        stock_data = load_json(os.path.join(DATA_DIR, f'stock_{s}.json')) or {}
        rep_data   = load_json(os.path.join(DATA_DIR, f'reputacion_{s}.json')) or []
        items      = stock_data.get('items', [])

        ventas_30d_total = sum(int(i.get('ventas_30d') or 0) for i in items)
        visitas_30d_tot  = sum(int(i.get('visitas_30d') or 0) for i in items)
        rep_latest       = rep_data[-1] if rep_data else {}

        sin_stock  = [i for i in items if i.get('alerta_stock') == 'SIN_STOCK']
        margen_neg = [i for i in items if i.get('alerta_margen') == 'NEGATIVO']

        reporte['cuentas'].append({
            'alias':            acc.alias,
            'publicaciones':    len(items),
            'ventas_30d_total': ventas_30d_total,
            'visitas_30d_tot':  visitas_30d_tot,
            'reputacion':       {
                'reclamos_pct':      rep_latest.get('reclamos_pct'),
                'demoras_pct':       rep_latest.get('demoras_pct'),
                'cancelaciones_pct': rep_latest.get('cancelaciones_pct'),
            },
            'alertas': {
                'sin_stock':       len(sin_stock),
                'margen_negativo': len(margen_neg),
            },
        })

    # Histórico del scheduler para incluir en el reporte
    if _job_manager is not None:
        reporte['scheduler'] = {
            'jobs':            _job_manager.list_jobs(),
        }

    fname = f'reporte_semanal_{_dt.now().strftime("%Y%m%d_%H%M")}.json'
    body  = json.dumps(reporte, ensure_ascii=False, indent=2)
    resp  = make_response(body)
    resp.headers['Content-Type']        = 'application/json; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


# ── Mercado Envíos Full ───────────────────────────────────────────────────────

@app.route('/full/<alias>')
def full_page(alias):
    return render_template('full.html', alias=alias, accounts=get_accounts())


@app.route('/api/full-stock-check/<alias>/<item_id>')
def api_full_stock_check(alias, item_id):
    """Consulta directa al API de ML para verificar el stock real de una publicación Full."""
    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    try:
        r = req_lib.get(
            f'https://api.mercadolibre.com/items/{item_id}',
            headers=heads,
            params={'attributes': 'id,title,available_quantity,sold_quantity,inventory_id,status,shipping'},
            timeout=10)
        if not r.ok:
            return jsonify({'ok': False, 'error': f'ML API: {r.status_code}'}), r.status_code

        body          = r.json()
        inv_id        = body.get('inventory_id', '')
        total_ml      = int(body.get('available_quantity', 0) or 0)
        stock_en_full = total_ml
        deposito      = 0

        if inv_id:
            try:
                rf = req_lib.get(
                    f'https://api.mercadolibre.com/inventories/{inv_id}/stock/fulfillment',
                    headers=heads, timeout=6)
                if rf.ok:
                    fdata   = rf.json()
                    f_total = fdata.get('total')
                    if f_total is not None:
                        stock_en_full = int(f_total)
                        deposito      = max(0, total_ml - stock_en_full)
            except Exception:
                pass

        return jsonify({
            'ok':             True,
            'id':             body.get('id'),
            'titulo':         body.get('title', '')[:80],
            'status':         body.get('status'),
            'stock_en_full':  stock_en_full,
            'deposito':       deposito,
            'total_ml':       total_ml,
            'sold_quantity':  body.get('sold_quantity'),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _sync_full_inventory(alias):
    """Consulta /inventories/{id}/stock/fulfillment para cada ítem Full y guarda en cache.

    El endpoint batch /items?ids=... no devuelve inventory_id, por eso:
      1. Batch con 'shipping' → identificar ítems Full por logistic_type
      2. Fetch individual por cada ítem Full → obtener inventory_id real
      3. /inventories/{id}/stock/fulfillment → separar stock ML vs depósito
    """
    from core.db_storage import db_load, db_save as db_save_fn
    from datetime import datetime as _dt
    import requests as _rq

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        print(f'[full_inventory] {alias} — auth error: {e}')
        return

    ML = 'https://api.mercadolibre.com'
    FULL_LOGISTICS = ('fulfillment', 'meli_fulfillment', 'self_service_fulfillment')

    # ── 1. Obtener todos los IDs activos ──────────────────────────────────────
    all_ids, offset = [], 0
    while True:
        r = _rq.get(f'{ML}/users/{user_id}/items/search', headers=heads,
                    params={'status': 'active', 'limit': 100, 'offset': offset}, timeout=12)
        if not r.ok:
            break
        data  = r.json()
        ids   = data.get('results', [])
        total = data.get('paging', {}).get('total', 0)
        all_ids.extend(ids)
        offset += len(ids)
        if not ids or offset >= total:
            break
        _time_module.sleep(0.1)

    print(f'[full_inventory] {alias} — {len(all_ids)} ítems activos encontrados')

    # ── 2. Identificar ítems Full por logistic_type (batch sí devuelve shipping) ─
    full_ids = []
    for b in range(0, len(all_ids), 20):
        batch = all_ids[b:b+20]
        try:
            r = _rq.get(f'{ML}/items', headers=heads,
                        params={'ids': ','.join(batch),
                                'attributes': 'id,shipping'},
                        timeout=12)
            if r.ok:
                for e in r.json():
                    if e.get('code') != 200:
                        continue
                    body    = e.get('body', {})
                    iid     = body.get('id', '')
                    logtype = (body.get('shipping') or {}).get('logistic_type', '')
                    if iid and logtype in FULL_LOGISTICS:
                        full_ids.append(iid)
        except Exception:
            pass
        _time_module.sleep(0.1)

    print(f'[full_inventory] {alias} — {len(full_ids)} ítems Full identificados')

    if not full_ids:
        # Guardar cache vacío para que el endpoint no muestre "sin sincronizar"
        cache = {'last_updated': _dt.now().strftime('%Y-%m-%d %H:%M'), 'items': {}}
        db_save_fn(os.path.join(DATA_DIR, f'full_inventory_{safe(alias)}.json'), cache)
        return

    # ── 3. Fetch individual por cada Full → obtener inventory_id ─────────────
    # El batch endpoint NO devuelve inventory_id; solo funciona en fetch individual
    result = {}
    for iid in full_ids:
        try:
            r = _rq.get(f'{ML}/items/{iid}',
                        headers=heads,
                        params={'attributes': 'id,available_quantity,inventory_id'},
                        timeout=8)
            if not r.ok:
                _time_module.sleep(0.2)
                continue
            body      = r.json()
            inv_id    = body.get('inventory_id', '')
            total_ml  = int(body.get('available_quantity', 0) or 0)
            stock_en_full = total_ml
            deposito      = 0

            if inv_id:
                try:
                    rf = _rq.get(f'{ML}/inventories/{inv_id}/stock/fulfillment',
                                 headers=heads, timeout=(3, 6))
                    if rf.ok:
                        fdata = rf.json()
                        # IMPORTANTE: usar comparación con None, no 'or'
                        # porque total=0 es válido (stock en ML = 0, todo en depósito)
                        f_total = fdata.get('total')
                        if f_total is not None:
                            stock_en_full = int(f_total)
                            deposito      = max(0, total_ml - stock_en_full)
                except Exception:
                    pass

            result[iid] = {'stock_en_full': stock_en_full, 'deposito': deposito,
                           'inv_id': inv_id}
        except Exception:
            pass
        _time_module.sleep(0.2)

    cache = {'last_updated': _dt.now().strftime('%Y-%m-%d %H:%M'), 'items': result}
    db_save_fn(os.path.join(DATA_DIR, f'full_inventory_{safe(alias)}.json'), cache)
    con_dep = sum(1 for v in result.values() if v.get('deposito', 0) > 0)
    print(f'[full_inventory] {alias} — {len(result)} ítems sincronizados, {con_dep} con stock en depósito')


@app.route('/api/full-inventory-sync/<alias>', methods=['POST'])
def api_full_inventory_sync(alias):
    """Sincroniza stock Full/depósito y devuelve resultado."""
    _sync_full_inventory(alias)
    cache = db_load(os.path.join(DATA_DIR, f'full_inventory_{safe(alias)}.json')) or {}
    items = cache.get('items', {})
    con_deposito = sum(1 for v in items.values() if v.get('deposito', 0) > 0)
    return jsonify({'ok': True,
                    'count': len(items),
                    'con_deposito': con_deposito,
                    'last_updated': cache.get('last_updated')})


@app.route('/api/full-inventory-cache/<alias>')
def api_full_inventory_cache(alias):
    """Devuelve metadata del último cache de inventario Full."""
    from core.db_storage import db_load
    cache = db_load(os.path.join(DATA_DIR, f'full_inventory_{safe(alias)}.json')) or {}
    items = cache.get('items', {})
    sample = [{'id': k, **v} for k, v in list(items.items())[:5]]
    return jsonify({'ok': True,
                    'last_updated': cache.get('last_updated'),
                    'count': len(items),
                    'sample': sample})


@app.route('/api/list-aliases')
def api_list_aliases():
    """Temporal: lista los aliases disponibles en el servidor."""
    accs = (load_json(os.path.join(CONFIG_DIR, 'accounts.json')) or {}).get('accounts', [])
    return jsonify({'aliases': [a.get('alias') for a in accs if a.get('active')]})


@app.route('/api/full-debug/<alias>')
def api_full_debug(alias):
    """Diagnóstico: busca el primer ítem Full activo y prueba todos los endpoints de inventario."""
    import requests as _rq
    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

    ML = 'https://api.mercadolibre.com'
    FULL_LOGISTICS = ('fulfillment', 'meli_fulfillment', 'self_service_fulfillment')

    # Buscar primer ítem Full activo directamente desde la API (sin cache)
    iid, inv_id = None, None
    try:
        r_ids = _rq.get(f'{ML}/users/{user_id}/items/search', headers=heads,
                        params={'status': 'active', 'limit': 50}, timeout=10)
        if r_ids.ok:
            ids = r_ids.json().get('results', [])
            # Batch para identificar cuáles son Full
            for b in range(0, len(ids), 20):
                batch = ids[b:b+20]
                rb = _rq.get(f'{ML}/items', headers=heads,
                             params={'ids': ','.join(batch), 'attributes': 'id,shipping,inventory_id'},
                             timeout=10)
                if rb.ok:
                    for e in rb.json():
                        if e.get('code') != 200:
                            continue
                        body    = e.get('body', {})
                        logtype = (body.get('shipping') or {}).get('logistic_type', '')
                        if logtype in FULL_LOGISTICS:
                            iid    = body.get('id')
                            inv_id = body.get('inventory_id', '')
                            break
                if iid:
                    break
    except Exception as ex:
        return jsonify({'ok': False, 'error': f'Error buscando ítems: {ex}'})

    if not iid:
        return jsonify({'ok': False, 'error': 'No se encontraron ítems Full activos en esta cuenta.'})

    result = {'item_id': iid, 'inv_id': inv_id, 'endpoints': {}}

    # Item individual (available_quantity + inventory_id)
    r4 = _rq.get(f'{ML}/items/{iid}', headers=heads, timeout=8)
    if r4.ok:
        body = r4.json()
        result['item_fields'] = {k: body.get(k) for k in
            ['id', 'available_quantity', 'inventory_id', 'shipping', 'status', 'sold_quantity']}
        if not inv_id:
            inv_id = body.get('inventory_id', '')
            result['inv_id'] = inv_id

    if inv_id:
        r1 = _rq.get(f'{ML}/inventories/{inv_id}/stock/fulfillment', headers=heads, timeout=8)
        result['endpoints']['stock_fulfillment'] = r1.json() if r1.ok else f'HTTP {r1.status_code}'

        r2 = _rq.get(f'{ML}/inventories/{inv_id}/stock', headers=heads, timeout=8)
        result['endpoints']['stock'] = r2.json() if r2.ok else f'HTTP {r2.status_code}'

        r3 = _rq.get(f'{ML}/inventories/{inv_id}', headers=heads, timeout=8)
        result['endpoints']['inventory'] = r3.json() if r3.ok else f'HTTP {r3.status_code}'
    else:
        result['endpoints']['nota'] = 'inventory_id vacío — ítem Full sin inventory_id asignado'

    return jsonify({'ok': True, **result})


@app.route('/api/full-data/<alias>')
def api_full_data(alias):
    """Análisis completo de publicaciones Full: stock, velocidad, alertas, reposición."""
    from datetime import datetime as _dt, timedelta as _td
    from core.db_storage import db_load

    try:
        token, user_id, heads = _ml_auth(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 401

    ML = 'https://api.mercadolibre.com'

    # Configuración guardada
    full_cfg = db_load(os.path.join(CONFIG_DIR, 'full_config.json'))  or {'global_lead_days': 18, 'items': {}}
    rep_cfg  = db_load(os.path.join(CONFIG_DIR, 'reposicion.json'))   or {'transit_days_global': 25, 'items': {}}
    costos   = db_load(os.path.join(CONFIG_DIR, 'costos.json'))       or {}
    # Días sin venta a partir de los cuales ML cobra almacenamiento (configurable, default 60)
    dias_muerto_full = int(full_cfg.get('dias_muerto_full', 60))

    # Stock snapshot guardado (velocidad pre-calculada)
    stock_snap = db_load(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    stock_map  = {s.get('id', ''): s for s in stock_snap.get('items', [])}

    # Unidades en tránsito a Full (pedidos con estado = enviado_full)
    pedidos_data = db_load(_pedidos_path(alias)) or {'pedidos': []}
    en_transito_map = {}
    for p in pedidos_data.get('pedidos', []):
        if p.get('estado') == 'enviado_full':
            for it in p.get('items', []):
                iid = it.get('item_id', '')
                if iid:
                    en_transito_map[iid] = en_transito_map.get(iid, 0) + int(it.get('unidades', 0))

    # 1 — Todos los IDs activos
    all_ids, offset = [], 0
    while True:
        r = req_lib.get(f'{ML}/users/{user_id}/items/search', headers=heads,
                        params={'status': 'active', 'limit': 100, 'offset': offset}, timeout=12)
        if not r.ok:
            break
        ids   = r.json().get('results', [])
        total = r.json().get('paging', {}).get('total', 0)
        all_ids.extend(ids)
        offset += len(ids)
        if not ids or offset >= total:
            break
        _time_module.sleep(0.1)

    # 3a — Leer cache de inventario Full (generado por scheduler o sync manual)
    # IMPORTANTE: la cache es la fuente de verdad para detectar ítems en Full.
    # ML cambia logistic_type dinámicamente (ej: pasa a 'self_service_in' cuando
    # el stock en el almacén ML llega a 0), así que no se puede confiar solo en
    # logistic_type para saber si un ítem está inscripto en Full.
    inv_cache = db_load(os.path.join(DATA_DIR, f'full_inventory_{safe(alias)}.json')) or {}
    inv_items  = inv_cache.get('items', {})
    fulfillment_map = {
        iid: (v.get('stock_en_full', 0), v.get('deposito', 0))
        for iid, v in inv_items.items()
    }
    cached_full_ids = set(fulfillment_map.keys())

    # 2 — Fetch detalles: separar Full vs no-Full
    # Full = en cache de inventario (ya sincronizado) O logistic_type en FULL_LOGISTICS
    FULL_LOGISTICS = ('fulfillment', 'meli_fulfillment', 'self_service_fulfillment')
    full_items  = []
    nofull_items = []
    for b in range(0, len(all_ids), 20):
        batch = all_ids[b:b+20]
        try:
            r = req_lib.get(f'{ML}/items', headers=heads,
                            params={'ids': ','.join(batch),
                                    'attributes': 'id,title,price,available_quantity,inventory_id,shipping,listing_type_id,permalink'},
                            timeout=12)
            if r.ok:
                for e in r.json():
                    if e.get('code') != 200:
                        continue
                    body     = e.get('body', {})
                    shipping = body.get('shipping') or {}
                    logistic = shipping.get('logistic_type', '')
                    iid      = body.get('id', '')
                    item_data = {
                        'id':           iid,
                        'titulo':       body.get('title', '')[:65],
                        'precio':       float(body.get('price', 0) or 0),
                        'stock':        int(body.get('available_quantity', 0) or 0),
                        'inventory_id': body.get('inventory_id', ''),
                        'listing_type': body.get('listing_type_id', ''),
                        'permalink':    body.get('permalink', ''),
                        'free_shipping': bool(shipping.get('free_shipping')),
                    }
                    if iid in cached_full_ids or logistic in FULL_LOGISTICS:
                        full_items.append(item_data)
                    else:
                        nofull_items.append(item_data)
        except Exception:
            pass
        _time_module.sleep(0.1)

    # 3 — Analizar cada item Full
    results = []
    for item in full_items:
        iid   = item['id']
        saved = stock_map.get(iid, {})

        # Velocidad: usar snapshot si existe, sino llamar API
        vel_30 = float(saved.get('velocidad', 0) or 0)
        if vel_30 == 0:
            try:
                desde = (_dt.utcnow() - _td(hours=3) - _td(days=30)).strftime('%Y-%m-%dT00:00:00.000-03:00')
                data  = req_lib.get(f'{ML}/orders/search', headers=heads,
                    params={'seller': user_id, 'item': iid, 'order.status': 'paid',
                            'order.date_created.from': desde, 'limit': 50}, timeout=8).json()
                units = sum(
                    sum(oi.get('quantity', 0) for oi in o.get('order_items', [])
                        if oi.get('item', {}).get('id') == iid)
                    for o in data.get('results', [])
                )
                vel_30 = round(units / 30, 2)
            except Exception:
                vel_30 = 0.0
            _time_module.sleep(0.15)

        # Última venta (para detectar stock muerto)
        sin_ventas_dias = None
        if vel_30 == 0:
            try:
                desde90 = (_dt.utcnow() - _td(hours=3) - _td(days=90)).strftime('%Y-%m-%dT00:00:00.000-03:00')
                data90  = req_lib.get(f'{ML}/orders/search', headers=heads,
                    params={'seller': user_id, 'item': iid, 'order.status': 'paid',
                            'order.date_created.from': desde90, 'limit': 1, 'sort': 'date_desc'},
                    timeout=8).json()
                orders90 = data90.get('results', [])
                if orders90:
                    last = orders90[0].get('date_created', '')[:10]
                    sin_ventas_dias = (_dt.utcnow().date() - _dt.strptime(last, '%Y-%m-%d').date()).days
                else:
                    sin_ventas_dias = 90
            except Exception:
                sin_ventas_dias = None
            _time_module.sleep(0.1)

        # Config por item
        item_full = full_cfg.get('items', {}).get(iid, {})
        item_rep  = rep_cfg.get('items', {}).get(iid, {})
        lead_days = item_full.get('lead_days',    full_cfg.get('global_lead_days', 18))
        transit   = item_rep.get('transit_days',  rep_cfg.get('transit_days_global', 25))
        en_transito = en_transito_map.get(iid, 0)

        # Stock separado: Full (en almacén ML) vs depósito propio (en bodega del vendedor)
        # deposito_stock se decrementa al marcar pedido como "enviado_full"
        manual_dep = item_rep.get('deposito_stock')
        if iid in fulfillment_map:
            stock_en_full, cache_dep = fulfillment_map[iid]
            cache_dep_int = int(cache_dep) if cache_dep else 0
            if cache_dep_int > 0 and manual_dep is not None:
                # Ambas fuentes disponibles: usar el menor (más conservador)
                deposito = min(cache_dep_int, int(manual_dep))
            elif cache_dep_int > 0:
                deposito = cache_dep_int
            elif manual_dep is not None:
                # ML API no devolvió deposito (devuelve 0); usar seguimiento manual
                deposito = int(manual_dep)
            else:
                deposito = None
        else:
            # Sin sync: si hay deposito_stock manual lo usamos; si no, es desconocido (None)
            stock_en_full = item['stock']
            deposito = int(manual_dep) if manual_dep is not None else None
        available_qty   = item['stock']   # available_quantity total desde ML
        dep_real        = deposito if deposito is not None else 0
        stock_full_real = stock_en_full   # lo que está físicamente en el almacén de ML
        item['stock']   = available_qty
        stock_total     = available_qty + en_transito

        # Días de stock (incluye en tránsito a Full)
        dias_stock = round(stock_total / vel_30, 1) if vel_30 > 0 else None
        # Días solo del stock físico en almacén Full (sin depósito ni en tránsito)
        dias_full  = round(stock_full_real / vel_30, 1) if vel_30 > 0 and stock_full_real > 0 else None

        # Margen (Full siempre free shipping, commission 16.5% gold_pro / 13% gold_special)
        comision = 0.13 if item['listing_type'] in ('gold_special', 'silver') else 0.165
        neto     = round(item['precio'] * (1 - comision), 2)
        costo_d  = costos.get(iid, {})
        costo    = costo_d.get('costo') if costo_d else None
        margen_pct = round((neto - costo) / item['precio'] * 100, 1) if costo else None

        # Unidades a pedir (descuenta en tránsito — ya están en camino)
        unidades_pedir = 0
        if vel_30 > 0:
            unidades_pedir = max(0, round((transit + 14) * vel_30 * 1.3 - stock_total))

        # Revenue 30 días estimado
        revenue_30d = round(vel_30 * item['precio'] * 30) if vel_30 > 0 else 0

        # Valor de inventario
        valor_venta = round(item['precio'] * item['stock'])
        valor_costo = round(costo * item['stock']) if costo else None

        # Costo almacenamiento acumulado: solo sobre stock en Full (~$12/u/día)
        costo_almac = None
        if sin_ventas_dias and stock_full_real > 0:
            costo_almac = round(stock_full_real * sin_ventas_dias * 12.0)

        # Clasificación (en tránsito cuenta para evitar falsas alertas)
        alerta = 'OK'
        if item['stock'] == 0 and en_transito == 0:
            alerta = 'SIN_STOCK'
        elif dias_stock is not None and dias_stock <= lead_days:
            alerta = 'REPONER_URGENTE'
        elif sin_ventas_dias is not None and sin_ventas_dias >= dias_muerto_full and stock_full_real > 0:
            # Stock muerto REAL: unidades físicas en almacén ML sin venderse → ML cobra almacenamiento
            alerta = 'STOCK_MUERTO'
        elif sin_ventas_dias is not None and sin_ventas_dias >= 21 and (deposito or 0) > 0:
            # Stock lento en depósito propio: no genera costo ML, pero tampoco se mueve
            alerta = 'SIN_MOVIMIENTO'
        elif vel_30 > 0 and margen_pct is not None and margen_pct >= 15 and (dias_stock is None or dias_stock >= 30):
            alerta = 'ESCALAR'

        results.append({
            'id':               iid,
            'titulo':           item['titulo'],
            'precio':           item['precio'],
            'stock':            item['stock'],
            'available_quantity': available_qty,
            'stock_full_real':  stock_full_real,
            'deposito':         deposito,
            'en_transito':     en_transito,
            'stock_total':     stock_total,
            'vel_30':          vel_30,
            'dias_stock':      dias_stock,
            'dias_full':       dias_full,
            'lead_days':       lead_days,
            'transit':         transit,
            'margen_pct':      margen_pct,
            'sin_ventas_dias': sin_ventas_dias,
            'unidades_pedir':  unidades_pedir,
            'revenue_30d':     revenue_30d,
            'valor_venta':     valor_venta,
            'valor_costo':     valor_costo,
            'costo_almac':     costo_almac,
            'alerta':          alerta,
            'permalink':       item['permalink'],
        })

    alerta_order = {'SIN_STOCK': 0, 'REPONER_URGENTE': 1, 'STOCK_MUERTO': 2, 'SIN_MOVIMIENTO': 3, 'ESCALAR': 4, 'OK': 5}
    def _full_sort_key(x):
        sfr = x.get('stock_full_real') or 0
        df  = x.get('dias_full')
        ao  = alerta_order.get(x['alerta'], 9)
        vel = -(x['vel_30'] or 0)
        if sfr > 0:
            # Tiene stock en Full: primero y ordenados por días restantes en Full (más urgente primero)
            return (0, df if df is not None else 9999, ao, vel)
        else:
            return (1, 9999, ao, vel)
    results.sort(key=_full_sort_key)

    costo_muerto = sum(r.get('costo_almac') or 0 for r in results if r['alerta'] == 'STOCK_MUERTO')
    resumen = {
        'total':            len(results),
        'sin_stock':        sum(1 for r in results if r['alerta'] == 'SIN_STOCK'),
        'urgente':          sum(1 for r in results if r['alerta'] == 'REPONER_URGENTE'),
        'muerto':           sum(1 for r in results if r['alerta'] == 'STOCK_MUERTO'),
        'sin_movimiento':   sum(1 for r in results if r['alerta'] == 'SIN_MOVIMIENTO'),
        'escalar':          sum(1 for r in results if r['alerta'] == 'ESCALAR'),
        'ok':               sum(1 for r in results if r['alerta'] in ('OK',)),
        'costo_muerto':     costo_muerto,
        'valor_venta_total': sum(r.get('valor_venta') or 0 for r in results),
        'valor_costo_total': sum(r.get('valor_costo') or 0 for r in results if r.get('valor_costo') is not None),
        'revenue_30d_total': sum(r.get('revenue_30d') or 0 for r in results),
    }

    # 4 — Sugeridos para Full: items NO en Full con buena velocidad de ventas
    full_ids_set = {r['id'] for r in results}
    sugeridos = []
    for item in nofull_items:
        iid   = item['id']
        if iid in full_ids_set:
            continue
        saved  = stock_map.get(iid, {})
        vel_30 = float(saved.get('velocidad', 0) or 0)
        if vel_30 < 0.2:   # mínimo ~6 unidades/mes para considerar
            continue
        if item['stock'] == 0:
            continue

        comision  = 0.13 if item['listing_type'] in ('gold_special', 'silver') else 0.165
        neto_full = round(item['precio'] * (1 - comision), 2)  # Full siempre free shipping
        costo_d   = costos.get(iid, {})
        costo     = costo_d.get('costo') if costo_d else None
        margen_full = round((neto_full - costo) / item['precio'] * 100, 1) if costo else None

        # Razones por las que conviene Full
        razones = []
        if vel_30 >= 1.0:
            razones.append(f'{vel_30:.1f} ventas/día — alta rotación')
        elif vel_30 >= 0.5:
            razones.append(f'{vel_30:.1f} ventas/día — buena rotación')
        else:
            razones.append(f'{vel_30:.1f} ventas/día — rotación moderada')
        if not item['free_shipping']:
            razones.append('sin envío gratis — Full lo activaría automáticamente')
        if margen_full is not None and margen_full >= 15:
            razones.append(f'margen {margen_full}% absorbería costo Full')

        # Score para ordenar: velocidad × bonus margen × bonus sin free shipping
        score = vel_30
        if margen_full is not None and margen_full >= 15:
            score *= 1.3
        if not item['free_shipping']:
            score *= 1.2

        sugeridos.append({
            'id':          iid,
            'titulo':      item['titulo'],
            'precio':      item['precio'],
            'stock':       item['stock'],
            'vel_30':      vel_30,
            'margen_full': margen_full,
            'free_ship':   item['free_shipping'],
            'permalink':   item['permalink'],
            'razones':     razones,
            'score':       round(score, 3),
        })

    sugeridos.sort(key=lambda x: -x['score'])

    return jsonify({'ok': True, 'items': results, 'resumen': resumen,
                    'sugeridos': sugeridos[:20],   # top 20
                    'global_lead_days': full_cfg.get('global_lead_days', 18),
                    'transit_days_global': rep_cfg.get('transit_days_global', 25)})


@app.route('/api/full-deposito-update', methods=['POST'])
def api_full_deposito_update():
    """Guarda el stock de depósito propio de un ítem Full directamente desde la tabla."""
    from core.db_storage import db_load, db_save as db_save_fn
    body    = request.get_json() or {}
    alias   = body.get('alias', '').strip()
    item_id = body.get('item_id', '').strip().upper()
    val     = int(body.get('deposito_stock', 0) or 0)
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Faltan campos'}), 400
    try:
        _resolve_alias(alias)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    rep_path = os.path.join(CONFIG_DIR, 'reposicion.json')
    rep_cfg  = db_load(rep_path) or {}
    rep_cfg.setdefault('items', {})[item_id] = {
        **(rep_cfg.get('items', {}).get(item_id) or {}),
        'deposito_stock': val,
    }
    db_save_fn(rep_path, rep_cfg)
    return jsonify({'ok': True, 'item_id': item_id, 'deposito_stock': val})


@app.route('/api/full-config/<alias>', methods=['GET', 'POST'])
def api_full_config(alias):
    """Lee o guarda la configuración de Full (lead times, tránsito, depósito)."""
    from core.db_storage import db_load, db_save as db_save_fn
    full_path = os.path.join(CONFIG_DIR, 'full_config.json')
    rep_path  = os.path.join(CONFIG_DIR, 'reposicion.json')

    if request.method == 'GET':
        full_cfg = db_load(full_path) or {'global_lead_days': 18, 'items': {}}
        rep_cfg  = db_load(rep_path)  or {'transit_days_global': 25, 'items': {}}
        return jsonify({'ok': True, 'full': full_cfg, 'rep': rep_cfg})

    data = request.get_json(silent=True) or {}
    try:
        if 'full' in data:
            db_save_fn(full_path, data['full'])
        if 'rep' in data:
            db_save_fn(rep_path, data['rep'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Pedidos de reposición Full ────────────────────────────────────────────────

def _pedidos_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f'full_pedidos_{safe(alias)}.json')


@app.route('/api/full-pedidos/<alias>', methods=['GET'])
def api_full_pedidos_get(alias):
    """Lista todos los pedidos de reposición guardados."""
    from core.db_storage import db_load
    data = db_load(_pedidos_path(alias)) or {'pedidos': []}
    return jsonify({'ok': True, 'pedidos': data.get('pedidos', [])})


@app.route('/api/full-pedidos/<alias>', methods=['POST'])
def api_full_pedidos_create(alias):
    """Crea un nuevo pedido de reposición."""
    from core.db_storage import db_load, db_save as db_save_fn
    import uuid
    from datetime import datetime as _dt

    body = request.get_json(silent=True) or {}
    items = body.get('items', [])
    if not items:
        return jsonify({'ok': False, 'error': 'Sin items'}), 400

    path = _pedidos_path(alias)
    data = db_load(path) or {'pedidos': []}

    pedido = {
        'id':             str(uuid.uuid4())[:8],
        'fecha_creacion': (_dt.utcnow() - __import__('datetime').timedelta(hours=3)).strftime('%Y-%m-%d'),
        'estado':         'pendiente',
        'notas':          body.get('notas', ''),
        'items':          items,   # [{item_id, titulo, unidades, notas}]
    }
    data['pedidos'].insert(0, pedido)
    db_save_fn(path, data)
    return jsonify({'ok': True, 'pedido': pedido})


@app.route('/api/full-pedido/<alias>/<pedido_id>', methods=['PUT'])
def api_full_pedido_update(alias, pedido_id):
    """Actualiza estado o notas de un pedido."""
    from core.db_storage import db_load, db_save as db_save_fn

    body  = request.get_json(silent=True) or {}
    path  = _pedidos_path(alias)
    data  = db_load(path) or {'pedidos': []}
    found = False
    pedido_items = []
    for p in data['pedidos']:
        if p['id'] == pedido_id:
            nuevo_estado = body.get('estado')
            if nuevo_estado:
                p['estado'] = nuevo_estado
            if 'notas' in body:
                p['notas'] = body['notas']
            if 'items' in body:
                p['items'] = body['items']
            pedido_items = p.get('items', [])
            found = True
            break
    if not found:
        return jsonify({'ok': False, 'error': 'Pedido no encontrado'}), 404
    db_save_fn(path, data)

    # Al marcar como "Enviado a Full" descontar del depósito propio
    if body.get('estado') == 'enviado_full' and pedido_items:
        rep_path = os.path.join(CONFIG_DIR, 'reposicion.json')
        rep_cfg  = db_load(rep_path) or {}
        rep_cfg.setdefault('items', {})
        for it in pedido_items:
            iid   = it.get('item_id', '')
            units = int(it.get('unidades', 0))
            if iid and units > 0:
                actual = int((rep_cfg['items'].get(iid) or {}).get('deposito_stock', 0))
                nuevo  = max(0, actual - units)
                rep_cfg['items'].setdefault(iid, {})['deposito_stock'] = nuevo
        db_save_fn(rep_path, rep_cfg)

    return jsonify({'ok': True})


@app.route('/api/full-pedido/<alias>/<pedido_id>', methods=['DELETE'])
def api_full_pedido_delete(alias, pedido_id):
    """Elimina un pedido."""
    from core.db_storage import db_load, db_save as db_save_fn

    path = _pedidos_path(alias)
    data = db_load(path) or {'pedidos': []}
    data['pedidos'] = [p for p in data['pedidos'] if p['id'] != pedido_id]
    db_save_fn(path, data)
    return jsonify({'ok': True})


# ── Análisis Experto ──────────────────────────────────────────────────────────

@app.route('/analisis-experto/<alias>')
def analisis_experto_page(alias):
    return render_template('analisis_experto.html', alias=alias, accounts=get_accounts())


@app.route('/api/analisis-experto/<alias>', methods=['GET'])
def api_analisis_experto_get(alias):
    """Devuelve el último análisis guardado."""
    from core.db_storage import db_load
    data = db_load(os.path.join(DATA_DIR, f'analisis_experto_{safe(alias)}.json'))
    if not data:
        return jsonify({'ok': False, 'error': 'Sin análisis guardado'})
    return jsonify({'ok': True, **data})


@app.route('/api/analisis-experto/<alias>', methods=['POST'])
def api_analisis_experto_run(alias):
    """Recopila todos los datos de la cuenta y genera el análisis con Claude."""
    from core.db_storage import db_load, db_save as db_save_fn
    from datetime import datetime as _dt, timedelta as _td

    # ── 1. Recopilar datos de la cuenta ──────────────────────────────────────
    stock_snap = db_load(os.path.join(DATA_DIR, f'stock_{safe(alias)}.json')) or {}
    rep_snap   = db_load(os.path.join(DATA_DIR, f'reputacion_{safe(alias)}.json')) or {}
    items_raw  = stock_snap.get('items', [])

    if not items_raw:
        return jsonify({'ok': False, 'error': 'Sin datos de stock. Ejecutá la actualización primero.'}), 400

    # ── 2. Revenue, posiciones y preguntas via ML API ─────────────────────────
    revenue_hoy = revenue_7d = revenue_30d = revenue_prev_7d = None
    pos_drops    = []
    n_unanswered = 0
    seller_metrics = {}
    heads = user_id = None
    try:
        token, user_id, heads = _ml_auth(alias)
        now      = _dt.utcnow() - _td(hours=3)
        hoy_from = now.strftime('%Y-%m-%dT00:00:00.000-03:00')
        d7_from  = (now - _td(days=7)).strftime('%Y-%m-%dT00:00:00.000-03:00')
        d30_from = (now - _td(days=30)).strftime('%Y-%m-%dT00:00:00.000-03:00')
        d14_from = (now - _td(days=14)).strftime('%Y-%m-%dT00:00:00.000-03:00')
        d7_to    = (now - _td(days=7)).strftime('%Y-%m-%dT23:59:59.000-03:00')

        def _fetch_rev(date_from, date_to=None):
            total, offset = 0, 0
            while True:
                params = {'seller': user_id, 'order.status': 'paid',
                          'order.date_created.from': date_from,
                          'limit': 50, 'offset': offset}
                if date_to:
                    params['order.date_created.to'] = date_to
                r = req_lib.get('https://api.mercadolibre.com/orders/search',
                    headers=heads, params=params, timeout=10)
                if not r.ok:
                    break
                data    = r.json()
                results = data.get('results', [])
                for o in results:
                    total += sum(float(i.get('unit_price', 0)) * int(i.get('quantity', 0))
                                 for i in o.get('order_items', []))
                paging_total = data.get('paging', {}).get('total', 0)
                offset += len(results)
                if not results or offset >= paging_total or offset >= 1000:
                    break
            return round(total) if total else None

        revenue_hoy     = _fetch_rev(hoy_from)
        revenue_7d      = _fetch_rev(d7_from)
        revenue_30d     = _fetch_rev(d30_from)
        revenue_prev_7d = _fetch_rev(d14_from, d7_to)

        try:
            _rq = req_lib.get('https://api.mercadolibre.com/questions/search',
                headers=heads,
                params={'seller_id': user_id, 'status': 'UNANSWERED', 'limit': 1},
                timeout=8)
            if _rq.ok:
                n_unanswered = _rq.json().get('paging', {}).get('total', 0)
        except Exception:
            pass

        try:
            _rs = req_lib.get(f'https://api.mercadolibre.com/users/{user_id}',
                headers=heads, timeout=8)
            if _rs.ok:
                _rep = _rs.json().get('seller_reputation', {})
                _met = _rep.get('metrics', {})
                seller_metrics = {
                    'cancelaciones': _met.get('cancellations', {}).get('rate'),
                    'demoras':       _met.get('delayed_handling_time', {}).get('rate'),
                    'reclamos':      _met.get('claims', {}).get('rate'),
                }
        except Exception:
            pass
    except Exception:
        pass

    try:
        pos_snap = db_load(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
        _now_art = _dt.utcnow() - _td(hours=3)
        for _pid, _pdata in pos_snap.items():
            _hist = _pdata.get('history', {})
            _pa, _prev = None, None
            for _d in range(0, 2):
                _k = (_now_art - _td(days=_d)).strftime('%Y-%m-%d')
                if _k in _hist:
                    _pa = _hist[_k]; break
            for _d in range(2, 8):
                _k = (_now_art - _td(days=_d)).strftime('%Y-%m-%d')
                if _k in _hist:
                    _prev = _hist[_k]; break
            if _pa and _prev and _pa != 999 and _pa - _prev >= 5:
                pos_drops.append({'titulo': _pdata.get('title', '')[:50],
                                  'antes': _prev, 'ahora': _pa, 'caida': _pa - _prev})
        pos_drops.sort(key=lambda x: x['caida'], reverse=True)
    except Exception:
        pass

    # ── 3. Clasificar items ──────────────────────────────────────────────────
    items_sorted = sorted(items_raw, key=lambda x: float(x.get('precio', 0)) * float(x.get('ventas_30d') or x.get('velocidad', 0) * 30 or 0), reverse=True)

    top_items, criticos_stock, sin_ventas, baja_conversion = [], [], [], []

    for it in items_sorted:
        iid       = it.get('id', '')
        titulo    = it.get('titulo', it.get('title', ''))[:55]
        precio    = float(it.get('precio', 0) or 0)
        vel       = float(it.get('velocidad', 0) or 0)
        vtas_30   = int(it.get('ventas_30d') or round(vel * 30))
        stock     = int(it.get('stock', 0) or 0)
        dias_stk  = round(stock / vel, 1) if vel > 0 else None
        margen    = it.get('margen_pct')
        visitas   = int(it.get('visitas_30d') or 0)
        conv      = it.get('conversion_pct')
        ingreso_30 = round(precio * vtas_30)

        entry = {
            'id': iid, 'titulo': titulo, 'precio': precio,
            'vel': vel, 'vtas_30': vtas_30, 'ingreso_30': ingreso_30,
            'stock': stock, 'dias_stock': dias_stk,
            'margen_pct': round(float(margen) * 100, 1) if margen else None,
        }

        if ingreso_30 > 0 and len(top_items) < 10:
            top_items.append(entry)

        if vel > 0 and dias_stk is not None and dias_stk <= 14:
            criticos_stock.append(entry)

        if vel == 0 and stock > 0:
            sin_ventas.append({**entry, 'titulo': titulo})

        if visitas >= 200 and conv is not None and float(conv) < 1.5:
            baja_conversion.append({**entry, 'visitas': visitas, 'conv': round(float(conv), 2)})

    # Reputación
    rep_latest = {}
    if isinstance(rep_snap, list) and rep_snap:
        rep_latest = rep_snap[-1]
    elif isinstance(rep_snap, dict):
        hist = rep_snap.get('historial', rep_snap.get('snapshots', []))
        if hist:
            rep_latest = hist[-1]

    reclamos   = rep_latest.get('reclamos_pct', rep_latest.get('claims_rate', None))
    demoras    = rep_latest.get('demoras_pct',  rep_latest.get('delayed_rate', None))
    nivel      = rep_latest.get('nivel', rep_latest.get('level_id', ''))

    # ── 3b. Precio vs competidores en top 5 productos ────────────────────────
    comp_precios = []
    if heads and top_items:
        try:
            _pos_kw = db_load(os.path.join(DATA_DIR, f'posiciones_{safe(alias)}.json')) or {}
            _kw_map = {pid: pd.get('keyword', '') for pid, pd in _pos_kw.items()}
            _stop   = {'de','para','con','sin','y','el','la','los','las','un','una','-','–'}

            def _check_price(it):
                _iid = it.get('id', '')
                _kw  = _kw_map.get(_iid, '')
                if not _kw:
                    _words = [w for w in it.get('titulo', '').lower().split()
                              if w.isalpha() and w not in _stop and len(w) > 2]
                    _kw = ' '.join(_words[:4])
                if not _kw:
                    return None
                _r = req_lib.get('https://api.mercadolibre.com/sites/MLA/search',
                    headers=heads, params={'q': _kw, 'limit': 10}, timeout=8)
                if not _r.ok:
                    return None
                _res = _r.json().get('results', [])
                _others = [float(x['price']) for x in _res
                           if x.get('id') != _iid and float(x.get('price', 0)) > 0][:5]
                if not _others:
                    return None
                _min_p = min(_others)
                _my_p  = float(it.get('precio', 0) or 0)
                if _my_p <= 0:
                    return None
                return {'titulo': it.get('titulo', '')[:50],
                        'mi_precio': round(_my_p),
                        'min_comp':  round(_min_p),
                        'gap_pct':   round((_my_p - _min_p) / _min_p * 100, 1)}

            from concurrent.futures import ThreadPoolExecutor as _TPExec
            with _TPExec(max_workers=3) as _pool:
                _futs = [_pool.submit(_check_price, it) for it in top_items[:5]]
                for _fut in _futs:
                    try:
                        _r2 = _fut.result(timeout=12)
                        if _r2:
                            comp_precios.append(_r2)
                    except Exception:
                        pass
        except Exception:
            pass

    # ── 3b.1 Meli Ads (caché del scheduler) ──────────────────────────────────
    ads_snap = db_load(os.path.join(DATA_DIR, f'ads_summary_{safe(alias)}.json')) or {}

    # ── 3c. Cobertura por categoría ───────────────────────────────────────────
    cat_stats = {}
    for _it in items_raw:
        _cat = _it.get('category_id', '') or 'otras'
        if _cat not in cat_stats:
            cat_stats[_cat] = {'total': 0, 'venden': 0}
        cat_stats[_cat]['total'] += 1
        if float(_it.get('velocidad', 0) or 0) > 0:
            cat_stats[_cat]['venden'] += 1

    cat_trend_lines = []
    for _cat, _st in sorted(cat_stats.items(), key=lambda x: x[1]['total'], reverse=True)[:6]:
        _pct  = round(_st['venden'] / max(_st['total'], 1) * 100)
        _flag = ' ⚠ baja cobertura' if _pct < 40 and _st['total'] >= 3 else ''
        cat_trend_lines.append(
            f"  {_cat}: {_st['venden']}/{_st['total']} publicaciones vendiendo ({_pct}%){_flag}"
        )
    cat_trend_txt = '\n'.join(cat_trend_lines) or '  Sin datos de categoría'

    # ── 4. Construir el prompt ────────────────────────────────────────────────
    top_txt = '\n'.join(
        f"  {i+1}. {it['titulo']} | ${it['ingreso_30']:,}/mes | "
        f"{it['vel']:.1f}u/día | Stock: {it['stock']}u "
        f"({'⚠️ ' + str(it['dias_stock']) + ' días' if it['dias_stock'] and it['dias_stock'] <= 20 else str(it['dias_stock']) + ' días' if it['dias_stock'] else 'sin vel'})"
        f" | Margen: {str(it['margen_pct']) + '%' if it['margen_pct'] else 'sin costo cargado'}"
        for i, it in enumerate(top_items)
    ) or '  Sin datos suficientes'

    crit_txt = '\n'.join(
        f"  - {it['titulo']}: {it['stock']}u, {it['dias_stock']}d de stock, {it['vel']:.1f}u/día"
        for it in criticos_stock[:6]
    ) or '  Ninguno'

    sin_vta_txt = '\n'.join(
        f"  - {it['titulo']}: {it['stock']}u en stock, 0 ventas"
        for it in sin_ventas[:8]
    ) or '  Ninguno'

    conv_txt = '\n'.join(
        f"  - {it['titulo']}: {it['visitas']} visitas/mes pero solo {it['conv']}% conversión"
        for it in baja_conversion[:5]
    ) or '  Ninguno'

    _fmt_m  = lambda v: f'${v:,.0f}' if v is not None else '—'
    _fmt_pct = lambda v: f"{round(float(v) * 100, 1)}%" if v is not None else 'sin dato'

    pos_drops_txt = '\n'.join(
        f"  - {p['titulo']}: cayó de #{p['antes']} a #{p['ahora']} (−{p['caida']} posiciones)"
        for p in pos_drops[:5]
    ) or '  Sin caídas detectadas esta semana'

    delta_7d_str = ''
    if revenue_7d is not None and revenue_prev_7d is not None and revenue_prev_7d > 0:
        _delta = round((revenue_7d - revenue_prev_7d) / revenue_prev_7d * 100, 1)
        _sign  = '▲' if _delta >= 0 else '▼'
        delta_7d_str = f'  {_sign} {abs(_delta)}% vs semana anterior ({_fmt_m(revenue_prev_7d)})'

    # Ads summary text
    if ads_snap and not ads_snap.get('sin_campanas'):
        _bw = ads_snap.get('budget_warnings', [])
        _bw_txt = ''
        if _bw:
            _bw_txt = '\n' + '\n'.join(
                f"  ⚠ Campaña '{w['nombre']}': usó {w['pct']}% del presupuesto diario "
                f"(${w['gastado_hoy']:,} de ${w['presupuesto']:,}) — "
                f"{'se queda sin presupuesto a mitad del día' if w['pct'] >= 90 else 'presupuesto casi agotado'}"
                for w in _bw
            )
        _roas_v = ads_snap.get('roas')
        _acos_v = ads_snap.get('acos')
        ads_context = (
            f"PUBLICIDAD MELI ADS (últimos 7 días — actualizado {ads_snap.get('fecha','?')}):\n"
            f"- Campañas activas: {ads_snap.get('campanas', '—')}\n"
            f"- Inversión: ${ads_snap.get('spend_7d', 0):,}\n"
            f"- Ventas generadas por ads: ${ads_snap.get('revenue_7d', 0):,}\n"
            f"- Conversiones vía ads: {ads_snap.get('conversiones', '—')}\n"
            f"- ROAS: {_roas_v}x (saludable >3x, crítico <2x) "
            f"{'⚠ BAJO' if _roas_v and _roas_v < 2 else ('OK' if _roas_v and _roas_v >= 3 else '')}\n"
            f"- ACoS: {round(_acos_v*100,1) if _acos_v else '—'}% (saludable <15%) "
            f"{'⚠ ALTO' if _acos_v and _acos_v > 0.20 else ''}\n"
            f"- CTR: {ads_snap.get('ctr', '—')}%"
            + _bw_txt
        )
    elif ads_snap.get('sin_campanas'):
        ads_context = 'PUBLICIDAD MELI ADS: Sin campañas activas detectadas.'
    else:
        ads_context = 'PUBLICIDAD MELI ADS: Sin datos (se actualizan en el próximo ciclo del scheduler).'

    comp_txt_lines = []
    for _cp in comp_precios:
        if _cp['gap_pct'] > 5:
            comp_txt_lines.append(
                f"  - {_cp['titulo']}: tu precio ${_cp['mi_precio']:,} | "
                f"más barato en mercado ${_cp['min_comp']:,} → {_cp['gap_pct']}% más caro"
            )
        elif _cp['gap_pct'] < -5:
            comp_txt_lines.append(
                f"  - {_cp['titulo']}: tu precio ${_cp['mi_precio']:,} | "
                f"más barato en mercado ${_cp['min_comp']:,} → podés subir {abs(_cp['gap_pct'])}%"
            )
        else:
            comp_txt_lines.append(
                f"  - {_cp['titulo']}: tu precio ${_cp['mi_precio']:,} | "
                f"más barato en mercado ${_cp['min_comp']:,} → precio competitivo"
            )
    comp_precios_txt = '\n'.join(comp_txt_lines) or '  Sin datos (corré Posiciones primero)'

    total_items  = len(items_raw)
    items_venden = sum(1 for it in items_raw if float(it.get('velocidad', 0) or 0) > 0)

    prompt = f"""Sos un consultor experto en ventas de MercadoLibre Argentina con más de 10 años de experiencia. \
Conocés en profundidad el algoritmo de ML, la gestión de catálogos, Full, posicionamiento y estrategia de precios.

Analizá los datos de esta cuenta y generá un informe práctico, directo y accionable. \
Hablale al vendedor de forma directa (tuteo). Sé específico: mencioná los productos por nombre, \
los números reales, y las acciones concretas. No seas genérico.

═══════════════════════════════════════════════
DATOS DE LA CUENTA — {alias}
Fecha del análisis: {(_dt.utcnow() - _td(hours=3)).strftime('%d/%m/%Y %H:%M')} ART
═══════════════════════════════════════════════

INGRESOS:
- Hoy: {_fmt_m(revenue_hoy)}
- Esta semana (7 días): {_fmt_m(revenue_7d)}{delta_7d_str}
- Semana anterior (7-14 días): {_fmt_m(revenue_prev_7d)}
- Últimos 30 días: {_fmt_m(revenue_30d)}

MÉTRICAS DE VENDEDOR (ML las usa para rankear toda la cuenta):
- Cancelaciones (60d): {_fmt_pct(seller_metrics.get('cancelaciones'))} [límite: 3%]
- Envíos demorados (60d): {_fmt_pct(seller_metrics.get('demoras'))} [límite: 10%]
- Reclamos (60d): {_fmt_pct(seller_metrics.get('reclamos'))} [límite: 2%]
- Preguntas sin responder: {n_unanswered} [ML penaliza vendedores lentos]

POSICIONAMIENTO — CAÍDAS ESTA SEMANA ({len(pos_drops)} publicaciones):
{pos_drops_txt}

COBERTURA POR CATEGORÍA:
{cat_trend_txt}

{ads_context}

PRECIO VS MERCADO (top 5 productos por ingresos):
{comp_precios_txt}

CATÁLOGO:
- Total publicaciones activas: {total_items}
- Publicaciones que venden: {items_venden} ({round(items_venden/max(total_items,1)*100)}%)
- Publicaciones sin ventas con stock: {len(sin_ventas)}

TOP PRODUCTOS POR INGRESOS (30 días):
{top_txt}

ALERTAS DE STOCK (stock crítico ≤14 días):
{crit_txt}

PROBLEMAS DE CONVERSIÓN (visitas altas, ventas bajas):
{conv_txt}

STOCK MUERTO:
{sin_vta_txt}

REPUTACIÓN:
- Nivel: {nivel or 'desconocido'}
- Reclamos: {str(round(float(reclamos)*100 if reclamos and float(reclamos) < 1 else float(reclamos) if reclamos else 0, 1)) + '%' if reclamos is not None else 'sin dato'} (límite ML: 2%)
- Demoras: {str(round(float(demoras)*100 if demoras and float(demoras) < 1 else float(demoras) if demoras else 0, 1)) + '%' if demoras is not None else 'sin dato'}
═══════════════════════════════════════════════

Generá el informe con EXACTAMENTE estas 5 secciones. Usá emojis como se indica. \
Sé concreto, específico y accionable. Priorizá por impacto económico real.

🔍 DIAGNÓSTICO SEMANAL
[Empezá con los números: esta semana vs la anterior en pesos y %. \
Luego identificá la causa raíz cruzando TODAS las señales disponibles: \
(1) métricas de vendedor fuera de límite → penalización de cuenta, \
(2) posiciones caídas → pérdida de visibilidad orgánica, \
(3) ROAS bajo o presupuesto de ads agotado → la publicidad no está rindiendo o se corta a mitad del día, \
(4) precios fuera de mercado → perdés el buy box, \
(5) categorías con baja cobertura → problema de nicho. \
Distinguí si el problema es de TODA la cuenta o de productos específicos. \
Sé directo, nombrá productos y números concretos.]

🚨 URGENTE — HACER HOY
[Máximo 4 acciones. Solo lo que si no se hace HOY tiene consecuencia directa en ventas o reputación. \
Para cada una: producto o métrica específica, problema, acción exacta, impacto estimado en pesos. \
Si el presupuesto de ads se agota antes de las 18hs, es urgente subirlo.]

💡 OPORTUNIDADES ESTA SEMANA
[Máximo 4 acciones de alto impacto con poco esfuerzo. \
Considerá: subir precio donde sos el más barato con margen, mejorar conversión en los de muchas visitas, \
activar Full en candidatos de alto volumen, responder preguntas, optimizar ACoS si está alto.]

📦 PRÓXIMO PEDIDO A CHINA
[Lista priorizada en 3 categorías:
PEDÍ SÍ O SÍ: productos top sellers con stock bajo
REPONER PRONTO: productos que venden bien pero tienen margen de tiempo
NO PIDAS: productos sin movimiento o con stock excesivo]

⚠️ LASTRES Y RIESGOS
[Qué está pesando sin dar retorno. Stock muerto, concentración de riesgo, categorías con baja cobertura. \
Sé directo — si algo hay que cortar, decilo.]"""

    # ── 5. Llamar a Claude ────────────────────────────────────────────────────
    try:
        ai = anthropic.Anthropic()
        msg = ai.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_token_usage('Análisis Experto', 'claude-sonnet-4-6',
                         msg.usage.input_tokens, msg.usage.output_tokens)
        texto = msg.content[0].text.strip()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Error Claude API: {e}'}), 500

    # ── 6. Guardar y devolver ─────────────────────────────────────────────────
    resultado = {
        'alias':     alias,
        'fecha':     (_dt.utcnow() - _td(hours=3)).strftime('%d/%m/%Y %H:%M'),
        'analisis':  texto,
        'revenue':   {'hoy': revenue_hoy, '7d': revenue_7d, '30d': revenue_30d,
                      'prev_7d': revenue_prev_7d},
        'tokens':    msg.usage.input_tokens + msg.usage.output_tokens,
    }
    db_save_fn(os.path.join(DATA_DIR, f'analisis_experto_{safe(alias)}.json'), resultado)
    return jsonify({'ok': True, **resultado})


_app_scheduler = None
_job_manager   = None  # JobManager wrappeando _app_scheduler
_scheduler_last_run = None  # datetime del último run completado
_scheduler_running  = False  # flag para evitar ejecuciones simultáneas

# Restaurar last_run desde disco (sobrevive reinicios)
try:
    _saved_run = db_load(os.path.join(DATA_DIR, '_scheduler_last_run.json')) or {}
    if _saved_run.get('last_run'):
        _scheduler_last_run = datetime.strptime(_saved_run['last_run'], '%Y-%m-%d %H:%M')
        print(f'[scheduler] Último run restaurado desde disco: {_scheduler_last_run}')
except Exception:
    pass

# Start scheduler on every worker — keep workers=1 in Procfile to avoid duplicate runs
_app_scheduler = _start_scheduler()


def _auto_update_if_needed():
    """Corre el scheduler en background si no corrió hoy. Llamado en before_request."""
    global _scheduler_running
    if _scheduler_running:
        return
    today = date.today()
    if _scheduler_last_run and _scheduler_last_run.date() >= today:
        return
    import threading
    _scheduler_running = True
    def _run():
        try:
            _scheduler_run_all_inner()
        finally:
            global _scheduler_running
            _scheduler_running = False
    threading.Thread(target=_run, daemon=True).start()
    print('[scheduler] Auto-run disparado: datos no actualizados hoy')

@app.route('/admin/restart', methods=['POST'])
def admin_restart():
    """Reinicia el proceso del servidor para aplicar cambios de código."""
    import threading
    def _do_restart():
        import time as _t
        _t.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    print('\n  Sistema ML — Interfaz web')
    print('  Abrí en tu navegador: http://localhost:8080\n')
    app.run(debug=False, host='0.0.0.0', port=8080)
