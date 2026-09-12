"""
Test de renderizado de las pantallas de contabilidad.

No prueba lógica de negocio (eso es test_contabilidad.py): prueba que las siete
plantillas se rendericen de verdad, con datos y vacías, y que los formularios
hagan lo que dicen. Un template Jinja roto no se detecta con un chequeo de
sintaxis Python — solo se ve cuando se renderiza.

Correr:  python3 tests/test_contabilidad_ui.py
"""
import os
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal

_tmpdir = tempfile.mkdtemp(prefix='cont_ui_')
os.environ['DATABASE_URL'] = ''
os.environ['CONT_TEST_DB'] = os.path.join(_tmpdir, 'ui.db')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import web.db as webdb  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker, scoped_session  # noqa: E402

webdb.DATABASE_URL = 'sqlite:///' + os.environ['CONT_TEST_DB']
webdb.engine = create_engine(webdb.DATABASE_URL, future=True,
                             connect_args={'check_same_thread': False})
webdb.SessionFactory = sessionmaker(bind=webdb.engine, autoflush=False,
                                    autocommit=False, future=True)
webdb.Session = scoped_session(webdb.SessionFactory)

from flask import Flask  # noqa: E402

from web.models_contabilidad import CostoProducto  # noqa: E402
from modules import contabilidad as cont  # noqa: E402
from modules import contabilidad_import as imp  # noqa: E402
from modules import contabilidad_cierre as cierre  # noqa: E402
from web import contabilidad_routes as rutas  # noqa: E402

webdb.Base.metadata.create_all(webdb.engine)

FALLOS, PASADOS = [], []


def check(cond, desc, detalle=''):
    if cond:
        PASADOS.append(desc)
        print(f'  ok    {desc}')
    else:
        FALLOS.append((desc, detalle))
        print(f'  FALLA {desc}' + (f'\n          → {detalle}' if detalle else ''))


def seccion(t):
    print(f'\n── {t} ' + '─' * max(0, 62 - len(t)))


# ── App de prueba ────────────────────────────────────────────────────────────

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class CuentaFalsa:
    """Lo mínimo que la sidebar de base.html le pide a una cuenta."""
    def __init__(self, alias):
        self.alias = alias

    def get(self, clave, default=None):
        return {'alias': self.alias}.get(clave, default)

    def __getitem__(self, clave):
        return {'alias': self.alias}[clave]


def armar_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(RAIZ, 'web', 'templates'),
        static_folder=os.path.join(RAIZ, 'web', 'static'),
    )
    app.secret_key = 'test'
    app.config['WTF_CSRF_ENABLED'] = False
    # La sidebar pide cuentas; en el test no arrancamos app.py entero.
    rutas._accounts = lambda: [CuentaFalsa('Novara'), CuentaFalsa('Cuenta2')]
    app.register_blueprint(rutas.bp)
    return app


# ── Fixtures ─────────────────────────────────────────────────────────────────

MI_ID, OTRO_ID, ALIAS = '123456789', '999888777', 'Novara'

ORDEN = {
    'id': 2000018424423218, 'status': 'paid',
    'date_created': '2026-03-12T13:34:20.000-03:00',
    'date_closed': '2026-03-12T13:34:24.000-03:00',
    'total_amount': 68400.0, 'paid_amount': 68400.0, 'currency_id': 'ARS',
    'order_items': [{
        'item': {'id': 'MLA3024679994', 'title': 'Cortador De Puntas Negro'},
        'quantity': 2, 'unit_price': 34200.0, 'sale_fee': 6000.25,
    }],
    'payments': [{
        'id': 177713012057, 'date_approved': '2026-03-12T13:34:24.000-03:00',
        'tax_details': [{
            'mov_detail': 'tax_withholding',
            'mov_financial_entity': 'retencion_iva',
            'original_amount': 2018.99, 'refunded_amount': 0,
            'tax_status': 'applied',
        }],
    }],
}

BILLING_CV = {
    'charge_info': {
        'creation_date_time': '2026-03-12T13:35:02', 'detail_id': 5555566666,
        'transaction_detail': 'Cargo por venta', 'detail_amount': 12000.50,
        'detail_type': 'CHARGE', 'detail_sub_type': 'CV',
        'legal_document_number': '0011A03800000',
    },
    'sales_info': [{'order_id': 2000018424423218, 'operation_id': 177713012057}],
    'items_info': [{'item_id': 'MLA3024679994', 'item_amount': 1}],
    'currency_info': {'currency_id': 'ARS'},
}

BILLING_RARO = {
    'charge_info': {
        'creation_date_time': '2026-03-14T10:00:00', 'detail_id': 9999900000,
        'transaction_detail': 'Cargo por concepto nuevo', 'detail_amount': 4200.0,
        'detail_type': 'CHARGE', 'detail_sub_type': 'CXYZNUEVO',
    },
    'currency_info': {'currency_id': 'ARS'},
}

