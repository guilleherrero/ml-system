"""
Grupos de producto — un analisis de competencia por PRODUCTO, no por publicacion.

El mismo producto suele estar publicado varias veces: negro y rojo, con y sin
Full, catalogo y tradicional. Contra todas compiten los mismos vendedores, asi
que cargar competidores publicacion por publicacion es hacer seis veces el mismo
trabajo y despues mirar seis analisis que dicen lo mismo.

Un grupo junta las publicaciones propias que son el mismo producto. Los
competidores se asocian al grupo, el analisis de competencia se hace una vez, y
la parte que sigue siendo individual —que le falta a ESTA publicacion, que
precio conviene en ESTA— se resuelve al momento del informe o de la
optimizacion, donde si tiene sentido separar.

Los grupos se proponen solos por parecido de titulo, ignorando los tokens de
variante (color y talle) que son justamente lo que distingue a los hermanos. La
propuesta no se aplica sola: el usuario confirma, porque dos productos pueden
tener titulos casi iguales y ser cosas distintas, y agrupar mal significa
comparar contra la competencia equivocada.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid

from modules import cerebro

_logger = logging.getLogger(__name__)

ARCHIVO = 'grupos_producto.json'

# Parecido minimo entre dos titulos, ya sin tokens de variante, para proponerlos
# como el mismo producto. Por debajo de esto se proponen separados: el costo de
# separar de mas es que el usuario una dos grupos con un clic; el de unir de mas
# es analizar contra la competencia de otro producto.
UMBRAL_AGRUPAR = 0.55

# Lo que distingue a un hermano de otro y por lo tanto NO define el producto
_VARIANTES = {
    'negro', 'blanco', 'rojo', 'azul', 'verde', 'gris', 'beige', 'rosa',
    'rosado', 'celeste', 'violeta', 'lila', 'marron', 'cafe', 'amarillo',
    'naranja', 'turquesa', 'fucsia', 'dorado', 'plateado', 'transparente',
    'xs', 's', 'm', 'l', 'xl', 'xxl', 'xxxl', 'chico', 'mediano', 'grande',
    'pequeno', 'unidad', 'unidades', 'pack', 'kit', 'set', 'combo',
}

_RUIDO = {
    'de', 'la', 'el', 'para', 'con', 'sin', 'por', 'y', 'a', 'en', 'un', 'una',
    'los', 'las', 'del', 'al', 'mas', 'nuevo', 'nueva', 'original', 'envio',
    'gratis', 'oferta', 'promo', 'super', 'premium', 'pro',
}


def _norm(t: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', (t or '').lower())
                   if unicodedata.category(c) != 'Mn')


def _tokens_producto(titulo: str) -> set[str]:
    """Las palabras que definen QUE producto es, sin las que definen cual variante."""
    palabras = re.findall(r'[a-z0-9]+', _norm(titulo))
    return {p for p in palabras
            if len(p) > 2 and p not in _RUIDO and p not in _VARIANTES
            and not p.isdigit()}


def parecido(a: str, b: str) -> float:
    ta, tb = _tokens_producto(a), _tokens_producto(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 3)


# ── Propuesta automatica ─────────────────────────────────────────────────────

def sugerir_grupos(items: list[dict]) -> list[dict]:
    """Agrupa las publicaciones propias que parecen el mismo producto.

    Union por transitividad: si A se parece a B y B a C, los tres van juntos,
    aunque A y C no se parezcan tanto entre si. Es lo correcto para variantes
    —"Cortador Negro" y "Cortador Rojo" pueden no tocarse directamente pero
    ambos tocan "Cortador Puntas Abiertas"—.
    """
    ids = [i.get('id', '') for i in items if i.get('id')]
    por_id = {i['id']: i for i in items if i.get('id')}
    padre = {i: i for i in ids}

    def raiz(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x

    for n in range(len(ids)):
        for m in range(n + 1, len(ids)):
            a, b = ids[n], ids[m]
            if parecido(por_id[a].get('titulo', ''), por_id[b].get('titulo', '')) >= UMBRAL_AGRUPAR:
                ra, rb = raiz(a), raiz(b)
                if ra != rb:
                    padre[rb] = ra

    juntos: dict[str, list[str]] = {}
    for i in ids:
        juntos.setdefault(raiz(i), []).append(i)

    grupos = []
    for miembros in juntos.values():
        miembros.sort()
        grupos.append({
            'id': 'g_' + uuid.uuid4().hex[:8],
            'nombre': _nombre_comun([por_id[m].get('titulo', '') for m in miembros]),
            'items': miembros,
            'sugerido': True,
        })
    grupos.sort(key=lambda g: (-len(g['items']), g['nombre']))
    return grupos


def _nombre_comun(titulos: list[dict]) -> str:
    """Nombre del grupo: lo que todos los titulos comparten, en su orden original."""
    if not titulos:
        return 'Sin nombre'
    if len(titulos) == 1:
        return titulos[0][:70]
    comunes = _tokens_producto(titulos[0])
    for t in titulos[1:]:
        comunes &= _tokens_producto(t)
    if not comunes:
        return titulos[0][:70]
    orden = [p for p in re.findall(r'[A-Za-zÀ-ÿ0-9]+', titulos[0])
             if _norm(p) in comunes]
    return ' '.join(orden[:8]).title() or titulos[0][:70]


# ── Persistencia ─────────────────────────────────────────────────────────────

def listar_grupos(alias: str) -> list[dict]:
    data = cerebro._load(alias, ARCHIVO, {'grupos': []})
    return data.get('grupos', []) if isinstance(data, dict) else []


def guardar_grupos(alias: str, grupos: list[dict]) -> bool:
    return cerebro._save(alias, ARCHIVO, {'grupos': grupos})


def grupo_de(alias: str, item_id: str) -> dict | None:
    for g in listar_grupos(alias):
        if item_id in (g.get('items') or []):
            return g
    return None


def hermanas_de(alias: str, item_id: str) -> list[str]:
    """Las publicaciones propias del mismo producto, incluida la consultada.

    Si no hay grupo definido, la publicacion es su propio grupo: nunca devuelve
    vacio, para que el llamador no tenga que preguntar si hay grupos o no.
    """
    g = grupo_de(alias, item_id)
    return list(g.get('items') or [item_id]) if g else [item_id]


def guardar_grupo(alias: str, *, grupo_id: str | None, nombre: str,
                  items: list[str]) -> dict:
    """Crea o actualiza un grupo. Un item pertenece a UN solo grupo."""
    grupos = listar_grupos(alias)
    items = [i.strip().upper() for i in items if i and i.strip()]

    # Sacar estos items de cualquier otro grupo: pertenecer a dos grupos haria
    # que el mismo producto se analice contra dos conjuntos de competidores.
    for g in grupos:
        if g.get('id') != grupo_id:
            g['items'] = [i for i in (g.get('items') or []) if i not in items]
    grupos = [g for g in grupos if g.get('items') or g.get('id') == grupo_id]

    actual = next((g for g in grupos if g.get('id') == grupo_id), None)
    if actual:
        actual.update({'nombre': nombre or actual.get('nombre', ''),
                       'items': items, 'sugerido': False})
        nuevo = actual
    else:
        nuevo = {'id': grupo_id or ('g_' + uuid.uuid4().hex[:8]),
                 'nombre': nombre or 'Sin nombre', 'items': items,
                 'sugerido': False}
        grupos.append(nuevo)

    guardar_grupos(alias, [g for g in grupos if g.get('items')])
    return nuevo


def eliminar_grupo(alias: str, grupo_id: str) -> bool:
    grupos = listar_grupos(alias)
    quedan = [g for g in grupos if g.get('id') != grupo_id]
    if len(quedan) == len(grupos):
        return False
    guardar_grupos(alias, quedan)
    return True


# ── Competidores del grupo ───────────────────────────────────────────────────

def competidores_del_grupo(alias: str, item_id: str,
                           clase: str | None = None) -> list[dict]:
    """Todos los competidores del producto, sin importar a que hermana se cargaron.

    Es la funcion que hace que cargar competidores una vez alcance: se capturan
    contra cualquier publicacion del grupo y los ve todo el grupo. Se
    deduplica por id de competidor, quedandose con el de mejor puntaje.
    """
    hermanas = set(hermanas_de(alias, item_id))
    mejores: dict[str, dict] = {}
    for c in cerebro.listar_competidores(alias, clase=clase):
        if c.get('item_propio') not in hermanas:
            continue
        prev = mejores.get(c['id'])
        if prev is None or (c.get('puntaje') or 0) > (prev.get('puntaje') or 0):
            mejores[c['id']] = c
    return sorted(mejores.values(),
                  key=lambda c: (c.get('puntaje') or 0), reverse=True)


def directos_del_grupo(alias: str, item_id: str) -> list[dict]:
    """Solo los confirmados como directos — los unicos que mueven decisiones."""
    return competidores_del_grupo(alias, item_id, clase=cerebro.CLASE_DIRECTO)
