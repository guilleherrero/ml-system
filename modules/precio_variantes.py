"""
Precio y cuotas para TODAS las variantes de una publicacion.

Bug real (2026-10-12, Cortador Ender Pro, Novara): la calculadora le cambio
precio y cuotas solo a Rojo (MLA1568967243) y Negro (MLA1481911017) quedo como
estaba. En el modelo User Products de ML cada variante (color) es un ITEM
DISTINTO, sin `variations[]`; lo que las agrupa es `family_id` — lo que el
panel de ML muestra como "una publicacion con 2 variantes" (URL /up/MLAU...).
Un PUT a /items/{id} toca una sola.

Este modulo:
  - encuentra las variantes de un item (misma family_id, mismo tipo catalogo/no),
  - calcula el margen actual y el resultante de cada una con SU costo,
  - aplica precio + cuotas a cada una y las vuelve a leer para verificar,
  - detecta familias desalineadas (precio o cuotas distintas) para alertar.

El cambio nunca se aplica directo: la calculadora deja una propuesta en Cerebro
(sale a Telegram sola) y `cerebro.aprobar_accion` llama a `ejecutar()` — un
unico punto, se apruebe desde el celular o desde el panel.
"""

import logging
import os

from core.db_storage import db_load, db_save
from modules.precio_motor import MARGEN_MINIMO_ACEPTABLE
from modules.trio_generador import CUOTAS_A_TAGS

_logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# Los tags de cuotas que ML reemplaza en bloque (el PUT pisa la lista entera).
TAGS_DE_CUOTAS = {'pcj-co-funded', '3x_campaign', '9x_campaign', '12x_campaign'}

ETIQUETA_ESCALON = {
    0: 'Sin cuotas', 4: '3 a 12 con interés bajo', 3: '3 cuotas sin interés',
    6: '6 cuotas sin interés', 9: '9 cuotas sin interés', 12: '12 cuotas sin interés',
}

_ATRIBUTOS = ('id,title,price,family_id,user_product_id,listing_type_id,tags,'
              'catalog_listing,status,attributes')


def _safe(alias: str) -> str:
    return (alias or '').replace(' ', '_').replace('/', '-')


def escalon_de(listing_type: str, tags) -> int | None:
    """Escalon de cuotas (la clave de CUOTAS_A_TAGS) que tiene hoy el item.
    None si la combinacion no es ninguna de las conocidas."""
    tag = next((t for t in (tags or []) if t in TAGS_DE_CUOTAS), None)
    for esc, (lt, ts) in CUOTAS_A_TAGS.items():
        if lt == listing_type and (ts[0] if ts else None) == tag:
            return esc
    return None


def etiqueta_escalon(esc) -> str:
    return ETIQUETA_ESCALON.get(esc, 'Cuotas no reconocidas')


def _attr(item: dict, attr_id: str) -> str:
    for a in item.get('attributes') or []:
        if a.get('id') == attr_id:
            return a.get('value_name') or ''
    return ''


def _resumen_item(it: dict) -> dict:
    esc = escalon_de(it.get('listing_type_id'), it.get('tags'))
    return {
        'id':              it.get('id'),
        'titulo':          it.get('title', ''),
        'color':           _attr(it, 'COLOR'),
        'sku':             _attr(it, 'SELLER_SKU'),
        'precio':          float(it.get('price') or 0),
        'listing_type':    it.get('listing_type_id'),
        'escalon':         esc,
        'cuotas':          etiqueta_escalon(esc),
        'user_product_id': it.get('user_product_id'),
        'catalog_listing': bool(it.get('catalog_listing')),
    }


