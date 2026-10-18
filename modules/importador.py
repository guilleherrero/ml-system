"""
Importador de publicaciones — "Importar como plantilla" (2026-10-18).

Pedido de Guille, a partir del Importador de UpSeller y su extension. ML
bloqueo leer publicaciones ajenas por API (403, correccion 59 de CEREBRO), asi
que el boton nuevo lee la PAGINA que el usuario esta mirando y la manda aca.

Que se usa de la publicacion ajena (decision aprobada por Guille):
- Si: titulo como referencia, precio como referencia, caracteristicas (ficha),
  y las fotos SOLO para mirarlas.
- No: las fotos no se copian. En Novara, los relojes de enfermeria
  importados con fotos ajenas recibieron el 2026-02-02 infracciones por marcas
  de agua, datos de contacto en fotos y categoria incorrecta.
- Descripcion: se trae como referencia y se puede "usar como base" (pedido de
  Guille 2026-10-18). Bloquea datos de contacto y la marca del otro si la del
  usuario es otra; avisa si queda casi igual a la original.

Dos formas de publicar, las dos solo con aprobacion:
- Propia: categoria sugerida por ML, ficha editable, fotos y descripcion del
  usuario, precio con la calculadora (piso 15%). POST /items.
- Catalogo (solo si la pagina era /p/MLA...): POST /items con
  catalog_product_id + catalog_listing; fotos y ficha las pone ML. Exige
  confirmar que el producto es exactamente ese (doc "Publicar en catalogo").
"""
import re
import unicodedata
import uuid
from datetime import datetime

from core.db_storage import db_load, db_save
from modules import duplicador as dup
from modules import revision_texto as rt
from modules.moderaciones import sugerir_categorias
from modules.trio_generador import CUOTAS_A_TAGS

ESCALONES = (0, 4, 3, 6, 9, 12)


class ImportadorError(Exception):
    """Error con un mensaje que se puede mostrar tal cual al usuario."""


def _safe(alias: str) -> str:
    return (alias or '').replace(' ', '_').replace('/', '-')


def _path(alias: str) -> str:
    return f'data/importador_{_safe(alias)}.json'


def _cargar(alias: str) -> dict:
    d = db_load(_path(alias)) or {}
    d.setdefault('borradores', {})
    return d


def _guardar(alias: str, d: dict):
    db_save(_path(alias), d)


def _norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', (s or '').lower())
    return re.sub(r'[^a-z0-9 ]', '', ''.join(c for c in s if not unicodedata.combining(c))).strip()


# ── Captura (lo que manda el boton) ──────────────────────────────────────────

def limpiar_captura(datos: dict) -> dict:
    """La captura viene de una pagina ajena: se valida y se recorta todo."""
    url = str(datos.get('url') or '')[:500]
    if not re.match(r'^https://([a-z]+\.)?mercadolibre\.com\.ar/', url):
        raise ImportadorError('La captura no viene de una página de MercadoLibre Argentina.')
    titulo = re.sub(r'\s+', ' ', str(datos.get('titulo') or '')).strip()[:200]
    if not titulo:
        raise ImportadorError('No se pudo leer el título de la publicación. ¿Estabas en la página de un producto?')
    try:
        precio = round(float(datos.get('precio') or 0), 2)
    except (TypeError, ValueError):
        precio = 0.0
    fotos = [f for f in (datos.get('fotos') or []) if isinstance(f, str)
             and re.match(r'^https://http2\.mlstatic\.com/', f)][:20]
    specs = []
    for par in (datos.get('specs') or [])[:80]:
        if isinstance(par, (list, tuple)) and len(par) == 2 and all(isinstance(x, str) for x in par):
            n, v = par[0].strip()[:120], par[1].strip()[:250]
            if n and v:
                specs.append([n, v])
    m_cat = re.search(r'/p/(MLA\d+)', url)
    m_up = re.search(r'/up/(MLAU\d+)', url)
    descripcion = str(datos.get('descripcion') or '').replace('\r\n', '\n').strip()[:50000]
    cat_pag = datos.get('categoria_pagina')
    return {'url': url, 'titulo': titulo, 'precio': precio, 'fotos': fotos, 'specs': specs, 'descripcion': descripcion,
            # El catalog_product_id solo vale si la pagina ES de catalogo: en las
            # /up/ el HTML trae ids de OTROS productos (visto: MLA2080523428).
            'catalog_product_id': m_cat.group(1) if m_cat else None,
            'user_product_id': m_up.group(1) if m_up else None,
            'categoria_pagina': cat_pag if isinstance(cat_pag, str) and re.fullmatch(r'MLA\d+', cat_pag) else None}


