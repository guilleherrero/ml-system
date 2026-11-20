"""
Calculadora — precio de venta sugerido a partir del costo y el margen deseado.

Todos los cargos salen EN VIVO de MercadoLibre para la categoría elegida
(verificado 2026-11-20 contra /sites/MLA/listing_prices y
/users/{id}/shipping_options/free):
  - comisión y costo de cuotas por modalidad (Clásica, interés bajo, Premium 3/6/9/12)
  - cargo fijo por tramo de precio (los tramos se detectan solos: si ML los mueve, se ven)
  - envío según peso/medidas para Mercado Envíos, Flex y Full
  - impuestos: los de la cuenta (misma config que el resto del sistema)
El cálculo del precio corre en el navegador para que la ruedita responda al instante.
"""
import time

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint('calculadora', __name__, url_prefix='/calculadora')

ESCALONES = [  # (clave, nombre, listing_type, tag)
    ('0', 'Clásica sin cuotas', 'gold_special', None),
    ('4', 'Clásica cuotas interés bajo', 'gold_special', 'pcj-co-funded'),
    ('3', 'Premium 3 cuotas', 'gold_pro', '3x_campaign'),
    ('6', 'Premium 6 cuotas', 'gold_pro', None),
    ('9', 'Premium 9 cuotas', 'gold_pro', '9x_campaign'),
    ('12', 'Premium 12 cuotas', 'gold_pro', '12x_campaign'),
]
LOGISTICAS = [('drop_off', 'Mercado Envíos'), ('self_service', 'Flex'), ('fulfillment', 'Full')]
_CACHE = {}  # ponytail: cache en memoria por worker (workers=1); a Redis si se escala


def _cache(clave, horas, fn):
    v = _CACHE.get(clave)
    if v and time.time() - v[0] < horas * 3600:
        return v[1]
    r = fn()
    _CACHE[clave] = (time.time(), r)
    return r


def _alias():
    from core.account_manager import AccountManager
    aliases = [a.alias for a in AccountManager().list_accounts() if getattr(a, 'alias', '')]
    a = request.args.get('alias') or request.cookies.get('ml_cuenta') or ''
    return a if a in aliases else (aliases[0] if aliases else '')


def _client(alias):
    from core.account_manager import AccountManager
    c = AccountManager().get_client(alias)
    c._ensure_token()
    return c


def _fee(client, category_id, precio, lt='gold_special', tag=None):
    p = {'price': precio, 'listing_type_id': lt, 'category_id': category_id}
    if tag:
        p['tags'] = tag
    for _ in range(3):  # listing_prices falla intermitente (ver importador_routes._tasas)
        try:
            r = client._get('/sites/MLA/listing_prices', params=p)
            d = r[0] if isinstance(r, list) and r else r
            if (d or {}).get('sale_fee_details'):
                return d['sale_fee_details']
        except Exception:
            pass
    return None


def tramos(valor_en, hasta, paso, pool):
    """Cortes de una tarifa escalonada valor_en(precio) -> [{desde, valor}].
    Grilla en paralelo cada `paso` y bisección dentro de cada salto."""
    grilla = [1] + list(range(paso, hasta + 1, paso))
    vals = dict(zip(grilla, pool.map(valor_en, grilla)))

    def corte(a, b):
        fa = vals[a]
        while b - a > 1:
            m = (a + b) // 2
            if valor_en(m) == fa:
                a = m
            else:
                b = m
        return b

    # ponytail: asume como mucho un corte por tramo de grilla; achicar `paso` si ML agrega tramos finos
    saltos = [(a, b) for a, b in zip(grilla, grilla[1:]) if vals[a] != vals[b]]
    cortes = list(pool.map(lambda ab: corte(*ab), saltos))
    return [{'desde': 0, 'valor': vals[1]}] + [{'desde': c, 'valor': vals[b]} for c, (_, b) in zip(cortes, saltos)]


@bp.route('/')
def pagina():
    return render_template('calculadora.html', alias=_alias())


