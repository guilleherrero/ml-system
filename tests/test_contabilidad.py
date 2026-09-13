"""
Tests del sistema contable.

Se prueban con la forma REAL de los datos de la cuenta (los conceptos, montos
y estados salen de movimientos observados en producción), no con datos
inventados de laboratorio.

Lo que se verifica:
  - idempotencia: reimportar no duplica
  - dirección del dinero: lo que Guille paga no cuenta como facturación
  - rechazados / devueltos: se guardan pero no suman
  - cargos de ML negativos, bonificaciones positivas
  - subtipo desconocido va a pendientes, no se pierde ni rompe el total
  - el cobro espejo de MP no duplica la venta
  - CMV y el aviso de ventas sin costo
  - parseo de costos pegados desde Excel

Correr:  python3 tests/test_contabilidad.py
"""
import os
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal

# Base de datos temporal ANTES de importar web.db (resuelve el engine al importar)
_tmpdir = tempfile.mkdtemp(prefix='cont_test_')
os.environ['DATABASE_URL'] = ''
os.environ['CONT_TEST_DB'] = os.path.join(_tmpdir, 'test.db')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Fuerza el fallback SQLite de web/db.py hacia el directorio temporal
import web.db as webdb  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker, scoped_session  # noqa: E402

webdb.DATABASE_URL = 'sqlite:///' + os.environ['CONT_TEST_DB']
webdb.engine = create_engine(webdb.DATABASE_URL, future=True,
                             connect_args={'check_same_thread': False})
webdb.SessionFactory = sessionmaker(bind=webdb.engine, autoflush=False,
                                    autocommit=False, future=True)
webdb.Session = scoped_session(webdb.SessionFactory)

from web.models_contabilidad import (  # noqa: E402
    AMBITO_PERSONAL, ORIGEN_CMV, ORIGEN_ML_ORDER, ORIGEN_MP_PAYMENT,
    RUBRO_SIN_CLASIFICAR,
    CostoProducto, Movimiento, Rubro,
)
from modules import contabilidad as cont  # noqa: E402
from modules import contabilidad_import as imp  # noqa: E402
from modules import contabilidad_cierre as cierre  # noqa: E402

webdb.Base.metadata.create_all(webdb.engine)

FALLOS = []
PASADOS = []


def check(condicion, descripcion, detalle=''):
    if condicion:
        PASADOS.append(descripcion)
        print(f'  ok    {descripcion}')
    else:
        FALLOS.append((descripcion, detalle))
        print(f'  FALLA {descripcion}' + (f'\n          → {detalle}' if detalle else ''))


def seccion(titulo):
    print(f'\n── {titulo} ' + '─' * max(0, 62 - len(titulo)))


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES — forma real de los datos
# ══════════════════════════════════════════════════════════════════════════════

MI_ID = '123456789'          # user id de la cuenta (collector)
OTRO_ID = '999888777'
ALIAS = 'Novara'

# Venta de ML cobrada por MP: él es el collector
PAGO_VENTA_ML = {
    'id': 177713012057,
    'status': 'approved',
    'date_approved': '2026-09-12T13:34:24.000-03:00',
    'date_created': '2026-09-12T13:34:20.000-03:00',
    'description': 'Cortador De Puntas Abiertas Para El Cabello Negro',
    'transaction_amount': 68400.0,
    'currency_id': 'ARS',
    'payment_type_id': 'credit_card',
    'collector_id': int(MI_ID),
    'payer': {'id': int(OTRO_ID)},
    'order': {'id': 2000018424423218, 'type': 'mercadolibre'},
    'fee_details': [{'type': 'mercadopago_fee', 'amount': 8200.0}],
    'transaction_details': {'net_received_amount': 60200.0},
}

# Compra personal en el supermercado: él es el PAYER. El módulo viejo contaba
# esto como facturación.
PAGO_PERSONAL_DIA = {
    'id': 177945637628,
    'status': 'approved',
    'date_approved': '2026-09-08T13:45:19.000-03:00',
    'description': 'Compra en DIA',
    'transaction_amount': 12450.0,
    'currency_id': 'ARS',
    'payment_type_id': 'account_money',
    'collector_id': int(OTRO_ID),
    'payer': {'id': int(MI_ID)},
    'fee_details': [{'type': 'mercadopago_fee', 'amount': 105.83}],
}

# Servicio del hogar pagado desde MP
PAGO_EDENOR = {
    'id': 176948405727,
    'status': 'approved',
    'date_approved': '2026-09-08T10:57:22.000-03:00',
    'description': 'Edenor',
    'transaction_amount': 83622.76,
    'currency_id': 'ARS',
    'payment_type_id': 'account_money',
    'collector_id': int(OTRO_ID),
    'payer': {'id': int(MI_ID)},
}

# Pago rechazado: tiene que quedar registrado pero NO sumar
PAGO_RECHAZADO = {
    'id': 177352915007,
    'status': 'rejected',
    'date_created': '2026-09-10T13:46:41.000-03:00',
    'description': 'Cortador De Puntas Abriertas Para Cabello Dañado - Ender Pro Negro',
    'transaction_amount': 60578.0,
    'currency_id': 'ARS',
    'collector_id': int(MI_ID),
    'payer': {'id': int(OTRO_ID)},
    'order': {'id': 2000018392085550},
}

# Envío cobrado al comprador
PAGO_ENVIO = {
    'id': 177564417027,
    'status': 'approved',
    'date_approved': '2026-09-11T16:13:11.000-03:00',
    'description': 'marketplace_shipment',
    'transaction_amount': 14490.0,
    'currency_id': 'ARS',
    'collector_id': int(MI_ID),
    'payer': {'id': int(OTRO_ID)},
    'transaction_details': {'net_received_amount': 14490.0},
}

# Cargo por venta de ML (comisión) — detalle de billing
BILLING_COMISION = {
    'charge_info': {
        'legal_document_number': '0011A03800000',
        'legal_document_status': 'PROCESSED',
        'creation_date_time': '2026-09-12T13:35:02',
        'detail_id': 5555566666,
        'transaction_detail': 'Cargo por venta',
        'detail_amount': 12000.50,
        'detail_type': 'CHARGE',
        'detail_sub_type': 'CV',
    },
    'discount_info': {'charge_amount_without_discount': 12000.50,
                      'discount_amount': 0},
    'sales_info': [{'order_id': 2000018424423218, 'operation_id': 177713012057,
                    'sale_date_time': '2026-09-12T13:34:20',
                    'transaction_amount': 68400}],
    'shipping_info': {'shipping_id': '47993053776'},
    'items_info': [{'item_id': 'MLA3024679994',
                    'item_title': 'Cortador De Puntas',
                    'item_amount': 1, 'item_price': 68400}],
    'document_info': {'document_id': 5555566666},
    'currency_info': {'currency_id': 'ARS'},
}

# Bonificación de envío — tiene que entrar POSITIVA
BILLING_BONIFICACION = {
    'charge_info': {
        'creation_date_time': '2026-09-12T13:35:02',
        'detail_id': 7777788888,
        'transaction_detail': 'Bonificación por Mercado Envíos',
        'detail_amount': 3500.0,
        'detail_type': 'BONUS',
        'detail_sub_type': 'BXD',
    },
    'currency_info': {'currency_id': 'ARS'},
}

# Subtipo que no conocemos: NO debe romper ni desaparecer
BILLING_SUBTIPO_RARO = {
    'charge_info': {
        'creation_date_time': '2026-09-12T14:00:00',
        'detail_id': 9999900000,
        'transaction_detail': 'Cargo por concepto nuevo de ML',
        'detail_amount': 4200.0,
        'detail_type': 'CHARGE',
        'detail_sub_type': 'CXYZNUEVO',
    },
    'currency_info': {'currency_id': 'ARS'},
}

# Product Ads
BILLING_ADS = {
    'charge_info': {
        'creation_date_time': '2026-09-05T00:00:30',
        'detail_id': 1212121212,
        'transaction_detail': 'Campañas de publicidad - Product Ads',
        'detail_amount': 48600.0,
        'detail_type': 'CHARGE',
        'detail_sub_type': 'PADS',
    },
    'currency_info': {'currency_id': 'ARS'},
}

