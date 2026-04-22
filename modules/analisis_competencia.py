"""
Módulo 2 — Análisis de competencia
Usa el catálogo de ML (products/search) para encontrar competidores en cada categoría.
Detecta keywords faltantes, atributos sin completar y posición de precio.

NOTA: El análisis de precio requiere el permiso "Ítems y búsqueda" activo en la app ML.
Una vez habilitado, la función _get_top_sellers_items() se activa automáticamente.
"""

import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from typing import Optional

import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from core.ml_client import MLClient
from modules.monitor_posicionamiento import _get_all_active_items

console = Console()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

STOPWORDS = {
    "de", "para", "con", "sin", "y", "el", "la", "los", "las", "un", "una",
    "en", "a", "por", "al", "del", "se", "su", "es", "que", "o", "e",
    "x", "cm", "ml", "gr", "kg", "mm", "lt", "unidad", "unidades",
    "pack", "set", "kit", "color", "talle", "talles", "modelo", "nuevo",
}


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-záéíóúüñ0-9]+", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def _keyword_gap(my_title: str, competitor_names: list[str]) -> list[tuple[str, int]]:
    """Keywords que usan los competidores pero no están en el título propio."""
    my_words = set(_tokenize(my_title))
    competitor_words: Counter = Counter()
    for name in competitor_names:
        for word in set(_tokenize(name)):
            if word not in my_words:
                competitor_words[word] += 1
    return competitor_words.most_common(12)


def _search_competitor_products(query: str, token: str, limit: int = 8) -> list[dict]:
    """
    Busca productos del catálogo ML relacionados con la query.
    Devuelve lista de productos con nombre y atributos.
    """
    resp = requests.get(
        "https://api.mercadolibre.com/products/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"site_id": "MLA", "q": query, "limit": limit},
        timeout=10,
    )
    if not resp.ok:
        return []
    results = resp.json().get("results", [])
    return results


def _get_product_detail(product_id: str, token: str) -> Optional[dict]:
    """Trae detalle completo de un producto del catálogo (con atributos)."""
    resp = requests.get(
        f"https://api.mercadolibre.com/products/{product_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=8,
    )
    if resp.ok:
        return resp.json()
    return None


def _get_top_sellers_items(category_id: str, token: str, limit: int = 5) -> list[dict]:
    """
    Intenta obtener los items más vendidos via búsqueda directa.
    Requiere permiso 'items' habilitado en la app ML.
    Devuelve lista vacía si el permiso no está activo.
    """
    resp = requests.get(
        "https://api.mercadolibre.com/sites/MLA/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"category": category_id, "sort": "sold_quantity", "limit": limit},
        timeout=10,
    )
    if resp.ok:
        return resp.json().get("results", [])
    return []  # 403 si el permiso no está activo


def _extract_keywords_for_search(title: str, max_words: int = 4) -> str:
    words = _tokenize(title)
    return " ".join(words[:max_words])


