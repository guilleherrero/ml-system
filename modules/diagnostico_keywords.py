"""
Barrido diario de keywords y ficha contra autosuggest.

Cerebro tenia un problema de calendario: medir si un cambio sirvio necesita 14
dias de historia, pero *diagnosticar que esta mal hoy* no necesita ninguno. Este
modulo es la mitad que faltaba — la que sirve desde el primer dia, porque no
mira series de tiempo sino la foto actual de cada publicacion contra la demanda
real del producto.

La demanda real es autosuggest: las frases que la gente efectivamente escribe en
ML, ordenadas por popularidad. No es una inferencia sobre keywords, es el dato.
`defensa_publicacion` ya sabe cruzar un titulo contra ese universo; lo que no
existia era recorrer las ~70 publicaciones, hacerlo en todas, y decir en cuales
conviene meterse.

Tres hallazgos, en orden de lo que cuesta arreglarlos:

  1. ERROR DE ESCRITURA — una letra que apaga una busqueda entera. El titulo del
     Cortador Ender Pro dice "Abriertas" donde la gente busca "abiertas".
     Es el mas barato de arreglar y el mas caro de dejar.
  2. FICHA INCOMPLETA — atributos vacios que ML usa para filtrar. No cuesta
     margen, no lo ve el comprador, no lo ve la competencia.
  3. KEYWORDS FUERA DEL TITULO — busquedas reales que no cubris con ninguna
     palabra. Se corrigen en descripcion y ficha; el titulo se congela despues
     de la primera venta.

Sobre las plata: solo se valua lo que se puede valuar. Si falta el costo, el
hallazgo sale igual pero sin numero, porque un impacto inventado es peor que no
tener impacto (criterio 1: no mentir). Y las publicaciones con poco trafico no
se valuan nunca: extrapolar desde 12 visitas es ruido con formato de plata.
"""

from __future__ import annotations

import logging
import os
import time

import requests

from core.db_storage import db_load, db_save
from modules import defensa_publicacion as dp

_logger = logging.getLogger(__name__)

_AS_URL = 'https://http2.mlstatic.com/resources/sites/MLA/autosuggest'
_AS_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    'Accept': 'application/json',
    'Referer': 'https://www.mercadolibre.com.ar/',
}

# El cache de autosuggest dura un dia: las busquedas de un producto no cambian
# de un rato a otro, y sin cache un barrido son ~200 llamadas cada vez.
CACHE_TTL_HORAS = 24

# Visitas minimas en 30 dias para animarse a poner un numero en pesos. Por
# debajo de esto la conversion propia es ruido y cualquier extrapolacion miente.
VISITAS_MINIMAS_PARA_VALUAR = 80

# Cuanto de la demanda no cubierta se convierte de verdad en visitas si se
# corrige. Cubrir una busqueda no es rankear en ella: entrar al universo de
# resultados es condicion necesaria, no suficiente. Mismo espiritu que el
# FACTOR_REALISMO de precio_motor.
FACTOR_REALISMO = 0.35

# Tope de sensatez: por mas que la cobertura sea pesima, corregir keywords no
# duplica el trafico de un mes para otro. Sin este tope la extrapolacion lineal
# se dispara justo donde menos datos hay — una publicacion que cubre el 20% de
# la demanda daria "4x visitas", que es una promesa que el sistema no puede
# cumplir. Entre prometer de mas y prometer de menos, de menos: el tope dice
# que corregir keywords suma hasta la mitad del trafico actual, no mas.
TOPE_VISITAS_RECUPERABLES = 0.5

# Por debajo de esto no vale molestar (criterio 6: saber donde NO meterse).
COBERTURA_SANA_PCT = 75.0

# Tope de llamadas a autosuggest por corrida, para que el barrido no se coma el
# job. Lo que no entra queda para la corrida siguiente: el cache hace que cada
# corrida avance sobre publicaciones distintas.
MAX_CONSULTAS_POR_CORRIDA = 240

_PAUSA_ENTRE_CONSULTAS = 0.12


# ── Autosuggest, con cache ───────────────────────────────────────────────────