PAGO_DIA = {
    'id': 177945637628, 'status': 'approved',
    'date_approved': '2026-03-08T13:45:19.000-03:00',
    'description': 'Compra en DIA', 'transaction_amount': 12450.0,
    'currency_id': 'ARS', 'payment_type_id': 'account_money',
    'collector_id': int(OTRO_ID), 'payer': {'id': int(MI_ID)},
}


def sembrar_datos():
    cont.sembrar_plan()
    lote = [
        imp._normalizar_detalle_billing(BILLING_CV, ALIAS, 'ML', '2026-03-01'),
        imp._normalizar_detalle_billing(BILLING_RARO, ALIAS, 'ML', '2026-03-01'),
        imp._normalizar_pago_mp(PAGO_DIA, MI_ID, ALIAS),
    ]
    lote += imp._normalizar_orden(ORDEN, ALIAS)
    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)
        for d in lote:
            cont.upsert_movimiento(s, d, reglas)
        s.add(CostoProducto(item_id='MLA9999999999', variacion='',
                            costo_unitario=Decimal('100'),
                            vigente_desde=date(2026, 1, 1)))
    cont.registrar_gasto_manual(
        cuenta_alias=ALIAS, fecha=date(2026, 3, 20), rubro_codigo='PROV',
        concepto='Pago a proveedor China', monto=1500000,
        proveedor='Shenzhen Beauty Co', nro_comprobante='A-0001',
    )


# ── Tests ────────────────────────────────────────────────────────────────────

PANTALLAS = [
    ('/contabilidad/', 'Resumen'),
    ('/contabilidad/movimientos', 'Libro'),
    ('/contabilidad/pendientes', 'Pendientes'),
    ('/contabilidad/gastos', 'Cargar gasto'),
    ('/contabilidad/costos', 'Costos'),
    ('/contabilidad/importar', 'Importar'),
    ('/contabilidad/cierre', 'Cierre'),
]


def test_render_vacio(cliente):
    seccion('Renderizado sin datos (estado inicial)')
    for url, nombre in PANTALLAS:
        r = cliente.get(url)
        cuerpo = r.get_data(as_text=True)
        check(r.status_code == 200, f'{nombre} responde 200 sin datos',
              f'{url} → {r.status_code}\n{cuerpo[:400]}')
        if r.status_code == 200:
            check('Contabilidad' in cuerpo and 'jinja' not in cuerpo.lower(),
                  f'{nombre} renderiza la cabecera')


def test_render_con_datos(cliente):
    seccion('Renderizado con datos')
    r = cliente.get('/contabilidad/?desde=2026-01-01&hasta=2026-12-31')
    cuerpo = r.get_data(as_text=True)
    check(r.status_code == 200, 'el resumen responde 200 con datos',
          cuerpo[:400] if r.status_code != 200 else '')
    check('$68.400' in cuerpo, 'el resumen muestra los ingresos formateados a la argentina',
          'no encontré $68.400 en el HTML')
    check('-$1.500.000' in cuerpo,
          'un egreso se muestra con el signo negativo explícito')
    check('graficoMeses' in cuerpo, 'se monta el gráfico mensual cuando hay datos')
    check('no es ganancia' in cuerpo or 'NO ganancia' in cuerpo,
          'el resumen advierte que sin costo de mercadería no es ganancia')

    r = cliente.get('/contabilidad/movimientos?desde=2026-01-01&hasta=2026-12-31')
    cuerpo = r.get_data(as_text=True)
    check(r.status_code == 200, 'el libro responde 200')
    check('Cortador De Puntas' in cuerpo, 'el libro lista la venta importada')
    check('Compra en DIA' in cuerpo, 'el libro lista el movimiento personal')
    check('personal' in cuerpo, 'el movimiento personal aparece etiquetado como tal')

    r = cliente.get('/contabilidad/pendientes')
    cuerpo = r.get_data(as_text=True)
    check(r.status_code == 200, 'la bandeja responde 200')
    check('CXYZNUEVO' in cuerpo,
          'el subtipo desconocido aparece en la bandeja de pendientes')

    r = cliente.get('/contabilidad/costos')
    cuerpo = r.get_data(as_text=True)
    check('MLA3024679994' in cuerpo,
          'la pantalla de costos lista la publicación vendida sin costo')