# Orden de venta con retenciones
ORDEN = {
    'id': 2000018424423218,
    'status': 'paid',
    'date_created': '2026-09-12T13:34:20.000-03:00',
    'date_closed': '2026-09-12T13:34:24.000-03:00',
    'total_amount': 68400.0,
    'paid_amount': 68400.0,
    'currency_id': 'ARS',
    'order_items': [{
        'item': {'id': 'MLA3024679994',
                 'title': 'Cortador De Puntas Abiertas Para El Cabello Negro'},
        'quantity': 2,
        'unit_price': 34200.0,
        'sale_fee': 6000.25,
    }],
    'payments': [{
        'id': 177713012057,
        'date_approved': '2026-09-12T13:34:24.000-03:00',
        'tax_details': [
            {'mov_detail': 'tax_withholding',
             'mov_financial_entity': 'retencion_iva',
             'original_amount': 2018.99, 'refunded_amount': 0,
             'tax_status': 'applied'},
            {'mov_detail': 'tax_withholding_collector',
             'mov_financial_entity': 'debitos_creditos',
             'original_amount': 500.0, 'refunded_amount': 0,
             'tax_status': 'applied'},
        ],
    }],
}

ORDEN_CANCELADA = {
    'id': 2000018343049490,
    'status': 'cancelled',
    'date_created': '2026-09-08T08:37:36.000-03:00',
    'total_amount': 24000.0,
    'paid_amount': 24000.0,
    'currency_id': 'ARS',
    'order_items': [{
        'item': {'id': 'MLA2240715828', 'title': 'Delineador De Cejas'},
        'quantity': 1, 'unit_price': 24000.0, 'sale_fee': 3000.0,
    }],
    'payments': [],
}


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════

def test_semilla():
    seccion('Plan de rubros')
    res = cont.sembrar_plan()
    check(res['rubros_creados'] == len(cont.PLAN_RUBROS),
          f'se crean los {len(cont.PLAN_RUBROS)} rubros del plan',
          f'creados: {res["rubros_creados"]}')
    check(res['reglas_creadas'] == len(cont.REGLAS_SEMILLA),
          f'se crean las {len(cont.REGLAS_SEMILLA)} reglas semilla',
          f'creadas: {res["reglas_creadas"]}')

    # Idempotencia de la semilla
    res2 = cont.sembrar_plan()
    check(res2['rubros_creados'] == 0 and res2['reglas_creadas'] == 0,
          'volver a sembrar no duplica nada', str(res2))

    with webdb.session_scope() as s:
        sin_clasif = s.query(Rubro).filter_by(codigo=RUBRO_SIN_CLASIFICAR).one()
        check(sin_clasif.afecta_resultado is False,
              'el rubro SIN_CLASIF no afecta el resultado')
        personal = s.query(Rubro).filter_by(codigo='PERSONAL').one()
        check(personal.afecta_resultado is False
              and personal.ambito == AMBITO_PERSONAL,
              'el rubro PERSONAL es personal y no afecta el resultado')


def test_direccion_mp():
    seccion('Dirección del dinero en Mercado Pago (el bug principal)')

    venta = imp._normalizar_pago_mp(PAGO_VENTA_ML, MI_ID, ALIAS)
    check(venta['monto'] == Decimal('60200.0'),
          'cobro de venta entra positivo y por el neto acreditado',
          f'monto: {venta["monto"]}')
    check(venta.get('rubro_sugerido') == 'CONCIL_MP',
          'el cobro espejo de una venta ML va al rubro neutro de conciliación',
          f'rubro: {venta.get("rubro_sugerido")}')

    dia = imp._normalizar_pago_mp(PAGO_PERSONAL_DIA, MI_ID, ALIAS)
    check(dia['monto'] == Decimal('-12450.0'),
          'la compra en DIA queda NEGATIVA (antes sumaba como facturación)',
          f'monto: {dia["monto"]}')

    edenor = imp._normalizar_pago_mp(PAGO_EDENOR, MI_ID, ALIAS)
    check(edenor['monto'] < 0,
          'el pago de Edenor queda negativo',
          f'monto: {edenor["monto"]}')

    rech = imp._normalizar_pago_mp(PAGO_RECHAZADO, MI_ID, ALIAS)
    check(rech['computable'] is False,
          'un pago rechazado se guarda pero no es computable',
          f'computable: {rech["computable"]}')

    # Dirección indeterminable
    huerfano = dict(PAGO_VENTA_ML, collector_id=int(OTRO_ID),
                    payer={'id': int(OTRO_ID)}, id=1)
    orf = imp._normalizar_pago_mp(huerfano, MI_ID, ALIAS)
    check(orf['revisar'] is True and orf['nota_revision'],
          'si no se puede determinar la dirección, queda marcado para revisar')


def test_signos_billing():
    seccion('Signos de facturación de MercadoLibre')

    com = imp._normalizar_detalle_billing(BILLING_COMISION, ALIAS, 'ML', '2026-09-01')
    check(com['monto'] == Decimal('-12000.50'),
          'un CHARGE de ML entra negativo', f'monto: {com["monto"]}')
    check(com['rubro_sugerido'] == 'COM_ML',
          'el subtipo CV se mapea a comisiones de venta')
    check(com['order_id'] == '2000018424423218' and com['item_id'] == 'MLA3024679994',
          'el cargo queda trazado a la orden y a la publicación')

    bon = imp._normalizar_detalle_billing(BILLING_BONIFICACION, ALIAS, 'ML', '2026-09-01')
    check(bon['monto'] == Decimal('3500.0'),
          'un BONUS de ML entra positivo', f'monto: {bon["monto"]}')

    ads = imp._normalizar_detalle_billing(BILLING_ADS, ALIAS, 'ML', '2026-09-01')
    check(ads['rubro_sugerido'] == 'ADS_ML' and ads['monto'] < 0,
          'Product Ads se mapea a publicidad y es un egreso')

    raro = imp._normalizar_detalle_billing(BILLING_SUBTIPO_RARO, ALIAS, 'ML', '2026-09-01')
    check(raro['rubro_sugerido'] is None and raro['revisar'] is True,
          'un subtipo desconocido no se adivina: queda para revisar')
    check('CXYZNUEVO' in (raro['nota_revision'] or ''),
          'la nota de revisión dice cuál es el subtipo desconocido',
          raro['nota_revision'])


def test_ordenes():
    seccion('Órdenes y retenciones')

    movs = imp._normalizar_orden(ORDEN, ALIAS)
    venta = [m for m in movs if m['subtipo'] == 'VENTA'][0]
    check(venta['monto'] == Decimal('68400.0') and venta['rubro_sugerido'] == 'VTA_ML',
          'la venta entra positiva como VTA_ML', f'monto: {venta["monto"]}')
    check(venta['cantidad'] == 2,
          'la cantidad suma las unidades de la orden', f'cant: {venta["cantidad"]}')

    rets = [m for m in movs if m['subtipo'] != 'VENTA']
    check(len(rets) == 2, 'se generan las dos retenciones de la orden',
          f'generadas: {len(rets)}')
    rubros_ret = {m['rubro_sugerido'] for m in rets}
    check(rubros_ret == {'RET_IVA', 'IMP_DEB_CRED'},
          'las retenciones se mapean a sus rubros impositivos', str(rubros_ret))
    check(all(m['monto'] < 0 for m in rets),
          'las retenciones son egresos')

    cancel = imp._normalizar_orden(ORDEN_CANCELADA, ALIAS)
    v_cancel = [m for m in cancel if m['subtipo'] == 'VENTA'][0]
    check(v_cancel['computable'] is False,
          'una orden cancelada no es computable')
    check(v_cancel['rubro_sugerido'] == 'CANCEL',
          'una orden cancelada va al rubro de cancelaciones')


