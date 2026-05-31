"""
Sincronización del catálogo Biobella desde la cuenta MercadoLibre "novara".

Reglas:
- Una sola cuenta ML: 'novara'. Si no está conectada, la sync falla con error claro.
- Lock por campo: si existe un row en product_locks para (product, field), ese
  campo NO se actualiza desde ML — la edición manual del admin prevalece.
- Rate limit: máx 10 req/s. Implementado con time.sleep(0.1) entre llamadas.
- Retry exponencial en 429 y 5xx: 3 reintentos con backoff 1s, 3s, 9s.
- Productos que ya no aparecen en ML (eliminados/pausados/cerrados) se marcan
  activo=False — NO se borran de la DB (preservar historial de órdenes).

Uso:
    from modules.tienda_sync import sync_catalogo
    result = sync_catalogo(trigger='manual', triggered_by='guille')
    # result: {'sync_log_id': N, 'creados': X, 'actualizados': Y, 'errores': [...]}
"""
import json
import time
from datetime import datetime
from typing import Iterable

from core.account_manager import AccountManager
from core.ml_client import MLClient, MLApiError
from web.db import session_scope
from web.models_tienda import Product, ProductLock, SyncLog, AppSetting

ML_ACCOUNT_ALIAS = 'novara'   # se resuelve case-insensitive contra accounts existentes
RATE_LIMIT_SLEEP = 0.1   # 10 req/s
MAX_RETRIES      = 3
BACKOFF_BASE_SEC = 1.0   # 1, 3, 9


def _resolve_account_alias(mgr) -> str | None:
    """Encuentra el alias real (case-insensitive) que matchea ML_ACCOUNT_ALIAS.
    Devuelve None si no hay cuenta que coincida."""
    target = ML_ACCOUNT_ALIAS.lower()
    for acc in mgr.list_accounts():
        if (acc.alias or '').lower() == target:
            return acc.alias
    return None


# ── SEO auto-generator ────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convierte texto a slug url-safe simple."""
    import re, unicodedata
    if not text:
        return ''
    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    t = t.lower()
    t = re.sub(r'[^a-z0-9]+', '-', t).strip('-')
    return t[:80]


def generate_seo_template(titulo: str, descripcion: str, store_name: str = 'Biobella') -> tuple[str, str]:
    """
    Genera meta_title y meta_description con un template determinístico — rápido,
    sin costo de IA. Sirve como default; el admin puede regenerar con IA después.

    Reglas (Google-friendly):
    - meta_title: 50-60 chars, "{titulo} | {store}" truncado
    - meta_description: 150-160 chars, summary del producto + CTA con keyword "comprá"
    """
    titulo = (titulo or '').strip()
    descripcion_raw = (descripcion or '').strip().replace('\n', ' ').replace('  ', ' ')

    # ── meta_title ──
    sufijo = f' | {store_name}'
    max_title = 60
    if titulo:
        if len(titulo) + len(sufijo) <= max_title:
            meta_title = titulo + sufijo
        else:
            corte = max_title - len(sufijo) - 1
            meta_title = titulo[:corte].rstrip() + '…' + sufijo
    else:
        meta_title = store_name

    # ── meta_description ──
    max_desc = 160
    # Tomamos las primeras ~110 chars de la descripción, agregamos CTA
    cta = f' Comprá online en {store_name} con envío a todo el país.'
    cuerpo_max = max_desc - len(cta)
    if descripcion_raw:
        if len(descripcion_raw) <= cuerpo_max:
            cuerpo = descripcion_raw
        else:
            cuerpo = descripcion_raw[:cuerpo_max - 1].rstrip(',.;: ') + '…'
        meta_description = cuerpo + cta
    else:
        # Sin descripción: usar título como base
        meta_description = (f'{titulo}.{cta}' if titulo else f'Productos {store_name}.{cta}')[:max_desc]

    return meta_title, meta_description


def maybe_autofill_seo(product, store_name: str = 'Biobella') -> bool:
    """Llena meta_title/description/slug si están vacíos. NO pisa overrides manuales.
    Devuelve True si modificó algo."""
    changed = False
    if not (product.meta_title or '').strip():
        mt, _ = generate_seo_template(product.titulo, product.descripcion, store_name)
        product.meta_title = mt
        changed = True
    if not (product.meta_description or '').strip():
        _, md = generate_seo_template(product.titulo, product.descripcion, store_name)
        product.meta_description = md
        changed = True
    if not (product.slug or '').strip():
        product.slug = _slugify(product.titulo) or product.mla_id.lower()
        changed = True
    return changed


