"""
Rutas del Importador de publicaciones (Blueprint). Igual que los otros
modulos: el registro en app.py son dos lineas y el `before_request` global
protege todas estas rutas (incluida la de recibir: corre con la sesion).

  /importar                    pantalla + instalador del boton
  /importar/recibir            ventanita que abre el boton y guarda la captura
  /importar/api/capturar       guarda lo que mando el boton (sesion, mismo origen)
  /importar/api/lista          importaciones de la cuenta
  /importar/api/preparar       categoria sugerida, campos de ficha, tasas, catalogo
  /importar/api/guardar        guarda la edicion
  /importar/api/evaluar        ganancia / margen de un precio (no guarda)
  /importar/api/descartar
  /importar/api/publicar       crea en ML (exige confirmado)
"""
import json
import os
from urllib.parse import quote

from flask import Blueprint, jsonify, render_template, request, session

from modules import duplicador as dup
from modules import importador as imp

bp = Blueprint('importador', __name__, url_prefix='/importar')


def _usuario():
    return session.get('username') or ''


def _client(alias):
    from core.account_manager import AccountManager
    if not alias:
        raise imp.ImportadorError('Falta elegir la cuenta.')
    try:
        return AccountManager().get_client(alias)
    except Exception as e:
        raise imp.ImportadorError(f'No se pudo conectar la cuenta {alias}: {e}')


def _error(e, code=400):
    return jsonify({'ok': False, 'error': str(e)}), code


def _boton(alias: str) -> str:
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'js', 'bookmarklet_importar.js')
    with open(ruta, encoding='utf-8') as f:
        codigo = f.read()
    cfg = json.dumps({'base': request.url_root.rstrip('/'), 'alias': alias})
    return 'javascript:' + quote(f'(function(){{window.__MLSYS_IMPORTAR__={cfg};{codigo}}})();', safe='')


@bp.route('')
def pantalla():
    alias = request.args.get('alias') or request.cookies.get('ml_cuenta') or ''
    return render_template('importar.html', boton=_boton(alias) if alias else '', alias_boton=alias)


@bp.route('/recibir')
def recibir():
    return render_template('importar_recibir.html', alias=request.args.get('alias', ''))


@bp.route('/api/capturar', methods=['POST'])
def api_capturar():
    body = request.get_json() or {}
    try:
        b = imp.capturar(body.get('alias', ''), body.get('datos') or {}, _usuario())
    except imp.ImportadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'id': b['id'], 'titulo': b['origen']['titulo']})


@bp.route('/api/lista')
def api_lista():
    return jsonify({'ok': True, 'importaciones': imp.listar(request.args.get('alias', ''))})


def _tasas(client, category_id: str, precio: float) -> dict:
    """Comision y costo de cuotas reales por escalon, por categoria (verificado
    2026-10-18: con category_id da lo mismo que con domain_id, 16 / 13,4 / 5 en
    Cortadoras de Pelo). /listing_prices omite escalones a veces: se reintenta."""
    from modules.trio_generador import CUOTAS_A_TAGS
    tasas = {}
    if not category_id:
        return tasas
    for _ in range(3):
        for esc, (lt, tags) in CUOTAS_A_TAGS.items():
            if (tasas.get(esc) or {}).get('comision') is not None:
                continue
            params = {'price': max(precio or 0, 100000), 'listing_type_id': lt, 'category_id': category_id}
            if tags:
                params['tags'] = tags[0]
            try:
                r = client._get('/sites/MLA/listing_prices', params=params)
                d = r[0] if isinstance(r, list) and r else (r if isinstance(r, dict) else {})
                sfd = d.get('sale_fee_details') or {}
                if sfd.get('meli_percentage_fee') is not None:
                    tasas[esc] = {'comision': sfd.get('meli_percentage_fee'), 'costo_cuotas': sfd.get('financing_add_on_fee') or 0}
            except Exception:
                pass
        if all(e in tasas for e in imp.ESCALONES):
            break
    return tasas


def _nombre_categoria(client):
    def f(cat):
        try:
            return (client._get(f'/categories/{cat}') or {}).get('name')
        except Exception:
            return None
    return f