def test_filtros(cliente):
    seccion('Filtros del libro')
    r = cliente.get('/contabilidad/movimientos?desde=2026-01-01&hasta=2026-12-31'
                    '&ambito=personal')
    cuerpo = r.get_data(as_text=True)
    check(r.status_code == 200 and 'Compra en DIA' in cuerpo,
          'el filtro por ámbito personal trae el consumo personal')
    check('Cortador De Puntas' not in cuerpo,
          'el filtro por ámbito personal excluye las ventas')

    r = cliente.get('/contabilidad/movimientos?desde=2026-01-01&hasta=2026-12-31'
                    '&rubro=SIN_CLASIF')
    check(r.status_code == 200 and 'CXYZNUEVO' in r.get_data(as_text=True),
          'el filtro por rubro sin clasificar funciona')

    r = cliente.get('/contabilidad/movimientos?desde=2026-01-01&hasta=2026-12-31'
                    '&q=Shenzhen')
    check(r.status_code == 200 and 'proveedor' in r.get_data(as_text=True).lower(),
          'la búsqueda por texto encuentra por proveedor')

    r = cliente.get('/contabilidad/movimientos?desde=2026-01-01&hasta=2026-12-31'
                    '&cuenta=Cuenta2')
    check(r.status_code == 200 and 'No hay movimientos' in r.get_data(as_text=True),
          'filtrar por una cuenta sin movimientos muestra el estado vacío')


def test_asignar_rubro(cliente):
    seccion('Asignar rubro desde la bandeja')
    pend = cont.pendientes()
    objetivo = [p for p in pend if p['subtipo'] == 'CXYZNUEVO']
    check(bool(objetivo), 'hay un pendiente para asignar')
    if not objetivo:
        return

    mov_id = objetivo[0]['id']
    r = cliente.post('/contabilidad/api/asignar-rubro',
                     json={'movimiento_id': mov_id, 'rubro': 'COM_ML',
                           'crear_regla': 1})
    datos = r.get_json() or {}
    check(r.status_code == 200 and datos.get('ok'),
          'la API asigna el rubro', str(datos))
    check(datos.get('regla_creada'), 'la API crea la regla para los próximos iguales')

    r = cliente.post('/contabilidad/api/asignar-rubro',
                     json={'movimiento_id': mov_id, 'rubro': 'NO_EXISTE'})
    check(r.status_code == 400, 'un rubro inexistente devuelve 400, no 500',
          f'status {r.status_code}')

    r = cliente.post('/contabilidad/api/asignar-rubro', json={'movimiento_id': 'x'})
    check(r.status_code == 400, 'un id inválido devuelve 400')


def test_alta_gasto(cliente):
    seccion('Alta de gasto por el formulario')
    r = cliente.post('/contabilidad/gastos', data={
        'fecha': '2026-04-05', 'cuenta': ALIAS, 'rubro': 'SERVICIOS',
        'concepto': 'Internet del depósito', 'monto': '85.000,50',
        'proveedor': 'Telecentro', 'tipo_comprobante': 'Factura A',
        'medio_pago': 'Débito automático',
    }, follow_redirects=True)
    check(r.status_code == 200, 'el formulario de gasto responde 200')

    datos = cont.listar_movimientos(desde=date(2026, 4, 1), hasta=date(2026, 4, 30),
                                    origen='manual')
    nuevo = [m for m in datos['movimientos'] if 'Internet' in (m['concepto'] or '')]
    check(bool(nuevo), 'el gasto quedó guardado')
    if nuevo:
        check(abs(nuevo[0]['monto'] + 85000.50) < 0.01,
              'el monto con formato argentino "85.000,50" se parsea y queda negativo',
              f'monto: {nuevo[0]["monto"]}')

    # Un rubro inválido no debe tirar 500
    r = cliente.post('/contabilidad/gastos', data={
        'fecha': '2026-04-05', 'rubro': '', 'concepto': 'x', 'monto': '10',
    }, follow_redirects=True)
    check(r.status_code == 200,
          'un gasto con rubro vacío muestra el error sin romper la página')


def test_carga_costos(cliente):
    seccion('Carga masiva de costos por el formulario')
    pegado = ('item_id\ttitulo\tcosto\n'
              'MLA3024679994\tCortador De Puntas\t$ 18.500,50\n'
              'NOESUNMLA\tInvalido\t500\n')
    r = cliente.post('/contabilidad/costos', data={
        'pegado': pegado, 'vigente_desde': '2026-01-01',
    }, follow_redirects=True)
    cuerpo = r.get_data(as_text=True)
    check(r.status_code == 200, 'la carga de costos responde 200')
    check('Rechazados' in cuerpo or 'rechazad' in cuerpo.lower(),
          'la pantalla informa las filas rechazadas')

    with webdb.session_scope() as s:
        c = s.query(CostoProducto).filter_by(item_id='MLA3024679994').one_or_none()
        check(c is not None and c.costo_unitario == Decimal('18500.50'),
              'el costo se guardó con el valor correcto',
              f'costo: {c.costo_unitario if c else None}')

    # Con el costo cargado, el CMV se aplica y el aviso crítico desaparece
    r = cliente.get('/contabilidad/?desde=2026-01-01&hasta=2026-12-31')
    cuerpo = r.get_data(as_text=True)
    check('NO ganancia' not in cuerpo and 'no es ganancia' not in cuerpo,
          'cargado el costo, desaparece el aviso de que no es ganancia')
    check('-$37.001' in cuerpo,
          'el resumen muestra el CMV de las 2 unidades (37.001)',
          'no encontré -$37.001')

    # Enviar vacío no debe romper
    r = cliente.post('/contabilidad/costos', data={'pegado': ''},
                     follow_redirects=True)
    check(r.status_code == 200, 'enviar el formulario de costos vacío no rompe')