def _save_report(alias: str, report: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    safe = alias.replace(" ", "_").replace("/", "-")
    path = os.path.join(DATA_DIR, f"competencia_{safe}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def run(client: MLClient, alias: str, max_categories: int = 15):
    """
    Ejecuta el análisis de competencia para una cuenta.
    """
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    console.print(f"\n[bold cyan]Análisis de competencia — {alias}[/bold cyan]")
    console.print(f"Fecha: {today}\n")

    client._ensure_token()
    token = client.account.access_token

    # Publicaciones activas
    console.print("Obteniendo publicaciones activas...", end=" ")
    items = _get_all_active_items(client)
    console.print(f"[green]{len(items)} publicaciones[/green]\n")

    if not items:
        console.print("[yellow]No hay publicaciones activas.[/yellow]")
        return

    # Agrupar por categoría
    by_category: dict[str, list[dict]] = {}
    for item in items:
        cat = item.get("category_id", "SIN_CATEGORIA")
        by_category.setdefault(cat, []).append(item)

    console.print(f"[dim]{len(by_category)} categorías. Analizando las {min(max_categories, len(by_category))} más grandes...[/dim]\n")

    full_report = {"fecha": today, "alias": alias, "categorias": {}}
    total_gaps = 0
    total_attrs_missing = 0
    cats_procesadas = 0

    for category_id, mis_items in sorted(by_category.items(), key=lambda x: -len(x[1])):
        if cats_procesadas >= max_categories:
            break

        # Nombre de categoría
        cat_resp = requests.get(
            f"https://api.mercadolibre.com/categories/{category_id}", timeout=8
        )
        cat_name = cat_resp.json().get("name", category_id) if cat_resp.ok else category_id

        console.rule(f"[bold]{cat_name}[/bold] ({len(mis_items)} pub. tuyas)")

        # Intentar search directo (si el permiso está activo)
        top_items = _get_top_sellers_items(category_id, token, limit=5)

        # Si search directo funciona, mostrar tabla de competidores con precios
        if top_items:
            comp_table = Table(box=box.SIMPLE, header_style="bold dim", expand=True)
            comp_table.add_column("#", width=3)
            comp_table.add_column("Título", ratio=3, no_wrap=True)
            comp_table.add_column("Precio", justify="right", min_width=12)
            comp_table.add_column("Ventas", justify="right", min_width=8)
            for i, comp in enumerate(top_items, 1):
                comp_table.add_row(
                    str(i),
                    comp.get("title", "")[:65],
                    f"${comp.get('price', 0):,.0f}",
                    str(comp.get("sold_quantity", "—")),
                )
            console.print(comp_table)
            comp_names = [c.get("title", "") for c in top_items]
            comp_prices = [c.get("price", 0) for c in top_items if c.get("price")]
        else:
            # Fallback: usar catálogo de productos
            console.print(f"  Buscando productos competidores en catálogo...", end=" ")
            # Usar el título del primer item de esta categoría como query
            sample_title = mis_items[0].get("title", "")
            query = _extract_keywords_for_search(sample_title, max_words=3)
            catalog_products = _search_competitor_products(query, token, limit=8)

            if not catalog_products:
                console.print("[dim]sin resultados[/dim]\n")
                cats_procesadas += 1
                continue

            # Obtener nombres de productos del catálogo como referencia competitiva
            comp_names = []
            comp_attrs_union: dict[str, str] = {}

            for prod in catalog_products[:5]:
                prod_detail = _get_product_detail(prod["id"], token)
                if prod_detail:
                    comp_names.append(prod_detail.get("name", ""))
                    for attr in prod_detail.get("attributes", []):
                        attr_id = attr.get("id", "")
                        attr_val = attr.get("value_name", "")
                        if attr_id and attr_val:
                            comp_attrs_union[attr_id] = attr_val
                time.sleep(0.15)

            console.print(f"[green]{len(comp_names)} productos del catálogo[/green]")

            # Mostrar nombres de competidores del catálogo
            comp_table = Table(box=box.SIMPLE, header_style="bold dim", expand=True)
            comp_table.add_column("#", width=3)
            comp_table.add_column("Producto catálogo competidor", ratio=1, no_wrap=True)
            comp_table.add_column("Atributos clave", ratio=1)
            for i, (prod, name) in enumerate(zip(catalog_products[:5], comp_names), 1):
                prod_detail = _get_product_detail(prod["id"], token)
                attrs_str = ""
                if prod_detail:
                    attrs_str = ", ".join(
                        f"{a['name']}: {a['value_name']}"
                        for a in prod_detail.get("attributes", [])[:3]
                        if a.get("value_name")
                    )
                comp_table.add_row(str(i), name[:60], attrs_str[:60])
                time.sleep(0.1)
            console.print(comp_table)
            comp_prices = []  # sin precios en modo catálogo

        # ── Análisis por publicación propia ──────────────────────────────────
        cat_report = {
            "nombre": cat_name,
            "modo": "search_directo" if top_items else "catalogo_productos",
            "competidores": comp_names,
            "mis_publicaciones": [],
        }

        for my_item in mis_items:
            my_title = my_item.get("title", "")
            my_price = my_item.get("price", 0)
            my_id = my_item["id"]

            # Keyword gap
            gaps = _keyword_gap(my_title, comp_names)
            gap_words = [w for w, _ in gaps[:8]]

            # Posición de precio (solo si tenemos precios reales de search directo)
            price_info = ""
            if comp_prices:
                avg = sum(comp_prices) / len(comp_prices)
                if my_price < min(comp_prices):
                    price_info = f"[green]más barato (mín: ${min(comp_prices):,.0f})[/green]"
                elif my_price > max(comp_prices):
                    price_info = f"[red]más caro (máx: ${max(comp_prices):,.0f})[/red]"
                elif my_price < avg:
                    price_info = f"[cyan]bajo promedio (avg: ${avg:,.0f})[/cyan]"
                else:
                    price_info = f"[yellow]sobre promedio (avg: ${avg:,.0f})[/yellow]"

            # Atributos faltantes
            my_attrs = {a["id"] for a in my_item.get("attributes", []) if a.get("value_name")}
            comp_attrs = comp_attrs_union if not top_items else {
                a["id"]: a.get("value_name", "")
                for comp in top_items
                for a in comp.get("attributes", [])
            }
            missing_attrs = [k for k in comp_attrs if k not in my_attrs][:5]

            # Output
            console.print(f"\n  [bold]Tu pub:[/bold] {my_title[:65]}")
            if price_info:
                console.print(f"  [bold]Precio:[/bold] ${my_price:,.0f} → {price_info}")

            if gap_words:
                console.print(
                    "  [bold yellow]Keywords faltantes:[/bold yellow] "
                    + ", ".join(f"[yellow]{w}[/yellow]" for w in gap_words)
                )
                total_gaps += 1
            else:
                console.print("  [green]✓ Título cubre keywords principales.[/green]")

            if missing_attrs:
                console.print(
                    f"  [bold red]Atributos sin completar:[/bold red] "
                    + ", ".join(missing_attrs)
                )
                total_attrs_missing += 1

            cat_report["mis_publicaciones"].append({
                "id": my_id,
                "titulo": my_title,
                "precio": my_price,
                "keywords_faltantes": gap_words,
                "atributos_faltantes": missing_attrs,
            })

        full_report["categorias"][category_id] = cat_report
        cats_procesadas += 1
        console.print()

    _save_report(alias, full_report)

    # ── Resumen ──────────────────────────────────────────────────────────────
    search_activo = any(
        v.get("modo") == "search_directo"
        for v in full_report["categorias"].values()
    )
    precio_note = (
        "[green]✓ Análisis de precio activo[/green]"
        if search_activo
        else "[yellow]⚠ Precio: activá permiso 'Ítems y búsqueda' en ML Developers[/yellow]"
    )

    summary = (
        f"[bold]Resumen — {alias}[/bold]\n\n"
        f"  Categorías analizadas:         {cats_procesadas}\n"
        f"  Publicaciones analizadas:      {len(items)}\n"
        f"  [yellow]Con keywords faltantes:[/yellow]       {total_gaps}\n"
        f"  [red]Con atributos incompletos:[/red]     {total_attrs_missing}\n"
        f"  {precio_note}\n\n"
        f"  Reporte → [dim]data/competencia_{alias.replace(' ', '_')}.json[/dim]"
    )
    console.print(Panel(summary, border_style="dim", padding=(0, 2)))

    return full_report
