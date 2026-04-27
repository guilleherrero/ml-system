"""
Módulo 1 — Monitor de posicionamiento
Detecta cuáles publicaciones bajaron de posición en ML y genera alertas.
"""

import os
from datetime import datetime, timedelta
from typing import Optional
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.db_storage import db_load, db_save

import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from core.ml_client import MLClient

console = Console()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SEARCH_PAGES = 6       # busca hasta 300 resultados (6 x 50) via API oficial
ALERT_DROP = 3         # alerta si cae más de 3 posiciones
NOT_FOUND_POS = 999    # posición asignada si no aparece en los primeros 300


def _data_path(account_alias: str) -> str:
    safe = account_alias.replace(" ", "_").replace("/", "-")
    return os.path.join(DATA_DIR, f"posiciones_{safe}.json")


def _load_snapshots(alias: str) -> dict:
    path = _data_path(alias)
    data = db_load(path)
    return data if data is not None else {}


_POS_RETENTION_DAYS = 60


def _trim_pos_history(data: dict) -> dict:
    """Elimina entradas de historial con más de 90 días de antigüedad."""
    cutoff = (datetime.now() - timedelta(days=_POS_RETENTION_DAYS)).strftime('%Y-%m-%d')
    for item in data.values():
        hist = item.get('history')
        if isinstance(hist, dict):
            item['history'] = {d: v for d, v in hist.items() if d >= cutoff}
    return data


def _save_snapshots(alias: str, data: dict):
    db_save(_data_path(alias), _trim_pos_history(data))


def _search_position(item_id: str, category_id: str, keywords: str,
                     token: str = "") -> int:
    """Busca el item usando la API oficial de ML y devuelve su posición (1-based). 999 = no encontrado."""
    if not token:
        return NOT_FOUND_POS
    headers = {'Authorization': f'Bearer {token}'}
    page_size = 50
    for page in range(SEARCH_PAGES):
        offset = page * page_size
        try:
            resp = requests.get(
                'https://api.mercadolibre.com/sites/MLA/search',
                headers=headers,
                params={'q': keywords, 'limit': page_size, 'offset': offset},
                timeout=10,
            )
            if not resp.ok:
                break
            results = resp.json().get('results', [])
            for i, item in enumerate(results):
                if item.get('id') == item_id:
                    return offset + i + 1
            if len(results) < page_size:
                break
            import time as _t; _t.sleep(0.2)
        except requests.RequestException:
            break
    return NOT_FOUND_POS


_AUTOSUGGEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Referer':    'https://www.mercadolibre.com.ar/',
    'Origin':     'https://www.mercadolibre.com.ar',
    'Accept':     'application/json',
}


def _extract_keywords(title: str, max_words: int = 4) -> str:
    """Extrae palabras clave cortas del título para consultar el autosuggest."""
    stopwords = {"de", "para", "con", "sin", "y", "el", "la", "los", "las", "un", "una", "-", "–"}
    words = [w for w in title.lower().split() if w not in stopwords and w.isalpha()]
    return " ".join(words[:max_words])


def _get_best_keyword(title: str) -> str:
    """
    Consulta el autosuggest de ML con las palabras clave del título
    y devuelve la sugerencia más popular (primera). Si falla, usa
    las palabras del título directamente.
    """
    seed = _extract_keywords(title, max_words=4)
    try:
        resp = requests.get(
            'https://http2.mlstatic.com/resources/sites/MLA/autosuggest',
            params={'q': seed, 'limit': 5, 'lang': 'es_AR'},
            headers=_AUTOSUGGEST_HEADERS,
            timeout=6,
        )
        if resp.ok:
            suggestions = resp.json().get('suggested_queries', [])
            if suggestions:
                return suggestions[0].get('q', seed)
    except Exception:
        pass
    return seed


def _get_all_active_items(client: MLClient) -> list[dict]:
    """Trae todos los ítems activos paginando."""
    items = []
    offset = 0
    limit = 50
    while True:
        data = client.get_my_listings(limit=limit, offset=offset, status="active")
        ids = data.get("results", [])
        if not ids:
            break
        # Traer detalles en lote (hasta 20 por request) con auth
        for batch_start in range(0, len(ids), 20):
            batch = ids[batch_start:batch_start + 20]
            entries = client._get("/items", params={"ids": ",".join(batch)})
            for entry in entries:
                item = entry.get("body", {})
                if item.get("status") == "active":
                    items.append(item)
        offset += limit
        if offset >= data.get("paging", {}).get("total", 0):
            break
    return items


def _posicion_label(pos: int) -> str:
    if pos == NOT_FOUND_POS:
        return "[dim]>200[/dim]"
    return str(pos)


def _delta_label(hoy: int, ayer: Optional[int]) -> str:
    if ayer is None:
        return "[dim]nuevo[/dim]"
    if hoy == NOT_FOUND_POS and ayer == NOT_FOUND_POS:
        return "[dim]—[/dim]"
    if hoy == NOT_FOUND_POS:
        return "[red]↓ desapareció[/red]"
    if ayer == NOT_FOUND_POS:
        return "[green]↑ apareció[/green]"
    delta = ayer - hoy  # positivo = mejoró (número más bajo = mejor posición)
    if delta > 0:
        return f"[green]↑ {delta}[/green]"
    if delta < 0:
        return f"[red]↓ {abs(delta)}[/red]"
    return "[dim]= igual[/dim]"