def test_idempotencia_y_resumen():
    seccion('Idempotencia e integridad del resumen')

    lote = [
        imp._normalizar_pago_mp(PAGO_VENTA_ML, MI_ID, ALIAS),
        imp._normalizar_pago_mp(PAGO_PERSONAL_DIA, MI_ID, ALIAS),
        imp._normalizar_pago_mp(PAGO_EDENOR, MI_ID, ALIAS),
        imp._normalizar_pago_mp(PAGO_RECHAZADO, MI_ID, ALIAS),
        imp._normalizar_pago_mp(PAGO_ENVIO, MI_ID, ALIAS),
        imp._normalizar_detalle_billing(BILLING_COMISION, ALIAS, 'ML', '2026-09-01'),
        imp._normalizar_detalle_billing(BILLING_BONIFICACION, ALIAS, 'ML', '2026-09-01'),
        imp._normalizar_detalle_billing(BILLING_ADS, ALIAS, 'ML', '2026-09-01'),
        imp._normalizar_detalle_billing(BILLING_SUBTIPO_RARO, ALIAS, 'ML', '2026-09-01'),
    ]
    lote += imp._normalizar_orden(ORDEN, ALIAS)
    lote += imp._normalizar_orden(ORDEN_CANCELADA, ALIAS)

    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)
        estados = [cont.upsert_movimiento(s, d, reglas) for d in lote]
    nuevos = sum(1 for e in estados if e == 'nuevo')
    check(nuevos == len(lote), f'se insertan los {len(lote)} movimientos',
          f'nuevos: {nuevos}')

    # Reimportar exactamente lo mismo
    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)
        estados2 = [cont.upsert_movimiento(s, d, reglas) for d in lote]
    check(all(e in ('igual', 'actualizado') for e in estados2),
          'reimportar el mismo rango no inserta nada nuevo',
          str(set(estados2)))

    with webdb.session_scope() as s:
        total_filas = s.query(Movimiento).count()
    check(total_filas == len(lote),
          f'siguen habiendo {len(lote)} filas después de reimportar',
          f'filas: {total_filas}')

    # ── Clasificación ──
    with webdb.session_scope() as s:
        dia = (s.query(Movimiento)
               .filter_by(external_id=str(PAGO_PERSONAL_DIA['id'])).one())
        rubro_dia = s.get(Rubro, dia.rubro_id)
        check(rubro_dia.codigo == 'PERSONAL',
              'la compra en DIA se clasifica como PERSONAL',
              f'rubro: {rubro_dia.codigo}')

        edenor = (s.query(Movimiento)
                  .filter_by(external_id=str(PAGO_EDENOR['id'])).one())
        check(s.get(Rubro, edenor.rubro_id).codigo == 'PERSONAL',
              'Edenor se clasifica como PERSONAL')

        envio = (s.query(Movimiento)
                 .filter_by(external_id=str(PAGO_ENVIO['id'])).one())
        check(s.get(Rubro, envio.rubro_id).codigo == 'ENVIO_COBRADO',
              'marketplace_shipment se clasifica como envío cobrado',
              f'rubro: {s.get(Rubro, envio.rubro_id).codigo}')

        raro = s.query(Movimiento).filter_by(subtipo='CXYZNUEVO').one()
        check(s.get(Rubro, raro.rubro_id).codigo == RUBRO_SIN_CLASIFICAR,
              'el subtipo desconocido queda en SIN_CLASIF')

    # ── Resumen ──
    r = cont.resumen(date(2026, 9, 1), date(2026, 9, 30))
    t = r['totales']

    # Ingresos = venta 68400 + envío cobrado 14490.
    # La bonificación de envío (+3500) NO es ingreso: es un cargo de envío que
    # ML devuelve, así que reduce el gasto de envío, no infla la facturación.
    check(abs(t['ingresos'] - (68400.0 + 14490.0)) < 0.01,
          'los ingresos suman venta + envío cobrado (la bonificación no es ingreso)',
          f'ingresos: {t["ingresos"]}')

    check(abs(t['personal'] - (-12450.0 - 83622.76)) < 0.01,
          'los movimientos personales se reportan aparte',
          f'personal: {t["personal"]}')

    # El personal NO debe estar en el resultado
    check(t['resultado'] > t['personal'],
          'el resultado no incluye los gastos personales')

    # La venta no se cuenta dos veces: el cobro de MP fue a CONCIL_MP (neutro)
    codigos = {f['codigo'] for f in r['por_rubro']}
    concil = [f for f in r['por_rubro'] if f['codigo'] == 'CONCIL_MP']
    check(concil and concil[0]['afecta_resultado'] is False,
          'el cobro espejo de MP está registrado pero no afecta el resultado')
    # El cobro de la venta en MP (60200 neto) está importado pero es neutro.
    # Si se duplicara, los ingresos rondarían los 143.000.
    check(abs(t['ingresos'] - 82890.0) < 0.01,
          'la venta se cuenta UNA sola vez (no se duplica con el cobro de MP)',
          f'ingresos: {t["ingresos"]} (si se duplicara sería ~143090)')

    # Impuestos: retención IVA 2018.99 + débitos/créditos 500
    check(abs(t['impuestos'] - (-2018.99 - 500.0)) < 0.01,
          'las retenciones impositivas se suman como egreso',
          f'impuestos: {t["impuestos"]}')

    # Gastos = comisión -12000.50 + bonificación de envío +3500 - ads 48600.
    # La orden cancelada NO entra: no es un gasto de 24000, es un no-hecho.
    check(abs(t['gastos'] - (-12000.50 + 3500.0 - 48600.0)) < 0.01,
          'la bonificación de envío reduce el gasto de envío en vez de ser ingreso',
          f'gastos: {t["gastos"]}')

    with webdb.session_scope() as s:
        cancelada = (s.query(Movimiento)
                     .filter_by(external_id=str(ORDEN_CANCELADA['id'])).one())
        check(cancelada.computable is False,
              'la orden cancelada está en el libro pero fuera de los totales')

    check(r['pendientes_de_clasificar'] == 1,
          'el resumen avisa que hay 1 movimiento sin clasificar',
          f'pendientes: {r["pendientes_de_clasificar"]}')

    avisos = [a['texto'] for a in r['advertencias']]
    check(any('ganancia' in a for a in avisos),
          'el resumen avisa que sin costo de mercadería el número no es ganancia',
          str(avisos))

    # El desglose tiene que leerse como un estado de resultados: Ingresos
    # primero y los neutros al final, no alfabéticamente por grupo.
    grupos_en_orden = []
    for fila in r['por_rubro']:
        if fila['grupo'] not in grupos_en_orden:
            grupos_en_orden.append(fila['grupo'])
    check(grupos_en_orden and grupos_en_orden[0] == 'Ingresos',
          'el desglose por rubro arranca por Ingresos', str(grupos_en_orden))
    check(grupos_en_orden[-1] in ('Personal', 'Pendientes', 'Neutros'),
          'los rubros que no afectan resultado quedan al final',
          str(grupos_en_orden))
    if 'Impuestos' in grupos_en_orden and 'Plataforma' in grupos_en_orden:
        check(grupos_en_orden.index('Plataforma') < grupos_en_orden.index('Impuestos'),
              'Plataforma se lee antes que Impuestos', str(grupos_en_orden))

    # El rechazado no está en ningún total, pero sí en la base
    with webdb.session_scope() as s:
        rech = (s.query(Movimiento)
                .filter_by(external_id=str(PAGO_RECHAZADO['id'])).one())
        check(rech.computable is False,
              'el pago rechazado quedó guardado y no computable')


def test_bandeja_pendientes():
    seccion('Bandeja de pendientes y aprendizaje de reglas')

    pend = cont.pendientes()
    ids_subtipo = [p for p in pend if p['subtipo'] == 'CXYZNUEVO']
    check(ids_subtipo, 'el subtipo desconocido aparece en la bandeja')

    mov_id = ids_subtipo[0]['id']
    res = cont.asignar_rubro(mov_id, 'COM_ML', crear_regla=True)
    check(res['ok'] and res['regla_creada'],
          'asignar rubro a mano crea una regla para los próximos iguales')

    with webdb.session_scope() as s:
        mov = s.get(Movimiento, mov_id)
        check(mov.rubro_manual is True and mov.revisar is False,
              'el movimiento queda fijado a mano y sale de la bandeja')

    # Una reclasificación no debe pisar lo que decidió el humano
    cont.reclasificar_pendientes()
    with webdb.session_scope() as s:
        mov = s.get(Movimiento, mov_id)
        check(s.get(Rubro, mov.rubro_id).codigo == 'COM_ML',
              'reclasificar no pisa el rubro fijado a mano')