def _contexto(client, alias, b, category_id=None):
    from web.duplicador_routes import _cargos
    sug = imp.sugerencias_categoria(b, _nombre_categoria(client))
    cat = category_id or b['edicion'].get('category_id') or (sug[0]['id'] if sug else None)
    ml_primera = next((s['id'] for s in sug if s.get('origen') == 'ml'), None)
    difiere = bool(b['origen'].get('categoria_pagina') and ml_primera and ml_primera != b['origen']['categoria_pagina'])
    campos = imp.atributos_categoria(client, cat) if cat else []
    try:
        info_cat = client._get(f'/categories/{cat}') if cat else {}
    except Exception:
        info_cat = {}
    tasas = _tasas(client, cat, b['origen']['precio'])
    cargos, _ = _cargos(alias, None)
    catalogo = None
    if b['origen'].get('catalog_product_id'):
        try:
            p = client._get(f"/products/{b['origen']['catalog_product_id']}") or {}
            catalogo = {'id': p.get('id'), 'nombre': p.get('name'), 'estado': p.get('status'),
                        'fotos': [x.get('url') for x in p.get('pictures') or []][:6],
                        'ficha': [{'nombre': a.get('name'), 'valor': a.get('value_name')}
                                  for a in p.get('attributes') or [] if a.get('value_name')][:30]}
        except Exception as e:
            catalogo = {'error': f'No se pudo leer el producto de catálogo: {e}'}
    me = client.get_me() or {}
    return {'sugeridas': sug, 'category_id': cat, 'categorias_difieren': difiere, 'categoria_nombre': (info_cat or {}).get('name'),
            'max_fotos': ((info_cat or {}).get('settings') or {}).get('max_pictures_per_item'),
            'campos': campos, 'precarga': imp.precargar_atributos(campos, b['origen']['specs']),
            'referencia_identidad': imp.referencia_identidad(campos, b['origen']['specs']),
            'tasas': tasas, 'cargos': cargos, 'catalogo': catalogo,
            'es_up': 'user_product_seller' in (me.get('tags') or [])}


def _propios(client):
    ids, offset = [], 0
    while True:
        d = client.get_my_listings(limit=50, offset=offset, status='active') or {}
        ids += d.get('results') or []
        offset += 50
        if offset >= (d.get('paging') or {}).get('total', 0):
            break
    out = []
    for k in range(0, len(ids), 20):
        for e in client._get('/items', params={'ids': ','.join(ids[k:k + 20]), 'attributes': 'id,title,category_id'}) or []:
            if e.get('code') == 200:
                out.append(e['body'])
    return out


@bp.route('/api/preparar')
def api_preparar():
    alias, bid = request.args.get('alias', ''), request.args.get('id', '')
    try:
        b = imp.obtener(alias, bid)
        client = _client(alias)
        ctx = _contexto(client, alias, b, request.args.get('category_id'))
        dups = imp.posibles_duplicados(_propios(client), b['edicion']['family_name'], ctx['category_id']) if ctx['category_id'] else []
    except imp.ImportadorError as e:
        return _error(e)
    except Exception as e:
        return _error(f'No se pudo preparar la importación: {e}')
    ctx['tasas'] = {str(k): v for k, v in ctx['tasas'].items()}
    ctx['cargos'] = ctx['cargos'].__dict__
    return jsonify({'ok': True, 'importacion': b, 'contexto': ctx, 'posibles_duplicados': dups,
                    'margen_minimo_pct': dup.MARGEN_MINIMO * 100})


@bp.route('/api/guardar', methods=['POST'])
def api_guardar():
    body = request.get_json() or {}
    try:
        b = imp.guardar_edicion(body.get('alias', ''), body.get('id', ''), body.get('edicion') or {})
    except imp.ImportadorError as e:
        return _error(e)
    avisos = rt_avisos(b)
    return jsonify({'ok': True, 'importacion': b, 'avisos_texto': avisos})


def rt_avisos(b):
    from modules import revision_texto as rt
    e = b['edicion']
    return rt.revisar(e['family_name'], 'titulo') + rt.revisar(e['descripcion'], 'descripcion')


@bp.route('/api/evaluar', methods=['POST'])
def api_evaluar():
    """Ganancia, margen y ROI de un precio, con las comisiones reales."""
    from web.duplicador_routes import _cargos
    body = request.get_json() or {}
    alias = body.get('alias', '')
    try:
        b = imp.obtener(alias, body.get('id', ''))
        precio, costo, esc = float(body.get('precio') or 0), body.get('costo'), int(body.get('escalon') or 0)
        if costo in (None, '') or float(costo) <= 0:
            raise imp.ImportadorError('Falta el costo: sin costo no se calcula el margen.')
        client = _client(alias)
        cat = body.get('category_id') or b['edicion'].get('category_id') or b['origen'].get('categoria_pagina')
        if not cat:
            raise imp.ImportadorError('Elegí la categoría primero: las comisiones dependen de ella.')
        tasas = _tasas(client, cat, b['origen']['precio'])
        cargos, _ = _cargos(alias, None)
        ev = dup.evaluar_precio(precio, esc, tasas, cargos, float(costo))
        roi = body.get('roi_objetivo')
        sugerido = None
        if roi not in (None, ''):
            from modules import precio_motor as pm
            pub = dup._publicacion(esc, tasas)
            sugerido = pm.redondear_arriba(pm.precio_objetivo(pub, cargos, float(costo),
                                           pm.Objetivo('roi', float(roi), float(roi))), cargos)
    except (imp.ImportadorError, dup.DuplicadorError, ValueError) as e:
        return _error(e)
    return jsonify({'ok': True, 'evaluacion': ev, 'precio_sugerido': sugerido})


@bp.route('/api/descartar', methods=['POST'])
def api_descartar():
    body = request.get_json() or {}
    try:
        b = imp.descartar(body.get('alias', ''), body.get('id', ''))
    except imp.ImportadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'importacion': b})


