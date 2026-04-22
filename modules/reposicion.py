"""
Módulo 10 — Reposición inteligente desde China

Calcula cuándo pedir, cuánto pedir y qué no pedir.
Combina: stock total (Full + depósito propio) + velocidad de ventas + tiempo de tránsito.

Clasificaciones:
  PEDIR_YA     — si no pedís esta semana, vas a tener quiebre antes de que llegue el envío
  PEDIR_PRONTO — tenés 1-2 semanas de margen, planificá el pedido
  OK           — stock suficiente para cubrir el tránsito con margen
  EVALUAR      — ventas bajaron >30% vs mes anterior, pensá antes de pedir
  NO_PEDIR     — sin ventas, categoría en caída o margen negativo

Guarda configuración en config/reposicion.json
"""

import json
import os
import time
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt, FloatPrompt
from rich.table import Table
from rich import box

from core.ml_client import MLClient
from modules.monitor_posicionamiento import _get_all_active_items
from modules.stock_rentabilidad import _load_costos, _calcular_margen

console = Console()
CONFIG_DIR  = os.path.join(os.path.dirname(__file__), "..", "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "reposicion.json")

DIAS_ANALISIS    = 30
DIAS_ANALISIS_V2 = 60    # para detectar tendencia bajista
MARGEN_SAFETY    = 1.3   # pedir stock para 30% más que la demanda proyectada


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {
            "transit_days_global": 25,
            "items": {}
        }
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _get_item_cfg(item_id: str, cfg: dict) -> dict:
    return cfg.get("items", {}).get(item_id, {})


def _transit_days(item_id: str, cfg: dict) -> int:
    return _get_item_cfg(item_id, cfg).get("transit_days", cfg.get("transit_days_global", 25))


def _deposito_stock(item_id: str, cfg: dict) -> int:
    """Stock en depósito propio (no en Full), configurado manualmente."""
    return _get_item_cfg(item_id, cfg).get("deposito_stock", 0)


# ── Velocidad de ventas ───────────────────────────────────────────────────────

def _get_velocity_period(item_id: str, client: MLClient, dias: int) -> float:
    """Unidades vendidas por día en los últimos `dias` días."""
    user_id = client.account.user_id
    fecha_desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-03:00")
    try:
        data = client._get("/orders/search", params={
            "seller": user_id,
            "item": item_id,
            "order.status": "paid",
            "order.date_created.from": fecha_desde,
            "limit": 50,
        })
        orders = data.get("results", [])
        total = sum(
            sum(oi.get("quantity", 0) for oi in o.get("order_items", []) if oi.get("item", {}).get("id") == item_id)
            for o in orders
        )
        return round(total / dias, 2)
    except Exception:
        return 0.0


# ── Análisis por producto ──────────────────────────────────────────────────────

def _analizar_item(item: dict, client: MLClient, cfg: dict, costos: dict) -> dict:
    item_id  = item["id"]
    titulo   = item.get("title", "")[:60]
    precio   = float(item.get("price", 0))
    stock_ml = item.get("available_quantity", 0)

    transit   = _transit_days(item_id, cfg)
    deposito  = _deposito_stock(item_id, cfg)
    stock_total = stock_ml + deposito

    # Velocidad período 1 (últimos 30d) y período 2 (últimos 60d) para detectar tendencia
    vel_30 = _get_velocity_period(item_id, client, DIAS_ANALISIS)
    time.sleep(0.15)
    vel_60 = _get_velocity_period(item_id, client, DIAS_ANALISIS_V2)
    time.sleep(0.15)

    # Tendencia: comparar primera mitad vs segunda mitad de los 60d
    # vel_60 = promedio de 60d, vel_30 = promedio de últimos 30d
    # Si vel_30 < vel_60 * 0.7 → bajando
    tendencia = "estable"
    if vel_60 > 0:
        ratio = vel_30 / vel_60 if vel_60 > 0 else 1
        if ratio < 0.7:
            tendencia = "bajando"
        elif ratio > 1.3:
            tendencia = "subiendo"

    # Días de stock con velocidad actual
    dias_stock = round(stock_total / vel_30, 1) if vel_30 > 0 else None

    # Margen
    costo_data = costos.get(item_id, {})
    costo = costo_data.get("costo") if costo_data else None
    listing_type  = item.get("listing_type_id", "gold_pro")
    free_shipping = item.get("shipping", {}).get("free_shipping", False) or item.get("shipping", {}).get("mode") == "me2"
    margen_data   = _calcular_margen(precio, costo, listing_type, free_shipping)

    # Cuánto pedir (unidades para cubrir tránsito + margen de seguridad)
    # Fórmula: (transit_days + 14 días buffer) * vel_30 * safety - stock_actual
    if vel_30 > 0:
        stock_objetivo = (transit + 14) * vel_30 * MARGEN_SAFETY
        unidades_pedir = max(0, round(stock_objetivo - stock_total))
    else:
        unidades_pedir = 0

    # Clasificación
    decision = _clasificar(
        stock_total, vel_30, dias_stock, transit,
        tendencia, margen_data["margen_pct"],
    )

    return {
        "id":           item_id,
        "titulo":       titulo,
        "precio":       precio,
        "stock_ml":     stock_ml,
        "deposito":     deposito,
        "stock_total":  stock_total,
        "vel_30":       vel_30,
        "vel_60":       vel_60,
        "tendencia":    tendencia,
        "dias_stock":   dias_stock,
        "transit":      transit,
        "margen_pct":   margen_data["margen_pct"],
        "unidades_pedir": unidades_pedir,
        "decision":     decision,
    }


def _clasificar(
    stock_total: int,
    vel: float,
    dias_stock: float | None,
    transit: int,
    tendencia: str,
    margen_pct: float | None,
) -> str:
    if vel == 0:
        return "NO_PEDIR"

    if margen_pct is not None and margen_pct < 0:
        return "NO_PEDIR"

    if tendencia == "bajando" and (dias_stock is None or dias_stock > transit * 2):
        return "EVALUAR"

    if dias_stock is None:
        return "OK"

    # Urgencia basada en días de stock vs transit
    if dias_stock <= transit:
        return "PEDIR_YA"
    if dias_stock <= transit + 14:
        return "PEDIR_PRONTO"
    return "OK"


# ── Presentación ──────────────────────────────────────────────────────────────

DECISION_LABELS = {
    "PEDIR_YA":    "[red bold]🚨 PEDIR YA[/red bold]",
    "PEDIR_PRONTO": "[yellow]⚡ PEDIR PRONTO[/yellow]",
    "EVALUAR":     "[cyan]🔍 EVALUAR[/cyan]",
    "NO_PEDIR":    "[dim]✋ NO PEDIR[/dim]",
    "OK":          "[green]✓ OK[/green]",
}


def _show_tabla(results: list[dict]):
    table = Table(
        title="Plan de reposición — China",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold",
        expand=True,
    )
    table.add_column("Publicación",   ratio=3, no_wrap=True)
    table.add_column("Stock ML",      justify="right", min_width=8)
    table.add_column("Depósito",      justify="right", min_width=8)
    table.add_column("Vel/día",       justify="right", min_width=7)
    table.add_column("Tendencia",     justify="center", min_width=10)
    table.add_column("Días stock",    justify="right", min_width=10)
    table.add_column("Tránsito",      justify="right", min_width=9)
    table.add_column("Pedir (u.)",    justify="right", min_width=9)
    table.add_column("Decisión",      justify="center", min_width=15)

    order = ["PEDIR_YA", "PEDIR_PRONTO", "EVALUAR", "OK", "NO_PEDIR"]
    results_sorted = sorted(results, key=lambda r: order.index(r["decision"]))

    for r in results_sorted:
        titulo  = r["titulo"][:42] + "…" if len(r["titulo"]) > 42 else r["titulo"]
        vel_str = f"{r['vel_30']:.1f}" if r["vel_30"] > 0 else "[dim]0[/dim]"

        if r["tendencia"] == "bajando":
            tend_str = "[red]↓ baja[/red]"
        elif r["tendencia"] == "subiendo":
            tend_str = "[green]↑ sube[/green]"
        else:
            tend_str = "[dim]= estable[/dim]"

        if r["dias_stock"] is None:
            dias_str = "[dim]—[/dim]"
        elif r["dias_stock"] <= r["transit"]:
            dias_str = f"[red bold]{r['dias_stock']:.0f}d[/red bold]"
        elif r["dias_stock"] <= r["transit"] + 14:
            dias_str = f"[yellow]{r['dias_stock']:.0f}d[/yellow]"
        else:
            dias_str = f"[green]{r['dias_stock']:.0f}d[/green]"

        pedir_str = str(r["unidades_pedir"]) if r["unidades_pedir"] > 0 else "[dim]—[/dim]"

        table.add_row(
            titulo,
            str(r["stock_ml"]),
            str(r["deposito"]) if r["deposito"] > 0 else "[dim]—[/dim]",
            vel_str,
            tend_str,
            dias_str,
            f"{r['transit']}d",
            pedir_str,
            DECISION_LABELS.get(r["decision"], r["decision"]),
        )

    console.print(table)


def _show_resumen(results: list[dict]):
    pedir_ya     = [r for r in results if r["decision"] == "PEDIR_YA"]
    pedir_pronto = [r for r in results if r["decision"] == "PEDIR_PRONTO"]
    evaluar      = [r for r in results if r["decision"] == "EVALUAR"]
    no_pedir     = [r for r in results if r["decision"] == "NO_PEDIR"]

    secciones = []

    if pedir_ya:
        secciones.append("[red bold]🚨 PEDÍ ESTA SEMANA — quiebre inminente:[/red bold]")
        for r in pedir_ya:
            dias_str = f"{r['dias_stock']:.0f}d de stock" if r["dias_stock"] else "sin ventas recientes"
            secciones.append(
                f"  • [bold]{r['titulo']}[/bold]  →  {r['unidades_pedir']} u.\n"
                f"    {dias_str} | tránsito: {r['transit']}d | vel: {r['vel_30']:.1f}/día"
            )

    if pedir_pronto:
        secciones.append("\n[yellow]⚡ PLANIFICÁ EL PEDIDO — margen de 1-2 semanas:[/yellow]")
        for r in pedir_pronto:
            secciones.append(
                f"  • {r['titulo']}  →  {r['unidades_pedir']} u.  "
                f"[dim]({r['dias_stock']:.0f}d stock, {r['transit']}d tránsito)[/dim]"
            )

    if evaluar:
        secciones.append("\n[cyan]🔍 EVALUÁ ANTES DE PEDIR — ventas bajaron >30%:[/cyan]")
        for r in evaluar:
            ratio = round(r["vel_30"] / r["vel_60"] * 100) if r["vel_60"] > 0 else 0
            secciones.append(f"  • {r['titulo']}  ({ratio}% de las ventas de hace 60d)")

    if no_pedir:
        secciones.append(f"\n[dim]✋ NO PEDIR ({len(no_pedir)} productos): sin ventas o margen negativo.[/dim]")

    if secciones:
        console.print(Panel(
            "\n".join(secciones),
            title="[bold]Plan de acción[/bold]",
            border_style="cyan",
            padding=(0, 2),
        ))


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup(client: MLClient, alias: str):
    """Configura tiempos de tránsito y stock en depósito propio."""
    console.print(f"\n[bold cyan]Configurar reposición — {alias}[/bold cyan]\n")

    cfg = _load_config()

    # Tránsito global
    transit_global = IntPrompt.ask(
        "Tiempo de tránsito global desde China (días)",
        default=cfg.get("transit_days_global", 25),
    )
    cfg["transit_days_global"] = transit_global

    # Por publicación
    items = _get_all_active_items(client)
    if not items:
        _save_config(cfg)
        return

    if Confirm.ask(f"\n¿Configurar tránsito y depósito individualmente para cada publicación?", default=False):
        console.print("[dim]Presioná Enter para usar el valor global o dejarlo sin cambios.[/dim]\n")
        for item in items:
            item_id = item["id"]
            titulo  = item.get("title", "")[:60]
            current = _get_item_cfg(item_id, cfg)
            current_transit  = current.get("transit_days", transit_global)
            current_deposito = current.get("deposito_stock", 0)

            console.print(f"  [bold]{titulo}[/bold]")
            t = Prompt.ask(f"  Tránsito (días, Enter={current_transit})", default=str(current_transit))
            d = Prompt.ask(f"  Stock en depósito propio (unidades, Enter={current_deposito})", default=str(current_deposito))

            try:
                t_val = int(t)
                d_val = int(d)
                cfg.setdefault("items", {})[item_id] = {
                    "transit_days":  t_val,
                    "deposito_stock": d_val,
                }
            except ValueError:
                pass

    _save_config(cfg)
    console.print(f"\n[green]✓ Configuración guardada en config/reposicion.json[/green]")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(client: MLClient, alias: str):
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(f"\n[bold cyan]Reposición desde China — {alias}[/bold cyan]")
    console.print(f"Fecha: {today}\n")

    cfg    = _load_config()
    costos = _load_costos()

    console.print("Obteniendo publicaciones activas...", end=" ")
    items = _get_all_active_items(client)
    if not items:
        console.print("[yellow]Sin publicaciones activas.[/yellow]")
        return
    console.print(f"[green]{len(items)} publicaciones[/green]")
    console.print(f"[dim]Calculando velocidad de ventas (30d y 60d) — esto puede tardar unos minutos...[/dim]\n")

    results = []
    for item in items:
        r = _analizar_item(item, client, cfg, costos)
        results.append(r)

    _show_tabla(results)
    _show_resumen(results)

    # Tip de configuración
    sin_config = [r for r in results if r["deposito"] == 0 and r["transit"] == cfg.get("transit_days_global", 25)]
    if sin_config:
        console.print(
            f"[dim]Tip: configurá tránsito y depósito propio por producto con "
            f"'python main.py reposicion-setup' para cálculos más precisos.[/dim]\n"
        )
