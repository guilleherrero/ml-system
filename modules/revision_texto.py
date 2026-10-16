"""
Prevencion de moderaciones — Parte 3: revision de texto.

Decision de Guille (2026-10-16): BLOQUEA lo que ML rechaza seguro (datos de
contacto y emojis; HTML en la descripcion) y AVISA el resto (terminos de
imitacion, redes sociales sueltas, su propia lista de palabras).

Es puro texto: no llama a ML. Lo usan el Duplicador (descripcion de la copia)
y la pantalla de Prevencion de moderaciones.

Criterios para no bloquear de mas:
- Telefonos solo en titulo y descripcion: en la ficha hay GTIN, modelos y
  medidas que se confunden con numeros (RH-6668, 7798123456789, 19.5 cm).
- Nombres de redes solos son aviso: "ideal para TikTok" o "compatible con
  WhatsApp" describen el producto, no un contacto.
"""
import re

from core.db_storage import db_load, db_save

# Emojis y simbolos pictograficos (mismo criterio que la Regla #3)
_EMOJI = re.compile('[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF]')
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_URL = re.compile(r'https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|shop|store|site|online)(?:\.ar)?\b', re.I)
_ARROBA = re.compile(r'(?<![\w.])@[A-Za-z0-9_.]{3,}')
_TELEFONOS = [
    re.compile(r'(?:\+\s?54|\b54)[\s.-]?9?[\s.-]?\d{2,4}[\s.-]?\d{3,4}[\s.-]?\d{4}\b'),           # internacional
    re.compile(r'\(?\b0?(?:11|2\d{2}|3\d{2})\)?[\s.-]?(?:15[\s.-]?)?\d{4}[\s.-]?\d{4}\b'),          # area + 8 digitos
    re.compile(r'\b(?:tel|tel[eé]fono|cel|celular|whats\s?app|wsp|wpp|llam[aá](?:me|nos))\b\D{0,12}\d[\d\s.-]{6,}', re.I),
]
_HTML = re.compile(r'<[a-zA-Z/][^>]*>')
_IMITACION = re.compile(r'\b(r[eé]plica|s[ií]mil|imitaci[oó]n|clon|alternativ[ao] a|inspirad[oa] en|aaa|1\s?:\s?1)\b', re.I)
_REDES = re.compile(r'\b(whats\s?app|instagram|facebook|tik\s?tok|telegram)\b', re.I)


def _safe(alias: str) -> str:
    return (alias or '').replace(' ', '_').replace('/', '-')


def _path(alias: str) -> str:
    return f'data/revision_texto_{_safe(alias)}.json'


def listas(alias: str) -> dict:
    d = db_load(_path(alias)) or {}
    return {'prohibidas': d.get('prohibidas', []), 'permitidas': d.get('permitidas', [])}


def guardar_listas(alias: str, prohibidas: list, permitidas: list) -> dict:
    limpiar = lambda xs: sorted({(x or '').strip().lower() for x in xs if (x or '').strip()})
    d = {'prohibidas': limpiar(prohibidas), 'permitidas': limpiar(permitidas)}
    db_save(_path(alias), d)
    return d


def _hallazgo(nivel, regla, campo, texto, mensaje):
    return {'nivel': nivel, 'regla': regla, 'campo': campo, 'texto': texto, 'mensaje': mensaje}


