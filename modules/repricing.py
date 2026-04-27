"""
Módulo 5 — Repricing automático
Ajusta precios de tus publicaciones automáticamente según la competencia
y tus reglas de margen mínimo/máximo.

Reglas:
  - Si hay competidor más barato: bajá al competidor -1% (pero nunca bajo precio_min)
  - Si sos el más barato o no hay competidores: subí 2% (hasta precio_max)
  - Si el competidor desapareció o subió: subí al precio_max configurado
"""

import json
import os
import time
from datetime import datetime

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, FloatPrompt
from rich.table import Table
from rich import box

from core.ml_client import MLClient
from core.fees import get_fee_rates, get_rate
from modules.monitor_posicionamiento import _get_all_active_items

console = Console()

CONFIG_DIR  = os.path.join(os.path.dirname(__file__), "..", "config")
DATA_DIR    = os.path.join(os.path.dirname(__file__), "..", "data")
CONFIG_PATH = os.path.join(CONFIG_DIR, "repricing.json")


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"items": {}}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _get_item_config(cfg: dict, item_id: str) -> dict:
    return cfg["items"].get(item_id, {})


# ── Competitor prices ─────────────────────────────────────────────────────────

def _get_competitor_info(item: dict, token: str) -> dict:
    """
    Obtiene información del competidor más relevante para este ítem.

    Para publicaciones de catálogo (tienen catalog_product_id):
      - Usa products/{id}/items para ver todos los vendedores del mismo producto
      - Identifica si ganamos el buy box o no
      - Devuelve el precio del competidor más barato (excluyendo nuestra publicación)

    Para publicaciones fuera de catálogo:
      - Búsqueda directa en categoria (requiere permiso especial, suele dar 403)
      - Resultado impreciso: puede ser un producto distinto al nuestro

    Retorna:
      {
        "competitor_price": float | None,
        "is_catalog": bool,
        "catalog_product_id": str | None,
        "buy_box_winner_id": str | None,    # item_id que gana el buy box
        "we_win_buy_box": bool,             # si nuestra pub gana el buy box
        "total_sellers": int,               # cuántos vendedores compiten
        "match_quality": "exact" | "approximate" | "none"
      }
    """
    item_id = item.get("id", "")
    title   = item.get("title", "")
    category = item.get("category_id", "")
    catalog_product_id = item.get("catalog_product_id")
    headers = {"Authorization": f"Bearer {token}"}

    # ── Caso 1: publicación de catálogo → comparación exacta ─────────────────
    if catalog_product_id:
        try:
            r = requests.get(
                f"https://api.mercadolibre.com/products/{catalog_product_id}/items",
                headers=headers,
                params={"limit": 10},
                timeout=8,
            )
            if r.ok:
                items_in_catalog = r.json().get("results", [])
                buy_box_winner_id = None
                min_competitor_price = None
                total_sellers = len(items_in_catalog)

                for ci in items_in_catalog:
                    ci_id    = ci.get("id") or ci.get("item_id", "")
                    ci_price = float(ci.get("price") or 0)
                    ci_bbw   = ci.get("winner_item_id") or ci.get("catalog_winner")

                    # Detectar buy box winner
                    if ci_bbw:
                        buy_box_winner_id = ci_bbw

                    # Competidores = todos excepto nosotros
                    if ci_id and ci_id != item_id and ci_price > 0:
                        if min_competitor_price is None or ci_price < min_competitor_price:
                            min_competitor_price = ci_price

                # Si no detectamos buy_box por campo directo, es el item más barato
                if not buy_box_winner_id and items_in_catalog:
                    cheapest = min(items_in_catalog, key=lambda x: float(x.get("price") or 999999999))
                    buy_box_winner_id = cheapest.get("id") or cheapest.get("item_id", "")

                we_win = (buy_box_winner_id == item_id) if buy_box_winner_id else False

                return {
                    "competitor_price": min_competitor_price,
                    "is_catalog": True,
                    "catalog_product_id": catalog_product_id,
                    "buy_box_winner_id": buy_box_winner_id,
                    "we_win_buy_box": we_win,
                    "total_sellers": total_sellers,
                    "match_quality": "exact",
                }
        except Exception:
            pass

    # ── Caso 2: no es catálogo → búsqueda por categoría (imprecisa) ──────────
    try:
        resp = requests.get(
            "https://api.mercadolibre.com/sites/MLA/search",
            headers=headers,
            params={"category": category, "sort": "price_asc", "limit": 5},
            timeout=8,
        )
        if resp.ok:
            results = resp.json().get("results", [])
            competitors = [r for r in results if r.get("id") != item_id]
            if competitors:
                return {
                    "competitor_price": float(competitors[0]["price"]),
                    "is_catalog": False,
                    "catalog_product_id": None,
                    "buy_box_winner_id": None,
                    "we_win_buy_box": False,
                    "total_sellers": len(results),
                    "match_quality": "approximate",
                }
    except Exception:
        pass

    return {
        "competitor_price": None,
        "is_catalog": False,
        "catalog_product_id": None,
        "buy_box_winner_id": None,
        "we_win_buy_box": False,
        "total_sellers": 0,
        "match_quality": "none",
    }


