"""
Motor unico de decision de precio — bloque 12.2 de los cimientos.

Antes de esto habia cuatro logicas de precio distintas que no se conocian entre
si, cada una con sus propias constantes y sus propios criterios:

  - `repricing.py`      : competidor x 0.99, o +2% si no hay competidor. No mira
                          conversion, ni elasticidad, ni los umbrales de ML.
  - `top_acciones_diarias.py`: baja un 8% fijo con un piso que permitia hasta
                          -10% de margen, es decir, sugerir vender a perdida.
  - `pricing_strategy.py`: el mas completo — elasticidad estimada, umbral de
                          envio gratis, costo de cuotas — pero nadie mas lo usa.
  - `web/app.py`        : aplica los cambios sin volver a validar nada.

Cuatro fuentes que podian recomendar cosas distintas para el mismo item el mismo
dia. Este modulo es el unico lugar donde se define que es el margen, cual es el
piso y que pasa al cruzar un umbral de ML.

Que NO hace: no decide la estrategia (eso es el bloque 3 del diseno, Sprint C).
Hace la aritmetica, y la hace bien y en un solo lugar.

La funcion mas util para el vendedor es `unidades_para_compensar()`: cuantas
unidades mas hay que vender para que una baja de precio no sea una perdida. No
es una prediccion, es una cuenta exacta, y hasta ahora ningun modulo la hacia.
"""

from __future__ import annotations

# ── Constantes unicas del sistema ────────────────────────────────────────────
# Estaban repetidas y con valores distintos en cada modulo.

# ML Argentina: por encima de este precio el envio gratis es obligatorio y lo
# paga el vendedor. Cruzar este umbral cambia el margen de golpe, no de a poco.
UMBRAL_ENVIO_GRATIS_ARS = 33_000

# Piso duro: por debajo de este margen el sistema NO sugiere un precio.
# top_acciones_diarias tenia -0.10, o sea permitia proponer vender perdiendo
# 10% en cada venta. Un sistema que sugiere perder plata no esta optimizando.
MARGEN_MINIMO_ACEPTABLE = 0.10

# Margen por debajo del cual se avisa aunque se acepte (zona de riesgo).
MARGEN_ALERTA = 0.18

# Cuanto de la mejora teorica de conversion se toma como real al estimar el
# impacto de una baja. Mismo criterio conservador que en duplicados: preferimos
# prometer de menos. Se recalibra cuando Cerebro mida elasticidad real (3.5).
FACTOR_REALISMO = 0.40


def _f(v, d=0.0) -> float:
    try:
        if v is None or v == '':
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


# ── Margen ───────────────────────────────────────────────────────────────────

def margen_unitario(precio, costo, fee_rate) -> float | None:
    """Lo que queda por unidad. None si falta el costo — no se inventa."""
    precio = _f(precio)
    fee    = _f(fee_rate)
    if precio <= 0 or costo in (None, ''):
        return None
    return precio * (1.0 - fee) - _f(costo)


def margen_pct(precio, costo, fee_rate) -> float | None:
    """Margen como fraccion del precio (0.35 = 35%)."""
    m = margen_unitario(precio, costo, fee_rate)
    p = _f(precio)
    return (m / p) if (m is not None and p > 0) else None


def precio_piso(costo, fee_rate, margen_min: float = MARGEN_MINIMO_ACEPTABLE) -> float | None:
    """El precio mas bajo que respeta el margen minimo. None si falta el costo."""
    if costo in (None, ''):
        return None
    den = 1.0 - _f(fee_rate) - margen_min
    if den <= 0:
        return None
    return _f(costo) / den


# ── Umbrales de ML ───────────────────────────────────────────────────────────

def cruce_umbral_envio(precio_antes, precio_despues) -> str | None:
    """Devuelve 'baja_cruzando' | 'sube_cruzando' | None.

    El margen no es lineal con el precio: cruzar el umbral de envio gratis lo
    mueve de golpe. Bajar un 3% puede costar mucho mas que un 3% si el precio
    queda del otro lado, y subir un 2% puede regalar margen si lo cruza hacia
    arriba. Solo `pricing_strategy` sabia de esto; ahora lo sabe todo el sistema.
    """
    a, d = _f(precio_antes), _f(precio_despues)
    if a <= 0 or d <= 0:
        return None
    if a >= UMBRAL_ENVIO_GRATIS_ARS > d:
        return 'baja_cruzando'
    if d >= UMBRAL_ENVIO_GRATIS_ARS > a:
        return 'sube_cruzando'
    return None


