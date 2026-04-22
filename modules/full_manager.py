"""
Módulo 9 — Gestión inteligente de Mercado Envíos Full

Detecta publicaciones que usan Mercado Envíos Full y las analiza en 3 dimensiones:

  ALERTAS CRÍTICAS:
    - Stock bajo en Full: queda menos stock que el tiempo de reposición (configurable)
    - Sin ventas recientes: stock parado que ML te cobra por almacenar

  ANÁLISIS SEMANAL:
    - Stock muerto: unidades disponibles pero sin ventas en N días
    - Candidatos a escalar: alta velocidad + buen margen + stock suficiente

  CONFIGURACIÓN:
    - Tiempo de reposición por producto (días hasta que el stock llega al centro Full)
    - Umbral de stock muerto (días sin ventas)
    - Costos para calcular margen real

Guarda configuración en config/full_config.json
"""

import json
import os
import time
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.table import Table
from rich import box

from core.ml_client import MLClient
from modules.monitor_posicionamiento import _get_all_active_items
from modules.stock_rentabilidad import _get_sales_velocity, _load_costos, _calcular_margen

console = Console()
CONFIG_DIR  = os.path.join(os.path.dirname(__file__), "..", "config")
DATA_DIR    = os.path.join(os.path.dirname(__file__), "..", "data")
CONFIG_PATH = os.path.join(CONFIG_DIR, "full_config.json")

# Costo de almacenamiento Full ML (ARS por unidad por día, aproximado)
COSTO_ALMACENAMIENTO_DIA = 12.0
DIAS_ANALISIS_VENTAS     = 30
DIAS_SIN_VENTA_MUERTO    = 21   # 3 semanas sin ventas → stock muerto por defecto


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"global_lead_days": 18, "items": {}}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _get_lead_days(item_id: str, cfg: dict) -> int:
    return cfg.get("items", {}).get(item_id, {}).get("lead_days", cfg.get("global_lead_days", 18))


# ── Detección de publicaciones Full ──────────────────────────────────────────

def _is_full(item: dict) -> bool:
    """Detecta si una publicación usa Mercado Envíos Full."""
    shipping = item.get("shipping", {})
    logistic = shipping.get("logistic_type", "")
    return logistic in ("fulfillment", "meli_fulfillment", "self_service_fulfillment")


def _get_full_items(client: MLClient) -> list[dict]:
    """Trae sólo las publicaciones activas que usan Full."""
    all_items = _get_all_active_items(client)
    full = [i for i in all_items if _is_full(i)]
    return full


# ── Análisis ──────────────────────────────────────────────────────────────────

def _analizar_item(item: dict, client: MLClient, cfg: dict, costos: dict) -> dict:
    item_id   = item["id"]
    titulo    = item.get("title", "")[:60]
    precio    = float(item.get("price", 0))
    stock     = item.get("available_quantity", 0)
    lead_days = _get_lead_days(item_id, cfg)

    velocidad = _get_sales_velocity(item_id, client)
    dias_stock = round(stock / velocidad, 1) if velocidad > 0 else None

    # Última venta (proxy: si velocidad > 0, hay ventas recientes)
    sin_ventas_dias = None
    if velocidad == 0:
        # Calcular días sin ventas buscando la última orden
        try:
            user_id = client.account.user_id
            fecha_desde = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00.000-03:00")
            data = client._get("/orders/search", params={
                "seller": user_id, "item": item_id,
                "order.status": "paid",
                "order.date_created.from": fecha_desde,
                "limit": 1,
                "sort": "date_desc",
            })
            orders = data.get("results", [])
            if orders:
                last_date = orders[0].get("date_created", "")[:10]
                delta = (datetime.now().date() - datetime.strptime(last_date, "%Y-%m-%d").date()).days
                sin_ventas_dias = delta
            else:
                sin_ventas_dias = 90  # sin ventas en 90 días
        except Exception:
            sin_ventas_dias = None

    # Margen
    costo_data = costos.get(item_id, {})
    costo = costo_data.get("costo") if costo_data else None
    listing_type = item.get("listing_type_id", "gold_pro")
    free_shipping = item.get("shipping", {}).get("free_shipping", True)  # Full siempre free
    margen_data = _calcular_margen(precio, costo, listing_type, free_shipping)

    # Clasificación
    alerta = None
    if stock == 0:
        alerta = "SIN_STOCK"
    elif dias_stock is not None and dias_stock <= lead_days:
        alerta = "REPONER_URGENTE"       # se agota antes de que llegue el próximo envío
    elif sin_ventas_dias is not None and sin_ventas_dias >= DIAS_SIN_VENTA_MUERTO and stock > 0:
        alerta = "STOCK_MUERTO"
    elif (
        velocidad > 0
        and margen_data["margen_pct"] is not None
        and margen_data["margen_pct"] >= 0.15
        and (dias_stock is None or dias_stock >= 30)
    ):
        alerta = "ESCALAR"

    # Costo de almacenamiento acumulado (solo para stock muerto)
    costo_almac = None
    if sin_ventas_dias is not None and stock > 0:
        costo_almac = round(stock * sin_ventas_dias * COSTO_ALMACENAMIENTO_DIA, 0)

    return {
        "id":             item_id,
        "titulo":         titulo,
        "precio":         precio,
        "stock":          stock,
        "velocidad":      velocidad,
        "dias_stock":     dias_stock,
        "lead_days":      lead_days,
        "sin_ventas_dias": sin_ventas_dias,
        "neto":           margen_data["neto"],
        "margen_pct":     margen_data["margen_pct"],
        "costo_almac":    costo_almac,
        "alerta":         alerta,
    }


