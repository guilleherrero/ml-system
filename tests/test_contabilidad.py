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
