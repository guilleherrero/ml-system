"""
Sistema contable — plan de rubros, clasificador y agregación.

Responde la pregunta que el resto del sistema no responde: cuánto gana el
negocio, por mes y por año, después de comisiones, envíos, publicidad,
impuestos, costo de mercadería y gastos.

Reglas de diseño:

1. UN SOLO LIBRO. Todo movimiento va a `cont_movimientos`. Ver el docstring
   de web/models_contabilidad.py.

2. NADA SE DESCARTA EN SILENCIO. Lo que el clasificador no reconoce queda con
   rubro NULL y sale en la bandeja de pendientes. Un subtipo nuevo de ML no
   hace que el total quede mal sin aviso: hace que aparezca un pendiente.

3. LAS VENTAS SE CUENTAN UNA SOLA VEZ. El ingreso por venta sale de las
   órdenes de ML (`ml_order`). El cobro espejo en Mercado Pago se importa
   igual — hace falta para conciliar y para el flujo de caja real — pero cae
   en el rubro neutro CONCIL_MP, que no afecta resultado. Así no se duplica.

4. SIGNO EN EL DATO. `monto` viene firmado (+ entra, - sale). El resultado es
   una suma, no un armado de restas por rubro.

Criterio contable vigente: CAJA PURA (todo bruto, tal como se movió la plata).
El IVA y las percepciones se guardan igual en columnas aparte, para poder
agregar la vista neta a futuro sin reimportar nada.
"""
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal
import re

from sqlalchemy import func, or_

from web.db import session_scope
from web.models_contabilidad import (
    AMBITO_NEGOCIO, AMBITO_PERSONAL,
    ORIGEN_MANUAL, ORIGEN_ML_BILLING, ORIGEN_ML_ORDER, ORIGEN_ML_PERCEPCION,
    ORIGEN_MP_PAYMENT,
    RUBRO_SIN_CLASIFICAR,
    TIPO_COSTO, TIPO_FINANCIERO, TIPO_GASTO, TIPO_IMPUESTO, TIPO_INGRESO,
    TIPO_NEUTRO,
    CostoProducto, CuentaMP, ImportRun, Movimiento, ReglaClasificacion, Rubro,
)


# ══════════════════════════════════════════════════════════════════════════════
# PLAN DE RUBROS
# ══════════════════════════════════════════════════════════════════════════════
# (codigo, nombre, tipo, grupo, afecta_resultado, ambito, orden)

PLAN_RUBROS = [
    # ── Ingresos ──
    ('VTA_ML',        'Ventas MercadoLibre',            TIPO_INGRESO,  'Ingresos',   True,  AMBITO_NEGOCIO,  10),
    ('ENVIO_COBRADO', 'Envíos cobrados al comprador',   TIPO_INGRESO,  'Ingresos',   True,  AMBITO_NEGOCIO,  20),
    ('VTA_OTRO',      'Ventas otros canales',           TIPO_INGRESO,  'Ingresos',   True,  AMBITO_NEGOCIO,  30),
    ('BONIF_ML',      'Bonificaciones y reintegros ML', TIPO_INGRESO,  'Ingresos',   True,  AMBITO_NEGOCIO,  40),
    ('COBRO_OTRO',    'Otros cobros del negocio',       TIPO_INGRESO,  'Ingresos',   True,  AMBITO_NEGOCIO,  50),

    # ── Costo de mercadería ──
    ('CMV',           'Costo de mercadería vendida',    TIPO_COSTO,    'Costos',     True,  AMBITO_NEGOCIO, 100),
    ('IMPORT',        'Costos de importación',          TIPO_COSTO,    'Costos',     True,  AMBITO_NEGOCIO, 110),

    # ── Cargos de la plataforma ──
    ('COM_ML',        'Comisiones por venta ML',        TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 200),
    # ML cobra aparte un cargo fijo por unidad vendida, distinto del porcentaje
    # de comisión: va en su propio rubro porque se comporta distinto.
    ('CARGO_FIJO',    'Cargo fijo por unidad vendida',  TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 205),
    ('ENVIO_ML',      'Cargos de envío ML',             TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 210),
    ('ADS_ML',        'Publicidad (Product Ads)',       TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 220),
    ('FULL_ML',       'Almacenamiento y servicios Full', TIPO_GASTO,   'Plataforma', True,  AMBITO_NEGOCIO, 230),
    ('COM_MP',        'Comisiones Mercado Pago',        TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 240),
    ('FIN_ML',        'Tarifas de financiación',        TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 250),
    ('SEGURO',        'Seguros y garantías',            TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 260),
    ('SERV_ML',       'Servicios y suscripciones de ML', TIPO_GASTO,   'Plataforma', True,  AMBITO_NEGOCIO, 265),
    ('CANCEL',        'Cancelaciones y devoluciones',   TIPO_GASTO,    'Plataforma', True,  AMBITO_NEGOCIO, 270),

    # ── Impuestos ──
    ('IVA',           'IVA',                            TIPO_IMPUESTO, 'Impuestos',  True,  AMBITO_NEGOCIO, 300),
    ('IIBB',          'Ingresos Brutos / Convenio',     TIPO_IMPUESTO, 'Impuestos',  True,  AMBITO_NEGOCIO, 310),
    ('RET_GAN',       'Retención de Ganancias',         TIPO_IMPUESTO, 'Impuestos',  True,  AMBITO_NEGOCIO, 320),
    ('RET_IVA',       'Retención de IVA',               TIPO_IMPUESTO, 'Impuestos',  True,  AMBITO_NEGOCIO, 330),
    ('IMP_DEB_CRED',  'Impuesto débitos y créditos',    TIPO_IMPUESTO, 'Impuestos',  True,  AMBITO_NEGOCIO, 340),
    ('PERCEP',        'Percepciones',                   TIPO_IMPUESTO, 'Impuestos',  True,  AMBITO_NEGOCIO, 350),
    ('IMP_OTRO',      'Otros impuestos y tasas',        TIPO_IMPUESTO, 'Impuestos',  True,  AMBITO_NEGOCIO, 360),

    # ── Gastos operativos ──
    ('PROV',          'Pagos a proveedores',            TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 400),
    ('LOGISTICA',     'Logística y fletes locales',     TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 410),
    ('SUELDOS',       'Sueldos y cargas sociales',      TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 420),
    ('SERVICIOS',     'Servicios',                      TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 430),
    ('ALQUILER',      'Alquileres',                     TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 440),
    ('SOFTWARE',      'Software y suscripciones',       TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 450),
    ('MARKETING',     'Marketing fuera de ML',          TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 460),
    ('GASTO_OTRO',    'Otros gastos',                   TIPO_GASTO,    'Operativos', True,  AMBITO_NEGOCIO, 470),

    # ── Financiero ──
    ('BANCO',         'Gastos bancarios y financieros', TIPO_FINANCIERO, 'Financiero', True, AMBITO_NEGOCIO, 500),
    ('INTERES',       'Intereses pagados',              TIPO_FINANCIERO, 'Financiero', True, AMBITO_NEGOCIO, 510),

    # ── Neutros: se registran y se ven, pero no afectan el resultado ──
    ('TRASPASO',      'Transferencias entre cuentas propias', TIPO_NEUTRO, 'Neutros', False, AMBITO_NEGOCIO, 600),
    ('CONCIL_MP',     'Contrapartida MP de ventas ya contabilizadas', TIPO_NEUTRO, 'Neutros', False, AMBITO_NEGOCIO, 610),
    # El resumen de percepciones repite cargos que ya vienen en el detalle de
    # facturacion. Se guarda para poder conciliar, pero no suma.
    ('CONCIL_PERCEP', 'Resumen de percepciones (ya contabilizadas en facturación)', TIPO_NEUTRO, 'Neutros', False, AMBITO_NEGOCIO, 620),
    ('PERSONAL',      'Personal / retiros',             TIPO_NEUTRO,   'Personal',   False, AMBITO_PERSONAL, 700),
    (RUBRO_SIN_CLASIFICAR, 'Sin clasificar',            TIPO_NEUTRO,   'Pendientes', False, AMBITO_NEGOCIO, 900),
]


