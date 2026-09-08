"""
Diagnostico diferencial de competencia: POR QUE te esta ganando.

Saber que un competidor te pasa no sirve de nada por si solo. Lo que cambia la
decision es la causa, porque cada causa tiene una respuesta distinta y bajar el
precio cuando el problema son las keywords es tirar margen a la basura.

El embudo tiene dos etapas y cada una falla distinto:

    ¿ME ENCUENTRAN?  -> posicion en la busqueda -> keywords, relevancia, ranking
    ¿ME ELIGEN?      -> conversion              -> precio, fotos, envio, cuotas,
                                                   reputacion, reviews

De ahi sale el diagnostico, que se apoya en dos cosas que el sistema ya tiene:
la serie diaria de Cerebro (visitas organicas y conversion, separadas del
trafico pago) y la comparacion contra el competidor.

    visitas organicas CAEN + posicion CAE     -> te ganan en POSICIONAMIENTO
    visitas SE MANTIENEN + conversion CAE     -> te encuentran pero no te eligen
    caen las dos                              -> te pasaron en las dos cosas
    cae todo pero la demanda del rubro tambien-> es el MERCADO, no vos

Y dentro de "no te eligen", se separa por que:

    el es mas barato de forma relevante        -> PRECIO
    precios parecidos pero el tiene Full,
    mas cuotas, mejor reputacion o mas fotos   -> CONDICIONES
    ninguna de las anteriores                  -> sin causa clara, no inventar

Cada diagnostico devuelve la evidencia que lo sostiene y las acciones
recomendadas ordenadas por lo que cuestan en margen. Las de precio pasan por
`precio_motor`, asi que respetan el piso de 15% y ofrecen la alternativa de
reducir cuotas antes que bajar el precio de lista.

Lo que se aprende: cada correccion derivada de un diagnostico se registra en
Cerebro con el diagnostico como hipotesis. A los 7 y 14 dias se sabe si la
respuesta fue la correcta, y con los casos acumulados el sistema aprende que
diagnostico acierta en cada rubro.
"""

from __future__ import annotations

import re

from modules import precio_motor

# Palabras que no distinguen un producto de otro
_RUIDO = {
    'de', 'la', 'el', 'para', 'con', 'sin', 'por', 'y', 'a', 'en', 'un', 'una',
    'los', 'las', 'del', 'al', 'mas', 'nuevo', 'nueva', 'original', 'envio',
    'gratis', 'oferta', 'promo', 'super', 'kit', 'set', 'pack', 'x', 'unidades',
}

# Cuanto mas barato tiene que estar para considerar que el precio es la causa
BRECHA_PRECIO_RELEVANTE = 0.05      # 5%
BRECHA_PRECIO_FUERTE    = 0.15      # 15%

# Caidas que se consideran senal y no ruido
CAIDA_VISITAS_SIGNIFICATIVA   = 15.0   # %
CAIDA_CONVERSION_SIGNIFICATIVA = 20.0  # %
CAIDA_POSICION_SIGNIFICATIVA   = 3     # lugares


def _tokens(texto: str) -> list[str]:
    palabras = re.findall(r'[a-z0-9]+', (texto or '').lower())
    return [p for p in palabras if len(p) > 2 and p not in _RUIDO]


def _f(v, d=0.0) -> float:
    try:
        if v is None or v == '':
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


# ── Comparacion de titulos ───────────────────────────────────────────────────

def comparar_titulos(mi_titulo: str, su_titulo: str,
                     keywords_buscadas: list[str] | None = None) -> dict:
    """Que dice el titulo del competidor que el mio no dice.

    `keywords_buscadas` (de autosuggest) sirve para separar lo que la gente
    realmente busca de lo que es relleno del vendedor.
    """
    mios = _tokens(mi_titulo)
    suyos = _tokens(su_titulo)
    set_mios, set_suyos = set(mios), set(suyos)

    faltantes = [t for t in suyos if t not in set_mios]
    sobrantes = [t for t in mios if t not in set_suyos]
    comunes   = [t for t in suyos if t in set_mios]

    buscadas = set()
    for kw in (keywords_buscadas or []):
        buscadas.update(_tokens(kw))

    faltantes_buscadas = [t for t in faltantes if t in buscadas] if buscadas else []

    solapamiento = len(comunes) / len(set_suyos | set_mios) if (set_suyos | set_mios) else 0.0

    return {
        'keywords_que_el_tiene_y_yo_no': faltantes,
        'keywords_que_yo_tengo_y_el_no': sobrantes,
        'keywords_en_comun':             comunes,
        'faltantes_que_la_gente_busca':  faltantes_buscadas,
        'solapamiento':                  round(solapamiento, 2),
        'mi_largo':                      len(mi_titulo or ''),
        'su_largo':                      len(su_titulo or ''),
    }