def _cache_path(alias: str) -> str:
    safe = (alias or '').replace(' ', '_').replace('/', '-')
    return os.path.join('data', f'autosuggest_cache_{safe}.json')


def _cache_vigente(entry: dict) -> bool:
    try:
        return (time.time() - float(entry.get('ts', 0))) < CACHE_TTL_HORAS * 3600
    except Exception:
        return False


def consultar_autosuggest(query: str, limit: int = 10) -> list[str]:
    """Las frases que la gente escribe de verdad, en orden de popularidad."""
    if not (query or '').strip():
        return []
    try:
        r = requests.get(_AS_URL,
                         params={'q': query, 'limit': limit, 'lang': 'es_AR'},
                         headers=_AS_HEADERS, timeout=6)
        if r.ok:
            return [s['q'] for s in r.json().get('suggested_queries', []) if s.get('q')]
        _logger.warning('[keywords] autosuggest HTTP %s para %r', r.status_code, query[:40])
    except requests.RequestException as e:
        _logger.warning('[keywords] autosuggest fallo para %r: %s', query[:40], e)
    return []


def semillas_de(titulo: str) -> list[str]:
    """Tres consultas cortas por publicacion, cada una abre una rama distinta.

    El titulo entero como consulta devuelve sugerencias de esa frase larga y no
    las busquedas del nicho: la gente busca con dos o tres palabras.
    """
    palabras = [p for p in dp._tokens(titulo, con_stop=False)
                if len(p) > 2 and not p.isdigit()]
    # _tokens devuelve un set; hay que respetar el orden del titulo
    orden = []
    vistos = set()
    for p in dp._norm(titulo).split():
        p = ''.join(c for c in p if c.isalnum())
        if p in palabras and p not in vistos:
            orden.append(p)
            vistos.add(p)
    if not orden:
        return []
    sem = [' '.join(orden[:3]), ' '.join(orden[:2])]
    if len(orden) > 3:
        sem.append(' '.join(orden[-3:]))
    return [s for i, s in enumerate(sem) if s and s not in sem[:i]]


def universo_de_item(titulo: str, alias: str, presupuesto: list[int]) -> list[dict]:
    """El universo de busquedas reales del producto, cacheado por semilla.

    `presupuesto` es una lista de un elemento que se descuenta en cada consulta
    real: asi el barrido se corta solo y sigue en la corrida siguiente.
    """
    cache = db_load(_cache_path(alias)) or {}
    if not isinstance(cache, dict):
        cache = {}

    sugerencias: dict[str, list[str]] = {}
    toco_red = False
    for semilla in semillas_de(titulo):
        entry = cache.get(semilla)
        if entry and _cache_vigente(entry):
            sugerencias[semilla] = entry.get('frases') or []
            continue
        if presupuesto[0] <= 0:
            continue
        presupuesto[0] -= 1
        toco_red = True
        frases = consultar_autosuggest(semilla)
        cache[semilla] = {'ts': time.time(), 'frases': frases}
        sugerencias[semilla] = frases
        time.sleep(_PAUSA_ENTRE_CONSULTAS)

    if toco_red:
        try:
            db_save(_cache_path(alias), cache)
        except Exception as e:
            _logger.warning('[keywords] no pude guardar el cache: %s', e)

    return dp.universo_keywords(sugerencias)


# ── Diagnostico por publicacion ──────────────────────────────────────────────

def _visitas_recuperables(cobertura: dict, visitas_30d: int) -> float:
    """Cuantas visitas mas traeria cubrir la demanda que hoy queda afuera.

    La regla: si con el X% del peso de la demanda cubierto conseguis V visitas,
    el (100-X)% que falta vale proporcionalmente lo mismo — descontado por el
    factor de realismo, porque cubrir una busqueda no es ganarla.
    """
    score = float(cobertura.get('score') or 0)
    if score <= 0 or score >= 100 or visitas_30d <= 0:
        return 0.0
    proporcion_faltante = (100.0 - score) / score
    crudo = visitas_30d * proporcion_faltante * FACTOR_REALISMO
    return round(min(crudo, visitas_30d * TOPE_VISITAS_RECUPERABLES), 1)


