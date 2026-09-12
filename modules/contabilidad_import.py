"""
Importadores del sistema contable.

Cada importador lee una fuente, normaliza al formato del libro único y hace
upsert idempotente. Reimportar el mismo rango no duplica nada.

Fuentes:
  importar_mp_pagos        → /v1/payments/search (Mercado Pago)
  importar_ml_billing      → /billing/integration/periods/key/{k}/group/{g}/details
  importar_ml_percepciones → /billing/integration/periods/key/{k}/perceptions/summary
  importar_ml_ordenes      → /orders/search (la venta en sí)
  importar_todo            → orquesta las cuatro para un rango

Dos bugs del módulo anterior que acá están arreglados explícitamente:

  1. TOPE DE 500 SIN PAGINAR. El resumen viejo leía como máximo 500 pagos y
     devolvía el mismo total para 30 y 90 días, en silencio. Acá la paginación
     de MP combina ventanas de fecha con offset, porque `offset` solo por sí
     mismo topea (~1000) y deja registros afuera en rangos largos.

  2. SIN FILTRO DE DIRECCIÓN. El resumen viejo contaba como ingreso los pagos
     que Guille HACÍA con MP (supermercado, SUBE, servicios). Acá la dirección
     se resuelve comparando collector.id / payer.id contra el user id de la
     cuenta, y el signo del monto sale de ahí.

Y uno de criterio: los rechazados, cancelados y devueltos se guardan pero con
`computable=False`, así quedan auditables y fuera de los totales.
"""
import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import requests

from web.db import session_scope
from web.models_contabilidad import (
    AMBITO_NEGOCIO,
    ORIGEN_ML_BILLING, ORIGEN_ML_ORDER, ORIGEN_ML_PERCEPCION, ORIGEN_MP_PAYMENT,
    CuentaMP, ImportRun, Movimiento,
)
from modules.contabilidad import (
    CONCEPTO_ML_RUBRO, ENTIDAD_FISCAL_RUBRO, SUBTIPO_ML_RUBRO,
    cargar_reglas, upsert_movimiento,
)

MP_API = 'https://api.mercadopago.com'
ML_API = 'https://api.mercadolibre.com'

# Estados que no deben sumar al resultado
ESTADOS_NO_COMPUTABLES = {
    'rejected', 'cancelled', 'refunded', 'charged_back', 'in_process',
    'pending', 'invalid',
}