# ══════════════════════════════════════════════════════════════════════════════
# MAPEO DE SUBTIPOS NATIVOS DE MERCADOLIBRE
# ══════════════════════════════════════════════════════════════════════════════
# Los `detail_sub_type` de /billing. En ML, la inicial C = cargo y B =
# bonificación del mismo concepto, así que se mapean al mismo rubro y el signo
# lo define `detail_type` (CHARGE / BONUS).
#
# Lo que no esté acá NO se adivina: cae en SIN_CLASIF con revisar=True.

SUBTIPO_ML_RUBRO = {
    # ── Subtipos REALES de MLA, tomados de la facturación de la cuenta ──
    # Los ejemplos de la documentación usan otros códigos (CV, CXD); estos son
    # los que devuelve Argentina en la práctica, con su etiqueta textual.
    'CVFV':    'COM_ML',       # "Cargo por vender"
    'CVFF':    'CARGO_FIJO',   # "Costo por unidad vendida" (cargo fijo)
    'CVFN':    'FIN_ML',       # "Costo por ofrecer cuotas"
    'CFF':     'ENVIO_ML',     # "Cargo por envíos de Mercado Libre"
    'CDSD':    'CANCEL',       # "Cargo por devolución"
    'CPAD':    'ADS_ML',       # campaña de Product Ads
    'CESM':    'SERV_ML',      # "Cargo por mantenimiento de Mi página"

    # ── Percepciones impositivas, observadas en el detalle de facturación ──
    # Vienen por /group/ML/details, NO solo por /perceptions/summary. Mapearlas
    # acá es lo que evita que queden sin clasificar y que haya que sumarlas
    # desde el resumen (que las repetiría).
    'CIVA':    'PERCEP',       # "Percepción de IVA Régimen General"
    'CIVAPP':  'PERCEP',
    'CIRE':    'PERCEP',       # "Percepción especial de IVA RG5319/2023"
    'IIBB':    'IIBB',         # IIBB régimen general, por provincia
    'IIBBME':  'IIBB',
    'IBCF':    'IIBB',         # CABA
    'IBCFME':  'IIBB',
    'CIBCPP':  'IIBB',
    'CIBBPP':  'IIBB',
    'IBCA':    'IIBB',         # Catamarca
    'CBCAPP':  'IIBB',
    'IBCO':    'IIBB',         # Corrientes
    'IBNQ':    'IIBB',         # Neuquén
    'CBNQPP':  'IIBB',
    'IBTU':    'IIBB',         # Tucumán
    'CBTUPP':  'IIBB',
    'CIBT':    'IIBB',         # Tucumán régimen especial
    'IBLP':    'IIBB',         # La Pampa
    'IBSA':    'IIBB',         # Salta
    'CGMV':    'IIBB',         # CABA régimen especial

    # Venta (códigos de la documentación, se dejan por compatibilidad)
    'CV':      'COM_ML',      # cobro por venta
    'BV':      'COM_ML',
    'CVPREM':  'COM_ML',
    # Envíos
    'CXD':     'ENVIO_ML',    # cobro por Mercado Envíos
    'BXD':     'ENVIO_ML',
    'CFLX':    'ENVIO_ML',    # Flex
    'BFLX':    'ENVIO_ML',
    # Publicidad
    'PADS':    'ADS_ML',
    'BPADS':   'ADS_ML',
    # Fulfillment
    'CFCB':    'FULL_ML',     # servicio de recolección
    'BFCB':    'FULL_ML',
    'CFWA':    'FULL_ML',     # almacenamiento
    'BFWA':    'FULL_ML',
    'CFAG':    'FULL_ML',     # almacenamiento prolongado (aging)
    'CFWD':    'FULL_ML',     # retiro de stock
    'CFOV':    'FULL_ML',     # overage
    'CFSP':    'FULL_ML',     # compra de espacio
    # Mercado Pago
    'CCMP':    'COM_MP',      # cargo de Mercado Pago
    'BCMP':    'COM_MP',
    # Financiación
    'CFONPN':  'FIN_ML',
    'CFON':    'FIN_ML',
    # Garantías
    'CEW':     'SEGURO',
    'BEW':     'SEGURO',
}

def rubro_de_subtipo(subtipo: str):
    """
    Rubro de un subtipo de facturación de ML, o None si no se reconoce.

    En ML la inicial marca el signo del concepto: `C` es cargo y `B` es la
    anulación o bonificación del MISMO concepto (CVFV "Cargo por vender" ↔
    BVFV "Anulación del cargo por vender"). Así que si no conocemos el código
    con B, se prueba su equivalente con C y se usa ese rubro. Eso hace que un
    par nuevo entre bien clasificado sin tocar la tabla.
    """
    sub = (subtipo or '').strip().upper()
    if not sub:
        return None
    if sub in SUBTIPO_ML_RUBRO:
        return SUBTIPO_ML_RUBRO[sub]
    if sub.startswith('B'):
        equivalente = 'C' + sub[1:]
        if equivalente in SUBTIPO_ML_RUBRO:
            return SUBTIPO_ML_RUBRO[equivalente]
    return None


# Cuando no hay subtipo conocido pero sí `concept_type`
CONCEPTO_ML_RUBRO = {
    'FLEX':        'ENVIO_ML',
    'FULFILLMENT': 'FULL_ML',
    'WARRANTY':    'SEGURO',
    'PADS':        'ADS_ML',
}

# Retenciones que vienen en `tax_details` de las órdenes
ENTIDAD_FISCAL_RUBRO = {
    'retencion_ganancias': 'RET_GAN',
    'retencion_iva':       'RET_IVA',
    'debitos_creditos':    'IMP_DEB_CRED',
    'retencion_iibb':      'IIBB',
    'iibb':                'IIBB',
}


