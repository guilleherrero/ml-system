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

import math
from dataclasses import dataclass, field

# ── Constantes unicas del sistema ────────────────────────────────────────────
# Estaban repetidas y con valores distintos en cada modulo.

# ML Argentina: por encima de este precio el envio gratis es obligatorio y lo
# paga el vendedor. Cruzar este umbral cambia el margen de golpe, no de a poco.
UMBRAL_ENVIO_GRATIS_ARS = 33_000

# Piso duro: por debajo de este margen el sistema NO sugiere un precio.
# Definido por el usuario: no bajar de 15% de margen. (top_acciones_diarias
# tenia -0.10, o sea permitia proponer vender perdiendo 10% en cada venta.)
MARGEN_MINIMO_ACEPTABLE = 0.15

# Margen por debajo del cual se avisa aunque se acepte (zona de riesgo).
MARGEN_ALERTA = 0.20

# Costo de financiamiento por cuota sin interes, como fraccion del precio.
# Cada cuota adicional se paga con margen: es plata que sale del bolsillo del
# vendedor aunque no se vea en el precio de lista.
COSTO_POR_CUOTA = 0.009

# Buckets con que ML agrupa las cuotas usadas realmente por los compradores,
# calculados desde las ordenes (payments[0].installments), no estimados.
BUCKET_MAX = {'1': 1, '2-3': 3, '4-6': 6, '7-12': 12, '13+': 18}
BUCKET_AVG = {'1': 1.0, '2-3': 2.5, '4-6': 5.0, '7-12': 9.5, '13+': 15.0}
BUCKET_ORDEN = ['1', '2-3', '4-6', '7-12', '13+']

# Hasta que porcentaje de compradores afectados se considera de bajo riesgo
# reducir cuotas.
RIESGO_CUOTAS_BAJO  = 0.08
RIESGO_CUOTAS_MEDIO = 0.20

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
                   unidades_30d=0, conv_actual=None, conv_referencia=None,
                   cuotas_breakdown=None, cuotas_actuales_max: int = 12) -> dict:
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

    # ¿Hay una forma mas barata de conseguir lo mismo? Antes de bajar el precio
    # —que es publico, lo ven los repricers ajenos y cuesta revertir— se mira si
    # reducir cuotas alcanza. El descuento que "compra" el ahorro de cuotas se
    # expresa en la misma unidad para poder compararlos de frente.
    if cuotas_breakdown and p_new < p_act:
        descuento_buscado = abs(res['delta_pct'] or 0)
        pasos = analizar_reduccion_cuotas(p_act, unidades_30d, cuotas_breakdown,
                                          cuotas_actuales_max)
        recomendables = [x for x in pasos if x['recomendado']]
        if recomendables:
            mejor = recomendables[0]
            res['alternativa_cuotas'] = mejor
            cubre = mejor['equivale_a_descuento_pct'] >= descuento_buscado * 0.6
            res['alternativa_cuotas_cubre'] = cubre
            res['avisos'].append(
                ('en vez de bajar el precio: ' if cubre else 'ademas del precio: ')
                + mejor['resumen']
                + f'. Equivale a un descuento de {mejor["equivale_a_descuento_pct"]:.1f}% '
                  f'sin tocar el precio de lista')

    return res


# ── Cuotas: la palanca que no toca el precio de lista ────────────────────────
# Bajar el precio es publico, universal e irreversible en la practica: lo ven
# todos los compradores y los repricers de la competencia. Reducir las cuotas
# sin interes recupera margen sin mover el precio de lista, y solo afecta al
# segmento que realmente las usaba.
#
# La cuenta que importa: bajar de 12 a 6 cuotas NO molesta a quien ya compraba
# en 6. Solo afecta a los que necesitaban 7 a 12. Si ese segmento es chico, es
# margen casi gratis. Esta logica existia dentro de pricing_strategy, encerrada
# en una pantalla que nada mas usaba; ahora vive en el motor y puede compararse
# con una baja de precio.