@bp.route('/api/publicar', methods=['POST'])
def api_publicar():
    """Crea la publicacion en ML. Valida todo antes de subir o crear nada."""
    from web.duplicador_routes import _cargos
    if request.form.get('confirmado') != 'true':
        return _error('Falta la confirmación explícita.')
    alias, bid, modo = request.form.get('alias', ''), request.form.get('id', ''), request.form.get('modo', 'propia')
    try:
        imp.guardar_edicion(alias, bid, json.loads(request.form.get('edicion') or '{}'))
        b = imp.obtener(alias, bid)
        if b['estado'] != 'pendiente':
            raise imp.ImportadorError(f'Ya está {b["estado"]}: no se publica dos veces.')
        client = _client(alias)
        ctx = _contexto(client, alias, b)
        e = b['edicion']
        cargos, _ = _cargos(alias, None)
        ev = None
        if e.get('precio') and e.get('costo'):
            ev = dup.evaluar_precio(e['precio'], e['escalon'], ctx['tasas'], cargos, e['costo'])

        if modo == 'catalogo':
            if not b['origen'].get('catalog_product_id'):
                raise imp.ImportadorError('Esta página no era de catálogo: no se puede publicar en catálogo.')
            if request.form.get('confirmo_catalogo') != 'true':
                raise imp.ImportadorError('Tenés que confirmar que tu producto es exactamente el del catálogo.')
            faltas = []
            if not e.get('precio'):
                faltas.append('Falta el precio.')
            if not e.get('costo'):
                faltas.append('Falta el costo: sin costo no se puede verificar el margen.')
            elif ev and not ev['cumple_piso']:
                faltas.append(f'A ese precio el margen queda en {ev["margen_pct"]}%, debajo del piso.')
            if faltas:
                raise imp.ImportadorError(' '.join(faltas))
            payload = imp.payload_catalogo(b, ctx['category_id'])
            foto_ids = []
        else:
            nuevas = []
            for n in range(int(request.form.get('n_fotos') or 0)):
                f = request.files.get(f'f{n}')
                if f:
                    nuevas.append({'nombre': f.filename or f'foto{n}.jpg', 'mime': (f.mimetype or '').lower(), 'contenido': f.read()})
            faltas = imp.validar_propia(b, ctx['campos'], nuevas, ev)
            dups = imp.posibles_duplicados(_propios(client), e['family_name'], e['category_id'])
            if dups and request.form.get('ack_duplicados') != 'true':
                faltas.append('Hay publicaciones tuyas muy parecidas en la misma categoría: confirmá que es otro producto.')
            if ctx['max_fotos'] and len(nuevas) > ctx['max_fotos']:
                faltas.append(f'La categoría admite hasta {ctx["max_fotos"]} fotos.')
            for n in nuevas:
                if n['mime'] not in dup.FORMATOS_FOTO:
                    faltas.append(f'{n["nombre"]}: ML acepta JPG o PNG.')
                elif len(n['contenido']) > dup.MAX_BYTES_FOTO:
                    faltas.append(f'{n["nombre"]}: pesa más de 10 MB.')
            if faltas:
                raise imp.ImportadorError(' · '.join(faltas))
            for pos, n in enumerate(nuevas):
                diag = dup.diagnosticar(client, e['category_id'], e['family_name'], contenido=n['contenido'],
                                        mime=n['mime'], principal=(pos == 0))
                if diag['problemas']:
                    raise imp.ImportadorError(f'Foto {pos + 1} ({n["nombre"]}): ' + ' '.join(diag['problemas']) + ' No se publicó nada.')
            foto_ids = [dup._subir_foto(client, n['contenido'], n['nombre'], n['mime']) for n in nuevas]
            payload = imp.payload_propia(b, ctx['es_up'], foto_ids)

        try:
            creado = client._post('/items', payload)
        except Exception as ex:
            raise imp.ImportadorError(f'ML rechazó la publicación: {ex}')
        nuevo = (creado or {}).get('id')
        if not nuevo:
            raise imp.ImportadorError(f'ML no devolvió el id de la publicación. Respuesta: {creado}')
        avisos = []
        if modo != 'catalogo' and e['descripcion'].strip():
            try:
                client._post(f'/items/{nuevo}/description', {'plain_text': e['descripcion'].strip()})
            except Exception as ex:
                avisos.append(f'No se pudo cargar la descripción ({ex}). Cargala a mano en {nuevo}.')
        elif modo != 'catalogo':
            avisos.append('Se publicó sin descripción.')
        try:
            leido = client.get_item(nuevo) or {}
            if float(leido.get('price') or 0) != float(e['precio']):
                avisos.append(f'ML muestra precio ${leido.get("price")} en vez de ${e["precio"]}.')
            if leido.get('status') != 'active':
                avisos.append(f'La publicación quedó en estado "{leido.get("status")}" {leido.get("sub_status") or ""}: revisala en ML.')
        except Exception as ex:
            avisos.append(f'Se creó pero no se pudo releer ({ex}).')
        b = imp.marcar_publicado(alias, bid, nuevo, modo, avisos, _usuario())
    except imp.ImportadorError as e:
        return _error(e)
    except dup.DuplicadorError as e:
        return _error(e)
    return jsonify({'ok': True, 'importacion': b})
