"""
Captura de baseline completo al momento de aplicar una optimización.

Reemplaza la captura plana de 6 campos (visitas_7d, ventas_30d, ventas_total,
conv_pct, posicion, posicion_kw) por una estructura agrupada en 5 secciones
con 14 métricas reales:

  visibilidad: score_calidad_ml, esta_en_catalogo, tiene_buy_box,
               posiciones_top_keywords (top 3), posicion_categoria_principal
  trafico:     visitas_7d, visitas_30d, conversion_7d, conversion_30d
  ventas:      unidades_7d, unidades_30d, facturacion_7d, facturacion_30d,
               ticket_promedio, ventas_total_historica
  engagement:  preguntas_pendientes, preguntas_total_30d,
               resenas_cantidad, resenas_promedio_estrellas
  salud:       reclamos_30d, cancelaciones_30d, mediaciones_30d

Cada métrica que no se puede capturar (por permisos faltantes o porque ML no
la expone) queda como `null` en el JSON y aparece listada en `_unavailable`.

La captura involucra ~12-14 llamadas a la API ML y dura 3-8s. Ofrecemos dos
modos:
  - capturar_baseline_completo()   — síncrono, retorna el dict
  - capturar_baseline_async()      — lanza en thread, escribe directo a
                                      monitor_evolucion.json al terminar

API pública:
  capturar_baseline_completo(item_id, alias, client, top_keywords=None) → dict
  capturar_baseline_async(item_id, alias, client, data_dir, on_complete=None,
                          publicacion_original_mla=None) → thread
  marcar_capturing(data_dir, item_id, alias) — escribe flag _capturing=True
                                                en la entry de monitor
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

import requests

_logger = logging.getLogger(__name__)
_ML = 'https://api.mercadolibre.com'

# Cuántas keywords trackear (decisión: top 3 en V1, escalable a 5 si feedback lo pide)
_TOP_KEYWORDS_N = 3

# Versión del schema del baseline — útil para migrar/detectar baselines viejos
_BASELINE_VERSION = 2


# ── Helpers de fecha ─────────────────────────────────────────────────────────

def _ago(days: int) -> str:
    """Fecha ISO en formato ML para 'hace N días' a las 00:00 ART."""
    return (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00.000-03:00')


def _ms_since(t0: float) -> int:
    return int((time.time() - t0) * 1000)


# ── Captura por sección ──────────────────────────────────────────────────────

def _capturar_visibilidad(item_id: str, headers: dict, top_kws: list[str],
                          alias: str, data_dir: str) -> tuple[dict, list, int]:
    """Captura métricas de visibilidad. Retorna (data, unavailable, api_calls)."""
    data: dict = {
        'score_calidad_ml':              None,
        'score_calidad_nivel':           None,
        'esta_en_catalogo':              None,
        'tiene_buy_box':                 None,
        'posicion_top_keywords':         None,
        'posicion_categoria_principal':  None,
    }
    unavailable: list = []
    api_calls = 0

    # Score de calidad ML (puede 404 en items recién creados)
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/items/{item_id}/quality_score',
                         headers=headers, timeout=6)
        if r.ok:
            body = r.json()
            data['score_calidad_ml']    = body.get('score') or body.get('quality_score')
            data['score_calidad_nivel'] = body.get('level') or body.get('score_level')
        else:
            unavailable.append('score_calidad_ml')
    except Exception:
        unavailable.append('score_calidad_ml')

    # Catálogo + buy box (requiere `catalog_product_id` del item)
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/items/{item_id}',
                         headers=headers,
                         params={'attributes': 'id,catalog_product_id'},
                         timeout=6)
        if r.ok:
            cpid = (r.json() or {}).get('catalog_product_id')
            data['esta_en_catalogo'] = bool(cpid)
            if cpid:
                # Verificar si tenemos el Buy Box (necesita ítems y búsqueda)
                try:
                    api_calls += 1
                    rp = requests.get(f'{_ML}/products/{cpid}/items',
                                      headers=headers, params={'limit': 5}, timeout=6)
                    if rp.ok:
                        sellers = rp.json().get('results', [])
                        data['tiene_buy_box'] = bool(sellers) and (
                            sellers[0].get('id') == item_id or
                            sellers[0].get('item_id') == item_id
                        )
                    else:
                        unavailable.append('tiene_buy_box')
                except Exception:
                    unavailable.append('tiene_buy_box')
            else:
                data['tiene_buy_box'] = False  # no aplica
        else:
            unavailable.append('esta_en_catalogo')
            unavailable.append('tiene_buy_box')
    except Exception:
        unavailable.append('esta_en_catalogo')
        unavailable.append('tiene_buy_box')

    # Posiciones por top keywords — requiere ítems y búsqueda activado
    if top_kws:
        positions: list = []
        for kw in top_kws[:_TOP_KEYWORDS_N]:
            try:
                api_calls += 1
                r = requests.get(f'{_ML}/sites/MLA/search',
                                 headers=headers,
                                 params={'q': kw, 'limit': 50}, timeout=8)
                if r.ok:
                    results = r.json().get('results', [])
                    pos = next(
                        (i + 1 for i, res in enumerate(results) if res.get('id') == item_id),
                        None,
                    )
                    positions.append({'kw': kw, 'posicion': pos})
                else:
                    positions.append({'kw': kw, 'posicion': None})
                time.sleep(0.1)
            except Exception:
                positions.append({'kw': kw, 'posicion': None})
        data['posicion_top_keywords'] = positions
        # Si TODAS volvieron None, marcar como unavailable
        if all(p['posicion'] is None for p in positions):
            unavailable.append('posicion_top_keywords')
    else:
        # Sin keywords disponibles desde la optimización — fallback a JSON local
        try:
            pos_data = _load_json(os.path.join(data_dir, f'posiciones_{_safe(alias)}.json'))
            if isinstance(pos_data, dict) and item_id in pos_data:
                hist = pos_data[item_id].get('history', {})
                if hist:
                    last_date = max(hist.keys())
                    pv = hist[last_date]
                    if pv != 999:
                        data['posicion_top_keywords'] = [
                            {'kw': pos_data[item_id].get('keywords', ['?'])[0]
                                   if pos_data[item_id].get('keywords') else '?',
                             'posicion': pv}
                        ]
        except Exception:
            unavailable.append('posicion_top_keywords')

    # posicion_categoria_principal — endpoint no público, queda null permanente
    unavailable.append('posicion_categoria_principal')

    return data, unavailable, api_calls


def _capturar_trafico(item_id: str, headers: dict) -> tuple[dict, list, int]:
    data: dict = {
        'visitas_7d':       None,
        'visitas_30d':      None,
        'visitas_unicas_7d': None,   # ML no expone únicas en endpoint público
        'conversion_7d':    None,
        'conversion_30d':   None,
    }
    unavailable = ['visitas_unicas_7d']
    api_calls = 0

    try:
        api_calls += 1
        r = requests.get(f'{_ML}/items/{item_id}/visits/time_window',
                         headers=headers, params={'last': 7, 'unit': 'day'}, timeout=6)
        if r.ok:
            data['visitas_7d'] = int(r.json().get('total_visits') or 0)
        else:
            unavailable.append('visitas_7d')
    except Exception:
        unavailable.append('visitas_7d')

    try:
        api_calls += 1
        r = requests.get(f'{_ML}/items/{item_id}/visits/time_window',
                         headers=headers, params={'last': 30, 'unit': 'day'}, timeout=6)
        if r.ok:
            data['visitas_30d'] = int(r.json().get('total_visits') or 0)
        else:
            unavailable.append('visitas_30d')
    except Exception:
        unavailable.append('visitas_30d')

    return data, unavailable, api_calls


def _capturar_ventas(item_id: str, user_id: str, headers: dict) -> tuple[dict, list, int]:
    data: dict = {
        'unidades_7d':            None,
        'unidades_30d':           None,
        'facturacion_7d':         None,
        'facturacion_30d':        None,
        'ticket_promedio':        None,
        'ventas_total_historica': None,
    }
    unavailable: list = []
    api_calls = 0

    # Ventas históricas totales del item
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/items/{item_id}',
                         headers=headers,
                         params={'attributes': 'sold_quantity'}, timeout=6)
        if r.ok:
            data['ventas_total_historica'] = int(r.json().get('sold_quantity') or 0)
    except Exception:
        unavailable.append('ventas_total_historica')

    # Orders agregadas — un solo fetch de los últimos 30d, después filtrar 7d
    try:
        api_calls += 1
        date_from = _ago(30)
        all_orders: list = []
        offset = 0
        while True:
            r = requests.get(f'{_ML}/orders/search',
                             headers=headers,
                             params={'seller': user_id,
                                     'order.status': 'paid',
                                     'order.date_created.from': date_from,
                                     'limit': 50, 'offset': offset,
                                     'sort': 'date_desc'},
                             timeout=10)
            if not r.ok:
                break
            d = r.json()
            batch = d.get('results', [])
            all_orders.extend(batch)
            total = d.get('paging', {}).get('total', 0)
            offset += len(batch)
            if not batch or offset >= total or offset >= 200:
                break

        # Filtrar por item_id en line items
        cutoff_7d = (datetime.now() - timedelta(days=7))
        u7d, f7d, u30d, f30d = 0, 0.0, 0, 0.0
        for o in all_orders:
            for li in (o.get('order_items') or []):
                item = (li.get('item') or {})
                if (item.get('id') or '').upper() != item_id.upper():
                    continue
                qty = int(li.get('quantity') or 0)
                unit_price = float(li.get('unit_price') or 0)
                rev = qty * unit_price
                u30d += qty
                f30d += rev
                # ¿Está en los últimos 7d?
                try:
                    fecha = (o.get('date_created') or '')[:10]
                    if fecha and datetime.strptime(fecha, '%Y-%m-%d') >= cutoff_7d:
                        u7d += qty
                        f7d += rev
                except Exception:
                    pass

        data['unidades_7d']     = u7d
        data['unidades_30d']    = u30d
        data['facturacion_7d']  = round(f7d, 0)
        data['facturacion_30d'] = round(f30d, 0)
        if u30d > 0:
            data['ticket_promedio'] = round(f30d / u30d, 0)
    except Exception:
        unavailable.extend(['unidades_7d', 'unidades_30d', 'facturacion_7d',
                            'facturacion_30d', 'ticket_promedio'])

    return data, unavailable, api_calls


def _capturar_engagement(item_id: str, headers: dict) -> tuple[dict, list, int]:
    data: dict = {
        'preguntas_pendientes':       None,
        'preguntas_total_30d':        None,
        'resenas_cantidad':           None,
        'resenas_promedio_estrellas': None,
    }
    unavailable: list = []
    api_calls = 0

    # Preguntas sin responder
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/questions/search',
                         headers=headers,
                         params={'item': item_id, 'status': 'UNANSWERED', 'limit': 1},
                         timeout=6)
        if r.ok:
            data['preguntas_pendientes'] = int(r.json().get('total') or 0)
    except Exception:
        unavailable.append('preguntas_pendientes')

    # Preguntas totales últimos 30d
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/questions/search',
                         headers=headers,
                         params={'item': item_id,
                                 'date_from': _ago(30),
                                 'limit': 1},
                         timeout=6)
        if r.ok:
            data['preguntas_total_30d'] = int(r.json().get('total') or 0)
    except Exception:
        unavailable.append('preguntas_total_30d')

    # Reseñas
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/reviews/item/{item_id}',
                         headers=headers, params={'limit': 1}, timeout=6)
        if r.ok:
            body = r.json()
            data['resenas_cantidad']           = int(body.get('paging', {}).get('total') or 0)
            data['resenas_promedio_estrellas'] = (body.get('rating_average') or
                                                   body.get('average') or None)
    except Exception:
        unavailable.append('resenas_cantidad')
        unavailable.append('resenas_promedio_estrellas')

    return data, unavailable, api_calls


def _capturar_salud(user_id: str, headers: dict) -> tuple[dict, list, int]:
    data: dict = {
        'reclamos_30d':      None,
        'cancelaciones_30d': None,
        'mediaciones_30d':   None,
    }
    unavailable: list = []
    api_calls = 0

    # Reclamos: vienen del seller_reputation
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/users/{user_id}',
                         headers=headers, timeout=6)
        if r.ok:
            rep = (r.json().get('seller_reputation') or {})
            metrics = rep.get('metrics') or {}
            claims = metrics.get('claims') or {}
            data['reclamos_30d'] = int(claims.get('value') or 0)
    except Exception:
        unavailable.append('reclamos_30d')

    # Cancelaciones — orders con status=cancelled
    try:
        api_calls += 1
        r = requests.get(f'{_ML}/orders/search',
                         headers=headers,
                         params={'seller': user_id,
                                 'order.status': 'cancelled',
                                 'order.date_created.from': _ago(30),
                                 'limit': 1},
                         timeout=8)
        if r.ok:
            data['cancelaciones_30d'] = int(r.json().get('paging', {}).get('total') or 0)
    except Exception:
        unavailable.append('cancelaciones_30d')

    # Mediaciones — endpoint /users/{id}/claims requiere permiso Postventa
    # (sabido del Sprint 0 que generalmente da 403). Marcamos unavailable.
    unavailable.append('mediaciones_30d')

    return data, unavailable, api_calls


# ── Función principal ───────────────────────────────────────────────────────

def capturar_baseline_completo(item_id: str, alias: str, client,
                               top_keywords: list[str] | None = None,
                               data_dir: str = '') -> dict:
    """Captura baseline completo síncrono.

    Args:
      item_id: ID de la publicación ML.
      alias:   alias de la cuenta (para fallbacks a JSONs locales).
      client:  MLClient con auth.
      top_keywords: lista de keywords a trackear (top 3 según priority_score).
                    Si es None, se intenta leer de posiciones_<alias>.json.
      data_dir: ruta a /data del proyecto, para fallbacks.

    Returns:
      dict con la estructura de baseline v2 + métricas legacy en root + diagnóstico.
    """
    t_total = time.time()
    breakdown: dict[str, int] = {}

    # Asegurar token + obtener user_id + headers
    client._ensure_token()
    headers = {'Authorization': f'Bearer {client.account.access_token}'}
    user_id = str(client.account.user_id or '')

    # ── Capturar cada sección, midiendo tiempo ───────────────────────────────
    t = time.time()
    visibilidad, vis_unav, vis_calls = _capturar_visibilidad(
        item_id, headers, top_keywords or [], alias, data_dir,
    )
    breakdown['visibilidad'] = _ms_since(t)

    t = time.time()
    trafico, traf_unav, traf_calls = _capturar_trafico(item_id, headers)
    breakdown['trafico'] = _ms_since(t)

    t = time.time()
    ventas, ventas_unav, ventas_calls = _capturar_ventas(item_id, user_id, headers)
    breakdown['ventas'] = _ms_since(t)

    t = time.time()
    engagement, eng_unav, eng_calls = _capturar_engagement(item_id, headers)
    breakdown['engagement'] = _ms_since(t)

    t = time.time()
    salud, salud_unav, salud_calls = _capturar_salud(user_id, headers)
    breakdown['salud'] = _ms_since(t)

    total_calls = vis_calls + traf_calls + ventas_calls + eng_calls + salud_calls
    total_unavailable = vis_unav + traf_unav + ventas_unav + eng_unav + salud_unav

    # ── Calcular conversiones (derivadas) ────────────────────────────────────
    if trafico['visitas_7d'] and ventas['unidades_7d'] is not None:
        if trafico['visitas_7d'] > 0:
            trafico['conversion_7d'] = round(
                ventas['unidades_7d'] / trafico['visitas_7d'] * 100, 2,
            )
        else:
            trafico['conversion_7d'] = 0.0

    if trafico['visitas_30d'] and ventas['unidades_30d'] is not None:
        if trafico['visitas_30d'] > 0:
            trafico['conversion_30d'] = round(
                ventas['unidades_30d'] / trafico['visitas_30d'] * 100, 2,
            )
        else:
            trafico['conversion_30d'] = 0.0

    # ── Armar baseline final con campos legacy en root + secciones nuevas ────
    fecha = datetime.now().strftime('%Y-%m-%d %H:%M')
    pos_kws = visibilidad.get('posicion_top_keywords') or []
    posicion_root = pos_kws[0]['posicion'] if pos_kws else None
    posicion_kw_root = pos_kws[0]['kw'] if pos_kws else None

    baseline = {
        # ── Metadata ──
        'fecha':   fecha,
        'version': _BASELINE_VERSION,

        # ── Legacy fields (compat con UI vieja del Monitor) ──
        'visitas_7d':   trafico.get('visitas_7d'),
        'ventas_30d':   ventas.get('unidades_30d'),
        'ventas_total': ventas.get('ventas_total_historica'),
        'conv_pct':     trafico.get('conversion_30d'),
        'posicion':     posicion_root,
        'posicion_kw':  posicion_kw_root,

        # ── Secciones nuevas ──
        'visibilidad': visibilidad,
        'trafico':     trafico,
        'ventas':      ventas,
        'engagement':  engagement,
        'salud':       salud,

        # ── Diagnóstico ──
        '_unavailable':    sorted(set(total_unavailable)),
        '_capture_errors': [],
        '_capture_metrics': {
            'ml_api_calls_count':     total_calls,
            'claude_api_calls_count': 0,
            'duration_ms':            _ms_since(t_total),
            'duration_breakdown':     breakdown,
        },
    }

    return baseline


# ── Captura async ───────────────────────────────────────────────────────────

def capturar_baseline_async(item_id: str, alias: str, client, data_dir: str,
                            top_keywords: list[str] | None = None,
                            on_complete: Callable | None = None,
                            publicacion_original_mla: str | None = None) -> threading.Thread:
    """Lanza la captura en thread daemon. Cuando termina, escribe el baseline
    en monitor_evolucion.json reemplazando el flag _capturing.

    Si publicacion_original_mla está seteado, captura ADEMÁS un snapshot de esa
    publicación y lo guarda en publicacion_original.snapshot_al_republicar.
    """
    def _run():
        try:
            baseline = capturar_baseline_completo(
                item_id, alias, client, top_keywords=top_keywords, data_dir=data_dir,
            )
            extras: dict = {}
            if publicacion_original_mla:
                try:
                    snap_orig = capturar_baseline_completo(
                        publicacion_original_mla, alias, client,
                        top_keywords=top_keywords, data_dir=data_dir,
                    )
                    extras['publicacion_original'] = {
                        'mla':                    publicacion_original_mla,
                        'snapshot_al_republicar': snap_orig,
                        'snapshots':              [],
                        'ultimo_snapshot':        None,
                        'estado_ml':              'active',
                        'fecha_estado':           datetime.now().strftime('%Y-%m-%d %H:%M'),
                    }
                except Exception as e:
                    _logger.warning('[baseline_capture] snapshot original %s falló: %s',
                                    publicacion_original_mla, e)

            _persistir_baseline_en_monitor(data_dir, item_id, alias, baseline, extras)
            if on_complete:
                on_complete(baseline)
        except Exception as e:
            _logger.error('[baseline_capture] error capturando %s: %s', item_id, e)
            _persistir_capturing_error(data_dir, item_id, alias, str(e))

    t = threading.Thread(target=_run, daemon=True, name=f'baseline_capture_{item_id}')
    t.start()
    return t


# ── Persistencia en monitor_evolucion.json ───────────────────────────────────

def marcar_capturing(data_dir: str, item_id: str, alias: str) -> None:
    """Marca la entry como _capturing=True mientras corre la captura async."""
    mon_path = os.path.join(data_dir, 'monitor_evolucion.json')
    mon = _load_json(mon_path) or {'items': []}
    if not isinstance(mon, dict):
        mon = {'items': []}
    entry = next((it for it in mon.get('items', [])
                  if it.get('item_id') == item_id and it.get('alias') == alias), None)
    if entry is not None:
        entry['_capturing'] = True
        entry['_capture_started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _save_json(mon_path, mon)


def _persistir_baseline_en_monitor(data_dir: str, item_id: str, alias: str,
                                    baseline: dict, extras: dict | None = None) -> None:
    """Reemplaza el baseline de la entry y limpia el flag _capturing."""
    mon_path = os.path.join(data_dir, 'monitor_evolucion.json')
    mon = _load_json(mon_path) or {'items': []}
    if not isinstance(mon, dict):
        mon = {'items': []}
    entry = next((it for it in mon.get('items', [])
                  if it.get('item_id') == item_id and it.get('alias') == alias), None)
    if entry is not None:
        entry['baseline'] = baseline
        entry.pop('_capturing', None)
        entry.pop('_capture_started_at', None)
        if extras:
            entry.update(extras)
        _save_json(mon_path, mon)


def _persistir_capturing_error(data_dir: str, item_id: str, alias: str, err: str) -> None:
    """Si la captura falla, deja el flag pero con error para que la UI lo muestre."""
    mon_path = os.path.join(data_dir, 'monitor_evolucion.json')
    mon = _load_json(mon_path) or {'items': []}
    if not isinstance(mon, dict):
        mon = {'items': []}
    entry = next((it for it in mon.get('items', [])
                  if it.get('item_id') == item_id and it.get('alias') == alias), None)
    if entry is not None:
        entry['_capturing'] = False
        entry['_capture_error'] = err[:300]
        _save_json(mon_path, mon)


# ── Helpers privados ─────────────────────────────────────────────────────────

def _safe(alias: str) -> str:
    return alias.replace(' ', '_').replace('/', '-')


def _load_json(path: str):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