def diagnosticar_item(item: dict, universo: list[dict], *,
                      ficha_texto: str = '', descripcion: str = '',
                      atributos_vacios: list[str] | None = None) -> dict:
    """Que le falta a esta publicacion para que la encuentren, y cuanto vale.

    `item` necesita: id, titulo, y opcionalmente visitas_30d, ventas_30d,
    precio, margen_pct para poder valuar.
    """
    titulo = item.get('titulo') or item.get('title') or ''
    if not universo:
        return {'item_id': item.get('id', ''), 'titulo': titulo,
                'sin_datos': True,
                'motivo': 'autosuggest no devolvio busquedas para este producto'}

    cobertura = dp.analizar_cobertura(titulo, universo,
                                      ficha_texto=ficha_texto,
                                      descripcion=descripcion)
    errores = dp.detectar_errores_de_escritura(titulo, universo)

    visitas = int(item.get('visitas_30d') or 0)
    ventas  = int(item.get('ventas_30d') or 0)
    precio  = float(item.get('precio') or 0)
    margen_pct = item.get('margen_pct')

    visitas_recuperables = _visitas_recuperables(cobertura, visitas)
    topeado = visitas_recuperables >= visitas * TOPE_VISITAS_RECUPERABLES and visitas > 0

    # Valuar solo cuando se puede. Un numero inventado es peor que ninguno.
    impacto = None
    no_valuado_porque = None
    if visitas < VISITAS_MINIMAS_PARA_VALUAR:
        no_valuado_porque = (f'{visitas} visitas en 30 dias — muy poco trafico como '
                             f'para extrapolar sin inventar')
    elif margen_pct is None or float(margen_pct) <= 0:
        no_valuado_porque = 'falta el costo del producto, sin eso el margen es una suposicion'
    elif ventas <= 0:
        no_valuado_porque = ('todavia no vendio: no hay conversion propia con la cual '
                             'traducir visitas en plata')
    else:
        conversion = ventas / visitas
        margen_unitario = precio * float(margen_pct)
        impacto = round(visitas_recuperables * conversion * margen_unitario, 2)

    # El titulo se congela despues de la primera venta: lo que se puede tocar
    # cambia segun eso, y las recomendaciones tienen que respetarlo.
    titulo_editable = ventas <= 0

    return {
        'item_id':   item.get('id', ''),
        'titulo':    titulo,
        'sin_datos': False,
        'cobertura_pct':        cobertura['score'],
        'busquedas_totales':    cobertura['total_busquedas'],
        'busquedas_cubiertas':  cobertura['cubiertas_titulo'],
        'busquedas_parciales':  cobertura['cubiertas_parcial'],
        'busquedas_perdidas':   cobertura['perdidas'],
        'palabras_que_faltan':  cobertura['palabras_que_faltan'],
        'detalle_perdidas':     cobertura['detalle_perdidas'],
        'errores_escritura':    errores,
        'atributos_vacios':     atributos_vacios or [],
        'visitas_30d':          visitas,
        'ventas_30d':           ventas,
        'visitas_recuperables': visitas_recuperables,
        'topeado_por_sensatez': topeado,
        'impacto_mensual_ars':  impacto,
        'no_valuado_porque':    no_valuado_porque,
        'titulo_editable':      titulo_editable,
        'formula': ('visitas actuales x (peso de demanda sin cubrir / peso cubierto) '
                    f'x {FACTOR_REALISMO} de realismo, topeado en '
                    f'{TOPE_VISITAS_RECUPERABLES:g}x las visitas actuales, '
                    'x conversion propia x margen unitario'),
        'acciones': _acciones(cobertura, errores, atributos_vacios or [], titulo_editable),
    }