# ── Pricing rules ─────────────────────────────────────────────────────────────

def _calculate_new_price(
    current_price: float,
    competitor_price: float | None,
    min_price: float,
    max_price: float,
    costo: float | None,
    fee_rate: float = 0.31,
) -> tuple[float, str]:
    """
    Devuelve (nuevo_precio, razón).
    Garantiza que el nuevo precio esté entre min_price y max_price.
    fee_rate se obtiene de la API de ML vía core/fees.py.
    """
    # Validar que min_price no sea menor que costo + comisión ML
    if costo:
        costo_con_fee = costo / (1 - fee_rate)
        min_price = max(min_price, round(costo_con_fee, 2))

    if competitor_price is None:
        # Sin datos de competencia: subir levemente si podemos
        new_price = min(round(current_price * 1.02, 2), max_price)
        if new_price != current_price:
            return new_price, f"sin competidor detectado → +2% (${new_price:,.0f})"
        return current_price, "sin competidor, ya en precio máximo"

    if competitor_price < current_price:
        # Competidor más barato: igualar -1%
        target = round(competitor_price * 0.99, 2)
        new_price = max(target, min_price)
        if new_price == min_price and target < min_price:
            return new_price, f"competidor ${competitor_price:,.0f} → bajamos a precio mínimo ${min_price:,.0f}"
        return new_price, f"competidor ${competitor_price:,.0f} → igualamos -1% (${new_price:,.0f})"

    if competitor_price > current_price * 1.05:
        # Competidor más caro por 5%+: subir hacia él
        new_price = min(round(competitor_price * 0.98, 2), max_price)
        if new_price > current_price:
            return new_price, f"competidor ${competitor_price:,.0f} más caro → subimos a ${new_price:,.0f}"

    return current_price, f"precio óptimo (comp: ${competitor_price:,.0f})"


# ── Setup interactivo ─────────────────────────────────────────────────────────

def setup(client: MLClient, alias: str):
    """
    Configura precio mínimo y máximo para las publicaciones de una cuenta.
    Se puede correr categoría por categoría o item por item.
    """
    console.print(f"\n[bold cyan]Configurar repricing — {alias}[/bold cyan]\n")

    items = _get_all_active_items(client)
    if not items:
        console.print("[yellow]Sin publicaciones activas.[/yellow]")
        return

    cfg = _load_config()

    # Agrupar por categoría para configurar en bulk
    by_category: dict[str, list[dict]] = {}
    for item in items:
        cat = item.get("category_id", "SIN_CAT")
        by_category.setdefault(cat, []).append(item)

    console.print(f"[dim]{len(items)} publicaciones en {len(by_category)} categorías.[/dim]\n")
    console.print("Podés configurar el [bold]margen mínimo %[/bold] por categoría.")
    console.print("El precio mínimo se calcula como: precio_actual × (1 - margen_minimo)\n")

    for cat_id, cat_items in sorted(by_category.items(), key=lambda x: -len(x[1])):
        cat_resp = requests.get(f"https://api.mercadolibre.com/categories/{cat_id}", timeout=6)
        cat_name = cat_resp.json().get("name", cat_id) if cat_resp.ok else cat_id

        console.print(f"[bold]{cat_name}[/bold] ({len(cat_items)} pubs)")

        margen_str = Prompt.ask(
            f"  Margen mínimo % (Enter para saltear)",
            default="",
        )
        if not margen_str.strip():
            continue

        try:
            margen = float(margen_str.strip()) / 100
        except ValueError:
            console.print("  [red]Valor inválido, saltando.[/red]")
            continue

        max_pct_str = Prompt.ask(
            f"  Subida máxima % sobre precio actual",
            default="20",
        )
        try:
            max_pct = float(max_pct_str.strip()) / 100
        except ValueError:
            max_pct = 0.20

        for item in cat_items:
            price = item.get("price", 0)
            cfg["items"][item["id"]] = {
                "titulo": item.get("title", "")[:50],
                "precio_actual": price,
                "precio_min": round(price * (1 - margen), 2),
                "precio_max": round(price * (1 + max_pct), 2),
                "margen_min_pct": margen,
                "alias": alias,
            }

        console.print(f"  [green]✓ {len(cat_items)} pubs configuradas[/green]")

    _save_config(cfg)
    total_conf = sum(1 for v in cfg["items"].values() if v.get("alias") == alias)
    console.print(f"\n[green]✓ {total_conf} publicaciones configuradas para repricing.[/green]")
    console.print(f"[dim]Guardado en config/repricing.json[/dim]")