def test_costos_y_cmv():
    seccion('Carga de costos y CMV')

    # Pegado tal cual sale de Excel: tabulaciones, signo $, decimal con coma
    pegado = (
        'item_id\ttitulo\tcosto\n'
        'MLA3024679994\tCortador De Puntas\t$ 18.500,50\n'
        'mla2240715828\tDelineador De Cejas\t4200\n'
        'MLA9999999999\tProducto sin ventas\t1.000,00\n'
        '\tFila sin item\t999\n'
        'MLA1111111111\tCosto ilegible\tabc\n'
        'NOESUNMLA\tId invalido\t500\n'
    )
    res = cierre.cargar_costos_texto(pegado, vigente_desde_default=date(2026, 1, 1))
    check(res['cargados'] == 3,
          'se cargan las 3 filas válidas del pegado de Excel',
          f'cargados: {res["cargados"]}, rechazados: {res["rechazados"]}')
    check(len(res['rechazados']) == 3,
          'se rechazan las 3 filas malas y se explica el motivo de cada una',
          str(res['rechazados']))

    with webdb.session_scope() as s:
        c = s.query(CostoProducto).filter_by(item_id='MLA3024679994').one()
        check(c.costo_unitario == Decimal('18500.50'),
              'el formato "$ 18.500,50" se parsea correcto',
              f'costo: {c.costo_unitario}')
        c2 = s.query(CostoProducto).filter_by(item_id='MLA2240715828').one()
        check(c2 is not None, 'el item_id en minúscula se normaliza a mayúscula')

    # Aplicar CMV
    res_cmv = cierre.aplicar_cmv(date(2026, 9, 1), date(2026, 9, 30))
    check(res_cmv['generados'] == 1,
          'se genera el CMV de la única venta computable',
          str(res_cmv))
    # 2 unidades × 18500.50
    check(abs(res_cmv['cmv_total'] + 37001.0) < 0.01,
          'el CMV valúa 2 unidades al costo vigente (37.001)',
          f'cmv: {res_cmv["cmv_total"]}')

    # Idempotencia del CMV
    res_cmv2 = cierre.aplicar_cmv(date(2026, 9, 1), date(2026, 9, 30))
    check(res_cmv2['generados'] == 0,
          'volver a aplicar el CMV no duplica')
    with webdb.session_scope() as s:
        check(s.query(Movimiento).filter_by(origen=ORIGEN_CMV).count() == 1,
              'hay un solo movimiento de CMV')

    # Ahora el resumen debe tener costos y cambiar el aviso
    r = cont.resumen(date(2026, 9, 1), date(2026, 9, 30))
    check(abs(r['totales']['costos'] + 37001.0) < 0.01,
          'el resumen ahora incluye el costo de mercadería',
          f'costos: {r["totales"]["costos"]}')
    avisos = [a['texto'] for a in r['advertencias']]
    check(not any('NO ganancia' in a for a in avisos),
          'con costo cargado desaparece el aviso de "no es ganancia"',
          str(avisos))

    # El resultado final tiene que ser la suma de todo lo que afecta resultado
    t = r['totales']
    esperado = (t['ingresos'] + t['costos'] + t['gastos']
                + t['impuestos'] + t['financiero'])
    check(abs(t['resultado'] - esperado) < 0.01,
          'el resultado es exactamente la suma de sus componentes',
          f'resultado: {t["resultado"]} vs suma: {esperado}')


def test_items_sin_costo():
    seccion('Detección de publicaciones sin costo')
    faltantes = cierre.items_sin_costo(desde=date(2026, 1, 1))
    ids = {f['item_id'] for f in faltantes}
    check('MLA3024679994' not in ids,
          'la publicación con costo cargado no aparece como faltante')
    # La orden cancelada no es computable, así que su item no debería figurar
    check(all(f['facturado'] != 0 for f in faltantes) or not faltantes,
          'el listado de faltantes reporta lo facturado por publicación')


def test_gasto_manual():
    seccion('Carga manual de gastos')

    mid = cont.registrar_gasto_manual(
        cuenta_alias=ALIAS, fecha=date(2026, 9, 15), rubro_codigo='PROV',
        concepto='Pago a proveedor China - lote agosto', monto=1500000,
        proveedor='Shenzhen Beauty Co', tipo_comprobante='Factura E',
        nro_comprobante='A-0001', medio_pago='Transferencia',
    )
    with webdb.session_scope() as s:
        m = s.get(Movimiento, mid)
        check(m.monto == Decimal('-1500000'),
              'un gasto cargado en positivo se guarda negativo automáticamente',
              f'monto: {m.monto}')
        check(m.rubro_manual is True,
              'el gasto manual queda marcado como clasificado a mano')
        check(s.get(Rubro, m.rubro_id).codigo == 'PROV',
              'el gasto va al rubro de proveedores')

    # Un impuesto cargado ya en negativo no debe invertirse dos veces
    mid2 = cont.registrar_gasto_manual(
        cuenta_alias=ALIAS, fecha=date(2026, 9, 20), rubro_codigo='IIBB',
        concepto='IIBB Convenio Multilateral', monto=-45000,
    )
    with webdb.session_scope() as s:
        m2 = s.get(Movimiento, mid2)
        check(m2.monto == Decimal('-45000'),
              'un gasto cargado en negativo se mantiene negativo',
              f'monto: {m2.monto}')


def test_multicuenta():
    seccion('Multicuenta')

    # Mismo external_id en otra cuenta: tiene que convivir
    datos = imp._normalizar_pago_mp(PAGO_VENTA_ML, MI_ID, 'Cuenta2')
    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)
        estado = cont.upsert_movimiento(s, datos, reglas)
    check(estado == 'nuevo',
          'el mismo pago en otra cuenta es un movimiento distinto')

    por_cuenta = cont.resumen_por_cuenta(date(2026, 9, 1), date(2026, 9, 30))
    cuentas = {c['cuenta'] for c in por_cuenta}
    check('TODAS' in cuentas and ALIAS in cuentas and 'Cuenta2' in cuentas,
          'el resumen por cuenta devuelve el global y cada cuenta',
          str(cuentas))

    solo_novara = cont.resumen(date(2026, 9, 1), date(2026, 9, 30), ALIAS)
    todas = cont.resumen(date(2026, 9, 1), date(2026, 9, 30))
    check(todas['totales']['ingresos'] >= solo_novara['totales']['ingresos'],
          'el global no puede facturar menos que una sola cuenta')


def test_rango_periodos():
    seccion('Utilidades de rango')
    ps = cont._rango_periodos(date(2026, 1, 1), date(2026, 9, 12))
    check(len(ps) == 9 and ps[0] == '2026-01' and ps[-1] == '2026-09',
          'el rango enero-septiembre da 9 períodos mensuales', str(ps))

    ventanas = list(imp._ventanas(date(2026, 1, 1), date(2026, 1, 10), 7))
    check(ventanas == [(date(2026, 1, 1), date(2026, 1, 7)),
                       (date(2026, 1, 8), date(2026, 1, 10))],
          'las ventanas de fecha no se solapan ni dejan huecos', str(ventanas))

    claves = list(imp._claves_periodo(date(2026, 8, 15), date(2026, 10, 2)))
    check(claves == ['2026-08-01', '2026-09-01', '2026-10-01'],
          'las claves de período de billing son el primer día de cada mes',
          str(claves))

    # Un año completo de ventanas semanales debe cubrir los 365 días
    total_dias = sum((f - d).days + 1
                     for d, f in imp._ventanas(date(2026, 1, 1), date(2026, 12, 31), 7))
    check(total_dias == 365,
          'las ventanas de un año cubren los 365 días sin perder ninguno',
          f'días cubiertos: {total_dias}')


def test_limite_billing():
    """
    La API de facturación de ML permite 5 requests por minuto. La primera
    versión usaba 0,35 s de pausa y la importación de un año moría con 429 en
    la sexta página, perdiendo los 2.056 movimientos ya leídos. Estos tests
    fijan el comportamiento corregido.
    """
    seccion('Límite de la API de facturación (5 por minuto)')
    import time as _t

    check(imp.PAUSA_BILLING >= 12.0,
          'la pausa entre llamadas a /billing respeta el límite de 5 por minuto',
          f'PAUSA_BILLING = {imp.PAUSA_BILLING}')

    check(imp._es_429(Exception('GET /x failed: {"status":429,'
                                '"type":"TOO_MANY_REQUESTS_ERROR"}')),
          'se reconoce el 429 de MercadoLibre')
    check(not imp._es_429(Exception('GET /x failed: 404 not found')),
          'un 404 no se confunde con un 429')

    # El limitador serializa: con la pausa bajada, dos turnos se espacian
    original = imp.PAUSA_BILLING
    imp.PAUSA_BILLING = 0.25
    imp._ultima_llamada_billing[0] = 0.0
    try:
        t0 = _t.monotonic()
        imp._esperar_turno_billing()
        imp._esperar_turno_billing()
        transcurrido = _t.monotonic() - t0
    finally:
        imp.PAUSA_BILLING = original
        imp._ultima_llamada_billing[0] = 0.0
    check(transcurrido >= 0.24,
          'el limitador espacia dos llamadas consecutivas',
          f'transcurrido: {transcurrido:.3f}s')

    # _billing_get reintenta ante 429 y no aborta
    class ClienteFalso:
        def __init__(self, fallos):
            self.fallos = fallos
            self.llamadas = 0

        def _get(self, path, params):
            self.llamadas += 1
            if self.llamadas <= self.fallos:
                raise Exception('{"status":429,"type":"TOO_MANY_REQUESTS_ERROR"}')
            return {'results': [], 'last_id': None}

    imp.PAUSA_BILLING = 0.01
    espera_original = imp.ESPERA_429_INICIAL
    imp.ESPERA_429_INICIAL = 0.01
    try:
        c = ClienteFalso(fallos=2)
        res = imp._billing_get(c, '/billing/x', {})
        check(res is not None and c.llamadas == 3,
              'ante 429 reintenta en vez de abortar la importación',
              f'llamadas: {c.llamadas}')

        # Agotados los reintentos, sí propaga
        c2 = ClienteFalso(fallos=99)
        try:
            imp._billing_get(c2, '/billing/x', {})
            check(False, 'agotados los reintentos propaga el error')
        except Exception:
            check(c2.llamadas == imp.REINTENTOS_429 + 1,
                  'agotados los reintentos propaga el error',
                  f'llamadas: {c2.llamadas}')
    finally:
        imp.PAUSA_BILLING = original
        imp.ESPERA_429_INICIAL = espera_original
        imp._ultima_llamada_billing[0] = 0.0


