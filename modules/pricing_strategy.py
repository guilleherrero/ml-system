"""
Simulador de políticas de precio.

Para cada publicación con costo cargado, simula 3 escenarios de baja de precio
(-5%, -10%, -20%) y proyecta el impacto en rentabilidad mensual considerando:
  - Fee real de ML (desde fee_rate del stock JSON)
  - Elasticidad precio-demanda según posición relativa al catálogo
  - Ganancia mensual proyectada vs actual

Escenarios:
  Conservadora  -5%  → uplift moderado, mínimo sacrificio de margen
  Moderada     -10%  → uplift significativo, balance precio/volumen
  Agresiva     -20%  → uplift fuerte, máximo volumen, mayor sacrificio

Umbral aceptable de impacto en ganancia: -5% (configurable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Escenarios estándar: (nombre, descuento como ratio)
ESCENARIOS = [
    ("Conservadora", 0.05),
    ("Moderada",     0.10),
    ("Agresiva",     0.20),
]

UMBRAL_ACEPTABLE_PCT = -5.0   # -5% de ganancia mensual = límite "viable"


@dataclass
class Escenario:
    nombre: str
    descuento_pct: float           # ej. 5.0
    precio_nuevo: float
    margen_nuevo_pct: float        # % neto sobre precio
    conv_estimada_pct: float       # % estimado de conversión
    ventas_estimadas: float        # unidades/mes proyectadas
    ganancia_mensual_nueva: float  # ARS/mes
    delta_ganancia_pct: float      # cambio vs actual, ej. -3.2
    es_viable: bool                # delta >= UMBRAL_ACEPTABLE_PCT
    es_win_win: bool               # delta >= 0 (baja precio Y sube ganancia)
    precio_bajo_piso: bool         # precio_nuevo < breakeven (costo+fee)


@dataclass
class ProductoPricingAnalysis:
    item_id: str
    titulo: str
    precio_actual: float
    margen_pct_actual: float       # % neto sobre precio
    ventas_30d: int
    visitas_30d: int
    conv_pct_actual: float
    ganancia_mensual_actual: float
    costo: float
    fee_rate_pct: float            # ej. 16.5
    elasticidad: float             # informativo
    escenarios: list[Escenario] = field(default_factory=list)
    tiene_win_win: bool = False
    mejor_escenario: Optional[str] = None   # nombre del escenario más viable


# ── Modelo de elasticidad precio-demanda ─────────────────────────────────────

def _estimar_elasticidad(conv_actual: float, avg_conv: float) -> float:
    """Elasticidad según posición relativa al catálogo.

    Lógica: items con conversión muy por debajo del promedio tienen más margen
    para crecer al bajar precio (clientes potenciales que se frenaban por el precio).
    Items cercanos al promedio son menos sensibles.

    Rango: 1.5 (poco elástico) → 3.0 (muy elástico).
    Interpretación: elasticidad=2.5 → -10% precio = +25% conversión estimada.
    """
    if avg_conv <= 0 or conv_actual <= 0:
        return 2.0
    ratio = conv_actual / avg_conv
    if ratio < 0.30:
        return 3.0    # conv muy baja vs avg: precio es la barrera principal
    if ratio < 0.60:
        return 2.5
    if ratio < 0.90:
        return 2.0
    return 1.5        # conv cercana al avg: menos sensible al precio


def analizar_producto(
    item: dict,
    costo: float,
    avg_conv: float,
) -> Optional[ProductoPricingAnalysis]:
    """Analiza un producto y calcula sus escenarios de pricing.

    Returns None si faltan datos mínimos (precio, costo).
    """
    precio    = float(item.get("precio") or 0)
    fee_rate  = float(item.get("fee_rate") or 0.15)
    vis       = int(item.get("visitas_30d") or 0)
    vtas      = int(item.get("ventas_30d") or 0)
    conv      = float(item.get("conversion_pct") or 0)
    titulo    = (item.get("titulo") or "")
    iid       = item.get("id", "")

    if precio <= 0 or costo <= 0:
        return None

    # ── Estado actual ────────────────────────────────────────────────────────
    neto_actual          = precio * (1.0 - fee_rate)
    ganancia_unit_actual = neto_actual - costo
    ganancia_mensual_act = vtas * ganancia_unit_actual
    margen_actual_pct    = (ganancia_unit_actual / precio * 100.0) if precio > 0 else 0.0

    # Precio piso: punto donde ganancia_unitaria = 0 (costo = neto)
    precio_piso = costo / (1.0 - fee_rate) if fee_rate < 1.0 else costo * 2.0

    elasticidad = _estimar_elasticidad(conv, avg_conv)

    # Conversión máxima creíble para evitar proyecciones fantasiosas
    # No más de 3× la actual ni más de 2.5× el promedio del catálogo
    if conv > 0:
        conv_max = min(conv * 3.0, avg_conv * 2.5)
    else:
        conv_max = avg_conv * 2.0

    escenarios: list[Escenario] = []

    for nombre, desc_ratio in ESCENARIOS:
        precio_nuevo  = round(precio * (1.0 - desc_ratio))
        neto_nuevo    = precio_nuevo * (1.0 - fee_rate)
        gan_unit_nvo  = neto_nuevo - costo
        margen_nvo_pct = (gan_unit_nvo / precio_nuevo * 100.0) if precio_nuevo > 0 else 0.0

        # Conversión estimada: elasticidad × descuento → uplift
        uplift = elasticidad * desc_ratio
        if conv > 0:
            conv_est = min(conv * (1.0 + uplift), conv_max)
        else:
            # Sin historial de conversión: estimar desde visitas y uplift sobre avg
            conv_est = min(avg_conv * uplift, conv_max)
        conv_est = max(conv_est, 0.0)

        ventas_est      = vis * conv_est / 100.0
        gan_mensual_nva = ventas_est * gan_unit_nvo

        # Delta en ganancia mensual
        if abs(ganancia_mensual_act) > 0.01:
            delta_pct = (gan_mensual_nva - ganancia_mensual_act) / abs(ganancia_mensual_act) * 100.0
        elif gan_mensual_nva > 0:
            delta_pct = 100.0    # ganancia actual ~0, nueva positiva = mejora
        else:
            delta_pct = 0.0

        bajo_piso  = precio_nuevo < precio_piso
        es_win_win = (delta_pct >= 0) and not bajo_piso
        es_viable  = (delta_pct >= UMBRAL_ACEPTABLE_PCT) and not bajo_piso

        escenarios.append(Escenario(
            nombre=nombre,
            descuento_pct=round(desc_ratio * 100, 1),
            precio_nuevo=precio_nuevo,
            margen_nuevo_pct=round(margen_nvo_pct, 1),
            conv_estimada_pct=round(conv_est, 2),
            ventas_estimadas=round(ventas_est, 1),
            ganancia_mensual_nueva=round(gan_mensual_nva),
            delta_ganancia_pct=round(delta_pct, 1),
            es_viable=es_viable,
            es_win_win=es_win_win,
            precio_bajo_piso=bajo_piso,
        ))

    tiene_win_win = any(e.es_win_win for e in escenarios)

    # Mejor escenario:
    # - Si hay múltiples win-win → recomienda Moderada (balance riesgo/proyección)
    # - Si solo un win-win → ese
    # - Si ningún win-win → el más agresivo que siga siendo viable
    win_wins = [e for e in escenarios if e.es_win_win]
    if len(win_wins) > 1:
        mejor = next((e.nombre for e in escenarios if e.nombre == "Moderada"), win_wins[0].nombre)
    elif len(win_wins) == 1:
        mejor = win_wins[0].nombre
    else:
        mejor = None
        for e in reversed(escenarios):
            if e.es_viable:
                mejor = e.nombre
                break

    return ProductoPricingAnalysis(
        item_id=iid,
        titulo=titulo,
        precio_actual=precio,
        margen_pct_actual=round(margen_actual_pct, 1),
        ventas_30d=vtas,
        visitas_30d=vis,
        conv_pct_actual=round(conv, 2),
        ganancia_mensual_actual=round(ganancia_mensual_act),
        costo=round(costo, 2),
        fee_rate_pct=round(fee_rate * 100, 1),
        elasticidad=round(elasticidad, 1),
        escenarios=escenarios,
        tiene_win_win=tiene_win_win,
        mejor_escenario=mejor,
    )


def analizar_catalogo(
    stock_items: list[dict],
    costos_data: dict,
    vis_minimas: int = 20,
) -> list[ProductoPricingAnalysis]:
    """Analiza todos los items con costo cargado y visitas suficientes.

    Returns lista ordenada: win-win primero, luego por visitas descendente.
    """
    convs = [
        float(i.get("conversion_pct") or 0)
        for i in stock_items
        if int(i.get("visitas_30d") or 0) > 0
    ]
    avg_conv = sum(convs) / len(convs) if convs else 1.5

    resultados: list[ProductoPricingAnalysis] = []

    for it in stock_items:
        iid = it.get("id", "")
        ce  = costos_data.get(iid, {})
        costo_val = ce.get("costo") if ce else it.get("costo")
        if costo_val is None:
            continue
        costo_val = float(costo_val)
        if costo_val <= 0:
            continue
        if int(it.get("visitas_30d") or 0) < vis_minimas:
            continue

        analisis = analizar_producto(it, costo_val, avg_conv)
        if analisis:
            resultados.append(analisis)

    resultados.sort(key=lambda x: (not x.tiene_win_win, -x.visitas_30d))
    return resultados