# ── Repricing run ─────────────────────────────────────────────────────────────

def run(client: MLClient, alias: str, dry_run: bool = True):
    """
    Ejecuta el repricing para todas las publicaciones configuradas de la cuenta.

    Args:
        dry_run: Si True, muestra los cambios sin aplicarlos (por defecto).
    """
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(f"\n[bold cyan]Repricing — {alias}[/bold cyan]")
    console.print(f"Fecha: {today} | Modo: {'[yellow]simulación[/yellow]' if dry_run else '[green]aplicando cambios[/green]'}\n")

    cfg = _load_config()
    items_cfg = {k: v for k, v in cfg["items"].items() if v.get("alias") == alias}

    # Cargar tasas de comisión desde la API de ML (auto-refresca si tiene >7 días)
    fees = get_fee_rates(client)

    if not items_cfg:
        console.print(
            "[yellow]No hay publicaciones configuradas para repricing.\n"
            "Corré primero:[/yellow]\n"
            "  python main.py repricing-setup\n"
        )
        return

    client._ensure_token()
    token = client.account.access_token

    console.print(f"[dim]{len(items_cfg)} publicaciones configuradas.[/dim]\n")

    results = []
    changes = 0

    for item_id, item_cfg in items_cfg.items():
        titulo = item_cfg.get("titulo", item_id)
        min_p  = item_cfg.get("precio_min", 0)
        max_p  = item_cfg.get("precio_max", 999999)
        costo  = item_cfg.get("costo")

        # Precio actual desde ML (puede haber cambiado)
        console.print(f"  {titulo[:50]}...", end="\r")
        try:
            item_data = client._get(f"/items/{item_id}")
            current_price = float(item_data.get("price", item_cfg.get("precio_actual", 0)))
        except Exception:
            item_data = {}
            current_price = float(item_cfg.get("precio_actual", 0))

        # Solo procesar publicaciones de catálogo
        if not item_data.get("catalog_product_id"):
            continue

        # Estado de la publicación
        item_status = item_data.get("status", "unknown")
        sub_status  = item_data.get("sub_status", [])
        if item_status == "closed" and "deleted" in sub_status:
            item_status = "deleted_by_ml"

        # Precio del competidor — usa catalog_product_id si está disponible
        competitor_info = _get_competitor_info({
            "id": item_id,
            "title": titulo,
            "category_id": item_data.get("category_id", item_cfg.get("category_id", "")),
            "catalog_product_id": item_data.get("catalog_product_id"),
        }, token)

        competitor_price = competitor_info["competitor_price"]
        is_catalog       = competitor_info["is_catalog"]
        we_win_buy_box   = competitor_info["we_win_buy_box"]
        total_sellers    = competitor_info["total_sellers"]
        match_quality    = competitor_info["match_quality"]

        # Calcular nuevo precio usando comisión real de la API de ML
        listing_type = item_data.get("listing_type_id", "gold_special")
        fee_rate = get_rate(listing_type, fees)
        new_price, reason = _calculate_new_price(current_price, competitor_price, min_p, max_p, costo, fee_rate)

        changed = abs(new_price - current_price) > 0.5

        results.append({
            "id": item_id,
            "titulo": titulo,
            "precio_actual": current_price,
            "precio_nuevo": new_price,
            "precio_min": min_p,
            "precio_max": max_p,
            "competidor": competitor_price,
            "razon": reason,
            "cambio": changed,
            "is_catalog": is_catalog,
            "we_win_buy_box": we_win_buy_box,
            "total_sellers": total_sellers,
            "match_quality": match_quality,
            "status": item_status,
        })

        if changed:
            changes += 1
            if not dry_run:
                try:
                    client._put(f"/items/{item_id}", {"price": new_price})
                    # Actualizar config con nuevo precio actual
                    cfg["items"][item_id]["precio_actual"] = new_price
                except Exception as e:
                    console.print(f"\n  [red]Error al actualizar {item_id}: {e}[/red]")

        time.sleep(0.2)

    if not dry_run:
        _save_config(cfg)

    # Mostrar tabla de resultados
    console.print(" " * 80, end="\r")
    _show_results_table(results)

    # Resumen
    subidas      = sum(1 for r in results if r["precio_nuevo"] > r["precio_actual"])
    bajadas      = sum(1 for r in results if r["precio_nuevo"] < r["precio_actual"])
    sin_cambio   = len(results) - subidas - bajadas
    en_catalogo  = sum(1 for r in results if r.get("is_catalog"))
    ganando_bbx  = sum(1 for r in results if r.get("we_win_buy_box"))

    action = "simulados" if dry_run else "aplicados"
    summary = (
        f"[bold]Resumen repricing — {alias}[/bold]\n\n"
        f"  Publicaciones analizadas:  {len(results)}\n"
        f"  [cyan]En catálogo ML:[/cyan]            {en_catalogo}"
        + (f" ({ganando_bbx} ganando buy box)" if en_catalogo else "") + "\n"
        f"  [green]Subidas de precio:[/green]         {subidas}\n"
        f"  [red]Bajadas de precio:[/red]         {bajadas}\n"
        f"  [dim]Sin cambio:[/dim]               {sin_cambio}\n\n"
    )
    if dry_run:
        summary += "  [yellow]⚠ Modo simulación — ningún precio fue modificado.\n"
        summary += "  Para aplicar: python main.py repricing [alias] --apply[/yellow]"
    else:
        summary += f"  [green]✓ {changes} cambios {action} en ML[/green]"

    console.print(Panel(summary, border_style="dim", padding=(0, 2)))

    return results