# ── Diagnostico ──────────────────────────────────────────────────────────────

def diagnosticar(mio: dict, competidor: dict, *,
                 delta_visitas_pct: float | None = None,
                 delta_conversion_pct: float | None = None,
                 delta_posicion: int | None = None,
                 delta_demanda_pct: float | None = None,
                 keywords_buscadas: list[str] | None = None) -> dict:
    """Por que este competidor te esta ganando.

    `mio` y `competidor`: dicts con titulo, precio, y opcionalmente costo,
    fee_rate, fotos, free_shipping, cuotas_max, reputacion, rating, ventas.
    Los deltas vienen de la serie de Cerebro (negativos = caida).

    Nunca inventa una causa: si la evidencia no alcanza, lo dice.
    """
    ev: list[str] = []
    causas: list[dict] = []

    mi_precio = _f(mio.get('precio'))
    su_precio = _f(competidor.get('precio'))
    brecha = ((mi_precio - su_precio) / mi_precio) if mi_precio > 0 and su_precio > 0 else 0.0

    titulos = comparar_titulos(mio.get('titulo', ''), competidor.get('titulo', ''),
                               keywords_buscadas)

    # ── 0. ¿Es el mercado y no el competidor? ────────────────────────────────
    if (delta_demanda_pct is not None and delta_visitas_pct is not None
            and delta_visitas_pct < 0 and delta_demanda_pct < 0
            and abs(delta_demanda_pct) >= abs(delta_visitas_pct) * 0.7):
        return {
            'causa_principal': 'mercado',
            'confianza': 'alta',
            'resumen': (f'las visitas cayeron {abs(delta_visitas_pct):.0f}% pero la demanda '
                        f'del rubro cayo {abs(delta_demanda_pct):.0f}%: te esta yendo como '
                        f'al mercado, no te esta ganando nadie'),
            'evidencia': [f'demanda del rubro {delta_demanda_pct:+.0f}%',
                          f'tus visitas organicas {delta_visitas_pct:+.0f}%'],
            'acciones': [{'tipo': 'esperar',
                          'detalle': 'no tocar precio ni ficha: el problema no es tuyo',
                          'costo_margen': 0}],
            'comparacion_titulos': titulos,
        }

    # ── 1. ¿Me encuentran? (posicionamiento) ─────────────────────────────────
    pierde_visibilidad = (
        (delta_visitas_pct is not None and delta_visitas_pct <= -CAIDA_VISITAS_SIGNIFICATIVA)
        or (delta_posicion is not None and delta_posicion >= CAIDA_POSICION_SIGNIFICATIVA)
    )
    if pierde_visibilidad:
        if delta_visitas_pct is not None:
            ev.append(f'visitas organicas {delta_visitas_pct:+.0f}%')
        if delta_posicion is not None:
            ev.append(f'bajaste {delta_posicion} lugares en la busqueda')
        faltan = titulos['faltantes_que_la_gente_busca'] or titulos['keywords_que_el_tiene_y_yo_no']
        if faltan:
            ev.append('su titulo tiene: ' + ', '.join(faltan[:6]))
        causas.append({
            'causa': 'keywords',
            'peso': 2 if faltan else 1,
            'detalle': ('no te encuentran: perdiste visibilidad en la busqueda'
                        + (f' y su titulo usa {len(faltan)} terminos que el tuyo no'
                           if faltan else '')),
        })

    # ── 2. ¿Me eligen? (conversion) ──────────────────────────────────────────
    pierde_eleccion = (delta_conversion_pct is not None
                       and delta_conversion_pct <= -CAIDA_CONVERSION_SIGNIFICATIVA)
    if pierde_eleccion:
        ev.append(f'conversion {delta_conversion_pct:+.0f}% con el trafico parecido')

        if brecha >= BRECHA_PRECIO_RELEVANTE:
            ev.append(f'esta {brecha * 100:.0f}% mas barato '
                      f'(${su_precio:,.0f} contra tus ${mi_precio:,.0f})'.replace(',', '.'))
            causas.append({
                'causa': 'precio',
                'peso': 3 if brecha >= BRECHA_PRECIO_FUERTE else 2,
                'detalle': f'te encuentran pero eligen el mas barato: esta {brecha * 100:.0f}% abajo',
            })

        ventajas = _ventajas_del_competidor(mio, competidor)
        if ventajas:
            ev.extend(ventajas)
            causas.append({
                'causa': 'condiciones',
                'peso': 2 if brecha < BRECHA_PRECIO_RELEVANTE else 1,
                'detalle': 'te gana por condiciones, no por precio: ' + '; '.join(ventajas),
            })

    # ── 3. Sin senal suficiente ──────────────────────────────────────────────
    if not causas:
        ventajas = _ventajas_del_competidor(mio, competidor)
        if brecha >= BRECHA_PRECIO_FUERTE:
            causas.append({'causa': 'precio', 'peso': 1,
                           'detalle': f'esta {brecha * 100:.0f}% mas barato, aunque todavia '
                                      f'no se nota en tus numeros'})
            ev.append(f'brecha de precio del {brecha * 100:.0f}%')
        elif ventajas:
            causas.append({'causa': 'condiciones', 'peso': 1,
                           'detalle': 'tiene ventajas que vos no: ' + '; '.join(ventajas)})
            ev.extend(ventajas)
        else:
            return {
                'causa_principal': 'sin_causa_clara',
                'confianza': 'baja',
                'resumen': 'no hay evidencia suficiente para decir que te esta ganando '
                           'ni por que. Mejor no tocar nada todavia',
                'evidencia': ev,
                'acciones': [],
                'comparacion_titulos': titulos,
            }

    causas.sort(key=lambda c: c['peso'], reverse=True)
    principal = causas[0]
    confianza = 'alta' if principal['peso'] >= 3 else ('media' if principal['peso'] == 2 else 'baja')

    return {
        'causa_principal':      principal['causa'],
        'causas_secundarias':   [c['causa'] for c in causas[1:]],
        'confianza':            confianza,
        'resumen':              principal['detalle'],
        'evidencia':            ev,
        'brecha_precio_pct':    round(brecha * 100, 1),
        'comparacion_titulos':  titulos,
        'acciones':             recomendar_acciones(principal['causa'], mio, competidor,
                                                    titulos, brecha),
    }