def _items_activos(client) -> list[dict]:
    """Todas las publicaciones activas con los campos que hacen falta aca.
    En vivo: el snapshot de stock puede tener un dia y esto decide que se
    cambia en ML."""
    out, offset = [], 0
    while True:
        data = client.get_my_listings(limit=50, offset=offset, status='active')
        ids = data.get('results') or []
        if not ids:
            break
        for i in range(0, len(ids), 20):
            entries = client._get('/items', params={'ids': ','.join(ids[i:i + 20]),
                                                    'attributes': _ATRIBUTOS})
            out += [e.get('body') or {} for e in entries if e.get('code') == 200]
        offset += 50
        if offset >= (data.get('paging') or {}).get('total', 0):
            break
    return out


def variantes(client, item_id: str, excluir=()) -> list[dict]:
    """El item pedido primero y despues sus hermanas de la misma familia.

    Mismo `catalog_listing` que el pedido: la publicacion de catalogo compite
    por buy box con su propio precio, no es "otra variante" de la clasica.
    `excluir`: item_ids que se separan a proposito (duplicadas Medio/Compensa
    del trio, que tienen precio y cuotas distintos por diseño).
    """
    raw = client._get(f'/items/{item_id}', params={'attributes': _ATRIBUTOS})
    base = {**_resumen_item(raw), 'sugerida': True}
    fam = raw.get('family_id')
    if not fam:
        return [base]
    excluir = set(excluir or ())
    otros: dict = {}
    for it in _items_activos(client):
        if (it.get('id') == item_id or it.get('family_id') != fam or it.get('id') in excluir
                or bool(it.get('catalog_listing')) != base['catalog_listing']):
            continue
        r = _resumen_item(it)
        # Mismo user_product_id = mismo color en otro escalon de cuotas (trio
        # Batalla/Medio/Compensa): no es otra variante, tiene otro precio a
        # proposito. Sin user_product_id (modelo viejo) no se puede saber.
        if base['user_product_id'] and r['user_product_id'] == base['user_product_id']:
            continue
        otros.setdefault(r['user_product_id'] or r['id'], []).append(r)
    out = [base]
    for pubs in otros.values():
        # De cada otro color se sugiere la publicacion del MISMO escalon que la
        # abierta (o la unica que tenga). Las demas se muestran sin tildar.
        for r in pubs:
            r['sugerida'] = len(pubs) == 1 or r['escalon'] == base['escalon']
            out.append(r)
    return out


# ── Margen por variante ──────────────────────────────────────────────────────

def margenes(vars_: list[dict], costos: dict, precio_nuevo: float,
             ganancia_venta_base: float, costo_base: float) -> list[dict]:
    """Margen resultante de cada variante con SU costo.

    Precio, cuotas, comision y envio son los mismos para todas (mismo producto,
    mismo precio nuevo, mismo escalon), asi que lo unico que cambia la ganancia
    es el costo: ganancia_i = ganancia_base + (costo_base - costo_i). Eso evita
    recalcular el modelo de cargos de la calculadora en otro lado.

    Sin costo cargado no se puede garantizar el 15% → la variante queda
    bloqueada, no se asume nada.
    """
    out = []
    for v in vars_:
        costo = (costos.get(v['id']) or {}).get('costo')
        fila = {**v, 'costo': costo, 'ganancia_nueva': None,
                'margen_nuevo_pct': None, 'bloqueo': None}
        if costo in (None, '', 0):
            fila['bloqueo'] = 'sin costo cargado, no se puede verificar el margen mínimo'
        elif precio_nuevo > 0:
            g = float(ganancia_venta_base) + float(costo_base) - float(costo)
            fila['ganancia_nueva'] = round(g, 2)
            fila['margen_nuevo_pct'] = round(g / precio_nuevo * 100, 1)
            if g / precio_nuevo < MARGEN_MINIMO_ACEPTABLE:
                fila['bloqueo'] = (f'el margen quedaría en {fila["margen_nuevo_pct"]}%, '
                                   f'debajo del mínimo de {MARGEN_MINIMO_ACEPTABLE:.0%}')
        out.append(fila)
    return out


# ── Aplicar y verificar ──────────────────────────────────────────────────────