# ══════════════════════════════════════════════════════════════════════════════
# REGLAS DE CLASIFICACIÓN SEMILLA
# ══════════════════════════════════════════════════════════════════════════════
# (nombre, prioridad, campo, operador, valor, rubro, ambito, solo_origen,
#  solo_signo, marcar_revisar)
#
# Los conceptos personales salen de los movimientos reales de la cuenta: son
# consumos de Guille pagados con MP, no plata del negocio. Van al rubro
# PERSONAL, que no afecta resultado.

REGLAS_SEMILLA = [
    # ── Envíos cobrados al comprador ──
    ('Envío cobrado al comprador', 10, 'concepto', 'contiene',
     'marketplace_shipment', 'ENVIO_COBRADO', None, ORIGEN_MP_PAYMENT, 'positivo', False),

    # ── Bonificaciones de envío Flex que ML debita ──
    ('Bonificación Flex', 20, 'concepto', 'contiene',
     'bonificaciones_flex', 'ENVIO_ML', None, None, None, False),

    # ── Impuestos y servicios pagados desde MP ──
    ('AFIP / ARCA', 30, 'concepto', 'regex',
     r'\b(afip|arca|monotributo|vep)\b', 'IMP_OTRO', AMBITO_NEGOCIO, None, 'negativo', True),
    ('Rentas / IIBB', 31, 'concepto', 'regex',
     r'\b(rentas|ingresos\s*brutos|iibb|agip|arba)\b', 'IIBB', AMBITO_NEGOCIO, None, 'negativo', True),

    # ── Consumos personales detectados en la cuenta ──
    ('Transporte SUBE', 50, 'concepto', 'contiene',
     'SUBE', 'PERSONAL', AMBITO_PERSONAL, None, None, False),
    ('Servicios del hogar', 51, 'concepto', 'regex',
     r'\b(edenor|edesur|metrogas|aysa|naturgy|telecentro|cablevision|movistar|personal\s*flow|claro)\b',
     'PERSONAL', AMBITO_PERSONAL, None, None, False),
    ('Supermercados y comercios', 52, 'concepto', 'regex',
     r'\b(compra\s+en|producto\s+de|dia\b|carrefour|coto|jumbo|disco|vea|chango\s*mas|farmacity)\b',
     'PERSONAL', AMBITO_PERSONAL, None, None, False),
    ('Gastronomía', 53, 'concepto', 'regex',
     r'\b(pizza|empanada|restaurant|resto|cafe|starbucks|mcdonald|burger|sushi|rappi|pedidosya)\b',
     'PERSONAL', AMBITO_PERSONAL, None, None, False),
    ('Combustible', 54, 'concepto', 'regex',
     r'\b(ypf|shell|axion|puma\s*energ)\b', 'PERSONAL', AMBITO_PERSONAL, None, None, False),

    # ── Traspasos: hay que mirarlos, no se asumen ──
    ('Transferencia bancaria', 80, 'concepto', 'regex',
     r'\b(bank\s*transfer|transferencia|cvu|cbu)\b', 'TRASPASO', None, None, None, True),
    ('Retiro de dinero', 81, 'concepto', 'regex',
     r'\b(retiro|withdraw|extracci)\b', 'TRASPASO', None, None, 'negativo', True),

    # ── Suscripciones de software del negocio ──
    ('Software y SaaS', 90, 'concepto', 'regex',
     r'\b(render\.com|render|vercel|openai|anthropic|google\s*cloud|aws|namecheap|godaddy|shopify|canva|notion|slack)\b',
     'SOFTWARE', AMBITO_NEGOCIO, None, 'negativo', False),
]


# ══════════════════════════════════════════════════════════════════════════════
# SEMILLA / MIGRACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def sembrar_plan(session=None) -> dict:
    """
    Crea los rubros y reglas del sistema si no existen. Idempotente: se puede
    llamar en cada arranque sin efecto secundario.

    No pisa rubros que el usuario haya editado (nombre, orden, activo): solo
    inserta los que faltan.
    """
    def _run(s):
        creados_rubros = 0
        existentes = {r.codigo: r for r in s.query(Rubro).all()}

        for codigo, nombre, tipo, grupo, afecta, ambito, orden in PLAN_RUBROS:
            if codigo in existentes:
                continue
            s.add(Rubro(
                codigo=codigo, nombre=nombre, tipo=tipo, grupo=grupo,
                afecta_resultado=afecta, ambito=ambito, orden=orden,
                es_sistema=True, activo=True,
            ))
            creados_rubros += 1
        s.flush()

        # Reglas — se identifican por nombre para no duplicar
        rubros = {r.codigo: r.id for r in s.query(Rubro).all()}
        nombres_existentes = {
            n for (n,) in s.query(ReglaClasificacion.nombre).all()
        }
        creadas_reglas = 0
        for (nombre, prio, campo, oper, valor, rubro_cod, ambito,
             solo_origen, solo_signo, revisar) in REGLAS_SEMILLA:
            if nombre in nombres_existentes:
                continue
            rubro_id = rubros.get(rubro_cod)
            if not rubro_id:
                continue
            s.add(ReglaClasificacion(
                nombre=nombre, prioridad=prio, campo=campo, operador=oper,
                valor=valor, rubro_id=rubro_id, ambito=ambito,
                solo_origen=solo_origen, solo_signo=solo_signo,
                marcar_revisar=revisar, es_sistema=True, activo=True,
            ))
            creadas_reglas += 1

        return {'rubros_creados': creados_rubros, 'reglas_creadas': creadas_reglas}

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def rubro_id_por_codigo(session, codigo: str):
    """Devuelve el id del rubro, o None. Cachea por sesión."""
    cache = session.info.get('_rubro_cache')
    if cache is None:
        cache = {r.codigo: r.id for r in session.query(Rubro).all()}
        session.info['_rubro_cache'] = cache
    if codigo not in cache:
        r = session.query(Rubro).filter_by(codigo=codigo).one_or_none()
        cache[codigo] = r.id if r else None
    return cache.get(codigo)


# ══════════════════════════════════════════════════════════════════════════════
# CLASIFICADOR
# ══════════════════════════════════════════════════════════════════════════════

def _matchea(regla: ReglaClasificacion, mov: dict) -> bool:
    """Evalúa una regla contra un movimiento (dict con las claves del modelo)."""
    if regla.solo_origen and mov.get('origen') != regla.solo_origen:
        return False

    if regla.solo_signo:
        monto = Decimal(str(mov.get('monto') or 0))
        if regla.solo_signo == 'positivo' and monto <= 0:
            return False
        if regla.solo_signo == 'negativo' and monto >= 0:
            return False

    campo_valor = mov.get(regla.campo)
    if campo_valor is None:
        return False
    texto = str(campo_valor)

    op, patron = regla.operador, regla.valor
    if op == 'igual':
        return texto.strip().lower() == patron.strip().lower()
    if op == 'contiene':
        return patron.lower() in texto.lower()
    if op == 'empieza':
        return texto.lower().startswith(patron.lower())
    if op == 'en_lista':
        opciones = [v.strip().lower() for v in patron.split(',') if v.strip()]
        return texto.strip().lower() in opciones
    if op == 'regex':
        try:
            return bool(re.search(patron, texto, re.IGNORECASE))
        except re.error:
            # Regla mal escrita: no debe hacer caer la importación entera
            return False
    return False


