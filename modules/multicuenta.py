"""
Módulo 11 — Panel multicuenta e inteligencia cruzada

Dos funciones:

  RESUMEN CONSOLIDADO:
    Vista de todas las cuentas: publicaciones activas, alertas de stock,
    preguntas pendientes y estado de reputación. Una sola mirada para saber
    en qué cuenta hay que actuar.

  INTELIGENCIA CRUZADA:
    1. Auto-competencia: detecta si dos cuentas publican en la misma categoría
       (estás compitiendo contra vos mismo y ML puede penalizarte).
    2. Oportunidades cruzadas: productos que funcionan bien en una cuenta
       (alta venta) cuya categoría no existe en las otras cuentas.
       Sugerencia concreta: "publicalo también en Cuenta 2".

Uso:
  python main.py multicuenta               # resumen + inteligencia cruzada
  python main.py multicuenta resumen       # solo vista consolidada
  python main.py multicuenta cruzada       # solo inteligencia cruzada
"""

import json
import os
import time
from datetime import datetime, timedelta

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich import box

from core.account_manager import AccountManager
from modules.monitor_posicionamiento import _get_all_active_items

console = Console()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
BASE     = "https://api.mercadolibre.com"

# Mínimo de unidades vendidas para considerar un producto "ganador"
MIN_VENDIDOS_GANADOR = 5
# Máximo de oportunidades a mostrar por cuenta
MAX_OPORTUNIDADES = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(alias: str) -> str:
    return alias.replace(" ", "_").replace("/", "-")


def _load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _get_category_name(cat_id: str, token: str, cache: dict) -> str:
    if cat_id in cache:
        return cache[cat_id]
    try:
        resp = requests.get(
            f"{BASE}/categories/{cat_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=6,
        )
        name = resp.json().get("name", cat_id) if resp.ok else cat_id
    except Exception:
        name = cat_id
    cache[cat_id] = name
    return name


def _get_pending_questions(client) -> int:
    try:
        data = client._get(
            "/questions/search",
            params={"seller_id": client.account.user_id, "status": "UNANSWERED", "limit": 1},
        )
        return data.get("total", 0)
    except Exception:
        return -1


def _count_stock_alerts(alias: str) -> dict:
    snap = _load_json(os.path.join(DATA_DIR, f"stock_{_safe(alias)}.json"))
    if not snap:
        return {"sin_stock": 0, "criticos": 0, "fecha": None}
    items = snap.get("items", [])
    return {
        "sin_stock": sum(1 for i in items if i.get("alerta_stock") == "SIN_STOCK"),
        "criticos":  sum(1 for i in items if i.get("alerta_stock") == "CRITICO"),
        "fecha":     snap.get("fecha"),
    }


def _get_rep_status(alias: str) -> str:
    data = _load_json(os.path.join(DATA_DIR, f"reputacion_{_safe(alias)}.json"))
    if not data:
        return "[dim]sin datos[/dim]"
    latest = data[-1]
    umbrales = {"reclamos_pct": 2.0, "demoras_pct": 15.0, "cancelaciones_pct": 5.0}
    en_riesgo = any(latest.get(k, 0) > v for k, v in umbrales.items())
    if en_riesgo:
        return "[red]⚠ RIESGO[/red]"
    return "[green]✓ OK[/green]"


# ── Vista consolidada ─────────────────────────────────────────────────────────