def aplicar_item(client, item_id: str, precio: float, escalon: int | None) -> dict:
    """Precio + cuotas en UN item. Movido tal cual de /api/pricing/aplicar.

    Reglas de ML (verificadas 2026-09-20): listing_type va por su endpoint
    dedicado y primero; el PUT de tags reemplaza la lista completa, asi que se
    conservan los tags que no son de cuotas.
    """
    antes = client.get_item(item_id)
    payload = {'price': float(precio)}
    if escalon is not None:
        lt_obj, tags_obj = CUOTAS_A_TAGS[escalon]
        if antes.get('listing_type_id') != lt_obj:
            client.update_listing_type(item_id, lt_obj)
        payload['tags'] = [t for t in (antes.get('tags') or []) if t not in TAGS_DE_CUOTAS] + list(tags_obj[:1])
    client.update_item(item_id, payload)
    return {'precio_antes': float(antes.get('price') or 0),
            'escalon_antes': escalon_de(antes.get('listing_type_id'), antes.get('tags'))}


def verificar_item(client, item_id: str, precio: float, escalon: int | None) -> str | None:
    """Vuelve a leer el item desde ML. None si quedo bien, si no el motivo."""
    it = client.get_item(item_id)
    problemas = []
    if abs(float(it.get('price') or 0) - float(precio)) > 0.5:
        problemas.append(f'precio en ML ${float(it.get("price") or 0):,.0f}, se pidió ${float(precio):,.0f}')
    if escalon is not None:
        esc = escalon_de(it.get('listing_type_id'), it.get('tags'))
        if esc != escalon:
            problemas.append(f'cuotas en ML "{etiqueta_escalon(esc)}", se pidió "{etiqueta_escalon(escalon)}"')
    return '; '.join(problemas) or None


def ejecutar(alias: str, accion: dict) -> dict:
    """Aplica una propuesta de la calculadora ya aprobada. Devuelve la accion
    actualizada con `detalle.resultado` = {item_id: 'ok' | 'error: ...'}.

    Cada variante se aplica y se verifica por separado: si una falla, las
    demas siguen y se dice cual fallo. El experimento se registra con las que
    quedaron bien.
    """
    from core.account_manager import AccountManager
    from modules import cerebro

    ej = (accion.get('detalle') or {}).get('ejecutar') or {}
    precio, escalon = float(ej['precio_nuevo']), ej.get('escalon')
    resultado, precios_antes, ganancias = {}, {}, {}

    try:
        client = AccountManager().get_client(alias)
    except Exception as e:
        resultado = {iid: f'error: no se pudo conectar con ML ({e})' for iid in ej['item_ids']}
        client = None

    for iid in (ej['item_ids'] if client else []):
        try:
            previo = aplicar_item(client, iid, precio, escalon)
            precios_antes[iid] = previo['precio_antes']
        except Exception as e:
            resultado[iid] = f'error: ML rechazó el cambio ({e})'
            continue
        try:
            problema = verificar_item(client, iid, precio, escalon)
        except Exception as e:
            problema = f'no se pudo releer para verificar ({e})'
        resultado[iid] = f'error: {problema}' if problema else 'ok'
        if not problema:
            ganancias[iid] = (ej.get('ganancias') or {}).get(iid, 0)

    exp_id = None
    if ganancias:
        try:
            from web.db import session_scope
            from web.models_pricing import PrecioExperimento
            with session_scope() as s:
                exp = PrecioExperimento(
                    item_ids=list(ganancias), ganancia_venta=ganancias,
                    precios_antes={i: precios_antes[i] for i in ganancias},
                    precios_despues={i: precio for i in ganancias},
                    **ej.get('experimento', {}))
                s.add(exp)
                s.flush()
                exp_id = exp.id
        except Exception as e:
            _logger.error('[precio_variantes] precio aplicado pero experimento sin guardar: %s', e)

    ok = [i for i, r in resultado.items() if r == 'ok']
    final = {'resultado': resultado, 'experimento_id': exp_id,
             'aplicadas': len(ok), 'total': len(ej['item_ids'])}
    if ok:
        return cerebro.marcar_aplicada(alias, accion['id'], final) or accion
    det = {**(accion.get('detalle') or {}), **final}
    return cerebro.actualizar_accion(alias, accion['id'], detalle=det) or accion