def _pct_que_usaba_mas_de(breakdown: dict, max_cuotas: int) -> float:
    """Fraccion de compradores que usaba MAS cuotas que el nuevo tope."""
    if not breakdown:
        return 0.0
    total = sum(_f(v) for v in breakdown.values()) or 100.0
    afectados = sum(_f(breakdown.get(b, 0)) for b in BUCKET_ORDEN
                    if BUCKET_MAX[b] > max_cuotas)
    return afectados / total


def _cuotas_promedio_con_tope(breakdown: dict, max_cuotas: int) -> float:
    if not breakdown:
        return 1.0
    total = sum(_f(v) for v in breakdown.values()) or 100.0
    prom = 0.0
    for b in BUCKET_ORDEN:
        prom += (_f(breakdown.get(b, 0)) / total) * min(BUCKET_AVG[b], float(max_cuotas))
    return prom


def analizar_reduccion_cuotas(precio, ventas_30d, cuotas_breakdown,
                              cuotas_actuales_max: int = 12) -> list[dict]:
    """Pasos posibles de reduccion de cuotas, con su ahorro y su riesgo real.

    Devuelve una lista ordenada de mejor a peor. Cada paso trae la traduccion
    que sirve para decidir: cuanto descuento de precio equivale ese ahorro.
    """
    if not cuotas_breakdown:
        return []
    p = _f(precio)
    v = _f(ventas_30d)
    pasos = []
    escalones = [(12, 6), (12, 3), (6, 3), (6, 1), (3, 1)]

    for de_max, a_max in escalones:
        if de_max > cuotas_actuales_max or a_max >= cuotas_actuales_max:
            continue
        pct_afectados = _pct_que_usaba_mas_de(cuotas_breakdown, a_max)

        prom_de = _cuotas_promedio_con_tope(cuotas_breakdown, min(de_max, cuotas_actuales_max))
        prom_a  = _cuotas_promedio_con_tope(cuotas_breakdown, a_max)
        ahorro_pct = max(0.0, (prom_de - prom_a) * COSTO_POR_CUOTA)
        if ahorro_pct <= 0:
            continue

        if pct_afectados <= 0.03:
            riesgo = 'muy_bajo'
        elif pct_afectados <= RIESGO_CUOTAS_BAJO:
            riesgo = 'bajo'
        elif pct_afectados <= RIESGO_CUOTAS_MEDIO:
            riesgo = 'medio'
        else:
            riesgo = 'alto'

        pasos.append({
            'de_max':            de_max,
            'a_max':             a_max,
            'pct_afectados':     round(pct_afectados * 100, 1),
            'pct_no_afectados':  round((1 - pct_afectados) * 100, 1),
            'ahorro_pct_precio': round(ahorro_pct * 100, 2),
            'ahorro_mensual_ars': round(v * p * ahorro_pct, 0),
            'riesgo':            riesgo,
            'recomendado':       riesgo in ('muy_bajo', 'bajo'),
            # La traduccion util: este ahorro "paga" un descuento de este tamano
            'equivale_a_descuento_pct': round(ahorro_pct * 100, 2),
            'resumen': (
                f'bajar de {de_max} a {a_max} cuotas recupera '
                f'{ahorro_pct * 100:.1f}% de margen y solo afecta al '
                f'{pct_afectados * 100:.0f}% de los compradores '
                f'(el {(1 - pct_afectados) * 100:.0f}% ya compraba en {a_max} o menos)'),
        })

    pasos.sort(key=lambda x: (not x['recomendado'], -x['ahorro_mensual_ars']))
    return pasos