def run(client: MLClient, alias: str, verbose: bool = True):
    """
    Ejecuta el monitor de posicionamiento para una cuenta.
    Toma un snapshot de hoy y lo compara con ayer y la semana pasada.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    last_week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    snapshots = _load_snapshots(alias)

    console.print(f"\n[bold cyan]Monitor de posicionamiento — {alias}[/bold cyan]")
    console.print(f"Fecha: {today}\n")

    # Obtener token para las búsquedas
    client._ensure_token()
    token = client.account.access_token

    # Traer publicaciones activas
    console.print("Obteniendo publicaciones activas...", end=" ")
    items = _get_all_active_items(client)
    console.print(f"[green]{len(items)} publicaciones[/green]")

    if not items:
        console.print("[yellow]No hay publicaciones activas.[/yellow]")
        return

    alerts = []
    today_results = []

    for item in items:
        item_id = item["id"]
        title = item.get("title", "")
        category = item.get("category_id", "")

        console.print(f"  Chequeando [dim]{title[:50]}...[/dim]", end="\r")

        # Obtener la keyword más buscada para este producto via autosuggest
        keywords = _get_best_keyword(title)

        pos_hoy = _search_position(item_id, category, keywords, token=token)

        # Guardar snapshot de hoy (incluyendo la query usada)
        if item_id not in snapshots:
            snapshots[item_id] = {"title": title, "category": category, "history": {}}
        snapshots[item_id]["history"][today] = pos_hoy
        snapshots[item_id]["title"]    = title
        snapshots[item_id]["keyword"]  = keywords   # query real usada

        pos_ayer = snapshots[item_id]["history"].get(yesterday)
        pos_semana = snapshots[item_id]["history"].get(last_week)

        today_results.append({
            "id": item_id,
            "title": title,
            "keyword": keywords,
            "pos_hoy": pos_hoy,
            "pos_ayer": pos_ayer,
            "pos_semana": pos_semana,
        })

        # Detectar alertas
        if pos_ayer is not None and pos_hoy != NOT_FOUND_POS:
            drop = pos_hoy - pos_ayer  # positivo = cayó (número más alto = peor)
            if drop >= ALERT_DROP:
                alerts.append((title, pos_ayer, pos_hoy, drop))
        elif pos_ayer is not None and pos_hoy == NOT_FOUND_POS:
            alerts.append((title, pos_ayer, NOT_FOUND_POS, 999))

    _save_snapshots(alias, snapshots)

    # ── Tabla de resultados ─────────────────────────────────────────────────
    console.print(" " * 80, end="\r")  # limpiar línea de progreso

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("Publicación", no_wrap=True, ratio=3)
    table.add_column("Búsqueda usada", no_wrap=True, ratio=2, style="dim")
    table.add_column("Hoy", justify="right", min_width=6)
    table.add_column("Ayer", justify="right", min_width=6)
    table.add_column("Δ", justify="center", min_width=12)
    table.add_column("7 días", justify="right", min_width=6)

    # Ordenar: primero los encontrados (por posición), luego los no encontrados
    today_results.sort(key=lambda x: (
        x["pos_hoy"] if x["pos_hoy"] != NOT_FOUND_POS else 10000
    ))

    for r in today_results:
        title_short = r["title"][:49] + "…" if len(r["title"]) > 49 else r["title"]
        kw_short    = r.get("keyword", "")[:35] + "…" if len(r.get("keyword","")) > 35 else r.get("keyword","")
        table.add_row(
            title_short,
            kw_short,
            _posicion_label(r["pos_hoy"]),
            _posicion_label(r["pos_ayer"]) if r["pos_ayer"] else "[dim]—[/dim]",
            _delta_label(r["pos_hoy"], r["pos_ayer"]),
            _posicion_label(r["pos_semana"]) if r["pos_semana"] else "[dim]—[/dim]",
        )

    console.print(table)

    # ── Resumen ──────────────────────────────────────────────────────────────
    found = [r for r in today_results if r["pos_hoy"] != NOT_FOUND_POS]
    top10  = sum(1 for r in found if r["pos_hoy"] <= 10)
    top50  = sum(1 for r in found if r["pos_hoy"] <= 50)
    top100 = sum(1 for r in found if r["pos_hoy"] <= 100)
    top300 = len(found)
    not_found = len(today_results) - top300

    summary = (
        f"[bold]Resumen — {alias}[/bold]\n\n"
        f"  [green]Top 10 :[/green]  {top10} publicaciones\n"
        f"  [cyan]Top 50 :[/cyan]  {top50} publicaciones\n"
        f"  [blue]Top 100:[/blue]  {top100} publicaciones\n"
        f"  [dim]Top 300:[/dim]  {top300} publicaciones\n"
        f"  [red]Fuera de top 300:[/red]  {not_found} publicaciones"
    )
    console.print(Panel(summary, border_style="dim", padding=(0, 2)))

    # ── Alertas ─────────────────────────────────────────────────────────────
    if alerts:
        alert_lines = []
        for title, pos_ant, pos_act, drop in alerts:
            if pos_act == NOT_FOUND_POS:
                alert_lines.append(f"[red]⚠ {title[:55]} — desapareció del top 300 (era #{pos_ant})[/red]")
            else:
                alert_lines.append(f"[red]⚠ {title[:55]} — cayó de #{pos_ant} a #{pos_act} (−{drop} posiciones)[/red]")
        console.print(Panel(
            "\n".join(alert_lines),
            title="[bold red]Alertas de posicionamiento[/bold red]",
            border_style="red",
        ))
    else:
        console.print("[green]✓ Sin caídas de posición detectadas hoy.[/green]")

    return today_results