# Pausa entre llamadas. Billing es sensible a 429 y la documentación pide
# consumo secuencial, no batch.
PAUSA_ML = 0.35
PAUSA_MP = 0.20


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _dec(v):
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _parse_fecha(v):
    """Parsea fechas de ML/MP (ISO con o sin offset) a datetime naive."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    s = str(v).strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z',
                    '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt.replace(tzinfo=None)


def _ventanas(desde: date, hasta: date, dias: int = 7):
    """Parte un rango en ventanas de N días. Base de la paginación robusta."""
    cur = desde
    while cur <= hasta:
        fin = min(cur + timedelta(days=dias - 1), hasta)
        yield cur, fin
        cur = fin + timedelta(days=1)


def _claves_periodo(desde: date, hasta: date):
    """Keys de período de billing: siempre el primer día de cada mes."""
    y, m = desde.year, desde.month
    while (y, m) <= (hasta.year, hasta.month):
        yield f'{y:04d}-{m:02d}-01'
        m += 1
        if m > 12:
            m, y = 1, y + 1


class _Run:
    """Context manager que registra la corrida en cont_import_runs."""

    def __init__(self, cuenta_alias, fuente, desde=None, hasta=None, periodo=None):
        self.datos = dict(cuenta_alias=cuenta_alias, fuente=fuente,
                          desde=desde, hasta=hasta, periodo=periodo)
        self.leidos = self.nuevos = self.actualizados = self.paginas = 0
        self.sin_clasificar = 0
        self.errores = []
        self.run_id = None

    def __enter__(self):
        with session_scope() as s:
            run = ImportRun(**self.datos)
            s.add(run)
            s.flush()
            self.run_id = run.id
        return self

    def __exit__(self, exc_type, exc, tb):
        with session_scope() as s:
            run = s.get(ImportRun, self.run_id)
            if run is None:
                return False
            run.terminado_at = datetime.now()
            run.leidos = self.leidos
            run.nuevos = self.nuevos
            run.actualizados = self.actualizados
            run.paginas = self.paginas
            run.sin_clasificar = self.sin_clasificar
            run.errores = self.errores or None
            if exc is not None:
                run.estado = 'error'
                run.mensaje = f'{exc_type.__name__}: {exc}'[:2000]
            elif self.errores:
                run.estado = 'parcial'
                run.mensaje = f'{len(self.errores)} errores no fatales'
            else:
                run.estado = 'ok'
        return False  # nunca traga la excepción

    def resultado(self):
        return {
            'run_id': self.run_id, 'leidos': self.leidos,
            'nuevos': self.nuevos, 'actualizados': self.actualizados,
            'paginas': self.paginas, 'errores': len(self.errores),
        }


# ══════════════════════════════════════════════════════════════════════════════
# MERCADO PAGO
# ══════════════════════════════════════════════════════════════════════════════

def _token_mp(cuenta: CuentaMP) -> str:
    """Token con prioridad a variable de entorno, para no depender de la base."""
    if cuenta.token_env:
        tok = os.environ.get(cuenta.token_env, '').strip()
        if tok:
            return tok
    if cuenta.access_token:
        return cuenta.access_token.strip()
    # Convención de fallback, útil en la cuenta principal
    for var in (f'MP_ACCESS_TOKEN_{cuenta.alias.upper().replace(" ", "_")}',
                'MP_ACCESS_TOKEN_PROD', 'MP_ACCESS_TOKEN'):
        tok = os.environ.get(var, '').strip()
        if tok:
            return tok
    raise RuntimeError(
        f'No hay token de Mercado Pago para la cuenta "{cuenta.alias}". '
        f'Cargá la variable de entorno o el token en la cuenta.'
    )


def _mp_get(token: str, path: str, params: dict) -> dict:
    resp = requests.get(
        f'{MP_API}{path}',
        headers={'Authorization': f'Bearer {token}'},
        params=params,
        timeout=45,
    )
    if resp.status_code == 429:
        time.sleep(3)
        resp = requests.get(
            f'{MP_API}{path}',
            headers={'Authorization': f'Bearer {token}'},
            params=params, timeout=45,
        )
    if not resp.ok:
        raise RuntimeError(f'MP GET {path} → {resp.status_code}: {resp.text[:400]}')
    return resp.json()


def _mp_user_id(token: str) -> str:
    return str(_mp_get(token, '/users/me', {}).get('id') or '')


def _normalizar_pago_mp(pago: dict, mi_id: str, alias: str) -> dict:
    """
    Convierte un pago de MP al formato del libro.

    La dirección del dinero es lo importante: si soy el collector, entra; si
    soy el payer, sale. Ese es el filtro que faltaba y que hacía que los
    consumos personales se contaran como facturación.
    """
    collector_id = str((pago.get('collector_id')
                        or (pago.get('collector') or {}).get('id') or ''))
    payer_id = str((pago.get('payer') or {}).get('id') or '')

    bruto = _dec(pago.get('transaction_amount')) or Decimal('0')
    detalles = pago.get('transaction_details') or {}
    neto_recibido = _dec(detalles.get('net_received_amount'))
    fee_total = Decimal('0')
    for fee in (pago.get('fee_details') or []):
        fee_total += _dec(fee.get('amount')) or Decimal('0')

    soy_collector = bool(mi_id) and collector_id == mi_id
    soy_payer = bool(mi_id) and payer_id == mi_id

    if soy_collector:
        # Entra plata. En caja pura lo que entra es el neto acreditado.
        monto = neto_recibido if neto_recibido is not None else (bruto - fee_total)
        signo_nota = None
    elif soy_payer:
        monto = -abs(bruto)
        signo_nota = None
    else:
        # No se pudo determinar la dirección: entra al libro pero marcado.
        monto = bruto
        signo_nota = ('No se pudo determinar si el dinero entra o sale '
                      '(collector/payer no coinciden con la cuenta)')

    orden_ml = (pago.get('order') or {}).get('id')
    estado = (pago.get('status') or '').lower()

    concepto = (pago.get('description')
                or detalles.get('payment_method_reference_id')
                or (pago.get('additional_info') or {}).get('items', [{}])[0].get('title', '')
                or pago.get('payment_type_id')
                or '')

    # Retenciones e impuestos que MP informa por pago
    retenciones = Decimal('0')
    for tax in (pago.get('taxes_amount_details') or pago.get('taxes') or []):
        retenciones += abs(_dec(tax.get('amount')) or Decimal('0'))

    datos = {
        'cuenta_alias': alias,
        'origen': ORIGEN_MP_PAYMENT,
        'external_id': str(pago.get('id')),
        'fecha': _parse_fecha(pago.get('date_approved') or pago.get('date_created')),
        'concepto': str(concepto)[:300],
        'subtipo': pago.get('payment_type_id'),
        'monto': monto,
        'moneda': pago.get('currency_id') or 'ARS',
        'monto_bruto': bruto,
        'comision': fee_total or None,
        'retenciones': retenciones or None,
        'estado': estado,
        'computable': estado not in ESTADOS_NO_COMPUTABLES,
        'payment_id': str(pago.get('id')),
        'order_id': str(orden_ml) if orden_ml else None,
        'raw': pago,
        'ambito': AMBITO_NEGOCIO,
        'revisar': bool(signo_nota),
        'nota_revision': signo_nota,
    }

    # Si el cobro corresponde a una venta de ML, la venta ya se contabiliza
    # desde /orders. Este movimiento va a un rubro neutro para conciliar sin
    # duplicar el ingreso.
    if orden_ml and monto > 0:
        datos['rubro_sugerido'] = 'CONCIL_MP'

    return datos


def importar_mp_pagos(alias: str, desde: date, hasta: date,
                      dias_ventana: int = 7) -> dict:
    """
    Importa todos los pagos de Mercado Pago del rango, sin tope.

    Paginación en dos niveles: ventanas de fecha (por defecto 7 días) y, dentro
    de cada ventana, offset hasta agotar. Así un año entero entra completo,
    que es justo lo que el resumen anterior no podía hacer.
    """
    with session_scope() as s:
        cuenta = s.query(CuentaMP).filter_by(alias=alias).one_or_none()
        if cuenta is None:
            raise ValueError(f'No hay cuenta de Mercado Pago con alias "{alias}"')
        token = _token_mp(cuenta)
        mi_id = cuenta.collector_id or ''

    if not mi_id:
        mi_id = _mp_user_id(token)
        with session_scope() as s:
            c = s.query(CuentaMP).filter_by(alias=alias).one_or_none()
            if c is not None:
                c.collector_id = mi_id

    with _Run(alias, ORIGEN_MP_PAYMENT, desde=desde, hasta=hasta) as run:
        with session_scope() as s:
            reglas = cargar_reglas(s)

            for v_desde, v_hasta in _ventanas(desde, hasta, dias_ventana):
                offset = 0
                limit = 50  # máximo estable de /v1/payments/search
                while True:
                    params = {
                        'sort': 'date_created',
                        'criteria': 'asc',
                        'range': 'date_created',
                        'begin_date': f'{v_desde.isoformat()}T00:00:00.000-03:00',
                        'end_date': f'{v_hasta.isoformat()}T23:59:59.999-03:00',
                        'offset': offset,
                        'limit': limit,
                    }
                    try:
                        data = _mp_get(token, '/v1/payments/search', params)
                    except Exception as e:
                        run.errores.append({
                            'ventana': f'{v_desde}..{v_hasta}',
                            'offset': offset, 'error': str(e)[:300],
                        })
                        break

                    run.paginas += 1
                    resultados = data.get('results') or []
                    if not resultados:
                        break

                    for pago in resultados:
                        run.leidos += 1
                        try:
                            datos = _normalizar_pago_mp(pago, mi_id, alias)
                            if datos['fecha'] is None:
                                run.errores.append({
                                    'pago': pago.get('id'), 'error': 'sin fecha',
                                })
                                continue
                            estado_up = upsert_movimiento(s, datos, reglas)
                            if estado_up == 'nuevo':
                                run.nuevos += 1
                            elif estado_up == 'actualizado':
                                run.actualizados += 1
                        except Exception as e:
                            run.errores.append({
                                'pago': pago.get('id'), 'error': str(e)[:300],
                            })

                    s.flush()
                    paging = data.get('paging') or {}
                    total = paging.get('total')
                    offset += limit
                    # Corte por total informado o por página incompleta
                    if len(resultados) < limit:
                        break
                    if total is not None and offset >= int(total):
                        break
                    if offset >= 1000:
                        # Tope duro de la API: la ventana es demasiado densa.
                        # Se subdivide en días para no perder nada.
                        run.errores.append({
                            'ventana': f'{v_desde}..{v_hasta}',
                            'error': 'ventana densa, se subdivide por día',
                        })
                        for d_desde, d_hasta in _ventanas(v_desde, v_hasta, 1):
                            _importar_mp_dia(token, mi_id, alias, d_desde,
                                             d_hasta, s, reglas, run)
                        break
                    time.sleep(PAUSA_MP)

        with session_scope() as s:
            c = s.query(CuentaMP).filter_by(alias=alias).one_or_none()
            if c is not None:
                c.last_import_at = datetime.now()

        return run.resultado()


def _importar_mp_dia(token, mi_id, alias, d_desde, d_hasta, s, reglas, run):
    """Subdivisión de emergencia cuando una ventana supera el tope de offset."""
    offset, limit = 0, 50
    while offset < 1000:
        params = {
            'sort': 'date_created', 'criteria': 'asc', 'range': 'date_created',
            'begin_date': f'{d_desde.isoformat()}T00:00:00.000-03:00',
            'end_date': f'{d_hasta.isoformat()}T23:59:59.999-03:00',
            'offset': offset, 'limit': limit,
        }
        try:
            data = _mp_get(token, '/v1/payments/search', params)
        except Exception as e:
            run.errores.append({'dia': str(d_desde), 'error': str(e)[:300]})
            return
        run.paginas += 1
        resultados = data.get('results') or []
        if not resultados:
            return
        for pago in resultados:
            run.leidos += 1
            try:
                datos = _normalizar_pago_mp(pago, mi_id, alias)
                if datos['fecha'] is None:
                    continue
                est = upsert_movimiento(s, datos, reglas)
                if est == 'nuevo':
                    run.nuevos += 1
                elif est == 'actualizado':
                    run.actualizados += 1
            except Exception as e:
                run.errores.append({'pago': pago.get('id'), 'error': str(e)[:300]})
        s.flush()
        if len(resultados) < limit:
            return
        offset += limit
        time.sleep(PAUSA_MP)


# ══════════════════════════════════════════════════════════════════════════════
# MERCADOLIBRE — BILLING
# ══════════════════════════════════════════════════════════════════════════════

def _rubro_desde_detalle(charge: dict, extra: dict = None) -> tuple:
    """
    Decide el rubro de un detalle de billing a partir del subtipo nativo.

    Devuelve (codigo_rubro | None, nota). None significa "no lo reconozco":
    el movimiento entra igual y queda en pendientes. Preferimos un pendiente
    visible antes que un total silenciosamente mal.
    """
    sub = (charge.get('detail_sub_type') or '').strip().upper()
    if sub in SUBTIPO_ML_RUBRO:
        return SUBTIPO_ML_RUBRO[sub], None

    concepto_tipo = (charge.get('concept_type')
                     or (extra or {}).get('concept_type') or '').strip().upper()
    if concepto_tipo in CONCEPTO_ML_RUBRO:
        return CONCEPTO_ML_RUBRO[concepto_tipo], (
            f'Subtipo {sub or "vacío"} no mapeado; clasificado por concepto '
            f'{concepto_tipo}'
        )

    return None, f'Subtipo de billing no mapeado: {sub or "(vacío)"}'


def _normalizar_detalle_billing(item: dict, alias: str, grupo: str,
                                periodo_key: str) -> dict:
    """Normaliza un detalle de /billing/.../details al formato del libro."""
    charge = item.get('charge_info') or {}
    ventas = item.get('sales_info') or []
    venta = ventas[0] if ventas else {}
    items_info = item.get('items_info') or []
    item_info = items_info[0] if items_info else {}
    envio = item.get('shipping_info') or {}
    descuento = item.get('discount_info') or {}
    doc = item.get('document_info') or {}
    moneda = (item.get('currency_info') or {}).get('currency_id') or 'ARS'
    ful = item.get('fulfillment_info') or {}

    monto_abs = abs(_dec(charge.get('detail_amount')) or Decimal('0'))
    detail_type = (charge.get('detail_type') or 'CHARGE').strip().upper()
    # CHARGE = ML te cobra → sale plata. BONUS = te bonifica → entra.
    monto = monto_abs if detail_type == 'BONUS' else -monto_abs

    rubro, nota = _rubro_desde_detalle(charge, ful)

    # Una bonificación anulada deja de ser un crédito
    estado = (charge.get('status') or '').strip().upper()
    fecha = _parse_fecha(charge.get('creation_date_time'))

    detalle_id = charge.get('detail_id')
    mov_id = charge.get('movement_id')
    ext_id = f'{grupo}-{detalle_id or mov_id}'
    # Un mismo detail_id puede traer varias filas (una por subtipo) en packs
    if charge.get('detail_sub_type'):
        ext_id += f'-{charge["detail_sub_type"]}'

    return {
        'cuenta_alias': alias,
        'origen': ORIGEN_ML_BILLING,
        'external_id': ext_id,
        'periodo': periodo_key[:7],
        'fecha': fecha,
        'concepto': (charge.get('transaction_detail') or '')[:300],
        'subtipo': (charge.get('detail_sub_type') or '').strip().upper() or None,
        'monto': monto,
        'moneda': moneda,
        'monto_bruto': _dec(descuento.get('charge_amount_without_discount')),
        'descuento': _dec(descuento.get('discount_amount')),
        'estado': estado or None,
        'computable': True,
        'order_id': str(venta.get('order_id')) if venta.get('order_id') else None,
        'item_id': item_info.get('item_id') or ful.get('item_id'),
        'payment_id': (str(venta.get('operation_id'))
                       if venta.get('operation_id') else
                       (str(charge.get('payment_id')) if charge.get('payment_id') else None)),
        'shipping_id': (str(envio.get('shipping_id'))
                        if envio.get('shipping_id') else None),
        'documento': charge.get('legal_document_number'),
        'cantidad': item_info.get('item_amount') or ful.get('quantity'),
        'raw': item,
        'rubro_sugerido': rubro,
        'nota_revision': nota,
        'revisar': rubro is None,
    }


def importar_ml_billing(alias: str, desde: date, hasta: date,
                        grupos=('ML', 'MP'),
                        document_types=('BILL', 'CREDIT_NOTE')) -> dict:
    """
    Importa el detalle de facturación de ML para cada mes del rango.

    Pagina con from_id / last_id como pide la documentación oficial: `offset`
    topea en 10.000 y no garantiza integridad en listados largos.
    """
    from core.account_manager import AccountManager

    mgr = AccountManager()
    client = mgr.get_client(alias)
    if client is None:
        raise ValueError(f'No hay cuenta ML con alias "{alias}"')

    with _Run(alias, ORIGEN_ML_BILLING, desde=desde, hasta=hasta) as run:
        with session_scope() as s:
            reglas = cargar_reglas(s)

            for key in _claves_periodo(desde, hasta):
                for grupo in grupos:
                    for doc_type in document_types:
                        from_id = 0
                        while True:
                            path = (f'/billing/integration/periods/key/{key}'
                                    f'/group/{grupo}/details')
                            params = {
                                'document_type': doc_type,
                                'limit': 1000,
                                'from_id': from_id,
                                'sort_by': 'ID',
                                'order_by': 'ASC',
                            }
                            try:
                                data = client._get(path, params)
                            except Exception as e:
                                msg = str(e)
                                # Período sin datos o sin permisos: no es fatal
                                if any(c in msg for c in ('404', '403', '400')):
                                    run.errores.append({
                                        'periodo': key, 'grupo': grupo,
                                        'document_type': doc_type,
                                        'error': msg[:200],
                                    })
                                    break
                                raise

                            run.paginas += 1
                            resultados = (data or {}).get('results') or []
                            if not resultados:
                                break

                            for item in resultados:
                                run.leidos += 1
                                try:
                                    datos = _normalizar_detalle_billing(
                                        item, alias, grupo, key)
                                    if datos['fecha'] is None:
                                        datos['fecha'] = datetime.fromisoformat(
                                            key + 'T00:00:00')
                                    if datos.get('rubro_sugerido') is None:
                                        run.sin_clasificar += 1
                                    est = upsert_movimiento(s, datos, reglas)
                                    if est == 'nuevo':
                                        run.nuevos += 1
                                    elif est == 'actualizado':
                                        run.actualizados += 1
                                except Exception as e:
                                    run.errores.append({
                                        'periodo': key,
                                        'detalle': (item.get('charge_info') or {}).get('detail_id'),
                                        'error': str(e)[:300],
                                    })

                            s.flush()

                            last_id = (data or {}).get('last_id')
                            if not last_id or last_id == from_id:
                                break
                            from_id = last_id
                            time.sleep(PAUSA_ML)

        return run.resultado()


def importar_ml_percepciones(alias: str, desde: date, hasta: date) -> dict:
    """
    Percepciones impositivas del período (exclusivo MLA). Son plata que se te
    retiene: entran como egreso en el rubro PERCEP.
    """
    from core.account_manager import AccountManager

    mgr = AccountManager()
    client = mgr.get_client(alias)
    if client is None:
        raise ValueError(f'No hay cuenta ML con alias "{alias}"')

    with _Run(alias, ORIGEN_ML_PERCEPCION, desde=desde, hasta=hasta) as run:
        with session_scope() as s:
            reglas = cargar_reglas(s)

            for key in _claves_periodo(desde, hasta):
                path = f'/billing/integration/periods/key/{key}/perceptions/summary'
                try:
                    data = client._get(path, {})
                except Exception as e:
                    run.errores.append({'periodo': key, 'error': str(e)[:200]})
                    continue

                run.paginas += 1
                # La respuesta trae una lista de percepciones por tipo/jurisdicción
                filas = []
                if isinstance(data, dict):
                    filas = (data.get('perceptions') or data.get('results')
                             or data.get('summary') or [])
                elif isinstance(data, list):
                    filas = data

                for i, fila in enumerate(filas or []):
                    if not isinstance(fila, dict):
                        continue
                    run.leidos += 1
                    monto = abs(_dec(fila.get('amount')
                                     or fila.get('total_amount')
                                     or fila.get('perception_amount')) or Decimal('0'))
                    if monto == 0:
                        continue
                    etiqueta = (fila.get('label') or fila.get('name')
                                or fila.get('tax_name')
                                or fila.get('type') or 'Percepción')
                    codigo = (fila.get('type') or fila.get('tax_id') or i)

                    # IIBB tiene su propio rubro; el resto va a PERCEP
                    texto = str(etiqueta).lower()
                    rubro = 'IIBB' if ('brutos' in texto or 'iibb' in texto) else 'PERCEP'

                    datos = {
                        'cuenta_alias': alias,
                        'origen': ORIGEN_ML_PERCEPCION,
                        'external_id': f'{key}-{codigo}',
                        'periodo': key[:7],
                        'fecha': datetime.fromisoformat(key + 'T00:00:00'),
                        'concepto': str(etiqueta)[:300],
                        'subtipo': str(fila.get('type') or '')[:40] or None,
                        'monto': -monto,
                        'percepciones': monto,
                        'moneda': 'ARS',
                        'computable': True,
                        'raw': fila,
                        'rubro_sugerido': rubro,
                    }
                    try:
                        est = upsert_movimiento(s, datos, reglas)
                        if est == 'nuevo':
                            run.nuevos += 1
                        elif est == 'actualizado':
                            run.actualizados += 1
                    except Exception as e:
                        run.errores.append({'periodo': key, 'error': str(e)[:300]})

                s.flush()
                time.sleep(PAUSA_ML)

        return run.resultado()


# ══════════════════════════════════════════════════════════════════════════════
# MERCADOLIBRE — ÓRDENES (la venta en sí)
# ══════════════════════════════════════════════════════════════════════════════

def _normalizar_orden(orden: dict, alias: str) -> list:
    """
    Una orden genera un movimiento de ingreso por venta. Si trae retenciones
    en `payments[].tax_details`, genera además un movimiento por retención.
    """
    out = []
    order_id = str(orden.get('id'))
    fecha = _parse_fecha(orden.get('date_closed') or orden.get('date_created'))
    estado = (orden.get('status') or '').lower()
    moneda = orden.get('currency_id') or 'ARS'

    items = orden.get('order_items') or []
    primer_item = (items[0].get('item') or {}) if items else {}
    titulo = primer_item.get('title') or ''
    item_id = primer_item.get('id')
    cantidad = sum(int(i.get('quantity') or 0) for i in items) or None

    # En caja pura el ingreso es lo que efectivamente pagó el comprador
    pagado = _dec(orden.get('paid_amount'))
    total = _dec(orden.get('total_amount')) or Decimal('0')
    monto = pagado if pagado is not None else total

    # marketplace_fee viene informado por ML; se guarda como referencia, el
    # cargo real se contabiliza desde billing para no duplicarlo.
    comision_ref = Decimal('0')
    for i in items:
        comision_ref += abs(_dec(i.get('sale_fee')) or Decimal('0')) * int(i.get('quantity') or 1)

    computable = estado not in ESTADOS_NO_COMPUTABLES

    out.append({
        'cuenta_alias': alias,
        'origen': ORIGEN_ML_ORDER,
        'external_id': order_id,
        'fecha': fecha,
        'concepto': (titulo or f'Venta {order_id}')[:300],
        'subtipo': 'VENTA',
        # Una orden cancelada se guarda con su monto pero computable=False: no
        # es un egreso de ese monto, simplemente no es un hecho económico.
        'monto': monto,
        'moneda': moneda,
        'monto_bruto': total,
        'comision': comision_ref or None,
        'estado': estado,
        'computable': computable,
        'order_id': order_id,
        'item_id': item_id,
        'cantidad': cantidad,
        'raw': {k: orden.get(k) for k in
                ('id', 'status', 'date_created', 'date_closed', 'total_amount',
                 'paid_amount', 'currency_id', 'order_items', 'payments',
                 'buyer', 'shipping')},
        'rubro_sugerido': 'CANCEL' if not computable else 'VTA_ML',
        'nota_revision': (f'Orden en estado {estado}: no suma al resultado'
                          if not computable else None),
    })

    # Retenciones impositivas informadas en los pagos de la orden
    for pago in (orden.get('payments') or []):
        pid = pago.get('id')
        for j, tax in enumerate(pago.get('tax_details') or []):
            monto_tax = abs(_dec(tax.get('original_amount')) or Decimal('0'))
            devuelto = abs(_dec(tax.get('refunded_amount')) or Decimal('0'))
            neto_tax = monto_tax - devuelto
            if neto_tax <= 0:
                continue
            entidad = (tax.get('mov_financial_entity') or '').strip().lower()
            rubro = ENTIDAD_FISCAL_RUBRO.get(entidad)
            detalle = (tax.get('mov_detail') or '').strip().lower()
            if rubro is None and 'sirtac' in detalle:
                rubro = 'IIBB'
            out.append({
                'cuenta_alias': alias,
                'origen': ORIGEN_ML_ORDER,
                'external_id': f'{order_id}-tax-{pid}-{j}',
                'fecha': _parse_fecha(pago.get('date_approved')) or fecha,
                'concepto': (f'{tax.get("mov_detail") or "Retención"} '
                             f'{tax.get("mov_financial_entity") or ""}').strip()[:300],
                'subtipo': (entidad or detalle or 'RETENCION')[:40].upper(),
                'monto': -neto_tax,
                'retenciones': neto_tax,
                'moneda': moneda,
                'estado': tax.get('tax_status'),
                'computable': (tax.get('tax_status') or '').lower() == 'applied',
                'order_id': order_id,
                'payment_id': str(pid) if pid else None,
                'raw': tax,
                'rubro_sugerido': rubro,
                'nota_revision': (None if rubro else
                                  f'Entidad fiscal no mapeada: {entidad or detalle}'),
                'revisar': rubro is None,
            })

    return out


def importar_ml_ordenes(alias: str, desde: date, hasta: date,
                        dias_ventana: int = 15) -> dict:
    """
    Importa las ventas del rango desde /orders/search.

    Se usa esta fuente y no billing para el ingreso por venta: la propia
    documentación de ML dice que billing es solo para conciliación fiscal y
    que los datos de la venta salen de /orders.
    """
    from core.account_manager import AccountManager

    mgr = AccountManager()
    client = mgr.get_client(alias)
    if client is None:
        raise ValueError(f'No hay cuenta ML con alias "{alias}"')

    seller_id = client.account.user_id
    if not seller_id:
        seller_id = (client.get_me() or {}).get('id')

    with _Run(alias, ORIGEN_ML_ORDER, desde=desde, hasta=hasta) as run:
        with session_scope() as s:
            reglas = cargar_reglas(s)

            for v_desde, v_hasta in _ventanas(desde, hasta, dias_ventana):
                offset, limit = 0, 50
                while True:
                    params = {
                        'seller': seller_id,
                        'order.date_created.from': f'{v_desde.isoformat()}T00:00:00.000-03:00',
                        'order.date_created.to': f'{v_hasta.isoformat()}T23:59:59.000-03:00',
                        'sort': 'date_asc',
                        'offset': offset,
                        'limit': limit,
                    }
                    try:
                        data = client._get('/orders/search', params)
                    except Exception as e:
                        run.errores.append({
                            'ventana': f'{v_desde}..{v_hasta}',
                            'offset': offset, 'error': str(e)[:300],
                        })
                        break

                    run.paginas += 1
                    resultados = (data or {}).get('results') or []
                    if not resultados:
                        break

                    for orden in resultados:
                        run.leidos += 1
                        try:
                            for datos in _normalizar_orden(orden, alias):
                                if datos['fecha'] is None:
                                    continue
                                if datos.get('rubro_sugerido') is None:
                                    run.sin_clasificar += 1
                                est = upsert_movimiento(s, datos, reglas)
                                if est == 'nuevo':
                                    run.nuevos += 1
                                elif est == 'actualizado':
                                    run.actualizados += 1
                        except Exception as e:
                            run.errores.append({
                                'orden': orden.get('id'), 'error': str(e)[:300],
                            })

                    s.flush()
                    total = ((data or {}).get('paging') or {}).get('total')
                    offset += limit
                    if len(resultados) < limit:
                        break
                    if total is not None and offset >= int(total):
                        break
                    if offset >= 10000:
                        run.errores.append({
                            'ventana': f'{v_desde}..{v_hasta}',
                            'error': 'ventana supera el tope de offset de /orders',
                        })
                        break
                    time.sleep(PAUSA_ML)

        return run.resultado()


# ══════════════════════════════════════════════════════════════════════════════
# ORQUESTADOR
# ══════════════════════════════════════════════════════════════════════════════

def importar_todo(alias_ml: str, desde: date, hasta: date,
                  alias_mp: str = None, fuentes=None) -> dict:
    """
    Corre todos los importadores para un rango. Secuencial a propósito: la
    documentación de billing advierte que el paralelismo es la causa típica
    de los 429.
    """
    fuentes = fuentes or ('ordenes', 'billing', 'percepciones', 'mp')
    salida = {}

    if 'ordenes' in fuentes:
        try:
            salida['ordenes'] = importar_ml_ordenes(alias_ml, desde, hasta)
        except Exception as e:
            salida['ordenes'] = {'error': str(e)[:300]}

    if 'billing' in fuentes:
        try:
            salida['billing'] = importar_ml_billing(alias_ml, desde, hasta)
        except Exception as e:
            salida['billing'] = {'error': str(e)[:300]}

    if 'percepciones' in fuentes:
        try:
            salida['percepciones'] = importar_ml_percepciones(alias_ml, desde, hasta)
        except Exception as e:
            salida['percepciones'] = {'error': str(e)[:300]}

    if 'mp' in fuentes:
        destino = alias_mp or alias_ml
        try:
            salida['mp'] = importar_mp_pagos(destino, desde, hasta)
        except Exception as e:
            salida['mp'] = {'error': str(e)[:300]}

    return salida