# ── Settings helpers ──────────────────────────────────────────────────────────

def get_setting(session, key: str, default=None):
    """Lee un app_setting; devuelve `default` si no existe o el JSON es inválido."""
    row = session.get(AppSetting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return default


def set_setting(session, key: str, value):
    row = session.get(AppSetting, key)
    payload = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(AppSetting(key=key, value=payload))
    else:
        row.value = payload


# ── ML API con retry ──────────────────────────────────────────────────────────

def _call_with_retry(fn, *args, **kwargs):
    """
    Ejecuta fn(*args, **kwargs) con retry exponencial en 429 o 5xx.
    Otros errores se propagan tal cual.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except MLApiError as e:
            status = getattr(e, 'status_code', None) or 0
            retriable = status == 429 or (500 <= status < 600)
            if not retriable or attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_BASE_SEC * (3 ** attempt)
            time.sleep(wait)
            last_exc = e
    raise last_exc  # defensivo, no debería llegar acá


def _iter_all_active_item_ids(client: MLClient) -> Iterable[str]:
    """Generator: paginado de items activos de la cuenta."""
    offset = 0
    page_size = 50
    while True:
        time.sleep(RATE_LIMIT_SLEEP)
        page = _call_with_retry(client.get_my_listings, limit=page_size, offset=offset, status='active')
        results = page.get('results') or []
        if not results:
            break
        for item_id in results:
            yield item_id
        total = (page.get('paging') or {}).get('total', 0)
        offset += page_size
        if offset >= total:
            break


def _fetch_description(client: MLClient, item_id: str) -> str:
    """ML expone description en endpoint separado. Devuelve '' si no existe."""
    try:
        time.sleep(RATE_LIMIT_SLEEP)
        data = _call_with_retry(client._get, f'/items/{item_id}/description')
        return (data or {}).get('plain_text') or ''
    except MLApiError as e:
        if getattr(e, 'status_code', None) == 404:
            return ''
        raise


# ── Mapeo ML → modelo local ───────────────────────────────────────────────────

def _extract_fields_from_ml(item: dict, descripcion: str) -> dict:
    """
    Mapea un item raw de ML a los campos sincronizables de Product.
    Lo que NO está acá nunca se toca por sync (precio_tienda_override,
    margen_override, slug, meta_*, activo, locks).
    """
    return {
        'titulo':      item.get('title') or '',
        'descripcion': descripcion or '',
        'fotos':       [p.get('secure_url') or p.get('url') for p in (item.get('pictures') or []) if p.get('secure_url') or p.get('url')],
        'variantes':   item.get('variations') or [],
        'stock':       int(item.get('available_quantity') or 0),
        'precio_ml':   float(item.get('price') or 0),
    }


def _locked_fields_for(session, product_id: int) -> set[str]:
    if product_id is None:
        return set()
    rows = session.query(ProductLock.field).filter(ProductLock.product_id == product_id).all()
    return {r[0] for r in rows}


def _apply_synced_fields(product: Product, ml_fields: dict, locked: set[str]) -> bool:
    """
    Aplica los campos de ML al Product, respetando locks.
    'precio_ml' siempre se actualiza (es un valor de referencia, no editable —
    el lock 'precio' aplica al precio_tienda_override, no al precio_ml).
    Devuelve True si hubo algún cambio real.
    """
    changed = False

    # Mapeo field-name de lock → atributo en Product
    field_to_attr = {
        'titulo':      'titulo',
        'descripcion': 'descripcion',
        'fotos':       'fotos',
        'variantes':   'variantes',
        'stock':       'stock',
    }

    for lock_field, attr in field_to_attr.items():
        if lock_field in locked:
            continue
        new_val = ml_fields[lock_field]
        if getattr(product, attr) != new_val:
            setattr(product, attr, new_val)
            changed = True

    # precio_ml: siempre se actualiza (la sync no lo "edita" — es el valor crudo de ML)
    new_precio = ml_fields['precio_ml']
    if float(product.precio_ml or 0) != float(new_precio):
        product.precio_ml = new_precio
        changed = True

    return changed


# ── Sync principal ────────────────────────────────────────────────────────────

def sync_catalogo(trigger: str = 'auto', triggered_by: str | None = None) -> dict:
    """
    Sincroniza el catálogo desde ML. Persiste un SyncLog.

    Args:
        trigger: 'auto' (cron) o 'manual' (botón admin)
        triggered_by: username si manual

    Returns: dict con resumen — útil para mostrar en UI.
    """
    started = datetime.now()

    # 1) Resolver client de ML (fuera del session_scope para fallar rápido si la
    #    cuenta no existe o el token no se puede refrescar)
    try:
        mgr = AccountManager()
        real_alias = _resolve_account_alias(mgr)
        if not real_alias:
            raise ValueError(f"Cuenta ML '{ML_ACCOUNT_ALIAS}' no existe (busqué case-insensitive)")
        client = mgr.get_client(real_alias)
    except Exception as e:
        # Persistimos un log de error aunque no haya conexión
        with session_scope() as s:
            log = SyncLog(
                started_at=started,
                finished_at=datetime.now(),
                trigger=trigger,
                triggered_by=triggered_by,
                status='error',
                error_resumen=f'No se pudo obtener client de ML "{ML_ACCOUNT_ALIAS}": {e}',
            )
            s.add(log)
            s.flush()
            log_id = log.id
        return {'sync_log_id': log_id, 'status': 'error', 'error': str(e)}

    # 2) Crear el SyncLog en su propia transacción para que sobreviva aun si
    #    el resto de la corrida revienta.
    creados = actualizados = desactivados = 0
    errores: list[dict] = []
    mla_ids_vistos: set[str] = set()
    fatal_error: str | None = None
    log_id: int | None = None

    with session_scope() as s:
        log = SyncLog(
            started_at=started,
            trigger=trigger,
            triggered_by=triggered_by,
            status='running',
        )
        s.add(log)
        s.flush()
        log_id = log.id

    # 3) Sync de productos en una segunda sesión. Si revienta, persistimos el
    #    error en una tercera sesión (no se pierde el SyncLog).
    try:
        with session_scope() as s:
            for item_id in _iter_all_active_item_ids(client):
                mla_ids_vistos.add(item_id)
                try:
                    time.sleep(RATE_LIMIT_SLEEP)
                    item = _call_with_retry(client.get_item, item_id)
                    descripcion = _fetch_description(client, item_id)
                    ml_fields = _extract_fields_from_ml(item, descripcion)

                    product = s.query(Product).filter(Product.mla_id == item_id).one_or_none()
                    is_new = product is None

                    if is_new:
                        product = Product(mla_id=item_id, activo=True)
                        s.add(product)
                        _apply_synced_fields(product, ml_fields, locked=set())
                        product.last_sync_at = datetime.now()
                        # SEO auto-generado para productos nuevos
                        store_name = get_setting(s, 'store_name', 'Biobella')
                        maybe_autofill_seo(product, store_name)
                        s.flush()
                        creados += 1
                    else:
                        locked = _locked_fields_for(s, product.id)
                        if _apply_synced_fields(product, ml_fields, locked):
                            actualizados += 1
                        if not product.activo:
                            product.activo = True
                        product.last_sync_at = datetime.now()
                        # Si el producto existe pero le falta SEO (creado antes de esta feature), llenarlo
                        store_name = get_setting(s, 'store_name', 'Biobella')
                        maybe_autofill_seo(product, store_name)

                except Exception as item_err:
                    errores.append({'mla_id': item_id, 'msg': str(item_err)})

            if mla_ids_vistos:
                stale = s.query(Product).filter(
                    Product.activo == True,  # noqa: E712
                    ~Product.mla_id.in_(mla_ids_vistos),
                ).all()
                for p in stale:
                    p.activo = False
                    desactivados += 1
    except Exception as outer:
        fatal_error = str(outer)

    # 4) Actualizar el SyncLog con el resultado final (siempre se ejecuta).
    with session_scope() as s:
        log = s.get(SyncLog, log_id)
        if log is not None:
            log.finished_at = datetime.now()
            log.productos_creados = creados
            log.productos_actualizados = actualizados
            log.productos_desactivados = desactivados
            log.errores = errores
            if fatal_error:
                log.status = 'error'
                log.error_resumen = fatal_error
            elif errores:
                log.status = 'partial'
            else:
                log.status = 'ok'

    return {
        'sync_log_id':            log_id,
        'status':                 'partial' if errores else 'ok',
        'creados':                creados,
        'actualizados':           actualizados,
        'desactivados':           desactivados,
        'errores':                errores[:20],  # cap para no romper la UI
        'total_errores':          len(errores),
    }
