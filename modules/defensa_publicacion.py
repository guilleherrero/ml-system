"""
Defensa de la publicacion — que tan dificil es desplazarte, y como blindarte.

Cerebro detecta y aprende; este modulo responde la otra mitad: que hacer para
que no te ganen. La idea de fondo es que una publicacion no se defiende con una
sola cosa sino con varias puertas, y el competidor entra por la que dejaste
abierta.

**Autosuggest es el centro de todo.** Es la unica fuente que quedo con demanda
real: son las frases que la gente efectivamente escribe en ML, ordenadas por
popularidad. No es una inferencia ni una opinion sobre keywords, es el dato. Por
eso todo el analisis se hace contra el universo de busquedas reales del producto
y no contra lo que uno cree que la gente busca.

Seis dimensiones de defensa:

  1. COBERTURA   — por cuantas busquedas reales te encuentran (autosuggest)
  2. CONCENTRACION — si dependes de una sola keyword, sos fragil
  3. FICHA       — atributos completos: te mete en los filtros
  4. CONTENIDO   — fotos y video contra los competidores
  5. CONDICIONES — envio, cuotas, Full
  6. REPUTACION  — rating y reviews

De ahi sale un indice 0-100 y, mas importante, la lista de puertas abiertas
ordenadas por lo que cuesta cerrarlas.

Un caso real encontrado con esto: el titulo del Cortador Ender Pro dice
"Abriertas" en vez de "Abiertas". Un error de una letra que deja afuera la
busqueda "cortador de puntas abiertas", que es de las mas populares del rubro.
Por eso el modulo compara cada palabra del titulo contra el vocabulario real de
autosuggest y avisa cuando una se parece demasiado a una busqueda real sin
serlo.

Restriccion que ordena las recomendaciones: ML congela el titulo despues de la
primera venta. En una publicacion con ventas, lo que se puede tocar es la ficha
y la descripcion; el titulo corregido se aplica a las publicaciones nuevas y a
los clones.
"""

from __future__ import annotations

import re
import unicodedata

# Palabras que no aportan a la busqueda
_STOP = {
    'de', 'la', 'el', 'para', 'con', 'sin', 'por', 'y', 'a', 'en', 'un', 'una',
    'los', 'las', 'del', 'al', 'mas', 'nuevo', 'nueva', 'original', 'envio',
    'gratis', 'oferta', 'promo', 'super',
}

# Pesos de cada dimension en el indice de defensa
PESOS = {
    'cobertura':     30,
    'concentracion': 15,
    'ficha':         20,
    'contenido':     15,
    'condiciones':   10,
    'reputacion':    10,
}

# Cuantas fotos se consideran suficientes
FOTOS_OBJETIVO = 6


def _norm(texto: str) -> str:
    t = ''.join(c for c in unicodedata.normalize('NFKD', (texto or '').lower())
                if unicodedata.category(c) != 'Mn')
    return t


def _tokens(texto: str, con_stop: bool = False) -> set[str]:
    palabras = re.findall(r'[a-z0-9]+', _norm(texto))
    if con_stop:
        return set(palabras)
    return {p for p in palabras if p not in _STOP}


