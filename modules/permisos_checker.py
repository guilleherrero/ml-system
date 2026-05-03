"""
Permisos checker — Sprint 4.5

Centraliza la detección y reporte de los permisos de la API de MercadoLibre
que el sistema necesita. Reemplaza los chequeos dispersos en cada módulo.

5 permisos trackeados:
  - publicidad_read    — GET /advertising/product_ads/campaigns
  - publicidad_write   — PUT /advertising/product_ads/campaigns/{id}  (inferido por historial)
  - items_busqueda     — GET /sites/MLA/search?seller_id=...
  - postventa          — GET /v1/claims?role=respondent&limit=1
  - ordenes            — GET /orders/search?seller=...&limit=1

Estados posibles por permiso:
  - activo         — el endpoint responde 200
  - faltante       — el endpoint responde 401/403
  - parcial        — caso edge (read OK pero write rechazado, o respuesta vacía sospechosa)
  - desconocido    — no se pudo testear todavía (ej. publicidad_write sin historial)
  - error          — fallo de red/timeout, no decisivo

Cache: data/permisos_{alias}.json con TTL 6h.
Log de inferencia para publicidad_write: data/permisos_inferencia_{alias}.json,
actualizado cada vez que un endpoint write devuelve un status definitivo.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

from core.ml_client import MLClient

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
ML_BASE  = 'https://api.mercadolibre.com'

CACHE_TTL_SECONDS = 6 * 3600   # 6 horas

# ── Catálogo de permisos ─────────────────────────────────────────────────────

PERMISOS_CATALOGO = [
    {
        'id': 'publicidad_read',
        'nombre': 'Publicidad (lectura)',
        'que_desbloquea': 'Ver métricas de campañas Meli ADS, ROAS, gasto, ítems en ads.',
        'afecta_secciones': ['/meli-ads'],
        'critico': False,
    },
    {
        'id': 'publicidad_write',
        'nombre': 'Publicidad (escritura)',
        'que_desbloquea': 'Pausar campañas, cambiar presupuesto, mover productos.',
        'afecta_secciones': ['/meli-ads'],
        'critico': True,   # bloquea acción real, no solo visualización
    },
    {
        'id': 'items_busqueda',
        'nombre': 'Ítems y búsqueda',
        'que_desbloquea': 'Rastreo de posiciones reales, análisis de precios de competidores.',
        'afecta_secciones': ['/posiciones', '/competencia'],
        'critico': False,
    },
    {
        'id': 'postventa',
        'nombre': 'Postventa',
        'que_desbloquea': 'Detalle de cada reclamo (motivo, mensajes del comprador, resolución).',
        'afecta_secciones': ['/reputacion'],
        'critico': False,
    },
    {
        'id': 'ordenes',
        'nombre': 'Órdenes',
        'que_desbloquea': 'Histórico de órdenes para Ventas, lanzamientos y dashboard.',
        'afecta_secciones': ['/ventas', '/reputacion'],
        'critico': False,
    },
]


# ── Helpers de IO ────────────────────────────────────────────────────────────

def _safe_alias(alias: str) -> str:
    return alias.replace(' ', '_').replace('/', '-')


def _cache_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f'permisos_{_safe_alias(alias)}.json')


def _inferencia_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f'permisos_inferencia_{_safe_alias(alias)}.json')


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


# ── Inferencia de publicidad_write ──────────────────────────────────────────

def log_write_attempt(alias: str, permiso_id: str, endpoint: str, status_code: int) -> None:
    """
    Registra el resultado de un intento de escritura para inferir el estado
    del permiso correspondiente. Llamado por hooks en módulos como meli_ads_engine
    cuando ejecutan PUT/POST/DELETE.

    status_code 200/204 → activo
    status_code 401/403 → faltante
    Otros → no concluyente, se ignora.
    """
    if status_code not in (200, 204, 401, 403):
        return
    path = _inferencia_path(alias)
    data = _read_json(path)
    data[permiso_id] = {
        'ultimo_status': status_code,
        'ultimo_at': _now_iso(),
        'ultimo_endpoint': endpoint,
    }
    _write_json(path, data)


def _read_inferencia(alias: str, permiso_id: str) -> dict:
    return _read_json(_inferencia_path(alias)).get(permiso_id, {})


# ── Tests por permiso ────────────────────────────────────────────────────────

def _test_publicidad_read(token: str) -> dict:
    try:
        r = requests.get(f'{ML_BASE}/advertising/product_ads/campaigns',
                         headers={'Authorization': f'Bearer {token}'},
                         timeout=8)
        if r.status_code == 200:
            return {'estado': 'activo'}
        if r.status_code in (401, 403):
            return {'estado': 'faltante', 'http_code': r.status_code,
                    'mensaje': 'GET de campañas devolvió 401/403 — falta el toggle de Publicidad en el panel ML.'}
        return {'estado': 'error', 'http_code': r.status_code,
                'mensaje': f'Respuesta inesperada: HTTP {r.status_code}'}
    except requests.RequestException as e:
        return {'estado': 'error', 'mensaje': f'Error de red: {e}'}


def _test_publicidad_write(alias: str) -> dict:
    """Inferencia desde el log — no hace request directo (sin endpoint idempotente)."""
    inf = _read_inferencia(alias, 'publicidad_write')
    if not inf:
        return {'estado': 'desconocido',
                'mensaje': 'No probamos este permiso todavía. Se actualizará cuando intentes una acción de escritura (cambiar presupuesto, pausar campaña, etc.).'}
    status = inf.get('ultimo_status')
    if status in (200, 204):
        return {'estado': 'activo',
                'mensaje': f'Última escritura exitosa el {inf.get("ultimo_at")}.'}
    if status in (401, 403):
        return {'estado': 'faltante', 'http_code': status,
                'mensaje': f'Última escritura ({inf.get("ultimo_endpoint","?")}) devolvió {status}. Activá Read+Write en el panel ML y reconectá la cuenta.'}
    return {'estado': 'desconocido', 'mensaje': f'Estado inferido {status} no concluyente.'}


def _test_items_busqueda(token: str, user_id) -> dict:
    if not user_id:
        return {'estado': 'error', 'mensaje': 'No se pudo obtener user_id de la cuenta.'}
    try:
        r = requests.get(f'{ML_BASE}/sites/MLA/search',
                         headers={'Authorization': f'Bearer {token}'},
                         params={'seller_id': user_id, 'limit': 1},
                         timeout=8)
        if r.status_code == 200:
            data = r.json()
            results = data.get('results') if isinstance(data, dict) else None
            if results is None:
                return {'estado': 'parcial', 'http_code': 200,
                        'mensaje': 'La API respondió 200 pero sin la clave "results" — formato inesperado.'}
            return {'estado': 'activo'}
        if r.status_code in (401, 403):
            return {'estado': 'faltante', 'http_code': r.status_code,
                    'mensaje': 'GET /sites/MLA/search devolvió 401/403 — falta el toggle "Ítems y búsqueda" en el panel ML.'}
        return {'estado': 'error', 'http_code': r.status_code,
                'mensaje': f'Respuesta inesperada: HTTP {r.status_code}'}
    except requests.RequestException as e:
        return {'estado': 'error', 'mensaje': f'Error de red: {e}'}


def _test_postventa(token: str) -> dict:
    try:
        r = requests.get(f'{ML_BASE}/v1/claims',
                         headers={'Authorization': f'Bearer {token}'},
                         params={'role': 'respondent', 'limit': 1},
                         timeout=8)
        if r.status_code == 200:
            return {'estado': 'activo'}
        if r.status_code in (401, 403):
            return {'estado': 'faltante', 'http_code': r.status_code,
                    'mensaje': 'GET /v1/claims devolvió 401/403 — el permiso Postventa requiere acuerdo formal con MercadoLibre.'}
        return {'estado': 'error', 'http_code': r.status_code,
                'mensaje': f'Respuesta inesperada: HTTP {r.status_code}'}
    except requests.RequestException as e:
        return {'estado': 'error', 'mensaje': f'Error de red: {e}'}


def _test_ordenes(token: str, user_id) -> dict:
    if not user_id:
        return {'estado': 'error', 'mensaje': 'No se pudo obtener user_id de la cuenta.'}
    try:
        r = requests.get(f'{ML_BASE}/orders/search',
                         headers={'Authorization': f'Bearer {token}'},
                         params={'seller': user_id, 'limit': 1},
                         timeout=8)
        if r.status_code == 200:
            return {'estado': 'activo'}
        if r.status_code in (401, 403):
            return {'estado': 'faltante', 'http_code': r.status_code,
                    'mensaje': 'GET /orders/search devolvió 401/403 — falta el toggle "Órdenes" en el panel ML.'}
        return {'estado': 'error', 'http_code': r.status_code,
                'mensaje': f'Respuesta inesperada: HTTP {r.status_code}'}
    except requests.RequestException as e:
        return {'estado': 'error', 'mensaje': f'Error de red: {e}'}


# ── Función principal ───────────────────────────────────────────────────────

def check_permisos(client: MLClient, alias: str, force_refresh: bool = False) -> dict:
    """
    Devuelve el estado de los 5 permisos para una cuenta. Usa cache TTL=6h
    salvo que force_refresh=True. Hace 4 requests HTTP cuando se ejecuta
    (publicidad_write se infiere del log local, sin request).
    """
    if not force_refresh:
        cached = _read_json(_cache_path(alias))
        if cached and cached.get('checked_at'):
            try:
                cached_at = datetime.fromisoformat(cached['checked_at'])
                age = (datetime.now().astimezone() - cached_at).total_seconds()
                if age < CACHE_TTL_SECONDS:
                    cached['from_cache'] = True
                    cached['cache_age_seconds'] = int(age)
                    return cached
            except Exception:
                pass

    client._ensure_token()
    token = client.account.access_token
    user_id = client.account.user_id

    started = time.time()
    tests = {
        'publicidad_read':  _test_publicidad_read(token),
        'publicidad_write': _test_publicidad_write(alias),
        'items_busqueda':   _test_items_busqueda(token, user_id),
        'postventa':        _test_postventa(token),
        'ordenes':          _test_ordenes(token, user_id),
    }

    permisos = []
    for cat in PERMISOS_CATALOGO:
        result = tests.get(cat['id'], {'estado': 'error', 'mensaje': 'Sin test definido.'})
        permisos.append({
            **cat,
            'estado': result.get('estado'),
            'http_code': result.get('http_code'),
            'mensaje': result.get('mensaje'),
        })

    estados = [p['estado'] for p in permisos]
    resumen = {
        'total':              len(permisos),
        'activos':            estados.count('activo'),
        'faltantes':          estados.count('faltante'),
        'parciales':          estados.count('parcial'),
        'desconocidos':       estados.count('desconocido'),
        'errores':            estados.count('error'),
        'criticos_faltantes': sum(1 for p in permisos
                                  if p['estado'] == 'faltante' and p.get('critico')),
    }

    payload = {
        'alias':       alias,
        'checked_at':  _now_iso(),
        'check_time_ms': int((time.time() - started) * 1000),
        'permisos':    permisos,
        'resumen':     resumen,
        'from_cache':  False,
    }
    _write_json(_cache_path(alias), payload)
    return payload


def get_permisos_summary(alias: str) -> dict:
    """
    Lectura rápida del resumen desde cache, sin tocar la API. Para el banner
    global que se renderiza en cada página — debe ser O(1).
    Devuelve {} si no hay cache todavía.
    """
    cached = _read_json(_cache_path(alias))
    if not cached:
        return {}
    return {
        'alias':      alias,
        'checked_at': cached.get('checked_at'),
        'resumen':    cached.get('resumen', {}),
    }
