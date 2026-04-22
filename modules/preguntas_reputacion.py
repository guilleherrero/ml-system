"""
Módulo 6 — Gestión de preguntas y reputación
Responde preguntas automáticamente con IA y monitorea la reputación de la cuenta.

Funciones:
  - Trae preguntas sin responder de todas las publicaciones
  - Genera respuesta inteligente con Claude basada en el título del producto
  - Muestra preview y permite aprobar/editar antes de enviar
  - Monitorea métricas de reputación: ventas, reclamos, demoras, cancelaciones
  - Alerta si algún indicador está en zona de riesgo
"""

import os
import sys
import time
from datetime import datetime

import anthropic
import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich import box

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.db_storage import db_load, db_save
from core.ml_client import MLClient

console = Console()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Umbrales de reputación ML Argentina (según política oficial)
REP_UMBRALES = {
    "reclamos_pct":      {"verde": 1.0,  "amarillo": 2.0},   # % ventas con reclamo
    "demoras_pct":       {"verde": 10.0, "amarillo": 15.0},  # % envíos con demora
    "cancelaciones_pct": {"verde": 2.0,  "amarillo": 5.0},   # % ventas canceladas por vendedor
}


# ── Preguntas ─────────────────────────────────────────────────────────────────

def _get_unanswered_questions(client: MLClient) -> list[dict]:
    """Trae todas las preguntas sin responder del vendedor."""
    questions = []
    offset = 0
    limit = 50
    user_id = client.account.user_id

    while True:
        try:
            data = client._get(
                "/questions/search",
                params={
                    "seller_id": user_id,
                    "status": "UNANSWERED",
                    "limit": limit,
                    "offset": offset,
                },
            )
        except Exception as e:
            console.print(f"[red]Error al traer preguntas: {e}[/red]")
            break

        items = data.get("questions", [])
        questions.extend(items)

        total = data.get("total", 0)
        offset += limit
        if offset >= total or not items:
            break

        time.sleep(0.3)

    return questions


def _get_item_info(item_id: str, client: MLClient) -> dict:
    """Obtiene título y descripción de la publicación."""
    try:
        item = client._get(f"/items/{item_id}")
        title = item.get("title", "")
        # Descripción
        try:
            desc_data = client._get(f"/items/{item_id}/description")
            description = desc_data.get("plain_text", "")[:500]
        except Exception:
            description = ""
        return {"title": title, "description": description}
    except Exception:
        return {"title": item_id, "description": ""}


def _build_answer_prompt(question_text: str, item_title: str, item_description: str) -> str:
    desc_section = f"\nDescripción del producto:\n{item_description}" if item_description else ""
    return f"""Sos un vendedor de MercadoLibre Argentina. Respondé esta pregunta de un comprador de manera clara, amable y vendedora.

Producto: {item_title}{desc_section}

Pregunta del comprador: {question_text}

INSTRUCCIONES:
- Respondé en español rioplatense (vos, tus)
- Sé directo y conciso (máximo 3 oraciones)
- Si la pregunta es sobre talle, color, compatibilidad: sé específico
- Si no tenés el dato exacto, ofrecé alternativas o invitá a preguntar más
- Terminá siempre con una invitación a comprar o a hacer más preguntas
- NO uses emojis ni signos de exclamación excesivos
- NO repitas el nombre del producto completo en la respuesta

Respondé SOLO con el texto de la respuesta, sin introducción ni formato."""


