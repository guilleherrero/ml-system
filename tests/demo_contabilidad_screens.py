"""
Levanta las pantallas de contabilidad con datos de demostración y saca capturas.

No es un test: es para ver cómo queda la UI sin tener que deployar. Los montos
son inventados pero con la forma y el orden de magnitud reales de la cuenta.

Correr:  python3 tests/demo_contabilidad_screens.py
Salida:  /mnt/user-data/outputs/contabilidad_*.png
"""
import calendar
import os
import random
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

_tmp = tempfile.mkdtemp(prefix='cont_demo_')
os.environ['DATABASE_URL'] = ''
os.environ['CONT_DEMO_DB'] = os.path.join(_tmp, 'demo.db')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import web.db as webdb  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker, scoped_session  # noqa: E402

webdb.DATABASE_URL = 'sqlite:///' + os.environ['CONT_DEMO_DB']
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

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SALIDA = '/mnt/user-data/outputs'
ALIAS = 'Novara'
MI_ID = '123456789'

# Catálogo con la forma real de la cuenta
PRODUCTOS = [
    ('MLA1481911017', 'Cortador De Puntas Abriertas Para Cabello Dañado - Ender Pro Negro', 60578, 21500),
    ('MLA3024679994', 'Cepillo Secador Y Alisador Usb Portatil Termica Recargable Rosa', 40850, 14200),
    ('MLA2240715828', 'Delineador De Cejas Perfilado Microblading Eyebrow Regina Negro', 12000, 3100),
    ('MLA1932975847', 'Parches Tratamiento Hongos En Las Unas Cuidado 32', 26000, 7800),
    ('MLA4410233781', 'Dilatador Nasal Magnetico Antirronquido Kit Tiras Iman Nariz', 9674, 2400),
    ('MLA5521098447', 'Faja Reductora Mujer Compresion Cintura Postparto Negro L', 29000, 11200),
    ('MLA6633471102', 'Trusa Modeladora Sin Costura Biobella Reductora Mujer Negro M', 15000, 5600),
    ('MLA7741220983', 'Crema Blanqueadora Corporal Snow Bleach Aclarante De Piel', 20000, None),
    ('MLA8852334091', 'Parche Cicatriz Silicona Medica Cesarea Queloide 12x3 Cm', 28722, None),
]


class CuentaFalsa:
    def __init__(self, alias):
        self.alias = alias

    def get(self, k, d=None):
        return {'alias': self.alias}.get(k, d)

    def __getitem__(self, k):
        return {'alias': self.alias}[k]