# ── La cuenta que le importa al vendedor ─────────────────────────────────────

def unidades_para_compensar(precio_antes, precio_despues, costo, fee_rate,
                            unidades_actuales) -> dict | None:
    """Cuanto mas hay que vender para que la baja no sea una perdida.

    No predice nada: es aritmetica exacta sobre el margen. Si la respuesta es
    "necesitas vender 60% mas", el vendedor puede juzgar solo si es plausible,
    que es mucho mas honesto que mostrarle una proyeccion optimista.
    """
    m_antes   = margen_unitario(precio_antes, costo, fee_rate)
    m_despues = margen_unitario(precio_despues, costo, fee_rate)
    u = _f(unidades_actuales)
    if m_antes is None or m_despues is None or m_antes <= 0:
        return None
    if m_despues <= 0:
        return {'imposible': True,
                'motivo': 'con el precio nuevo cada venta pierde plata: '
                          'no hay volumen que lo compense'}

    ganancia_actual   = m_antes * u
    unidades_necesarias = ganancia_actual / m_despues
    return {
        'imposible':            False,
        'margen_antes':         round(m_antes, 2),
        'margen_despues':       round(m_despues, 2),
        'unidades_actuales':    round(u, 2),
        'unidades_necesarias':  round(unidades_necesarias, 2),
        'unidades_extra':       round(unidades_necesarias - u, 2),
        'aumento_necesario_pct': round((unidades_necesarias / u - 1) * 100, 1) if u > 0 else None,
        'ganancia_actual_mes':  round(ganancia_actual, 0),
    }


# ── Evaluacion completa de un cambio de precio ───────────────────────────────

def evaluar_cambio(precio_actual, precio_nuevo, costo, fee_rate,
                   unidades_30d=0, conv_actual=None, conv_referencia=None) -> dict:
    """Todo lo que hay que saber antes de mover un precio, en un solo lugar.

    Devuelve siempre `viable` (bool) y `motivos` (lista): si no es viable, el
    sistema no debe proponerlo, y el motivo se puede mostrar tal cual.
    """
    p_act = _f(precio_actual)
    p_new = _f(precio_nuevo)
    res: dict = {
        'precio_actual': p_act,
        'precio_nuevo':  p_new,
        'delta_pct':     round((p_new / p_act - 1) * 100, 2) if p_act > 0 else None,
        'viable':        True,
        'motivos':       [],
        'avisos':        [],
    }

    if p_act <= 0 or p_new <= 0:
        res['viable'] = False
        res['motivos'].append('precio invalido')
        return res

    if costo in (None, ''):
        res['viable'] = False
        res['motivos'].append('sin costo cargado no se puede evaluar el margen')
        return res

    m_act = margen_pct(p_act, costo, fee_rate)
    m_new = margen_pct(p_new, costo, fee_rate)
    res['margen_actual_pct']  = round(m_act * 100, 1) if m_act is not None else None
    res['margen_nuevo_pct']   = round(m_new * 100, 1) if m_new is not None else None
    res['margen_unitario_actual'] = round(margen_unitario(p_act, costo, fee_rate) or 0, 0)
    res['margen_unitario_nuevo']  = round(margen_unitario(p_new, costo, fee_rate) or 0, 0)

    piso = precio_piso(costo, fee_rate)
    res['precio_piso'] = round(piso, 0) if piso else None

    if m_new is None or m_new < MARGEN_MINIMO_ACEPTABLE:
        res['viable'] = False
        res['motivos'].append(
            f'el margen quedaria en {res["margen_nuevo_pct"]}%, debajo del minimo '
            f'de {MARGEN_MINIMO_ACEPTABLE:.0%}'
            + (f' (el precio no puede bajar de ${piso:,.0f})'.replace(',', '.') if piso else ''))
    elif m_new < MARGEN_ALERTA:
        res['avisos'].append(
            f'margen ajustado: queda en {res["margen_nuevo_pct"]}%')

    cruce = cruce_umbral_envio(p_act, p_new)
    res['cruce_umbral_envio'] = cruce
    if cruce == 'baja_cruzando':
        res['avisos'].append(
            f'esta baja cruza el umbral de envio gratis de '
            f'${UMBRAL_ENVIO_GRATIS_ARS:,.0f}'.replace(',', '.')
            + ': el envio deja de ser obligatorio, pero tambien se pierde el badge '
              'que ayuda a convertir')
    elif cruce == 'sube_cruzando':
        res['avisos'].append(
            f'esta suba cruza el umbral de ${UMBRAL_ENVIO_GRATIS_ARS:,.0f}'.replace(',', '.')
            + ': a partir de ahi el envio gratis es obligatorio y lo pagas vos, '
              'asi que el margen real sube menos de lo que parece')

    compensar = unidades_para_compensar(p_act, p_new, costo, fee_rate, unidades_30d)
    res['compensacion'] = compensar
    if compensar and not compensar.get('imposible') and compensar.get('aumento_necesario_pct'):
        res['resumen_compensacion'] = (
            f'bajando a ${p_new:,.0f} necesitas vender {compensar["aumento_necesario_pct"]:.0f}% '
            f'mas ({compensar["unidades_necesarias"]:.1f} en vez de '
            f'{compensar["unidades_actuales"]:.1f} al mes) solo para no perder plata'
        ).replace(',', '.')

    if conv_actual is not None and conv_referencia:
        res['conversion_actual']    = _f(conv_actual)
        res['conversion_referencia'] = _f(conv_referencia)
        res['mejora_conv_necesaria_pct'] = (
            round((_f(conv_referencia) / _f(conv_actual) - 1) * 100, 1)
            if _f(conv_actual) > 0 else None)

    return res