def capturar(alias: str, datos: dict, usuario: str = '') -> dict:
    origen = limpiar_captura(datos)
    d = _cargar(alias)
    for b in d['borradores'].values():
        if b['estado'] == 'pendiente' and b['origen']['url'].split('#')[0] == origen['url'].split('#')[0]:
            # Ya estaba importada: no se duplica el borrador, pero se refresca lo
            # leido de la pagina (p. ej. capturas viejas sin descripcion). La
            # edicion del usuario no se toca.
            b['origen'] = origen
            _guardar(alias, d)
            return b
    bid = uuid.uuid4().hex[:10]
    b = {'id': bid, 'estado': 'pendiente', 'creado': datetime.now().isoformat(timespec='seconds'),
         'creado_por': usuario, 'origen': origen,
         'edicion': {'family_name': origen['titulo'][:60], 'category_id': None, 'atributos': {},
                     'escalon': 0, 'precio': None, 'costo': None, 'stock': 1,
                     'garantia_tipo': '', 'garantia_tiempo': '', 'descripcion': ''}}
    d['borradores'][bid] = b
    _guardar(alias, d)
    return b


def listar(alias: str) -> list[dict]:
    return sorted(_cargar(alias)['borradores'].values(), key=lambda b: b['creado'], reverse=True)


def obtener(alias: str, bid: str) -> dict:
    b = _cargar(alias)['borradores'].get(bid)
    if not b:
        raise ImportadorError('Importación no encontrada.')
    return b


def eliminar(alias: str, bid: str) -> dict:
    """Saca la importacion de la lista (solo datos de ml-system). Si ya se
    publico, la publicacion en ML sigue igual: esto no la toca."""
    d = _cargar(alias)
    b = d['borradores'].pop(bid, None)
    if not b:
        raise ImportadorError('Importación no encontrada.')
    _guardar(alias, d)
    return b


def guardar_edicion(alias: str, bid: str, edicion: dict) -> dict:
    d = _cargar(alias)
    b = d['borradores'].get(bid)
    if not b:
        raise ImportadorError('Importación no encontrada.')
    if b['estado'] != 'pendiente':
        raise ImportadorError(f'Ya está {b["estado"]}: no se edita.')
    e = b['edicion']
    e['family_name'] = re.sub(r'\s+', ' ', str(edicion.get('family_name') or '')).strip()[:120]
    cat = edicion.get('category_id')
    e['category_id'] = cat if isinstance(cat, str) and re.fullmatch(r'MLA\d+', cat) else None
    e['atributos'] = {k: str(v).strip()[:250] for k, v in (edicion.get('atributos') or {}).items()
                      if re.fullmatch(r'[A-Z0-9_]{2,60}', str(k)) and str(v).strip()}
    try:
        e['escalon'] = int(edicion.get('escalon') or 0)
    except (TypeError, ValueError):
        e['escalon'] = 0
    if e['escalon'] not in ESCALONES:
        raise ImportadorError('Escalón de cuotas inválido.')
    for k in ('precio', 'costo'):
        try:
            v = edicion.get(k)
            e[k] = round(float(v), 2) if v not in (None, '') else None
        except (TypeError, ValueError):
            raise ImportadorError(f'Valor inválido en {k}.')
    try:
        e['stock'] = max(1, int(edicion.get('stock') or 1))
    except (TypeError, ValueError):
        raise ImportadorError('Stock inválido.')
    e['garantia_tipo'] = str(edicion.get('garantia_tipo') or '')[:60]
    e['garantia_tiempo'] = str(edicion.get('garantia_tiempo') or '')[:40]
    e['descripcion'] = str(edicion.get('descripcion') or '').replace('\r\n', '\n')[:50000]
    _guardar(alias, d)
    return b


# ── Preparar la edicion (lecturas de ML) ─────────────────────────────────────