@bp.route('/api/categorias')
def api_categorias():
    import requests
    q = (request.args.get('q') or '').strip()
    if len(q) < 3:
        return jsonify({'ok': True, 'categorias': []})
    r = requests.get('https://api.mercadolibre.com/sites/MLA/domain_discovery/search',
                     params={'q': q, 'limit': 8}, timeout=8)
    vistos, out = set(), []
    for d in (r.json() if r.ok else []):
        if d.get('category_id') not in vistos:
            vistos.add(d.get('category_id'))
            out.append({'id': d.get('category_id'), 'nombre': f"{d.get('category_name')} ({d.get('domain_name')})"})
    return jsonify({'ok': True, 'categorias': out})


@bp.route('/api/datos')
def api_datos():
    from web.duplicador_routes import _cargos
    alias = _alias()
    cat = request.args.get('category_id', '')
    try:
        peso = int(float(request.args.get('peso') or 500))
        dims = [int(float(request.args.get(k) or 10)) for k in ('largo', 'ancho', 'alto')]
    except ValueError:
        return jsonify({'ok': False, 'error': 'Peso y medidas tienen que ser números.'}), 400
    if not cat.startswith('MLA') or not (1 <= peso <= 100000) or not all(1 <= d <= 300 for d in dims):
        return jsonify({'ok': False, 'error': 'Elegí una categoría y cargá peso (g) y medidas (cm) válidos.'}), 400
    try:
        client = _client(alias)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudo conectar la cuenta {alias}: {e}'}), 502

    from concurrent.futures import ThreadPoolExecutor
    dim = f"{dims[0]}x{dims[1]}x{dims[2]},{peso}"

    def envio_en(lt):
        def f(precio):
            try:
                r = client._get(f'/users/{client.account.user_id}/shipping_options/free', params={
                    'dimensions': dim, 'item_price': precio, 'listing_type_id': 'gold_special',
                    'mode': 'me2', 'condition': 'new', 'logistic_type': lt})
                return ((r.get('coverage') or {}).get('all_country') or {}).get('list_cost')
            except Exception:
                return None
        return f

    with ThreadPoolExecutor(10) as pool:
        def modalidades():
            ds = pool.map(lambda e: _fee(client, cat, 100000, e[2], e[3]), ESCALONES)  # sin cargo fijo
            return [{'clave': k, 'nombre': n, 'comision': d.get('meli_percentage_fee'), 'cuotas': d.get('financing_add_on_fee') or 0}
                    for (k, n, _, _), d in zip(ESCALONES, ds) if d]

        mods = _cache(('mod', cat), 12, modalidades)
        fijos = _cache(('fijo', cat), 24, lambda: tramos(lambda p: (_fee(client, cat, p) or {}).get('fixed_fee'), 60000, 2500, pool))
        if not mods or any(t['valor'] is None for t in fijos):
            _CACHE.pop(('mod', cat), None), _CACHE.pop(('fijo', cat), None)
            return jsonify({'ok': False, 'error': 'ML no devolvió las comisiones de esta categoría. Probá de nuevo en un rato.'}), 502
        envios = []
        for lt, nombre in LOGISTICAS:
            t = _cache(('env', alias, dim, lt), 12, lambda: tramos(envio_en(lt), 100000, 5000, pool))
            if any(x['valor'] is None for x in t):
                _CACHE.pop(('env', alias, dim, lt), None)
                t = []
            envios.append({'clave': lt, 'nombre': nombre, 'tramos': t})
    umbral = next((t['desde'] for t in fijos if t['valor'] == 0 and t['desde'] > 0), 33000)
    c, origen = _cargos(alias, None)
    return jsonify({'ok': True, 'modalidades': mods, 'tramos_fijo': fijos, 'umbral': umbral,
                    'envios': envios,
                    'iibb': c.iibb, 'percepcion_iva': c.percepcion_iva, 'envio_bajo_umbral': c.envio_bajo_umbral,
                    'origen_impuestos': origen})
