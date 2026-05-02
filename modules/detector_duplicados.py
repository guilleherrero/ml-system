"""
Detector de publicaciones duplicadas / canibalización
======================================================

Identifica clusters de publicaciones similares en el catálogo de un vendedor
y los clasifica en 3 niveles de severidad. Solo expone públicamente los que
requieren acción (puros y subperformantes); los sanos se filtran de la salida
de UI pero quedan disponibles internamente para análisis.

Algoritmo:
  1. Normalizar título — quitar tildes, lowercase, sacar palabras promo,
     tokens >2 caracteres.
  2. Comparar pares con difflib.SequenceMatcher.ratio() >= 0.85.
  3. Filtrar pares marcados manualmente como "ignorados" por el usuario.
  4. Construir clusters por unión de pares (transitividad).
  5. Clasificar severidad con detección de tokens "variante" legítima
     (talles, colores, capacidades) y métricas de venta del cluster.

Sin dependencias nuevas — solo stdlib.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable

# ── Configuración ────────────────────────────────────────────────────────────

THRESHOLD_SIMILARIDAD = 0.85   # ratio mínimo para considerar dos títulos del mismo cluster

# Palabras promocionales/genéricas que se eliminan al normalizar — no aportan
# señal semántica y generan falsos matches.
_PROMO_WORDS = {
    'oferta', 'descuento', 'promo', 'promocion', 'gratis', 'envio',
    'cuotas', 'nuevo', 'usado', 'original', 'garantia', 'mejor', 'barato',
    'economico', 'precio', 'venta', 'pack', 'kit', 'combo', 'super', 'mega',
    'ultra', 'premium', 'calidad', 'oficial',
}

# Tokens "variante" — si dos títulos solo difieren en estos tokens, el
# cluster es de variantes legítimas (talles/colores/medidas) y se clasifica
# como subperformante o sano según métricas, no como duplicado puro.
_TOKENS_TALLE = {
    'xs', 's', 'm', 'l', 'xl', 'xxl', 'xxxl',
    'pequeno', 'chico', 'mediano', 'grande', 'extra',
}
_TOKENS_COLOR = {
    'negro', 'blanco', 'rojo', 'azul', 'verde', 'gris', 'beige', 'rosa',
    'rosado', 'celeste', 'violeta', 'lila', 'marron', 'cafe', 'amarillo',
    'naranja', 'turquesa', 'fucsia', 'dorado', 'plateado', 'palido',
    'bordo', 'mostaza', 'aqua', 'coral',
}
_TOKENS_VARIANTE = _TOKENS_TALLE | _TOKENS_COLOR

_DIGITOS_RE = re.compile(r'^\d+$')


# ── Tipos de salida ──────────────────────────────────────────────────────────

@dataclass
class ItemCluster:
    """Una publicación dentro de un cluster, con sus métricas comparativas."""
    id: str
    titulo: str
    precio: float
    ventas_30d: int
    visitas_30d: int
    conversion_pct: float
    es_ganadora: bool = False


@dataclass
class Cluster:
    """Un cluster de publicaciones potencialmente duplicadas."""
    cluster_id: str
    severidad: str            # 'puro' | 'subperformante' | 'sano'
    items: list[ItemCluster] = field(default_factory=list)
    titulo_corto: str = ''
    visitas_perdidas_30d: int = 0
    impacto_monetario_estimado: float = 0.0


# ── Helpers de normalización ────────────────────────────────────────────────

def _strip_accents(text: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFKD', text or '')
        if unicodedata.category(c) != 'Mn'
    )


def _normalizar_titulo(titulo: str) -> str:
    """Lowercase, sin tildes, sin promo words, tokens >2 chars."""
    s = _strip_accents((titulo or '').lower())
    tokens = [
        t for t in re.findall(r'[a-z0-9]+', s)
        if len(t) > 2 and t not in _PROMO_WORDS
    ]
    return ' '.join(tokens)


def _tokens_diferencia(titulo_a_norm: str, titulo_b_norm: str) -> set[str]:
    """Tokens que están en uno y no en el otro."""
    sa = set(titulo_a_norm.split())
    sb = set(titulo_b_norm.split())
    return sa.symmetric_difference(sb)


def _diferencias_son_solo_variantes(diff: set[str]) -> bool:
    """True si todos los tokens de diferencia son talles/colores/números."""
    if not diff:
        return False  # idénticos → no es "solo variantes", es duplicado puro
    return all(
        t in _TOKENS_VARIANTE or _DIGITOS_RE.match(t)
        for t in diff
    )


# ── Pares ignorados ──────────────────────────────────────────────────────────

def _ruta_ignorados(alias: str, data_dir: str) -> str:
    safe_alias = alias.replace(' ', '_').replace('/', '-')
    return os.path.join(data_dir, f'duplicados_ignorados_{safe_alias}.json')


def cargar_pares_ignorados(alias: str, data_dir: str) -> list[dict]:
    """Lee la lista de pares marcados como ignorados por el usuario.

    Estructura del JSON:
      [{"mla_a": "MLA...", "mla_b": "MLA...", "marcado_en": "2026-05-02",
        "razon_opcional": "..."}]
    """
    ruta = _ruta_ignorados(alias, data_dir)
    if not os.path.exists(ruta):
        return []
    try:
        with open(ruta, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def marcar_par_ignorado(alias: str, data_dir: str, mla_a: str, mla_b: str,
                        razon: str = '') -> bool:
    """Agrega un par a la lista de ignorados. Idempotente — si ya existe, no duplica.

    Almacena los MLAs en orden alfabético dentro del par para canonicalizar.
    """
    a, b = sorted([mla_a.strip().upper(), mla_b.strip().upper()])
    if not a or not b or a == b:
        return False

    pares = cargar_pares_ignorados(alias, data_dir)
    for p in pares:
        if {p.get('mla_a'), p.get('mla_b')} == {a, b}:
            return True   # ya está, no duplicamos

    pares.append({
        'mla_a':           a,
        'mla_b':           b,
        'marcado_en':      datetime.now().strftime('%Y-%m-%d'),
        'razon_opcional':  (razon or '').strip(),
    })
    ruta = _ruta_ignorados(alias, data_dir)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(pares, f, ensure_ascii=False, indent=2)
    return True


def _es_par_ignorado(mla_a: str, mla_b: str, ignorados: list[dict]) -> bool:
    a, b = sorted([mla_a, mla_b])
    return any(
        {p.get('mla_a'), p.get('mla_b')} == {a, b}
        for p in ignorados
    )


# ── Trazabilidad de acciones automáticas ────────────────────────────────────

def registrar_accion_automatica(alias: str, data_dir: str, accion: str,
                                datos: dict) -> None:
    """Append-only log de acciones automáticas (pausar duplicados, etc).

    Estructura: [{timestamp, accion, ...datos}]
    """
    safe_alias = alias.replace(' ', '_').replace('/', '-')
    ruta = os.path.join(data_dir, f'acciones_automaticas_{safe_alias}.json')

    log: list = []
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding='utf-8') as f:
                log = json.load(f) or []
                if not isinstance(log, list):
                    log = []
        except Exception:
            log = []

    entry = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'accion':    accion,
        **datos,
    }
    log.append(entry)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


# ── Clusterización ───────────────────────────────────────────────────────────

def _generar_cluster_id(mla_list: Iterable[str]) -> str:
    """Hash determinístico de los MLAs ordenados — estable entre corridas."""
    key = '|'.join(sorted(mla_list))
    return hashlib.md5(key.encode()).hexdigest()[:8]


def _construir_clusters(items: list[dict], ignorados: list[dict]) -> list[list[dict]]:
    """N²/2 SequenceMatcher → unión por transitividad → lista de clusters."""
    norms = [(it.get('id', ''), _normalizar_titulo(it.get('titulo', '')), it)
             for it in items]
    n = len(norms)

    # parent[] para union-find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        id_i, norm_i, _ = norms[i]
        if not norm_i:
            continue
        for j in range(i + 1, n):
            id_j, norm_j, _ = norms[j]
            if not norm_j:
                continue
            if _es_par_ignorado(id_i, id_j, ignorados):
                continue
            ratio = SequenceMatcher(None, norm_i, norm_j).ratio()
            if ratio >= THRESHOLD_SIMILARIDAD:
                union(i, j)

    # agrupar por raíz
    grupos: dict[int, list[dict]] = {}
    for i in range(n):
        r = find(i)
        grupos.setdefault(r, []).append(norms[i][2])

    return [g for g in grupos.values() if len(g) >= 2]


# ── Clasificación de severidad ───────────────────────────────────────────────

def _clasificar_cluster(cluster_items: list[dict]) -> str:
    """Devuelve 'puro' | 'subperformante' | 'sano' según títulos y métricas."""
    norms = [_normalizar_titulo(it.get('titulo', '')) for it in cluster_items]

    # ¿Todos los títulos normalizados son idénticos? → duplicado puro
    if len(set(norms)) == 1:
        return 'puro'

    # ¿Las diferencias entre todos los pares son solo tokens variante?
    todas_son_variantes = True
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            diff = _tokens_diferencia(norms[i], norms[j])
            if not _diferencias_son_solo_variantes(diff):
                todas_son_variantes = False
                break
        if not todas_son_variantes:
            break

    if not todas_son_variantes:
        # Diferencias significativas pero ratio >0.85 → tratamos como puro
        # (productos casi iguales con cambios mínimos no-variante)
        return 'puro'

    # Es cluster de variantes — clasificar por métricas
    cuenta_con_ventas = sum(1 for it in cluster_items if (it.get('ventas_30d') or 0) > 0)
    total = len(cluster_items)
    visitas_sin_venta = sum(
        (it.get('visitas_30d') or 0)
        for it in cluster_items
        if (it.get('ventas_30d') or 0) == 0
    )

    # Sano: la mayoría vende (≥60%)
    if cuenta_con_ventas / total >= 0.60:
        return 'sano'
    # Subperformante: variantes con visitas perdiéndose
    if visitas_sin_venta >= 10:
        return 'subperformante'
    # Pocas visitas perdidas, pocas ventas → sano (no genera ruido)
    return 'sano'


def _identificar_ganadora(cluster_items: list[dict]) -> str:
    """Devuelve el id de la publicación con mayor ventas_30d (o más visitas si todas en 0)."""
    if not cluster_items:
        return ''
    return max(
        cluster_items,
        key=lambda it: (it.get('ventas_30d') or 0, it.get('visitas_30d') or 0),
    ).get('id', '')


def _calcular_impacto_monetario(cluster_items: list[dict], ganadora_id: str) -> tuple[int, float]:
    """Estima impacto: visitas_perdidas × conv_ganadora × precio_ganadora.

    Returns (visitas_perdidas_30d, impacto_estimado_en_pesos).
    """
    ganadora = next((it for it in cluster_items if it.get('id') == ganadora_id), None)
    if not ganadora:
        return 0, 0.0

    conv_g = float(ganadora.get('conversion_pct') or 0) / 100.0  # 1.5 → 0.015
    precio_g = float(ganadora.get('precio') or 0)

    visitas_perdidas = sum(
        int(it.get('visitas_30d') or 0)
        for it in cluster_items
        if it.get('id') != ganadora_id
    )

    impacto = visitas_perdidas * conv_g * precio_g
    return visitas_perdidas, round(impacto, 0)


def _titulo_corto(cluster_items: list[dict]) -> str:
    """Tokens en común entre todos los títulos del cluster — qué representa el cluster."""
    if not cluster_items:
        return ''
    sets = [set(_normalizar_titulo(it.get('titulo', '')).split()) for it in cluster_items]
    comunes = set.intersection(*sets) if sets else set()
    if not comunes:
        # Fallback: primeras 5 palabras del título de la primera
        primer = (cluster_items[0].get('titulo') or '').split()[:5]
        return ' '.join(primer)
    # Mantener orden del primer título
    primer_tokens = _normalizar_titulo(cluster_items[0].get('titulo', '')).split()
    ordenados = [t for t in primer_tokens if t in comunes][:6]
    return ' '.join(ordenados).title()


# ── API pública ──────────────────────────────────────────────────────────────

def detectar_duplicados(stock_items: list[dict], alias: str,
                        data_dir: str,
                        incluir_sanos: bool = False) -> list[Cluster]:
    """Detecta clusters de publicaciones duplicadas/canibalizándose.

    Args:
      stock_items: lista de items con campos id, titulo, precio, ventas_30d,
                   visitas_30d, conversion_pct.
      alias: alias de la cuenta (usado para cargar la lista de pares ignorados).
      data_dir: ruta a /data del proyecto.
      incluir_sanos: si False (default), filtra los clusters clasificados
                     como 'sano' antes de devolver. La UI no los muestra.

    Returns:
      Lista de Cluster ordenada por severidad (puro → subperformante → sano)
      y dentro de cada nivel por impacto_monetario_estimado descendente.
    """
    ignorados = cargar_pares_ignorados(alias, data_dir)
    grupos = _construir_clusters(stock_items, ignorados)

    clusters: list[Cluster] = []
    for grupo in grupos:
        severidad = _clasificar_cluster(grupo)
        if not incluir_sanos and severidad == 'sano':
            continue

        ganadora_id = _identificar_ganadora(grupo)
        visitas_perdidas, impacto = _calcular_impacto_monetario(grupo, ganadora_id)

        items_cluster = [
            ItemCluster(
                id=it.get('id', ''),
                titulo=it.get('titulo', ''),
                precio=float(it.get('precio') or 0),
                ventas_30d=int(it.get('ventas_30d') or 0),
                visitas_30d=int(it.get('visitas_30d') or 0),
                conversion_pct=float(it.get('conversion_pct') or 0),
                es_ganadora=(it.get('id') == ganadora_id),
            )
            for it in grupo
        ]
        # Ordenar dentro del cluster: ganadora primero, luego por ventas desc
        items_cluster.sort(key=lambda x: (not x.es_ganadora, -x.ventas_30d, -x.visitas_30d))

        cluster_id = _generar_cluster_id(it.id for it in items_cluster)
        clusters.append(Cluster(
            cluster_id=cluster_id,
            severidad=severidad,
            items=items_cluster,
            titulo_corto=_titulo_corto(grupo),
            visitas_perdidas_30d=visitas_perdidas,
            impacto_monetario_estimado=impacto,
        ))

    # Ordenar: puro primero, luego subperformante, dentro de cada nivel por impacto desc
    orden_severidad = {'puro': 0, 'subperformante': 1, 'sano': 2}
    clusters.sort(key=lambda c: (orden_severidad[c.severidad], -c.impacto_monetario_estimado))
    return clusters


def resumen_para_alertas(clusters: list[Cluster]) -> dict:
    """Devuelve resumen agregable al centro de alertas existente."""
    puros = [c for c in clusters if c.severidad == 'puro']
    subs  = [c for c in clusters if c.severidad == 'subperformante']
    impacto_total = sum(c.impacto_monetario_estimado for c in puros + subs)
    visitas_total = sum(c.visitas_perdidas_30d for c in puros + subs)
    return {
        'puros':          len(puros),
        'subperformantes': len(subs),
        'visitas_perdidas_30d': visitas_total,
        'impacto_monetario_estimado': impacto_total,
    }
