"""
Módulo Historial — Tendencias de métricas a lo largo del tiempo.

Lee los snapshots acumulados por los módulos 1 y 6 y muestra evolución.

Sub-comandos:
  reputacion  — Evolución de reclamos, demoras y cancelaciones (hasta 30 registros)
  posiciones  — Evolución de posición por publicación (historial diario)
  (ninguno)   — Muestra ambos
"""

import json
import os
from datetime import datetime, timedelta

from rich.console import Console
from rich.table import Table
from rich.rule import Rule
from rich import box

console = Console()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

NOT_FOUND_POS = 999


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _safe(alias: str) -> str:
    return alias.replace(" ", "_").replace("/", "-")


def _semaforo(val: float, verde: float, amarillo: float) -> str:
    if val > amarillo:
        return f"[red]{val:.1f}%[/red]"
    if val > verde:
        return f"[yellow]{val:.1f}%[/yellow]"
    return f"[green]{val:.1f}%[/green]"


def _tendencia(prev: float | None, curr: float | None, menor_es_mejor: bool = True) -> str:
    """Flecha de tendencia. Por defecto: menor es mejor (métricas de riesgo)."""
    if prev is None or curr is None:
        return "[dim]—[/dim]"
    delta = curr - prev
    if delta == 0:
        return "[dim]=[/dim]"
    mejora = delta < 0 if menor_es_mejor else delta > 0
    arrow  = "↓" if delta < 0 else "↑"
    color  = "green" if mejora else "red"
    return f"[{color}]{arrow} {abs(delta):.1f}[/{color}]"


# ── Historial de reputación ───────────────────────────────────────────────────

def run_reputacion(alias: str):
    path = os.path.join(DATA_DIR, f"reputacion_{_safe(alias)}.json")
    data = _load_json(path)

    if not data:
        console.print(
            f"[yellow]Sin datos de reputación para '{alias}'. "
            f"Corré 'python main.py reputacion' primero.[/yellow]"
        )
        return

    console.print(Rule(f"[bold cyan]Historial de reputación — {alias}[/bold cyan]"))
    console.print(f"[dim]{len(data)} registros guardados[/dim]\n")

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("Fecha",            min_width=17)
    table.add_column("Nivel",            min_width=10)
    table.add_column("Reclamos",         justify="right", min_width=10)
    table.add_column("Δ",               justify="center", min_width=8)
    table.add_column("Demoras",          justify="right", min_width=10)
    table.add_column("Δ",               justify="center", min_width=8)
    table.add_column("Cancelaciones",    justify="right", min_width=13)
    table.add_column("Δ",               justify="center", min_width=8)

    prev = None
    for entry in data:
        rec = entry.get("reclamos_pct", 0)
        dem = entry.get("demoras_pct", 0)
        can = entry.get("cancelaciones_pct", 0)

        table.add_row(
            entry.get("fecha", "—"),
            entry.get("nivel", "—"),
            _semaforo(rec, 1.0, 2.0),
            _tendencia(prev.get("reclamos_pct") if prev else None, rec),
            _semaforo(dem, 10.0, 15.0),
            _tendencia(prev.get("demoras_pct") if prev else None, dem),
            _semaforo(can, 2.0, 5.0),
            _tendencia(prev.get("cancelaciones_pct") if prev else None, can),
        )
        prev = entry

    console.print(table)
    console.print()


# ── Historial de posiciones ───────────────────────────────────────────────────

def run_posiciones(alias: str, dias: int = 7):
    path = os.path.join(DATA_DIR, f"posiciones_{_safe(alias)}.json")
    data = _load_json(path)

    if not data:
        console.print(
            f"[yellow]Sin datos de posiciones para '{alias}'. "
            f"Corré 'python main.py posiciones' primero.[/yellow]"
        )
        return

    # Todas las fechas disponibles en el historial
    all_dates: set[str] = set()
    for item_data in data.values():
        all_dates.update(item_data.get("history", {}).keys())

    if not all_dates:
        console.print("[yellow]El archivo existe pero no tiene historial registrado.[/yellow]")
        return

    # Últimos `dias` días con datos
    sorted_dates = sorted(all_dates)[-dias:]

    console.print(Rule(f"[bold cyan]Historial de posiciones — {alias}[/bold cyan]"))
    console.print(
        f"[dim]{len(sorted_dates)} días con datos: "
        f"{sorted_dates[0]} → {sorted_dates[-1]}[/dim]\n"
    )

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("Publicación", ratio=3, no_wrap=True)
    for fecha in sorted_dates:
        table.add_column(fecha[5:], justify="right", min_width=6)  # MM-DD
    table.add_column("Tendencia", justify="center", min_width=13)

    def _pos_cell(pos: int | None) -> str:
        if pos is None:
            return "[dim]—[/dim]"
        if pos == NOT_FOUND_POS:
            return "[dim]>200[/dim]"
        if pos <= 10:
            return f"[green]{pos}[/green]"
        if pos <= 50:
            return str(pos)
        return f"[dim]{pos}[/dim]"

    def _latest_real_pos(item_data: dict) -> int:
        history = item_data.get("history", {})
        for d in reversed(sorted_dates):
            p = history.get(d)
            if p is not None:
                return p if p != NOT_FOUND_POS else 9999
        return 9999

    items_sorted = sorted(data.items(), key=lambda kv: _latest_real_pos(kv[1]))

    for _item_id, item_data in items_sorted:
        title   = item_data.get("title", _item_id)[:45]
        history = item_data.get("history", {})

        cells     = [_pos_cell(history.get(d)) for d in sorted_dates]
        positions = [history[d] for d in sorted_dates if d in history]

        # Tendencia: primer dato disponible vs último (posición más baja = mejor)
        real_positions = [p for p in positions if p != NOT_FOUND_POS]
        if len(real_positions) >= 2:
            delta = real_positions[0] - real_positions[-1]  # positivo = mejoró
            if delta > 0:
                tendencia = f"[green]↑ +{delta}[/green]"
            elif delta < 0:
                tendencia = f"[red]↓ {delta}[/red]"
            else:
                tendencia = "[dim]= igual[/dim]"
        else:
            tendencia = "[dim]—[/dim]"

        table.add_row(title, *cells, tendencia)

    console.print(table)
    console.print()


# ── Entry point ───────────────────────────────────────────────────────────────

def run(alias: str, modulo: str | None = None, dias: int = 7):
    """
    Args:
        alias:  Alias de la cuenta (requerido)
        modulo: "reputacion", "posiciones", o None para ambos
        dias:   Días a mostrar en el historial de posiciones (default: 7)
    """
    if modulo in (None, "reputacion"):
        run_reputacion(alias)

    if modulo in (None, "posiciones"):
        run_posiciones(alias, dias=dias)
