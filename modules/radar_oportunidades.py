"""
Módulo 8 — Radar de oportunidades y calendario de ventas

Parte A — Radar de nichos:
  Escanea categorías relacionadas a las tuyas buscando alta demanda + poca competencia.
  Toma las categorías donde publicás, busca las categorías hermanas (mismo nivel jerárquico)
  y evalúa cada una: cuánto se vende vs cuántos vendedores hay.
  Score de oportunidad = ventas promedio de los top items / cantidad de vendedores únicos.

Parte B — Calendario de ventas:
  Fechas comerciales clave de Argentina con conteo regresivo.
  Muestra acciones recomendadas según cuántos días faltan.
"""

import json
import os
import time
from datetime import datetime, timedelta, date

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich import box

from core.ml_client import MLClient
from modules.monitor_posicionamiento import _get_all_active_items

console = Console()

SITE = "MLA"
BASE = "https://api.mercadolibre.com"


# ── Calendario ────────────────────────────────────────────────────────────────

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Devuelve el n-ésimo día de la semana del mes. weekday: 0=lunes, 6=domingo."""
    d = date(year, month, 1)
    first_wd = d.weekday()
    delta = (weekday - first_wd) % 7
    target = d + timedelta(days=delta + (n - 1) * 7)
    return target


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Último día de la semana en el mes dado."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


def _build_calendario(year: int) -> list[dict]:
    """Genera las fechas comerciales clave para el año dado."""
    return [
        {
            "nombre":  "Día de los Enamorados",
            "fecha":   date(year, 2, 14),
            "icono":   "💝",
            "consejo": "Regalos, accesorios, indumentaria. Activá descuentos 10 días antes.",
        },
        {
            "nombre":  "Día de la Mujer",
            "fecha":   date(year, 3, 8),
            "icono":   "💜",
            "consejo": "Indumentaria, cuidado personal, accesorios. Publicaciones específicas.",
        },
        {
            "nombre":  "Hot Sale",
            "fecha":   _nth_weekday_of_month(year, 5, 0, 2),  # 2do lunes de mayo
            "icono":   "🔥",
            "consejo": "El evento más grande del año. Preparar stock 45 días antes. Activar descuentos.",
        },
        {
            "nombre":  "Día del Padre",
            "fecha":   _nth_weekday_of_month(year, 6, 6, 3),  # 3er domingo de junio
            "icono":   "👔",
            "consejo": "Herramientas, indumentaria, electrónica. Preparar stock 30 días antes.",
        },
        {
            "nombre":  "Día del Amigo",
            "fecha":   date(year, 7, 20),
            "icono":   "🤝",
            "consejo": "Regalos variados, juegos. Oportunidad de liquidar stock acumulado.",
        },
        {
            "nombre":  "Día del Niño",
            "fecha":   _nth_weekday_of_month(year, 8, 6, 2),  # 2do domingo de agosto
            "icono":   "🧸",
            "consejo": "Juguetes, ropa infantil, accesorios bebé. Mayor evento del 2do semestre.",
        },
        {
            "nombre":  "Día de la Madre",
            "fecha":   _nth_weekday_of_month(year, 10, 6, 3),  # 3er domingo de octubre
            "icono":   "💐",
            "consejo": "Accesorios, cuidado personal, ropa. Preparar stock y publicidad 3 semanas antes.",
        },
        {
            "nombre":  "Cyber Monday",
            "fecha":   _last_weekday_of_month(year, 11, 0),   # último lunes de noviembre
            "icono":   "💻",
            "consejo": "Segundo evento más grande. Precios agresivos. Stock asegurado.",
        },
        {
            "nombre":  "Navidad",
            "fecha":   date(year, 12, 24),
            "icono":   "🎄",
            "consejo": "Regalos en todas las categorías. Pico de ventas. Envíos Full recomendados.",
        },
    ]


def _acciones_por_dias(dias: int) -> str:
    if dias < 0:
        return "[dim]ya pasó[/dim]"
    if dias == 0:
        return "[red bold]¡HOY![/red bold]"
    if dias <= 7:
        return "[red]Revisá stock y precios AHORA[/red]"
    if dias <= 15:
        return "[yellow]Activar publicidad y revisar precios[/yellow]"
    if dias <= 30:
        return "[cyan]Preparar stock y optimizar publicaciones[/cyan]"
    if dias <= 60:
        return "[green]Planificar pedidos a proveedores[/green]"
    return "[dim]Sin acción inmediata[/dim]"


def _dias_label(dias: int) -> str:
    if dias < 0:
        return f"[dim]hace {abs(dias)}d[/dim]"
    if dias == 0:
        return "[red bold]HOY[/red bold]"
    if dias <= 7:
        return f"[red bold]{dias} días[/red bold]"
    if dias <= 15:
        return f"[red]{dias} días[/red]"
    if dias <= 30:
        return f"[yellow]{dias} días[/yellow]"
    return f"[green]{dias} días[/green]"


def run_calendario():
    today = date.today()
    year  = today.year

    console.print(Rule("[bold cyan]Calendario de ventas — Argentina[/bold cyan]"))
    console.print()

    # Construir fechas de este año y próximo para los que ya pasaron
    eventos = []
    for ev in _build_calendario(year):
        dias = (ev["fecha"] - today).days
        if dias < -7:
            # Ya pasó hace más de una semana → mostrar el del año próximo
            proximas = [e for e in _build_calendario(year + 1) if e["nombre"] == ev["nombre"]]
            if proximas:
                prox = proximas[0]
                dias = (prox["fecha"] - today).days
                eventos.append({**prox, "dias": dias})
            else:
                eventos.append({**ev, "dias": dias})
        else:
            eventos.append({**ev, "dias": dias})

    # Ordenar por días restantes
    eventos.sort(key=lambda e: e["dias"])

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("",          width=3)
    table.add_column("Evento",    min_width=22)
    table.add_column("Fecha",     min_width=12, justify="center")
    table.add_column("Faltan",    min_width=10, justify="right")
    table.add_column("Acción recomendada", ratio=2)

    for ev in eventos:
        dias = ev["dias"]
        table.add_row(
            ev["icono"],
            ev["nombre"],
            ev["fecha"].strftime("%d/%m/%Y"),
            _dias_label(dias),
            _acciones_por_dias(dias),
        )

    console.print(table)

    # Próximo evento importante (≤30 días)
    urgentes = [e for e in eventos if 0 <= e["dias"] <= 30]
    if urgentes:
        ev = urgentes[0]
        console.print(Panel(
            f"  {ev['icono']}  [bold]{ev['nombre']}[/bold] — {ev['dias']} días\n\n"
            f"  {ev['consejo']}",
            title="[bold yellow]⚡ Próximo evento importante[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))

    console.print()


# ── Radar de nichos ───────────────────────────────────────────────────────────

def _get_category_info(category_id: str, token: str) -> dict:
    resp = requests.get(
        f"{BASE}/categories/{category_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=8,
    )
    return resp.json() if resp.ok else {}


def _get_category_children(category_id: str, token: str) -> list[dict]:
    data = _get_category_info(category_id, token)
    return data.get("children_categories", [])


def _search_category_items(category_id: str, token: str, limit: int = 20) -> list[dict]:
    """Busca items en una categoría ordenados por relevancia. Requiere permiso búsqueda."""
    resp = requests.get(
        f"{BASE}/sites/{SITE}/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"category": category_id, "sort": "sold_quantity", "limit": limit},
        timeout=10,
    )
    if resp.ok:
        return resp.json().get("results", [])
    return []


def _score_category(items: list[dict]) -> dict:
    """
    Calcula score de oportunidad para una categoría.
    Score = ventas_promedio / vendedores_únicos
    Cuanto mayor, más demanda por vendedor → más oportunidad.
    """
    if not items:
        return {"score": 0, "avg_sold": 0, "sellers": 0, "avg_price": 0}

    sold_quantities = [i.get("sold_quantity", 0) for i in items]
    prices          = [i.get("price", 0) for i in items if i.get("price", 0) > 0]
    sellers         = {i.get("seller", {}).get("id") for i in items if i.get("seller")}

    avg_sold  = sum(sold_quantities) / len(sold_quantities) if sold_quantities else 0
    avg_price = sum(prices) / len(prices) if prices else 0
    n_sellers = len(sellers) if sellers else 1

    score = round(avg_sold / n_sellers, 1)
    return {
        "score":     score,
        "avg_sold":  round(avg_sold, 0),
        "sellers":   n_sellers,
        "avg_price": round(avg_price, 0),
    }


def run_radar(client: MLClient, alias: str, max_categorias: int = 5):
    """
    Escanea categorías relacionadas buscando alta demanda + poca competencia.

    Flujo:
      1. Toma las categorías donde el usuario ya publica
      2. Por cada categoría, sube un nivel jerárquico para encontrar las hermanas
      3. Evalúa cada categoría hermana (no usada) con un score de oportunidad
      4. Muestra el ranking de oportunidades
    """
    console.print(Rule("[bold cyan]Radar de oportunidades — " + alias + "[/bold cyan]"))
    console.print()

    client._ensure_token()
    token = client.account.access_token

    # Categorías del usuario
    console.print("Analizando tus categorías actuales...", end=" ")
    items = _get_all_active_items(client)
    if not items:
        console.print("[yellow]Sin publicaciones activas.[/yellow]")
        return

    my_categories = {i.get("category_id") for i in items if i.get("category_id")}
    console.print(f"[green]{len(my_categories)} categorías propias[/green]")

    # Por cada categoría propia, buscar categorías hermanas
    candidate_ids: dict[str, str] = {}   # cat_id → nombre
    checked_parents: set[str] = set()

    console.print("Explorando categorías relacionadas...", end=" ")
    for cat_id in list(my_categories)[:8]:   # limitar para no saturar API
        cat_info  = _get_category_info(cat_id, token)

        # ML a veces devuelve parent_id=None; usar path_from_root como fallback
        parent_id = cat_info.get("parent_id")
        if not parent_id:
            path = cat_info.get("path_from_root", [])
            if len(path) >= 2:
                parent_id = path[-2].get("id")

        if not parent_id or parent_id in checked_parents:
            continue
        checked_parents.add(parent_id)

        siblings = _get_category_children(parent_id, token)
        for sib in siblings:
            sib_id = sib.get("id", "")
            if sib_id and sib_id not in my_categories:
                candidate_ids[sib_id] = sib.get("name", sib_id)
        time.sleep(0.2)

    console.print(f"[green]{len(candidate_ids)} categorías candidatas[/green]")

    if not candidate_ids:
        console.print("[yellow]No se encontraron categorías relacionadas para analizar.[/yellow]")
        return

    # Intentar scoring con API de búsqueda
    test_cat   = next(iter(candidate_ids))
    test_items = _search_category_items(test_cat, token, limit=3)
    search_ok  = bool(test_items)

    scored: list[dict] = []

    if search_ok:
        console.print(f"\nScoreando {min(max_categorias * 3, len(candidate_ids))} categorías candidatas...\n")
        for cat_id, cat_name in list(candidate_ids.items())[:max_categorias * 3]:
            results = _search_category_items(cat_id, token, limit=20)
            if not results:
                time.sleep(0.2)
                continue
            sc = _score_category(results)
            if sc["avg_sold"] > 0:
                scored.append({
                    "id":           cat_id,
                    "nombre":       cat_name,
                    "score":        sc["score"],
                    "avg_sales":    int(sc["avg_sold"]),
                    "sellers_count": sc["sellers"],
                    "avg_price":    sc["avg_price"],
                    "sample_title": results[0].get("title", "")[:55] if results else "",
                })
            time.sleep(0.3)
        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:max_categorias]
    else:
        # Sin API de búsqueda: guardamos todas las candidatas sin score
        console.print("[yellow]Sin permiso de búsqueda — guardando categorías candidatas sin score.[/yellow]")
        for cat_id, cat_name in list(candidate_ids.items())[:50]:
            scored.append({
                "id":           cat_id,
                "nombre":       cat_name,
                "score":        0,
                "avg_sales":    None,
                "sellers_count": None,
                "avg_price":    None,
            })
        top = scored[:max_categorias * 4]  # más categorías ya que no hay ranking

    # Guardar resultados en JSON
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    safe_alias = alias.replace(' ', '_').replace('/', '-')
    out_path   = os.path.join(data_dir, f'radar_{safe_alias}.json')
    payload = {
        "fecha":       datetime.now().strftime('%Y-%m-%d %H:%M'),
        "alias":       alias,
        "con_score":   search_ok,
        "nichos":      top,
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    console.print(f"[dim]Guardado en {out_path}[/dim]")

    if not top:
        console.print("[yellow]No se pudo obtener datos para las categorías candidatas.[/yellow]")
        return

    # Mostrar tabla
    table = Table(
        title=f"{'Top ' + str(len(top)) + ' oportunidades' if search_ok else str(len(top)) + ' categorías relacionadas'} — no usás todavía",
        box=box.ROUNDED, show_header=True, header_style="bold", expand=True,
    )
    table.add_column("#",            width=3)
    table.add_column("Categoría",    ratio=2)
    if search_ok:
        table.add_column("Score",        justify="right", min_width=8)
        table.add_column("Vtas/pub",     justify="right", min_width=9)
        table.add_column("Vendedores",   justify="right", min_width=11)
        table.add_column("Precio prom.", justify="right", min_width=12)
        table.add_column("Ej. producto", ratio=2, no_wrap=True)
    else:
        table.add_column("ID", min_width=14)
        table.add_column("Nota", ratio=1)

    for i, sc in enumerate(top, 1):
        if search_ok:
            score_val = sc["score"]
            score_str = (f"[green bold]{score_val}[/green bold]" if score_val > 10
                         else f"[yellow]{score_val}[/yellow]" if score_val > 3
                         else str(score_val))
            table.add_row(str(i), sc["nombre"], score_str,
                          str(int(sc["avg_sales"] or 0)),
                          str(sc["sellers_count"] or "—"),
                          f"${sc['avg_price']:,.0f}" if sc.get("avg_price") else "—",
                          sc.get("sample_title", ""))
        else:
            table.add_row(str(i), sc["nombre"], sc["id"],
                          "[dim]Activá permiso para ver ventas[/dim]")

    console.print(table)
    if search_ok:
        console.print(
            "[dim]Score = ventas promedio / vendedores únicos. "
            "Mayor score = más demanda por competidor = mejor oportunidad.[/dim]\n"
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def run(client: MLClient, alias: str, modo: str | None = None, max_nichos: int = 5):
    """
    Args:
        modo:       "radar", "calendario", o None para ambos
        max_nichos: Cantidad de nichos a mostrar en el radar (default 5)
    """
    if modo in (None, "calendario"):
        run_calendario()

    if modo in (None, "radar"):
        run_radar(client, alias, max_categorias=max_nichos)