def test_token_mp_en_campo_equivocado():
    """
    En la primera corrida real el token se pegó en el campo que pedía el nombre
    de la variable de entorno, y el sistema reportaba "falta token" sin explicar
    nada. Ahora se detecta, se guarda donde va y nunca se vuelve a mostrar.
    """
    seccion('Token de MP pegado en el campo del nombre de variable')

    check(cont._parece_token('APP_USR-3490679534133896-071012-'
                             '5eb61814161dfe5db63dbf9f48a71d55-55993545'),
          'un token de producción se reconoce como token')
    check(cont._parece_token('TEST-1234567890123456-091213-'
                             'abcdef0123456789abcdef0123456789-55993545'),
          'un token de prueba se reconoce como token')
    check(not cont._parece_token('MP_ACCESS_TOKEN'),
          'un nombre de variable NO se confunde con un token')
    check(not cont._parece_token('MP_ACCESS_TOKEN_PROD'),
          'un nombre de variable con sufijo tampoco se confunde')
    check(not cont._parece_token(''), 'el vacío no es un token')

    tok = ('APP_USR-9999999999999999-091213-'
           'ffffffff0123456789abcdef01234567-55993545')
    res = cont.guardar_cuenta_mp(alias='MP-Test', ml_alias=ALIAS, token_env=tok)
    check(res['guardado_como'] == 'token',
          'guardar_cuenta_mp detecta el token pegado y avisa', str(res))

    with webdb.session_scope() as s:
        from web.models_contabilidad import CuentaMP
        c = s.query(CuentaMP).filter_by(alias='MP-Test').one()
        check(c.access_token == tok,
              'el token quedó guardado en el campo del token')
        check(c.token_env is None,
              'el campo del nombre de variable quedó limpio')

    listado = [c for c in cont.listar_cuentas_mp() if c['alias'] == 'MP-Test']
    check(listado and listado[0]['tiene_token'],
          'la cuenta figura con token válido')
    check(listado and 'access_token' not in listado[0]
          and 'token_env' not in listado[0],
          'el listado nunca devuelve el valor del token', str(listado))

    # Un nombre de variable real se guarda como variable
    res2 = cont.guardar_cuenta_mp(alias='MP-Env', token_env='MP_ACCESS_TOKEN')
    check(res2['guardado_como'] == 'variable',
          'un nombre de variable se guarda como variable', str(res2))

    # La migración arregla una fila que ya quedó mal en la base
    with webdb.session_scope() as s:
        from web.models_contabilidad import CuentaMP
        s.add(CuentaMP(alias='MP-Roto', token_env=tok, access_token=None))
    res3 = cont.migrar_tokens_mp_mal_guardados()
    check('MP-Roto' in res3['corregidas'],
          'la migración detecta y corrige la fila mal guardada', str(res3))
    with webdb.session_scope() as s:
        from web.models_contabilidad import CuentaMP
        c = s.query(CuentaMP).filter_by(alias='MP-Roto').one()
        check(c.access_token == tok and c.token_env is None,
              'la fila corregida quedó con el token en su lugar')
    res4 = cont.migrar_tokens_mp_mal_guardados()
    check(not res4['corregidas'], 'la migración es idempotente', str(res4))


def test_subtipos_reales_mla():
    """
    Los subtipos que devuelve MLA en la practica no son los de los ejemplos de
    la documentacion. En la primera corrida real 1.830 de 2.056 detalles
    quedaron sin clasificar por eso. Estos son los codigos observados en la
    facturacion de la cuenta.
    """
    seccion('Subtipos reales de facturación de MLA')

    esperado = {
        'CVFV': 'COM_ML',      # Cargo por vender
        'CVFF': 'CARGO_FIJO',  # Costo por unidad vendida
        'CVFN': 'FIN_ML',      # Costo por ofrecer cuotas
        'CFF':  'ENVIO_ML',    # Cargo por envíos de Mercado Libre
        'CDSD': 'CANCEL',      # Cargo por devolución
        'CESM': 'SERV_ML',     # Cargo por mantenimiento de Mi página
    }
    for sub, rubro in esperado.items():
        check(cont.rubro_de_subtipo(sub) == rubro,
              f'{sub} se mapea a {rubro}',
              f'devolvió {cont.rubro_de_subtipo(sub)}')

    # La inicial B es la anulación del mismo concepto: mismo rubro, sin
    # necesidad de listarla aparte.
    for sub_b, rubro in (('BVFV', 'COM_ML'), ('BVFN', 'FIN_ML'),
                         ('BFF', 'ENVIO_ML'), ('BVFF', 'CARGO_FIJO')):
        check(cont.rubro_de_subtipo(sub_b) == rubro,
              f'{sub_b} (anulación) cae en el mismo rubro que su cargo',
              f'devolvió {cont.rubro_de_subtipo(sub_b)}')

    check(cont.rubro_de_subtipo('CQUIENSABE') is None,
          'un subtipo realmente desconocido sigue devolviendo None')
    check(cont.rubro_de_subtipo('') is None and cont.rubro_de_subtipo(None) is None,
          'un subtipo vacío no rompe')

    # Los rubros nuevos existen en el plan
    codigos = {r[0] for r in cont.PLAN_RUBROS}
    check('CARGO_FIJO' in codigos and 'SERV_ML' in codigos,
          'los rubros nuevos están en el plan', str(sorted(codigos))[:120])


def test_reclasificar_aplica_mapa_de_subtipos():
    """
    Al ampliar el mapa de subtipos, lo ya importado tenia que poder
    reclasificarse sin reimportar el año entero. El reclasificador solo pasaba
    las reglas y no volvia a mirar el subtipo nativo, asi que no servia.
    """
    seccion('Reclasificar aplica el mapa de subtipos')

    # Un movimiento de facturación con un subtipo que el mapa NO conocía
    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)
        cont.upsert_movimiento(s, {
            'cuenta_alias': ALIAS,
            'origen': 'ml_billing',
            'external_id': 'ML-reclas-1',
            'fecha': datetime(2026, 5, 10, 12, 0),
            'concepto': 'Cargo por vender',
            'subtipo': 'CVFV',
            'monto': Decimal('-5815'),
            'rubro_sugerido': None,       # entra sin rubro, como en la realidad
            'revisar': True,
            'nota_revision': 'Subtipo de billing no mapeado: CVFV',
        }, reglas)

    with webdb.session_scope() as s:
        mov = s.query(Movimiento).filter_by(external_id='ML-reclas-1').one()
        check(s.get(Rubro, mov.rubro_id).codigo == RUBRO_SIN_CLASIFICAR,
              'el movimiento arranca sin clasificar')

    res = cont.reclasificar_pendientes()
    check(res['reclasificados'] >= 1,
          'la reclasificación mueve al menos un movimiento', str(res))

    with webdb.session_scope() as s:
        mov = s.query(Movimiento).filter_by(external_id='ML-reclas-1').one()
        check(s.get(Rubro, mov.rubro_id).codigo == 'COM_ML',
              'el CVFV quedó en comisiones por venta sin reimportar nada',
              f'quedó en {s.get(Rubro, mov.rubro_id).codigo}')
        check(mov.revisar is False and mov.nota_revision is None,
              'se limpia la marca de revisar y la nota')