def impacto_estimado_baja(precio_actual, precio_nuevo, costo, fee_rate,
                          visitas_30d, conv_actual, conv_referencia,
                          unidades_30d=0) -> tuple[float, dict]:
    """Ganancia mensual adicional estimada por bajar el precio, con freno.

    El calculo anterior asumia que la conversion saltaba directo al promedio del
    catalogo por el solo hecho de bajar el precio. Aca se toma solo una fraccion
    de esa mejora teorica (FACTOR_REALISMO) y se descuenta lo que el item ya
    gana hoy: lo que ya vendes no es ganancia nueva.
    """
    m_new = margen_unitario(precio_nuevo, costo, fee_rate)
    m_act = margen_unitario(precio_actual, costo, fee_rate)
    vis   = _f(visitas_30d)
    c_act = _f(conv_actual) / 100.0
    c_ref = _f(conv_referencia) / 100.0

    if m_new is None or m_new <= 0 or vis <= 0 or c_ref <= c_act:
        return 0.0, {'motivo': 'no hay mejora estimable o el margen nuevo no es positivo'}

    conv_estimada = c_act + (c_ref - c_act) * FACTOR_REALISMO
    unidades_estimadas = vis * conv_estimada
    ganancia_estimada  = unidades_estimadas * m_new
    ganancia_actual    = _f(unidades_30d) * (m_act or 0)

    impacto = max(ganancia_estimada - ganancia_actual, 0.0)

    detalle = {
        'conversion_actual_pct':   round(c_act * 100, 2),
        'conversion_referencia_pct': round(c_ref * 100, 2),
        'conversion_estimada_pct': round(conv_estimada * 100, 2),
        'factor_realismo':         FACTOR_REALISMO,
        'unidades_estimadas':      round(unidades_estimadas, 2),
        'unidades_actuales':       round(_f(unidades_30d), 2),
        'margen_unitario_nuevo':   round(m_new, 0),
        'ganancia_estimada_mes':   round(ganancia_estimada, 0),
        'ganancia_actual_mes':     round(ganancia_actual, 0),
        'formula': ('(visitas x conversion estimada x margen nuevo) - lo que ya gana hoy; '
                    'la conversion estimada toma solo una fraccion de la mejora teorica'),
    }
    return round(impacto, 0), detalle
