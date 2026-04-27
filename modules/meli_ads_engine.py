"""
Meli ADS Engine — lectura y normalización de CSVs por SKU.

CSVs esperados en data/raw/:
  ads_performance.csv  — sku, date, impressions, clicks, spend, conversions, revenue
  costs.csv            — sku, unit_cost
  stock.csv            — sku, stock_quantity, avg_daily_sales
  listings.csv         — sku, title, price, category, listing_type, free_shipping
  objectives.csv       — sku, objective (PROFIT | LAUNCH | LIQUIDATE_STOCK)
"""

import csv
import logging
import os
from typing import Any

_RAW_DIR      = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw')
_APPROVALS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'approvals')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _csv_path(filename: str) -> str:
    return os.path.join(_RAW_DIR, filename)


def _read_csv(filename: str) -> list[dict]:
    path = _csv_path(filename)
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({k.strip(): v.strip() for k, v in row.items()})
    except Exception as e:
        logging.warning(f'[meli_ads] Error leyendo {filename}: {e}')
    return rows


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(',', '.').strip())
    except (ValueError, TypeError):
        return default


def _int(val: Any, default: int = 0) -> int:
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return default


def _str(val: Any, default: str = '') -> str:
    v = str(val).strip() if val is not None else ''
    return v if v else default


# ── Lectores por fuente ────────────────────────────────────────────────────────

def _load_ads_performance() -> dict[str, dict]:
    """Agrega métricas de ads por SKU sumando todas las filas (multi-fecha)."""
    rows = _read_csv('ads_performance.csv')
    agg: dict[str, dict] = {}
    for r in rows:
        sku = _str(r.get('sku'))
        if not sku:
            continue
        if sku not in agg:
            agg[sku] = {'impressions': 0, 'clicks': 0, 'spend': 0.0,
                        'conversions': 0, 'revenue': 0.0}
        agg[sku]['impressions']  += _int(r.get('impressions'))
        agg[sku]['clicks']       += _int(r.get('clicks'))
        agg[sku]['spend']        += _float(r.get('spend'))
        agg[sku]['conversions']  += _int(r.get('conversions'))
        agg[sku]['revenue']      += _float(r.get('revenue'))
    return agg


def _load_costs() -> dict[str, float]:
    rows = _read_csv('costs.csv')
    return {_str(r.get('sku')): _float(r.get('unit_cost'))
            for r in rows if _str(r.get('sku'))}


def _load_stock() -> dict[str, dict]:
    rows = _read_csv('stock.csv')
    result = {}
    for r in rows:
        sku = _str(r.get('sku'))
        if not sku:
            continue
        result[sku] = {
            'stock_quantity':  _int(r.get('stock_quantity')),
            'avg_daily_sales': _float(r.get('avg_daily_sales')),
        }
    return result


def _load_listings() -> dict[str, dict]:
    rows = _read_csv('listings.csv')
    result = {}
    for r in rows:
        sku = _str(r.get('sku'))
        if not sku:
            continue
        result[sku] = {
            'title':         _str(r.get('title')),
            'price':         _float(r.get('price')),
            'category':      _str(r.get('category')),
            'listing_type':  _str(r.get('listing_type')),
            'free_shipping': _str(r.get('free_shipping')).lower() in ('true', '1', 'si', 'sí', 'yes'),
        }
    return result


def _load_objectives() -> dict[str, str]:
    rows = _read_csv('objectives.csv')
    valid = {'PROFIT', 'LAUNCH', 'LIQUIDATE_STOCK'}
    result = {}
    for r in rows:
        sku = _str(r.get('sku'))
        obj = _str(r.get('objective')).upper()
        if sku:
            result[sku] = obj if obj in valid else ''
    return result


# ── Normalización unificada ────────────────────────────────────────────────────

def load_skus() -> dict[str, dict]:
    """
    Lee todos los CSVs y devuelve un dict {sku: datos_unificados}.
    Solo incluye SKUs que aparezcan al menos en listings o ads_performance.
    Campos faltantes quedan en sus defaults (0 / '' / False).
    """
    ads      = _load_ads_performance()
    costs    = _load_costs()
    stock    = _load_stock()
    listings = _load_listings()
    objectives = _load_objectives()

    # Universo de SKUs: unión de listings + ads (fuentes primarias)
    all_skus = set(listings.keys()) | set(ads.keys())

    result = {}
    for sku in sorted(all_skus):
        ad  = ads.get(sku, {})
        lst = listings.get(sku, {})
        stk = stock.get(sku, {})

        result[sku] = {
            # Identificación
            'sku':            sku,
            'title':          lst.get('title', ''),
            'category':       lst.get('category', ''),
            'listing_type':   lst.get('listing_type', ''),
            'free_shipping':  lst.get('free_shipping', False),

            # Precio y costo
            'price':          lst.get('price', 0.0),
            'unit_cost':      costs.get(sku, 0.0),

            # Stock
            'stock_quantity':  stk.get('stock_quantity', 0),
            'avg_daily_sales': stk.get('avg_daily_sales', 0.0),

            # Ads performance (agregado)
            'impressions':    ad.get('impressions', 0),
            'clicks':         ad.get('clicks', 0),
            'spend':          ad.get('spend', 0.0),
            'conversions':    ad.get('conversions', 0),
            'revenue_ads':    ad.get('revenue', 0.0),

            # Objetivo declarado
            'objective':      objectives.get(sku, ''),
        }

    return result


def calc_ad_metrics(sku_data: dict) -> dict:
    """
    Calcula métricas publicitarias para un SKU ya normalizado.
    Todos los valores son seguros (sin división por cero).

    CTR       = clicks / impressions
    CPC       = spend / clicks
    conv_rate = conversions / clicks
    ACoS      = spend / revenue_ads        (Advertising Cost of Sales)
    ROAS      = revenue_ads / spend        (Return on Ad Spend)
    """
    impressions  = sku_data.get('impressions', 0)
    clicks       = sku_data.get('clicks', 0)
    spend        = sku_data.get('spend', 0.0)
    conversions  = sku_data.get('conversions', 0)
    revenue_ads  = sku_data.get('revenue_ads', 0.0)

    ctr       = clicks / impressions   if impressions  > 0 else 0.0
    cpc       = spend  / clicks        if clicks       > 0 else 0.0
    conv_rate = conversions / clicks   if clicks       > 0 else 0.0
    acos      = spend  / revenue_ads   if revenue_ads  > 0 else None   # None = sin ventas ads
    roas      = revenue_ads / spend    if spend        > 0 else None   # None = sin inversión

    return {
        'ctr':       round(ctr, 4),        # 0.0234 = 2.34%
        'cpc':       round(cpc, 2),        # ARS por click
        'conv_rate': round(conv_rate, 4),  # 0.05 = 5%
        'acos':      round(acos, 4) if acos is not None else None,
        'roas':      round(roas, 2)  if roas is not None else None,
    }


def calc_profitability(sku_data: dict) -> dict:
    """
    Calcula rentabilidad real por SKU.

    Constantes ML Argentina:
      Comisión classic:  13%
      Comisión premium:  16.5%
      Envío gratis:      $700 ARS estimado
      IVA sobre comisión: 21%

    Fórmula margen neto:
      margen = price - unit_cost - comision - iva_comision - envio - spend_por_venta

    break_even_acos:
      máximo ACoS que el SKU puede soportar sin perder dinero (antes de ads)
      = margen_sin_ads / price
    """
    price        = sku_data.get('price', 0.0)
    unit_cost    = sku_data.get('unit_cost', 0.0)
    listing_type = sku_data.get('listing_type', '').lower()
    free_ship    = sku_data.get('free_shipping', False)
    conversions  = sku_data.get('conversions', 0)
    spend        = sku_data.get('spend', 0.0)

    # Comisión ML según tipo de publicación
    if listing_type in ('gold_special', 'gold_pro', 'premium'):
        commission_rate = 0.165
    else:
        commission_rate = 0.13

    commission     = round(price * commission_rate, 2)
    iva_commission = round(commission * 0.21, 2)
    shipping_cost  = 700.0 if free_ship else 0.0

    # Gasto en ads prorrateado por conversión (costo ads por venta generada)
    spend_per_sale = round(spend / conversions, 2) if conversions > 0 else 0.0

    # Revenue bruto (ventas totales del listing, no solo de ads)
    revenue_total = round(price * conversions, 2) if conversions > 0 else 0.0

    # Margen sin ads (para calcular break_even_acos)
    margin_before_ads = price - unit_cost - commission - iva_commission - shipping_cost
    margin_net        = margin_before_ads - spend_per_sale

    margin_pct     = round(margin_net / price, 4)       if price > 0 else 0.0
    break_even_acos = round(margin_before_ads / price, 4) if price > 0 else None

    return {
        'price':            round(price, 2),
        'unit_cost':        round(unit_cost, 2),
        'commission':       commission,
        'commission_rate':  commission_rate,
        'iva_commission':   iva_commission,
        'shipping_cost':    shipping_cost,
        'spend_per_sale':   spend_per_sale,
        'revenue_total':    revenue_total,
        'margin_net':       round(margin_net, 2),
        'margin_pct':       margin_pct,        # 0.18 = 18%
        'break_even_acos':  break_even_acos,   # None si price=0
    }


def resolve_objective(sku_data: dict) -> str:
    """
    Devuelve el objetivo del SKU.
    Fuente primaria: objectives.csv (campo 'objective' ya cargado en sku_data).
    Si no está declarado, infiere desde señales de stock y ventas como fallback.

    Reglas de inferencia (solo si objective vacío):
      - stock_quantity > 0 y avg_daily_sales == 0  → LIQUIDATE_STOCK (sin movimiento)
      - conversions == 0 y spend > 0               → LAUNCH (invirtiendo sin ventas)
      - margen_neto > 0                             → PROFIT (default rentable)
      - resto                                       → PROFIT
    """
    declared = sku_data.get('objective', '').strip().upper()
    if declared in ('PROFIT', 'LAUNCH', 'LIQUIDATE_STOCK'):
        return declared

    # Inferencia
    stock       = sku_data.get('stock_quantity', 0)
    daily_sales = sku_data.get('avg_daily_sales', 0.0)
    conversions = sku_data.get('conversions', 0)
    spend       = sku_data.get('spend', 0.0)

    if stock > 0 and daily_sales == 0:
        return 'LIQUIDATE_STOCK'
    if conversions == 0 and spend > 0:
        return 'LAUNCH'
    return 'PROFIT'