def test_importar_costos_del_sistema():
    """
    Los costos ya cargados en config/costos.json (el modulo "Cargar costos")
    tienen que entrar solos: pedirle al usuario que los cargue de nuevo es
    hacerlo trabajar dos veces.
    """
    seccion('Traer los costos ya cargados en el sistema')

    import core.db_storage as almacen
    original = almacen.db_load

    almacen.db_load = lambda ruta: {
        'MLA1481911017': {'alias': 'Novara', 'titulo': 'Cortador Ender Pro',
                          'costo': 21500, 'updated': '2026-08-01'},
        'MLA1694974575': {'alias': 'Novara', 'titulo': 'Parches Uñas',
                          'costo': 7800.50, 'updated': '2026-08-01'},
        'MLA9999999999': {'alias': 'Novara', 'titulo': 'Sin costo cargado',
                          'costo': None},
        'MLA8888888888': {'alias': 'Novara', 'titulo': 'Costo cero', 'costo': 0},
        'MLA7777777777': 'no es un dict',
    }
    try:
        res = cierre.importar_costos_del_sistema(vigente_desde=date(2026, 1, 1))
        check(res.get('cargados') == 2,
              'trae los 2 costos válidos del JSON del sistema', str(res))
        check(res.get('sin_costo') == 2,
              'cuenta los que están sin costo o en cero', str(res))

        with webdb.session_scope() as s:
            c = s.query(CostoProducto).filter_by(item_id='MLA1694974575').one()
            check(c.costo_unitario == Decimal('7800.50'),
                  'el costo con decimales se guarda exacto',
                  f'quedó {c.costo_unitario}')
            check(c.origen_dato == 'costos_json',
                  'queda marcado de dónde salió el costo')
            check(c.vigente_desde == date(2026, 1, 1),
                  'la vigencia cubre todo el año que se contabiliza')

        # Idempotente
        res2 = cierre.importar_costos_del_sistema(vigente_desde=date(2026, 1, 1))
        check(res2.get('cargados') == 0,
              'volver a traerlos no duplica', str(res2))

        # Si el costo cambia en el JSON, se actualiza
        almacen.db_load = lambda ruta: {
            'MLA1481911017': {'titulo': 'Cortador Ender Pro', 'costo': 23900},
        }
        res3 = cierre.importar_costos_del_sistema(vigente_desde=date(2026, 1, 1))
        check(res3.get('actualizados') == 1,
              'un costo que cambió en el sistema se actualiza', str(res3))
        with webdb.session_scope() as s:
            c = s.query(CostoProducto).filter_by(item_id='MLA1481911017').one()
            check(c.costo_unitario == Decimal('23900'),
                  'el costo actualizado quedó bien')

        # Un JSON vacío no rompe
        almacen.db_load = lambda ruta: {}
        res4 = cierre.importar_costos_del_sistema()
        check(res4.get('cargados') == 0 and 'mensaje' in res4,
              'sin costos en el sistema devuelve un mensaje claro', str(res4))

        # Un error de la capa de persistencia no revienta
        def _explota(ruta):
            raise RuntimeError('kv_store caído')
        almacen.db_load = _explota
        res5 = cierre.importar_costos_del_sistema()
        check('error' in res5, 'un fallo al leer se informa sin romper', str(res5))
    finally:
        almacen.db_load = original


def test_percepciones_contra_esquema_real():
    """
    La primera version del importador de percepciones adivinaba los campos
    (`type`, `label`, `name`) que NO existen en la respuesta, usaba la posicion
    en la lista como identificador y no miraba `status`. Resultado en
    produccion: 19 movimientos por 12,7 millones contra 146 millones de ventas,
    un 8,7% de percepciones. Estos tests usan la forma documentada real.
    """
    seccion('Percepciones contra el esquema documentado')

    # ── Clasificación por los campos reales ──
    iva = {'tax_type': 'CRGI', 'regimen_tax_type': 'MLA_RE_IVA_N',
           'regimen_tax_type_description': 'Percepción de IVA nuevos del régimen especial'}
    rubro, etiqueta = imp._clasificar_percepcion(iva)
    check(rubro == 'PERCEP', 'una percepción de IVA va a Percepciones', rubro)
    check('IVA' in etiqueta,
          'la etiqueta sale de la descripción real, no del genérico "Percepción"',
          etiqueta)

    iibb = {'tax_type': 'CRIB', 'regimen_tax_type': 'MLA_IIBB_SIRTAC',
            'regimen_tax_type_description': 'Percepción de Ingresos Brutos SIRTAC'}
    check(imp._clasificar_percepcion(iibb)[0] == 'IIBB',
          'una percepción de Ingresos Brutos va a IIBB (antes caía en PERCEP)',
          imp._clasificar_percepcion(iibb)[0])

    gan = {'tax_type': 'CRGAN',
           'tax_type_description': 'Retención de Ganancias'}
    check(imp._clasificar_percepcion(gan)[0] == 'RET_GAN',
          'una retención de Ganancias va a su rubro')

    # ── Importador completo con la respuesta documentada ──
    import core.account_manager as gestor

    RESPUESTA = {
        '2026-01-01': {
            'summary': [
                {'document_id': 123456789, 'society': 'ML',
                 'legal_document_number': '0011A012345678',
                 'amount': 229314.11, 'taxable_amount': 22931410.96,
                 'aliquot': 1.00, 'tax_type': 'CRGI',
                 'regimen_tax_type': 'MLA_RE_IVA_N',
                 'regimen_tax_type_description': 'Percepción de IVA nuevos',
                 'bill_date': '2026-01-29', 'status': 'APPLIED',
                 'currency': 'ARS'},
                {'document_id': 987654321, 'amount': 50000.0,
                 'taxable_amount': 2500000.0, 'aliquot': 2.0,
                 'tax_type': 'CRIB',
                 'regimen_tax_type_description': 'Percepción de Ingresos Brutos',
                 'bill_date': '2026-01-29', 'status': 'APPLIED',
                 'currency': 'ARS'},
                # No aplicada: se guarda pero NO debe sumar
                {'document_id': 111222333, 'amount': 999999.0,
                 'tax_type': 'CRGI', 'status': 'CANCELLED',
                 'regimen_tax_type_description': 'Percepción anulada',
                 'currency': 'ARS'},
            ],
            'errors': [],
        },
    }

    class ClientePercep:
        def _get(self, path, params):
            for key, resp in RESPUESTA.items():
                if f'/key/{key}/' in path:
                    # Solo el grupo ML devuelve filas en este fixture
                    return resp if params.get('group') == 'ML' else {'summary': []}
            return {'summary': []}

    class GestorFalso:
        def get_client(self, alias):
            return ClientePercep()

    original_gestor = gestor.AccountManager
    pausa_original = imp.PAUSA_BILLING
    gestor.AccountManager = GestorFalso
    imp.PAUSA_BILLING = 0.01
    imp._ultima_llamada_billing[0] = 0.0
    try:
        res = imp.importar_ml_percepciones(ALIAS, date(2026, 1, 1), date(2026, 1, 31))
        check(res['nuevos'] == 3,
              'importa las 3 filas del resumen (incluida la no aplicada)', str(res))

        with webdb.session_scope() as s:
            filas = (s.query(Movimiento)
                     .filter(Movimiento.origen == 'ml_percepcion')
                     .all())
            por_ext = {m.external_id: m for m in filas}

            check('ML-123456789-CRGI' in por_ext,
                  'el external_id es estable: grupo + documento + impuesto',
                  str(sorted(por_ext))[:160])
            check(not any(e[0].isdigit() for e in por_ext),
                  'ningún id quedó con el formato posicional viejo')

            # Ahora NO suman: el resumen repite cargos del detalle de facturación
            check(all(m.computable is False for m in filas),
                  'ninguna fila del resumen es computable (ya está en el detalle)')
            check(all(s.get(Rubro, m.rubro_id).codigo == 'CONCIL_PERCEP'
                      for m in filas),
                  'todas van al rubro neutro de conciliación')

            iva_mov = por_ext.get('ML-123456789-CRGI')
            check(iva_mov is not None and iva_mov.fecha.date() == date(2026, 1, 29),
                  'usa bill_date en vez del día 1 del período',
                  str(iva_mov.fecha) if iva_mov else 'no está')
            check(iva_mov is not None
                  and iva_mov.monto == Decimal('-229314.11'),
                  'toma `amount` (la percepción), no `taxable_amount` (la base)',
                  str(iva_mov.monto) if iva_mov else '')
            check(iva_mov is not None and iva_mov.notas
                  and 'alícuota' in iva_mov.notas,
                  'deja anotada la base y la alícuota para poder auditar',
                  iva_mov.notas if iva_mov else '')
            check(iva_mov is not None and iva_mov.nota_revision
                  and 'detalle de facturación' in iva_mov.nota_revision,
                  'la nota explica por qué no suma',
                  iva_mov.nota_revision if iva_mov else '')

        # Reimportar no duplica ni cambia montos
        res2 = imp.importar_ml_percepciones(ALIAS, date(2026, 1, 1), date(2026, 1, 31))
        check(res2['nuevos'] == 0,
              'reimportar no crea percepciones nuevas', str(res2))
        with webdb.session_scope() as s:
            cant = (s.query(Movimiento)
                    .filter(Movimiento.origen == 'ml_percepcion').count())
            check(cant == 3, 'siguen siendo 3 percepciones', f'hay {cant}')

        # Nada del resumen entra al resultado
        r = cont.resumen(date(2026, 1, 1), date(2026, 1, 31))
        codigos = {f['codigo'] for f in r['por_rubro']}
        check('CONCIL_PERCEP' not in codigos,
              'el resumen de percepciones no aparece en el resultado '
              '(no es computable)', str(sorted(codigos))[:160])
    finally:
        gestor.AccountManager = original_gestor
        imp.PAUSA_BILLING = pausa_original
        imp._ultima_llamada_billing[0] = 0.0