def _distancia(a: str, b: str) -> int:
    """Distancia de edicion, para detectar una palabra mal escrita."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ── Universo de busquedas reales ─────────────────────────────────────────────

def universo_keywords(sugerencias_por_semilla: dict[str, list[str]]) -> list[dict]:
    """Arma el universo de busquedas reales a partir de varias consultas a autosuggest.

    Se pasan varias semillas porque autosuggest devuelve pocas por consulta y
    cada familia de terminos ("cortador", "florecidas", "maquina corta") abre
    una rama distinta. La posicion en que ML devuelve cada frase es su orden de
    popularidad, asi que sirve para priorizar.
    """
    universo: dict[str, dict] = {}
    for semilla, sugerencias in (sugerencias_por_semilla or {}).items():
        for pos, frase in enumerate(sugerencias or []):
            clave = _norm(frase).strip()
            if not clave:
                continue
            # Mejor posicion en cualquiera de las semillas = mas popular
            actual = universo.get(clave)
            if actual is None or pos < actual['posicion']:
                universo[clave] = {
                    'frase': frase, 'posicion': pos, 'semilla': semilla,
                    'tokens': _tokens(frase),
                }
    ordenado = sorted(universo.values(), key=lambda x: x['posicion'])
    for i, u in enumerate(ordenado):
        # Peso decreciente por popularidad; la primera vale el doble que la quinta
        u['peso'] = round(1.0 / (1 + u['posicion'] * 0.25), 3)
    return ordenado


def vocabulario(universo: list[dict]) -> set[str]:
    """Todas las palabras que la gente realmente usa al buscar este producto."""
    vocab: set[str] = set()
    for u in universo:
        vocab |= u['tokens']
    return vocab


# ── Cobertura ────────────────────────────────────────────────────────────────

def analizar_cobertura(titulo: str, universo: list[dict],
                       ficha_texto: str = '', descripcion: str = '') -> dict:
    """Por cuantas busquedas reales te encuentran, y cuales estas dejando afuera."""
    t_titulo = _tokens(titulo)
    t_resto  = _tokens(f'{ficha_texto} {descripcion}')
    t_todo   = t_titulo | t_resto

    cubiertas, parciales, perdidas = [], [], []
    peso_total = sum(u['peso'] for u in universo) or 1.0
    peso_cubierto = 0.0

    for u in universo:
        faltan_titulo = u['tokens'] - t_titulo
        faltan_todo   = u['tokens'] - t_todo
        fila = {'frase': u['frase'], 'posicion': u['posicion'], 'peso': u['peso'],
                'faltan': sorted(faltan_todo)}
        if not faltan_titulo:
            cubiertas.append(fila)
            peso_cubierto += u['peso']
        elif not faltan_todo:
            # Esta en la ficha o la descripcion: cuenta menos que en el titulo
            parciales.append(fila)
            peso_cubierto += u['peso'] * 0.4
        else:
            perdidas.append(fila)

    perdidas.sort(key=lambda f: -f['peso'])
    return {
        'total_busquedas':   len(universo),
        'cubiertas_titulo':  len(cubiertas),
        'cubiertas_parcial': len(parciales),
        'perdidas':          len(perdidas),
        'score':             round(peso_cubierto / peso_total * 100, 1),
        'detalle_cubiertas': cubiertas,
        'detalle_parciales': parciales,
        'detalle_perdidas':  perdidas[:10],
        'palabras_que_faltan': _palabras_mas_valiosas(perdidas),
    }


def _palabras_mas_valiosas(perdidas: list[dict]) -> list[dict]:
    """Que palabra sumar rinde mas: la que desbloquea mas busquedas perdidas."""
    puntaje: dict[str, float] = {}
    apariciones: dict[str, int] = {}
    for f in perdidas:
        for p in f['faltan']:
            puntaje[p] = puntaje.get(p, 0) + f['peso']
            apariciones[p] = apariciones.get(p, 0) + 1
    orden = sorted(puntaje.items(), key=lambda kv: -kv[1])
    return [{'palabra': p, 'desbloquea_busquedas': apariciones[p], 'valor': round(v, 3)}
            for p, v in orden[:8]]


def detectar_errores_de_escritura(titulo: str, universo: list[dict]) -> list[dict]:
    """Palabras del titulo que se parecen demasiado a una busqueda real sin serlo.

    Un error de una letra puede costar una keyword entera: el Cortador Ender Pro
    dice "Abriertas" donde la gente busca "abiertas".
    """
    vocab = vocabulario(universo)
    hallazgos = []
    for palabra in _tokens(titulo):
        if palabra in vocab or len(palabra) < 5:
            continue
        for real in vocab:
            if len(real) < 5:
                continue
            d = _distancia(palabra, real)
            if d <= 2 and d > 0:
                afectadas = [u['frase'] for u in universo if real in u['tokens']]
                hallazgos.append({
                    'escrito':     palabra,
                    'deberia_ser': real,
                    'distancia':   d,
                    'busquedas_que_se_pierden': afectadas[:5],
                    'cuantas':     len(afectadas),
                })
                break
    hallazgos.sort(key=lambda h: -h['cuantas'])
    return hallazgos


# ── Indice de defensa ────────────────────────────────────────────────────────

def indice_defensa(item: dict, cobertura: dict, *,
                   competidores: list[dict] | None = None,
                   atributos_totales: int = 0,
                   atributos_completos: int = 0,
                   concentracion_visitas: float | None = None) -> dict:
    """Que tan dificil es desplazarte, dimension por dimension.

    `concentracion_visitas`: fraccion de las visitas que llega por una sola
    keyword. Alta = fragil, porque el dia que perdes esa posicion perdes todo.
    """
    comps = competidores or []
    puntos: dict[str, float] = {}
    debilidades: list[dict] = []

    # 1. Cobertura de busquedas reales
    puntos['cobertura'] = cobertura.get('score', 0) / 100 * PESOS['cobertura']
    if cobertura.get('score', 0) < 60:
        debilidades.append({
            'dimension': 'cobertura',
            'gravedad': 'alta' if cobertura['score'] < 35 else 'media',
            'detalle': (f'te encuentran por {cobertura["cubiertas_titulo"]} de '
                        f'{cobertura["total_busquedas"]} busquedas reales del rubro'),
            'costo_arreglar': 0,
        })

    # 2. Concentracion
    if concentracion_visitas is None:
        puntos['concentracion'] = PESOS['concentracion'] * 0.5
    else:
        puntos['concentracion'] = (1 - min(concentracion_visitas, 1.0)) * PESOS['concentracion']
        if concentracion_visitas >= 0.7:
            debilidades.append({
                'dimension': 'concentracion',
                'gravedad': 'alta',
                'detalle': (f'el {concentracion_visitas * 100:.0f}% de tus visitas llega por '
                            f'una sola busqueda: si perdes esa posicion, perdes casi todo'),
                'costo_arreglar': 0,
            })

    # 3. Ficha
    if atributos_totales > 0:
        ratio = atributos_completos / atributos_totales
        puntos['ficha'] = ratio * PESOS['ficha']
        if ratio < 0.8:
            debilidades.append({
                'dimension': 'ficha',
                'gravedad': 'media',
                'detalle': (f'{atributos_totales - atributos_completos} atributos vacios: '
                            f'quedas afuera de los filtros que usa el comprador'),
                'costo_arreglar': 0,
            })
    else:
        puntos['ficha'] = PESOS['ficha'] * 0.5

    # 4. Contenido
    fotos = float(item.get('fotos') or 0)
    fotos_comp = [float(c.get('fotos') or 0) for c in comps if c.get('fotos')]
    objetivo = max(FOTOS_OBJETIVO, max(fotos_comp) if fotos_comp else 0)
    puntos['contenido'] = min(fotos / objetivo, 1.0) * PESOS['contenido'] if objetivo else PESOS['contenido']
    if fotos < objetivo:
        debilidades.append({
            'dimension': 'contenido',
            'gravedad': 'media' if fotos >= FOTOS_OBJETIVO else 'alta',
            'detalle': f'tenes {int(fotos)} fotos y el mejor competidor {int(objetivo)}',
            'costo_arreglar': 0,
        })

    # 5. Condiciones
    cond = 0.0
    faltantes = []
    if item.get('free_shipping'):
        cond += 0.4
    else:
        faltantes.append('envio gratis')
    if item.get('fulfillment'):
        cond += 0.3
    elif any(c.get('fulfillment') for c in comps):
        faltantes.append('Full (los competidores lo tienen)')
    else:
        cond += 0.15
    max_cuotas_comp = max([float(c.get('cuotas_max') or 0) for c in comps], default=0)
    if float(item.get('cuotas_max') or 0) >= max_cuotas_comp:
        cond += 0.3
    else:
        faltantes.append(f'cuotas ({int(max_cuotas_comp)} tienen los competidores)')
    puntos['condiciones'] = min(cond, 1.0) * PESOS['condiciones']
    if faltantes:
        debilidades.append({
            'dimension': 'condiciones', 'gravedad': 'media',
            'detalle': 'te falta: ' + ', '.join(faltantes),
            'costo_arreglar': 'medio',
        })

    # 6. Reputacion
    rating = float(item.get('rating') or 0)
    puntos['reputacion'] = (min(rating / 4.8, 1.0) * PESOS['reputacion']) if rating else PESOS['reputacion'] * 0.5
    if rating and rating < 4.3:
        debilidades.append({
            'dimension': 'reputacion', 'gravedad': 'alta',
            'detalle': f'puntaje {rating}: no se arregla con precio, se arregla con producto y postventa',
            'costo_arreglar': 0,
        })

    total = round(sum(puntos.values()), 1)
    orden = {'alta': 0, 'media': 1, 'baja': 2}
    debilidades.sort(key=lambda d: (orden.get(d['gravedad'], 3),
                                    0 if d['costo_arreglar'] == 0 else 1))
    return {
        'indice': total,
        'nivel': ('solida' if total >= 75 else
                  'aceptable' if total >= 55 else
                  'expuesta' if total >= 35 else 'muy expuesta'),
        'por_dimension': {k: round(v, 1) for k, v in puntos.items()},
        'maximos': PESOS,
        'puertas_abiertas': debilidades,
    }


# ── Plan de defensa ──────────────────────────────────────────────────────────

def plan_de_defensa(item: dict, cobertura: dict, defensa: dict,
                    errores_escritura: list[dict] | None = None,
                    tiene_ventas: bool = True) -> list[dict]:
    """Que hacer, en orden, para que sea dificil desplazarte.

    Primero lo que no cuesta plata y mas rinde. El titulo se trata aparte:
    si la publicacion ya vendio, ML no lo deja cambiar.
    """
    acciones: list[dict] = []

    for err in (errores_escritura or []):
        acciones.append({
            'prioridad': 1,
            'tipo': 'titulo' if not tiene_ventas else 'descripcion',
            'detalle': (f'dice "{err["escrito"]}" donde la gente busca "{err["deberia_ser"]}": '
                        f'estas perdiendo {err["cuantas"]} busquedas reales'),
            'busquedas_afectadas': err['busquedas_que_se_pierden'],
            'costo': 0,
            'nota': ('el titulo ya esta congelado por ventas: corregirlo en la descripcion '
                     'y en la ficha, y usar la forma correcta en las publicaciones nuevas'
                     if tiene_ventas else 'todavia se puede corregir el titulo'),
        })

    faltan = cobertura.get('palabras_que_faltan') or []
    if faltan:
        top = faltan[:4]
        acciones.append({
            'prioridad': 2,
            'tipo': 'descripcion' if tiene_ventas else 'titulo',
            'detalle': ('sumar los terminos que mas busquedas desbloquean: '
                        + ', '.join(f'{p["palabra"]} (+{p["desbloquea_busquedas"]} busquedas)'
                                    for p in top)),
            'costo': 0,
            'nota': ('la descripcion es la capa de cobertura de las keywords que no '
                     'entran en el titulo' if tiene_ventas else ''),
        })

    for d in defensa.get('puertas_abiertas', []):
        if d['dimension'] in ('cobertura',):
            continue
        acciones.append({
            'prioridad': 3 if d['costo_arreglar'] == 0 else 4,
            'tipo': d['dimension'],
            'detalle': d['detalle'],
            'costo': d['costo_arreglar'],
        })

    acciones.sort(key=lambda a: a['prioridad'])
    return acciones


def registrar_para_aprender(alias: str, item_id: str, defensa: dict, accion: dict) -> dict:
    """Cada refuerzo se evalua despues: asi se aprende que defensa protege de verdad.

    Es el circulo virtuoso: no se trata de suponer que completar la ficha sirve,
    sino de saber, con casos propios, si sirvio en este rubro.
    """
    from modules import cerebro
    return cerebro.registrar_accion(
        alias,
        tipo=('ficha' if accion.get('tipo') in ('ficha', 'titulo', 'contenido')
              else accion.get('tipo', 'ficha')),
        item_id=item_id,
        origen=cerebro.ORIGEN_PROPUESTO,
        hipotesis=(f'defensa {defensa.get("nivel")} (indice {defensa.get("indice")}): '
                   f'{accion.get("detalle", "")[:180]}'),
        estado_previo=cerebro.capturar_estado_previo(alias, item_id),
        detalle={'origen_defensa': True,
                 'indice_defensa': defensa.get('indice'),
                 'dimension': accion.get('tipo'),
                 'accion': accion},
        ejecutado_por='defensa_publicacion')
