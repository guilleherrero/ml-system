"""
Módulo Dashboard — Vista de salud general de todas las cuentas.

Lee los snapshots locales (sin llamadas API masivas) más una llamada
rápida por cuenta para contar preguntas pendientes.

Muestra por cuenta:
  - Preguntas sin responder (API)
  - Estado de reputación (último snapshot)
  - Alertas de stock (último snapshot)
  - Caídas de posición hoy vs ayer (snapshot del día)
"""

import json
import os
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich import box

from core.account_manager import AccountManager

console = Console()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

NOT_FOUND_POS = 999
ALERT_DROP = 3


# ── Helpers de carga ──────────────────────────────────────────────────────────

def _load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _safe(alias: str) -> str:
    return alias.replace(" ", "_").replace("/", "-")


# ── Datos por módulo ──────────────────────────────────────────────────────────

def _get_pending_questions_count(client) -> int:
    """Una sola llamada con limit=1 para obtener el total de preguntas sin responder."""
    try:
        data = client._get(
            "/questions/search",
            params={
                "seller_id": client.account.user_id,
                "status": "UNANSWERED",
                "limit": 1,
                "offset": 0,
            },
        )
        return data.get("total", 0)
    except Exception:
        return -1


def _rep_status(snapshots: list | None) -> dict:
    if not snapshots:
        return {"ok": None, "nivel": "—", "alertas": [], "fecha": None}

    latest = snapshots[-1]
    umbrales = {
        "reclamos_pct":      2.0,
        "demoras_pct":       15.0,
        "cancelaciones_pct": 5.0,
    }
    nombres = {
        "reclamos_pct":      "reclamos",
        "demoras_pct":       "demoras",
        "cancelaciones_pct": "cancelaciones",
    }
    alertas = [nombres[k] for k, limite in umbrales.items() if latest.get(k, 0) > limite]

    nivel_map = {
        "1_verde":    "Nuevo",
        "2_amarillo": "Nivel 2",
        "3_naranja":  "Nivel 3",
        "4_rojo":     "Nivel 4",
        "5_plateado": "Platinum",
    }
    return {
        "ok":     len(alertas) == 0,
        "nivel":  nivel_map.get(latest.get("nivel", ""), latest.get("nivel", "—")),
        "alertas": alertas,
        "fecha":  latest.get("fecha", "—"),
    }


def _stock_status(snapshot: dict | None) -> dict:
    if not snapshot:
        return {"sin_stock": 0, "criticos": 0, "margen_neg": 0, "fecha": None}
    items = snapshot.get("items", [])
    return {
        "sin_stock":  sum(1 for i in items if i.get("alerta_stock") == "SIN_STOCK"),
        "criticos":   sum(1 for i in items if i.get("alerta_stock") == "CRITICO"),
        "margen_neg": sum(1 for i in items if i.get("alerta_margen") == "NEGATIVO"),
        "fecha":      snapshot.get("fecha"),
    }


def _posiciones_status(snapshots: dict | None) -> dict:
    if not snapshots:
        return {"bajaron": 0, "desaparecieron": 0, "total": 0, "fecha": None}

    today     = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    bajaron = desaparecieron = total = 0
    ultima_fecha = None

    for item_data in snapshots.values():
        history = item_data.get("history", {})
        pos_hoy = history.get(today)
        if pos_hoy is None:
            continue
        total += 1

        # Fecha más reciente registrada
        for fecha in sorted(history.keys(), reverse=True):
            if ultima_fecha is None or fecha > ultima_fecha:
                ultima_fecha = fecha
            break

        pos_ayer = history.get(yesterday)
        if pos_ayer is None:
            continue

        if pos_hoy == NOT_FOUND_POS and pos_ayer != NOT_FOUND_POS:
            desaparecieron += 1
        elif pos_hoy != NOT_FOUND_POS and (pos_hoy - pos_ayer) >= ALERT_DROP:
            bajaron += 1

    return {
        "bajaron":       bajaron,
        "desaparecieron": desaparecieron,
        "total":         total,
        "fecha":         ultima_fecha,
    }


# ── Panel por cuenta ──────────────────────────────────────────────────────────

