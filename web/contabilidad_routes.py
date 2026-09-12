"""
Rutas del sistema contable (Blueprint).

Va en un archivo aparte y no dentro de web/app.py a propósito: app.py ya tiene
20.000 líneas, y el registro del blueprint son dos líneas allá. El
`before_request` global de la app protege estas rutas igual que al resto.

Pantallas:
  /contabilidad                → dashboard: resultado del período y por mes
  /contabilidad/movimientos    → el libro, con filtros
  /contabilidad/pendientes     → bandeja de lo que no se pudo clasificar
  /contabilidad/gastos         → alta manual (proveedores, impuestos, otros)
  /contabilidad/costos         → carga masiva de costos de mercadería
  /contabilidad/importar       → disparar importaciones y ver su historial
"""
import threading
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for,
)

from modules import contabilidad as cont
from modules import contabilidad_cierre as cierre

bp = Blueprint('contabilidad', __name__, url_prefix='/contabilidad')

# Estado en memoria de las importaciones en curso, por cuenta. Solo para que la
# UI sepa que hay algo corriendo; el dato duro y persistente está en
# cont_import_runs.
_importando = {}
_lock = threading.Lock()


@bp.app_template_filter('pesos')
def _filtro_pesos(valor, decimales=0):
    """
    Formato argentino: separador de miles con punto y decimal con coma.
    El signo se muestra explícito en los negativos porque en una pantalla
    contable confundir un ingreso con un egreso es el peor error posible.
    """
    try:
        num = float(valor or 0)
    except (TypeError, ValueError):
        return '—'
    txt = f'{abs(num):,.{decimales}f}'
    txt = txt.replace(',', '@').replace('.', ',').replace('@', '.')
    return f'-${txt}' if num < 0 else f'${txt}'


@bp.app_template_filter('fecha_corta')
def _filtro_fecha_corta(valor):
    if not valor:
        return '—'
    try:
        return datetime.fromisoformat(str(valor)).strftime('%d/%m/%y')
    except ValueError:
        return str(valor)[:10]


@bp.app_template_filter('fecha_hora')
def _filtro_fecha_hora(valor):
    if not valor:
        return '—'
    try:
        return datetime.fromisoformat(str(valor)).strftime('%d/%m %H:%M')
    except ValueError:
        return str(valor)[:16]


def _accounts():
    """Cuentas ML para la sidebar. Import diferido: app.py importa este módulo."""
    try:
        from web.app import get_accounts
        return get_accounts()
    except Exception:
        return []


def _fecha(valor, default):
    if not valor:
        return default
    try:
        return datetime.strptime(str(valor).strip(), '%Y-%m-%d').date()
    except ValueError:
        return default


def _rango_pedido():
    """
    Rango del request. Por defecto: 1 de enero del año en curso hasta hoy, que
    es la pregunta que originó todo esto.
    """
    hoy = date.today()
    desde = _fecha(request.args.get('desde'), date(hoy.year, 1, 1))
    hasta = _fecha(request.args.get('hasta'), hoy)
    if hasta < desde:
        desde, hasta = hasta, desde
    return desde, hasta