def _acciones(cobertura: dict, errores: list[dict],
              atributos_vacios: list[str], titulo_editable: bool) -> list[dict]:
    """Que hacer, ordenado por lo que cuesta hacerlo — lo barato primero."""
    acciones: list[dict] = []

    for e in errores:
        donde = 'el titulo' if titulo_editable else 'la descripcion y la ficha'
        nota = '' if titulo_editable else (
            ' El titulo esta congelado por tener ventas, asi que la palabra bien '
            'escrita va en la descripcion y la ficha, y el titulo corregido queda '
            'para publicaciones nuevas y clones.')
        acciones.append({
            'tipo': 'corregir_error_escritura',
            'costo': 'gratis',
            'aplicar_en': 'titulo' if titulo_editable else 'descripcion',
            'texto': (f'Dice "{e["escrito"]}" donde la gente busca "{e["deberia_ser"]}". '
                      f'Apaga {e["cuantas"]} busquedas reales. Corregir en {donde}.{nota}'),
            'detalle': e,
        })

    if atributos_vacios:
        acciones.append({
            'tipo': 'completar_ficha',
            'costo': 'gratis',
            'aplicar_en': 'ficha',
            'texto': (f'{len(atributos_vacios)} atributos vacios en la ficha. '
                      f'ML los usa para filtrar busquedas, asi que cada uno vacio '
                      f'te saca de un filtro: {", ".join(atributos_vacios[:6])}'),
            'detalle': {'atributos': atributos_vacios},
        })

    faltan = cobertura.get('palabras_que_faltan') or []
    if faltan and cobertura.get('score', 100) < COBERTURA_SANA_PCT:
        top = faltan[:3]
        listado = ', '.join(f'"{p["palabra"]}" (abre {p["desbloquea_busquedas"]} busquedas)'
                            for p in top)
        acciones.append({
            'tipo': 'sumar_keywords',
            'costo': 'gratis',
            'aplicar_en': 'titulo' if titulo_editable else 'descripcion',
            'texto': (f'Cubris el {cobertura["score"]}% de la demanda real. '
                      f'Las palabras que mas rinden sumar: {listado}'),
            'detalle': {'palabras': top,
                        'busquedas_perdidas': cobertura.get('detalle_perdidas', [])[:5]},
        })

    return acciones


# ── Barrido de todo el catalogo ──────────────────────────────────────────────

def barrer(alias: str, items: list[dict], *,
           ficha_por_item: dict | None = None,
           max_items: int | None = None) -> dict:
    """Diagnostica el catalogo entero y devuelve los hallazgos ordenados.

    Los items con mas visitas primero: si el presupuesto de consultas se agota,
    que se agote despues de haber mirado donde esta el resultado (criterio 6).
    """
    ficha_por_item = ficha_por_item or {}
    orden = sorted(items or [], key=lambda i: -int(i.get('visitas_30d') or 0))
    if max_items:
        orden = orden[:max_items]

    presupuesto = [MAX_CONSULTAS_POR_CORRIDA]
    hallazgos, sin_datos = [], []

    for it in orden:
        titulo = it.get('titulo') or it.get('title') or ''
        if not titulo:
            continue
        universo = universo_de_item(titulo, alias, presupuesto)
        f = ficha_por_item.get(it.get('id', '')) or {}
        d = diagnosticar_item(
            it, universo,
            ficha_texto=f.get('ficha_texto', ''),
            descripcion=f.get('descripcion', ''),
            atributos_vacios=f.get('atributos_vacios') or [])
        if d.get('sin_datos'):
            sin_datos.append(d)
        else:
            hallazgos.append(d)

    # Primero lo valuado por plata; despues lo no valuado por cuanta demanda deja
    # afuera. Nada de mezclar los dos ordenes en una sola cifra falsa.
    valuados    = [h for h in hallazgos if h.get('impacto_mensual_ars')]
    no_valuados = [h for h in hallazgos if not h.get('impacto_mensual_ars')]
    valuados.sort(key=lambda h: -h['impacto_mensual_ars'])
    no_valuados.sort(key=lambda h: h.get('cobertura_pct', 100))

    con_accion = [h for h in hallazgos if h.get('acciones')]
    return {
        'alias':               alias,
        'items_analizados':    len(hallazgos),
        'items_sin_datos':     len(sin_datos),
        'items_con_hallazgos': len(con_accion),
        'consultas_usadas':    MAX_CONSULTAS_POR_CORRIDA - presupuesto[0],
        'presupuesto_agotado': presupuesto[0] <= 0,
        'valuados':            valuados,
        'no_valuados':         no_valuados,
        'sin_datos':           sin_datos,
    }