def test_limpieza_percepciones_viejas():
    seccion('Limpieza de las percepciones con id posicional')

    # Simula lo que quedó en producción con la primera versión
    with webdb.session_scope() as s:
        for i, monto in enumerate((-670000, -580000)):
            s.add(Movimiento(
                cuenta_alias=ALIAS, origen='ml_percepcion',
                external_id=f'2026-03-01-{i}',   # formato posicional viejo
                periodo='2026-03', fecha=datetime(2026, 3, 1),
                concepto='Percepción', monto=Decimal(monto),
                computable=True,
            ))

    res = cont.limpiar_percepciones_con_id_posicional()
    check(res['borrados'] == 2,
          'borra las percepciones con id posicional', str(res))
    check(abs(res['monto_liberado'] + 1250000) < 0.01,
          'informa cuánto monto mal cargado libera', str(res))

    with webdb.session_scope() as s:
        quedan = {m.external_id for m in s.query(Movimiento)
                  .filter(Movimiento.origen == 'ml_percepcion').all()}
        check(all(e.startswith(('ML-', 'MP-')) for e in quedan),
              'solo sobreviven las que tienen id estable', str(sorted(quedan))[:140])
        check(len(quedan) == 3, 'las 3 correctas siguen ahí', f'quedan {len(quedan)}')

    res2 = cont.limpiar_percepciones_con_id_posicional()
    check(res2['borrados'] == 0, 'la limpieza es idempotente', str(res2))


def test_percepciones_no_se_duplican_con_facturacion():
    """
    El bug que encontro el usuario mirando el libro: las percepciones llegan
    TAMBIEN por el detalle de facturacion (subtipos CIVA, CIRE, IBNQ, IBCA...)
    y ademas se importaban del resumen. Mismo peso contado dos veces: en
    produccion 25,4 millones de percepciones donde habia 12,7.
    """
    seccion('Percepciones: detalle de facturación vs resumen')

    # Los subtipos de percepcion del DETALLE ahora se clasifican
    for sub, esperado in (('CIVA', 'PERCEP'), ('CIRE', 'PERCEP'),
                          ('IBNQ', 'IIBB'), ('IBCA', 'IIBB'),
                          ('IIBB', 'IIBB'), ('CGMV', 'IIBB'),
                          ('CIBT', 'IIBB'), ('IBSA', 'IIBB')):
        check(cont.rubro_de_subtipo(sub) == esperado,
              f'el subtipo de facturación {sub} se mapea a {esperado}',
              f'dio {cont.rubro_de_subtipo(sub)}')

    # Una percepcion por el detalle SI computa
    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)
        cont.upsert_movimiento(s, imp._normalizar_detalle_billing({
            'charge_info': {
                'creation_date_time': '2026-02-11T00:00:00',
                'detail_id': 4242424242,
                'transaction_detail': 'Percepción de IVA Régimen General',
                'detail_amount': 88109.0,
                'detail_type': 'CHARGE', 'detail_sub_type': 'CIVA',
            },
            'currency_info': {'currency_id': 'ARS'},
        }, ALIAS, 'ML', '2026-02-01'), reglas)

    r = cont.resumen(date(2026, 2, 1), date(2026, 2, 28))
    percep = {f['codigo']: f['total'] for f in r['por_rubro']}
    check(abs(percep.get('PERCEP', 0) + 88109.0) < 0.01,
          'la percepción del detalle de facturación sí suma, una sola vez',
          f'dio {percep.get("PERCEP")}')


def test_neutralizar_resumen_percepciones():
    seccion('Migración: sacar del resultado el resumen de percepciones')

    # Simula lo que quedo en produccion: filas del resumen computando
    with webdb.session_scope() as s:
        rid = cont.rubro_id_por_codigo(s, 'PERCEP')
        for i, monto in enumerate((-757407, -88109)):
            s.add(Movimiento(
                cuenta_alias=ALIAS, origen='ml_percepcion',
                external_id=f'ML-99900{i}-CIRE', periodo='2026-04',
                fecha=datetime(2026, 4, 10), concepto='MLA_RE_IVA',
                monto=Decimal(monto), rubro_id=rid, computable=True,
            ))

    antes = cont.resumen(date(2026, 4, 1), date(2026, 4, 30))['totales']['impuestos']
    res = cont.neutralizar_resumen_percepciones()
    check(res['neutralizados'] >= 2,
          'neutraliza las filas del resumen que estaban computando', str(res))
    check(abs(res['monto_sacado'] + 845516) < 1,
          'informa cuánto saca del resultado', str(res))

    despues = cont.resumen(date(2026, 4, 1), date(2026, 4, 30))['totales']['impuestos']
    check(despues > antes,
          'el total de impuestos baja al dejar de duplicar',
          f'antes {antes} → después {despues}')

    with webdb.session_scope() as s:
        filas = (s.query(Movimiento)
                 .filter(Movimiento.origen == 'ml_percepcion').all())
        check(all(not m.computable for m in filas),
              'ninguna fila del resumen quedó computable')
        check(all(s.get(Rubro, m.rubro_id).codigo == 'CONCIL_PERCEP'
                  for m in filas),
              'todas quedaron en el rubro neutro')

    res2 = cont.neutralizar_resumen_percepciones()
    check(res2['neutralizados'] == 0, 'la migración es idempotente', str(res2))


def test_serie_mensual_por_fecha_no_por_periodo():
    """
    El bug que hacia que "el ultimo mes" diera negativo.

    `periodo` es el periodo de FACTURACION de ML, no el mes calendario: un
    cargo del 30 de agosto puede venir en el periodo de septiembre. La serie
    mensual agrupaba por `periodo` mientras las ventas entraban por fecha, asi
    que el grafico comparaba las ventas de un mes contra los cargos de otro.
    En produccion el mes en curso mostraba -3.140.168 cuando el septiembre
    real era +330.225. El total anual nunca estuvo mal: la diferencia entre
    meses sumaba exactamente cero, lo que estaba mal era el reparto.
    """
    seccion('Serie mensual: por fecha calendario, no por período de facturación')

    alias = 'SerieMes'
    with webdb.session_scope() as s:
        rid_vta = cont.rubro_id_por_codigo(s, 'VTA_ML')
        rid_com = cont.rubro_id_por_codigo(s, 'COM_ML')
        # Venta del 20 de mayo, periodo de facturacion de mayo
        s.add(Movimiento(
            cuenta_alias=alias, origen=ORIGEN_ML_ORDER, external_id='SM-1',
            periodo='2026-05', fecha=datetime(2026, 5, 20),
            concepto='Venta', monto=Decimal('100000'),
            rubro_id=rid_vta, computable=True))
        # Comision del 30 de mayo que ML factura en el periodo de JUNIO.
        # Es el caso real: fecha en un mes, periodo en el siguiente.
        s.add(Movimiento(
            cuenta_alias=alias, origen='ml_billing', external_id='SM-2',
            periodo='2026-06', fecha=datetime(2026, 5, 30),
            concepto='Cargo por vender', subtipo='CVFV',
            monto=Decimal('-30000'), rubro_id=rid_com, computable=True))

    r = cont.resumen(date(2026, 5, 1), date(2026, 6, 30), cuenta_alias=alias)
    meses = {m['periodo']: m for m in r['por_mes']}

    check(abs(meses['2026-05']['resultado'] - 70000) < 0.01,
          'el cargo cae en el mes de su fecha, no en el de la factura',
          f'mayo dio {meses["2026-05"]["resultado"]}')
    check(abs(meses['2026-06']['resultado']) < 0.01,
          'el mes siguiente no queda cargado con gastos que no son suyos',
          f'junio dio {meses["2026-06"]["resultado"]}')

    # La suma de los meses tiene que dar el total del periodo: si no, el
    # grafico y el resultado se contradicen (que es lo que pasaba).
    suma_meses = sum(m['resultado'] for m in r['por_mes'])
    check(abs(suma_meses - r['totales']['resultado']) < 0.01,
          'la suma de los meses coincide con el resultado del período',
          f'meses {suma_meses} vs total {r["totales"]["resultado"]}')

    # Y el mes por separado tiene que dar lo mismo que el mes dentro del año
    solo_mayo = cont.resumen(date(2026, 5, 1), date(2026, 5, 31),
                             cuenta_alias=alias)['totales']['resultado']
    check(abs(solo_mayo - meses['2026-05']['resultado']) < 0.01,
          'pedir el mes solo da lo mismo que el mes dentro del rango anual',
          f'solo mayo {solo_mayo} vs serie {meses["2026-05"]["resultado"]}')