def menu_de_palancas(precio, costo, fee_rate, *, precio_sugerido=None,
                     ventas_30d=0, cuotas_breakdown=None,
                     cuotas_actuales_max: int = 12) -> dict:
    """Compara bajar el precio contra reducir cuotas, en la misma unidad.

    Responde la pregunta del vendedor: "necesito ser mas competitivo, cual es la
    forma mas barata en margen de lograrlo".
    """
    p = _f(precio)
    res: dict = {'precio_actual': p, 'opciones': []}

    if precio_sugerido:
        ev = evaluar_cambio(p, precio_sugerido, costo, fee_rate, unidades_30d=ventas_30d)
        res['opciones'].append({
            'palanca':          'bajar_precio',
            'detalle':          f'bajar a ${_f(precio_sugerido):,.0f}'.replace(',', '.'),
            'descuento_pct':    abs(ev.get('delta_pct') or 0),
            'costo_margen_pp':  round((ev.get('margen_actual_pct') or 0)
                                      - (ev.get('margen_nuevo_pct') or 0), 1),
            'margen_actual_pct': ev.get('margen_actual_pct'),
            'margen_nuevo_pct':  ev.get('margen_nuevo_pct'),
            'viable':           ev.get('viable'),
            'motivos':          ev.get('motivos'),
            'avisos':           ev.get('avisos'),
            'compensacion':     ev.get('resumen_compensacion'),
            'publico':          True,
        })

    for paso in analizar_reduccion_cuotas(p, ventas_30d, cuotas_breakdown,
                                          cuotas_actuales_max):
        # Reducir cuotas no baja el precio: SUBE el margen. El descuento que
        # "compra" es cuanto se podria bajar el precio quedando igual que hoy.
        m_actual = margen_pct(p, costo, fee_rate)
        res['opciones'].append({
            'palanca':          'reducir_cuotas',
            'detalle':          f'de {paso["de_max"]} a {paso["a_max"]} cuotas',
            'descuento_pct':    0.0,
            'costo_margen_pp':  0.0,
            'gana_margen_pp':   paso['ahorro_pct_precio'],
            'margen_actual_pct': round(m_actual * 100, 1) if m_actual is not None else None,
            'margen_nuevo_pct':  round((m_actual + paso['ahorro_pct_precio'] / 100) * 100, 1)
                                 if m_actual is not None else None,
            'pct_afectados':    paso['pct_afectados'],
            'riesgo':           paso['riesgo'],
            'viable':           paso['recomendado'],
            'ahorro_mensual_ars': paso['ahorro_mensual_ars'],
            'equivale_a_descuento_pct': paso['equivale_a_descuento_pct'],
            'resumen':          paso['resumen'],
            'publico':          False,
        })

    # La mejor es la que consigue el objetivo costando menos margen
    viables = [o for o in res['opciones'] if o.get('viable')]
    if viables:
        mejor = min(viables, key=lambda o: o.get('costo_margen_pp', 0)
                    - o.get('gana_margen_pp', 0))
        res['recomendada'] = mejor['palanca']
        res['motivo_recomendacion'] = (
            mejor.get('resumen')
            or f'es la que menos margen cuesta ({mejor.get("costo_margen_pp")} puntos)')
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


# ── Estrategia de 3 publicaciones (Calculadora de Estrategia de Precios) ────
# Todo lo de arriba evalua UN cambio de precio sobre una publicacion existente.
# Esto resuelve el problema inverso y mas amplio: dado un costo y un objetivo
# de ganancia (ROI o margen), que precio hay que poner en cada una de tres
# publicaciones (Batalla / Medio / Compensa) segun la estrategia elegida, y
# cuantas ventas por dia hacen falta para que convenga frente a lo que se gana
# hoy. Vive acá y no en un archivo aparte para que siga habiendo un solo motor:
# comparte UMBRAL_ENVIO_GRATIS_ARS y MARGEN_MINIMO_ACEPTABLE con el resto del
# archivo en vez de redefinirlos.
#
# A diferencia de margen_unitario() (que toma un fee_rate ya mezclado, tipico
# de datos reales de ordenes), acá los cargos van desglosados (comision +
# cuotas + IIBB + percepcion de IVA) porque hace falta variar cada componente
# por separado entre las tres publicaciones y por escalon de cuotas.
#
# Percepcion de IVA: en teoria es un credito fiscal recuperable, pero para
# esta cuenta en la practica no se recupera — por eso se trata como costo
# real, igual que el IIBB, y se resta siempre. Decision explicita del usuario
# (no la default silenciosa que sugería la spec original).