def _ventajas_del_competidor(mio: dict, comp: dict) -> list[str]:
    """Lo que el tiene y vos no, sin contar el precio."""
    v = []
    if comp.get('free_shipping') and not mio.get('free_shipping'):
        v.append('tiene envio gratis y vos no')
    if comp.get('fulfillment') and not mio.get('fulfillment'):
        v.append('esta en Full (llega mas rapido)')
    if _f(comp.get('cuotas_max')) > _f(mio.get('cuotas_max')):
        v.append(f'ofrece {int(_f(comp.get("cuotas_max")))} cuotas contra tus '
                 f'{int(_f(mio.get("cuotas_max")))}')
    if _f(comp.get('rating')) - _f(mio.get('rating')) >= 0.4 and _f(comp.get('rating')) > 0:
        v.append(f'tiene mejor puntaje ({comp.get("rating")} contra {mio.get("rating")})')
    fotos_m, fotos_c = _f(mio.get('fotos')), _f(comp.get('fotos'))
    if fotos_c - fotos_m >= 3:
        v.append(f'tiene {int(fotos_c)} fotos y vos {int(fotos_m)}')
    if _f(comp.get('ventas')) > _f(mio.get('ventas')) * 2 and _f(mio.get('ventas')) > 0:
        v.append('vendio bastante mas, y eso lo posiciona mejor')
    return v


# ── Acciones ─────────────────────────────────────────────────────────────────