def cargar_reglas(session) -> list:
    """Reglas activas ordenadas por prioridad ascendente (gana la primera)."""
    return (session.query(ReglaClasificacion)
            .filter_by(activo=True)
            .order_by(ReglaClasificacion.prioridad.asc(),
                      ReglaClasificacion.id.asc())
            .all())


def clasificar(session, mov: dict, reglas: list = None) -> dict:
    """
    Decide rubro y ámbito de un movimiento.

    Orden de decisión:
      1. Rubro sugerido por el importador (`rubro_sugerido`), que ya viene del
         subtipo nativo de ML. Es el dato más confiable.
      2. Reglas del usuario, por prioridad.
      3. SIN_CLASIF + revisar=True.

    Devuelve dict con rubro_id, ambito, regla_id, revisar, nota_revision.
    Nunca lanza: un movimiento que no se puede clasificar igual entra al libro.
    """
    if reglas is None:
        reglas = cargar_reglas(session)

    sugerido = mov.get('rubro_sugerido')
    if sugerido:
        rid = rubro_id_por_codigo(session, sugerido)
        if rid:
            rubro = session.get(Rubro, rid)
            return {
                'rubro_id': rid,
                'ambito': mov.get('ambito') or (rubro.ambito if rubro else AMBITO_NEGOCIO),
                'regla_id': None,
                'revisar': bool(mov.get('revisar')),
                'nota_revision': mov.get('nota_revision'),
            }

    for regla in reglas:
        if _matchea(regla, mov):
            rubro = session.get(Rubro, regla.rubro_id)
            return {
                'rubro_id': regla.rubro_id,
                'ambito': regla.ambito or (rubro.ambito if rubro else AMBITO_NEGOCIO),
                'regla_id': regla.id,
                'revisar': bool(regla.marcar_revisar or mov.get('revisar')),
                'nota_revision': mov.get('nota_revision'),
            }

    nota = mov.get('nota_revision')
    if not nota:
        sub = mov.get('subtipo')
        nota = (f'Subtipo desconocido: {sub}' if sub
                else 'No coincidió con ninguna regla')
    return {
        'rubro_id': rubro_id_por_codigo(session, RUBRO_SIN_CLASIFICAR),
        'ambito': mov.get('ambito') or AMBITO_NEGOCIO,
        'regla_id': None,
        'revisar': True,
        'nota_revision': nota,
    }


def reclasificar_pendientes(cuenta_alias: str = None, solo_sin_clasificar: bool = True) -> dict:
    """
    Vuelve a pasar el clasificador sobre movimientos ya importados. Se usa
    después de que Guille agrega o corrige una regla.

    Nunca toca movimientos con `rubro_manual=True`: lo que un humano decidió
    manda sobre cualquier regla.
    """
    with session_scope() as s:
        reglas = cargar_reglas(s)
        sin_clasif_id = rubro_id_por_codigo(s, RUBRO_SIN_CLASIFICAR)

        q = s.query(Movimiento).filter(Movimiento.rubro_manual == False)  # noqa: E712
        if cuenta_alias:
            q = q.filter(Movimiento.cuenta_alias == cuenta_alias)
        if solo_sin_clasificar:
            q = q.filter(or_(Movimiento.rubro_id == None,  # noqa: E711
                             Movimiento.rubro_id == sin_clasif_id))

        cambiados = 0
        revisados = 0
        for mov in q.yield_per(500):
            datos = {
                'origen': mov.origen,
                'concepto': mov.concepto,
                'subtipo': mov.subtipo,
                'monto': mov.monto,
                'proveedor': mov.proveedor,
            }
            # Re-derivar el rubro del subtipo nativo, no solo pasar las reglas:
            # si no, ampliar el mapa de subtipos no servía de nada para lo ya
            # importado y habia que reimportar el año entero.
            if mov.origen == ORIGEN_ML_BILLING:
                sugerido = rubro_de_subtipo(mov.subtipo)
                if sugerido:
                    datos['rubro_sugerido'] = sugerido
            res = clasificar(s, datos, reglas)
            revisados += 1
            if res['rubro_id'] != mov.rubro_id:
                mov.rubro_id = res['rubro_id']
                mov.ambito = res['ambito']
                mov.regla_id = res['regla_id']
                mov.revisar = res['revisar']
                mov.nota_revision = res['nota_revision']
                cambiados += 1
            elif res['rubro_id'] and not res['revisar'] and mov.revisar:
                # Mismo rubro pero ya no hay nada que revisar
                mov.revisar = False
                mov.nota_revision = None

        return {'revisados': revisados, 'reclasificados': cambiados}


# ══════════════════════════════════════════════════════════════════════════════
# ALTA DE MOVIMIENTOS (upsert idempotente)
# ══════════════════════════════════════════════════════════════════════════════

def upsert_movimiento(session, datos: dict, reglas: list = None) -> str:
    """
    Inserta o actualiza un movimiento por (cuenta_alias, origen, external_id).

    Devuelve 'nuevo' | 'actualizado' | 'igual'.

    Es lo que hace que reimportar un período sea seguro: el mismo cargo de ML
    consultado diez veces sigue siendo una sola fila.
    """
    cuenta = datos['cuenta_alias']
    origen = datos['origen']
    ext_id = str(datos['external_id'])

    existente = (session.query(Movimiento)
                 .filter_by(cuenta_alias=cuenta, origen=origen, external_id=ext_id)
                 .one_or_none())

    fecha = datos['fecha']
    if isinstance(fecha, str):
        fecha = datetime.fromisoformat(fecha.replace('Z', '+00:00')).replace(tzinfo=None)
    periodo = datos.get('periodo') or f'{fecha:%Y-%m}'

    clasif = clasificar(session, datos, reglas)

    campos = {
        'periodo': periodo,
        'fecha': fecha,
        'concepto': (datos.get('concepto') or '')[:300],
        'subtipo': datos.get('subtipo'),
        'monto': Decimal(str(datos.get('monto') or 0)),
        'moneda': datos.get('moneda') or 'ARS',
        'monto_bruto': datos.get('monto_bruto'),
        'iva': datos.get('iva'),
        'percepciones': datos.get('percepciones'),
        'retenciones': datos.get('retenciones'),
        'comision': datos.get('comision'),
        'descuento': datos.get('descuento'),
        'estado': datos.get('estado'),
        'computable': datos.get('computable', True),
        'order_id': datos.get('order_id'),
        'item_id': datos.get('item_id'),
        'payment_id': datos.get('payment_id'),
        'shipping_id': datos.get('shipping_id'),
        'documento': datos.get('documento'),
        'cantidad': datos.get('cantidad'),
        'proveedor': datos.get('proveedor'),
        'tipo_comprobante': datos.get('tipo_comprobante'),
        'nro_comprobante': datos.get('nro_comprobante'),
        'medio_pago': datos.get('medio_pago'),
        'notas': datos.get('notas'),
        'raw': datos.get('raw'),
    }

    if existente is None:
        mov = Movimiento(
            cuenta_alias=cuenta, origen=origen, external_id=ext_id,
            rubro_id=clasif['rubro_id'], ambito=clasif['ambito'],
            regla_id=clasif['regla_id'], revisar=clasif['revisar'],
            nota_revision=clasif['nota_revision'],
            **campos,
        )
        session.add(mov)
        return 'nuevo'

    # Actualizar: la fuente puede haber cambiado (un cargo bonificado después,
    # un pago que pasó de pending a approved).
    cambio = False
    for k, v in campos.items():
        actual = getattr(existente, k)
        if k == 'monto' and actual is not None:
            if Decimal(str(actual)) != Decimal(str(v)):
                setattr(existente, k, v)
                cambio = True
            continue
        if actual != v:
            setattr(existente, k, v)
            cambio = True

    # El rubro solo se recalcula si nadie lo fijó a mano
    if not existente.rubro_manual and existente.rubro_id != clasif['rubro_id']:
        existente.rubro_id = clasif['rubro_id']
        existente.ambito = clasif['ambito']
        existente.regla_id = clasif['regla_id']
        cambio = True

    return 'actualizado' if cambio else 'igual'