def decide_sku(sku_data: dict) -> dict:
    """
    Motor de decisión por objetivo. Devuelve score, decision y diagnostico.

    Score 0-100:
      - 80-100 → escalar / mantener activo
      - 50-79  → optimizar antes de escalar
      - 20-49  → reducir inversión
      - 0-19   → pausar

    Decision values: ESCALAR | MANTENER | OPTIMIZAR | REDUCIR | PAUSAR
    """
    objective   = sku_data.get('objective', 'PROFIT')
    ad          = sku_data.get('ad_metrics', {})
    prof        = sku_data.get('profitability', {})

    acos        = ad.get('acos')            # None si sin ventas
    ctr         = ad.get('ctr', 0.0)
    conv_rate   = ad.get('conv_rate', 0.0)
    margin_pct  = prof.get('margin_pct', 0.0)
    be_acos     = prof.get('break_even_acos')  # None si price=0
    clicks      = sku_data.get('clicks', 0)
    spend       = sku_data.get('spend', 0.0)
    conversions = sku_data.get('conversions', 0)
    stock       = sku_data.get('stock_quantity', 0)
    daily_sales = sku_data.get('avg_daily_sales', 0.0)

    # Guard: precio = 0 → imposible calcular rentabilidad
    if objective == 'PROFIT' and sku_data.get('price', 0.0) == 0.0:
        return {
            'score':       0,
            'decision':    'OPTIMIZAR',
            'diagnostico': ['Precio = 0 en listings.csv — no se puede calcular rentabilidad.'],
        }

    score      = 50   # base neutral
    diagnostico = []
    decision   = 'MANTENER'

    # ── PROFIT — escalar solo si rentable ────────────────────────────────────
    if objective == 'PROFIT':
        if acos is None and spend == 0:
            score = 30
            diagnostico.append('Sin inversión en ads — no hay datos de performance.')
            decision = 'OPTIMIZAR'

        elif acos is None and spend > 0:
            score = 15
            diagnostico.append('Inversión activa sin conversiones — ads no generan ventas.')
            decision = 'PAUSAR'

        else:
            # ACoS vs break_even
            if be_acos and acos < be_acos * 0.8:
                score += 30
                diagnostico.append(f'ACoS ({acos:.1%}) muy por debajo del break-even ({be_acos:.1%}) — rentabilidad sólida.')
                decision = 'ESCALAR'
            elif be_acos and acos < be_acos:
                score += 15
                diagnostico.append(f'ACoS ({acos:.1%}) bajo break-even ({be_acos:.1%}) — rentable pero con margen ajustado.')
                decision = 'MANTENER'
            elif be_acos and acos >= be_acos:
                score -= 20
                diagnostico.append(f'ACoS ({acos:.1%}) supera break-even ({be_acos:.1%}) — ads consumiendo el margen.')
                decision = 'REDUCIR'

            # CTR
            if ctr >= 0.03:
                score += 10
                diagnostico.append(f'CTR alto ({ctr:.2%}) — buena relevancia del anuncio.')
            elif ctr < 0.01 and clicks > 50:
                score -= 10
                diagnostico.append(f'CTR bajo ({ctr:.2%}) — creatividad o segmentación a revisar.')

            # Conversión
            if conv_rate >= 0.05:
                score += 10
                diagnostico.append(f'Tasa de conversión fuerte ({conv_rate:.2%}).')
            elif conv_rate < 0.02 and clicks > 30:
                score -= 10
                diagnostico.append(f'Conversión baja ({conv_rate:.2%}) — posible problema de precio o ficha.')

    # ── LAUNCH — no juzgar rápido, evaluar tracción ──────────────────────────
    elif objective == 'LAUNCH':
        if clicks < 100:
            score = 55
            diagnostico.append(f'Pocos clicks ({clicks}) — fase temprana, insuficiente para juzgar.')
            decision = 'MANTENER'
        elif conversions == 0 and clicks >= 100:
            score = 25
            diagnostico.append(f'{clicks} clicks sin conversiones — revisar ficha, precio o público objetivo.')
            decision = 'OPTIMIZAR'
        elif conversions > 0:
            if acos and be_acos and acos < be_acos * 1.5:
                score = 70
                diagnostico.append(f'Primeras conversiones con ACoS aceptable para lanzamiento ({acos:.1%}).')
                decision = 'MANTENER'
            else:
                score = 45
                diagnostico.append(f'Conversiones presentes pero ACoS elevado para lanzamiento.')
                decision = 'OPTIMIZAR'

        if ctr >= 0.02:
            score += 5
            diagnostico.append(f'CTR inicial aceptable ({ctr:.2%}).')

    # ── LIQUIDATE_STOCK — vender rápido con límite de pérdida ────────────────
    elif objective == 'LIQUIDATE_STOCK':
        days_stock = (stock / daily_sales) if daily_sales > 0 else None

        if stock == 0:
            score = 90
            diagnostico.append('Stock liquidado — objetivo cumplido.')
            decision = 'PAUSAR'
        elif days_stock and days_stock <= 30:
            score = 75
            diagnostico.append(f'Stock para ~{days_stock:.0f} días — ritmo de liquidación aceptable.')
            decision = 'MANTENER'
        elif days_stock and days_stock > 90:
            score = 30
            diagnostico.append(f'Stock para ~{days_stock:.0f} días — ritmo insuficiente, considerar bajar precio.')
            decision = 'REDUCIR'
        else:
            score = 50
            diagnostico.append(f'Stock para ~{days_stock:.0f} días — monitorear.' if days_stock else 'Sin datos de velocidad de venta.')
            decision = 'OPTIMIZAR'

        # No escalar ads si margen ya es negativo
        if margin_pct < 0:
            score = min(score, 35)
            diagnostico.append(f'Margen negativo ({margin_pct:.1%}) — no escalar inversión en ads.')
            if decision == 'ESCALAR':
                decision = 'MANTENER'

    score = max(0, min(100, score))

    return {
        'score':       score,
        'decision':    decision,
        'diagnostico': diagnostico,
    }


_ACTION_LABELS = {
    'increase_budget': 'Aumentar presupuesto',
    'decrease_budget': 'Reducir presupuesto',
    'pause':           'Pausar campaña',
    'keep':            'Mantener sin cambios',
    'fix_listing':     'Optimizar ficha/precio',
}


def suggest_action(sku_data: dict) -> dict:
    """
    Devuelve la acción concreta recomendada para el SKU, con justificación.

    Mapeo decision → acción principal:
      ESCALAR   → increase_budget
      MANTENER  → keep
      OPTIMIZAR → fix_listing  (si CTR/conv bajo) | decrease_budget (si ACoS alto)
      REDUCIR   → decrease_budget
      PAUSAR    → pause

    Condiciones de override:
      - CTR < 1% con > 50 clicks                     → fix_listing (ficha o creatividad)
      - conv_rate < 2% con > 30 clicks               → fix_listing (precio o ficha)
      - margin_pct < 0 y decision != PAUSAR           → decrease_budget
      - stock == 0 y objetivo LIQUIDATE_STOCK         → pause
    """
    engine      = sku_data.get('engine', {})
    ad          = sku_data.get('ad_metrics', {})
    prof        = sku_data.get('profitability', {})
    objective   = sku_data.get('objective', 'PROFIT')

    decision    = engine.get('decision', 'MANTENER')
    ctr         = ad.get('ctr', 0.0)
    conv_rate   = ad.get('conv_rate', 0.0)
    margin_pct  = prof.get('margin_pct', 0.0)
    clicks      = sku_data.get('clicks', 0)
    stock       = sku_data.get('stock_quantity', 0)

    # Acción base según decisión
    action_map = {
        'ESCALAR':   'increase_budget',
        'MANTENER':  'keep',
        'OPTIMIZAR': 'fix_listing',
        'REDUCIR':   'decrease_budget',
        'PAUSAR':    'pause',
    }
    action = action_map.get(decision, 'keep')

    # Overrides prioritarios
    if stock == 0 and objective == 'LIQUIDATE_STOCK':
        action = 'pause'
    elif objective == 'LIQUIDATE_STOCK' and decision == 'OPTIMIZAR':
        action = 'decrease_budget'
    elif margin_pct < 0 and action not in ('pause',):
        action = 'decrease_budget'
    elif clicks > 50  and ctr < 0.01 and action == 'fix_listing':
        action = 'fix_listing'   # confirmar: es ficha/creatividad
    elif clicks > 30 and conv_rate < 0.02 and action in ('keep', 'increase_budget'):
        action = 'fix_listing'   # buena llegada pero no convierte → ficha/precio

    # Justificación
    justificacion = _build_justification(action, sku_data, ad, prof)

    return {
        'action':          action,
        'action_label':    _ACTION_LABELS.get(action, action),
        'justificacion':   justificacion,
    }


def _build_justification(action: str, sku_data: dict, ad: dict, prof: dict) -> str:
    acos       = ad.get('acos')
    be_acos    = prof.get('break_even_acos')
    ctr        = ad.get('ctr', 0.0)
    conv_rate  = ad.get('conv_rate', 0.0)
    margin_pct = prof.get('margin_pct', 0.0)
    clicks     = sku_data.get('clicks', 0)
    stock      = sku_data.get('stock_quantity', 0)
    objective  = sku_data.get('objective', 'PROFIT')

    if action == 'increase_budget':
        if acos and be_acos:
            return (f'ACoS {acos:.1%} bien por debajo del break-even {be_acos:.1%}. '
                    f'Margen {margin_pct:.1%}. Escalar captura más ventas rentables.')
        return 'Performance positiva — aumentar presupuesto para capturar más volumen.'

    if action == 'decrease_budget':
        if margin_pct < 0:
            return f'Margen neto negativo ({margin_pct:.1%}). Reducir inversión hasta corregir rentabilidad.'
        if acos and be_acos and acos >= be_acos:
            return (f'ACoS {acos:.1%} supera break-even {be_acos:.1%}. '
                    f'Reducir presupuesto para proteger margen.')
        return 'Inversión por encima de lo que el margen puede sostener.'

    if action == 'pause':
        if stock == 0 and objective == 'LIQUIDATE_STOCK':
            return 'Stock liquidado — pausar campaña, objetivo cumplido.'
        return 'Sin conversiones o margen insuficiente para sostener inversión activa.'

    if action == 'fix_listing':
        parts = []
        if clicks > 50 and ctr < 0.01:
            parts.append(f'CTR muy bajo ({ctr:.2%}) con {clicks} clicks — revisar título, foto principal o relevancia del anuncio.')
        if clicks > 30 and conv_rate < 0.02:
            parts.append(f'Conversión baja ({conv_rate:.2%}) — revisar precio, descripción o ficha técnica.')
        return ' '.join(parts) or 'Ficha o precio necesitan mejora antes de escalar inversión.'

    if action == 'keep':
        return 'Performance estable dentro de parámetros aceptables. Monitorear sin cambios.'

    return ''


def classify_ads_priority(sku_data: dict) -> dict:
    """
    Clasifica el SKU en uno de 4 grupos de prioridad publicitaria.
    Usa exclusivamente campos ya calculados por el pipeline (engine, ad_metrics, profitability).

    Retorna dict con 'priority' (str) y 'reason' (str explicativo).

    Orden de evaluación (sin solapamiento):
      TOP         — score >= 70, margen positivo, conversiones > 0, motor no dice pausar/reducir/optimizar
      QUITAR      — clicks >= 50, spend > 0, no LAUNCH, conv_rate < 1%,
                    margen negativo o ACoS >> break-even
      MANTENIMIENTO — conversiones > 0, margen >= 0
      ACTIVAR     — todo lo demás
    """
    engine  = sku_data.get('engine', {})
    ad      = sku_data.get('ad_metrics', {})
    prof    = sku_data.get('profitability', {})

    score       = engine.get('score', 0)
    decision    = engine.get('decision', '')
    margin_pct  = prof.get('margin_pct', 0.0)
    be_acos     = prof.get('break_even_acos')
    acos        = ad.get('acos')
    conv_rate   = ad.get('conv_rate', 0.0)
    clicks      = sku_data.get('clicks', 0)
    conversions = sku_data.get('conversions', 0)
    spend       = sku_data.get('spend', 0.0)
    objective   = sku_data.get('objective', '')

    # TOP: rentable, con ventas, motor alineado (no cortar ni optimizar primero)
    if (score >= 70
            and margin_pct > 0
            and conversions > 0
            and decision not in ('PAUSAR', 'REDUCIR', 'OPTIMIZAR')):
        if acos is not None and be_acos is not None:
            reason = f'ACoS {acos:.1%} bajo break-even {be_acos:.1%} — score {score}/100. Escalar.'
        else:
            reason = f'Score {score}/100, margen {margin_pct:.1%}, {conversions} conversiones. Escalar.'
        return {'priority': 'TOP', 'reason': reason}

    # QUITAR: data suficiente + gasto activo + no es lanzamiento + sin retorno
    if (clicks >= 50
            and spend > 0
            and objective != 'LAUNCH'
            and conv_rate < 0.01
            and (margin_pct < 0
                 or (acos is not None and be_acos is not None and acos >= be_acos * 1.5))):
        if margin_pct < 0:
            reason = f'Margen negativo ({margin_pct:.1%}) con {clicks} clicks — gasto sin retorno.'
        else:
            reason = f'ACoS {acos:.1%} supera 1.5× break-even {be_acos:.1%} — inversión no rentable.'
        return {'priority': 'QUITAR', 'reason': reason}

    # MANTENIMIENTO: ventas estables con margen aceptable
    if conversions > 0 and margin_pct >= 0:
        reason = f'{conversions} conversiones, margen {margin_pct:.1%} — sostener inversión actual.'
        return {'priority': 'MANTENIMIENTO', 'reason': reason}

    # ACTIVAR: todo lo demás, con razón específica por sub-caso
    if objective == 'LAUNCH':
        reason = 'Lanzamiento en curso — acumular data antes de juzgar.'
    elif spend == 0:
        reason = 'Sin gasto en ads — sin señal para evaluar potencial.'
    elif clicks < 50:
        reason = f'Solo {clicks} clicks — insuficiente data, seguir midiendo.'
    else:
        reason = 'Sin conversiones con data limitada — optimizar ficha o segmentación.'
    return {'priority': 'ACTIVAR', 'reason': reason}