def atributos_categoria(client, category_id: str) -> list[dict]:
    attrs = client._get(f'/categories/{category_id}/attributes') or []
    out = []
    for a in attrs:
        tags = a.get('tags') or {}
        if tags.get('read_only') or tags.get('hidden') or tags.get('fixed') or tags.get('others'):
            continue
        out.append({'id': a['id'], 'nombre': a.get('name') or a['id'],
                    'obligatorio': bool(tags.get('required')),
                    'tipo': a.get('value_type'),
                    'valores': [v.get('name') for v in a.get('values') or [] if v.get('name')][:200]})
    out.sort(key=lambda x: (not x['obligatorio'], x['nombre']))
    return out


# Identifican el producto DE OTRO: no se precargan (riesgo de "no era original",
# 24 publicaciones de Novara). Se muestran como referencia para completar los tuyos.
ATRIBUTOS_IDENTIDAD = {'BRAND', 'MODEL', 'GTIN', 'SELLER_SKU', 'MPN', 'ALPHANUMERIC_MODEL', 'LINE'}


def _mapear(campos: list[dict], specs: list[list]) -> dict:
    por_nombre = {_norm(n): v for n, v in specs}
    return {c['id']: por_nombre[_norm(c['nombre'])] for c in campos if _norm(c['nombre']) in por_nombre}


def precargar_atributos(campos: list[dict], specs: list[list]) -> dict:
    """Mapea las caracteristicas leidas a los campos de la categoria por
    nombre normalizado, salvo las que identifican el producto del otro."""
    return {k: v for k, v in _mapear(campos, specs).items() if k not in ATRIBUTOS_IDENTIDAD}


def referencia_identidad(campos: list[dict], specs: list[list]) -> dict:
    return {k: v for k, v in _mapear(campos, specs).items() if k in ATRIBUTOS_IDENTIDAD}


def marca_ajena(specs: list[list]) -> str | None:
    return next((v for n, v in specs if _norm(n) == 'marca'), None)


def posibles_duplicados(propios: list[dict], family_name: str, category_id: str) -> list[dict]:
    """Publicaciones propias activas de la misma categoria con un titulo muy
    parecido: ML podria tratar la nueva como el mismo producto."""
    base = {w for w in _norm(family_name).split() if len(w) > 3}
    if not base:
        return []
    out = []
    for it in propios:
        if it.get('category_id') != category_id:
            continue
        otras = {w for w in _norm(it.get('title', '')).split() if len(w) > 3}
        if otras and len(base & otras) / len(base) >= 0.6:
            out.append({'id': it['id'], 'titulo': it.get('title', '')})
    return out


# ── Publicar ─────────────────────────────────────────────────────────────────

def validar_propia(b: dict, campos: list[dict], fotos_nuevas: list[dict], evaluacion: dict | None) -> list[str]:
    """Todo lo que impide publicar como publicacion propia, en castellano."""
    e, faltas = b['edicion'], []
    if not e['family_name']:
        faltas.append('Falta el nombre del producto.')
    if not e['category_id']:
        faltas.append('Falta elegir la categoría.')
    if not e.get('precio') or e['precio'] <= 0:
        faltas.append('Falta el precio.')
    if not e.get('costo'):
        faltas.append('Falta el costo: sin costo no se puede verificar el margen.')
    elif evaluacion and not evaluacion['cumple_piso']:
        faltas.append(f'A ese precio el margen queda en {evaluacion["margen_pct"]}%, debajo del piso de '
                      f'{dup.MARGEN_MINIMO * 100:.0f}%.')
    for c in campos:
        if c['obligatorio'] and not e['atributos'].get(c['id']):
            faltas.append(f'Falta "{c["nombre"]}" (obligatorio en esta categoría).')
    if not fotos_nuevas:
        faltas.append('Falta al menos una foto tuya: las del otro vendedor no se usan.')
    ajena = marca_ajena(b['origen']['specs'])
    if ajena and _norm(ajena) not in ('generica', 'generico', 'sin marca', 'nobrand', 'no brand'):
        mia = e['atributos'].get('BRAND', '')
        patron = r'(?<!\w)' + re.escape(_norm(ajena)) + r'(?!\w)'
        if _norm(ajena) != _norm(mia):
            if re.search(patron, _norm(e['family_name'])):
                faltas.append(f'El nombre incluye "{ajena}", la marca del otro vendedor, y tu marca es otra. '
                              'Sacala del nombre: ML lo toma como producto no original.')
            if re.search(patron, _norm(e['descripcion'])):
                faltas.append(f'La descripción menciona "{ajena}", la marca del otro vendedor, y tu marca es otra. '
                              'Sacala: ML lo toma como producto no original.')
    for h in rt.bloquea(rt.revisar(e['family_name'], 'titulo') + rt.revisar(e['descripcion'], 'descripcion')):
        faltas.append(f'{h["mensaje"]} ("{h["texto"]}")')
    return faltas