def run_resumen(manager: AccountManager):
    accounts = [a for a in manager.list_accounts() if a.active]
    if not accounts:
        console.print("[yellow]No hay cuentas activas.[/yellow]")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(Rule(f"[bold cyan]Panel multicuenta — {now}[/bold cyan]"))
    console.print()

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("Cuenta",        min_width=18)
    table.add_column("Usuario",       min_width=12)
    table.add_column("Publicaciones", justify="right", min_width=13)
    table.add_column("Sin stock",     justify="right", min_width=10)
    table.add_column("Críticos",      justify="right", min_width=9)
    table.add_column("Preguntas",     justify="right", min_width=10)
    table.add_column("Reputación",    justify="center", min_width=12)

    for acc in accounts:
        client      = manager.get_client(acc.alias)
        stock_st    = _count_stock_alerts(acc.alias)
        rep_str     = _get_rep_status(acc.alias)
        preguntas   = _get_pending_questions(client)

        # Contar publicaciones activas (llamada rápida)
        try:
            data = client._get(
                f"/users/{acc.user_id}/items/search",
                params={"status": "active", "limit": 1},
            )
            total_pubs = data.get("paging", {}).get("total", "—")
        except Exception:
            total_pubs = "—"

        sin_stock_str = f"[red]{stock_st['sin_stock']}[/red]" if stock_st["sin_stock"] else "[dim]0[/dim]"
        criticos_str  = f"[yellow]{stock_st['criticos']}[/yellow]" if stock_st["criticos"] else "[dim]0[/dim]"
        preguntas_str = f"[yellow]{preguntas}[/yellow]" if preguntas > 0 else ("[dim]error[/dim]" if preguntas < 0 else "[green]0[/green]")

        table.add_row(
            acc.alias,
            acc.nickname or "—",
            str(total_pubs),
            sin_stock_str,
            criticos_str,
            preguntas_str,
            rep_str,
        )

    console.print(table)
    console.print()


# ── Inteligencia cruzada ───────────────────────────────────────────────────────

def _build_account_profile(client, alias: str) -> dict:
    """Trae todas las publicaciones activas de una cuenta y las analiza."""
    items = _get_all_active_items(client)
    categories = {}   # cat_id → list of items
    for item in items:
        cat = item.get("category_id", "")
        if cat:
            categories.setdefault(cat, []).append(item)

    # Top performers = más vendidos con ventas significativas
    top = sorted(
        [i for i in items if i.get("sold_quantity", 0) >= MIN_VENDIDOS_GANADOR],
        key=lambda x: x.get("sold_quantity", 0),
        reverse=True,
    )[:10]

    return {
        "alias":      alias,
        "items":      items,
        "categories": categories,
        "top":        top,
        "client":     client,
    }


def _detect_auto_competencia(profiles: list[dict], cat_cache: dict, token: str) -> list[dict]:
    """
    Detecta categorías donde más de una cuenta tiene publicaciones.
    Resultado: lista de {categoria, cuentas: [alias1, alias2]}
    """
    cat_to_accounts: dict[str, list[str]] = {}
    for p in profiles:
        for cat_id in p["categories"]:
            cat_to_accounts.setdefault(cat_id, [])
            if p["alias"] not in cat_to_accounts[cat_id]:
                cat_to_accounts[cat_id].append(p["alias"])

    conflictos = []
    for cat_id, aliases in cat_to_accounts.items():
        if len(aliases) >= 2:
            cat_name = _get_category_name(cat_id, token, cat_cache)
            conflictos.append({
                "cat_id":   cat_id,
                "cat_name": cat_name,
                "cuentas":  aliases,
                "total_pubs": sum(
                    len(p["categories"].get(cat_id, []))
                    for p in profiles
                    if p["alias"] in aliases
                ),
            })

    return sorted(conflictos, key=lambda x: -x["total_pubs"])


def _detect_oportunidades(profiles: list[dict], cat_cache: dict, token: str) -> list[dict]:
    """
    Para cada cuenta, busca sus top products y verifica cuáles otras cuentas
    no tienen esa categoría. Esas son oportunidades cruzadas.
    """
    oportunidades = []

    for origen in profiles:
        otras_cats = set()
        for otra in profiles:
            if otra["alias"] != origen["alias"]:
                otras_cats.update(otra["categories"].keys())

        for item in origen["top"]:
            cat_id  = item.get("category_id", "")
            vendidos = item.get("sold_quantity", 0)
            if not cat_id:
                continue

            # Cuentas destino que NO tienen esta categoría
            destinos = [
                p["alias"] for p in profiles
                if p["alias"] != origen["alias"] and cat_id not in p["categories"]
            ]
            if not destinos:
                continue

            cat_name = _get_category_name(cat_id, token, cat_cache)
            oportunidades.append({
                "cuenta_origen":  origen["alias"],
                "titulo":         item.get("title", "")[:55],
                "cat_id":         cat_id,
                "cat_name":       cat_name,
                "vendidos":       vendidos,
                "precio":         item.get("price", 0),
                "cuentas_destino": destinos,
            })
            time.sleep(0.1)

    # Ordenar por vendidos descendente
    return sorted(oportunidades, key=lambda x: -x["vendidos"])