def suggest_budget(sku_data: dict) -> dict:
    """
    Sugiere acción presupuestaria concreta para el SKU.
    Solo recomendación — no ejecuta nada.

    Retorna:
      type   — 'increase' | 'maintain' | 'test' | 'decrease' | 'pause'
      pct    — int con signo (ej. +20, 0, -30) o None si type='pause'
      reason — string breve explicando la sugerencia
    """
    priority   = sku_data.get('ads_priority', 'ACTIVAR')
    engine     = sku_data.get('engine', {})
    ad         = sku_data.get('ad_metrics', {})
    prof       = sku_data.get('profitability', {})

    score      = engine.get('score', 0)
    decision   = engine.get('decision', '')
    margin_pct = prof.get('margin_pct', 0.0)
    be_acos    = prof.get('break_even_acos')
    acos       = ad.get('acos')
    clicks     = sku_data.get('clicks', 0)
    spend      = sku_data.get('spend', 0.0)
    objective  = sku_data.get('objective', '')

    # Guard global: si el motor indica no escalar, ningún grupo puede devolver 'increase'
    _motor_blocks_increase = decision in ('PAUSAR', 'REDUCIR', 'OPTIMIZAR')

    # ── TOP ──────────────────────────────────────────────────────────────────
    if priority == 'TOP':
        # Guard: margen < 10% → conservador sin importar ACoS ni score
        if margin_pct < 0.10:
            return {'type': 'maintain', 'pct': 0,
                    'reason': f'Margen ajustado ({margin_pct:.1%}) — mantener sin escalar.'}

        has_acos = acos is not None and be_acos and be_acos > 0

        if has_acos and acos < be_acos * 0.70 and score >= 85:
            pct = +30
            reason = f'ACoS {acos:.1%} muy por debajo del BE ({be_acos:.1%}) — escalar agresivo.'
        elif has_acos and acos < be_acos * 0.85 and score >= 70:
            pct = +20
            reason = f'ACoS {acos:.1%} bajo BE ({be_acos:.1%}) — escalar con confianza.'
        else:
            pct = +10
            reason = f'Rentable y eficiente (score {score}/100) — escalar gradual.'

        if _motor_blocks_increase:
            return {'type': 'maintain', 'pct': 0,
                    'reason': f'Motor indica {decision.lower()} — mantener sin escalar.'}
        return {'type': 'increase', 'pct': pct, 'reason': reason}

    # ── MANTENIMIENTO ─────────────────────────────────────────────────────────
    if priority == 'MANTENIMIENTO':
        has_acos = acos is not None and be_acos and be_acos > 0

        if score >= 60 and has_acos and acos < be_acos:
            if _motor_blocks_increase:
                return {'type': 'maintain', 'pct': 0,
                        'reason': f'Motor indica {decision.lower()} — mantener sin escalar.'}
            return {'type': 'increase', 'pct': +5,
                    'reason': 'Rendimiento estable con ACoS bajo BE — leve incremento.'}
        if score >= 40 and margin_pct >= 0:
            return {'type': 'maintain', 'pct': 0,
                    'reason': 'Performance dentro de parámetros — mantener inversión.'}
        return {'type': 'decrease', 'pct': -5,
                'reason': f'Margen ajustado ({margin_pct:.1%}) o score bajo — reducir levemente.'}

    # ── ACTIVAR ───────────────────────────────────────────────────────────────
    if priority == 'ACTIVAR':
        if spend == 0:
            return {'type': 'test', 'pct': 0,
                    'reason': 'Sin gasto activo — activar con presupuesto base mínimo.'}
        if objective == 'LAUNCH' or clicks < 50:
            return {'type': 'test', 'pct': +10,
                    'reason': 'Fase temprana — presupuesto de prueba controlado (+10%).'}
        return {'type': 'test', 'pct': +10,
                'reason': 'Data limitada — test controlado antes de escalar.'}

    # ── QUITAR ────────────────────────────────────────────────────────────────
    if priority == 'QUITAR':
        if margin_pct < -0.20:
            return {'type': 'pause', 'pct': None,
                    'reason': f'Margen muy negativo ({margin_pct:.1%}) — pausar campaña.'}
        has_acos = acos is not None and be_acos and be_acos > 0
        if margin_pct < 0 or (has_acos and acos > be_acos * 2.0):
            label = (f'ACoS {acos:.1%} duplica BE ({be_acos:.1%})'
                     if has_acos and acos > be_acos * 2.0 else f'Margen negativo ({margin_pct:.1%})')
            return {'type': 'decrease', 'pct': -50,
                    'reason': f'{label} — reducir fuerte.'}
        return {'type': 'decrease', 'pct': -30,
                'reason': f'ACoS sobre 1.5× BE — reducir presupuesto.'}

    # Fallback (no debería ocurrir)
    return {'type': 'maintain', 'pct': 0, 'reason': 'Sin datos suficientes para recomendar.'}


def rank_ads_priority(sku_data: dict) -> float:
    """
    Calcula un score de ranking interno dentro de cada grupo de ads_priority.
    Mayor score = atender primero (en todos los grupos).
    Usa exclusivamente campos ya calculados; no modifica ningún otro campo.

    TOP         — eficiencia de margen + conversión + ROAS + volumen
    MANTENIMIENTO — estabilidad (holgura sobre break-even + margen + conv)
    ACTIVAR     — señales tempranas (CTR + margen posible + gasto mínimo activo)
    QUITAR      — magnitud de la fuga (spend + margen negativo + ACoS vs BE)
    """
    priority = sku_data.get('ads_priority', 'ACTIVAR')
    ad       = sku_data.get('ad_metrics', {})
    prof     = sku_data.get('profitability', {})

    margin_pct  = prof.get('margin_pct', 0.0)
    be_acos     = prof.get('break_even_acos')
    acos        = ad.get('acos')
    roas        = ad.get('roas') or 0.0
    conv_rate   = ad.get('conv_rate', 0.0)
    ctr         = ad.get('ctr', 0.0)
    conversions = sku_data.get('conversions', 0)
    clicks      = sku_data.get('clicks', 0)
    spend       = sku_data.get('spend', 0.0)

    if priority == 'TOP':
        # Cada factor capped para contribución máx ~30 pts → escala total 0-100
        r_margin = margin_pct * 60                             # 0-30 (margin 0-50%)
        r_conv   = min(conv_rate, 0.15) / 0.15 * 30           # 0-30 (cap 15%)
        r_roas   = min(roas, 10.0) / 10.0 * 20                # 0-20 (cap 10×)
        r_vol    = min(conversions, 100) / 100.0 * 20          # 0-20 (cap 100 conv)
        return r_margin + r_conv + r_roas + r_vol

    if priority == 'MANTENIMIENTO':
        holgura = ((be_acos - acos) / be_acos * 20
                   if acos is not None and be_acos and be_acos > 0 else 0.0)
        return margin_pct * 50 + conv_rate * 30 + holgura

    if priority == 'ACTIVAR':
        # CTR normalizado sobre máx 5% → cap 40 pts; clicks cap 200 → máx 20 pts
        r_ctr    = min(ctr, 0.05) / 0.05 * 40                 # 0-40
        r_margin = max(margin_pct, 0.0) * 30                  # 0-15
        r_spend  = 20.0 if spend > 0 else 0.0                 # 0-20
        r_clicks = min(clicks, 200) / 200.0 * 20              # 0-20
        return r_ctr + r_margin + r_spend + r_clicks

    # QUITAR — mayor score = mayor fuga = cortar primero
    # Severidad (margin loss) domina sobre volumen de gasto
    exceso_acos = ((acos / be_acos - 1) * 20
                   if acos is not None and be_acos and be_acos > 0 else 0.0)
    r_severity = abs(min(margin_pct, 0.0)) * 60               # 0-60 (pérdida real)
    r_spend    = min(spend, 500.0) / 500.0 * 30               # 0-30 (capped $500)
    return r_severity + r_spend + exceso_acos


def enrich_with_metrics(skus: dict) -> dict:
    """
    Pipeline completo por SKU:
      1. métricas publicitarias
      2. rentabilidad
      3. objetivo
      4. decisión del motor
      5. acción sugerida
      6. prioridad publicitaria + ranking + budget suggestion
      7. distribución de presupuesto (group_budget / allocated_budget / final_budget)
         — solo si ads_budget.csv existe
    """
    for sku, data in skus.items():
        data['ad_metrics']    = calc_ad_metrics(data)
        data['profitability'] = calc_profitability(data)
        data['objective']     = resolve_objective(data)
        data['engine']        = decide_sku(data)
        data['suggestion']    = suggest_action(data)
        _prio = classify_ads_priority(data)
        data['ads_priority']            = _prio['priority']
        data['ads_priority_reason']     = _prio['reason']
        data['ads_priority_rank_score'] = rank_ads_priority(data)
        data['budget']                  = suggest_budget(data)

        # Acción sugerida consolidada — usa los mismos nombres que AD_ACTIONS
        _action_map = {
            'increase_budget': 'increase_budget',
            'decrease_budget': 'decrease_budget',
            'pause':           'pause',
            'keep':            'keep',
            'fix_listing':     'keep',   # no implica cambio presupuestario
        }
        data['action_suggested'] = _action_map.get(
            data.get('suggestion', {}).get('action', ''), 'keep'
        )
        data['action_status'] = 'NONE'   # sin acción del usuario todavía

    # Distribución de presupuesto — paso final sobre todos los SKUs ya enriquecidos
    total_budget = load_ads_budget()
    if total_budget:
        dyn_split = calc_dynamic_budget_split(skus)
        allocate_budgets(skus, calc_budget_distribution(total_budget, dyn_split))

    return skus