# ── Presentación ──────────────────────────────────────────────────────────────

def _show_alertas_criticas(results: list[dict]):
    urgentes     = [r for r in results if r["alerta"] == "REPONER_URGENTE"]
    sin_stock    = [r for r in results if r["alerta"] == "SIN_STOCK"]
    stock_muerto = [r for r in results if r["alerta"] == "STOCK_MUERTO"]
    escalar      = [r for r in results if r["alerta"] == "ESCALAR"]

    if sin_stock or urgentes:
        lineas = []
        if sin_stock:
            lineas.append("[red bold]⛔ SIN STOCK EN FULL:[/red bold]")
            for r in sin_stock:
                lineas.append(f"   • {r['titulo']}")
        if urgentes:
            lineas.append("\n[red]🚨 REPONER URGENTE — se agotan antes de que llegue el pedido:[/red]")
            for r in urgentes:
                dias_str = f"{r['dias_stock']:.0f}d" if r["dias_stock"] else "—"
                lineas.append(
                    f"   • {r['titulo']}\n"
                    f"     Stock: {r['stock']} u. | Vel: {r['velocidad']:.1f}/día | "
                    f"Se acaba en: [red]{dias_str}[/red] | Lead time: {r['lead_days']}d"
                )
        console.print(Panel(
            "\n".join(lineas),
            title="[bold red]Alertas críticas Full[/bold red]",
            border_style="red",
            padding=(0, 2),
        ))

    if stock_muerto:
        lineas = ["[yellow]📦 STOCK MUERTO — sin ventas, ML te cobra almacenamiento:[/yellow]\n"]
        for r in stock_muerto:
            costo_str = f"  Costo acum: [red]${r['costo_almac']:,.0f}[/red]" if r["costo_almac"] else ""
            dias_str  = f"{r['sin_ventas_dias']}d sin ventas" if r["sin_ventas_dias"] else "sin ventas recientes"
            lineas.append(
                f"   • {r['titulo']}\n"
                f"     Stock: {r['stock']} u. | {dias_str}{costo_str}\n"
                f"     → Bajá el precio o retirá el stock del centro Full"
            )
        console.print(Panel(
            "\n".join(lineas),
            title="[bold yellow]Stock muerto[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))

    if escalar:
        lineas = ["[green]🚀 CANDIDATOS PARA ESCALAR — alta vel. + buen margen:[/green]\n"]
        for r in escalar:
            margen_str = f"{r['margen_pct']*100:.1f}%" if r["margen_pct"] else "—"
            lineas.append(
                f"   • {r['titulo']}\n"
                f"     Vel: {r['velocidad']:.1f}/día | Margen: [green]{margen_str}[/green] | "
                f"Stock: {r['stock']} u.\n"
                f"     → Aumentá stock, optimizá publicación y activá ads"
            )
        console.print(Panel(
            "\n".join(lineas),
            title="[bold green]Para escalar[/bold green]",
            border_style="green",
            padding=(0, 2),
        ))


def _show_tabla_full(results: list[dict]):
    if not results:
        return

    table = Table(
        title="Publicaciones Mercado Envíos Full",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold",
        expand=True,
    )
    table.add_column("Publicación",  ratio=3, no_wrap=True)
    table.add_column("Stock",        justify="right", min_width=6)
    table.add_column("Vel/día",      justify="right", min_width=7)
    table.add_column("Días stock",   justify="right", min_width=10)
    table.add_column("Lead",         justify="right", min_width=6)
    table.add_column("Margen",       justify="right", min_width=8)
    table.add_column("Estado",       justify="center", min_width=16)

    estados = {
        "SIN_STOCK":      "[red bold]⛔ SIN STOCK[/red bold]",
        "REPONER_URGENTE": "[red]🚨 REPONER YA[/red]",
        "STOCK_MUERTO":   "[yellow]📦 STOCK MUERTO[/yellow]",
        "ESCALAR":        "[green]🚀 ESCALAR[/green]",
        None:             "[dim]OK[/dim]",
    }

    def dias_label(dias, lead):
        if dias is None:
            return "[dim]—[/dim]"
        if dias <= lead:
            return f"[red]{dias:.0f}d[/red]"
        if dias <= lead * 1.5:
            return f"[yellow]{dias:.0f}d[/yellow]"
        return f"[green]{dias:.0f}d[/green]"

    for r in sorted(results, key=lambda x: (x["alerta"] != "SIN_STOCK", x["alerta"] != "REPONER_URGENTE", x["velocidad"] * -1)):
        titulo  = r["titulo"][:45] + "…" if len(r["titulo"]) > 45 else r["titulo"]
        margen  = f"{r['margen_pct']*100:.1f}%" if r["margen_pct"] is not None else "[dim]—[/dim]"
        vel_str = f"{r['velocidad']:.1f}" if r["velocidad"] > 0 else "[dim]0[/dim]"
        table.add_row(
            titulo,
            str(r["stock"]),
            vel_str,
            dias_label(r["dias_stock"], r["lead_days"]),
            f"{r['lead_days']}d",
            margen,
            estados.get(r["alerta"], "[dim]OK[/dim]"),
        )

    console.print(table)


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup(client: MLClient, alias: str):
    """Configura tiempos de reposición por producto para el análisis Full."""
    console.print(f"\n[bold cyan]Configurar Full — {alias}[/bold cyan]\n")
    console.print("[dim]Configurá cuántos días tardás en reponer stock en el centro Full.[/dim]")
    console.print("[dim]Esto incluye: producción/compra + tránsito + ingreso al centro ML.[/dim]\n")

    cfg = _load_config()

    # Lead time global
    global_lead = IntPrompt.ask(
        "Lead time global (días)",
        default=cfg.get("global_lead_days", 18),
    )
    cfg["global_lead_days"] = global_lead

    # Por publicación (opcional)
    items = _get_full_items(client)
    if not items:
        console.print("[yellow]No se encontraron publicaciones con Full activo.[/yellow]")
        _save_config(cfg)
        return

    if Confirm.ask(f"\n¿Configurar lead time individual para cada una de las {len(items)} publicaciones Full?", default=False):
        for item in items:
            item_id = item["id"]
            titulo  = item.get("title", "")[:60]
            current = _get_lead_days(item_id, cfg)
            console.print(f"\n  [bold]{titulo}[/bold]")
            lead = IntPrompt.ask(f"  Lead time en días (Enter = {current}d global)", default=current)
            if lead != global_lead:
                cfg.setdefault("items", {})[item_id] = {"lead_days": lead}

    _save_config(cfg)
    console.print(f"\n[green]✓ Configuración guardada en config/full_config.json[/green]")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(client: MLClient, alias: str):
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(f"\n[bold cyan]Mercado Envíos Full — {alias}[/bold cyan]")
    console.print(f"Fecha: {today}\n")

    console.print("Detectando publicaciones Full...", end=" ")
    full_items = _get_full_items(client)

    if not full_items:
        console.print(
            "\n[yellow]No se encontraron publicaciones con Mercado Envíos Full activo.[/yellow]\n"
            "[dim]Si usás Full, verificá que las publicaciones tengan logistic_type=fulfillment.[/dim]"
        )
        return

    console.print(f"[green]{len(full_items)} publicaciones Full[/green]")

    cfg    = _load_config()
    costos = _load_costos()

    console.print(f"[dim]Analizando velocidad de ventas y márgenes...[/dim]\n")

    results = []
    for item in full_items:
        r = _analizar_item(item, client, cfg, costos)
        results.append(r)
        time.sleep(0.2)

    _show_tabla_full(results)
    _show_alertas_criticas(results)

    # Resumen
    sin_stock  = sum(1 for r in results if r["alerta"] == "SIN_STOCK")
    urgentes   = sum(1 for r in results if r["alerta"] == "REPONER_URGENTE")
    muertos    = sum(1 for r in results if r["alerta"] == "STOCK_MUERTO")
    escalar    = sum(1 for r in results if r["alerta"] == "ESCALAR")
    costo_muerto = sum(r["costo_almac"] or 0 for r in results if r["alerta"] == "STOCK_MUERTO")

    resumen = (
        f"  Full activo:         {len(results)} publicaciones\n"
        f"  [red]Sin stock:[/red]           {sin_stock}\n"
        f"  [red]Reponer urgente:[/red]     {urgentes}\n"
        f"  [yellow]Stock muerto:[/yellow]        {muertos}"
        + (f"  (costo almac. est. [red]${costo_muerto:,.0f}[/red])" if costo_muerto else "")
        + f"\n  [green]Para escalar:[/green]        {escalar}"
    )
    console.print(Panel(resumen, title="Resumen Full", border_style="dim", padding=(0, 2)))
