"""
Rutas del Optimizador IA 2.0 (Blueprint).

  /api/competidores-cors/<alias>/lista   GET  los elegidos de un producto, para el botón
  /api/competidores-cors/<alias>/ficha   POST lo que el botón leyó de cada página
  /optimizar2/api/informe                GET  tu publicación contra cada competidor

Las dos primeras las llama el botón desde mercadolibre.com.ar, sin la sesión del
sistema: van con CORS y se protegen con el token del botón (igual que
/api/capturar-competidor).
"""
from flask import Blueprint, jsonify, make_response, request

from modules import cerebro
from modules import optimizador2 as op2

bp = Blueprint('optimizador2', __name__)


def _cors(resp, status=200):
    resp = make_response(resp, status)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Cerebro-Token'
    return resp


def _app():
    """El app.py ya cargado. En producción gunicorn lo carga como `app` (corre
    desde web/): importar `web.app` cargaría una segunda copia, con otro scheduler."""
    import sys
    return sys.modules.get('app') or sys.modules['web.app']


def _token_ok(alias: str, enviado: str) -> bool:
    return _app()._token_bookmarklet_ok(alias, enviado)


def _item(v) -> str:
    v = (v or '').strip().upper()
    return v if v.startswith('MLA') and v[3:].isdigit() else ''


@bp.route('/api/competidores-cors/<alias>/lista', methods=['GET', 'OPTIONS'])
def lista(alias):
    if request.method == 'OPTIONS':
        return _cors('', 204)
    if not _token_ok(alias, request.headers.get('X-Cerebro-Token')):
        return _cors(jsonify({'ok': False, 'error': 'Token inválido: volvé a instalar el botón'}), 403)
    item_id = _item(request.args.get('item_id'))
    if not item_id:
        return _cors(jsonify({'ok': False, 'error': 'Elegí de cuál publicación tuya son competencia.'}), 400)
    return _cors(jsonify({'ok': True, 'competidores': op2.lista_para_leer(alias, item_id)}))


@bp.route('/api/competidores-cors/<alias>/ficha', methods=['POST', 'OPTIONS'])
def ficha(alias):
    if request.method == 'OPTIONS':
        return _cors('', 204)
    if not _token_ok(alias, request.headers.get('X-Cerebro-Token')):
        return _cors(jsonify({'ok': False, 'error': 'Token inválido: volvé a instalar el botón'}), 403)
    body = request.get_json(silent=True) or {}
    item_id = _item(body.get('item_id'))
    validas = {c['clave'] for c in op2.elegidos(alias, item_id)} if item_id else set()
    guardadas = 0
    for f in (body.get('fichas') or [])[:50]:
        clave = (f or {}).get('clave') if isinstance(f, dict) else None
        # Solo competidores que el usuario eligió para ese producto
        if clave in validas and cerebro.actualizar_ficha(alias, clave, op2.limpiar_ficha(f)):
            guardadas += 1
    return _cors(jsonify({'ok': True, 'guardadas': guardadas}))


@bp.route('/optimizar2/api/informe')
def informe():
    from core.account_manager import AccountManager
    alias = (request.args.get('alias') or '').strip()
    item_id = _item(request.args.get('item_id'))
    if not alias or not item_id:
        return jsonify({'ok': False, 'error': 'Faltan cuenta o publicación.'}), 400
    try:
        mio = op2.mi_publicacion(AccountManager().get_client(alias), item_id)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No pude leer tu publicación: {e}'}), 502
    return jsonify({'ok': True, **op2.informe(mio, op2.elegidos(alias, item_id)),
                    'busquedas': _busquedas(alias, item_id)})


def _busquedas(alias: str, item_id: str) -> list[dict]:
    """Las búsquedas capturadas con el botón: tu puesto y cuántos pagos había."""
    import os
    from modules import grupos_producto as gp
    a = _app()
    data = a.load_json(os.path.join(a.DATA_DIR, f'busquedas_capturadas_{a.safe(alias)}.json')) or {}
    hermanas = set(gp.hermanas_de(alias, item_id)) | {item_id}
    out = [{'busqueda': q, 'fecha': b.get('fecha'), 'resultados': b.get('resultados'),
            'tu_puesto': min((p['posicion'] for p in b.get('mis_posiciones') or []
                              if p.get('id') in hermanas and isinstance(p.get('posicion'), int)), default=None),
            'pagos': len(b.get('pagos') or []),
            'pagos_arriba': len([x for x in b.get('pagos') or [] if isinstance(x, int) and x <= 10])}
           for q, b in (data.get('busquedas') or {}).items()]
    # Solo las búsquedas donde apareció este producto: las demás son de otros
    propias = [b for b in out if b['tu_puesto']]
    return sorted(propias, key=lambda b: b.get('fecha') or '', reverse=True)[:8]