def recomendar_acciones(causa: str, mio: dict, comp: dict,
                        titulos: dict, brecha: float) -> list[dict]:
    """Que hacer, ordenado por lo que cuesta en margen.

    La regla: primero lo que no cuesta margen (keywords, fotos, ficha), despues
    lo que cuesta poco (cuotas, condiciones), y el precio al final — es publico,
    lo ven los repricers de la competencia y cuesta revertirlo.
    """
    acciones: list[dict] = []

    if causa == 'keywords':
        faltan = titulos['faltantes_que_la_gente_busca'] or titulos['keywords_que_el_tiene_y_yo_no']
        if faltan:
            acciones.append({
                'tipo': 'ficha',
                'detalle': ('sumar al titulo o a la ficha los terminos que el usa y vos no: '
                            + ', '.join(faltan[:6])),
                'costo_margen': 0,
                'nota': 'si la publicacion ya vendio, ML no deja cambiar el titulo: '
                        'va a la descripcion y a la ficha, y al titulo de las nuevas',
            })
        acciones.append({
            'tipo': 'ficha',
            'detalle': 'completar los atributos vacios de la ficha: mejoran el match '
                       'de busqueda y los filtros, y no cuestan un peso',
            'costo_margen': 0,
        })

    elif causa == 'condiciones':
        for v in _ventajas_del_competidor(mio, comp):
            if 'envio gratis' in v:
                acciones.append({'tipo': 'envio', 'detalle': 'evaluar envio gratis',
                                 'costo_margen': 'medio'})
            elif 'Full' in v:
                acciones.append({'tipo': 'full', 'detalle': 'evaluar mandar stock a Full',
                                 'costo_margen': 'medio'})
            elif 'cuotas' in v:
                acciones.append({'tipo': 'cuotas',
                                 'detalle': 'igualar las cuotas sin interes que ofrece',
                                 'costo_margen': 'bajo'})
            elif 'fotos' in v:
                acciones.append({'tipo': 'fotos',
                                 'detalle': 'sumar fotos: es lo mas barato de igualar',
                                 'costo_margen': 0})
            elif 'puntaje' in v:
                acciones.append({'tipo': 'reputacion',
                                 'detalle': 'trabajar reviews y postventa; no se arregla '
                                            'bajando el precio',
                                 'costo_margen': 0})

    elif causa == 'precio':
        # Antes de bajar el precio: ¿alcanza con reducir cuotas?
        alternativa = precio_motor.analizar_reduccion_cuotas(
            _f(mio.get('precio')), _f(mio.get('ventas')), mio.get('cuotas_breakdown'))
        recomendables = [x for x in alternativa if x['recomendado']]
        if recomendables:
            acciones.append({
                'tipo': 'cuotas',
                'detalle': recomendables[0]['resumen'],
                'costo_margen': 'negativo (gana margen)',
                'nota': 'no toca el precio de lista, no lo ven los repricers ajenos',
            })

        objetivo = _f(comp.get('precio')) * 1.0
        ev = precio_motor.evaluar_cambio(
            _f(mio.get('precio')), objetivo, mio.get('costo'), mio.get('fee_rate'),
            unidades_30d=_f(mio.get('ventas')),
            cuotas_breakdown=mio.get('cuotas_breakdown'))
        acciones.append({
            'tipo': 'precio',
            'detalle': (f'igualar su precio (${objetivo:,.0f})'.replace(',', '.')
                        + (f' — margen {ev.get("margen_actual_pct")}% → '
                           f'{ev.get("margen_nuevo_pct")}%' if ev.get('margen_nuevo_pct') else '')),
            'viable': ev.get('viable'),
            'motivos': ev.get('motivos'),
            'compensacion': ev.get('resumen_compensacion'),
            'costo_margen': 'alto',
            'nota': 'el precio es publico y cuesta revertirlo: dejarlo para el final',
        })
        if not ev.get('viable'):
            acciones.append({
                'tipo': 'esperar',
                'detalle': 'no se puede igualar sin romper el piso de margen: conviene '
                           'competir por ficha, fotos y condiciones',
                'costo_margen': 0,
            })

    return acciones


# ── Registro para aprender ───────────────────────────────────────────────────

def registrar_para_aprender(alias: str, item_id: str, diagnostico: dict,
                            accion_elegida: dict) -> dict:
    """Deja la correccion registrada en Cerebro con el diagnostico como hipotesis.

    Asi, a los 7 y 14 dias, no se evalua solo "funciono el cambio" sino
    "acerto el diagnostico". Con los casos acumulados el sistema aprende que
    causa suele ser la correcta en cada rubro, que es lo que le permite dejar de
    proponer respuestas que en ese producto nunca sirvieron.
    """
    from modules import cerebro
    return cerebro.registrar_accion(
        alias,
        tipo=accion_elegida.get('tipo', 'ficha'),
        item_id=item_id,
        origen=cerebro.ORIGEN_PROPUESTO,
        hipotesis=(f'diagnostico: {diagnostico.get("causa_principal")} '
                   f'(confianza {diagnostico.get("confianza")}) — '
                   f'{diagnostico.get("resumen", "")[:200]}'),
        estado_previo=cerebro.capturar_estado_previo(alias, item_id),
        detalle={
            'origen_diagnostico': True,
            'causa':      diagnostico.get('causa_principal'),
            'confianza':  diagnostico.get('confianza'),
            'evidencia':  diagnostico.get('evidencia'),
            'accion':     accion_elegida,
        },
        ejecutado_por='competencia_diagnostico')