def registrar_gasto_manual(
    cuenta_alias: str,
    fecha,
    rubro_codigo: str,
    concepto: str,
    monto,
    proveedor: str = None,
    tipo_comprobante: str = None,
    nro_comprobante: str = None,
    medio_pago: str = None,
    iva=None,
    notas: str = None,
    moneda: str = 'ARS',
) -> int:
    """
    Carga un gasto (o cobro) a mano: pagos a proveedores, impuestos, y todo lo
    que no pasa por ML ni por MP.

    `monto` se normaliza al signo del rubro: un gasto siempre queda negativo,
    un ingreso siempre positivo, sin importar cómo lo escriba el usuario. Es
    la fuente de error más común al cargar a mano.
    """
    with session_scope() as s:
        rubro = s.query(Rubro).filter_by(codigo=rubro_codigo).one_or_none()
        if rubro is None:
            raise ValueError(f'Rubro inexistente: {rubro_codigo}')

        monto_dec = abs(Decimal(str(monto)))
        if rubro.tipo in (TIPO_COSTO, TIPO_GASTO, TIPO_IMPUESTO, TIPO_FINANCIERO):
            monto_dec = -monto_dec
        elif rubro.tipo == TIPO_NEUTRO and Decimal(str(monto)) < 0:
            monto_dec = -monto_dec

        if isinstance(fecha, str):
            fecha = datetime.fromisoformat(fecha)
        if isinstance(fecha, date) and not isinstance(fecha, datetime):
            fecha = datetime.combine(fecha, datetime.min.time())

        # external_id estable para que no se dupliquen recargas del mismo form
        base = f'{fecha:%Y%m%d}|{rubro_codigo}|{(proveedor or "")}|{(nro_comprobante or concepto)}|{monto_dec}'
        ext_id = f'man-{abs(hash(base)) % (10 ** 16)}'

        mov = Movimiento(
            cuenta_alias=cuenta_alias,
            origen=ORIGEN_MANUAL,
            external_id=ext_id,
            periodo=f'{fecha:%Y-%m}',
            fecha=fecha,
            concepto=(concepto or '')[:300],
            monto=monto_dec,
            moneda=moneda,
            iva=Decimal(str(iva)) if iva is not None else None,
            rubro_id=rubro.id,
            ambito=rubro.ambito,
            rubro_manual=True,
            computable=True,
            proveedor=proveedor,
            tipo_comprobante=tipo_comprobante,
            nro_comprobante=nro_comprobante,
            medio_pago=medio_pago,
            notas=notas,
        )
        s.add(mov)
        s.flush()
        return mov.id


# ══════════════════════════════════════════════════════════════════════════════
# AGREGACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def _rango_periodos(desde: date, hasta: date) -> list:
    """Lista de 'YYYY-MM' entre dos fechas, inclusive."""
    out = []
    y, m = desde.year, desde.month
    while (y, m) <= (hasta.year, hasta.month):
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def resumen(desde: date, hasta: date, cuenta_alias: str = None,
            incluir_personal: bool = False) -> dict:
    """
    Resumen del período: resultado, desglose por rubro y serie mensual.

    `resultado` suma únicamente movimientos con `computable=True` y rubro con
    `afecta_resultado=True`. Los personales y traspasos quedan reportados
    aparte, no sumados.
    """
    with session_scope() as s:
        q = (s.query(
                Movimiento.periodo,
                Rubro.codigo, Rubro.nombre, Rubro.tipo, Rubro.grupo,
                Rubro.afecta_resultado, Rubro.ambito, Rubro.orden,
                func.sum(Movimiento.monto).label('total'),
                func.count(Movimiento.id).label('cantidad'),
             )
             .outerjoin(Rubro, Movimiento.rubro_id == Rubro.id)
             .filter(Movimiento.fecha >= datetime.combine(desde, datetime.min.time()))
             .filter(Movimiento.fecha <= datetime.combine(hasta, datetime.max.time()))
             .filter(Movimiento.computable == True)  # noqa: E712
             .group_by(Movimiento.periodo, Rubro.codigo, Rubro.nombre,
                       Rubro.tipo, Rubro.grupo, Rubro.afecta_resultado,
                       Rubro.ambito, Rubro.orden))
        if cuenta_alias:
            q = q.filter(Movimiento.cuenta_alias == cuenta_alias)

        filas = q.all()

        por_rubro = {}
        por_mes = OrderedDict((p, {'resultado': Decimal('0'),
                                   'ingresos': Decimal('0'),
                                   'egresos': Decimal('0')})
                              for p in _rango_periodos(desde, hasta))
        totales = {
            'ingresos': Decimal('0'), 'costos': Decimal('0'),
            'gastos': Decimal('0'), 'impuestos': Decimal('0'),
            'financiero': Decimal('0'), 'resultado': Decimal('0'),
            'personal': Decimal('0'), 'traspasos': Decimal('0'),
            'sin_clasificar': Decimal('0'),
        }
        cant_sin_clasif = 0

        for (periodo, codigo, nombre, tipo, grupo, afecta, ambito, orden,
             total, cantidad) in filas:
            total = Decimal(str(total or 0))
            codigo = codigo or RUBRO_SIN_CLASIFICAR
            nombre = nombre or 'Sin clasificar'
            tipo = tipo or TIPO_NEUTRO
            afecta = bool(afecta) if afecta is not None else False

            r = por_rubro.setdefault(codigo, {
                'codigo': codigo, 'nombre': nombre, 'tipo': tipo,
                'grupo': grupo or 'Pendientes', 'ambito': ambito or AMBITO_NEGOCIO,
                'afecta_resultado': afecta,
                'orden': orden if orden is not None else 999,
                'total': Decimal('0'), 'cantidad': 0,
            })
            r['total'] += total
            r['cantidad'] += cantidad

            if codigo == RUBRO_SIN_CLASIFICAR:
                totales['sin_clasificar'] += total
                cant_sin_clasif += cantidad
            elif ambito == AMBITO_PERSONAL:
                totales['personal'] += total
            elif tipo == TIPO_NEUTRO:
                totales['traspasos'] += total

            if afecta:
                if tipo == TIPO_INGRESO:
                    totales['ingresos'] += total
                elif tipo == TIPO_COSTO:
                    totales['costos'] += total
                elif tipo == TIPO_GASTO:
                    totales['gastos'] += total
                elif tipo == TIPO_IMPUESTO:
                    totales['impuestos'] += total
                elif tipo == TIPO_FINANCIERO:
                    totales['financiero'] += total
                totales['resultado'] += total

                if periodo in por_mes:
                    por_mes[periodo]['resultado'] += total
                    if total >= 0:
                        por_mes[periodo]['ingresos'] += total
                    else:
                        por_mes[periodo]['egresos'] += total

        ingresos = totales['ingresos']
        margen_pct = (float(totales['resultado'] / ingresos * 100)
                      if ingresos else None)

        return {
            'desde': desde.isoformat(),
            'hasta': hasta.isoformat(),
            'cuenta': cuenta_alias or 'TODAS',
            'criterio': 'caja_pura',
            'totales': {k: float(v) for k, v in totales.items()},
            'margen_pct': margen_pct,
            # Orden de lectura de un estado de resultados (Ingresos, Costos,
            # Plataforma, Impuestos, Operativos, y los neutros al final), no
            # alfabetico: lo da el campo `orden` del plan de rubros.
            'por_rubro': sorted(
                ({**v, 'total': float(v['total'])} for v in por_rubro.values()),
                key=lambda r: (r['orden'], -abs(r['total'])),
            ),
            'por_mes': [
                {'periodo': p, **{k: float(v) for k, v in vals.items()}}
                for p, vals in por_mes.items()
            ],
            'pendientes_de_clasificar': cant_sin_clasif,
            # Aviso honesto: sin COGS el resultado no es ganancia real
            'advertencias': _advertencias(totales, cant_sin_clasif),
        }