@dataclass
class Cargos:
    iibb: float = 3.0               # %
    percepcion_iva: float = 7.0     # % — no se recupera para esta cuenta, se trata como costo real
    envio: float = 5000.0           # $ por venta cuando corresponde
    umbral_envio: float = 33000.0   # $ desde donde el envio lo paga el vendedor
    envio_bajo_umbral: bool = False
    fijo_menos_15k: float = 1115.0
    fijo_15k_25k: float = 2300.0
    fijo_25k_umbral: float = 2810.0
    redondeo: bool = True           # precios terminados en 999


@dataclass
class Publicacion:
    nombre: str                     # "Batalla" | "Medio" | "Compensa" (editable)
    comision: float                 # %
    costo_cuotas: float             # % (0 si el interes lo paga el comprador)
    descripcion_tipo: str = ""      # "Clasica, cuotas con interes", etc.
    publicidad_dia: float = 0.0     # $ por dia, 0 si no se invierte
    listing_type: str = "gold_special"   # gold_special = Clasica, gold_pro = Premium
    cuotas: int = 0                 # escalon de cuotas sin interes; 0 = sin cuotas propias


@dataclass
class Situacion:                    # "Tu situacion hoy"
    precio: float
    comision: float
    costo_cuotas: float
    ventas_dia: float
    publicidad_dia: float = 0.0


@dataclass
class Objetivo:
    modo: str                       # "roi" | "margen"
    objetivo: float                 # % (ej. 120 en roi, 30 en margen)
    piso: float                     # % en el mismo modo


def tasa_cargos(pub: Publicacion, c: Cargos) -> float:
    return (pub.comision + pub.costo_cuotas + c.iibb + c.percepcion_iva) / 100


def cargo_fijo(precio: float, c: Cargos) -> float:
    f = 0.0
    if precio >= c.umbral_envio or c.envio_bajo_umbral:
        f += c.envio
    if precio < c.umbral_envio:
        if precio < 15000:   f += c.fijo_menos_15k
        elif precio < 25000: f += c.fijo_15k_25k
        else:                f += c.fijo_25k_umbral
    return f


def ganancia_publicacion(precio: float, pub: Publicacion, c: Cargos, costo: float) -> float:
    return precio * (1 - tasa_cargos(pub, c)) - cargo_fijo(precio, c) - costo


def _step(P: float) -> int:
    return 100 if P < 10000 else 1000


def redondear_arriba(P: float, c: Cargos) -> float:
    if not c.redondeo or math.isinf(P): return P
    s = _step(P)
    return math.ceil((P + 1) / s) * s - 1


def redondear_abajo(P: float, c: Cargos) -> float:
    if not c.redondeo or math.isinf(P): return P
    s = _step(P)
    return math.floor((P + 1) / s) * s - 1


def precio_para_ganancia(G: float, pub: Publicacion, c: Cargos, costo: float) -> float:
    """Precio que deja G pesos de ganancia. Itera porque el cargo fijo depende del precio."""
    r = tasa_cargos(pub, c)
    if r >= 1: return math.inf
    P = (costo + G + c.envio) / (1 - r)
    for _ in range(8):
        P = (costo + G + cargo_fijo(P, c)) / (1 - r)
    return P


def precio_para_margen(m: float, pub: Publicacion, c: Cargos, costo: float) -> float:
    """m viene en fraccion (0.30 = 30%)."""
    d = 1 - tasa_cargos(pub, c) - m
    if d <= 0: return math.inf
    P = (costo + c.envio) / d
    for _ in range(8):
        P = (costo + cargo_fijo(P, c)) / d
    return P