def texto_resultado(accion: dict) -> str:
    """Una linea por variante para Telegram y el panel."""
    det = accion.get('detalle') or {}
    res = det.get('resultado') or {}
    nombres = {v['id']: (v.get('color') or v['id']) for v in det.get('variantes') or []}
    lineas = [f'Aplicado en {det.get("aplicadas", 0)} de {det.get("total", len(res))} variantes.']
    for iid, r in res.items():
        lineas.append(f'{"✅" if r == "ok" else "❌"} {nombres.get(iid, iid)} ({iid})'
                      + ('' if r == 'ok' else f' — {r[7:] if r.startswith("error: ") else r}'))
    return '\n'.join(lineas)


# ── Control preventivo: familias desalineadas ────────────────────────────────

def _a_proposito_path(alias: str) -> str:
    return os.path.join(DATA_DIR, f'variantes_a_proposito_{_safe(alias)}.json')


def familias_a_proposito(alias: str) -> dict:
    return db_load(_a_proposito_path(alias)) or {}


def marcar_a_proposito(alias: str, family_id: str, a_proposito: bool = True,
                       nota: str = '') -> None:
    """Flag permanente (no vence como el 'ocultar' de top acciones): esta
    familia tiene precios/cuotas distintos por decision propia."""
    data = familias_a_proposito(alias)
    if a_proposito:
        data[str(family_id)] = {'nota': nota[:200]}
    else:
        data.pop(str(family_id), None)
    db_save(_a_proposito_path(alias), data)


def desalineadas(items: list[dict], a_proposito: dict | None = None) -> list[dict]:
    """Familias donde las variantes (colores) no ofrecen lo mismo.

    En User Products cada color es un `user_product_id` distinto. Un mismo
    color puede tener VARIAS publicaciones a proposito (el trio Batalla /
    Medio / Compensa: mismo producto, otro precio por escalon de cuotas) —
    esas comparten user_product_id y no se comparan entre si. Lo que se
    compara es la oferta de cada color: el conjunto (precio, cuotas) de sus
    publicaciones. Si dos colores no ofrecen exactamente lo mismo, alerta.
    Sin user_product_id (modelo viejo) cada item cuenta como su propio color.

    `items`: dicts con id, titulo, precio, family_id, user_product_id,
    listing_type, tags, catalog_listing (snapshot de stock o `_items_activos`).
    """
    a_proposito = a_proposito or {}
    grupos: dict = {}
    for it in items:
        fam = it.get('family_id')
        if not fam or str(fam) in a_proposito:
            continue
        lt = it.get('listing_type') or it.get('listing_type_id')
        grupos.setdefault((str(fam), bool(it.get('catalog_listing'))), []).append({
            'id': it.get('id'), 'titulo': it.get('titulo') or it.get('title', ''),
            'user_product_id': it.get('user_product_id') or it.get('id'),
            'precio': float(it.get('precio') or it.get('price') or 0),
            'cuotas': etiqueta_escalon(escalon_de(lt, it.get('tags'))),
        })
    out = []
    for (fam, catalogo), vs in grupos.items():
        por_color: dict = {}
        for v in vs:
            por_color.setdefault(v['user_product_id'], set()).add((v['precio'], v['cuotas']))
        if len(por_color) < 2:
            continue
        ofertas = list(por_color.values())
        if all(o == ofertas[0] for o in ofertas):
            continue
        out.append({'family_id': fam, 'catalogo': catalogo,
                    'variantes': sorted(vs, key=lambda v: (v['user_product_id'], v['cuotas'])),
                    'difiere_precio': len({frozenset(p for p, _ in o) for o in ofertas}) > 1,
                    'difiere_cuotas': len({frozenset(c for _, c in o) for o in ofertas}) > 1})
    return out