def _advertencias(totales: dict, cant_sin_clasif: int) -> list:
    """
    Avisos que tienen que viajar junto al número. La regla es no mostrar un
    resultado que parezca ganancia cuando estructuralmente no lo es.
    """
    avisos = []
    if totales['costos'] == 0 and totales['ingresos'] > 0:
        avisos.append({
            'nivel': 'critico',
            'texto': 'No hay costo de mercadería cargado en el período. '
                     'El resultado que ves es margen sobre plataforma, NO ganancia.',
        })
    if cant_sin_clasif:
        avisos.append({
            'nivel': 'warning',
            'texto': f'{cant_sin_clasif} movimientos sin clasificar. '
                     'No entran al resultado hasta asignarles rubro.',
        })
    return avisos


def resumen_anual(anio: int, cuenta_alias: str = None) -> dict:
    """Atajo: resumen del año calendario completo."""
    return resumen(date(anio, 1, 1), date(anio, 12, 31), cuenta_alias)


def resumen_por_cuenta(desde: date, hasta: date) -> list:
    """
    Resultado de cada cuenta por separado, más el global. Cubre el requisito
    de "vista global entre cuentas y separado por cuenta".
    """
    with session_scope() as s:
        aliases = [a for (a,) in s.query(Movimiento.cuenta_alias)
                   .distinct().order_by(Movimiento.cuenta_alias).all()]

    salida = [{'cuenta': 'TODAS', **resumen(desde, hasta)}]
    for alias in aliases:
        salida.append({'cuenta': alias, **resumen(desde, hasta, alias)})
    return salida


def pendientes(cuenta_alias: str = None, limite: int = 200) -> list:
    """
    Bandeja de pendientes: lo que el clasificador no pudo resolver y lo marcado
    para revisar. Es el mecanismo que garantiza que ningún dato se pierda en
    silencio — en vez de desaparecer, aparece acá.
    """
    with session_scope() as s:
        sin_clasif_id = rubro_id_por_codigo(s, RUBRO_SIN_CLASIFICAR)
        q = (s.query(Movimiento)
             .filter(or_(Movimiento.rubro_id == None,  # noqa: E711
                         Movimiento.rubro_id == sin_clasif_id,
                         Movimiento.revisar == True))  # noqa: E712
             .order_by(Movimiento.fecha.desc()))
        if cuenta_alias:
            q = q.filter(Movimiento.cuenta_alias == cuenta_alias)

        return [{
            'id': m.id,
            'cuenta': m.cuenta_alias,
            'fecha': m.fecha.isoformat(),
            'origen': m.origen,
            'concepto': m.concepto,
            'subtipo': m.subtipo,
            'monto': float(m.monto or 0),
            'nota': m.nota_revision,
            'order_id': m.order_id,
        } for m in q.limit(limite).all()]


def asignar_rubro(movimiento_id: int, rubro_codigo: str,
                  crear_regla: bool = False) -> dict:
    """
    Asigna rubro a mano desde la bandeja. Si `crear_regla`, genera una regla
    para que los movimientos parecidos se resuelvan solos en adelante: así la
    bandeja se vacía en vez de crecer.
    """
    with session_scope() as s:
        mov = s.get(Movimiento, movimiento_id)
        if mov is None:
            raise ValueError(f'Movimiento {movimiento_id} inexistente')
        rubro = s.query(Rubro).filter_by(codigo=rubro_codigo).one_or_none()
        if rubro is None:
            raise ValueError(f'Rubro inexistente: {rubro_codigo}')

        mov.rubro_id = rubro.id
        mov.ambito = rubro.ambito
        mov.rubro_manual = True
        mov.revisar = False
        mov.nota_revision = None

        regla_creada = None
        if crear_regla:
            if mov.subtipo:
                campo, operador, valor = 'subtipo', 'igual', mov.subtipo
            else:
                # Usa el concepto como patrón literal
                valor = (mov.concepto or '').strip()[:200]
                campo, operador = 'concepto', 'contiene'
            if valor:
                nombre = f'Auto: {valor[:60]} → {rubro_codigo}'
                ya = s.query(ReglaClasificacion).filter_by(nombre=nombre).one_or_none()
                if ya is None:
                    regla = ReglaClasificacion(
                        nombre=nombre, prioridad=150, campo=campo,
                        operador=operador, valor=valor, rubro_id=rubro.id,
                        ambito=rubro.ambito, es_sistema=False, activo=True,
                    )
                    s.add(regla)
                    s.flush()
                    regla_creada = regla.id

        return {'ok': True, 'movimiento_id': mov.id,
                'rubro': rubro_codigo, 'regla_creada': regla_creada}