def precio_objetivo(pub: Publicacion, c: Cargos, costo: float, obj: Objetivo) -> float:
    return (precio_para_ganancia(costo * obj.objetivo / 100, pub, c, costo)
            if obj.modo == "roi" else
            precio_para_margen(obj.objetivo / 100, pub, c, costo))


def precio_piso_objetivo(pub: Publicacion, c: Cargos, costo: float, obj: Objetivo) -> float:
    return (precio_para_ganancia(costo * obj.piso / 100, pub, c, costo)
            if obj.modo == "roi" else
            precio_para_margen(obj.piso / 100, pub, c, costo))


def precio_batalla(safe0: float, floor0: float, competidor_min: float, c: Cargos) -> tuple[float, str]:
    """Precio de la publicacion de batalla. Devuelve (precio, motivo)."""
    if competidor_min <= 0:
        return redondear_arriba(safe0, c), "sin_competidor"
    if safe0 <= competidor_min * 0.99:
        # el precio objetivo ya le gana: no bajar de mas
        return redondear_arriba(safe0, c), "objetivo_ya_gana"
    p0 = redondear_abajo(competidor_min * 0.99, c)
    if p0 >= floor0:
        return p0, "debajo_del_competidor"
    return redondear_arriba(floor0, c), "piso_no_permite_ganar"


def estrategia(goal: str, pubs: list[Publicacion], c: Cargos, costo: float,
              obj: Objetivo, competidor_min: float) -> tuple[list[float], list[float], str]:
    """goal: 'rent' | 'comp' | 'vel'.

    rent (Maxima rentabilidad): las tres al objetivo.
    comp (Competir sin perder rentabilidad): solo la batalla baja, hasta el
        competidor y nunca debajo del piso. Medio y Compensa quedan en el objetivo.
    vel (Velocidad con rentabilidad excelente): las tres bajan igualando la
        ganancia en pesos de la batalla, con el piso como limite inferior.
    """
    safe   = [precio_objetivo(p, c, costo, obj) for p in pubs]
    floors = [precio_piso_objetivo(p, c, costo, obj) for p in pubs]
    p0, motivo = precio_batalla(safe[0], floors[0], competidor_min, c)

    if goal == "rent":
        precios = [redondear_arriba(x, c) for x in safe]
    elif goal == "comp":
        precios = [p0] + [redondear_arriba(x, c) for x in safe[1:]]
    elif goal == "vel":
        G = ganancia_publicacion(p0, pubs[0], c, costo)
        precios = [p0] + [redondear_arriba(max(floors[i], precio_para_ganancia(G, pubs[i], c, costo)), c)
                          for i in (1, 2)]
    else:
        raise ValueError(goal)

    ganancias = [ganancia_publicacion(precios[i], pubs[i], c, costo) for i in range(len(pubs))]
    return precios, ganancias, motivo


def ganancia_hoy(s: Situacion, c: Cargos, costo: float) -> tuple[float, float]:
    r = (s.comision + s.costo_cuotas + c.iibb + c.percepcion_iva) / 100
    g_venta = s.precio * (1 - r) - cargo_fijo(s.precio, c) - costo
    g_dia = g_venta * s.ventas_dia - s.publicidad_dia
    return g_venta, g_dia


def ventas_para_empatar(g_dia_hoy: float, ganancias: list[float],
                        publicidad_total: float) -> tuple[float, float] | None:
    """Devuelve (minimo, maximo). El rango sale de que distintas publicaciones
    dejan distinta ganancia por venta."""
    meta = g_dia_hoy + publicidad_total
    validas = [g for g in ganancias if g > 0 and math.isfinite(g)]
    if not validas: return None
    return meta / max(validas), meta / min(validas)


def _ganancia_promedio_ponderada(ganancias: list[float], mix_batalla_pct: float,
                                 split_medio: float) -> float:
    """Ganancia por venta promedio si la Batalla se lleva `mix_batalla_pct`% de
    las ventas y el resto se reparte Medio/Compensa segun `split_medio`."""
    b = mix_batalla_pct / 100
    sp = split_medio / 100
    return b * ganancias[0] + (1 - b) * (sp * ganancias[1] + (1 - sp) * ganancias[2])