def revisar(texto: str, campo: str, listas_usuario: dict | None = None) -> list[dict]:
    """campo: 'titulo' | 'descripcion' | 'ficha'. Devuelve hallazgos, los que
    bloquean con nivel 'bloquea' y el resto 'aviso'."""
    t = texto or ''
    lu = listas_usuario or {}
    permitidas = set(lu.get('permitidas') or [])
    out = []

    def permitido(s):
        return s.strip().lower() in permitidas

    for m in set(_EMOJI.findall(t)):
        out.append(_hallazgo('bloquea', 'emoji', campo, m, 'ML rechaza emojis en títulos, descripciones y fichas.'))
    # Datos de contacto: se juntan todas las coincidencias y, si se pisan
    # (el telefono y "Llamanos al <telefono>", o el email y su dominio), queda
    # la mas larga. Asi cada dato aparece una sola vez.
    spans = []
    for rx, msg in ((_EMAIL, 'Email: ML no permite datos de contacto.'),
                    (_URL, 'Link externo: ML no permite datos de contacto ni links.'),
                    (_ARROBA, 'Usuario de red social: ML no permite datos de contacto.')):
        for m in rx.finditer(t):
            if not permitido(m.group(0)):
                spans.append((m.start(), m.end(), m.group(0), msg))
    if campo != 'ficha':
        for rx in _TELEFONOS:
            for m in rx.finditer(t):
                spans.append((m.start(), m.end(), m.group(0).strip(), 'Parece un teléfono: ML no permite datos de contacto.'))
    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    elegidos = []
    for sp in sorted(spans, key=lambda x: -(x[1] - x[0])):
        if not any(sp[0] < e[1] and e[0] < sp[1] for e in elegidos):
            elegidos.append(sp)
    for a, b, txt, msg in sorted(elegidos):
        out.append(_hallazgo('bloquea', 'contacto', campo, txt, msg))
    if campo == 'descripcion' and _HTML.search(t):
        out.append(_hallazgo('bloquea', 'html', campo, _HTML.search(t).group(0), 'ML solo acepta texto plano en la descripción.'))

    for m in {x.group(0) for x in _IMITACION.finditer(t)}:
        if not permitido(m):
            out.append(_hallazgo('aviso', 'imitacion', campo, m,
                                 'Término de imitación: ML lo asocia a productos no originales (la causa más repetida de tus infracciones).'))
    for m in {x.group(0) for x in _REDES.finditer(t)}:
        if not permitido(m):
            out.append(_hallazgo('aviso', 'red_social', campo, m,
                                 'Nombre de red social: si describe el producto está bien; si invita a contactarte, ML lo sanciona.'))
    t_low = t.lower()
    for p in lu.get('prohibidas') or []:
        if p and p not in permitidas and re.search(r'(?<!\w)' + re.escape(p) + r'(?!\w)', t_low):
            out.append(_hallazgo('aviso', 'lista_propia', campo, p, 'Está en tu lista de palabras prohibidas.'))
    return out


def bloquea(hallazgos: list[dict]) -> list[dict]:
    return [h for h in hallazgos if h['nivel'] == 'bloquea']


def revisar_item(item: dict, descripcion: str | None, listas_usuario: dict | None = None) -> list[dict]:
    ficha = ' | '.join(f"{a.get('name') or a.get('id')}: {a.get('value_name')}"
                       for a in item.get('attributes') or [] if a.get('value_name'))
    out = revisar(item.get('title', ''), 'titulo', listas_usuario) + revisar(ficha, 'ficha', listas_usuario)
    if descripcion is not None:
        out += revisar(descripcion, 'descripcion', listas_usuario)
    return out


def revisar_catalogo(client, alias: str) -> dict:
    """Titulo y ficha de todas las activas, de a 20 por llamada. La descripcion
    no: se revisa una por una cuando se abre (decision de Guille: liviano)."""
    lu = listas(alias)
    ids, offset = [], 0
    while True:
        data = client.get_my_listings(limit=50, offset=offset, status='active') or {}
        ids += data.get('results') or []
        offset += 50
        if offset >= (data.get('paging') or {}).get('total', 0):
            break
    filas = []
    for k in range(0, len(ids), 20):
        entries = client._get('/items', params={'ids': ','.join(ids[k:k + 20]),
                                                'attributes': 'id,title,attributes,permalink,secure_thumbnail,thumbnail,catalog_listing'}) or []
        for e in entries:
            b = e.get('body') or {}
            if e.get('code') != 200 or b.get('catalog_listing'):
                continue          # en catalogo titulo y ficha los pone ML
            h = revisar_item(b, None, lu)
            filas.append({'id': b['id'], 'titulo': b.get('title', ''), 'permalink': b.get('permalink'),
                          'thumbnail': (b.get('secure_thumbnail') or b.get('thumbnail') or '').replace('http://', 'https://') or None, 'hallazgos': h,
                          'bloquea': len(bloquea(h)), 'avisos': len(h) - len(bloquea(h))})
    filas.sort(key=lambda f: (-f['bloquea'], -f['avisos']))
    return {'revisadas': len(filas), 'con_problemas': sum(1 for f in filas if f['hallazgos']), 'filas': filas}
