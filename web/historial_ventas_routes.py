"""
Rutas de Historial de ventas (Blueprint), igual que moderaciones y duplicador:
el registro en app.py son dos líneas y el `before_request` global protege estas
rutas como al resto (login o Bearer MCP_API_TOKEN, para los agentes).

  /historial-ventas                      pantalla
  /historial-ventas/api/pendientes       meses que faltan pedir a ML (solo lee)
  /historial-ventas/api/sincronizar      pide UN mes a ML y lo guarda (POST)
  /historial-ventas/api/analisis         análisis completo (solo lee lo guardado)
  /historial-ventas/api/item             detalle de una publicación (solo lee)
"""
import re

from flask import Blueprint, jsonify, render_template, request

from modules import historial_ventas as hv

bp = Blueprint('historial_ventas', __name__, url_prefix='/historial-ventas')


def _client(alias):
    from core.account_manager import AccountManager
    return AccountManager().get_client(alias)


@bp.route('')
def pantalla():
    return render_template('historial_ventas.html')


@bp.route('/api/pendientes')
def api_pendientes():
    alias = request.args.get('alias', '')
    try:
        meses = max(1, min(24, int(request.args.get('meses', hv.MESES_DEFAULT))))
    except ValueError:
        meses = hv.MESES_DEFAULT
    return jsonify({'ok': True, 'meses': hv.meses_a_sincronizar(alias, meses)})


@bp.route('/api/sincronizar', methods=['POST'])
def api_sincronizar():
    body = request.get_json(silent=True) or {}
    alias = body.get('alias') or request.args.get('alias', '')
    mes = body.get('mes') or request.args.get('mes', '')
    if not re.fullmatch(r'\d{4}-\d{2}', mes or ''):
        return jsonify({'ok': False, 'error': 'Mes inválido (AAAA-MM).'}), 400
    try:
        res = hv.sincronizar_mes(_client(alias), alias, mes)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudieron traer las ventas de {mes}: {e}'}), 400
    return jsonify({'ok': True, **res})


@bp.route('/api/analisis')
def api_analisis():
    alias = request.args.get('alias', '')
    try:
        return jsonify({'ok': True, **hv.analizar(alias)})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudo analizar el historial: {e}'}), 400


@bp.route('/api/item')
def api_item():
    alias = request.args.get('alias', '')
    item_id = request.args.get('item_id', '').strip().upper()
    datos = hv.analizar(alias)
    it = next((x for x in datos.get('items', []) if x['item_id'] == item_id), None)
    if not it:
        return jsonify({'ok': False, 'error': 'Esa publicación no tiene ventas guardadas.'}), 404
    return jsonify({'ok': True, 'item': it})