def test_cuenta_mp(cliente):
    seccion('Alta de cuenta de Mercado Pago')
    r = cliente.post('/contabilidad/cuentas-mp', data={
        'alias': 'Novara', 'ml_alias': 'Novara',
        'token_env': 'MP_ACCESS_TOKEN_PROD',
    }, follow_redirects=True)
    check(r.status_code == 200, 'el alta de cuenta MP responde 200')
    cuentas = cont.listar_cuentas_mp()
    check(any(c['alias'] == 'Novara' for c in cuentas),
          'la cuenta de MP quedó guardada', str(cuentas))
    check(all('token_env' not in c and 'access_token' not in c for c in cuentas),
          'el listado NO expone el valor ni el nombre del token', str(cuentas))

    # El campo acepta un token pegado: tiene que guardarlo como token y avisar
    r = cliente.post('/contabilidad/cuentas-mp', data={
        'alias': 'CuentaPegada', 'ml_alias': 'Novara',
        'token_env': 'APP_USR-1234567890123456-091213-abcdef0123456789abcdef0123456789-55993545',
    }, follow_redirects=True)
    cuerpo = r.get_data(as_text=True)
    check(r.status_code == 200, 'pegar el token en el campo no rompe')
    check('token' in cuerpo.lower(),
          'la pantalla avisa que interpretó el valor como token')
    pegada = [c for c in cont.listar_cuentas_mp() if c['alias'] == 'CuentaPegada']
    check(pegada and pegada[0]['tiene_token'],
          'la cuenta con el token pegado queda con token válido', str(pegada))
    check(pegada and pegada[0]['fuente_token'] == 'guardado en la base',
          'informa que el token quedó en la base, no en una variable',
          str(pegada))
    check('APP_USR-1234567890123456' not in cuerpo,
          'el token pegado NO se vuelve a imprimir en la pantalla')

    # Alias vacío: error manejado, no 500
    r = cliente.post('/contabilidad/cuentas-mp', data={'alias': ''},
                     follow_redirects=True)
    check(r.status_code == 200, 'un alias vacío se maneja sin romper')


def test_api_resumen(cliente):
    seccion('API de resumen (para el gráfico y el MCP)')
    r = cliente.get('/contabilidad/api/resumen?desde=2026-01-01&hasta=2026-12-31')
    datos = r.get_json() or {}
    check(r.status_code == 200 and 'totales' in datos,
          'la API devuelve el resumen en JSON', str(datos)[:200])
    check(datos.get('criterio') == 'caja_pura',
          'la API declara el criterio contable usado')
    check(len(datos.get('por_mes') or []) == 12,
          'la serie mensual del año trae 12 períodos',
          f'{len(datos.get("por_mes") or [])} períodos')


def test_rango_invertido(cliente):
    seccion('Robustez de los parámetros')
    r = cliente.get('/contabilidad/?desde=2026-12-31&hasta=2026-01-01')
    check(r.status_code == 200, 'un rango invertido se corrige en vez de romper')
    r = cliente.get('/contabilidad/?desde=no-es-fecha&hasta=tampoco')
    check(r.status_code == 200, 'una fecha basura cae al default sin romper')
    r = cliente.get('/contabilidad/movimientos?pagina=999999')
    check(r.status_code == 200, 'una página fuera de rango no rompe')
    r = cliente.get('/contabilidad/movimientos?pagina=abc')
    check(r.status_code == 200, 'una página no numérica no rompe')


def main():
    print('═' * 70)
    print('TESTS DE LAS PANTALLAS DE CONTABILIDAD')
    print('═' * 70)

    app = armar_app()
    with app.test_client() as cliente:
        cont.sembrar_plan()
        test_render_vacio(cliente)
        sembrar_datos()
        test_render_con_datos(cliente)
        test_filtros(cliente)
        test_asignar_rubro(cliente)
        test_alta_gasto(cliente)
        test_carga_costos(cliente)
        test_cuenta_mp(cliente)
        test_api_resumen(cliente)
        test_rango_invertido(cliente)

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
