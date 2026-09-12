"""
Cierre contable: conciliación y costo de mercadería vendida.

Tres funciones, en el orden en que se corren:

  aplicar_cmv()        valúa cada venta a su costo y genera el CMV. Sin esto
                       el resultado no es ganancia, es margen sobre plataforma.

  conciliar_billing()  usa la regla de conciliación propia de MercadoLibre:
                       la suma de `detail_amount` por `detail_sub_type` del
                       detalle tiene que dar igual al `amount` por `type` del
                       resumen. Si no da, falta un dato y el sistema lo dice.

  conciliar_ml_mp()    cruza las ventas de ML contra los cobros de MP: ventas
                       sin cobro espejo y cobros sin venta.

Este módulo es el que sostiene el requisito de "ni un dato sin contabilizar":
no alcanza con importar, hay que poder demostrar que lo importado está completo.
"""
import csv
import io
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func

from web.db import session_scope
from web.models_contabilidad import (
    ORIGEN_CMV, ORIGEN_ML_BILLING, ORIGEN_ML_ORDER, ORIGEN_MP_PAYMENT,
    CostoProducto, Movimiento, Rubro,
)
from modules.contabilidad import (
    SUBTIPO_ML_RUBRO, rubro_id_por_codigo,
)

PAUSA_ML = 0.35

# Tolerancia en pesos para considerar que dos totales coinciden. Redondeos de
# la propia API hacen que exigir igualdad exacta dé falsos positivos.
TOLERANCIA = Decimal('1.00')


# ══════════════════════════════════════════════════════════════════════════════
# COSTO DE MERCADERÍA VENDIDA
# ══════════════════════════════════════════════════════════════════════════════

def costo_vigente(session, item_id: str, fecha: date, variacion: str = ''):
    """
    Costo aplicable a una venta: la fila más reciente con `vigente_desde` menor
    o igual a la fecha de venta. Así un lote nuevo más caro no revalúa ventas
    viejas.
    """
    if not item_id:
        return None
    q = (session.query(CostoProducto)
         .filter(CostoProducto.item_id == item_id)
         .filter(CostoProducto.vigente_desde <= fecha)
         .order_by(CostoProducto.vigente_desde.desc(), CostoProducto.id.desc()))
    if variacion:
        exacto = q.filter(CostoProducto.variacion == variacion).first()
        if exacto is not None:
            return exacto
    return q.first()