def _call_claude_answer(prompt: str) -> str:
    """Genera respuesta con Claude."""
    ai = anthropic.Anthropic()
    full_text = ""
    with ai.messages.stream(
        model="claude-opus-4-6",
        max_tokens=256,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            full_text += text
    return full_text.strip()


def _answer_question(question_id: str, answer_text: str, client: MLClient) -> bool:
    """Publica la respuesta en ML."""
    try:
        client._post("/answers", {"question_id": question_id, "text": answer_text})
        return True
    except Exception as e:
        console.print(f"[red]Error al responder: {e}[/red]")
        return False


def run_preguntas(client: MLClient, alias: str, auto_responder: bool = False):
    """
    Trae preguntas sin responder y genera respuestas con Claude.

    Args:
        auto_responder: Si True, envía las respuestas automáticamente sin confirmar.
    """
    console.print(f"\n[bold cyan]Preguntas sin responder — {alias}[/bold cyan]\n")

    questions = _get_unanswered_questions(client)

    if not questions:
        console.print("[green]✓ No hay preguntas pendientes.[/green]")
        return

    console.print(f"[yellow]{len(questions)} pregunta(s) sin responder.[/yellow]\n")

    respondidas = 0
    omitidas = 0
    item_cache: dict[str, dict] = {}

    for q in questions:
        q_id = q.get("id")
        q_text = q.get("text", "").strip()
        item_id = q.get("item_id", "")
        fecha = q.get("date_created", "")[:10]

        # Caché de items para no volver a buscar
        if item_id not in item_cache:
            item_cache[item_id] = _get_item_info(item_id, client)
        item_info = item_cache[item_id]

        console.rule(f"[dim]Pregunta {q_id} — {fecha}[/dim]")
        console.print(f"[bold]Producto:[/bold] {item_info['title'][:70]}")
        console.print(f"[bold]Pregunta:[/bold] {q_text}\n")

        # Generar respuesta con Claude
        console.print("  Generando respuesta...", end=" ")
        try:
            prompt = _build_answer_prompt(q_text, item_info["title"], item_info["description"])
            respuesta = _call_claude_answer(prompt)
            console.print("[green]listo[/green]")
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                console.print("[red]Sin créditos en Anthropic.[/red]")
                return
            console.print(f"[red]Error API: {e}[/red]")
            omitidas += 1
            continue
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            omitidas += 1
            continue

        # Mostrar respuesta sugerida
        console.print(Panel(
            f"[green]{respuesta}[/green]",
            title="Respuesta sugerida",
            border_style="green",
            padding=(0, 2),
        ))

        # Decidir si enviar
        if auto_responder:
            enviar = True
        else:
            accion = Prompt.ask(
                "  [bold]¿Qué hacemos?[/bold]",
                choices=["s", "e", "o"],
                default="s",
                show_choices=False,
                show_default=False,
            )
            console.print("  [dim](s=enviar, e=editar, o=omitir)[/dim]", end="\r")

            if accion == "o":
                omitidas += 1
                continue
            elif accion == "e":
                respuesta = Prompt.ask("  Editá la respuesta", default=respuesta)
            enviar = True

        if enviar:
            ok = _answer_question(q_id, respuesta, client)
            if ok:
                console.print(f"  [green]✓ Respuesta enviada[/green]")
                respondidas += 1
            else:
                omitidas += 1

        time.sleep(0.3)

    console.print(Panel(
        f"[bold]Resumen — {alias}[/bold]\n\n"
        f"  Preguntas procesadas: {len(questions)}\n"
        f"  [green]Respondidas:[/green]         {respondidas}\n"
        f"  [dim]Omitidas:[/dim]            {omitidas}\n",
        border_style="dim", padding=(0, 2),
    ))


# ── Reputación ────────────────────────────────────────────────────────────────

def _get_reputation(client: MLClient) -> dict:
    """Trae las métricas de reputación del vendedor."""
    user_id = client.account.user_id
    try:
        data = client._get(f"/users/{user_id}/seller_reputation")
        return data
    except Exception as e:
        console.print(f"[red]Error al traer reputación: {e}[/red]")
        return {}


def _semaforo(valor: float, umbral_verde: float, umbral_amarillo: float, menor_es_mejor: bool = True) -> str:
    """Devuelve color rich según umbrales."""
    if menor_es_mejor:
        if valor <= umbral_verde:
            return f"[green]{valor:.1f}%[/green]"
        elif valor <= umbral_amarillo:
            return f"[yellow]{valor:.1f}%[/yellow]"
        else:
            return f"[red]{valor:.1f}%[/red]"
    else:
        if valor >= umbral_verde:
            return f"[green]{valor:.1f}[/green]"
        elif valor >= umbral_amarillo:
            return f"[yellow]{valor:.1f}[/yellow]"
        else:
            return f"[red]{valor:.1f}[/red]"


def _nivel_reputacion(nivel: str) -> str:
    colores = {
        "5_green":  "[bold green]⭐⭐⭐⭐⭐ MercadoLíder Platinum[/bold green]",
        "4_light_green": "[green]⭐⭐⭐⭐ MercadoLíder Gold[/green]",
        "3_yellow": "[yellow]⭐⭐⭐ MercadoLíder[/yellow]",
        "2_orange": "[yellow]⭐⭐ Bueno[/yellow]",
        "1_red":    "[red]⭐ Nuevo[/red]",
    }
    return colores.get(nivel, f"[dim]{nivel}[/dim]")


def _save_reputation_snapshot(alias: str, data: dict):
    safe = alias.replace(" ", "_").replace("/", "-")
    path = os.path.join(DATA_DIR, f"reputacion_{safe}.json")

    historial = db_load(path) or []
    if not isinstance(historial, list):
        historial = []

    historial.append({"fecha": datetime.now().strftime("%Y-%m-%d %H:%M"), **data})
    historial = historial[-30:]

    db_save(path, historial)


def run_reputacion(client: MLClient, alias: str):
    """Muestra las métricas de reputación del vendedor y alerta si hay riesgo."""
    console.print(f"\n[bold cyan]Reputación — {alias}[/bold cyan]\n")

    rep = _get_reputation(client)
    if not rep:
        return

    nivel = rep.get("level_id", "")
    power_seller = rep.get("power_seller_status", "")
    metrics = rep.get("metrics", {})
    transactions = rep.get("transactions", {})

    # Métricas clave
    ventas_total = transactions.get("total", 0)
    ventas_completadas = transactions.get("completed", 0)
    ventas_canceladas = transactions.get("canceled", 0)

    claims = metrics.get("claims", {})
    delayed = metrics.get("delayed_handling_time", {})
    cancels = metrics.get("cancellations", {})

    claims_pct    = claims.get("rate", 0) * 100
    delayed_pct   = delayed.get("rate", 0) * 100
    cancels_pct   = cancels.get("rate", 0) * 100

    # Panel de nivel
    console.print(Panel(
        f"Nivel: {_nivel_reputacion(nivel)}\n"
        f"Power Seller: [cyan]{power_seller or '—'}[/cyan]\n"
        f"Ventas totales (últimos 60 días): [bold]{ventas_total}[/bold]  |  "
        f"Completadas: [green]{ventas_completadas}[/green]  |  "
        f"Canceladas: [red]{ventas_canceladas}[/red]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # Tabla de métricas
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("Métrica",       ratio=2)
    table.add_column("Valor",         justify="right", min_width=10)
    table.add_column("Verde",         justify="right", min_width=8)
    table.add_column("Amarillo",      justify="right", min_width=8)
    table.add_column("Estado",        justify="center", min_width=10)

    rows = [
        (
            "Reclamos",
            claims_pct,
            REP_UMBRALES["reclamos_pct"]["verde"],
            REP_UMBRALES["reclamos_pct"]["amarillo"],
        ),
        (
            "Demoras en despacho",
            delayed_pct,
            REP_UMBRALES["demoras_pct"]["verde"],
            REP_UMBRALES["demoras_pct"]["amarillo"],
        ),
        (
            "Cancelaciones",
            cancels_pct,
            REP_UMBRALES["cancelaciones_pct"]["verde"],
            REP_UMBRALES["cancelaciones_pct"]["amarillo"],
        ),
    ]

    alertas = []
    for nombre, valor, verde, amarillo in rows:
        semaforo = _semaforo(valor, verde, amarillo)
        if valor > amarillo:
            estado = "[red]⚠ RIESGO[/red]"
            alertas.append((nombre, valor, amarillo))
        elif valor > verde:
            estado = "[yellow]⚡ ATENCIÓN[/yellow]"
        else:
            estado = "[green]✓ OK[/green]"
        table.add_row(nombre, semaforo, f"≤{verde:.0f}%", f"≤{amarillo:.0f}%", estado)

    console.print(table)

    # Alertas críticas
    if alertas:
        alerta_text = "[bold red]⚠ INDICADORES EN ZONA DE RIESGO:[/bold red]\n\n"
        for nombre, valor, limite in alertas:
            alerta_text += f"  • {nombre}: {valor:.1f}% (límite: {limite:.0f}%)\n"
        alerta_text += "\n  [dim]Acción urgente requerida para evitar baja de nivel.[/dim]"
        console.print(Panel(alerta_text, border_style="red", padding=(0, 2)))
    else:
        console.print("\n[green]✓ Todos los indicadores en verde.[/green]")

    # Guardar snapshot
    _save_reputation_snapshot(alias, {
        "nivel": nivel,
        "power_seller": power_seller,
        "ventas_total": ventas_total,
        "reclamos_pct": round(claims_pct, 2),
        "demoras_pct": round(delayed_pct, 2),
        "cancelaciones_pct": round(cancels_pct, 2),
    })
    console.print(f"[dim]Snapshot guardado en data/reputacion_{alias.replace(' ', '_')}.json[/dim]")


# ── Entry point combinado ─────────────────────────────────────────────────────

def run(client: MLClient, alias: str, auto_responder: bool = False):
    """Ejecuta ambas funciones: preguntas y reputación."""
    run_reputacion(client, alias)
    run_preguntas(client, alias, auto_responder=auto_responder)