def _ctx_base():
    return {
        'accounts': _accounts(),
        'cuentas_libro': cont.listar_cuentas(),
        'rubros': cont.listar_rubros(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/')
def dashboard():
    desde, hasta = _rango_pedido()
    cuenta = request.args.get('cuenta') or None

    resumen = cont.resumen(desde, hasta, cuenta)
    por_cuenta = []
    cuentas = cont.listar_cuentas()
    if len(cuentas) > 1 and not cuenta:
        for alias in cuentas:
            r = cont.resumen(desde, hasta, alias)
            por_cuenta.append({
                'cuenta': alias,
                'ingresos': r['totales']['ingresos'],
                'resultado': r['totales']['resultado'],
                'margen_pct': r['margen_pct'],
            })

    faltan_costos = cierre.items_sin_costo(cuenta, desde)

    return render_template(
        'contabilidad.html',
        resumen=resumen,
        desde=desde.isoformat(),
        hasta=hasta.isoformat(),
        cuenta_sel=cuenta or '',
        por_cuenta=por_cuenta,
        faltan_costos=faltan_costos[:10],
        cant_faltan_costos=len(faltan_costos),
        pendientes=len(cont.pendientes(cuenta, limite=500)),
        **_ctx_base(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# LIBRO DE MOVIMIENTOS
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/movimientos')
def movimientos():
    desde, hasta = _rango_pedido()
    try:
        pagina = int(request.args.get('pagina', 1))
    except ValueError:
        pagina = 1

    datos = cont.listar_movimientos(
        desde=desde, hasta=hasta,
        cuenta_alias=request.args.get('cuenta') or None,
        rubro_codigo=request.args.get('rubro') or None,
        origen=request.args.get('origen') or None,
        texto=request.args.get('q') or None,
        ambito=request.args.get('ambito') or None,
        solo_computables=request.args.get('computables') == '1',
        pagina=pagina,
    )

    return render_template(
        'contabilidad_movimientos.html',
        datos=datos,
        desde=desde.isoformat(),
        hasta=hasta.isoformat(),
        filtros={
            'cuenta': request.args.get('cuenta', ''),
            'rubro': request.args.get('rubro', ''),
            'origen': request.args.get('origen', ''),
            'q': request.args.get('q', ''),
            'ambito': request.args.get('ambito', ''),
            'computables': request.args.get('computables', ''),
        },
        **_ctx_base(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# BANDEJA DE PENDIENTES
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/pendientes')
def pendientes():
    cuenta = request.args.get('cuenta') or None
    filas = cont.pendientes(cuenta, limite=300)
    return render_template(
        'contabilidad_pendientes.html',
        filas=filas,
        cuenta_sel=cuenta or '',
        **_ctx_base(),
    )


@bp.route('/api/asignar-rubro', methods=['POST'])
def api_asignar_rubro():
    data = request.get_json(silent=True) or request.form
    try:
        mov_id = int(data.get('movimiento_id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'movimiento_id inválido'}), 400
    rubro = (data.get('rubro') or '').strip()
    crear_regla = str(data.get('crear_regla', '')).lower() in ('1', 'true', 'on', 'si')
    try:
        res = cont.asignar_rubro(mov_id, rubro, crear_regla=crear_regla)
        return jsonify(res)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500


@bp.route('/api/reclasificar', methods=['POST'])
def api_reclasificar():
    """Vuelve a pasar las reglas sobre lo que quedó sin clasificar."""
    cuenta = (request.form.get('cuenta') or None)
    try:
        res = cont.reclasificar_pendientes(cuenta)
        flash(f'Reclasificados {res["reclasificados"]} de {res["revisados"]} '
              f'movimientos revisados.', 'success')
    except Exception as e:
        flash(f'Error al reclasificar: {e}', 'danger')
    return redirect(url_for('contabilidad.pendientes', cuenta=cuenta or ''))


# ══════════════════════════════════════════════════════════════════════════════
# GASTOS MANUALES
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/gastos', methods=['GET', 'POST'])
def gastos():
    if request.method == 'POST':
        f = request.form
        try:
            monto = (f.get('monto') or '').strip().replace('$', '').replace(' ', '')
            if ',' in monto and '.' in monto:
                monto = (monto.replace('.', '').replace(',', '.')
                         if monto.rfind(',') > monto.rfind('.')
                         else monto.replace(',', ''))
            elif ',' in monto:
                monto = monto.replace(',', '.')

            cont.registrar_gasto_manual(
                cuenta_alias=(f.get('cuenta') or 'manual').strip(),
                fecha=_fecha(f.get('fecha'), date.today()),
                rubro_codigo=(f.get('rubro') or '').strip(),
                concepto=(f.get('concepto') or '').strip(),
                monto=monto,
                proveedor=(f.get('proveedor') or '').strip() or None,
                tipo_comprobante=(f.get('tipo_comprobante') or '').strip() or None,
                nro_comprobante=(f.get('nro_comprobante') or '').strip() or None,
                medio_pago=(f.get('medio_pago') or '').strip() or None,
                iva=(f.get('iva') or '').strip() or None,
                notas=(f.get('notas') or '').strip() or None,
            )
            flash('Movimiento cargado.', 'success')
            return redirect(url_for('contabilidad.gastos'))
        except Exception as e:
            flash(f'No se pudo cargar: {e}', 'danger')

    hoy = date.today()
    datos = cont.listar_movimientos(
        desde=date(hoy.year, 1, 1), hasta=hoy,
        origen='manual', por_pagina=60,
    )
    return render_template(
        'contabilidad_gastos.html',
        datos=datos,
        hoy=hoy.isoformat(),
        **_ctx_base(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# CARGA MASIVA DE COSTOS
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/costos', methods=['GET', 'POST'])
def costos():
    resultado = None
    if request.method == 'POST':
        contenido = request.form.get('pegado') or ''
        archivo = request.files.get('archivo')
        if archivo and archivo.filename:
            try:
                contenido = archivo.read().decode('utf-8-sig', errors='replace')
            except Exception as e:
                flash(f'No se pudo leer el archivo: {e}', 'danger')
                contenido = ''
        vigente = _fecha(request.form.get('vigente_desde'),
                         date.today().replace(day=1))
        if contenido.strip():
            try:
                resultado = cierre.cargar_costos_texto(
                    contenido, vigente_desde_default=vigente,
                    origen='excel' if archivo and archivo.filename else 'manual',
                )
                msg = (f'{resultado["cargados"]} costos nuevos, '
                       f'{resultado["actualizados"]} actualizados')
                if resultado['rechazados']:
                    msg += f', {len(resultado["rechazados"])} rechazados'
                flash(msg, 'warning' if resultado['rechazados'] else 'success')

                # Recalcula el CMV del año con los costos nuevos
                hoy = date.today()
                res_cmv = cierre.aplicar_cmv(date(hoy.year, 1, 1), hoy)
                resultado['cmv'] = res_cmv
            except Exception as e:
                flash(f'Error procesando los costos: {e}', 'danger')
        else:
            flash('No pegaste ni subiste nada.', 'warning')

    faltantes = cierre.items_sin_costo()
    return render_template(
        'contabilidad_costos.html',
        resultado=resultado,
        faltantes=faltantes,
        mes_actual=date.today().replace(day=1).isoformat(),
        **_ctx_base(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# IMPORTACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def _correr_importacion(alias_ml, desde, hasta, alias_mp, fuentes):
    """Corre la importación en un thread y libera el flag al terminar."""
    from modules import contabilidad_import as imp
    clave = alias_ml
    try:
        imp.importar_todo(alias_ml, desde, hasta,
                          alias_mp=alias_mp, fuentes=fuentes)
    except Exception:
        pass
    finally:
        with _lock:
            _importando.pop(clave, None)


@bp.route('/importar', methods=['GET', 'POST'])
def importar():
    if request.method == 'POST':
        f = request.form
        alias_ml = (f.get('alias_ml') or '').strip()
        alias_mp = (f.get('alias_mp') or '').strip() or None
        hoy = date.today()
        desde = _fecha(f.get('desde'), date(hoy.year, 1, 1))
        hasta = _fecha(f.get('hasta'), hoy)
        fuentes = tuple(f.getlist('fuentes')) or ('ordenes', 'billing',
                                                  'percepciones', 'mp')

        if not alias_ml:
            flash('Elegí una cuenta de MercadoLibre.', 'warning')
            return redirect(url_for('contabilidad.importar'))

        with _lock:
            if alias_ml in _importando:
                flash(f'Ya hay una importación corriendo para {alias_ml}.',
                      'warning')
                return redirect(url_for('contabilidad.importar'))
            _importando[alias_ml] = datetime.now().isoformat()

        threading.Thread(
            target=_correr_importacion,
            args=(alias_ml, desde, hasta, alias_mp, fuentes),
            daemon=True,
        ).start()

        dias = (hasta - desde).days + 1
        flash(f'Importación de {alias_ml} arrancada para {dias} días. '
              f'Corre en segundo plano — el historial de abajo se actualiza solo.',
              'success')
        return redirect(url_for('contabilidad.importar'))

    hoy = date.today()
    with _lock:
        corriendo = dict(_importando)

    return render_template(
        'contabilidad_importar.html',
        corridas=cont.ultimas_importaciones(25),
        corriendo=corriendo,
        cuentas_mp=cont.listar_cuentas_mp(),
        desde_default=date(hoy.year, 1, 1).isoformat(),
        hasta_default=hoy.isoformat(),
        **_ctx_base(),
    )


@bp.route('/api/importaciones')
def api_importaciones():
    """Para que la pantalla de importar refresque sin recargar."""
    with _lock:
        corriendo = dict(_importando)
    return jsonify({'corridas': cont.ultimas_importaciones(25),
                    'corriendo': corriendo})


@bp.route('/cuentas-mp', methods=['POST'])
def cuentas_mp():
    f = request.form
    try:
        res = cont.guardar_cuenta_mp(
            alias=f.get('alias'),
            ml_alias=f.get('ml_alias'),
            token_env=f.get('token_env'),
            access_token=f.get('access_token') or None,
        )
        # Decirle qué interpretó: el campo acepta las dos cosas, así que sin
        # este aviso el usuario no sabe si guardó un token o un nombre.
        if res.get('guardado_como') == 'token':
            flash('Cuenta guardada. Detecté que pegaste el token en vez del '
                  'nombre de la variable, así que lo guardé como token — no se '
                  'va a volver a mostrar en pantalla.', 'success')
        elif res.get('guardado_como') == 'variable':
            flash('Cuenta guardada, leyendo el token de la variable de entorno.',
                  'success')
        else:
            flash('Cuenta de Mercado Pago guardada.', 'success')
    except Exception as e:
        flash(f'No se pudo guardar: {e}', 'danger')
    return redirect(url_for('contabilidad.importar'))


# ══════════════════════════════════════════════════════════════════════════════
# CIERRE Y CONCILIACIÓN
# ══════════════════════════════════════════════════════════════════════════════

@bp.route('/cierre')
def cierre_periodo():
    alias = request.args.get('cuenta') or ''
    periodo = request.args.get('periodo') or f'{date.today():%Y-%m}'
    resultado = None
    if alias:
        try:
            resultado = cierre.cerrar_periodo(alias, periodo)
        except Exception as e:
            flash(f'No se pudo cerrar el período: {e}', 'danger')

    # Períodos disponibles: los últimos 18 meses
    periodos = []
    cur = date.today().replace(day=1)
    for _ in range(18):
        periodos.append(f'{cur:%Y-%m}')
        cur = (cur - timedelta(days=1)).replace(day=1)

    return render_template(
        'contabilidad_cierre.html',
        resultado=resultado,
        alias_sel=alias,
        periodo_sel=periodo,
        periodos=periodos,
        **_ctx_base(),
    )


@bp.route('/api/resumen')
def api_resumen():
    """Resumen en JSON — alimenta el gráfico y sirve para el MCP."""
    desde, hasta = _rango_pedido()
    return jsonify(cont.resumen(desde, hasta, request.args.get('cuenta') or None))
