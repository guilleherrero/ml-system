"""
Rutas de Prevencion de moderaciones (Blueprint), igual que contabilidad y
duplicador: el registro en app.py son dos lineas y el `before_request` global
protege estas rutas como al resto.

  /moderaciones                      pantalla
  /moderaciones/api/historial        infracciones agrupadas (solo lee)
  /moderaciones/api/texto            titulo y ficha de las activas (solo lee)
  /moderaciones/api/texto/descripcion   descripcion de UNA publicacion (solo lee)
  /moderaciones/api/texto/listas     palabras prohibidas / permitidas propias
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


def _client(alias):
    from core.account_manager import AccountManager
    return AccountManager().get_client(alias)


@bp.route('/api/texto')
def api_texto():
    from modules import revision_texto as rt
    alias = request.args.get('alias', '')
    try:
        return jsonify({'ok': True, **rt.revisar_catalogo(_client(alias), alias)})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudieron revisar las publicaciones: {e}'}), 400


@bp.route('/api/texto/descripcion')
def api_texto_descripcion():
    from modules import revision_texto as rt
    alias, item_id = request.args.get('alias', ''), request.args.get('item_id', '').strip().upper()
    if not re.fullmatch(r'MLA\d+', item_id):
        return jsonify({'ok': False, 'error': 'ID de publicación inválido.'}), 400
    try:
        texto = (_client(alias)._get(f'/items/{item_id}/description') or {}).get('plain_text') or ''
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudo leer la descripción: {e}'}), 400
    return jsonify({'ok': True, 'largo': len(texto), 'hallazgos': rt.revisar(texto, 'descripcion', rt.listas(alias))})


@bp.route('/api/texto/listas', methods=['GET', 'POST'])
def api_texto_listas():
    from modules import revision_texto as rt
    if request.method == 'GET':
        return jsonify({'ok': True, **rt.listas(request.args.get('alias', ''))})
    body = request.get_json() or {}
    try:
        d = rt.guardar_listas(body.get('alias', ''), body.get('prohibidas') or [], body.get('permitidas') or [])
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudieron guardar las listas: {e}'}), 400
    return jsonify({'ok': True, **d})