def test_subtipos_full_y_anulaciones_provinciales():
    """
    Subtipos que quedaron en la bandeja de produccion con 250 pendientes.
    Las anulaciones provinciales de IIBB no empiezan con C sino con I
    (IBNQ ↔ BBNQ), asi que el fallback B→C no las cubria.
    """
    seccion('Subtipos de Full, anulaciones provinciales y otros cargos')

    esperados = {
        'CFBA': 'FULL_ML',    # cargo por stock antiguo en Full
        'CFRS': 'FULL_ML',    # cargo por retiro de stock Full
        'CFPB': 'FULL_ML',    # incumplimiento en Envíos Full
        'CPOPC': 'COM_MP',    # cargo por cobrar con Mercado Pago
        'CRIA': 'FIN_ML',     # adelanto de disponibilidad de dinero
        'CSERRE': 'PERSONAL',  # servicio de restaurantes: no es del negocio
        'BIB': 'IIBB',        # anulación de percepción IIBB Buenos Aires
        'BBNQ': 'IIBB',
        'BBCA': 'IIBB',
        'BBSA': 'IIBB',
        'BIBME': 'IIBB',
        'BIRE': 'PERCEP',     # bonificación de percepción de IVA especial
        'BIVA': 'PERCEP',
    }
    for sub, esperado in esperados.items():
        check(cont.rubro_de_subtipo(sub) == esperado,
              f'{sub} → {esperado}', f'dio {cont.rubro_de_subtipo(sub)}')

    # El fallback B→I resuelve un par nuevo sin tocar la tabla
    check(cont.rubro_de_subtipo('BBCF') == 'IIBB',
          'una anulación provincial nueva se resuelve por el par I',
          f'dio {cont.rubro_de_subtipo("BBCF")}')
    check(cont.rubro_de_subtipo('BZZZZ') is None,
          'un código que no tiene par conocido sigue yendo a pendientes')


def test_reclasificar_billing_por_subtipo():
    """
    Ampliar el mapa no alcanzaba: lo ya importado seguia sin rubro hasta que
    alguien apretaba "Reclasificar". Ahora corre al arrancar.
    """
    seccion('Migración: aplicar el mapa de subtipos a lo ya importado')

    alias = 'RecSub'
    with webdb.session_scope() as s:
        sc = cont.rubro_id_por_codigo(s, RUBRO_SIN_CLASIFICAR)
        s.add(Movimiento(
            cuenta_alias=alias, origen='ml_billing', external_id='RS-1',
            periodo='2026-07', fecha=datetime(2026, 7, 10),
            concepto='Cargo por stock antiguo en Full', subtipo='CFBA',
            monto=Decimal('-2350'), rubro_id=sc, computable=True,
            revisar=True, nota_revision='Subtipo de billing no mapeado: CFBA'))
        # Lo que un humano ya decidio no se toca
        rid_gasto = cont.rubro_id_por_codigo(s, 'GASTO_OTRO')
        s.add(Movimiento(
            cuenta_alias=alias, origen='ml_billing', external_id='RS-2',
            periodo='2026-07', fecha=datetime(2026, 7, 11),
            concepto='Cargo por retiro de stock Full', subtipo='CFRS',
            monto=Decimal('-2975'), rubro_id=rid_gasto, rubro_manual=True,
            computable=True))

    res = cont.reclasificar_billing_por_subtipo()
    check(res['reclasificados'] >= 1,
          'reclasifica el pendiente con subtipo ahora mapeado', str(res))

    with webdb.session_scope() as s:
        m1 = (s.query(Movimiento)
              .filter_by(cuenta_alias=alias, external_id='RS-1').one())
        check(s.get(Rubro, m1.rubro_id).codigo == 'FULL_ML',
              'CFBA quedó en Almacenamiento y servicios Full',
              s.get(Rubro, m1.rubro_id).codigo)
        check(not m1.revisar and not m1.nota_revision,
              'deja de estar marcado para revisar')

        m2 = (s.query(Movimiento)
              .filter_by(cuenta_alias=alias, external_id='RS-2').one())
        check(s.get(Rubro, m2.rubro_id).codigo == 'GASTO_OTRO',
              'no pisa la clasificación manual de un humano',
              s.get(Rubro, m2.rubro_id).codigo)

    res2 = cont.reclasificar_billing_por_subtipo()
    check(res2['reclasificados'] == 0, 'es idempotente', str(res2))


def test_debito_de_factura_ml_no_duplica():
    """
    Lo que planteo el usuario: ML cobra cargo por cargo y ademas debita el
    resumen mensual por Mercado Pago. Si ese debito entra como gasto, se
    cuenta dos veces toda la factura (IIBB, percepciones, publicidad).
    """
    seccion('Débito de la factura mensual de ML: no puede sumar de nuevo')

    alias = 'FactML'
    pago = {
        'id': 77770001,
        'date_approved': '2026-06-08T10:00:00.000-03:00',
        'description': 'Pago de factura de Mercado Libre',
        'transaction_amount': 4200000.0,
        'status': 'approved',
        'currency_id': 'ARS',
        'collector_id': 111111,
        'payer': {'id': 222222},
        'payment_type_id': 'account_money',
    }
    datos = imp._normalizar_pago_mp(pago, '222222', alias)
    check(datos['rubro_sugerido'] == 'CONCIL_FACT_ML',
          'el débito de la factura va al rubro neutro',
          str(datos.get('rubro_sugerido')))
    check(datos['computable'] is False,
          'queda no computable: no puede inflar los gastos')
    check(datos['revisar'] is True,
          'queda marcado para revisar, no se decide en silencio')

    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)
        cont.upsert_movimiento(s, datos, reglas)

    r = cont.resumen(date(2026, 6, 1), date(2026, 6, 30), cuenta_alias=alias)
    check(abs(r['totales']['resultado']) < 0.01,
          'pagar la factura no cambia el resultado del mes',
          str(r['totales']))
    check(r['totales']['impuestos'] == 0,
          'el IIBB de la factura no se cuenta por segunda vez',
          str(r['totales']['impuestos']))

    # Un gasto real que NO es la factura sigue entrando como gasto
    otro = imp._normalizar_pago_mp({
        'id': 77770002,
        'date_approved': '2026-06-09T10:00:00.000-03:00',
        'description': 'Pago a proveedor Distribuidora Sur',
        'transaction_amount': 500000.0, 'status': 'approved',
        'currency_id': 'ARS', 'collector_id': 333333,
        'payer': {'id': 222222}, 'payment_type_id': 'account_money',
    }, '222222', alias)
    check(otro.get('rubro_sugerido') != 'CONCIL_FACT_ML',
          'un pago a proveedor no se confunde con la factura de ML',
          str(otro.get('rubro_sugerido')))
    check(otro['computable'] is True,
          'y sigue siendo computable')


def main():
    print('═' * 70)
    print('TESTS DEL SISTEMA CONTABLE')
    print('═' * 70)

    test_semilla()
    test_direccion_mp()
    test_signos_billing()
    test_ordenes()
    test_idempotencia_y_resumen()
    test_bandeja_pendientes()
    test_costos_y_cmv()
    test_items_sin_costo()
    test_gasto_manual()
    test_multicuenta()
    test_rango_periodos()
    test_limite_billing()
    test_token_mp_en_campo_equivocado()
    test_subtipos_reales_mla()
    test_reclasificar_aplica_mapa_de_subtipos()
    test_importar_costos_del_sistema()
    test_percepciones_contra_esquema_real()
    test_limpieza_percepciones_viejas()
    test_percepciones_no_se_duplican_con_facturacion()
    test_neutralizar_resumen_percepciones()
    test_serie_mensual_por_fecha_no_por_periodo()
    test_subtipos_full_y_anulaciones_provinciales()
    test_reclasificar_billing_por_subtipo()
    test_debito_de_factura_ml_no_duplica()

    print('\n' + '═' * 70)
    print(f'PASARON: {len(PASADOS)}    FALLARON: {len(FALLOS)}')
    print('═' * 70)
    if FALLOS:
        print('\nFallas:')
        for desc, det in FALLOS:
            print(f'  · {desc}')
            if det:
                print(f'      {det}')
        return 1
    print('\nTodo verde.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