def mapa_decision(g_dia_hoy: float, ganancias: list[float], split_medio: float,
                  publicidad_total: float, ventas_hoy: float) -> dict:
    """Solo tiene sentido para la estrategia 'comp' (la unica donde las tres
    publicaciones dejan ganancias distintas segun quien se lleve la venta).

    split_medio: % del reparto entre Medio y Compensa que se lleva Medio
    (supuesto). Devuelve filas (ventas/dia) x columnas (% que se lleva la
    batalla) con la diferencia diaria de ganancia vs. hoy.
    """
    cols = [20, 30, 40, 50, 60, 70, 80, 90, 100]
    base = max(0.5, ventas_hoy)
    filas = sorted({max(0.5, round(base * m * 2) / 2)
                    for m in (0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4)}, reverse=True)

    return {
        "cols": cols,
        "filas": filas,
        "celdas": [[u * _ganancia_promedio_ponderada(ganancias, cc, split_medio) - publicidad_total - g_dia_hoy
                    for cc in cols] for u in filas],
    }


def resultado_prueba(ganancias: list[float], g_dia_hoy: float, publicidad_total: float,
                     ventas_dia_prueba: float, mix_batalla_pct: float | None = None,
                     split_medio: float = 50.0) -> dict:
    """Evalua a mano lo que paso en una prueba real contra la ganancia de hoy.

    Version simple del veredicto (spec sprint 4) que no necesita el snapshot
    diario automatico: se carga lo medido y da un numero. `mix_batalla_pct` es
    el % de las ventas de la prueba que se llevo la Batalla — solo importa
    cuando las tres publicaciones dejan ganancias distintas (estrategia
    'comp'); si no se pasa, se promedia entre las publicaciones viables.
    """
    finitas = [g for g in ganancias if g is not None and math.isfinite(g)]
    if not finitas:
        return {"viable": False}

    if mix_batalla_pct is not None and len(ganancias) == 3 and all(math.isfinite(g) for g in ganancias):
        ganancia_prom = _ganancia_promedio_ponderada(ganancias, mix_batalla_pct, split_medio)
    else:
        ganancia_prom = sum(finitas) / len(finitas)

    ganancia_dia_prueba = ventas_dia_prueba * ganancia_prom - publicidad_total
    delta = ganancia_dia_prueba - g_dia_hoy
    return {
        "viable": True,
        "ganancia_dia_prueba": round(ganancia_dia_prueba, 2),
        "delta_vs_hoy": round(delta, 2),
        "conviene": delta >= 0,
    }


def escalera_meta(multiplicador: float, g_dia_hoy: float, pub: Publicacion, c: Cargos,
                  costo: float, precios: list[float], stock: int, dias_reposicion: int,
                  piso_precio: float, competidor_min: float) -> list[dict]:
    """Para cada precio candidato: ganancia/venta, ventas/dia necesarias para
    multiplicar la ganancia de hoy, dias de stock que eso implica, y banderas
    de viabilidad. El "precio a probar" no lo elige esta funcion: es el precio
    mas alto de la lista que cumple las tres condiciones (le gana al
    competidor, no rompe el piso, el stock alcanza) — eso lo decide quien
    llama, mostrando siempre la regla usada junto al numero."""
    meta = g_dia_hoy * multiplicador
    out = []
    for p in precios:
        g = ganancia_publicacion(p, pub, c, costo)
        if g <= 0:
            out.append({"precio": p, "viable": False}); continue
        n = meta / g
        dias = stock / n if n > 0 else math.inf
        out.append({
            "precio": p, "ganancia_venta": g, "ventas_necesarias": n,
            "dias_stock": dias,
            "rompe_piso": p < piso_precio,
            "no_gana_competidor": competidor_min > 0 and p >= competidor_min,
            "stock_insuficiente": dias < dias_reposicion,
        })
    return out