def _render_account(acc, manager: AccountManager):
    safe = _safe(acc.alias)

    rep_data   = _load_json(os.path.join(DATA_DIR, f"reputacion_{safe}.json"))
    stock_data = _load_json(os.path.join(DATA_DIR, f"stock_{safe}.json"))
    pos_data   = _load_json(os.path.join(DATA_DIR, f"posiciones_{safe}.json"))

    rep_st   = _rep_status(rep_data)
    stock_st = _stock_status(stock_data)
    pos_st   = _posiciones_status(pos_data)

    try:
        client    = manager.get_client(acc.alias)
        preguntas = _get_pending_questions_count(client)
    except Exception:
        preguntas = -1

    lines = []

    # Preguntas
    if preguntas < 0:
        lines.append("  [dim]❓ Preguntas:   error al conectar[/dim]")
    elif preguntas == 0:
        lines.append("  [green]✓ Preguntas:   sin pendientes[/green]")
    else:
        lines.append(f"  [yellow]❓ Preguntas:   [bold]{preguntas}[/bold] sin responder  →  python main.py preguntas[/yellow]")

    # Reputación
    if rep_st["ok"] is None:
        lines.append("  [dim]— Reputación:  sin datos (corré reputacion)[/dim]")
    elif rep_st["ok"]:
        lines.append(f"  [green]✓ Reputación:  {rep_st['nivel']} — indicadores OK[/green]  [dim]({rep_st['fecha']})[/dim]")
    else:
        alertas_str = ", ".join(rep_st["alertas"])
        lines.append(f"  [red]⚠ Reputación:  {rep_st['nivel']} — RIESGO: {alertas_str}[/red]  [dim]({rep_st['fecha']})[/dim]")

    # Stock
    if stock_st["fecha"] is None:
        lines.append("  [dim]— Stock:        sin datos (corré stock-rentabilidad)[/dim]")
    else:
        partes = []
        if stock_st["sin_stock"]:
            partes.append(f"[red bold]{stock_st['sin_stock']} sin stock[/red bold]")
        if stock_st["criticos"]:
            partes.append(f"[red]{stock_st['criticos']} críticos (<7 días)[/red]")
        if stock_st["margen_neg"]:
            partes.append(f"[red]{stock_st['margen_neg']} margen negativo[/red]")

        if partes:
            lines.append(f"  [yellow]⚠ Stock:        {', '.join(partes)}[/yellow]  [dim]({stock_st['fecha']})[/dim]")
        else:
            lines.append(f"  [green]✓ Stock:        sin alertas[/green]  [dim]({stock_st['fecha']})[/dim]")

    # Posiciones
    if pos_st["total"] == 0:
        lines.append("  [dim]— Posiciones:  sin datos de hoy (corré posiciones)[/dim]")
    else:
        pos_partes = []
        if pos_st["desaparecieron"]:
            pos_partes.append(f"[red]{pos_st['desaparecieron']} desaparecieron[/red]")
        if pos_st["bajaron"]:
            pos_partes.append(f"[yellow]{pos_st['bajaron']} bajaron ≥{ALERT_DROP} lugares[/yellow]")

        if pos_partes:
            lines.append(f"  [yellow]↓ Posiciones:  {', '.join(pos_partes)}[/yellow]  [dim]({pos_st['fecha']})[/dim]")
        else:
            lines.append(
                f"  [green]✓ Posiciones:  {pos_st['total']} publicaciones monitoreadas, sin caídas[/green]"
                f"  [dim]({pos_st['fecha']})[/dim]"
            )

    title = f"[bold]{acc.alias}[/bold]"
    if acc.nickname:
        title += f"  [dim]{acc.nickname}[/dim]"

    console.print(Panel(
        "\n".join(lines),
        title=title,
        border_style="cyan",
        padding=(0, 1),
    ))


# ── Entry point ───────────────────────────────────────────────────────────────

def run(manager: AccountManager):
    """Muestra el dashboard de salud de todas las cuentas activas."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(f"\n[bold cyan]Dashboard — {now}[/bold cyan]\n")

    accounts = [a for a in manager.list_accounts() if a.active]
    if not accounts:
        console.print("[yellow]No hay cuentas activas.[/yellow]")
        return

    for acc in accounts:
        _render_account(acc, manager)

    console.print()