def load_approvals() -> dict[str, dict]:
    """
    Lee data/approvals/approvals.csv y devuelve dict {sku: aprobacion}.

    Columnas esperadas: sku, decision, accion, score, fecha, estado
    Estado posibles: PENDING | APPROVED | REJECTED

    Solo se considera la aprobación más reciente por SKU (última fila).
    """
    path = os.path.join(_APPROVALS_DIR, 'approvals.csv')
    if not os.path.exists(path):
        return {}
    result: dict[str, dict] = {}
    try:
        with open(path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sku = _str(row.get('sku'))
                if not sku:
                    continue
                result[sku] = {
                    'decision': _str(row.get('decision')),
                    'accion':   _str(row.get('accion')),
                    'score':    _int(row.get('score')),
                    'fecha':    _str(row.get('fecha')),
                    'estado':   _str(row.get('estado', 'PENDING')).upper(),
                }
    except Exception as e:
        logging.warning(f'[meli_ads] Error leyendo approvals.csv: {e}')
    return result


def apply_approval_override(sku_data: dict, approvals: dict) -> dict:
    """
    Aplica el estado de aprobación al SKU y bloquea ejecución si no fue aprobado.

    Reglas:
      - PENDING  → accion_status = 'PENDING',  ejecutable = False
      - APPROVED → accion_status = 'APPROVED', ejecutable = True
      - REJECTED → accion_status = 'REJECTED', ejecutable = False
                   y sobreescribe decision → 'MANTENER', accion → 'keep'
      - Sin registro → accion_status = 'PENDING', ejecutable = False
                       (ninguna acción se ejecuta sin aprobación explícita)
    """
    sku      = sku_data.get('sku', '')
    approval = approvals.get(sku)

    if approval is None:
        sku_data['approval'] = {
            'estado':        'PENDING',
            'ejecutable':    False,
            'motivo':        'Sin registro en approvals.csv — requiere aprobación antes de ejecutar.',
        }
        return sku_data

    estado = approval.get('estado', 'PENDING')

    if estado == 'APPROVED':
        sku_data['approval'] = {
            'estado':     'APPROVED',
            'ejecutable': True,
            'fecha':      approval.get('fecha', ''),
            'motivo':     'Acción aprobada manualmente.',
        }

    elif estado == 'REJECTED':
        # Override: bloquear y revertir a posición neutral
        if 'engine' in sku_data:
            sku_data['engine']['decision'] = 'MANTENER'
        if 'suggestion' in sku_data:
            sku_data['suggestion']['action']       = 'keep'
            sku_data['suggestion']['action_label'] = _ACTION_LABELS.get('keep', 'keep')
            sku_data['suggestion']['justificacion'] = 'Acción rechazada — manteniendo posición actual sin cambios.'
        sku_data['approval'] = {
            'estado':     'REJECTED',
            'ejecutable': False,
            'fecha':      approval.get('fecha', ''),
            'motivo':     'Acción rechazada manualmente.',
        }

    else:  # PENDING u otro valor desconocido
        sku_data['approval'] = {
            'estado':     'PENDING',
            'ejecutable': False,
            'fecha':      approval.get('fecha', ''),
            'motivo':     'Pendiente de aprobación.',
        }

    return sku_data


def export_json(skus: dict, path: str) -> None:
    """
    Exporta el dict completo de SKUs a JSON.
    Crea el directorio si no existe.
    """
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(skus, f, indent=2, ensure_ascii=False)


def export_csv(skus: dict, path: str) -> None:
    """
    Exporta un CSV plano con una fila por SKU y las métricas clave.
    Columnas: sku, title, objective, score, decision, action, ejecutable,
              acos, roas, ctr, conv_rate, cpc, margin_pct, break_even_acos,
              spend, revenue_ads, conversions, clicks, impressions,
              stock_quantity, avg_daily_sales, approval_estado, justificacion
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)

    fieldnames = [
        'sku', 'title', 'objective', 'score', 'decision', 'action',
        'ejecutable', 'approval_estado',
        'acos', 'roas', 'ctr', 'conv_rate', 'cpc',
        'margin_pct', 'break_even_acos',
        'price', 'unit_cost', 'margin_net',
        'spend', 'revenue_ads', 'conversions', 'clicks', 'impressions',
        'stock_quantity', 'avg_daily_sales',
        'justificacion',
    ]

    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for sku, d in skus.items():
            ad   = d.get('ad_metrics', {})
            prof = d.get('profitability', {})
            eng  = d.get('engine', {})
            sug  = d.get('suggestion', {})
            appr = d.get('approval', {})

            writer.writerow({
                'sku':              sku,
                'title':            d.get('title', ''),
                'objective':        d.get('objective', ''),
                'score':            eng.get('score', ''),
                'decision':         eng.get('decision', ''),
                'action':           sug.get('action', ''),
                'ejecutable':       appr.get('ejecutable', False),
                'approval_estado':  appr.get('estado', ''),
                'acos':             ad.get('acos', ''),
                'roas':             ad.get('roas', ''),
                'ctr':              ad.get('ctr', ''),
                'conv_rate':        ad.get('conv_rate', ''),
                'cpc':              ad.get('cpc', ''),
                'margin_pct':       prof.get('margin_pct', ''),
                'break_even_acos':  prof.get('break_even_acos', ''),
                'price':            prof.get('price', ''),
                'unit_cost':        prof.get('unit_cost', ''),
                'margin_net':       prof.get('margin_net', ''),
                'spend':            d.get('spend', ''),
                'revenue_ads':      d.get('revenue_ads', ''),
                'conversions':      d.get('conversions', ''),
                'clicks':           d.get('clicks', ''),
                'impressions':      d.get('impressions', ''),
                'stock_quantity':   d.get('stock_quantity', ''),
                'avg_daily_sales':  d.get('avg_daily_sales', ''),
                'justificacion':    sug.get('justificacion', ''),
            })


_ACTIONS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'actions', 'actions.json')


def load_action_states() -> dict:
    """Lee data/actions/actions.json. Devuelve dict {sku: {action, status, fecha}}."""
    if not os.path.exists(_ACTIONS_PATH):
        return {}
    try:
        import json
        with open(_ACTIONS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f'[meli_ads] Error leyendo actions.json: {e}')
        return {}


def save_action_state(sku: str, action: str, status: str,
                      extra: dict | None = None) -> None:
    """Persiste el estado de una acción en data/actions/actions.json."""
    import json
    from datetime import datetime
    os.makedirs(os.path.dirname(_ACTIONS_PATH), exist_ok=True)
    states = load_action_states()
    states[sku] = {
        'action':    action,
        'status':    status,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        **(extra or {}),
    }
    try:
        with open(_ACTIONS_PATH, 'w', encoding='utf-8') as f:
            json.dump(states, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.warning(f'[meli_ads] Error guardando actions.json: {e}')


# Reglas que bloquean ejecución por acción
_BLOCK_INCREASE = {'PAUSAR', 'REDUCIR', 'OPTIMIZAR'}

def execute_action_simulation(sku_data: dict, action: str) -> dict:
    """
    Valida y simula la ejecución de una acción sobre un SKU.
    No conecta con ML API — registra el resultado simulado.

    Retorna:
      ok        — bool
      blocked   — True si una regla bloqueó la ejecución
      motivo    — razón del bloqueo (si aplica)
      simulado  — dict con detalle del cambio simulado
    """
    from datetime import datetime

    engine     = sku_data.get('engine', {})
    prof       = sku_data.get('profitability', {})
    budget     = sku_data.get('budget', {})
    priority   = sku_data.get('ads_priority', '')
    decision   = engine.get('decision', '')
    margin_pct = prof.get('margin_pct', 0.0)
    spend      = sku_data.get('spend', 0.0)
    final_bud  = sku_data.get('final_budget') or spend  # base para el cálculo
    bud_pct    = budget.get('pct') or 0

    # Normalizar acción al simulador interno
    sim_action = _ACTION_SIM.get(action, action)

    # ── Validaciones de bloqueo ───────────────────────────────────────────────
    if action in _ESCALATE_ACTIONS:
        if priority == 'QUITAR':
            return {'ok': False, 'blocked': True,
                    'motivo': 'SKU clasificado como QUITAR — no se puede escalar presupuesto.'}
        if decision in _BLOCK_INCREASE:
            return {'ok': False, 'blocked': True,
                    'motivo': f'Motor indica {decision} — acción de escalada bloqueada.'}
        if margin_pct < 0:
            return {'ok': False, 'blocked': True,
                    'motivo': f'Margen negativo ({margin_pct:.1%}) — no se puede escalar.'}

    if action == 'pause':
        objective  = sku_data.get('objective', '')
        clicks     = sku_data.get('clicks', 0)
        if objective == 'LAUNCH' and clicks < 30:
            return {'ok': False, 'blocked': True,
                    'motivo': f'Solo {clicks} clicks en fase LAUNCH — pausar sin evidencia suficiente bloqueado.'}

    # ── Simulación del cambio ─────────────────────────────────────────────────
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if sim_action == 'increase':
        delta     = round(final_bud * (abs(bud_pct) / 100), 2) if bud_pct else round(final_bud * 0.10, 2)
        nuevo_bud = round(final_bud + delta, 2)
        simulado  = {
            'descripcion':  f'Presupuesto simulado: ${final_bud:,.0f} → ${nuevo_bud:,.0f} (+{delta:,.0f})',
            'budget_antes': final_bud,
            'budget_nuevo': nuevo_bud,
            'pct_cambio':   bud_pct or 10,
        }

    elif sim_action == 'activate':
        # Activar con presupuesto mínimo de prueba (base = 1000 ARS si no hay historial)
        base      = max(final_bud, 1000.0)
        nuevo_bud = round(base * 1.10, 2)
        simulado  = {
            'descripcion':  f'Activar Ads — presupuesto base ${base:,.0f} → ${nuevo_bud:,.0f}',
            'budget_antes': final_bud,
            'budget_nuevo': nuevo_bud,
            'pct_cambio':   10,
        }

    elif sim_action == 'test':
        # Presupuesto mínimo controlado para probar
        base      = max(final_bud, 500.0)
        nuevo_bud = round(base * 1.05, 2)
        simulado  = {
            'descripcion':  f'Test con presupuesto bajo — ${base:,.0f} → ${nuevo_bud:,.0f} (+5%)',
            'budget_antes': final_bud,
            'budget_nuevo': nuevo_bud,
            'pct_cambio':   5,
        }

    elif sim_action == 'decrease':
        delta     = round(final_bud * (abs(bud_pct) / 100), 2) if bud_pct else round(final_bud * 0.30, 2)
        nuevo_bud = round(max(final_bud - delta, 0), 2)
        simulado  = {
            'descripcion':  f'Presupuesto simulado: ${final_bud:,.0f} → ${nuevo_bud:,.0f} (-{delta:,.0f})',
            'budget_antes': final_bud,
            'budget_nuevo': nuevo_bud,
            'pct_cambio':   -(bud_pct or 30),
        }

    elif sim_action == 'pause':
        simulado = {
            'descripcion':  f'Campaña pausada — presupuesto ${final_bud:,.0f} → $0',
            'budget_antes': final_bud,
            'budget_nuevo': 0,
            'pct_cambio':   -100,
        }

    else:  # keep / ignore / review_product
        label_map = {'ignore': 'Ignorado sin cambios.', 'review_product': 'Marcado para revisión de publicación.'}
        simulado = {
            'descripcion':  label_map.get(action, 'Sin cambio de presupuesto.'),
            'budget_antes': final_bud,
            'budget_nuevo': final_bud,
            'pct_cambio':   0,
        }

    simulado['timestamp'] = ts
    simulado['action']    = action
    return {'ok': True, 'blocked': False, 'motivo': '', 'simulado': simulado}


# ── Modelo de estados para ejecución manual futura ────────────────────────────

# Estados posibles de una acción por SKU
ACTION_STATES = {
    'SUGGESTED',   # motor generó la acción, no revisada aún
    'APPROVED',    # usuario aprobó — lista para ejecutar
    'REJECTED',    # usuario rechazó — no ejecutar
    'APPLIED',     # ejecutada exitosamente
    'FAILED',      # ejecución intentada pero falló
    'BLOCKED',     # regla interna impidió la ejecución
}

# Acciones soportadas sobre ads
AD_ACTIONS = {
    'pause':            'Pausar campaña/ad',
    'increase_budget':  'Aumentar presupuesto',
    'decrease_budget':  'Reducir presupuesto',
    'keep':             'Mantener sin cambios',
    'activate':         'Activar Ads',
    'test_low_budget':  'Testear con presupuesto bajo',
    'ignore':           'Ignorar por ahora',
    'review_product':   'Revisar publicación',
}

# Acciones que implican escalar inversión (sujetas a bloqueos de seguridad)
_ESCALATE_ACTIONS = {'increase_budget', 'activate', 'test_low_budget'}

# Mapeo acción → operación de simulación interna
_ACTION_SIM = {
    'increase_budget': 'increase',
    'decrease_budget': 'decrease',
    'pause':           'pause',
    'keep':            'keep',
    'activate':        'activate',
    'test_low_budget': 'test',
    'ignore':          'keep',
    'review_product':  'keep',
}

# Transiciones válidas entre estados (state → set de destinos permitidos)
ACTION_STATE_TRANSITIONS: dict[str, set[str]] = {
    'SUGGESTED': {'APPROVED', 'REJECTED'},
    'APPROVED':  {'APPLIED', 'FAILED', 'BLOCKED'},
    'REJECTED':  set(),          # terminal
    'APPLIED':   set(),          # terminal
    'FAILED':    {'APPROVED'},   # puede re-aprobarse para reintentar
    'BLOCKED':   {'SUGGESTED'},  # puede volver a evaluar
}


def build_action_record(sku: str, action: str, state: str = 'SUGGESTED',
                        extra: dict | None = None) -> dict:
    """
    Construye un registro de acción normalizado para un SKU.
    No persiste ni ejecuta nada.

    Campos:
      sku       — identificador del SKU
      action    — clave de AD_ACTIONS
      state     — estado inicial (default SUGGESTED)
      label     — texto legible de la acción
      extra     — datos adicionales opcionales (advertiser_id, campaign_id, etc.)
    """
    from datetime import datetime
    if action not in AD_ACTIONS:
        logging.warning(f'[meli_ads_actions] Acción desconocida: {action}')
    if state not in ACTION_STATES:
        logging.warning(f'[meli_ads_actions] Estado desconocido: {state}')
    return {
        'sku':       sku,
        'action':    action,
        'label':     AD_ACTIONS.get(action, action),
        'state':     state,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        **(extra or {}),
    }


def transition_action_state(record: dict, new_state: str) -> tuple[bool, str]:
    """
    Aplica una transición de estado a un registro de acción.
    No persiste — devuelve (ok, motivo).

    Modifica record['state'] y record['updated_at'] in-place si la transición es válida.
    """
    from datetime import datetime
    current = record.get('state', 'SUGGESTED')
    allowed = ACTION_STATE_TRANSITIONS.get(current, set())

    if new_state not in ACTION_STATES:
        return False, f'Estado destino desconocido: {new_state}'
    if new_state not in allowed:
        return False, (
            f'Transición inválida: {current} → {new_state}. '
            f'Permitidas: {", ".join(sorted(allowed)) or "ninguna (estado terminal)"}'
        )

    record['state']      = new_state
    record['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return True, ''


def get_missing_files() -> list[str]:
    """Devuelve lista de CSVs faltantes para mostrar advertencia en UI."""
    expected = ['ads_performance.csv', 'costs.csv', 'stock.csv',
                'listings.csv', 'objectives.csv']
    return [f for f in expected if not os.path.exists(_csv_path(f))]


# Porcentajes base de distribución por grupo (deben sumar 1.0)
BUDGET_SPLIT = {
    'TOP':          0.50,   # mejores performers — escalar
    'MANTENIMIENTO': 0.30,  # sostener rendimiento actual
    'ACTIVAR':      0.15,   # prueba controlada
    'QUITAR':       0.05,   # mínimo mientras se reduce
}


def calc_dynamic_budget_split(skus: dict) -> dict:
    """
    Ajusta BUDGET_SPLIT según performance real de cada grupo.

    Reglas (se aplican en orden, cada una toma de otro grupo):
      TOP avg_score >= 80          → TOP +5%, ACTIVAR −5%
      TOP avg_margin >= 0.20       → TOP +5%, QUITAR −3% (mínimo 2%)
      ACTIVAR avg_conv_rate < 0.01 → ACTIVAR −5%, MANTENIMIENTO +5%

    Siempre normaliza para sumar exactamente 1.0.
    Mínimos: TOP ≥ 30%, MANT ≥ 15%, ACTIVAR ≥ 5%, QUITAR ≥ 2%.
    """
    by_group: dict[str, list] = {k: [] for k in BUDGET_SPLIT}
    for data in skus.values():
        p = data.get('ads_priority', 'ACTIVAR')
        if p in by_group:
            by_group[p].append(data)

    split = {k: v for k, v in BUDGET_SPLIT.items()}  # copia mutable

    # ── TOP: performance sólida → merece más presupuesto ─────────────────────
    top = by_group['TOP']
    if top:
        avg_score  = sum(d.get('engine', {}).get('score', 0) for d in top) / len(top)
        avg_margin = sum(d.get('profitability', {}).get('margin_pct', 0) for d in top) / len(top)

        if avg_score >= 80:
            delta = min(0.05, split['ACTIVAR'] - 0.05)
            split['TOP']    = min(split['TOP'] + delta, 0.70)
            split['ACTIVAR'] = max(split['ACTIVAR'] - delta, 0.05)

        if avg_margin >= 0.20:
            delta = min(0.05, split['QUITAR'] - 0.02)
            split['TOP']   = min(split['TOP'] + delta, 0.70)
            split['QUITAR'] = max(split['QUITAR'] - delta, 0.02)

    # ── ACTIVAR: sin conversiones → reducir ──────────────────────────────────
    act = by_group['ACTIVAR']
    if act:
        avg_conv = sum(d.get('ad_metrics', {}).get('conv_rate', 0) for d in act) / len(act)
        if avg_conv < 0.01:
            delta = min(0.05, split['ACTIVAR'] - 0.05)
            split['ACTIVAR']      = max(split['ACTIVAR'] - delta, 0.05)
            split['MANTENIMIENTO'] = split['MANTENIMIENTO'] + delta

    # ── Normalizar → suma exactamente 1.0 ────────────────────────────────────
    total = sum(split.values())
    return {k: round(v / total, 4) for k, v in split.items()}


def calc_budget_distribution(total_budget: float, split: dict | None = None) -> dict:
    """
    Calcula el presupuesto asignado a cada grupo.
    Si split es None usa BUDGET_SPLIT estático; si se pasa uno dinámico lo usa.

    Retorna dict con:
      {grupo: {'pct': float, 'amount': float}}
    """
    effective = split if split is not None else BUDGET_SPLIT
    return {
        group: {
            'pct':    pct,
            'amount': round(total_budget * pct, 2),
        }
        for group, pct in effective.items()
    }


def calc_final_budget(sku_data: dict) -> float:
    """
    Aplica reglas de seguridad sobre allocated_budget y devuelve final_budget.

    Orden de prioridad (de mayor a menor restricción):
      1. QUITAR o decision PAUSAR o margin_pct < 0   → 0
      2. decision REDUCIR                             → 50% del asignado
      3. objective LAUNCH                             → cap 30% del asignado
      4. margin_pct < 0.10                            → 50% del asignado
      5. decision OPTIMIZAR                           → 70% del asignado
      6. Sin restricción                              → allocated_budget
    """
    allocated  = sku_data.get('allocated_budget', 0.0) or 0.0
    priority   = sku_data.get('ads_priority', 'ACTIVAR')
    engine     = sku_data.get('engine', {})
    prof       = sku_data.get('profitability', {})

    decision   = engine.get('decision', '')
    margin_pct = prof.get('margin_pct', 0.0)
    objective  = sku_data.get('objective', '')

    # Regla 1: corte total
    if (priority == 'QUITAR'
            or decision == 'PAUSAR'
            or margin_pct < 0):
        return 0.0

    # Regla 2: reducción fuerte
    if decision == 'REDUCIR':
        return round(allocated * 0.50, 2)

    # Regla 3: lanzamiento — limitar a 30% para no sobre-invertir sin datos
    if objective == 'LAUNCH':
        return round(allocated * 0.30, 2)

    # Regla 4: margen ajustado — recorte preventivo
    if margin_pct < 0.10:
        return round(allocated * 0.50, 2)

    # Regla 5: necesita optimización antes de escalar
    if decision == 'OPTIMIZAR':
        return round(allocated * 0.70, 2)

    return round(allocated, 2)


def allocate_budgets(skus: dict, budget_dist: dict) -> None:
    """
    Distribuye el presupuesto de cada grupo entre sus SKUs
    proporcionalmente al ads_priority_rank_score.
    Modifica skus in-place — agrega 'allocated_budget' a cada SKU.

    sku_budget = group_budget * (rank_score / total_rank_score)
    Si todos los scores del grupo son 0, reparte en partes iguales.
    """
    # Agrupar referencias por priority
    groups: dict[str, list] = {}
    for data in skus.values():
        priority = data.get('ads_priority', 'ACTIVAR')
        groups.setdefault(priority, []).append(data)

    for priority, members in groups.items():
        group_budget = budget_dist.get(priority, {}).get('amount', 0.0)
        total_score  = sum(d.get('ads_priority_rank_score', 0.0) for d in members)

        for data in members:
            score = data.get('ads_priority_rank_score', 0.0)
            data['group_budget'] = group_budget
            if total_score > 0:
                data['allocated_budget'] = round(group_budget * score / total_score, 2)
            else:
                data['allocated_budget'] = round(group_budget / len(members), 2)
            data['final_budget'] = calc_final_budget(data)


# ── Mercado Ads API — conector ───────────────────────────────────────────────────────────────────────────────

_ML_BASE = 'https://api.mercadolibre.com'
_ADS_LOG = '[meli_ads_connector]'


def _ads_headers(token: str) -> dict:
    """Devuelve headers estándar para Mercado Ads API."""
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
    }


def _ads_get(path: str, token: str, params: dict | None = None,
             base: str = _ML_BASE) -> dict:
    """
    GET al endpoint dado. Devuelve dict con:
      ok     — bool
      status — int HTTP status code (0 si error de red)
      data   — dict | list | None
      error  — str | None
    """
    import requests
    url = f'{base}{path}'
    try:
        r = requests.get(url, headers=_ads_headers(token),
                         params=params or {}, timeout=10)
        try:
            body = r.json()
        except Exception:
            body = None
        if not r.ok:
            logging.warning(f'{_ADS_LOG} GET {path} → {r.status_code}')
        return {'ok': r.ok, 'status': r.status_code, 'data': body, 'error': None}
    except requests.RequestException as e:
        logging.warning(f'{_ADS_LOG} Error de red GET {path}: {e}')
        return {'ok': False, 'status': 0, 'data': None, 'error': str(e)}


def _ads_post(path: str, token: str, payload: dict,
              base: str = _ML_BASE) -> dict:
    """POST al endpoint dado. Misma estructura de retorno que _ads_get."""
    import requests
    url = f'{base}{path}'
    try:
        r = requests.post(url, headers=_ads_headers(token),
                          json=payload, timeout=10)
        try:
            body = r.json()
        except Exception:
            body = None
        if not r.ok:
            logging.warning(f'{_ADS_LOG} POST {path} → {r.status_code}')
        return {'ok': r.ok, 'status': r.status_code, 'data': body, 'error': None}
    except requests.RequestException as e:
        logging.warning(f'{_ADS_LOG} Error de red POST {path}: {e}')
        return {'ok': False, 'status': 0, 'data': None, 'error': str(e)}


def _ads_put(path: str, token: str, payload: dict,
             base: str = _ML_BASE) -> dict:
    """PUT al endpoint dado. Misma estructura de retorno que _ads_get."""
    import requests
    url = f'{base}{path}'
    try:
        r = requests.put(url, headers=_ads_headers(token),
                         json=payload, timeout=10)
        try:
            body = r.json()
        except Exception:
            body = None
        if not r.ok:
            logging.warning(f'{_ADS_LOG} PUT {path} → {r.status_code}: {body}')
        return {'ok': r.ok, 'status': r.status_code, 'data': body, 'error': None}
    except requests.RequestException as e:
        logging.warning(f'{_ADS_LOG} Error de red PUT {path}: {e}')
        return {'ok': False, 'status': 0, 'data': None, 'error': str(e)}


# ── Helpers internos (flujo real confirmado) ────────────────────────────────────────────────────────────

def _get_user_id(token: str) -> tuple[int | None, bool, list[str]]:
    """
    Obtiene user_id desde /users/me.
    Retorna (user_id | None, api_reached: bool, warnings: list[str]).
    """
    warnings: list[str] = []
    r = _ads_get('/users/me', token)
    if not r['ok']:
        if r['status'] == 0:
            warnings.append('Error de red — no se pudo contactar la API.')
            return None, False, warnings
        if r['status'] == 401:
            warnings.append('Token inválido o expirado (401).')
        else:
            warnings.append(f'/users/me respondió {r["status"]}.')
        return None, True, warnings

    user_id = (r['data'] or {}).get('id')
    if not user_id:
        warnings.append('No se pudo obtener user_id desde /users/me.')
        return None, True, warnings

    return int(user_id), True, warnings


def _get_advertiser_id_from_items(token: str, user_id: int) -> int | None:
    """
    Escanea ítems activos hasta encontrar el primer advertiser_id real.
    Endpoint: GET /advertising/product_ads/items/{item_id}
    Retorna advertiser_id o None si ningún ítem tiene ads activos.
    """
    import time
    r = _ads_get(f'/users/{user_id}/items/search', token,
                 params={'status': 'active', 'limit': 50})
    if not r['ok']:
        logging.warning(f'{_ADS_LOG} items/search → {r["status"]}')
        return None

    data = r['data'] or {}
    item_ids = data if isinstance(data, list) else data.get('results', [])

    for item_id in item_ids:
        r2 = _ads_get(f'/advertising/product_ads/items/{item_id}', token)
        if r2['ok'] and r2['data']:
            adv_id = (r2['data'] or {}).get('advertiser_id')
            if adv_id:
                return int(adv_id)
        time.sleep(0.05)

    return None


def _discover_campaign_ids(token: str, user_id: int) -> dict[int, list[str]]:
    """
    Escanea ítems activos y devuelve {campaign_id: [item_id, ...]}.
    Endpoint confirmado: GET /advertising/product_ads/items/{item_id}
    Permite saber qué ítems comparten cada campaña.
    """
    import time
    result: dict[int, list[str]] = {}

    r = _ads_get(f'/users/{user_id}/items/search', token,
                 params={'status': 'active', 'limit': 100})
    if not r['ok']:
        logging.warning(f'{_ADS_LOG} _discover_campaign_ids items/search → {r["status"]}')
        return result

    data = r['data'] or {}
    item_ids = data if isinstance(data, list) else data.get('results', [])

    for item_id in item_ids:
        r2 = _ads_get(f'/advertising/product_ads/items/{item_id}', token)
        if r2['ok'] and r2['data']:
            camp_id = (r2['data'] or {}).get('campaign_id')
            if camp_id:
                result.setdefault(int(camp_id), []).append(str(item_id))
        time.sleep(0.05)

    return result


def _get_campaign_detail(token: str, campaign_id: int) -> dict:
    """
    Obtiene detalle de una campaña.
    Endpoint confirmado: GET /advertising/product_ads/campaigns/{campaign_id}
    """
    r = _ads_get(f'/advertising/product_ads/campaigns/{campaign_id}', token)
    if not r['ok']:
        logging.warning(f'{_ADS_LOG} campaign detail {campaign_id} → {r["status"]}')
        return {}
    data = r['data'] or {}
    return {
        'id':            data.get('id'),
        'name':          _str(data.get('name')),
        'status':        _str(data.get('status')),
        'budget':        _float(data.get('budget'), default=0.0),
        'strategy':      _str(data.get('strategy')),
        'acos_target':   _float(data.get('acos_target'), default=0.0),
        'advertiser_id': data.get('advertiser_id'),
    }


# ── Funciones públicas del conector ─────────────────────────────────────────────────────────────────────────────

def get_advertiser_id(token: str) -> int | None:
    """
    Devuelve el advertiser_id asociado al token escaneando ítems activos.
    Retorna None si no hay ítems con Product Ads o sin acceso.
    """
    user_id, _, warnings = _get_user_id(token)
    for w in warnings:
        logging.warning(f'{_ADS_LOG} {w}')
    if not user_id:
        return None
    return _get_advertiser_id_from_items(token, user_id)


def get_campaigns(token: str, advertiser_id: int | None = None) -> dict:
    """
    Lee campañas reales usando el flujo confirmado: ítem → campaign_id → detalle.

    Retorna:
      ok               — bool
      campaigns_count  — int
      campaigns_summary — list[dict] con campos normalizados
      error            — str | None
    """
    user_id, _, warnings = _get_user_id(token)
    if not user_id:
        return {
            'ok': False, 'campaigns_count': 0, 'campaigns_summary': [],
            'error': warnings[0] if warnings else 'Sin user_id.',
        }

    campaign_map = _discover_campaign_ids(token, user_id)
    if not campaign_map:
        return {'ok': True, 'campaigns_count': 0, 'campaigns_summary': [], 'error': None}

    summary = []
    for camp_id in campaign_map:
        detail = _get_campaign_detail(token, camp_id)
        if detail:
            summary.append(detail)

    return {
        'ok':                True,
        'campaigns_count':   len(summary),
        'campaigns_summary': summary,
        'error':             None,
    }


def get_ads(token: str, advertiser_id: int | None = None) -> dict:
    """
    Devuelve todos los ítems con Product Ads activos y su campaign_id.

    Retorna:
      ok          — bool
      ads_count   — int
      ads_summary — list[dict] con item_id y campaign_id
      error       — str | None
    """
    user_id, _, warnings = _get_user_id(token)
    if not user_id:
        return {
            'ok': False, 'ads_count': 0, 'ads_summary': [],
            'error': warnings[0] if warnings else 'Sin user_id.',
        }

    campaign_map = _discover_campaign_ids(token, user_id)

    ads_summary = []
    for camp_id, item_ids in campaign_map.items():
        for item_id in item_ids:
            ads_summary.append({'item_id': item_id, 'campaign_id': camp_id})

    return {'ok': True, 'ads_count': len(ads_summary), 'ads_summary': ads_summary, 'error': None}


def get_metrics(token: str, advertiser_id: int,
                date_from: str, date_to: str,
                campaign_ids: list[int] | None = None) -> dict:
    """
    Lee métricas por campaign_id usando el endpoint confirmado.
    Endpoint: GET /advertising/product_ads/campaigns/{campaign_id}/metrics

    Campos reales → internos:
      cost               → spend
      amount_total       → revenue_ads
      sold_quantity_total → conversions
      tacos              → tacos (campo propio)

    Retorna:
      ok                — bool
      metrics_available — bool
      metrics_summary   — list[dict] por campaign_id
      aggregated        — dict con totales
      warnings          — list[str]
      error             — str | None
    """
    import time

    warnings_list: list[str] = []

    if not campaign_ids:
        user_id, _, w = _get_user_id(token)
        warnings_list.extend(w)
        if not user_id:
            return {
                'ok': False, 'metrics_available': False,
                'metrics_summary': [], 'aggregated': {},
                'warnings': warnings_list, 'error': 'Sin user_id.',
            }
        campaign_map = _discover_campaign_ids(token, user_id)
        campaign_ids = list(campaign_map.keys())

    if not campaign_ids:
        return {
            'ok': False, 'metrics_available': False,
            'metrics_summary': [], 'aggregated': {},
            'warnings': ['Sin campañas disponibles.'], 'error': 'Sin campañas.',
        }

    metrics_rows: list[dict] = []

    for camp_id in campaign_ids:
        r = _ads_get(
            f'/advertising/product_ads/campaigns/{camp_id}/metrics',
            token,
            params={'date_from': date_from, 'date_to': date_to},
        )

        if not r['ok']:
            warnings_list.append(
                f'Métricas no disponibles para campaña {camp_id} (HTTP {r["status"]}).')
            logging.warning(f'{_ADS_LOG} get_metrics campaña {camp_id} → {r["status"]}')
            time.sleep(0.1)
            continue

        body = r['data'] or {}

        clicks      = _int(body.get('clicks', 0))
        impressions = _int(body.get('impressions', 0))
        spend       = _float(body.get('cost', 0.0))
        revenue_ads = _float(body.get('amount_total', 0.0))
        conversions = _int(body.get('sold_quantity_total', 0))
        ctr         = _float(body.get('ctr', 0.0))
        cpc         = _float(body.get('cpc', 0.0))
        tacos       = _float(body.get('tacos', 0.0))

        if not ctr and impressions > 0:
            ctr = round(clicks / impressions, 4)
        if not cpc and clicks > 0:
            cpc = round(spend / clicks, 2)

        acos = round(spend / revenue_ads, 4) if revenue_ads > 0 else None
        roas = round(revenue_ads / spend, 2) if spend > 0 else None

        metrics_rows.append({
            'campaign_id': camp_id,
            'impressions': impressions,
            'clicks':      clicks,
            'spend':       spend,
            'conversions': conversions,
            'revenue_ads': revenue_ads,
            'ctr':         ctr,
            'cpc':         cpc,
            'tacos':       tacos,
            'acos':        acos,
            'roas':        roas,
        })
        time.sleep(0.1)

    if not metrics_rows:
        return {
            'ok': False, 'metrics_available': False,
            'metrics_summary': [], 'aggregated': {},
            'warnings': warnings_list or ['Sin métricas para las campañas consultadas.'],
            'error': 'Sin datos de métricas.',
        }

    agg: dict = {f: 0 for f in ('impressions', 'clicks', 'spend', 'conversions', 'revenue_ads')}
    for row in metrics_rows:
        for f in agg:
            agg[f] += row.get(f, 0)

    agg['ctr']  = round(agg['clicks'] / agg['impressions'], 4) if agg['impressions'] > 0 else 0.0
    agg['cpc']  = round(agg['spend']  / agg['clicks'], 2)      if agg['clicks']      > 0 else 0.0
    agg['acos'] = round(agg['spend']  / agg['revenue_ads'], 4) if agg['revenue_ads'] > 0 else None
    agg['roas'] = round(agg['revenue_ads'] / agg['spend'], 2)  if agg['spend']       > 0 else None

    has_data = any(agg.get(f, 0) for f in ('impressions', 'clicks', 'spend'))

    return {
        'ok':                True,
        'metrics_available': has_data,
        'metrics_summary':   metrics_rows,
        'aggregated':        agg,
        'warnings':          warnings_list,
        'error':             None,
    }


# ── Normalización API → modelo interno ────────────────────────────────────────────────────────────────

# Campos de performance que el motor consume directamente
_PERF_FIELDS = ('impressions', 'clicks', 'spend', 'conversions', 'revenue_ads')


def normalize_api_metrics(aggregated: dict) -> dict:
    """
    Mapea el dict 'aggregated' de get_metrics() a los campos internos del motor.
    Solo campos de performance — no toca precio, costo ni stock.

    Mapeos reales confirmados:
      cost               → spend          (ya normalizado en get_metrics)
      amount_total       → revenue_ads    (ya normalizado en get_metrics)
      sold_quantity_total → conversions   (ya normalizado en get_metrics)
      tacos              → tacos          (campo propio)
    """
    return {
        'impressions': _int(aggregated.get('impressions', 0)),
        'clicks':      _int(aggregated.get('clicks', 0)),
        'spend':       _float(aggregated.get('spend', 0.0)),
        'conversions': _int(aggregated.get('conversions', 0)),
        'revenue_ads': _float(aggregated.get('revenue_ads', 0.0)),
        'tacos':       _float(aggregated.get('tacos', 0.0)),
    }


def normalize_api_ads(ads_summary: list[dict]) -> dict[str, dict]:
    """
    Indexa ads_summary de get_ads() por item_id para correlación con SKUs.
    Devuelve {item_id: {campaign_id}}.
    """
    result: dict[str, dict] = {}
    for ad in ads_summary:
        item_id = _str(ad.get('item_id'))
        if not item_id:
            continue
        result[item_id] = {'campaign_id': ad.get('campaign_id')}
    return result


def merge_sku_sources(csv_sku: dict,
                      api_perf: dict | None = None,
                      api_ad_info: dict | None = None) -> dict:
    """
    Combina datos CSV con datos de API para un SKU y establece source_mode.

    source_mode:
      'CSV'            — solo datos CSV (api_perf ausente o vacío)
      'API'            — métricas de campaña con ítem único (item_via_campaign)
      'MIXED'          — API + CSV con performance propia
      'CAMPAIGN_LEVEL' — métricas de campaña compartida entre varios ítems

    metrics_scope:
      'item_via_campaign' — ítem único en su campaña
      'campaign'          — varios ítems comparten la campaña
    """
    sku = {**csv_sku}

    csv_has_perf = any(sku.get(f, 0) for f in _PERF_FIELDS)
    api_has_perf = bool(api_perf and any(api_perf.get(f, 0) for f in _PERF_FIELDS))

    if api_has_perf:
        sku.update({k: v for k, v in api_perf.items()
                    if k in _PERF_FIELDS or k == 'tacos'})
        sku['source_mode'] = 'MIXED' if csv_has_perf else 'API'
    else:
        sku['source_mode'] = 'CSV'
        if not csv_has_perf:
            logging.warning(f'{_ADS_LOG} SKU {sku.get("sku")} sin datos de performance.')

    if api_ad_info:
        items_in_camp = api_ad_info.get('campaign_items_count', 1)
        sku['campaign_id']         = api_ad_info.get('campaign_id')
        sku['campaign_items_count'] = items_in_camp
        if items_in_camp > 1:
            sku['metrics_scope'] = 'campaign'
            sku['source_mode']   = 'CAMPAIGN_LEVEL'
        else:
            sku['metrics_scope'] = 'item_via_campaign'

    return sku


def resolve_source_mode(csv_sku: dict,
                        api_perf: dict | None,
                        api_connected: bool,
                        advertiser_id: int | None) -> tuple[str, list[str]]:
    """
    Decide source_mode para un SKU dado el estado de ambas fuentes.
    No modifica datos — solo clasifica y genera warnings.
    """
    warnings: list[str] = []
    sku_id = csv_sku.get('sku', '?')

    if not api_connected or not advertiser_id:
        return 'CSV', []

    api_perf = api_perf or {}
    _KEY_FIELDS  = ('impressions', 'clicks', 'spend')
    _FULL_FIELDS = ('impressions', 'clicks', 'spend', 'conversions', 'revenue_ads')

    api_key_present = all(api_perf.get(f, 0) > 0 for f in _KEY_FIELDS)
    api_has_any     = any(api_perf.get(f, 0) > 0 for f in _FULL_FIELDS)

    if not api_has_any:
        warnings.append(f'SKU {sku_id}: API conectada pero sin métricas — usando CSV.')
        return 'CSV', warnings

    csv_has_perf = any(csv_sku.get(f, 0) > 0 for f in _FULL_FIELDS)
    missing_api  = [f for f in _FULL_FIELDS if not api_perf.get(f, 0) > 0]

    if api_key_present and not csv_has_perf:
        mode = 'API'
        if missing_api:
            warnings.append(f'SKU {sku_id} [API]: campos en 0 — {", ".join(missing_api)}.')
        return mode, warnings

    if api_key_present and csv_has_perf:
        mode = 'MIXED'
        if missing_api:
            warnings.append(f'SKU {sku_id} [MIXED]: API incompleta en {", ".join(missing_api)}.')
        return mode, warnings

    warnings.append(f'SKU {sku_id}: API parcial ({", ".join(missing_api)} ausentes) — MIXED.')
    return 'MIXED', warnings


def build_skus_from_api(token: str, date_from: str, date_to: str,
                        advertiser_id: int | None = None) -> tuple[dict, dict]:
    """
    Construye el dict de SKUs mezclando CSV base con datos reales de ML Ads API.

    Flujo real confirmado:
      1. user_id desde /users/me
      2. Escanear ítems activos → {campaign_id: [item_ids]}
      3. Por cada campaign_id: detalle + métricas
      4. Asignar métricas a todos los ítems de la campaña con source_mode honesto:
           campaign_items_count > 1 → metrics_scope='campaign', source_mode='CAMPAIGN_LEVEL'
           campaign_items_count == 1 → metrics_scope='item_via_campaign', source_mode='API'

    meta keys: advertiser_id, campaigns_count, ads_count, metrics_per_sku, warnings
    """
    import time

    skus: dict = load_skus()
    meta: dict = {
        'advertiser_id':   None,
        'campaigns_count': 0,
        'ads_count':       0,
        'metrics_per_sku': False,
        'warnings':        [],
    }

    # 1. Obtener user_id
    user_id, api_reached, warnings = _get_user_id(token)
    meta['warnings'].extend(warnings)
    if not user_id:
        for data in skus.values():
            data['source_mode'] = 'CSV'
        if not api_reached:
            meta['warnings'].append('Sin conexión con la API — datos desde CSV.')
        return skus, meta

    # 2. Descubrir campañas escaneando ítems activos
    campaign_map = _discover_campaign_ids(token, user_id)
    if not campaign_map:
        meta['warnings'].append('No se encontraron campañas con Product Ads activos.')
        for data in skus.values():
            data['source_mode'] = 'CSV'
        return skus, meta

    meta['campaigns_count'] = len(campaign_map)
    meta['ads_count']       = sum(len(ids) for ids in campaign_map.values())

    # Mapa inverso: item_id → campaign_id
    item_to_camp: dict[str, int] = {
        item_id: camp_id
        for camp_id, item_ids in campaign_map.items()
        for item_id in item_ids
    }

    # 3. Obtener detalle y métricas de cada campaña
    campaign_metrics: dict[int, dict] = {}
    campaign_details: dict[int, dict] = {}

    for camp_id in campaign_map:
        detail = _get_campaign_detail(token, camp_id)
        campaign_details[camp_id] = detail

        if not meta['advertiser_id'] and detail.get('advertiser_id'):
            meta['advertiser_id'] = detail['advertiser_id']

        r = _ads_get(
            f'/advertising/product_ads/campaigns/{camp_id}/metrics',
            token,
            params={'date_from': date_from, 'date_to': date_to},
        )
        if r['ok'] and r['data']:
            body        = r['data'] or {}
            clicks      = _int(body.get('clicks', 0))
            impressions = _int(body.get('impressions', 0))
            spend       = _float(body.get('cost', 0.0))
            revenue_ads = _float(body.get('amount_total', 0.0))
            conversions = _int(body.get('sold_quantity_total', 0))
            ctr         = _float(body.get('ctr', 0.0))
            cpc         = _float(body.get('cpc', 0.0))
            tacos       = _float(body.get('tacos', 0.0))

            if not ctr and impressions > 0:
                ctr = round(clicks / impressions, 4)
            if not cpc and clicks > 0:
                cpc = round(spend / clicks, 2)

            campaign_metrics[camp_id] = {
                'impressions': impressions,
                'clicks':      clicks,
                'spend':       spend,
                'conversions': conversions,
                'revenue_ads': revenue_ads,
                'ctr':         ctr,
                'cpc':         cpc,
                'tacos':       tacos,
            }
        else:
            meta['warnings'].append(
                f'Métricas no disponibles para campaña {camp_id} (HTTP {r["status"]}).')
            logging.warning(f'{_ADS_LOG} métricas campaña {camp_id} → {r["status"]}')

        time.sleep(0.1)

    # Advertir sobre métricas compartidas (una vez por campaña)
    for camp_id, item_ids in campaign_map.items():
        if len(item_ids) > 1 and camp_id in campaign_metrics:
            preview = ', '.join(item_ids[:3]) + ('...' if len(item_ids) > 3 else '')
            meta['warnings'].append(
                f'Campaña {camp_id}: métricas compartidas entre {len(item_ids)} ítems '
                f'({preview}). Se adjuntan sin prorratear.')

    has_metrics = bool(campaign_metrics)

    # 3b. Para ítems con ads que no están en CSV, traer datos básicos desde ML API
    missing_from_csv = set(item_to_camp.keys()) - set(skus.keys())
    for item_id in missing_from_csv:
        r = _ads_get(f'/items/{item_id}', token)
        if r['ok'] and r['data']:
            d = r['data'] or {}
            listing_type = _str(d.get('listing_type_id', ''))
            skus[item_id] = {
                'sku':             item_id,
                'title':           _str(d.get('title', '')),
                'price':           _float(d.get('price', 0.0)),
                'category':        _str(d.get('category_id', '')),
                'listing_type':    listing_type,
                'free_shipping':   bool((d.get('shipping') or {}).get('free_shipping', False)),
                'unit_cost':       0.0,
                'stock_quantity':  _int(d.get('available_quantity', 0)),
                'avg_daily_sales': 0.0,
                'impressions':     0,
                'clicks':          0,
                'spend':           0.0,
                'conversions':     0,
                'revenue_ads':     0.0,
                'objective':       '',
            }
        time.sleep(0.05)

    # 4. Merge por SKU
    for sku, data in skus.items():
        camp_id = item_to_camp.get(sku)

        if camp_id and camp_id in campaign_metrics:
            metrics       = campaign_metrics[camp_id]
            items_in_camp = len(campaign_map.get(camp_id, []))
            detail        = campaign_details.get(camp_id, {})

            data.update(metrics)
            data['campaign_id']          = camp_id
            data['campaign_items_count'] = items_in_camp
            data['campaign_name']        = detail.get('name', '')
            data['campaign_status']      = detail.get('status', '')
            data['campaign_budget']      = detail.get('budget', 0.0)
            data['campaign_strategy']    = detail.get('strategy', '')
            data['campaign_acos_target'] = detail.get('acos_target', 0.0)

            if items_in_camp > 1:
                data['metrics_scope'] = 'campaign'
                data['source_mode']   = 'CAMPAIGN_LEVEL'
            else:
                data['metrics_scope'] = 'item_via_campaign'
                data['source_mode']   = 'API'
        else:
            data['source_mode'] = 'CSV'

    meta['metrics_per_sku'] = has_metrics

    return skus, meta


def check_product_ads_access(token: str) -> dict:
    """
    Verifica si la cuenta tiene acceso a Product Ads y obtiene el advertiser_id.
    Flujo confirmado: /users/me → escanear ítems → advertiser_id.

    Retorna:
      api_connected   — bool
      advertiser_id   — int | None
      account_status  — 'active' | 'unknown'
      warnings        — list[str]
    """
    result: dict = {
        'api_connected':  False,
        'advertiser_id':  None,
        'account_status': 'unknown',
        'warnings':       [],
    }

    user_id, api_reached, warnings = _get_user_id(token)
    result['api_connected'] = api_reached
    result['warnings']      = warnings

    if not user_id:
        return result

    adv_id = _get_advertiser_id_from_items(token, user_id)
    if not adv_id:
        result['warnings'].append(
            'No se encontró ningún ítem con Product Ads activo en esta cuenta.'
        )
        return result

    result['advertiser_id']  = adv_id
    result['account_status'] = 'active'

    return result


def load_ads_budget() -> float | None:
    """
    Lee el presupuesto total de ads desde data/raw/ads_budget.csv.
    Columna esperada: total_budget

    Retorna el valor como float, o None si el archivo no existe o está vacío.
    Crear el CSV con una fila: total_budget\\n50000
    """
    rows = _read_csv('ads_budget.csv')
    for row in rows:
        val = _float(row.get('total_budget'), default=-1.0)
        if val >= 0:
            return val
    return None


# ── Vista de campañas (nivel campaña, no nivel SKU) ───────────────────────────

def _calc_stock_alerts(sales_data: dict, date_from: str, date_to: str) -> dict:
    """
    Detecta productos con stock bajo en relación a su ritmo de ventas.
    Umbral: stock cubre menos de 7 días al ritmo actual.
    Retorna {'count': int, 'items': [{id, title, stock, days_left, sold_daily}]}
    """
    from datetime import datetime as _dt
    try:
        d0 = _dt.strptime(date_from, '%Y-%m-%d')
        d1 = _dt.strptime(date_to,   '%Y-%m-%d')
        period_days = max((d1 - d0).days, 1)
    except Exception:
        period_days = 30

    alerts = []
    for item_id, sd in sales_data.items():
        stock      = _int(sd.get('available_quantity', 0))
        sold_total = _int(sd.get('sold_quantity', 0))
        sold_daily = round(sold_total / period_days, 2)
        if sold_daily > 0:
            days_left = round(stock / sold_daily, 1)
        else:
            days_left = 999  # sin ventas → stock no es urgente

        if days_left < 7:
            alerts.append({
                'id':         item_id,
                'title':      _str(sd.get('title', item_id)),
                'stock':      stock,
                'days_left':  days_left,
                'sold_daily': sold_daily,
            })

    alerts.sort(key=lambda x: x['days_left'])
    return {'count': len(alerts), 'alerts_list': alerts}


def _get_today_spend(token: str, camp_id: int) -> float:
    """
    Devuelve el gasto de hoy para una campaña.
    Usa date_from=today&date_to=today en la API de métricas.
    """
    from datetime import date as _date
    today = _date.today().strftime('%Y-%m-%d')
    r = _ads_get(
        f'/advertising/product_ads/campaigns/{camp_id}/metrics',
        token,
        params={'date_from': today, 'date_to': today},
    )
    if r['ok'] and r['data']:
        return _float(r['data'].get('cost', 0.0))
    return 0.0


def build_campaigns_from_api(token: str, date_from: str, date_to: str) -> tuple[list, dict]:
    """
    Construye lista de campañas con métricas reales para la vista de campañas.
    Retorna ([campaign_dict, ...], meta_dict).

    Cada campaign_dict tiene:
      id, name, status, budget, strategy, acos_target,
      items_count, item_ids, items (lista [{id, title, price}]),
      metrics {impressions, clicks, spend, revenue_ads, conversions, ctr, cpc, acos, roas},
      today_spend, today_pct,
      stock_alerts {count, items [{id, title, stock, days_left, sold_daily}]}
    """
    import time

    meta: dict = {
        'advertiser_id':   None,
        'campaigns_count': 0,
        'ads_count':       0,
        'warnings':        [],
    }

    user_id, api_reached, warnings = _get_user_id(token)
    meta['warnings'].extend(warnings)
    if not user_id:
        if not api_reached:
            meta['warnings'].append('Sin conexión con la API.')
        return [], meta

    # ── Test rápido de permisos de Publicidad ────────────────────────────────
    # Antes de escanear todos los ítems verificamos si el token tiene acceso
    # al endpoint de advertising. Si devuelve 401 mostramos un mensaje claro.
    _perm_test = _ads_get('/advertising/product_ads/campaigns', token)
    if _perm_test['status'] == 401:
        meta['auth_error'] = True
        meta['warnings'].append(
            'El token no tiene permisos de Publicidad (HTTP 401). '
            'Reconectá tu cuenta desde el botón "Reconectar / Activar publicidad" '
            'para habilitar el acceso a métricas de campañas.'
        )
        return [], meta

    # ── Intento directo: listar campañas sin escanear ítem por ítem ──────────
    campaign_map: dict[int, list[str]] = {}
    _direct_ok = False
    if _perm_test['ok'] and _perm_test['data']:
        _direct_data = _perm_test['data']
        _direct_camps = _direct_data if isinstance(_direct_data, list) else _direct_data.get('results', [])
        for _c in _direct_camps:
            _cid = _c.get('id')
            if _cid:
                campaign_map[int(_cid)] = []
        if campaign_map:
            _direct_ok = True
            # Obtener ítems por campaña
            import time as _t2
            for _camp_id in list(campaign_map.keys()):
                _ri = _ads_get('/advertising/product_ads/items', token,
                                params={'campaign_id': _camp_id, 'limit': 100})
                if _ri['ok'] and _ri['data']:
                    _items_raw = _ri['data']
                    _items_list = _items_raw if isinstance(_items_raw, list) else _items_raw.get('results', [])
                    campaign_map[_camp_id] = [
                        str(i.get('item_id') or i.get('id', ''))
                        for i in _items_list
                        if i.get('item_id') or i.get('id')
                    ]
                _t2.sleep(0.05)

    # ── Fallback: escanear ítems activos ─────────────────────────────────────
    if not _direct_ok:
        campaign_map = _discover_campaign_ids(token, user_id)

    if not campaign_map:
        meta['warnings'].append('No se encontraron campañas con Product Ads activos.')
        return [], meta

    meta['campaigns_count'] = len(campaign_map)
    meta['ads_count']       = sum(len(ids) for ids in campaign_map.values())

    campaigns: list[dict] = []

    for camp_id, item_ids in campaign_map.items():
        detail = _get_campaign_detail(token, camp_id)

        if not meta['advertiser_id'] and detail.get('advertiser_id'):
            meta['advertiser_id'] = detail['advertiser_id']

        # Métricas del período
        r = _ads_get(
            f'/advertising/product_ads/campaigns/{camp_id}/metrics',
            token,
            params={'date_from': date_from, 'date_to': date_to},
        )

        metrics: dict = {}
        if r['ok'] and r['data']:
            body        = r['data'] or {}
            clicks      = _int(body.get('clicks', 0))
            impressions = _int(body.get('impressions', 0))
            spend       = _float(body.get('cost', 0.0))
            revenue_ads = _float(body.get('amount_total', 0.0))
            conversions = _int(body.get('sold_quantity_total', 0))
            ctr         = _float(body.get('ctr', 0.0))
            cpc         = _float(body.get('cpc', 0.0))

            if not ctr and impressions > 0:
                ctr = round(clicks / impressions, 4)
            if not cpc and clicks > 0:
                cpc = round(spend / clicks, 2)

            acos = round(spend / revenue_ads, 4) if revenue_ads > 0 else None
            roas = round(revenue_ads / spend, 2)  if spend > 0       else None

            metrics = {
                'impressions': impressions,
                'clicks':      clicks,
                'spend':       spend,
                'revenue_ads': revenue_ads,
                'conversions': conversions,
                'ctr':         ctr,
                'cpc':         cpc,
                'acos':        acos,
                'roas':        roas,
            }
        else:
            meta['warnings'].append(f'Métricas no disponibles para campaña {camp_id}.')

        # Gasto de hoy vs presupuesto diario
        daily_budget = _float(detail.get('budget', 0.0))
        today_spend  = _get_today_spend(token, camp_id)
        today_pct    = round(today_spend / daily_budget * 100, 1) if daily_budget > 0 else 0.0
        time.sleep(0.05)

        # Stock alerts: productos con stock bajo en relación a su ritmo de ventas
        sales_data   = _batch_fetch_items_sales(token, list(item_ids))
        stock_alerts = _calc_stock_alerts(sales_data, date_from, date_to)

        # Detalle de items (primeros 8 para mostrar)
        items: list[dict] = []
        for item_id in item_ids[:8]:
            r_item = _ads_get(f'/items/{item_id}', token)
            if r_item['ok'] and r_item['data']:
                d = r_item['data'] or {}
                items.append({
                    'id':    item_id,
                    'title': _str(d.get('title', '')),
                    'price': _float(d.get('price', 0.0)),
                })
            time.sleep(0.05)

        campaigns.append({
            'id':          camp_id,
            'name':        detail.get('name', f'Campaña {camp_id}'),
            'status':      detail.get('status', ''),
            'budget':      daily_budget,
            'strategy':    detail.get('strategy', ''),
            'acos_target': detail.get('acos_target', 0.0),
            'items_count': len(item_ids),
            'item_ids':    list(item_ids),
            'products':    items,
            'metrics':     metrics,
            'today_spend': today_spend,
            'today_pct':   today_pct,
            'stock_alerts': stock_alerts,
        })

        time.sleep(0.1)

    return campaigns, meta


def update_campaign_budget(token: str, campaign_id: int, new_budget: float) -> dict:
    """
    Actualiza el presupuesto diario de una campaña.
    Retorna {'ok': bool, 'status': int, 'message': str}
    """
    r = _ads_put(
        f'/advertising/product_ads/campaigns/{campaign_id}',
        token,
        {'daily_budget': new_budget},
    )
    if r['ok']:
        return {'ok': True, 'status': r['status'],
                'message': f'Presupuesto actualizado a ${new_budget:,.0f}/día.'}

    body      = r.get('data') or {}
    error_msg = body.get('message') or body.get('error') or str(body)

    if r['status'] == 401:
        return {'ok': False, 'status': 401,
                'message': 'Sin permiso para escribir en la API de Advertising. '
                           'Reconectá tu cuenta con permisos de escritura ampliados.'}
    return {'ok': False, 'status': r['status'], 'message': f'Error {r["status"]}: {error_msg}'}


def update_campaign_status(token: str, campaign_id: int, status: str) -> dict:
    """
    Activa o pausa una campaña.  status: 'active' | 'paused'
    Retorna {'ok': bool, 'status': int, 'message': str}
    """
    r = _ads_put(
        f'/advertising/product_ads/campaigns/{campaign_id}',
        token,
        {'status': status},
    )
    if r['ok']:
        label = 'activada' if status == 'active' else 'pausada'
        return {'ok': True, 'status': r['status'], 'message': f'Campaña {label} exitosamente.'}

    body      = r.get('data') or {}
    error_msg = body.get('message') or body.get('error') or str(body)

    if r['status'] == 401:
        return {'ok': False, 'status': 401,
                'message': 'Sin permiso para escribir en la API de Advertising. '
                           'Reconectá tu cuenta con permisos de escritura ampliados.'}
    return {'ok': False, 'status': r['status'], 'message': f'Error {r["status"]}: {error_msg}'}


# ── Operaciones por ítem (con manejo de 401 explícito) ────────────────────────

_WRITE_401_MSG = (
    'Sin permiso de escritura en la API de Advertising. '
    'Reconectá la cuenta con permisos ampliados de publicidad.'
)


def get_campaign_items_detail(token: str, item_ids: list[str]) -> list[dict]:
    """
    Obtiene señales de calidad de cada ítem desde /advertising/product_ads/items/{id}.
    No hay métricas por ítem disponibles en la API — devuelve señales de salud.

    Campos devueltos por ítem:
      id, title, price, status, current_level, buy_box_winner,
      image_quality, listing_type_id, has_discount, permalink, thumbnail,
      suggestion  (texto con problemas detectados)
    """
    import time
    results: list[dict] = []

    for item_id in item_ids:
        r = _ads_get(f'/advertising/product_ads/items/{item_id}', token)
        if not r['ok'] or not r['data']:
            results.append({'id': item_id, 'error': True})
            time.sleep(0.05)
            continue

        d = r['data'] or {}
        level        = _str(d.get('current_level', '')).lower()
        buy_box      = bool(d.get('buy_box_winner', False))
        img_quality  = _str(d.get('image_quality', ''))
        status       = _str(d.get('status', ''))

        issues: list[str] = []
        if level == 'red':
            issues.append('Salud del anuncio en ROJO — visibilidad muy limitada.')
        elif level == 'yellow':
            issues.append('Salud en AMARILLO — mejorar ficha aumenta impresiones.')
        if not buy_box:
            issues.append('Sin Buy Box — hay competidor más barato o con mejor reputación.')
        if 'bad' in img_quality or img_quality == 'low_quality_thumbnail':
            issues.append('Calidad de imagen baja — mejorar fotos sube el CTR.')

        suggestion = ' '.join(issues) if issues else 'Sin problemas detectados.'

        results.append({
            'id':           item_id,
            'title':        _str(d.get('title', '')),
            'price':        _float(d.get('price', 0.0)),
            'status':       status,
            'level':        level,
            'buy_box':      buy_box,
            'img_quality':  img_quality,
            'listing_type': _str(d.get('listing_type_id', '')),
            'has_discount': bool(d.get('has_discount', False)),
            'permalink':    _str(d.get('permalink', '')),
            'thumbnail':    _str(d.get('thumbnail', '')),
            'suggestion':   suggestion,
            'error':        False,
        })
        time.sleep(0.08)

    return results


def move_item_to_campaign(token: str, item_id: str, new_campaign_id: int) -> dict:
    """
    Mueve un ítem a otra campaña vía PUT /advertising/product_ads/items/{id}.
    Retorna {'ok': bool, 'status': int, 'message': str, 'needs_reauth': bool}
    """
    r = _ads_put(
        f'/advertising/product_ads/items/{item_id}',
        token,
        {'campaign_id': new_campaign_id},
    )
    if r['ok']:
        return {'ok': True, 'status': r['status'],
                'message': f'Ítem movido a campaña {new_campaign_id} exitosamente.',
                'needs_reauth': False}

    body      = r.get('data') or {}
    error_msg = body.get('message') or body.get('error') or str(body)

    if r['status'] == 401:
        return {'ok': False, 'status': 401,
                'message': _WRITE_401_MSG, 'needs_reauth': True}
    return {'ok': False, 'status': r['status'],
            'message': f'Error {r["status"]}: {error_msg}', 'needs_reauth': False}


def remove_item_from_campaign(token: str, item_id: str) -> dict:
    """
    Elimina un ítem de su campaña de Product Ads vía DELETE.
    Retorna {'ok': bool, 'status': int, 'message': str, 'needs_reauth': bool}
    """
    import requests as req
    url = f'{_ML_BASE}/advertising/product_ads/items/{item_id}'
    try:
        r = req.delete(url, headers=_ads_headers(token), timeout=10)
        try:
            body = r.json()
        except Exception:
            body = {}

        if r.ok:
            return {'ok': True, 'status': r.status_code,
                    'message': 'Ítem eliminado de la campaña exitosamente.',
                    'needs_reauth': False}

        error_msg = (body or {}).get('message') or (body or {}).get('error') or str(body)

        if r.status_code == 401:
            return {'ok': False, 'status': 401,
                    'message': _WRITE_401_MSG, 'needs_reauth': True}
        return {'ok': False, 'status': r.status_code,
                'message': f'Error {r.status_code}: {error_msg}', 'needs_reauth': False}

    except Exception as e:
        return {'ok': False, 'status': 0, 'message': str(e), 'needs_reauth': False}


# ── Análisis de distribución cross-campaña ────────────────────────────────────

def _batch_fetch_items_sales(token: str, item_ids: list[str]) -> dict[str, dict]:
    """
    Obtiene sold_quantity, available_quantity, price y title para todos los items.
    Usa GET /items?ids=... en lotes de 20 (límite de la API de ML).
    Retorna {item_id: {sold_quantity, available_quantity, price, title}}
    """
    import time
    result: dict[str, dict] = {}
    for i in range(0, len(item_ids), 20):
        batch = item_ids[i:i + 20]
        r = _ads_get('/items', token, params={'ids': ','.join(batch)})
        if r['ok'] and r['data']:
            for entry in (r['data'] or []):
                if not isinstance(entry, dict) or entry.get('code') != 200:
                    continue
                body    = entry.get('body', {})
                item_id = str(body.get('id', ''))
                if item_id:
                    result[item_id] = {
                        'sold_quantity':      _int(body.get('sold_quantity', 0)),
                        'available_quantity': _int(body.get('available_quantity', 0)),
                        'price':              _float(body.get('price', 0.0)),
                        'title':              _str(body.get('title', '')),
                        'listing_type':       _str(body.get('listing_type_id', '')),
                    }
        time.sleep(0.1)
    return result


def analyze_distribution(token: str,
                         campaign_map: dict[int, list[str]],
                         campaign_details: dict[int, dict]) -> dict:
    """
    Analiza si cada producto está en la campaña correcta en base a su volumen de ventas
    histórico y señales de calidad.

    Retorna:
      campaigns_ranked: lista de campañas ordenadas por sold_quantity promedio (mayor = más TOP)
      items: lista de todos los items con:
        id, title, price, sold_quantity, stock, perf_pct (0-100),
        level, buy_box, campaign_id, campaign_name,
        status: 'ok' | 'subir' | 'bajar' | 'revisar'
        suggestion: texto explicativo
        target_campaign_id / target_campaign_name (si status != 'ok')
    """
    import time

    all_ids  = [iid for ids in campaign_map.values() for iid in ids]
    sales    = _batch_fetch_items_sales(token, all_ids)

    # Señales de calidad por item (ads API)
    quality: dict[str, dict] = {}
    for item_id in all_ids:
        r = _ads_get(f'/advertising/product_ads/items/{item_id}', token)
        if r['ok'] and r['data']:
            d = r['data'] or {}
            quality[item_id] = {
                'level':   _str(d.get('current_level', 'unknown')).lower(),
                'buy_box': bool(d.get('buy_box_winner', False)),
            }
        time.sleep(0.06)

    # Calcular percentiles globales de sold_quantity
    all_sold = sorted(s.get('sold_quantity', 0) for s in sales.values())
    n        = len(all_sold)
    p33 = all_sold[max(0, n // 3 - 1)]   if n > 0 else 0
    p66 = all_sold[max(0, 2 * n // 3 - 1)] if n > 0 else 0
    sold_max = all_sold[-1] if all_sold else 1

    # Calcular promedio de sold_quantity por campaña
    camp_avgs: dict[int, float] = {}
    for camp_id, item_ids in campaign_map.items():
        vals = [sales.get(iid, {}).get('sold_quantity', 0) for iid in item_ids]
        camp_avgs[camp_id] = sum(vals) / len(vals) if vals else 0.0

    # Ordenar campañas: la de mayor avg de ventas es la "TOP", la menor es "IMPULSO"
    camps_sorted = sorted(camp_avgs.keys(), key=lambda k: camp_avgs[k], reverse=True)
    camp_rank    = {cid: i for i, cid in enumerate(camps_sorted)}  # 0 = más top

    # Construir lista de items con análisis
    items_out: list[dict] = []

    for camp_id, item_ids in campaign_map.items():
        camp_name   = campaign_details.get(camp_id, {}).get('name', str(camp_id))
        c_rank      = camp_rank.get(camp_id, 0)
        n_camps     = len(camp_rank)

        for item_id in item_ids:
            sd   = sales.get(item_id, {})
            qd   = quality.get(item_id, {})
            sold = sd.get('sold_quantity', 0)
            lvl  = qd.get('level', 'unknown')
            bbox = qd.get('buy_box', False)

            # Percentil de ventas (0-100)
            perf_pct = round(sold / sold_max * 100, 1) if sold_max > 0 else 0

            # Tier de performance global
            if sold >= p66:
                perf_tier = 'alto'     # top 33%
            elif sold >= p33:
                perf_tier = 'medio'    # mid 33%
            else:
                perf_tier = 'bajo'     # bottom 33%

            # Detectar si está bien ubicado
            # Lógica: item con ventas ALTAS debería estar en campaña TOP (rank 0)
            #         item con ventas BAJAS debería estar en campaña IMPULSO (rank n-1)
            status       = 'ok'
            suggestion   = ''
            target_id    = None
            target_name  = ''

            if lvl == 'red':
                # Ficha rota — sacar de cualquier campaña TOP
                status     = 'revisar'
                suggestion = f'Calidad ROJA ({sold} ventas) — la publicación tiene problemas críticos. Mejorá la ficha antes de invertir en ads.'

            elif perf_tier == 'alto' and c_rank > 0 and n_camps > 1:
                # Vendedor TOP en campaña que no es la más TOP
                target_id   = camps_sorted[0]
                target_name = campaign_details.get(target_id, {}).get('name', str(target_id))
                status      = 'subir'
                suggestion  = (f'{sold} ventas — está en el top {100-perf_pct:.0f}% de todos tus productos '
                               f'pero en una campaña de menor prioridad. '
                               f'Moverlo a "{target_name}" le daría más presupuesto para escalar.')

            elif perf_tier == 'bajo' and c_rank < n_camps - 1 and sold == 0 and n_camps > 1:
                # Sin ventas en una campaña TOP/MEDIA → mover a impulso
                target_id   = camps_sorted[-1]
                target_name = campaign_details.get(target_id, {}).get('name', str(target_id))
                status      = 'bajar'
                suggestion  = (f'Sin ventas históricas en campaña "{camp_name}". '
                               f'Moverlo a "{target_name}" libera presupuesto TOP para productos que sí convierten.')

            elif perf_tier == 'bajo' and c_rank == 0 and sold < p33 and n_camps > 1:
                # Bajo rendimiento en la campaña más top
                if n_camps > 1:
                    target_id   = camps_sorted[1]
                    target_name = campaign_details.get(target_id, {}).get('name', str(target_id))
                    status      = 'bajar'
                    suggestion  = (f'Solo {sold} ventas (bajo promedio del {100-perf_pct:.0f}%) en la campaña TOP. '
                                   f'Moverlo a "{target_name}" y reservar el presupuesto TOP para los que más convierten.')

            elif perf_tier == 'medio' and c_rank == n_camps - 1 and n_camps > 1:
                # Rendimiento medio en la campaña de menor rango → posible ascenso
                target_id   = camps_sorted[max(0, n_camps - 2)]
                target_name = campaign_details.get(target_id, {}).get('name', str(target_id))
                status      = 'subir'
                suggestion  = (f'{sold} ventas — mejor que muchos en impulso. '
                               f'Moverlo a "{target_name}" para darle más presupuesto y ver si escala.')

            else:
                suggestion = f'{sold} ventas — en el rango esperado para esta campaña.'

            items_out.append({
                'id':              item_id,
                'title':           sd.get('title', item_id),
                'price':           sd.get('price', 0.0),
                'sold_quantity':   sold,
                'stock':           sd.get('available_quantity', 0),
                'perf_pct':        perf_pct,
                'perf_tier':       perf_tier,
                'level':           lvl,
                'buy_box':         bbox,
                'campaign_id':     camp_id,
                'campaign_name':   camp_name,
                'camp_rank':       c_rank,
                'status':          status,
                'suggestion':      suggestion,
                'target_campaign_id':   target_id,
                'target_campaign_name': target_name,
            })

    # Ordenar por sold_quantity desc dentro de cada campaña
    items_out.sort(key=lambda x: (-x['camp_rank'], -x['sold_quantity']))

    # Resumen
    n_subir   = sum(1 for i in items_out if i['status'] == 'subir')
    n_bajar   = sum(1 for i in items_out if i['status'] == 'bajar')
    n_revisar = sum(1 for i in items_out if i['status'] == 'revisar')

    # Campañas ordenadas con su avg de ventas
    camps_ranked = [
        {
            'id':       cid,
            'name':     campaign_details.get(cid, {}).get('name', str(cid)),
            'budget':   campaign_details.get(cid, {}).get('budget', 0),
            'avg_sold': round(camp_avgs[cid], 1),
            'rank':     camp_rank[cid],
        }
        for cid in camps_sorted
    ]

    return {
        'items':           items_out,
        'camps_ranked':    camps_ranked,
        'summary': {
            'total':    len(items_out),
            'subir':    n_subir,
            'bajar':    n_bajar,
            'revisar':  n_revisar,
            'ok':       len(items_out) - n_subir - n_bajar - n_revisar,
        },
    }
