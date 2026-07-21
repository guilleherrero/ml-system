"""
Simulador de políticas de precio.

Para cada publicación con costo cargado analiza tres dimensiones:
  1. Escenarios de baja de precio (-5%, -10%, -20%)
  2. Optimización de cuotas (¿cuántos compradores realmente las usan?)
  3. Estrategia de umbral de envío gratis ($33k → ¿conviene cruzar hacia abajo?)

Todos los cálculos parten del fee_rate real de órdenes históricas, que incluye
comisión ML + IVA + costo de envío gratis. No se usan tasas estimadas cuando
hay datos reales disponibles.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional

# Acceso a core/fees desde modules/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.fees import get_fee_rates, get_rate


# ── Constantes ───────────────────────────────────────────────────────────────

ESCENARIOS = [
    ("Conservadora", 0.05),
    ("Moderada",     0.10),
    ("Agresiva",     0.20),
]

UMBRAL_ACEPTABLE_PCT   = -5.0    # impacto máximo aceptable en ganancia mensual
UMBRAL_ENVIO_GRATIS    = 33_000  # ARS — precio mínimo para envío gratis obligatorio en ML Argentina
COSTO_CUOTA_EST        = 0.009   # ~0.9% por cuota sin interés (tasa de financiamiento ML estimada)
IMPACTO_CONV_SIN_ENVIO = 0.20    # caída estimada de conversión al perder badge envío gratis (-20%)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class Escenario:
    nombre: str
    descuento_pct: float           # ej. 5.0
    precio_nuevo: float
    margen_nuevo_pct: float        # % neto sobre precio nuevo
    conv_estimada_pct: float
    ventas_estimadas: float        # unidades/mes proyectadas
    ganancia_mensual_nueva: float  # ARS/mes
    delta_ganancia_pct: float      # cambio vs actual (ej. +4.7 o -3.2)
    es_viable: bool                # delta >= UMBRAL_ACEPTABLE_PCT
    es_win_win: bool               # delta >= 0
    precio_bajo_piso: bool         # precio cae bajo el breakeven (costo + fee)


@dataclass
class AnalisisCuotas:
    """Análisis del uso real de cuotas en las ventas históricas del item."""
    n_ordenes: int
    pct_contado: float             # % de órdenes pagadas en 1 cuota (contado)
    cuotas_promedio: float         # promedio ponderado de cuotas (ej. 3.2)
    cuotas_breakdown: dict         # {"1": 65, "2-3": 10, "4-6": 20, "7-12": 5}

    # Costo de financiamiento actual (estimado como % del precio)
    # = (cuotas_promedio - 1) × COSTO_CUOTA_EST
    # Ej: cuotas_promedio=3.2 → (3.2-1)×0.009 = 1.98% del precio por orden
    costo_financiamiento_pct: float

    # Si se redujeran las cuotas máximas a 3:
    ahorro_fee_pct: float          # % de fee que se ahorraría por orden
    ahorro_mensual_ars: float      # ARS/mes estimado de ahorro

    # Veredicto
    sugerencia: str                # "reducir_cuotas" | "mantener" | "cuotas_son_driver"
    mensaje: str                   # explicación legible


@dataclass
class AnalisisUmbralEnvio:
    """Estrategia: ¿conviene publicar una versión por debajo del umbral de envío gratis?"""
    precio_actual: float
    precio_bajo_umbral: float      # UMBRAL_ENVIO_GRATIS - 100

    fee_rate_actual: float         # fee real (incluye comisión + envío)
    fee_rate_sin_envio: float      # fee estimado sin envío gratis (solo comisión + IVA)
    shipping_pct_estimado: float   # componente de envío en el fee actual

    # Ganancia unitaria en cada escenario
    ganancia_unit_actual: float
    ganancia_unit_nueva: float     # a precio_bajo_umbral, sin envío gratis

    # Impacto en conversión por perder el badge de envío gratis
    # Se calculan tres subescenarios de caída de conversión
    ventas_actuales: int
    ventas_pesimista: float        # -30% conversión
    ventas_realista: float         # -20% conversión
    ventas_optimista: float        # -10% conversión

    ganancia_mensual_actual: float
    ganancia_mensual_pesimista: float
    ganancia_mensual_realista: float
    ganancia_mensual_optimista: float

    delta_pesimista_pct: float
    delta_realista_pct: float
    delta_optimista_pct: float

    vale_la_pena: bool             # True si incluso el escenario realista es positivo o neutro
    estrategia: str                # "nueva_publicacion" | "no_recomendado" | "analizar_con_cuidado"
    mensaje: str


@dataclass
class ProductoPricingAnalysis:
    item_id: str
    titulo: str
    precio_actual: float
    margen_pct_actual: float
    ventas_30d: int
    visitas_30d: int
    conv_pct_actual: float
    ganancia_mensual_actual: float
    costo: float
    fee_rate_pct: float            # ej. 26.6
    elasticidad: float
    escenarios: list[Escenario] = field(default_factory=list)
    tiene_win_win: bool = False
    mejor_escenario: Optional[str] = None
    analisis_cuotas: Optional[AnalisisCuotas] = None
    analisis_envio: Optional[AnalisisUmbralEnvio] = None


# ── Modelo de elasticidad precio-demanda ──────────────────────────────────────

def _estimar_elasticidad(conv_actual: float, avg_conv: float) -> float:
    """Elasticidad según posición relativa al catálogo.

    Items con conversión muy por debajo del promedio son más sensibles al precio
    (el precio es la barrera principal). Rango: 1.5 (poco) → 3.0 (muy elástico).
    Interpretación: elasticidad 2.5 con -10% precio = +25% conversión estimada.
    """
    if avg_conv <= 0 or conv_actual <= 0:
        return 2.0
    ratio = conv_actual / avg_conv
    if ratio < 0.30:
        return 3.0
    if ratio < 0.60:
        return 2.5
    if ratio < 0.90:
        return 2.0
    return 1.5


# ── Análisis de cuotas ────────────────────────────────────────────────────────

def _analizar_cuotas(item: dict, precio: float, ventas_30d: int) -> Optional[AnalisisCuotas]:
    """Analiza el uso real de cuotas en las ventas del item.

    Requiere pct_contado y cuotas_promedio en el item (capturados de órdenes reales).
    Devuelve None si no hay suficientes datos.
    """
    pct_contado      = item.get("pct_contado")
    cuotas_promedio  = item.get("cuotas_promedio")
    cuotas_breakdown = item.get("cuotas_breakdown") or {}

    # Necesitamos al menos los dos valores clave
    if pct_contado is None or cuotas_promedio is None:
        return None

    # n_ordenes lo inferimos del breakdown (suma de porcentajes → no sirve; usamos ventas como proxy)
    # El breakdown es en %, no en conteo absoluto. Usamos ventas_30d como referencia de muestra.
    n_ordenes = max(ventas_30d, 1)

    # ── Costo de financiamiento actual ───────────────────────────────────────
    # Cada cuota sin interés que ML ofrece tiene un costo de ~0.9% por cuota.
    # El costo total por orden = (cuotas_promedio - 1) × 0.9%
    # Ej: cuotas_promedio = 3.2 → (3.2 - 1) × 0.009 = 1.98% del precio
    costo_financiamiento_pct = max(0.0, cuotas_promedio - 1) * COSTO_CUOTA_EST

    # ── Ahorro si se reducen cuotas máximas a 3 ──────────────────────────────
    # Nuevo cuotas_promedio asumiendo que quienes usaban más de 3 pasan a usar 3:
    #   nueva_promedio = pct_contado × 1 + (1 - pct_contado) × 3
    nuevo_cuotas_promedio   = pct_contado * 1 + (1 - pct_contado) * 3
    nuevo_costo_financ_pct  = max(0.0, nuevo_cuotas_promedio - 1) * COSTO_CUOTA_EST
    ahorro_fee_pct          = max(0.0, costo_financiamiento_pct - nuevo_costo_financ_pct)

    # Impacto mensual en ARS:
    # Ahorro por unidad = precio × ahorro_fee_pct
    # Ahorro mensual = ventas_30d × ahorro_por_unidad
    ahorro_mensual_ars = round(ventas_30d * precio * ahorro_fee_pct)

    # ── Veredicto ─────────────────────────────────────────────────────────────
    pct_con_cuotas = 1 - pct_contado
    if pct_contado >= 0.80:
        sugerencia = "reducir_cuotas"
        mensaje = (
            f"El {round(pct_contado * 100)}% de tus compradores paga en efectivo (1 cuota). "
            f"Las cuotas sin interés te cuestan financiamiento pero pocos las aprovechan. "
            f"Reducir a 3 cuotas máximas podría ahorrarte ~${ahorro_mensual_ars:,.0f}/mes "
            f"sin impacto significativo en ventas."
        )
    elif pct_con_cuotas >= 0.60:
        sugerencia = "cuotas_son_driver"
        mensaje = (
            f"El {round(pct_con_cuotas * 100)}% de tus compradores usa cuotas "
            f"(promedio {cuotas_promedio:.1f} cuotas). "
            f"Son un driver clave de ventas en este producto — no las reduzcas."
        )
    else:
        sugerencia = "mantener"
        mensaje = (
            f"Mix equilibrado: {round(pct_contado * 100)}% contado, "
            f"{round(pct_con_cuotas * 100)}% en cuotas (promedio {cuotas_promedio:.1f}). "
            f"Las cuotas agregan algo de costo de financiamiento pero también facilitan ventas."
        )

    return AnalisisCuotas(
        n_ordenes=n_ordenes,
        pct_contado=round(pct_contado, 4),
        cuotas_promedio=round(cuotas_promedio, 2),
        cuotas_breakdown=cuotas_breakdown,
        costo_financiamiento_pct=round(costo_financiamiento_pct * 100, 2),  # en %
        ahorro_fee_pct=round(ahorro_fee_pct * 100, 2),                      # en %
        ahorro_mensual_ars=ahorro_mensual_ars,
        sugerencia=sugerencia,
        mensaje=mensaje,
    )


# ── Análisis de umbral envío gratis ──────────────────────────────────────────

def _analizar_umbral_envio(
    item: dict,
    costo: float,
    ventas_30d: int,
    ganancia_mensual_actual: float,
    fees: Optional[dict],
) -> Optional[AnalisisUmbralEnvio]:
    """Evalúa si conviene publicar una versión por debajo del umbral de envío gratis.

    Solo aplica si:
      - El item tiene envío gratis (free_shipping=True)
      - El precio está entre el umbral y 1.5× el umbral (más arriba no tiene sentido)
      - El precio_bajo_umbral es rentable (ganancia unitaria positiva)

    El fee sin envío se estima como la tasa base de comisión ML para ese tipo de
    publicación (obtenida de core/fees.py, calculada a precio $10k donde no aplica
    envío gratis). La diferencia fee_real − fee_base ≈ costo de envío gratis.
    """
    precio        = float(item.get("precio") or 0)
    free_shipping = item.get("free_shipping", False)
    listing_type  = item.get("listing_type", "gold_special")
    fee_rate      = float(item.get("fee_rate") or 0)

    # Solo analizar items con envío gratis obligatorio y en rango útil
    if not free_shipping:
        return None
    if precio <= UMBRAL_ENVIO_GRATIS:
        return None
    if precio > UMBRAL_ENVIO_GRATIS * 1.5:   # más de 50% sobre el umbral → demasiada bajada
        return None
    if fee_rate <= 0 or costo <= 0:
        return None

    # ── Fee sin envío gratis ─────────────────────────────────────────────────
    # fees.py calcula la tasa a precio $10k (sin envío gratis obligatorio).
    # Esa tasa = comisión ML + IVA, SIN shipping. Es la base.
    if fees:
        fee_base = get_rate(listing_type, fees)
    else:
        fee_base = get_rate(listing_type)

    # Componente de envío = diferencia entre lo que se cobra realmente y la base
    shipping_pct = max(0.0, fee_rate - fee_base)

    # Si el shipping estimado es 0 (fee_real <= fee_base), probablemente el fee_rate
    # proviene de una estimación y no de órdenes reales. En ese caso no mostramos el análisis.
    if shipping_pct < 0.01:
        return None

    precio_bajo_umbral = UMBRAL_ENVIO_GRATIS - 100   # $32.900

    # ── Ganancia unitaria actual ──────────────────────────────────────────────
    # precio × (1 - fee_rate) - costo
    ganancia_unit_actual = precio * (1.0 - fee_rate) - costo

    # ── Ganancia unitaria a precio_bajo_umbral sin envío gratis ──────────────
    # Fee = fee_base (solo comisión, sin shipping)
    neto_nuevo          = precio_bajo_umbral * (1.0 - fee_base)
    ganancia_unit_nueva = neto_nuevo - costo

    # Si la ganancia unitaria nueva es negativa no tiene sentido
    if ganancia_unit_nueva <= 0:
        return None

    # ── Impacto en conversión por perder el badge de envío gratis ────────────
    # Estimamos tres escenarios de caída de conversión:
    caida_pesimista  = 0.30   # -30%: compradores muy sensibles al envío
    caida_realista   = 0.20   # -20%: caída típica en Argentina
    caida_optimista  = 0.10   # -10%: compradores más sensibles al precio que al envío

    ventas_pesimista  = round(ventas_30d * (1 - caida_pesimista), 1)
    ventas_realista   = round(ventas_30d * (1 - caida_realista),  1)
    ventas_optimista  = round(ventas_30d * (1 - caida_optimista), 1)

    gan_mens_pesimista  = ventas_pesimista * ganancia_unit_nueva
    gan_mens_realista   = ventas_realista  * ganancia_unit_nueva
    gan_mens_optimista  = ventas_optimista * ganancia_unit_nueva

    # ── Deltas vs situación actual ────────────────────────────────────────────
    base = abs(ganancia_mensual_actual) if abs(ganancia_mensual_actual) > 0.01 else 1.0
    delta_pesimista_pct = round((gan_mens_pesimista - ganancia_mensual_actual) / base * 100, 1)
    delta_realista_pct  = round((gan_mens_realista  - ganancia_mensual_actual) / base * 100, 1)
    delta_optimista_pct = round((gan_mens_optimista - ganancia_mensual_actual) / base * 100, 1)

    # Vale la pena si en el escenario realista la pérdida es menor al 10%
    vale_la_pena = delta_realista_pct >= -10.0

    if delta_optimista_pct >= 0:
        estrategia = "nueva_publicacion"
        mensaje = (
            f"Crear una publicación paralela a ${precio_bajo_umbral:,.0f} (sin envío gratis) "
            f"puede ser viable. En el escenario realista ({round(caida_realista*100)}% menos ventas) "
            f"el impacto es {delta_realista_pct:+.1f}% en ganancia mensual. "
            f"Si tus compradores son sensibles al precio más que al envío, incluso podría mejorar."
        )
    elif vale_la_pena:
        estrategia = "analizar_con_cuidado"
        mensaje = (
            f"La bajada a ${precio_bajo_umbral:,.0f} elimina el costo de envío (−{round(shipping_pct*100, 1)}% del fee) "
            f"pero perder el badge de envío gratis puede bajar las ventas. "
            f"Impacto realista: {delta_realista_pct:+.1f}% en ganancia mensual. "
            f"Evaluá en tu categoría cuánto valoran los compradores el envío gratis."
        )
    else:
        estrategia = "no_recomendado"
        mensaje = (
            f"No recomendado: bajar a ${precio_bajo_umbral:,.0f} reduce el fee de envío "
            f"pero la caída esperada de ventas (-20%) genera una pérdida de "
            f"{delta_realista_pct:+.1f}% en ganancia mensual. "
            f"El ahorro de fee no compensa perder el badge de envío gratis."
        )

    return AnalisisUmbralEnvio(
        precio_actual=precio,
        precio_bajo_umbral=precio_bajo_umbral,
        fee_rate_actual=round(fee_rate * 100, 1),
        fee_rate_sin_envio=round(fee_base * 100, 1),
        shipping_pct_estimado=round(shipping_pct * 100, 1),
        ganancia_unit_actual=round(ganancia_unit_actual),
        ganancia_unit_nueva=round(ganancia_unit_nueva),
        ventas_actuales=ventas_30d,
        ventas_pesimista=ventas_pesimista,
        ventas_realista=ventas_realista,
        ventas_optimista=ventas_optimista,
        ganancia_mensual_actual=round(ganancia_mensual_actual),
        ganancia_mensual_pesimista=round(gan_mens_pesimista),
        ganancia_mensual_realista=round(gan_mens_realista),
        ganancia_mensual_optimista=round(gan_mens_optimista),
        delta_pesimista_pct=delta_pesimista_pct,
        delta_realista_pct=delta_realista_pct,
        delta_optimista_pct=delta_optimista_pct,
        vale_la_pena=vale_la_pena,
        estrategia=estrategia,
        mensaje=mensaje,
    )


# ── Análisis principal de escenarios de precio ───────────────────────────────

def analizar_producto(
    item: dict,
    costo: float,
    avg_conv: float,
    fees: Optional[dict] = None,
) -> Optional[ProductoPricingAnalysis]:
    """Analiza un producto y calcula todos sus escenarios de pricing.

    Devuelve None si faltan datos mínimos (precio, costo).
    """
    precio   = float(item.get("precio") or 0)
    fee_rate = float(item.get("fee_rate") or 0.15)
    vis      = int(item.get("visitas_30d") or 0)
    vtas     = int(item.get("ventas_30d") or 0)
    conv     = float(item.get("conversion_pct") or 0)
    titulo   = (item.get("titulo") or "")
    iid      = item.get("id", "")

    if precio <= 0 or costo <= 0:
        return None

    # ── Estado actual ────────────────────────────────────────────────────────
    neto_actual          = precio * (1.0 - fee_rate)
    ganancia_unit_actual = neto_actual - costo
    ganancia_mensual_act = vtas * ganancia_unit_actual
    margen_actual_pct    = (ganancia_unit_actual / precio * 100.0) if precio > 0 else 0.0

    # Breakeven: precio donde ganancia_unitaria = 0
    precio_piso = costo / (1.0 - fee_rate) if fee_rate < 1.0 else costo * 2.0

    elasticidad = _estimar_elasticidad(conv, avg_conv)

    # Cap de conversión para evitar proyecciones irreales
    conv_max = min(conv * 3.0, avg_conv * 2.5) if conv > 0 else avg_conv * 2.0

    # ── Escenarios de precio ─────────────────────────────────────────────────
    escenarios: list[Escenario] = []

    for nombre, desc_ratio in ESCENARIOS:
        precio_nuevo   = round(precio * (1.0 - desc_ratio))
        neto_nuevo     = precio_nuevo * (1.0 - fee_rate)
        gan_unit_nvo   = neto_nuevo - costo
        margen_nvo_pct = (gan_unit_nvo / precio_nuevo * 100.0) if precio_nuevo > 0 else 0.0

        # Uplift de conversión por elasticidad precio-demanda
        uplift = elasticidad * desc_ratio
        if conv > 0:
            conv_est = min(conv * (1.0 + uplift), conv_max)
        else:
            conv_est = min(avg_conv * uplift, conv_max)
        conv_est = max(conv_est, 0.0)

        ventas_est      = vis * conv_est / 100.0
        gan_mensual_nva = ventas_est * gan_unit_nvo

        if abs(ganancia_mensual_act) > 0.01:
            delta_pct = (gan_mensual_nva - ganancia_mensual_act) / abs(ganancia_mensual_act) * 100.0
        elif gan_mensual_nva > 0:
            delta_pct = 100.0
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

    # Recomendada: el escenario con mayor ganancia mensual absoluta entre los win-win.
    # Si no hay win-win, el más agresivo que sea viable.
    win_wins = [e for e in escenarios if e.es_win_win]
    if win_wins:
        mejor = max(win_wins, key=lambda e: e.ganancia_mensual_nueva).nombre
    else:
        mejor = None
        for e in reversed(escenarios):
            if e.es_viable:
                mejor = e.nombre
                break

    # ── Análisis de cuotas ───────────────────────────────────────────────────
    analisis_cuotas = _analizar_cuotas(item, precio, vtas)

    # ── Análisis de umbral envío gratis ──────────────────────────────────────
    analisis_envio = _analizar_umbral_envio(
        item, costo, vtas, ganancia_mensual_act, fees
    )

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
        analisis_cuotas=analisis_cuotas,
        analisis_envio=analisis_envio,
    )


# ── Análisis del catálogo completo ────────────────────────────────────────────

def analizar_catalogo(
    stock_items: list[dict],
    costos_data: dict,
    vis_minimas: int = 20,
) -> list[ProductoPricingAnalysis]:
    """Analiza todos los items con costo cargado y visitas suficientes.

    Retorna lista ordenada: win-win primero, luego por visitas descendente.
    """
    # Cargar tasas base de comisión (sin envío gratis) para el análisis de umbral
    fees = get_fee_rates()

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

        analisis = analizar_producto(it, costo_val, avg_conv, fees=fees)
        if analisis:
            resultados.append(analisis)

    resultados.sort(key=lambda x: (not x.tiene_win_win, -x.visitas_30d))
    return resultados
