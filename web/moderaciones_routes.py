"""
Rutas de Prevencion de moderaciones (Blueprint), igual que contabilidad y
duplicador: el registro en app.py son dos lineas y el `before_request` global
protege estas rutas como al resto.

  /moderaciones                      pantalla
  /moderaciones/api/historial        infracciones agrupadas (solo lee)
"""
import re

from flask import Blueprint, jsonify, render_template, request

from modules import moderaciones as mo

bp = Blueprint('moderaciones', __name__, url_prefix='/moderaciones')


@bp.route('')
def pantalla():
    return render_template('moderaciones.html')


@bp.route('/api/historial')
def api_historial():
    from core.account_manager import AccountManager
    alias = request.args.get('alias', '')
    desde = request.args.get('desde') or mo.DESDE_DEFAULT
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', desde):
        return jsonify({'ok': False, 'error': 'Fecha inválida (AAAA-MM-DD).'}), 400
    try:
        client = AccountManager().get_client(alias)
        datos = mo.historial(client, alias, desde, refrescar=request.args.get('refrescar') == '1')
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudo leer el historial de ML: {e}'}), 400
    return jsonify({'ok': True, **datos})