def sembrar():
    """Nueve meses de operación: ventas, comisiones, envíos, Ads, impuestos."""
    random.seed(7)
    cont.sembrar_plan()

    with webdb.session_scope() as s:
        # Costos: se dejan dos productos sin costo a propósito, para que se vea
        # el aviso de que el resultado está sobreestimado.
        for item_id, titulo, _precio, costo in PRODUCTOS:
            if costo:
                s.add(CostoProducto(item_id=item_id, variacion='', titulo=titulo,
                                    costo_unitario=Decimal(costo),
                                    vigente_desde=date(2026, 1, 1),
                                    origen_dato='excel'))

    hoy = date(2026, 9, 12)
    orden_seq = 2000018000000000
    detalle_seq = 500000000

    with webdb.session_scope() as s:
        reglas = cont.cargar_reglas(s)

        for mes in range(1, 10):
            # Días reales del mes (febrero tiene 28 y 2026 no es bisiesto)
            dias_mes = 12 if mes == 9 else calendar.monthrange(2026, mes)[1]
            # Crecimiento a lo largo del año, con un bajón en julio
            volumen = {1: 38, 2: 41, 3: 52, 4: 58, 5: 63,
                       6: 71, 7: 49, 8: 84, 9: 36}[mes]

            for _ in range(volumen):
                item_id, titulo, precio, _c = random.choice(PRODUCTOS)
                cantidad = random.choice([1, 1, 1, 2])
                dia = random.randint(1, dias_mes)
                fecha = datetime(2026, mes, dia,
                                 random.randint(8, 22), random.randint(0, 59))
                orden_seq += random.randint(100, 9000)
                total = precio * cantidad
                cancelada = random.random() < 0.045

                orden = {
                    'id': orden_seq,
                    'status': 'cancelled' if cancelada else 'paid',
                    'date_created': fecha.isoformat(),
                    'date_closed': fecha.isoformat(),
                    'total_amount': float(total), 'paid_amount': float(total),
                    'currency_id': 'ARS',
                    'order_items': [{
                        'item': {'id': item_id, 'title': titulo},
                        'quantity': cantidad, 'unit_price': float(precio),
                        'sale_fee': round(precio * 0.17, 2),
                    }],
                    'payments': [],
                }
                # Retenciones en una de cada cinco ventas
                if not cancelada and random.random() < 0.2:
                    orden['payments'] = [{
                        'id': orden_seq + 1,
                        'date_approved': fecha.isoformat(),
                        'tax_details': [
                            {'mov_detail': 'tax_withholding',
                             'mov_financial_entity': 'retencion_iva',
                             'original_amount': round(total * 0.03, 2),
                             'refunded_amount': 0, 'tax_status': 'applied'},
                            {'mov_detail': 'tax_withholding_collector',
                             'mov_financial_entity': 'debitos_creditos',
                             'original_amount': round(total * 0.006, 2),
                             'refunded_amount': 0, 'tax_status': 'applied'},
                        ],
                    }]

                for datos in imp._normalizar_orden(orden, ALIAS):
                    cont.upsert_movimiento(s, datos, reglas)

                if cancelada:
                    continue

                # Comisión por venta y cargo de envío
                for subtipo, detalle, monto in (
                    ('CV', 'Cargo por venta', round(total * 0.17, 2)),
                    ('CXD', 'Cargo por Mercado Envios', round(total * 0.07, 2)),
                ):
                    detalle_seq += 1
                    bill = {
                        'charge_info': {
                            'creation_date_time': fecha.isoformat(),
                            'detail_id': detalle_seq,
                            'transaction_detail': detalle,
                            'detail_amount': monto,
                            'detail_type': 'CHARGE', 'detail_sub_type': subtipo,
                            'legal_document_number': f'0011A{mes:02d}00{detalle_seq % 10000:04d}',
                        },
                        'sales_info': [{'order_id': orden['id']}],
                        'items_info': [{'item_id': item_id, 'item_title': titulo,
                                        'item_amount': cantidad}],
                        'currency_info': {'currency_id': 'ARS'},
                    }
                    cont.upsert_movimiento(
                        s, imp._normalizar_detalle_billing(
                            bill, ALIAS, 'ML', f'2026-{mes:02d}-01'), reglas)

            # Product Ads del mes
            detalle_seq += 1
            cont.upsert_movimiento(s, imp._normalizar_detalle_billing({
                'charge_info': {
                    'creation_date_time': datetime(2026, mes, min(28, dias_mes)).isoformat(),
                    'detail_id': detalle_seq,
                    'transaction_detail': 'Campanas de publicidad - Product Ads',
                    'detail_amount': round(random.uniform(90000, 260000), 2),
                    'detail_type': 'CHARGE', 'detail_sub_type': 'PADS',
                },
                'currency_info': {'currency_id': 'ARS'},
            }, ALIAS, 'ML', f'2026-{mes:02d}-01'), reglas)

            # Almacenamiento Full
            detalle_seq += 1
            cont.upsert_movimiento(s, imp._normalizar_detalle_billing({
                'charge_info': {
                    'creation_date_time': datetime(2026, mes, min(27, dias_mes)).isoformat(),
                    'detail_id': detalle_seq,
                    'transaction_detail': 'Cargo por almacenamiento Full',
                    'detail_amount': round(random.uniform(18000, 52000), 2),
                    'detail_type': 'CHARGE', 'detail_sub_type': 'CFWA',
                    'concept_type': 'FULFILLMENT',
                },
                'currency_info': {'currency_id': 'ARS'},
            }, ALIAS, 'ML', f'2026-{mes:02d}-01'), reglas)

            # Un subtipo desconocido, para que se vea la bandeja de pendientes
            if mes in (4, 8):
                detalle_seq += 1
                cont.upsert_movimiento(s, imp._normalizar_detalle_billing({
                    'charge_info': {
                        'creation_date_time': datetime(2026, mes, 15).isoformat(),
                        'detail_id': detalle_seq,
                        'transaction_detail': 'Cargo por servicio nuevo de ML',
                        'detail_amount': 7400.0,
                        'detail_type': 'CHARGE', 'detail_sub_type': 'CNUEVO2026',
                    },
                    'currency_info': {'currency_id': 'ARS'},
                }, ALIAS, 'ML', f'2026-{mes:02d}-01'), reglas)

            # Consumos personales desde la cuenta de MP
            for desc, monto in (('Compra en DIA', random.uniform(9000, 24000)),
                                ('Edenor', random.uniform(60000, 95000)),
                                ('SUBE', 20000.0)):
                cont.upsert_movimiento(s, imp._normalizar_pago_mp({
                    'id': 900000000 + mes * 1000 + int(monto) % 997,
                    'status': 'approved',
                    'date_approved': datetime(2026, mes, random.randint(3, 25)).isoformat(),
                    'description': desc, 'transaction_amount': round(monto, 2),
                    'currency_id': 'ARS', 'payment_type_id': 'account_money',
                    'collector_id': 777, 'payer': {'id': int(MI_ID)},
                }, MI_ID, ALIAS), reglas)

    # Gastos cargados a mano
    for mes, rubro, concepto, monto, prov in (
        (2, 'PROV', 'Pago a proveedor China - lote febrero', 4200000, 'Shenzhen Beauty Co'),
        (5, 'PROV', 'Pago a proveedor China - lote mayo', 5800000, 'Guangzhou Hair Tools'),
        (3, 'IIBB', 'IIBB Convenio Multilateral - marzo', 186000, None),
        (6, 'IIBB', 'IIBB Convenio Multilateral - junio', 241000, None),
        (4, 'IMPORT', 'Flete y despacho lote febrero', 1350000, 'Despachante Luna'),
        (7, 'SUELDOS', 'Sueldos y cargas - julio', 1900000, None),
        (8, 'SERVICIOS', 'Internet y telefonia del deposito', 96000, 'Telecentro'),
        (8, 'SOFTWARE', 'Render + APIs', 74000, None),
        (9, 'ALQUILER', 'Alquiler del deposito - septiembre', 720000, None),
    ):
        cont.registrar_gasto_manual(
            cuenta_alias=ALIAS, fecha=date(2026, mes, 10), rubro_codigo=rubro,
            concepto=concepto, monto=monto, proveedor=prov,
            tipo_comprobante='Factura A' if prov else None,
            medio_pago='Transferencia',
        )

    res = cierre.aplicar_cmv(date(2026, 1, 1), hoy)
    print(f'  CMV aplicado: {res["generados"]} movimientos, '
          f'{res["ventas_sin_costo"]} ventas sin costo')

    r = cont.resumen(date(2026, 1, 1), hoy)
    print(f'  Ingresos: {r["totales"]["ingresos"]:,.0f}')
    print(f'  Resultado: {r["totales"]["resultado"]:,.0f}')
    print(f'  Pendientes: {r["pendientes_de_clasificar"]}')


