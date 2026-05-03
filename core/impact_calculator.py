"""
Sprint 4.1 — Calculadores puros de impacto monetario mensual estimado.

Cada función toma datos crudos de un detector (Buy Box perdido, Ads sangrando,
tráfico desperdiciado, stock crítico, stock muerto Full, duplicados) y devuelve
un float en ARS/mes. Sin side effects, sin I/O.

Conservadurismo: cuando faltan datos, devolvemos 0 antes de inventar números.
Las fórmulas reflejan deltas plausibles, no garantías.
"""

import logging

_logger = logging.getLogger(__name__)


def impacto_buybox_perdido(
    precio: float,
    margen_pct: float,
    velocidad_mensual: float,
    bb_lift: float = 0.40,
) -> float:
    """Estimación recuperable si el item recupera el Buy Box.

    Fórmula: ventas_extra = velocidad_actual_mensual * bb_lift
             impacto      = ventas_extra * (precio * margen_pct/100)

    bb_lift=0.40 — supuesto conservador: ganar BB sube las ventas un 40%
    (estudios de ML sugieren 30-60% según categoría).
    """
    if precio <= 0 or margen_pct <= 0 or velocidad_mensual <= 0:
        return 0.0
    margen_unit = precio * (margen_pct / 100.0)
    ventas_extra = velocidad_mensual * bb_lift
    return round(ventas_extra * margen_unit, 2)


def impacto_ads_gap(spend_30d: float, revenue_ads_30d: float) -> float:
    """Dinero "sangrado" en una campaña: spend - revenue. Si revenue >= spend,
    no hay gap (la campaña paga sus ventas). Devuelve siempre >= 0."""
    if spend_30d <= 0:
        return 0.0
    gap = spend_30d - max(0.0, revenue_ads_30d)
    return round(max(0.0, gap), 2)


def impacto_trafico_desperdiciado(
    visitas_30d: int,
    precio: float,
    margen_pct: float,
    conversion_objetivo: float = 0.01,
) -> float:
    """Item con tráfico alto y 0 (o casi 0) ventas: cuánto se ganaría si
    convirtiera al objetivo (1% por default).

    Fórmula: ventas_objetivo = visitas * conversion_objetivo
             impacto         = ventas_objetivo * margen_unitario

    Threshold mínimo: 50 visitas/30d para evitar ruido."""
    if visitas_30d < 50 or precio <= 0 or margen_pct <= 0:
        return 0.0
    margen_unit = precio * (margen_pct / 100.0)
    ventas_objetivo = visitas_30d * conversion_objetivo
    return round(ventas_objetivo * margen_unit, 2)


def impacto_stock_critico(
    velocidad_diaria: float,
    dias_hasta_quiebre: float,
    precio: float,
    margen_pct: float,
    horizonte_dias: int = 30,
) -> float:
    """Si el item se queda sin stock antes del horizonte, perdés ventas hasta
    que repongas. Estimamos el costo del quiebre dentro del próximo mes.

    Fórmula: dias_sin_stock = max(0, horizonte - dias_hasta_quiebre)
             impacto        = dias_sin_stock * velocidad * margen_unit

    Threshold: velocidad >= 0.5 unid/día (15+ ventas/mes) para evitar ruido."""
    if velocidad_diaria < 0.5 or precio <= 0 or margen_pct <= 0:
        return 0.0
    if dias_hasta_quiebre is None or dias_hasta_quiebre >= horizonte_dias:
        return 0.0
    dias_sin_stock = max(0, horizonte_dias - dias_hasta_quiebre)
    margen_unit = precio * (margen_pct / 100.0)
    return round(dias_sin_stock * velocidad_diaria * margen_unit, 2)


def impacto_stock_muerto_full(
    costo_almacenamiento_acumulado: float,
    dias_acumulados: float,
) -> float:
    """Costo mensual proyectado de stock muerto en Full.
    Si acumuló X ARS en Y días, mensual = X * (30/Y)."""
    if costo_almacenamiento_acumulado <= 0 or dias_acumulados <= 0:
        return 0.0
    return round(costo_almacenamiento_acumulado * (30.0 / dias_acumulados), 2)


def impacto_duplicados(
    visitas_perdidas_30d: int,
    precio_ganadora: float,
    conversion_ganadora: float,
    margen_pct: float = 100.0,
) -> float:
    """Wrapper sobre la lógica de detector_duplicados. El módulo ya calcula
    impacto_monetario_estimado, esto se usa solo si necesitamos recalcular
    desde campos crudos.

    margen_pct=100 (default) trata el precio como ganancia bruta — usar margen_pct
    real cuando lo tengas para evitar sobreestimar."""
    if visitas_perdidas_30d <= 0 or precio_ganadora <= 0 or conversion_ganadora <= 0:
        return 0.0
    margen_unit = precio_ganadora * (margen_pct / 100.0)
    return round(visitas_perdidas_30d * conversion_ganadora * margen_unit, 2)