def run_cruzada(manager: AccountManager):
    accounts = [a for a in manager.list_accounts() if a.active]
    if len(accounts) < 2:
        console.print("[yellow]La inteligencia cruzada requiere al menos 2 cuentas activas.[/yellow]")
        return

    console.print(Rule("[bold cyan]Inteligencia cruzada entre cuentas[/bold cyan]"))
    console.print()
    console.print(f"[dim]Cargando publicaciones de {len(accounts)} cuentas...[/dim]\n")

    # Obtener token de primera cuenta para calls públicas (categorías)
    first_client = manager.get_client(accounts[0].alias)
    first_client._ensure_token()
    token = first_client.account.access_token

    cat_cache: dict[str, str] = {}
    profiles = []
    for acc in accounts:
        client = manager.get_client(acc.alias)
        console.print(f"  → {acc.alias}...", end=" ")
        profile = _build_account_profile(client, acc.alias)
        console.print(f"[green]{len(profile['items'])} publicaciones, {len(profile['top'])} ganadores[/green]")
        profiles.append(profile)
        time.sleep(0.2)

    console.print()

    # ── Auto-competencia ──────────────────────────────────────────────────────
    conflictos = _detect_auto_competencia(profiles, cat_cache, token)

    if conflictos:
        lineas = ["Estas categorías tienen publicaciones en más de una cuenta:\n"]
        for c in conflictos:
            cuentas_str = " + ".join(c["cuentas"])
            lineas.append(
                f"  ⚠  [bold]{c['cat_name']}[/bold]\n"
                f"     Cuentas: [yellow]{cuentas_str}[/yellow]  |  "
                f"{c['total_pubs']} publicaciones en total\n"
                f"     → Revisá si ML está penalizando tu alcance en esta categoría."
            )
        console.print(Panel(
            "\n".join(lineas),
            title="[bold yellow]Auto-competencia detectada[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))
    else:
        console.print("[green]✓ Sin auto-competencia: tus cuentas operan en categorías distintas.[/green]\n")

    # ── Oportunidades cruzadas ────────────────────────────────────────────────
    oportunidades = _detect_oportunidades(profiles, cat_cache, token)

    if not oportunidades:
        console.print("[dim]Sin oportunidades cruzadas detectadas (todas las cuentas ya comparten categorías).[/dim]")
        return

    console.print(Rule("[bold green]Oportunidades cruzadas[/bold green]"))
    console.print(
        "[dim]Productos con buen desempeño en una cuenta cuya categoría "
        "no existe todavía en las otras.[/dim]\n"
    )

    # Agrupar por cuenta origen para mostrar claramente
    by_origin: dict[str, list] = {}
    for op in oportunidades:
        by_origin.setdefault(op["cuenta_origen"], []).append(op)

    for alias, ops in by_origin.items():
        table = Table(
            title=f"Ganadores de {alias} → oportunidades para otras cuentas",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold dim",
            expand=True,
        )
        table.add_column("Producto",        ratio=3, no_wrap=True)
        table.add_column("Categoría",       ratio=2, no_wrap=True)
        table.add_column("Vendidos",        justify="right", min_width=9)
        table.add_column("Precio",          justify="right", min_width=10)
        table.add_column("Publicar en",     ratio=1)

        for op in ops[:MAX_OPORTUNIDADES]:
            destinos_str = ", ".join(op["cuentas_destino"])
            table.add_row(
                op["titulo"],
                op["cat_name"],
                str(op["vendidos"]),
                f"${op['precio']:,.0f}",
                f"[cyan]{destinos_str}[/cyan]",
            )

        console.print(table)
        console.print()


# ── Entry point ───────────────────────────────────────────────────────────────

def run(manager: AccountManager, modo: str | None = None):
    """
    Args:
        modo: "resumen", "cruzada", o None para ambos
    """
    if modo in (None, "resumen"):
        run_resumen(manager)

    if modo in (None, "cruzada"):
        run_cruzada(manager)