def _status_order(r: dict) -> int:
    s = r.get("status", "unknown")
    if s == "active":         return 0
    if s == "paused":         return 1
    if s == "deleted_by_ml":  return 3
    return 2  # closed por vendedor u otros


def _status_label(r: dict) -> str:
    s = r.get("status", "unknown")
    if s == "active":        return "[green]Activa[/green]"
    if s == "paused":        return "[yellow]Pausada[/yellow]"
    if s == "deleted_by_ml": return "[red]Eliminada ML[/red]"
    if s == "closed":        return "[dim]Cerrada[/dim]"
    return "[dim]?[/dim]"


def _show_results_table(results: list[dict]):
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("Publicación", ratio=3, no_wrap=True)
    table.add_column("Estado",     min_width=12)
    table.add_column("Tipo",       min_width=8)
    table.add_column("Competidor", justify="right", min_width=12)
    table.add_column("Actual",     justify="right", min_width=10)
    table.add_column("Nuevo",      justify="right", min_width=10)
    table.add_column("Razón", ratio=2)

    for r in sorted(results, key=lambda x: (_status_order(x), -x.get("is_catalog", False), -abs(x["precio_nuevo"] - x["precio_actual"]))):
        titulo = r["titulo"][:45] + "…" if len(r["titulo"]) > 45 else r["titulo"]
        delta  = r["precio_nuevo"] - r["precio_actual"]

        estado = _status_label(r)

        # Tipo y calidad del match
        if r.get("is_catalog"):
            sellers = r.get("total_sellers", 0)
            if r.get("we_win_buy_box"):
                tipo = f"[green]Catálogo ★[/green] ({sellers} vend.)"
            else:
                tipo = f"[yellow]Catálogo[/yellow] ({sellers} vend.)"
        elif r.get("match_quality") == "approximate":
            tipo = "[dim]Categoría~[/dim]"
        else:
            tipo = "[dim]Sin datos[/dim]"

        # Precio competidor
        if r["competidor"]:
            comp = f"${r['competidor']:,.0f}"
        else:
            comp = "[dim]—[/dim]"

        if delta > 0:
            nuevo = f"[green]${r['precio_nuevo']:,.0f} ↑[/green]"
        elif delta < 0:
            nuevo = f"[red]${r['precio_nuevo']:,.0f} ↓[/red]"
        else:
            nuevo = f"[dim]${r['precio_nuevo']:,.0f}[/dim]"

        table.add_row(titulo, estado, tipo, comp, f"${r['precio_actual']:,.0f}", nuevo, r["razon"])

    console.print(table)

    # Leyenda de tipos de comparación
    catalog_count = sum(1 for r in results if r.get("is_catalog"))
    approx_count  = sum(1 for r in results if not r.get("is_catalog") and r.get("match_quality") == "approximate")
    no_data_count = sum(1 for r in results if r.get("match_quality") == "none")

    legend = []
    if catalog_count:
        legend.append(f"[green]★ Catálogo ({catalog_count})[/green]: comparación exacta del mismo producto")
    if approx_count:
        legend.append(f"[dim]~ Categoría ({approx_count})[/dim]: precio más barato de la categoría (puede ser otro producto)")
    if no_data_count:
        legend.append(f"[dim]Sin datos ({no_data_count})[/dim]: no se pudo obtener precio de referencia")

    if legend:
        console.print("\n[bold]Calidad de comparación:[/bold]")
        for l in legend:
            console.print(f"  {l}")
