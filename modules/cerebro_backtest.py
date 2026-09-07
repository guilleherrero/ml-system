"""
Cerebro — backtest de la logica de evaluacion sobre el historico ya existente.

Para que sirve: Cerebro tarda 7 a 14 dias en emitir su primer veredicto, y esa
espera a ciegas es inaceptable antes de dejarlo decidir. Este modulo corre el
MISMO evaluador que usa el cron, pero sobre las optimizaciones que ya estan en
`monitor_evolucion.json`, y pone el resultado al lado del Veredicto IA que el
sistema emitio en su momento (Sprint 3.2). Asi se puede juzgar la logica hoy.

Que se compara:
  - veredicto de Cerebro   : estadistico, contra control de hermanas, sin IA
  - veredicto IA existente : Claude Opus mirando los mismos snapshots
  - los numeros crudos     : para poder darle la razon a cualquiera de los dos

Honestidad sobre los limites (importante al leer los resultados):

1. Los snapshots del monitor guardan `visitas_7d` (ventana movil) y
   `ventas_total` (acumulado historico), no visitas ni ventas del dia. Aca se
   derivan: visitas/dia = visitas_7d / 7, y unidades/dia = diferencia de
   `ventas_total` entre snapshots dividida por los dias transcurridos. Es una
   reconstruccion, no el dato exacto. La serie que Cerebro empezo a guardar el
   dia del deploy si es diaria y exacta.

2. No hay serie previa a la optimizacion: el monitor arranca a capturar el dia
   que se aplica. La ventana "antes" se sintetiza desde el baseline (que si
   tiene visitas y conversion de los 7 dias previos), como una serie plana. Al
   ser plana no tiene ruido propio, asi que el umbral de significancia cae a su
   piso del 5% y el backtest se vuelve MAS sensible que el sistema real: canta
   "funciono" o "empeoro" donde en produccion diria "neutra". Si el backtest ya
   dice neutra, en produccion lo diria con mas razon.

3. El veredicto se decide por VISITAS ORGANICAS, no por ventas. Las visitas
   salen de la misma fuente en las dos ventanas (`visitas_7d`); las ventas no,
   asi que compararlas empujaria el resultado hacia "empeoro" por como se
   calculan y no por lo que paso. Las ventas se muestran igual, para mirarlas.

4. No hay precio en los snapshots del monitor, asi que las acciones de precio no
   se pueden backtestear. Solo titulo, descripcion y ficha.

Todo corre sobre un alias sandbox (`_backtest_<Alias>`), no toca la memoria real.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from core.db_storage import db_load, db_save
from modules import cerebro

_logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# Minimo de snapshots posteriores para que la comparacion tenga sentido
MIN_SNAPSHOTS = 4
# Dias de ventana "antes" sintetizados desde el baseline
DIAS_PRE = 7

# veredicto de Cerebro -> veredicto del Veredicto IA (Sprint 3.2)
_EQUIVALENCIA = {
    cerebro.VEREDICTO_FUNCIONO: 'ganadora',
    cerebro.VEREDICTO_NEUTRA:   'neutra',
    cerebro.VEREDICTO_EMPEORO:  'perdedora',
}


def _num(v, d=0.0) -> float:
    try:
        if v is None or v == '':
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _dia(v) -> str:
    return (str(v or '')[:10])


def _baseline_metricas(item: dict) -> dict:
    """Visitas y conversion previas, del baseline agrupado o del plano."""
    b = item.get('baseline') or {}
    trafico = b.get('trafico') if isinstance(b.get('trafico'), dict) else {}
    ventas  = b.get('ventas')  if isinstance(b.get('ventas'), dict)  else {}

    visitas_7d = _num(trafico.get('visitas_7d'), _num(b.get('visitas_7d')))
    conv_7d    = _num(trafico.get('conversion_7d'), _num(b.get('conv_pct')))
    ventas_30d = _num(ventas.get('ventas_30d'), _num(b.get('ventas_30d')))
    return {'visitas_7d': visitas_7d, 'conversion_7d': conv_7d,
            'ventas_30d': ventas_30d}


def _serie_pre(fecha_opt: datetime, base: dict, categoria: str | None) -> list[dict]:
    """Ventana previa sintetizada desde el baseline (serie plana)."""
    visitas_dia = base['visitas_7d'] / 7.0 if base['visitas_7d'] else 0.0
    if base['conversion_7d']:
        unidades_dia = visitas_dia * base['conversion_7d'] / 100.0
    else:
        unidades_dia = base['ventas_30d'] / 30.0 if base['ventas_30d'] else 0.0

    serie = []
    for i in range(DIAS_PRE, 0, -1):
        dia = (fecha_opt - timedelta(days=i)).strftime('%Y-%m-%d')
        serie.append(cerebro.construir_snapshot(
            fecha=dia, visitas_totales=visitas_dia, clics_ads=0,
            unidades=unidades_dia, categoria=categoria))
    return serie


def _serie_post(item: dict, categoria: str | None) -> list[dict]:
    """Snapshots reales del monitor, convertidos a visitas y unidades por dia."""
    snaps = sorted((item.get('snapshots') or []),
                   key=lambda s: _dia(s.get('fecha')))
    serie = []
    prev_total = None
    prev_fecha = None
    for s in snaps:
        dia = _dia(s.get('fecha'))
        if not dia:
            continue
        visitas_dia = _num(s.get('visitas_7d')) / 7.0

        unidades_dia = 0.0
        total = s.get('ventas_total')
        if total is not None and prev_total is not None and prev_fecha:
            try:
                d = (datetime.strptime(dia, '%Y-%m-%d')
                     - datetime.strptime(prev_fecha, '%Y-%m-%d')).days or 1
            except ValueError:
                d = 1
            unidades_dia = max(_num(total) - _num(prev_total), 0.0) / d
        if total is not None:
            prev_total, prev_fecha = total, dia

        pos = s.get('posicion')
        serie.append(cerebro.construir_snapshot(
            fecha=dia, visitas_totales=visitas_dia, clics_ads=0,
            unidades=unidades_dia, categoria=categoria,
            posicion=pos if pos not in (None, 999) else None))
    return serie


def _tipo_accion(item: dict) -> tuple[str, str]:
    """Que se cambio en esa optimizacion, segun lo que guardo el monitor."""
    campos = []
    for clave, etiqueta in (('titulo_nuevo', 'titulo'),
                            ('descripcion_nueva', 'descripcion'),
                            ('atributos_aplicados', 'ficha')):
        if item.get(clave):
            campos.append(etiqueta)
    if not campos and item.get('titulo_antes'):
        campos = ['titulo']
    tipo = 'descripcion' if campos == ['descripcion'] else 'ficha'
    return tipo, ', '.join(campos) if campos else 'optimizacion'


def _explicar(ev: dict, ver_ia: str | None, coincide: bool | None) -> str:
    """Una linea en castellano que diga por que dio lo que dio."""
    if ev.get('veredicto') == cerebro.VEREDICTO_SIN_DATOS:
        return f"Sin datos suficientes: {ev.get('motivo', '')}"
    if ev.get('veredicto') == cerebro.VEREDICTO_CONTAMINADA:
        otras = ', '.join(ev.get('contaminada_por') or [])
        return (f'Hubo otra accion sobre el mismo item dentro de la ventana '
                f'({otras}), asi que el resultado no se le puede atribuir a esta. '
                f'No alimenta los aprendizajes.')

    metrica = {'unidades_dia': 'las unidades por dia',
               'visitas_organicas_dia': 'las visitas organicas por dia',
               }.get(ev.get('metrica_principal'), ev.get('metrica_principal'))
    efecto = _num(ev.get('efecto_pct'))
    umbral = _num(ev.get('umbral_pct'))
    control = ev.get('control') or {}
    ctipo = {'hermanas': f"contra {control.get('n', 0)} publicaciones hermanas no tocadas",
             'propia_historica': 'contra su propia historia previa',
             'sin_control': 'sin control disponible'}.get(control.get('tipo'), '')

    direccion = 'subieron' if efecto > 0 else ('bajaron' if efecto < 0 else 'quedaron igual')
    txt = (f'{metrica.capitalize()} {direccion} {abs(efecto):.1f}% {ctipo} '
           f'(el mercado se movio {_num(control.get("delta_pct")):+.1f}%). '
           f'Hacia falta superar {umbral:.1f}% para llamarlo señal.')
    if coincide is False and ver_ia:
        txt += f' El Veredicto IA en su momento dijo "{ver_ia}".'
    return txt


def correr(alias: str, limite: int | None = None) -> dict:
    """Corre el backtest y devuelve (y persiste) el informe."""
    mon = db_load(os.path.join(DATA_DIR, 'monitor_evolucion.json')) or {}
    items = [it for it in (mon.get('items') or []) if it.get('alias') == alias]

    sandbox = f'_backtest_{alias}'
    # Sandbox limpio en cada corrida: no toca la memoria real de la cuenta
    db_save(os.path.join(DATA_DIR, f'cerebro_{sandbox}', 'snapshots.json'), {'items': {}})
    db_save(os.path.join(DATA_DIR, f'cerebro_{sandbox}', 'acciones.json'), {'acciones': []})
    db_save(os.path.join(DATA_DIR, f'cerebro_{sandbox}', 'aprendizajes.json'),
            {'aprendizajes': {}})

    elegibles, descartados = [], []
    for it in items:
        snaps = it.get('snapshots') or []
        if not it.get('baseline'):
            descartados.append({'item_id': it.get('item_id'),
                                'titulo': (it.get('titulo') or '')[:70],
                                'motivo': 'sin baseline capturado'})
            continue
        if len(snaps) < MIN_SNAPSHOTS:
            descartados.append({'item_id': it.get('item_id'),
                                'titulo': (it.get('titulo') or '')[:70],
                                'motivo': f'solo {len(snaps)} snapshots (hacen falta {MIN_SNAPSHOTS})'})
            continue
        elegibles.append(it)

    elegibles.sort(key=lambda x: _dia(x.get('fecha_opt')), reverse=True)
    if limite:
        elegibles = elegibles[:limite]

    # 1) Cargar las series en el sandbox y registrar la accion sintetica
    acciones_por_item: dict[str, dict] = {}
    for it in elegibles:
        item_id = it.get('item_id')
        try:
            fecha_opt = datetime.strptime(_dia(it.get('fecha_opt')), '%Y-%m-%d')
        except ValueError:
            continue
        categoria = it.get('category_id') or (it.get('baseline') or {}).get('category_id')
        base = _baseline_metricas(it)

        serie = _serie_pre(fecha_opt, base, categoria) + _serie_post(it, categoria)
        cerebro.guardar_snapshots_batch(sandbox, {})   # asegura estructura
        for snap in serie:
            cerebro.guardar_snapshot(sandbox, item_id, snap)

        tipo, campos = _tipo_accion(it)
        acc = cerebro.registrar_accion(
            sandbox, tipo=tipo, item_id=item_id, origen=cerebro.ORIGEN_USUARIO,
            hipotesis=f'optimizacion aplicada ({campos})',
            estado_previo={'categoria': categoria, **base},
            detalle={'campos': campos},
            ejecutado_por='backtest')
        cerebro.actualizar_accion(sandbox, acc['id'],
                                  aplicada_ts=fecha_opt.strftime('%Y-%m-%d %H:%M:%S'))
        acciones_por_item[item_id] = {'accion_id': acc['id'], 'item': it,
                                      'campos': campos, 'tipo': tipo}

    # 2) Correr el evaluador real
    # Se fuerza visitas organicas: es la unica metrica que viene de la misma
    # fuente en las dos ventanas. Las unidades previas son una estimacion del
    # baseline y las posteriores salen de diferencias de ventas acumuladas.
    resumen_eval = cerebro.evaluar_acciones_pendientes(
        sandbox, metrica_forzada='visitas_organicas_dia')

    # 3) Comparar contra el Veredicto IA que ya existia
    filas = []
    coincidencias = difieren = sin_ia = 0
    for item_id, meta in acciones_por_item.items():
        acc = cerebro.get_accion(sandbox, meta['accion_id']) or {}
        evs = acc.get('evaluacion') or {}
        ev = evs.get('14d') or evs.get('7d') or {}
        it = meta['item']

        ver_ia_raw = (it.get('veredicto') or {})
        ver_ia = ver_ia_raw.get('veredicto') or ver_ia_raw.get('resultado')
        ver_cb = ev.get('veredicto')
        equivalente = _EQUIVALENCIA.get(ver_cb)

        coincide = None
        if ver_ia and equivalente:
            coincide = (ver_ia == equivalente)
            if coincide:
                coincidencias += 1
            else:
                difieren += 1
        elif not ver_ia:
            sin_ia += 1

        deltas = ev.get('deltas') or {}
        filas.append({
            'item_id':      item_id,
            'titulo':       (it.get('titulo') or '')[:80],
            'fecha_opt':    _dia(it.get('fecha_opt')),
            'campos':       meta['campos'],
            'snapshots':    len(it.get('snapshots') or []),
            'veredicto_cerebro': ver_cb,
            'veredicto_ia':      ver_ia,
            'equivalente':       equivalente,
            'coincide':          coincide,
            'efecto_pct':        ev.get('efecto_pct'),
            'umbral_pct':        ev.get('umbral_pct'),
            'metrica':           ev.get('metrica_principal'),
            'control':           ev.get('control'),
            'dias_pre':          ev.get('dias_pre'),
            'dias_post':         ev.get('dias_post'),
            'contaminada_por':   ev.get('contaminada_por') or [],
            'visitas_antes':     (deltas.get('visitas_organicas_dia') or {}).get('antes'),
            'visitas_despues':   (deltas.get('visitas_organicas_dia') or {}).get('despues'),
            'unidades_antes':    (deltas.get('unidades_dia') or {}).get('antes'),
            'unidades_despues':  (deltas.get('unidades_dia') or {}).get('despues'),
            'explicacion':       _explicar(ev, ver_ia, coincide),
        })

    filas.sort(key=lambda f: f['fecha_opt'], reverse=True)
    comparables = coincidencias + difieren
    informe = {
        'alias':          alias,
        'generado':       datetime.now().strftime('%Y-%m-%d %H:%M'),
        'total_monitor':  len(items),
        'evaluadas':      len(filas),
        'descartadas':    descartados,
        'coincidencias':  coincidencias,
        'difieren':       difieren,
        'sin_veredicto_ia': sin_ia,
        'acuerdo_pct':    round(coincidencias / comparables * 100, 1) if comparables else None,
        'resumen_eval':   resumen_eval,
        'filas':          filas,
        'advertencias': [
            'Las visitas por dia se derivan de visitas_7d/7 y las unidades de la '
            'diferencia de ventas acumuladas: es una reconstruccion del historico, '
            'no el dato diario exacto que Cerebro guarda desde ahora.',
            'La ventana previa se sintetiza desde el baseline como serie plana. Al '
            'no tener ruido propio, el umbral cae a su piso de 5% y el backtest se '
            'vuelve mas sensible que el sistema real: si aca dice neutra, en '
            'produccion lo diria con mas razon.',
            'El veredicto se decide por visitas organicas, no por ventas: las '
            'visitas salen de la misma fuente en las dos ventanas y las ventas no, '
            'asi que compararlas mediria como se calculan y no lo que paso. Las '
            'ventas igual se muestran al lado.',
            'Las acciones de precio no se pueden backtestear: los snapshots del '
            'monitor no guardan precio.',
        ],
    }
    db_save(os.path.join(DATA_DIR, f'cerebro_backtest_{alias.replace(" ", "_")}.json'),
            informe)
    return informe


def ultimo(alias: str) -> dict | None:
    return db_load(os.path.join(DATA_DIR,
                                f'cerebro_backtest_{alias.replace(" ", "_")}.json'))
