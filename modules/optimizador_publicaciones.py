"""
Módulo 3 — Optimizador de publicaciones con IA (CLI)

Usa el motor SEO v2 (seo_optimizer.py) que obtiene keywords reales del
autosuggest de ML + análisis profundo de competidores para generar:
  - 3 títulos alternativos optimizados
  - Ficha técnica perfecta con los atributos de la categoría
  - Descripción superadora de 650-800 palabras
"""

import json
import os
import time
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich import box

from core.ml_client import MLClient
from modules.seo_optimizer import run_full_optimization
from modules.monitor_posicionamiento import _get_all_active_items

console = Console()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def _save_report(alias: str, report: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    safe = alias.replace(" ", "_").replace("/", "-")
    path = os.path.join(DATA_DIR, f"optimizaciones_{safe}.json")
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    opts = existing.get("optimizaciones", [])
    item_id = report.get("item_id", "")
    opts = [o for o in opts if o.get("item_id") != item_id]
    opts.insert(0, report)
    existing["optimizaciones"] = opts[:20]
    existing["fecha"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def _apply_changes(item_id: str, new_title: str, new_description: str, client: MLClient):
    """Aplica título y/o descripción a la publicación vía API de ML."""
    errors = []
    if new_title:
        try:
            client._put(f"/items/{item_id}", {"title": new_title})
            console.print(f"  [green]✓ Título actualizado[/green]")
        except Exception as e:
            errors.append(f"título: {e}")
    if new_description:
        try:
            client._put(f"/items/{item_id}/description", {"plain_text": new_description})
            console.print(f"  [green]✓ Descripción actualizada[/green]")
        except Exception as e:
            errors.append(f"descripción: {e}")
    if errors:
        console.print(f"  [red]Errores al aplicar: {'; '.join(errors)}[/red]")


def _show_result(result: dict):
    """Muestra el resultado completo del análisis y la optimización."""
    item_data  = result.get("item_data", {})
    ml_score   = result.get("ml_score", {})
    seo        = result.get("seo_result", {})
    kws        = result.get("autosuggest_kws", [])
    rankings   = result.get("rankings", {})

    # Score actual
    score_color = "green" if ml_score.get("total", 0) >= 70 else "yellow" if ml_score.get("total", 0) >= 45 else "red"
    console.print(f"\n  Score ML actual: [{score_color}]{ml_score.get('total', 0)}/100[/{score_color}]")
    console.print(f"  Atributos req: {ml_score.get('attrs_required_pct', 0):.0f}%  |  "
                  f"Opcionales: {ml_score.get('attrs_optional_pct', 0):.0f}%  |  "
                  f"Fotos: {ml_score.get('photos', 0)}")

    # Keywords del autosuggest con posición
    if kws:
        console.print(f"\n  [bold]Keywords reales (autosuggest ML):[/bold]")
        for i, kw in enumerate(kws[:8], 1):
            pos = rankings.get(kw)
            pos_s = f"[green]pos #{pos}[/green]" if pos and pos <= 10 else \
                    f"[yellow]pos #{pos}[/yellow]" if pos else "[red]no rankea[/red]"
            in_t = " [dim]← en tu título[/dim]" if kw.lower() in item_data.get("title", "").lower() else ""
            console.print(f"    {i}. {kw} → {pos_s}{in_t}")

    # Los 3 títulos alternativos
    titulos_alt = seo.get("titulos_alt", [])
    if titulos_alt:
        console.print(f"\n  [bold cyan]3 TÍTULOS ALTERNATIVOS:[/bold cyan]")
        old_title = item_data.get("title", "")
        table = Table(box=box.SIMPLE, expand=True, show_header=True, header_style="bold dim")
        table.add_column("#",        width=4)
        table.add_column("Título",   ratio=1)
        table.add_column("Chars",    width=6, justify="right")
        table.add_column("Estrategia", ratio=1, style="dim")

        table.add_row("Actual", f"[dim]{old_title}[/dim]", str(len(old_title)), "—")
        for i, t in enumerate(titulos_alt, 1):
            tit = t.get("titulo", "")
            table.add_row(
                f"[green]{i}[/green]",
                f"[green]{tit}[/green]",
                str(len(tit)),
                t.get("estrategia", ""),
            )
        console.print(table)

    # Ficha técnica perfecta
    ficha = seo.get("ficha_perfecta", "")
    if ficha:
        preview = ficha[:400] + "…" if len(ficha) > 400 else ficha
        console.print(Panel(preview, title="[cyan]Ficha técnica perfecta[/cyan]",
                            border_style="cyan", padding=(0, 1)))

    # Descripción nueva
    desc = seo.get("descripcion_nueva", "")
    if desc:
        preview = desc[:350] + "…" if len(desc) > 350 else desc
        console.print(Panel(preview, title="[green]Descripción nueva (preview)[/green]",
                            border_style="green", padding=(0, 1)))


def run(
    client: MLClient,
    alias: str,
    max_items: int = 5,
    auto_apply: bool = False,
    item_id_filter: str = None,
):
    """
    Optimiza publicaciones usando el motor SEO v2 (autosuggest + competencia + Claude).

    Args:
        max_items:       Cuántas publicaciones optimizar (default: 5)
        auto_apply:      Si True, aplica los cambios sin preguntar
        item_id_filter:  Si se especifica, solo optimiza ese item_id
    """
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(f"\n[bold cyan]Optimizador IA v2 — {alias}[/bold cyan]")
    console.print(f"Fecha: {today}")
    console.print("[dim]Motor: autosuggest ML (keywords reales) + análisis de competidores + Claude Opus[/dim]\n")

    # Obtener publicaciones activas
    items = _get_all_active_items(client)
    if not items:
        console.print("[yellow]Sin publicaciones activas.[/yellow]")
        return

    if item_id_filter:
        items = [i for i in items if i["id"] == item_id_filter]
        if not items:
            console.print(f"[red]Item {item_id_filter} no encontrado.[/red]")
            return

    items_a_procesar = items[:max_items]
    console.print(f"[dim]Procesando {len(items_a_procesar)} de {len(items)} publicaciones activas.[/dim]\n")

    aplicados_total = 0

    for idx, item in enumerate(items_a_procesar, 1):
        item_id    = item["id"]
        item_title = item.get("title", item_id)

        console.rule(f"[bold]({idx}/{len(items_a_procesar)}) {item_title[:65]}[/bold]")

        try:
            result = run_full_optimization(item_id, client, console=console)
        except Exception as e:
            console.print(f"  [red]Error en análisis: {e}[/red]")
            time.sleep(1)
            continue

        if not result:
            continue

        _show_result(result)

        # Aplicar cambios
        seo            = result.get("seo_result", {})
        titulos_alt    = seo.get("titulos_alt", [])
        desc_nueva     = seo.get("descripcion_nueva", "")
        item_data_res  = result.get("item_data", {})
        my_sold        = item_data_res.get("sold_quantity", 0)
        titulo_elegido = ""

        if not auto_apply and titulos_alt:
            if my_sold > 0:
                console.print(
                    f"\n  [yellow]⚠ Esta publicación tiene {my_sold} ventas — "
                    f"ML no permite cambiar el título. Los títulos son para referencia futura.[/yellow]"
                )
                if Confirm.ask("  ¿Aplicar solo la descripción?", default=False):
                    _apply_changes(item_id, "", desc_nueva, client)
                    aplicados_total += 1
            else:
                opciones = "/".join(str(i) for i in range(1, len(titulos_alt) + 1))
                console.print(f"\n  ¿Qué título aplicar? ({opciones}/N para ninguno)")
                eleccion = input("  → ").strip().upper()
                if eleccion.isdigit() and 1 <= int(eleccion) <= len(titulos_alt):
                    titulo_elegido = titulos_alt[int(eleccion) - 1]["titulo"]
                    _apply_changes(item_id, titulo_elegido, desc_nueva, client)
                    aplicados_total += 1
                elif eleccion == "N" or eleccion == "":
                    console.print("  [dim]Sin cambios aplicados.[/dim]")
        elif auto_apply and titulos_alt and my_sold == 0:
            titulo_elegido = titulos_alt[0]["titulo"]
            _apply_changes(item_id, titulo_elegido, desc_nueva, client)
            aplicados_total += 1

        # Guardar en historial
        _save_report(alias, {
            "item_id":           item_id,
            "titulo_actual":     item_data_res.get("title", ""),
            "titulo_nuevo":      titulo_elegido or seo.get("titulo_principal", ""),
            "titulos_alt":       titulos_alt,
            "descripcion_nueva": desc_nueva,
            "ficha_perfecta":    seo.get("ficha_perfecta", ""),
            "autosuggest_kws":   result.get("autosuggest_kws", []),
            "ml_score_antes":    result.get("ml_score", {}).get("total", 0),
            "categoria":         result.get("category", ""),
            "rankings":          result.get("rankings", {}),
            "aplicado":          bool(titulo_elegido or (my_sold > 0 and desc_nueva)),
            "fecha":             today,
        })

        time.sleep(0.5)

    console.print(Panel(
        f"[bold]Resumen — {alias}[/bold]\n\n"
        f"  Publicaciones analizadas:  {len(items_a_procesar)}\n"
        f"  Cambios aplicados:         {aplicados_total}\n\n"
        f"  Historial → [dim]data/optimizaciones_{alias.replace(' ', '_')}.json[/dim]",
        border_style="dim", padding=(0, 2),
    ))
