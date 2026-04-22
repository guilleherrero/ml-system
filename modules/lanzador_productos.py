"""
Módulo 4 — Lanzador de nuevos productos (powered by SEO Engine)

Antes de publicar un producto nuevo, ejecuta el motor SEO completo:
  1. Detecta la categoría óptima en ML
  2. Descubre las keywords más buscadas en esa categoría
  3. Identifica los competidores top para ese producto específico
  4. Analiza en profundidad cada competidor: atributos, fotos, keywords, descripción
  5. Obtiene todos los atributos requeridos y opcionales de la categoría
  6. Genera con Claude una publicación perfecta: título keyword-first, descripción
     con SEO natural, lista de atributos a completar y análisis competitivo.

El resultado es una publicación lista para copiar y pegar que ya tiene todo lo
necesario para posicionarse desde el primer día.
"""

import json
import os
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich import box

from rich.prompt import FloatPrompt
from core.ml_client import MLClient
from modules.seo_optimizer import run_new_listing

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
console  = Console()


def _save_report(product_idea: str, result: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    safe = product_idea[:30].replace(" ", "_").replace("/", "-")
    ts   = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(DATA_DIR, f"lanzamiento_{safe}_{ts}.json")

    # Serializar solo lo necesario (evitar objetos no serializables)
    report = {
        "fecha":        ts,
        "producto":     product_idea,
        "categoria":    result.get("category", ""),
        "categoria_id": result.get("category_id", ""),
        "keywords_top": result.get("keywords", {}).get("top_keywords", [])[:15],
        "competitors": [
            {
                "id":    c.get("id", ""),
                "title": c.get("title", ""),
                "price": c.get("price", 0),
                "photo_count":   c.get("photo_count", 0),
                "free_shipping": c.get("free_shipping", False),
                "attr_count":    len(c.get("attributes", [])),
            }
            for c in result.get("competitors", [])
        ],
        "atributos_requeridos": result.get("ml_score", {}).get("missing_required", []),
        "titulo_generado":      result.get("seo_result", {}).get("title", ""),
        "keywords_generadas":   result.get("seo_result", {}).get("keywords", []),
        "atributos_sugeridos":  result.get("seo_result", {}).get("attributes", []),
        "descripcion":          result.get("seo_result", {}).get("description", ""),
        "analisis":             result.get("seo_result", {}).get("analysis", ""),
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return path


def _show_result(result: dict):
    """Muestra el resultado completo del lanzamiento."""
    seo       = result.get("seo_result", {})
    keywords  = result.get("keywords", {}).get("top_keywords", [])
    comps     = result.get("competitors", [])
    cat_attrs = result.get("category_attrs", {})

    # Resumen de mercado
    console.print(f"\n[bold]Categoría:[/bold] {result.get('category', '—')}")
    console.print(f"[bold]Keywords top en la categoría:[/bold]")
    for kw, pct in keywords[:8]:
        bar = "█" * (pct // 10)
        console.print(f"  [cyan]{kw:<25}[/cyan] {bar} {pct}%")

    # Competidores
    if comps:
        table = Table(title="Competidores directos analizados", box=box.ROUNDED)
        table.add_column("Título", ratio=1, no_wrap=True)
        table.add_column("Precio", justify="right", style="cyan")
        table.add_column("Fotos", justify="center")
        table.add_column("Attrs", justify="center")
        table.add_column("Envío", justify="center")
        for c in comps:
            envio = "[green]Gratis[/green]" if c.get("free_shipping") else "[dim]Pago[/dim]"
            table.add_row(
                c.get("title", "")[:55],
                f"${c.get('price', 0):,.0f}",
                str(c.get("photo_count", "?")),
                str(len(c.get("attributes", []))),
                envio,
            )
        console.print(table)

    # Atributos requeridos de la categoría
    req_attrs = cat_attrs.get("required", [])
    if req_attrs:
        req_names = ", ".join(a["name"] for a in req_attrs[:8])
        console.print(f"\n[bold]Atributos requeridos por ML ({len(req_attrs)}):[/bold] [dim]{req_names}[/dim]")

    # Resultado generado por Claude
    console.rule("[bold green]PUBLICACIÓN GENERADA[/bold green]")

    new_title = seo.get("title", "")
    if new_title:
        char_color = "green" if 55 <= len(new_title) <= 60 else "yellow"
        console.print(Panel(
            f"[bold]{new_title}[/bold]",
            title=f"Título optimizado  [{char_color}]{len(new_title)} caracteres[/{char_color}]",
            border_style="green",
        ))

    gen_keywords = seo.get("keywords", [])
    if gen_keywords:
        console.print(f"[bold]Keywords priorizadas:[/bold] {', '.join(gen_keywords[:8])}")

    attrs_sugeridos = seo.get("attributes", [])
    if attrs_sugeridos:
        console.print("\n[bold]Atributos a completar al publicar:[/bold]")
        for a in attrs_sugeridos:
            console.print(f"  • [cyan]{a['name']}:[/cyan] {a['value']}")

    desc = seo.get("description", "")
    if desc:
        preview = desc[:400] + "…" if len(desc) > 400 else desc
        console.print(Panel(
            preview,
            title="Descripción optimizada (preview)",
            border_style="green",
            padding=(0, 1),
        ))

    analysis = seo.get("analysis", "")
    if analysis:
        console.print(Panel(
            analysis,
            title="[cyan]Análisis competitivo[/cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))


def run(client: MLClient, product_idea: str = None):
    """
    Ejecuta el análisis de lanzamiento SEO completo para un producto nuevo.

    Args:
        client:       MLClient autenticado (para llamadas a la API de ML)
        product_idea: Nombre/descripción del producto. Si no se pasa, se pregunta.
    """
    console.print("\n[bold cyan]Lanzador de productos — Motor SEO[/bold cyan]\n")
    console.print("[dim]Análisis: categoría → keywords → competidores → atributos → publicación perfecta[/dim]\n")

    if not product_idea:
        product_idea = Prompt.ask(
            "[bold]¿Qué producto querés lanzar?[/bold]\n"
            "  Ejemplo: 'faja postparto con varillas', 'termo 500ml doble pared'\n"
            "  Producto"
        )

    expected_price = FloatPrompt.ask(
        "[bold]¿A qué precio pensás venderlo?[/bold] (en ARS, Enter para saltar)",
        default=0.0,
    )

    console.print(f"\n[bold]Analizando:[/bold] {product_idea}")
    if expected_price > 0:
        console.print(f"[dim]Precio esperado: ${expected_price:,.0f} — se usará para filtrar competidores del mismo segmento[/dim]")
    console.print()

    try:
        result = run_new_listing(product_idea, client, expected_price=expected_price, console=console)
    except Exception as e:
        console.print(f"\n[red]Error en el análisis: {e}[/red]")
        return {}

    if not result:
        console.print("[yellow]No se pudo completar el análisis.[/yellow]")
        return {}

    _show_result(result)

    path = _save_report(product_idea, result)
    console.print(Panel(
        f"[bold]Reporte guardado[/bold]\n\n  {path}",
        border_style="dim", padding=(0, 2),
    ))

    return result