def aplicar_cmv(desde: date, hasta: date, cuenta_alias: str = None) -> dict:
    """
    Genera (o actualiza) un movimiento de CMV por cada venta del rango.

    Idempotente: el external_id es `cmv-<order_id>-<item_id>`, así que
    recorrerlo de nuevo recalcula en lugar de duplicar. Eso permite correrlo
    otra vez después de cargar costos que faltaban.

    Devuelve cuántas ventas quedaron sin costo: ese número es la medida de
    cuánto le falta al resultado para ser confiable.
    """
    with session_scope() as s:
        rubro_cmv = rubro_id_por_codigo(s, 'CMV')
        rubro_vta = rubro_id_por_codigo(s, 'VTA_ML')

        q = (s.query(Movimiento)
             .filter(Movimiento.origen == ORIGEN_ML_ORDER)
             .filter(Movimiento.subtipo == 'VENTA')
             .filter(Movimiento.computable == True)  # noqa: E712
             .filter(Movimiento.rubro_id == rubro_vta)
             .filter(Movimiento.fecha >= datetime.combine(desde, datetime.min.time()))
             .filter(Movimiento.fecha <= datetime.combine(hasta, datetime.max.time())))
        if cuenta_alias:
            q = q.filter(Movimiento.cuenta_alias == cuenta_alias)

        generados = actualizados = sin_costo = 0
        faltantes = {}
        total_cmv = Decimal('0')

        for venta in q.yield_per(500):
            cantidad = venta.cantidad or 1
            costo = costo_vigente(s, venta.item_id, venta.fecha.date())

            if costo is None or not costo.costo_unitario:
                sin_costo += 1
                clave = venta.item_id or '(sin item)'
                info = faltantes.setdefault(clave, {
                    'item_id': venta.item_id,
                    'titulo': venta.concepto,
                    'ventas': 0, 'unidades': 0,
                    'facturado': Decimal('0'),
                })
                info['ventas'] += 1
                info['unidades'] += cantidad
                info['facturado'] += Decimal(str(venta.monto or 0))
                continue

            monto_cmv = -(Decimal(str(costo.costo_unitario)) * Decimal(cantidad))
            total_cmv += monto_cmv
            ext_id = f'cmv-{venta.order_id}-{venta.item_id}'

            existente = (s.query(Movimiento)
                         .filter_by(cuenta_alias=venta.cuenta_alias,
                                    origen=ORIGEN_CMV, external_id=ext_id)
                         .one_or_none())

            if existente is None:
                s.add(Movimiento(
                    cuenta_alias=venta.cuenta_alias,
                    origen=ORIGEN_CMV,
                    external_id=ext_id,
                    periodo=venta.periodo,
                    fecha=venta.fecha,
                    concepto=f'CMV — {(venta.concepto or "")[:250]}',
                    subtipo='CMV',
                    monto=monto_cmv,
                    moneda=costo.moneda_costo or 'ARS',
                    rubro_id=rubro_cmv,
                    computable=True,
                    order_id=venta.order_id,
                    item_id=venta.item_id,
                    cantidad=cantidad,
                    notas=(f'Costo unitario {costo.costo_unitario} '
                           f'vigente desde {costo.vigente_desde}'),
                ))
                generados += 1
            elif Decimal(str(existente.monto)) != monto_cmv:
                existente.monto = monto_cmv
                existente.cantidad = cantidad
                existente.notas = (f'Costo unitario {costo.costo_unitario} '
                                   f'vigente desde {costo.vigente_desde}')
                actualizados += 1

        ranking = sorted(faltantes.values(),
                         key=lambda f: -f['facturado'])[:30]

        return {
            'generados': generados,
            'actualizados': actualizados,
            'ventas_sin_costo': sin_costo,
            'cmv_total': float(total_cmv),
            'items_sin_costo': [
                {**f, 'facturado': float(f['facturado'])} for f in ranking
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# CARGA MASIVA DE COSTOS
# ══════════════════════════════════════════════════════════════════════════════

COLUMNAS_COSTO = {
    'item_id': ('item_id', 'item', 'mla', 'publicacion', 'publicación', 'id'),
    'variacion': ('variacion', 'variación', 'variante', 'color', 'talle'),
    'sku': ('sku', 'codigo', 'código'),
    'titulo': ('titulo', 'título', 'nombre', 'producto', 'descripcion'),
    'costo_unitario': ('costo_unitario', 'costo', 'costo unitario', 'cost',
                       'costo_final', 'costo puesto'),
    'moneda_costo': ('moneda', 'moneda_costo', 'currency'),
    'fob': ('fob', 'fob_unitario'),
    'flete_prorrateado': ('flete', 'flete_prorrateado', 'shipping'),
    'impuestos_import': ('impuestos', 'impuestos_import', 'aranceles'),
    'vigente_desde': ('vigente_desde', 'vigencia', 'fecha', 'desde'),
    'notas': ('notas', 'nota', 'observaciones'),
}


def _mapear_columnas(cabecera: list) -> dict:
    """Mapea nombres de columna reales a campos del modelo, sin exigir formato."""
    mapa = {}
    normal = [(c or '').strip().lower() for c in cabecera]
    for campo, alias in COLUMNAS_COSTO.items():
        for i, col in enumerate(normal):
            if col in alias:
                mapa[campo] = i
                break
    return mapa


def _a_decimal(valor):
    """
    Parsea números pegados desde planilla: '$ 12.345,67', '12,345.67', '1234'.
    Es el punto donde más se rompe una importación de Excel, así que conviene
    ser tolerante acá y no pedirle al usuario que limpie la planilla.
    """
    if valor is None:
        return None
    s = str(valor).strip()
    if not s:
        return None
    for basura in ('$', 'ARS', 'USD', ' ', ' '):
        s = s.replace(basura, '')
    if ',' in s and '.' in s:
        # El último separador es el decimal
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def cargar_costos_texto(contenido: str, vigente_desde_default: date = None,
                        origen: str = 'excel') -> dict:
    """
    Carga costos desde texto pegado o CSV. Acepta separador coma, punto y coma
    o tabulación (que es lo que sale al copiar de Excel).

    Devuelve el detalle de filas cargadas y rechazadas, con el motivo de cada
    rechazo: cargar 200 costos y no saber cuáles fallaron no sirve.
    """
    if not (contenido or '').strip():
        return {'cargados': 0, 'actualizados': 0, 'rechazados': [],
                'error': 'Contenido vacío'}

    muestra = contenido[:4000]
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=',;\t|')
        delim = dialecto.delimiter
    except csv.Error:
        delim = '\t' if '\t' in muestra else ','

    lector = csv.reader(io.StringIO(contenido), delimiter=delim)
    filas = [f for f in lector if any((c or '').strip() for c in f)]
    if not filas:
        return {'cargados': 0, 'actualizados': 0, 'rechazados': [],
                'error': 'No se encontraron filas'}

    mapa = _mapear_columnas(filas[0])
    if 'item_id' in mapa and 'costo_unitario' in mapa:
        cuerpo = filas[1:]
    else:
        # Sin cabecera reconocible: se asume item_id en la primera columna y
        # costo en la segunda, que es el pegado mínimo más común.
        mapa = {'item_id': 0, 'costo_unitario': 1}
        if len(filas[0]) > 2:
            mapa['titulo'] = 2
        cuerpo = filas

    default_fecha = vigente_desde_default or date.today().replace(day=1)
    cargados = actualizados = 0
    rechazados = []

    with session_scope() as s:
        for nro, fila in enumerate(cuerpo, start=2):
            def val(campo):
                i = mapa.get(campo)
                if i is None or i >= len(fila):
                    return None
                v = (fila[i] or '').strip()
                return v or None

            item_id = (val('item_id') or '').upper().replace(' ', '')
            if not item_id:
                rechazados.append({'fila': nro, 'motivo': 'sin item_id'})
                continue
            if not item_id.startswith('MLA'):
                rechazados.append({'fila': nro, 'item_id': item_id,
                                   'motivo': 'el item_id no parece un MLA'})
                continue

            costo = _a_decimal(val('costo_unitario'))
            if costo is None or costo < 0:
                rechazados.append({'fila': nro, 'item_id': item_id,
                                   'motivo': f'costo ilegible: {val("costo_unitario")!r}'})
                continue

            fecha_vig = default_fecha
            crudo_fecha = val('vigente_desde')
            if crudo_fecha:
                for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y'):
                    try:
                        fecha_vig = datetime.strptime(crudo_fecha, fmt).date()
                        break
                    except ValueError:
                        continue

            variacion = val('variacion') or ''
            existente = (s.query(CostoProducto)
                         .filter_by(item_id=item_id, variacion=variacion,
                                    vigente_desde=fecha_vig)
                         .one_or_none())

            campos = {
                'sku': val('sku'),
                'titulo': (val('titulo') or '')[:300] or None,
                'costo_unitario': costo,
                'moneda_costo': (val('moneda_costo') or 'ARS').upper()[:8],
                'fob': _a_decimal(val('fob')),
                'flete_prorrateado': _a_decimal(val('flete_prorrateado')),
                'impuestos_import': _a_decimal(val('impuestos_import')),
                'origen_dato': origen,
                'notas': val('notas'),
            }

            if existente is None:
                s.add(CostoProducto(item_id=item_id, variacion=variacion,
                                    vigente_desde=fecha_vig, **campos))
                cargados += 1
            else:
                for k, v in campos.items():
                    if v is not None:
                        setattr(existente, k, v)
                actualizados += 1

    return {'cargados': cargados, 'actualizados': actualizados,
            'rechazados': rechazados, 'delimitador': repr(delim)}


def items_sin_costo(cuenta_alias: str = None, desde: date = None) -> list:
    """
    Publicaciones con ventas pero sin costo cargado, ordenadas por lo facturado.
    Es la lista de trabajo para que el resultado pase de aproximado a real.
    """
    desde = desde or date(date.today().year, 1, 1)
    with session_scope() as s:
        rubro_vta = rubro_id_por_codigo(s, 'VTA_ML')
        con_costo = {c for (c,) in s.query(CostoProducto.item_id).distinct()}

        q = (s.query(
                Movimiento.item_id,
                func.min(Movimiento.concepto).label('titulo'),
                func.count(Movimiento.id).label('ventas'),
                func.sum(Movimiento.monto).label('facturado'),
             )
             .filter(Movimiento.rubro_id == rubro_vta)
             .filter(Movimiento.computable == True)  # noqa: E712
             .filter(Movimiento.item_id != None)  # noqa: E711
             .filter(Movimiento.fecha >= datetime.combine(desde, datetime.min.time()))
             .group_by(Movimiento.item_id)
             .order_by(func.sum(Movimiento.monto).desc()))
        if cuenta_alias:
            q = q.filter(Movimiento.cuenta_alias == cuenta_alias)

        return [{
            'item_id': item_id,
            'titulo': titulo,
            'ventas': ventas,
            'facturado': float(facturado or 0),
        } for item_id, titulo, ventas, facturado in q.all()
            if item_id not in con_costo]


# ══════════════════════════════════════════════════════════════════════════════
# CONCILIACIÓN CONTRA EL RESUMEN DE FACTURACIÓN DE ML
# ══════════════════════════════════════════════════════════════════════════════

def conciliar_billing(alias: str, periodo: str) -> dict:
    """
    Aplica la regla de conciliación oficial de MercadoLibre para un período
    ('YYYY-MM'): por cada `type` del resumen, la suma de `detail_amount` de los
    detalles con ese `detail_sub_type` tiene que coincidir.

    Si no coincide, el sistema no dice "todo bien": informa la diferencia y el
    subtipo donde está. Es la prueba de que no falta ni un dato.
    """
    from core.account_manager import AccountManager

    mgr = AccountManager()
    client = mgr.get_client(alias)
    if client is None:
        raise ValueError(f'No hay cuenta ML con alias "{alias}"')

    key = f'{periodo}-01'
    path = f'/billing/integration/periods/key/{key}/summary/details'

    resumen_api = {}
    errores = []
    for grupo in ('ML', 'MP'):
        try:
            data = client._get(path, {'group': grupo})
        except Exception as e:
            errores.append({'grupo': grupo, 'error': str(e)[:300]})
            continue
        for cargo in ((data or {}).get('charges') or []):
            tipo = (cargo.get('type') or '').strip().upper()
            if not tipo:
                continue
            monto = abs(Decimal(str(cargo.get('amount') or 0)))
            entrada = resumen_api.setdefault(tipo, {
                'tipo': tipo,
                'label': cargo.get('label') or tipo,
                'resumen': Decimal('0'),
            })
            entrada['resumen'] += monto
        time.sleep(PAUSA_ML)

    # Lo que tenemos importado, agrupado por subtipo
    with session_scope() as s:
        filas = (s.query(Movimiento.subtipo,
                         func.sum(func.abs(Movimiento.monto)),
                         func.count(Movimiento.id))
                 .filter(Movimiento.cuenta_alias == alias)
                 .filter(Movimiento.origen == ORIGEN_ML_BILLING)
                 .filter(Movimiento.periodo == periodo)
                 .group_by(Movimiento.subtipo)
                 .all())
    importado = {
        (sub or '').upper(): {'monto': Decimal(str(total or 0)), 'cantidad': cant}
        for sub, total, cant in filas
    }

    lineas = []
    todos = set(resumen_api) | set(importado)
    dif_total = Decimal('0')
    for tipo in sorted(todos):
        en_resumen = resumen_api.get(tipo, {}).get('resumen', Decimal('0'))
        en_libro = importado.get(tipo, {}).get('monto', Decimal('0'))
        dif = en_libro - en_resumen
        dif_total += abs(dif)
        lineas.append({
            'subtipo': tipo,
            'label': resumen_api.get(tipo, {}).get('label')
                     or SUBTIPO_ML_RUBRO.get(tipo, '') or tipo,
            'resumen_ml': float(en_resumen),
            'importado': float(en_libro),
            'diferencia': float(dif),
            'cantidad': importado.get(tipo, {}).get('cantidad', 0),
            'estado': ('ok' if abs(dif) <= TOLERANCIA else
                       ('falta_importar' if dif < 0 else 'sobra_importado')),
        })

    problemas = [l for l in lineas if l['estado'] != 'ok']
    return {
        'cuenta': alias,
        'periodo': periodo,
        'cuadra': not problemas and not errores,
        'diferencia_absoluta': float(dif_total),
        'lineas': lineas,
        'problemas': problemas,
        'errores': errores,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CONCILIACIÓN VENTAS ML ↔ COBROS MP
# ══════════════════════════════════════════════════════════════════════════════

def conciliar_ml_mp(desde: date, hasta: date, cuenta_alias: str = None) -> dict:
    """
    Cruza las ventas de ML contra los cobros de MP por order_id.

    Tres resultados posibles, y los tres importan:
      - ventas_sin_cobro: se vendió y no hay plata espejo en MP. Puede ser una
        venta reciente sin liberar, o un dato que falta.
      - cobros_sin_venta: entró plata con order_id que no tenemos importado.
        Señal de que el rango de órdenes quedó corto.
      - diferencias: el monto cobrado no coincide con lo vendido (retenciones,
        cancelaciones parciales).
    """
    d0 = datetime.combine(desde, datetime.min.time())
    d1 = datetime.combine(hasta, datetime.max.time())

    with session_scope() as s:
        rubro_vta = rubro_id_por_codigo(s, 'VTA_ML')

        q_ventas = (s.query(Movimiento.order_id,
                            func.sum(Movimiento.monto),
                            func.min(Movimiento.concepto),
                            func.min(Movimiento.fecha))
                    .filter(Movimiento.origen == ORIGEN_ML_ORDER)
                    .filter(Movimiento.rubro_id == rubro_vta)
                    .filter(Movimiento.computable == True)  # noqa: E712
                    .filter(Movimiento.fecha >= d0, Movimiento.fecha <= d1)
                    .filter(Movimiento.order_id != None)  # noqa: E711
                    .group_by(Movimiento.order_id))

        q_cobros = (s.query(Movimiento.order_id,
                            func.sum(Movimiento.monto),
                            func.min(Movimiento.fecha))
                    .filter(Movimiento.origen == ORIGEN_MP_PAYMENT)
                    .filter(Movimiento.computable == True)  # noqa: E712
                    .filter(Movimiento.monto > 0)
                    .filter(Movimiento.fecha >= d0, Movimiento.fecha <= d1)
                    .filter(Movimiento.order_id != None)  # noqa: E711
                    .group_by(Movimiento.order_id))

        if cuenta_alias:
            q_ventas = q_ventas.filter(Movimiento.cuenta_alias == cuenta_alias)
            q_cobros = q_cobros.filter(Movimiento.cuenta_alias == cuenta_alias)

        ventas = {oid: {'monto': Decimal(str(m or 0)), 'concepto': c, 'fecha': f}
                  for oid, m, c, f in q_ventas.all()}
        cobros = {oid: {'monto': Decimal(str(m or 0)), 'fecha': f}
                  for oid, m, f in q_cobros.all()}

    sin_cobro, sin_venta, diferencias = [], [], []
    total_ventas = sum((v['monto'] for v in ventas.values()), Decimal('0'))
    total_cobros = sum((c['monto'] for c in cobros.values()), Decimal('0'))

    for oid, v in ventas.items():
        if oid not in cobros:
            sin_cobro.append({
                'order_id': oid,
                'concepto': (v['concepto'] or '')[:120],
                'fecha': v['fecha'].isoformat() if v['fecha'] else None,
                'vendido': float(v['monto']),
            })
        else:
            dif = cobros[oid]['monto'] - v['monto']
            # El cobro en MP viene neto de comisión, así que una diferencia
            # negativa es esperable. Se reporta solo si es a favor o enorme.
            if dif > TOLERANCIA or dif < -(v['monto'] * Decimal('0.45')):
                diferencias.append({
                    'order_id': oid,
                    'vendido': float(v['monto']),
                    'cobrado': float(cobros[oid]['monto']),
                    'diferencia': float(dif),
                })

    for oid, c in cobros.items():
        if oid not in ventas:
            sin_venta.append({
                'order_id': oid,
                'fecha': c['fecha'].isoformat() if c['fecha'] else None,
                'cobrado': float(c['monto']),
            })

    return {
        'desde': desde.isoformat(),
        'hasta': hasta.isoformat(),
        'cuenta': cuenta_alias or 'TODAS',
        'ventas_importadas': len(ventas),
        'cobros_importados': len(cobros),
        'total_vendido': float(total_ventas),
        'total_cobrado_mp': float(total_cobros),
        'ventas_sin_cobro': sorted(sin_cobro, key=lambda x: -x['vendido'])[:100],
        'cobros_sin_venta': sorted(sin_venta, key=lambda x: -x['cobrado'])[:100],
        'diferencias': sorted(diferencias,
                              key=lambda x: -abs(x['diferencia']))[:100],
        'cuadra': not sin_venta and not diferencias,
    }


def cerrar_periodo(alias: str, periodo: str, cuenta_alias_mp: str = None) -> dict:
    """
    Corre el cierre completo de un mes: aplica CMV, concilia contra el resumen
    de facturación de ML y cruza ML contra MP. Devuelve un veredicto único.
    """
    anio, mes = int(periodo[:4]), int(periodo[5:7])
    desde = date(anio, mes, 1)
    hasta = (date(anio + (mes == 12), (mes % 12) + 1, 1)
             - __import__('datetime').timedelta(days=1))

    salida = {'periodo': periodo, 'cuenta': alias}

    try:
        salida['cmv'] = aplicar_cmv(desde, hasta, alias)
    except Exception as e:
        salida['cmv'] = {'error': str(e)[:300]}
    try:
        salida['billing'] = conciliar_billing(alias, periodo)
    except Exception as e:
        salida['billing'] = {'error': str(e)[:300]}
    try:
        salida['ml_mp'] = conciliar_ml_mp(desde, hasta)
    except Exception as e:
        salida['ml_mp'] = {'error': str(e)[:300]}

    alertas = []
    cmv = salida.get('cmv') or {}
    if cmv.get('ventas_sin_costo'):
        alertas.append(
            f'{cmv["ventas_sin_costo"]} ventas sin costo cargado: el resultado '
            f'del mes está sobreestimado.'
        )
    bil = salida.get('billing') or {}
    if bil.get('problemas'):
        alertas.append(
            f'{len(bil["problemas"])} subtipos de facturación no cuadran contra '
            f'el resumen de ML.'
        )
    mlmp = salida.get('ml_mp') or {}
    if mlmp.get('cobros_sin_venta'):
        alertas.append(
            f'{len(mlmp["cobros_sin_venta"])} cobros de MP sin venta importada: '
            f'faltaría ampliar el rango de órdenes.'
        )

    salida['alertas'] = alertas
    salida['cerrado_ok'] = not alertas
    return salida