# ══════════════════════════════════════════════════════════════════════════════
# CONSULTAS PARA LA UI
# ══════════════════════════════════════════════════════════════════════════════

def listar_rubros(solo_activos: bool = True) -> list:
    """Rubros para poblar los selects, agrupados y en orden de presentación."""
    with session_scope() as s:
        q = s.query(Rubro).order_by(Rubro.orden.asc())
        if solo_activos:
            q = q.filter(Rubro.activo == True)  # noqa: E712
        return [{
            'codigo': r.codigo, 'nombre': r.nombre, 'tipo': r.tipo,
            'grupo': r.grupo or 'Otros', 'ambito': r.ambito,
            'afecta_resultado': r.afecta_resultado,
            'es_sistema': r.es_sistema,
        } for r in q.all()]


def listar_movimientos(desde: date = None, hasta: date = None,
                       cuenta_alias: str = None, rubro_codigo: str = None,
                       origen: str = None, texto: str = None,
                       ambito: str = None, solo_computables: bool = False,
                       pagina: int = 1, por_pagina: int = 100) -> dict:
    """
    Libro de movimientos con filtros y paginación, para la pantalla del libro.

    Devuelve también el total del filtro aplicado, que es lo que permite
    verificar a ojo que un subconjunto suma lo que tiene que sumar.
    """
    with session_scope() as s:
        q = s.query(Movimiento).outerjoin(Rubro, Movimiento.rubro_id == Rubro.id)

        if desde:
            q = q.filter(Movimiento.fecha >= datetime.combine(desde, datetime.min.time()))
        if hasta:
            q = q.filter(Movimiento.fecha <= datetime.combine(hasta, datetime.max.time()))
        if cuenta_alias:
            q = q.filter(Movimiento.cuenta_alias == cuenta_alias)
        if rubro_codigo:
            if rubro_codigo == RUBRO_SIN_CLASIFICAR:
                sc = rubro_id_por_codigo(s, RUBRO_SIN_CLASIFICAR)
                q = q.filter(or_(Movimiento.rubro_id == None,  # noqa: E711
                                 Movimiento.rubro_id == sc))
            else:
                q = q.filter(Rubro.codigo == rubro_codigo)
        if origen:
            q = q.filter(Movimiento.origen == origen)
        if ambito:
            q = q.filter(Movimiento.ambito == ambito)
        if solo_computables:
            q = q.filter(Movimiento.computable == True)  # noqa: E712
        if texto:
            patron = f'%{texto.strip()}%'
            q = q.filter(or_(Movimiento.concepto.ilike(patron),
                             Movimiento.proveedor.ilike(patron),
                             Movimiento.order_id.ilike(patron),
                             Movimiento.item_id.ilike(patron),
                             Movimiento.external_id.ilike(patron)))

        total_filas = q.count()
        suma = q.with_entities(func.sum(Movimiento.monto)).scalar() or 0

        pagina = max(1, int(pagina or 1))
        filas = (q.order_by(Movimiento.fecha.desc(), Movimiento.id.desc())
                 .offset((pagina - 1) * por_pagina)
                 .limit(por_pagina)
                 .all())

        rubros = {r.id: (r.codigo, r.nombre) for r in s.query(Rubro).all()}

        movimientos = []
        for m in filas:
            codigo, nombre = rubros.get(m.rubro_id, (RUBRO_SIN_CLASIFICAR,
                                                     'Sin clasificar'))
            movimientos.append({
                'id': m.id,
                'fecha': m.fecha.isoformat(),
                'cuenta': m.cuenta_alias,
                'origen': m.origen,
                'concepto': m.concepto,
                'subtipo': m.subtipo,
                'monto': float(m.monto or 0),
                'moneda': m.moneda,
                'rubro_codigo': codigo,
                'rubro_nombre': nombre,
                'ambito': m.ambito,
                'computable': m.computable,
                'revisar': m.revisar,
                'nota_revision': m.nota_revision,
                'rubro_manual': m.rubro_manual,
                'order_id': m.order_id,
                'item_id': m.item_id,
                'proveedor': m.proveedor,
                'nro_comprobante': m.nro_comprobante,
                'external_id': m.external_id,
            })

        return {
            'movimientos': movimientos,
            'total': total_filas,
            'suma': float(suma),
            'pagina': pagina,
            'por_pagina': por_pagina,
            'paginas': max(1, (total_filas + por_pagina - 1) // por_pagina),
        }


def listar_cuentas() -> list:
    """Aliases con movimientos cargados, para los filtros por cuenta."""
    with session_scope() as s:
        return [a for (a,) in s.query(Movimiento.cuenta_alias)
                .distinct().order_by(Movimiento.cuenta_alias).all()]


def ultimas_importaciones(limite: int = 20) -> list:
    """Historial de corridas de importación, para la pantalla de importar."""
    with session_scope() as s:
        filas = (s.query(ImportRun)
                 .order_by(ImportRun.iniciado_at.desc())
                 .limit(limite).all())
        return [{
            'id': r.id, 'cuenta': r.cuenta_alias, 'fuente': r.fuente,
            'desde': r.desde.isoformat() if r.desde else None,
            'hasta': r.hasta.isoformat() if r.hasta else None,
            'iniciado': r.iniciado_at.isoformat() if r.iniciado_at else None,
            'terminado': r.terminado_at.isoformat() if r.terminado_at else None,
            'estado': r.estado, 'leidos': r.leidos, 'nuevos': r.nuevos,
            'actualizados': r.actualizados, 'paginas': r.paginas,
            'sin_clasificar': r.sin_clasificar,
            'mensaje': r.mensaje,
            'errores': (r.errores or [])[:5] if isinstance(r.errores, list) else None,
            'cant_errores': len(r.errores) if isinstance(r.errores, list) else 0,
        } for r in filas]


def _parece_token(valor: str) -> bool:
    """
    Distingue un token pegado de un nombre de variable de entorno.

    Existe porque en la primera corrida real el usuario pegó el token completo
    en el campo que pedía el nombre de la variable — el campo invitaba a
    confundirse. En vez de dejarlo fallar en silencio, se detecta y se guarda
    donde va.

    Un nombre de variable es corto y en mayúsculas con guiones bajos
    (MP_ACCESS_TOKEN); un token de MP/ML trae guiones medios y es largo.
    """
    v = (valor or '').strip()
    if not v:
        return False
    return (v.startswith(('APP_USR-', 'TEST-'))
            or len(v) > 60
            or (v.count('-') >= 3 and len(v) > 30))


def listar_cuentas_mp() -> list:
    """
    Cuentas de Mercado Pago dadas de alta.

    NUNCA devuelve el valor del token: solo si existe y, cuando es un nombre de
    variable de entorno, ese nombre (que no es secreto). Devolverlo era lo que
    hacía que el token apareciera escrito en la pantalla.
    """
    import os as _os
    with session_scope() as s:
        filas = s.query(CuentaMP).order_by(CuentaMP.alias).all()
        out = []
        for c in filas:
            env_valido = (c.token_env
                          and not _parece_token(c.token_env)
                          and _os.environ.get(c.token_env))
            tiene_token = bool(
                env_valido
                or c.access_token
                or _os.environ.get(
                    f'MP_ACCESS_TOKEN_{c.alias.upper().replace(" ", "_")}')
                or _os.environ.get('MP_ACCESS_TOKEN')
                or _os.environ.get('MP_ACCESS_TOKEN_PROD')
            )
            # De dónde sale el token, para que el usuario sepa qué configuró
            if env_valido:
                fuente = f'variable {c.token_env}'
            elif c.access_token:
                fuente = 'guardado en la base'
            elif tiene_token:
                fuente = 'variable de entorno del sistema'
            else:
                fuente = None

            out.append({
                'id': c.id, 'alias': c.alias, 'ml_alias': c.ml_alias,
                'collector_id': c.collector_id, 'nickname': c.nickname,
                'tiene_token': tiene_token, 'fuente_token': fuente,
                'activo': c.activo,
                'last_import': (c.last_import_at.isoformat()
                                if c.last_import_at else None),
            })
        return out


def guardar_cuenta_mp(alias: str, ml_alias: str = None, token_env: str = None,
                      access_token: str = None, collector_id: str = None) -> dict:
    """
    Da de alta o actualiza una cuenta de MP. Cubre el requisito de asociar
    varias cuentas para tener la vista global y la separada.

    Si en `token_env` viene un token pegado en vez de un nombre de variable, se
    guarda como token y no como nombre: es el error natural con ese campo y no
    tiene sentido castigarlo con un "falta token" incomprensible.

    Devuelve `guardado_como` para que la UI pueda avisar qué interpretó.
    """
    alias = (alias or '').strip()
    if not alias:
        raise ValueError('El alias de la cuenta es obligatorio')

    token_env = (token_env or '').strip()
    access_token = (access_token or '').strip()
    guardado_como = None

    if token_env and _parece_token(token_env):
        access_token = token_env
        token_env = ''
        guardado_como = 'token'

    with session_scope() as s:
        cuenta = s.query(CuentaMP).filter_by(alias=alias).one_or_none()
        if cuenta is None:
            cuenta = CuentaMP(alias=alias)
            s.add(cuenta)
        cuenta.ml_alias = (ml_alias or '').strip() or None
        if token_env:
            cuenta.token_env = token_env
            guardado_como = guardado_como or 'variable'
        if access_token:
            cuenta.access_token = access_token
            cuenta.token_env = None   # un token concreto manda sobre la variable
        if collector_id is not None:
            cuenta.collector_id = (collector_id or '').strip() or None
        cuenta.activo = True
        s.flush()
        return {'ok': True, 'alias': cuenta.alias, 'id': cuenta.id,
                'guardado_como': guardado_como}


def neutralizar_resumen_percepciones() -> dict:
    """
    Saca del resultado las percepciones importadas desde /perceptions/summary.

    Se descubrio mirando el libro en produccion que las percepciones llegan
    TAMBIEN por el detalle de facturacion, con sus propios subtipos (CIVA,
    CIRE, IBNQ, IBCA, ...). El resumen es un resumen de esos mismos cargos, no
    una fuente distinta, asi que importarlo ademas duplicaba exactamente todo
    lo impositivo: en produccion, 25,4 millones de percepciones donde habia
    12,7, y 10,6 de IIBB donde habia 5,3.

    El detalle de facturacion queda como fuente buena — es granular y por
    cargo. Las filas del resumen se mueven al rubro neutro CONCIL_PERCEP y se
    marcan no computables: siguen visibles para cruzar contra el detalle, pero
    no suman. Idempotente.
    """
    with session_scope() as s:
        rubro_id = rubro_id_por_codigo(s, 'CONCIL_PERCEP')
        if not rubro_id:
            return {'neutralizados': 0, 'error': 'falta el rubro CONCIL_PERCEP'}

        filas = (s.query(Movimiento)
                 .filter(Movimiento.origen == ORIGEN_ML_PERCEPCION)
                 .filter((Movimiento.computable == True)          # noqa: E712
                         | (Movimiento.rubro_id != rubro_id))
                 .all())
        total = Decimal('0')
        for m in filas:
            if m.computable:
                total += Decimal(str(m.monto or 0))
            m.computable = False
            m.rubro_id = rubro_id
            m.rubro_manual = False
            m.revisar = False
            m.nota_revision = ('Resumen de percepciones: el cargo ya está '
                               'contabilizado en el detalle de facturación')
        return {'neutralizados': len(filas), 'monto_sacado': float(total)}


def limpiar_percepciones_con_id_posicional() -> dict:
    """
    Borra las percepciones importadas con la primera version del importador,
    que usaba la POSICION en la respuesta como identificador (`2026-01-01-0`,
    `-1`, ...) en vez de algo estable.

    Con esa clave, reimportar mezclaba las percepciones entre si y las
    acumulaba: en produccion quedaron 19 movimientos por 12,7 millones, contra
    ventas de 146 millones — un 8,7% de percepciones, imposible.

    Las filas nuevas siempre arrancan con el grupo (`ML-` o `MP-`), asi que
    esas se reconocen y no se tocan. Es la unica excepcion a "nada se descarta":
    estos registros no estan incompletos, estan mal, y dejarlos sumaria doble
    contra los correctos. Idempotente: despues de la primera pasada no hay nada
    que borrar.
    """
    with session_scope() as s:
        viejos = (s.query(Movimiento)
                  .filter(Movimiento.origen == ORIGEN_ML_PERCEPCION)
                  .filter(~Movimiento.external_id.startswith('ML-'))
                  .filter(~Movimiento.external_id.startswith('MP-'))
                  .all())
        borrados = len(viejos)
        total = sum((Decimal(str(m.monto or 0)) for m in viejos), Decimal('0'))
        for m in viejos:
            s.delete(m)
        return {'borrados': borrados, 'monto_liberado': float(total)}


def migrar_tokens_mp_mal_guardados() -> dict:
    """
    Corrige cuentas donde un token quedó guardado en `token_env` (el campo del
    nombre de variable). Idempotente, se corre en cada arranque.

    Es la contracara en datos del arreglo de arriba: sin esto, la fila que ya
    quedó mal en producción sigue mal para siempre.
    """
    with session_scope() as s:
        corregidas = []
        for c in s.query(CuentaMP).all():
            if c.token_env and _parece_token(c.token_env):
                if not c.access_token:
                    c.access_token = c.token_env
                c.token_env = None
                corregidas.append(c.alias)
        return {'corregidas': corregidas}