def payload_propia(b: dict, es_up: bool, foto_ids: list[str]) -> dict:
    e = b['edicion']
    lt, tags = CUOTAS_A_TAGS[e['escalon']]
    p = {'category_id': e['category_id'], 'price': e['precio'], 'currency_id': 'ARS',
         'available_quantity': e['stock'], 'buying_mode': 'buy_it_now', 'condition': 'new',
         'listing_type_id': lt, 'pictures': [{'id': i} for i in foto_ids],
         'attributes': [{'id': k, 'value_name': v} for k, v in e['atributos'].items()]}
    # Cuenta User Product: family_name y SIN title (doc "Precio por variacion").
    if es_up:
        p['family_name'] = e['family_name']
    else:
        p['title'] = e['family_name'][:60]
    if tags:
        p['tags'] = list(tags)
    st = []
    if e.get('garantia_tipo'):
        st.append({'id': 'WARRANTY_TYPE', 'value_name': e['garantia_tipo']})
    if e.get('garantia_tiempo'):
        st.append({'id': 'WARRANTY_TIME', 'value_name': e['garantia_tiempo']})
    if st:
        p['sale_terms'] = st
    return p


def payload_catalogo(b: dict, category_id: str) -> dict:
    e = b['edicion']
    lt, tags = CUOTAS_A_TAGS[e['escalon']]
    p = {'category_id': category_id, 'price': e['precio'], 'currency_id': 'ARS',
         'available_quantity': e['stock'], 'buying_mode': 'buy_it_now', 'condition': 'new',
         'listing_type_id': lt, 'catalog_product_id': b['origen']['catalog_product_id'], 'catalog_listing': True}
    if tags:
        p['tags'] = list(tags)
    return p


def marcar_publicado(alias: str, bid: str, item_id: str, modo: str, avisos: list[str], usuario: str = '') -> dict:
    d = _cargar(alias)
    b = d['borradores'][bid]
    b.update(estado='publicado', item_creado=item_id, modo=modo, avisos=avisos,
             publicado=datetime.now().isoformat(timespec='seconds'), publicado_por=usuario)
    _guardar(alias, d)
    return b


def sugerencias_categoria(b: dict, nombre_de=None) -> list[dict]:
    """La categoria del vendedor original va PRIMERO: con titulos cortos la
    sugerencia de ML falla (2026-10-18: "Cortadora Puntas Abiertas Recargable
    Sin Cable" -> "Puntas de Destornillador"; la de la pagina, Cortadoras de
    Pelo, era la correcta). Las de ML quedan como alternativas."""
    sug = [dict(s, origen='ml') for s in (sugerir_categorias(b['origen']['titulo']) or [])]
    pag = b['origen'].get('categoria_pagina')
    if pag:
        sug = [s for s in sug if s['id'] != pag]
        nombre = nombre_de(pag) if nombre_de else None
        sug.insert(0, {'id': pag, 'nombre': nombre, 'origen': 'vendedor'})
    return sug


def parecido_descripcion(b: dict) -> float:
    """0 a 1: cuanto se parece la descripcion propia a la del otro vendedor.

    Jaccard sobre grupos de 3 palabras: lineal en el largo del texto. Antes se
    usaba difflib.SequenceMatcher, que es cuadratico: con dos descripciones de
    50.000 caracteres podia tardar minutos y colgar el unico worker
    (hallazgo de la revision de seguridad, 2026-10-18)."""
    def grupos(texto):
        w = _norm(texto).split()[:20000]
        return {' '.join(w[i:i + 3]) for i in range(max(len(w) - 2, 0))} or set(w)
    a, o = grupos(b['edicion'].get('descripcion')), grupos(b['origen'].get('descripcion'))
    if not a or not o:
        return 0.0
    return round(len(a & o) / len(a | o), 2)


def avisos_propia(b: dict) -> list[str]:
    """Lo que conviene mirar pero no bloquea."""
    out = []
    if parecido_descripcion(b) >= 0.8:
        out.append(f'La descripción es {int(parecido_descripcion(b) * 100)}% igual a la del otro vendedor: '
                   'conviene pasarla a tus palabras (es texto de otro y ML puede verla como copia).')
    return out