def armar_app():
    app = Flask(__name__,
                template_folder=os.path.join(RAIZ, 'web', 'templates'),
                static_folder=os.path.join(RAIZ, 'web', 'static'))
    app.secret_key = 'demo'
    rutas._accounts = lambda: [CuentaFalsa('Novara'), CuentaFalsa('Cuenta Hijos')]
    app.register_blueprint(rutas.bp)
    return app


def capturar():
    from playwright.sync_api import sync_playwright

    os.makedirs(SALIDA, exist_ok=True)
    app = armar_app()

    servidor = threading.Thread(
        target=lambda: app.run(port=5099, debug=False, use_reloader=False),
        daemon=True)
    servidor.start()
    time.sleep(2.5)

    pantallas = [
        ('contabilidad_1_resumen.png',
         'http://127.0.0.1:5099/contabilidad/?desde=2026-01-01&hasta=2026-09-12', 2400),
        ('contabilidad_2_libro.png',
         'http://127.0.0.1:5099/contabilidad/movimientos?desde=2026-01-01&hasta=2026-09-12', 1500),
        ('contabilidad_3_pendientes.png',
         'http://127.0.0.1:5099/contabilidad/pendientes', 1100),
        ('contabilidad_4_costos.png',
         'http://127.0.0.1:5099/contabilidad/costos', 1400),
        ('contabilidad_5_gastos.png',
         'http://127.0.0.1:5099/contabilidad/gastos', 1500),
        ('contabilidad_6_importar.png',
         'http://127.0.0.1:5099/contabilidad/importar', 1400),
    ]

    # El CDN de Bootstrap/Chart.js no se alcanza desde el contenedor de
    # desarrollo, asi que se interceptan esas requests y se sirven las copias
    # locales. En produccion las baja el navegador del usuario normalmente.
    VENDOR = '/tmp/vendor'
    MAPA = {
        'bootstrap.min.css': ('bootstrap.min.css', 'text/css'),
        'bootstrap-icons.min.css': ('bootstrap-icons.min.css', 'text/css'),
        'chart.umd.min.js': ('chart.umd.min.js', 'application/javascript'),
        'bootstrap.bundle.min.js': (None, 'application/javascript'),
    }

    def servir_local(route):
        url = route.request.url
        for clave, (archivo, tipo) in MAPA.items():
            if url.endswith(clave):
                if archivo is None:
                    return route.fulfill(status=200, content_type=tipo, body='')
                ruta = os.path.join(VENDOR, archivo)
                if os.path.exists(ruta):
                    with open(ruta, 'rb') as fh:
                        return route.fulfill(status=200, content_type=tipo,
                                             body=fh.read())
        return route.fulfill(status=200, content_type='text/plain', body='')

    with sync_playwright() as p:
        navegador = p.chromium.launch(executable_path='/opt/pw-browsers/chromium')
        for nombre, url, alto in pantallas:
            pagina = navegador.new_page(viewport={'width': 1440, 'height': alto},
                                        device_scale_factor=2)
            pagina.route('**cdn.jsdelivr.net/**', servir_local)
            pagina.goto(url, wait_until='networkidle', timeout=30000)
            pagina.wait_for_timeout(1400)  # que termine de dibujar el gráfico
            destino = os.path.join(SALIDA, nombre)
            pagina.screenshot(path=destino)
            print(f'  {nombre}')
            pagina.close()
        navegador.close()


if __name__ == '__main__':
    print('Sembrando nueve meses de operación…')
    sembrar()
    print('Capturando pantallas…')
    capturar()
    print('Listo.')