def desalineadas_cuenta(alias: str) -> list[dict]:
    """Desde el snapshot de stock (sin pegarle a ML: lo usan el resumen diario
    y top acciones, que no deben llamar la API en vivo)."""
    stock = db_load(os.path.join(DATA_DIR, f'stock_{_safe(alias)}.json')) or {}
    return desalineadas(stock.get('items') or [], familias_a_proposito(alias))


if __name__ == '__main__':
    # Self-check de la logica pura (sin ML).
    assert escalon_de('gold_special', ['pcj-co-funded', 'good_quality']) == 4
    assert escalon_de('gold_pro', ['3x_campaign']) == 3
    assert escalon_de('gold_pro', []) == 6
    assert escalon_de('gold_special', []) == 0
    caso = [  # el caso real del Cortador Ender Pro
        {'id': 'R', 'precio': 72000, 'family_id': 1, 'listing_type': 'gold_special', 'tags': ['pcj-co-funded']},
        {'id': 'N', 'precio': 48000, 'family_id': 1, 'listing_type': 'gold_pro', 'tags': ['3x_campaign']},
        {'id': 'C', 'precio': 50000, 'family_id': 1, 'listing_type': 'gold_pro', 'tags': [], 'catalog_listing': True},
        {'id': 'X', 'precio': 10000, 'family_id': 2, 'listing_type': 'gold_pro', 'tags': []},
        {'id': 'Y', 'precio': 10000, 'family_id': 2, 'listing_type': 'gold_pro', 'tags': []},
    ]
    trio = [  # mismo color en 3 escalones (mismo user_product_id): a proposito
        {'id': 'B', 'precio': 48000, 'family_id': 3, 'user_product_id': 'U1', 'listing_type': 'gold_special', 'tags': []},
        {'id': 'M', 'precio': 56000, 'family_id': 3, 'user_product_id': 'U1', 'listing_type': 'gold_special', 'tags': ['pcj-co-funded']},
        {'id': 'C', 'precio': 71000, 'family_id': 3, 'user_product_id': 'U1', 'listing_type': 'gold_pro', 'tags': []},
    ]
    assert desalineadas(trio) == []
    # dos colores con el mismo trio: alineados; si un color sube Medio, alerta
    trio2 = trio + [{**t, 'id': t['id'] + '2', 'user_product_id': 'U2'} for t in trio]
    assert desalineadas(trio2) == []
    trio2[4] = {**trio2[4], 'precio': 60000}
    d2 = desalineadas(trio2)
    assert len(d2) == 1 and d2[0]['difiere_precio'] and not d2[0]['difiere_cuotas']
    d = desalineadas(caso)
    assert len(d) == 1 and d[0]['difiere_precio'] and d[0]['difiere_cuotas']
    assert {v['id'] for v in d[0]['variantes']} == {'R', 'N'}  # catalogo aparte
    assert desalineadas(caso, {'1': {}}) == []                 # marcada a proposito
    m = margenes([{'id': 'R'}, {'id': 'N'}, {'id': 'Z'}],
                 {'R': {'costo': 24000}, 'N': {'costo': 22000}},
                 precio_nuevo=48000, ganancia_venta_base=9000, costo_base=24000)
    assert m[0]['margen_nuevo_pct'] == 18.8 and m[0]['bloqueo'] is None
    assert m[1]['ganancia_nueva'] == 11000                      # 2000 menos de costo
    assert m[2]['bloqueo'] and 'sin costo' in m[2]['bloqueo']
    m = margenes([{'id': 'R'}], {'R': {'costo': 24000}}, 48000, 6000, 24000)
    assert m[0]['bloqueo'] and '12.5%' in m[0]['bloqueo']        # debajo del 15%
    print('precio_variantes: ok')
