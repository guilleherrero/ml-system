"""
Módulo 7 — Stock y rentabilidad
Monitorea el stock de tus publicaciones, calcula el margen real por producto
y predice cuándo vas a quedar sin stock según la velocidad de ventas.

Funciones:
  - Stock actual y disponible por publicación
  - Margen real: precio - comisión ML - envío estimado - costo
  - Velocidad de ventas (unidades/día en los últimos 30 días)
  - Días de stock restantes basado en velocidad
  - Alertas por stock crítico (<7 días) o margen negativo
  - Ranking de productos más y menos rentables
"""

import json
import os
import time
import requests as _req
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich import box

from core.ml_client import MLClient
from core.fees import get_fee_rates, get_rate
from modules.monitor_posicionamiento import _get_all_active_items
from modules.seo_optimizer import _tokenize

console = Console()
DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
COSTOS_PATH = os.path.join(CONFIG_DIR, "costos.json")

# Días de análisis para velocidad de ventas
DIAS_ANALISIS = 30

# Umbrales de alerta
DIAS_STOCK_CRITICO   = 7
DIAS_STOCK_ADVERTENCIA = 15
MARGEN_MINIMO_PCT    = 0.10   # 10%


# ── Costos ────────────────────────────────────────────────────────────────────

def _load_costos() -> dict:
    if not os.path.exists(COSTOS_PATH):
        return {}
    with open(COSTOS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_costos(costos: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(COSTOS_PATH, "w", encoding="utf-8") as f:
        json.dump(costos, f, indent=2, ensure_ascii=False)


# ── Ventas y fees reales ──────────────────────────────────────────────────────

def _get_all_orders_30d(client: MLClient) -> list[dict]:
    """
    Trae todas las órdenes pagadas de los últimos 30 días con paginación completa.
    Se llama UNA sola vez y se reutiliza para velocidad + fee_rate.
    """
    user_id = client.account.user_id
    fecha_desde = (datetime.now() - timedelta(days=DIAS_ANALISIS)).strftime("%Y-%m-%dT00:00:00.000-03:00")
    orders = []
    offset = 0
    while True:
        try:
            data = client._get(
                "/orders/search",
                params={
                    "seller": user_id,
                    "order.status": "paid",
                    "order.date_created.from": fecha_desde,
                    "limit": 50,
                    "offset": offset,
                    "sort": "date_desc",
                },
            )
            results = data.get("results", [])
            orders.extend(results)
            total = data.get("paging", {}).get("total", 0)
            offset += len(results)
            if not results or offset >= total:
                break
            time.sleep(0.1)
        except Exception:
            break
    return orders


def _compute_item_stats(orders: list[dict]) -> dict:
    """
    A partir de todas las órdenes, calcula por item:
      - total_units: unidades vendidas en 30d
      - fee_rates: lista de sale_fee/precio por orden (para promedio real)
    Devuelve {item_id: {'units': N, 'fee_rate': F}}
    """
    stats: dict[str, dict] = {}
    for order in orders:
        for oi in order.get("order_items", []):
            item_id = (oi.get("item") or {}).get("id")
            if not item_id:
                continue
            qty       = oi.get("quantity", 0)
            price     = float(oi.get("unit_price") or 0)
            sale_fee  = oi.get("sale_fee")

            if item_id not in stats:
                stats[item_id] = {"units": 0, "fee_amounts": [], "prices": []}
            stats[item_id]["units"] += qty
            if sale_fee is not None and price > 0:
                stats[item_id]["fee_amounts"].append(float(sale_fee))
                stats[item_id]["prices"].append(price * qty)

    result = {}
    for item_id, s in stats.items():
        total_fees   = sum(s["fee_amounts"])
        total_income = sum(s["prices"])
        fee_rate = (total_fees / total_income) if total_income > 0 else None
        result[item_id] = {
            "units":    s["units"],
            "fee_rate": round(fee_rate, 4) if fee_rate else None,
        }
    return result


def _get_visits_all(item_ids: list[str], client: MLClient) -> dict[str, int]:
    """
    Trae el total de visitas en los últimos 30 días para cada item.
    Devuelve {item_id: total_visits}.
    """
    visits = {}
    token = client.account.access_token
    heads = {"Authorization": f"Bearer {token}"}
    for item_id in item_ids:
        try:
            r = _req.get(
                f"https://api.mercadolibre.com/items/{item_id}/visits/time_window",
                headers=heads,
                params={"last": 30, "unit": "day"},
                timeout=8,
            )
            if r.ok:
                visits[item_id] = r.json().get("total_visits", 0)
        except Exception:
            pass
        time.sleep(0.1)
    return visits


def _get_items_with_stock(client: MLClient) -> list[dict]:
    """Trae publicaciones activas con su stock actual."""
    items = _get_all_active_items(client)
    result = []
    for item in items:
        available = item.get("available_quantity", 0)
        listing_type = item.get("listing_type_id", "gold_special")
        free_shipping = (
            item.get("shipping", {}).get("free_shipping", False)
            or item.get("shipping", {}).get("mode") == "me2"
        )
        result.append({
            "id":            item["id"],
            "titulo":        item.get("title", ""),        # título completo para SEO
            "titulo_corto":  item.get("title", "")[:60],  # solo para display
            "precio":        float(item.get("price", 0)),
            "stock":         available,
            "listing_type":  listing_type,
            "free_shipping": free_shipping,
            "category_id":   item.get("category_id", ""),
            "family_id":     item.get("family_id"),
        })
    return result


# ── Margen ────────────────────────────────────────────────────────────────────


def _calcular_margen(
    precio: float,
    costo: float | None,
    listing_type: str,
    real_fee_rate: float | None = None,
    fees: dict | None = None,
) -> dict:
    """
    Calcula fee real (comisión + IVA + envío incluido) y margen.
    Usa real_fee_rate de órdenes históricas si está disponible.
    Si no, usa las tasas de core/fees.py (obtenidas de la API de ML).
    El fee_rate cubre TODO (comisión ML, IVA, costo de envío a cargo del vendedor).
    """
    if real_fee_rate and real_fee_rate > 0:
        fee_rate = real_fee_rate
        fee_source = "real"
    else:
        fee_rate = get_rate(listing_type, fees)
        fee_source = "api" if fees and "_updated_at" in fees else "estimado"

    # El fee_rate cubre comisión + IVA + envío → todo en uno
    total_fee = round(precio * fee_rate, 2)
    ingresos_netos = precio - total_fee

    if costo and costo > 0:
        ganancia   = ingresos_netos - costo
        margen_pct = ganancia / precio if precio > 0 else 0
    else:
        ganancia   = None
        margen_pct = None

    return {
        "comision":     total_fee,   # renombrado conceptualmente: todo el costo ML
        "fee_rate":     fee_rate,
        "fee_source":   fee_source,
        "envio_est":    0,           # ya incluido en fee_rate
        "neto":         ingresos_netos,
        "ganancia":     ganancia,
        "margen_pct":   margen_pct,
    }


# ── Setup de costos ───────────────────────────────────────────────────────────

def setup_costos(client: MLClient, alias: str):
    """
    Permite ingresar el costo de cada publicación para calcular margen real.
    Solo pide los que no tienen costo cargado.
    """
    console.print(f"\n[bold cyan]Cargar costos — {alias}[/bold cyan]\n")
    console.print("[dim]Ingresá el costo de cada producto (lo que te costó a vos, sin envío ni comisión).[/dim]")
    console.print("[dim]Presioná Enter para saltar una publicación.\n[/dim]")

    items = _get_items_with_stock(client)
    costos = _load_costos()

    sin_costo = [i for i in items if i["id"] not in costos or not costos[i["id"]].get("costo")]

    if not sin_costo:
        console.print("[green]✓ Todos los productos tienen costo cargado.[/green]")
        return

    console.print(f"[yellow]{len(sin_costo)} productos sin costo cargado.[/yellow]\n")

    guardados = 0
    for item in sin_costo:
        console.print(f"  [bold]{item['titulo']}[/bold]  [dim]${item['precio']:,.0f}[/dim]")
        costo_str = Prompt.ask("  Costo (Enter para saltar)", default="")
        if not costo_str.strip():
            continue
        try:
            costo = float(costo_str.strip().replace(",", "."))
            costos[item["id"]] = {
                "alias":   alias,
                "titulo":  item["titulo"],
                "costo":   costo,
                "updated": datetime.now().strftime("%Y-%m-%d"),
            }
            guardados += 1
        except ValueError:
            console.print("  [red]Valor inválido, saltando.[/red]")

    _save_costos(costos)
    console.print(f"\n[green]✓ {guardados} costos guardados en config/costos.json[/green]")


# ── Run principal ─────────────────────────────────────────────────────────────

def run(client: MLClient, alias: str, mostrar_todos: bool = False):
    """
    Analiza stock y rentabilidad de todas las publicaciones activas.

    Args:
        mostrar_todos: Si False, solo muestra los que necesitan atención.
    """
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(f"\n[bold cyan]Stock y rentabilidad — {alias}[/bold cyan]")
    console.print(f"Fecha: {today}\n")

    items = _get_items_with_stock(client)
    if not items:
        console.print("[yellow]Sin publicaciones activas.[/yellow]")
        return

    costos = _load_costos()

    # Cargar tasas de comisión desde la API de ML (auto-refresca si tiene >7 días)
    fees = get_fee_rates(client)
    fee_src = "API de ML" if fees.get("_updated_at") else "fallback"
    console.print(f"[dim]Comisiones: {fee_src} ({fees.get('_updated_at', 'valores estimados')})[/dim]")

    console.print(f"[dim]Analizando {len(items)} publicaciones...[/dim]")
    console.print(f"[dim]Obteniendo órdenes y fees reales de los últimos {DIAS_ANALISIS} días...[/dim]")

    # Keywords top por categoría — una búsqueda por categoría, no por item
    console.print(f"[dim]Obteniendo keywords SEO por categoría...[/dim]")
    seo_keywords_cat = _get_seo_keywords_por_categoria(items, client)

    # Un solo fetch de todas las órdenes → velocidad + fee_rate real por item
    all_orders = _get_all_orders_30d(client)
    item_stats = _compute_item_stats(all_orders)
    console.print(f"[dim]{len(all_orders)} órdenes procesadas, {len(item_stats)} productos con datos de fee.[/dim]")

    # Visitas por item (30 días)
    console.print(f"[dim]Obteniendo visitas de {len(items)} publicaciones...[/dim]")
    item_ids = [i["id"] for i in items]
    visits_map = _get_visits_all(item_ids, client)
    console.print(f"[dim]{len(visits_map)} items con datos de visitas.\n[/dim]")

    resultados = []

    for item in items:
        item_id = item["id"]
        precio  = item["precio"]
        stock   = item["stock"]
        costo_data = costos.get(item_id, {})
        costo   = costo_data.get("costo") if costo_data else None

        # Velocidad y fee_rate real desde las órdenes ya descargadas
        stats         = item_stats.get(item_id, {})
        ventas_30d    = stats.get("units", 0)
        velocidad     = round(ventas_30d / DIAS_ANALISIS, 2)
        real_fee_rate = stats.get("fee_rate")

        # Visitas y conversión
        visitas_30d    = visits_map.get(item_id, 0)
        conversion_pct = round(ventas_30d / visitas_30d * 100, 2) if visitas_30d > 0 else None
        dias_stock = round(stock / velocidad, 0) if velocidad > 0 else None

        # Margen con fee real (o tasa de API ML si no hay historial)
        margen_data = _calcular_margen(
            precio, costo, item["listing_type"],
            real_fee_rate=real_fee_rate,
            fees=fees,
        )

        # Nivel de alerta
        alerta_stock = None
        if stock == 0:
            alerta_stock = "SIN_STOCK"
        elif dias_stock is not None:
            if dias_stock <= DIAS_STOCK_CRITICO:
                alerta_stock = "CRITICO"
            elif dias_stock <= DIAS_STOCK_ADVERTENCIA:
                alerta_stock = "ADVERTENCIA"

        alerta_margen = None
        if margen_data["margen_pct"] is not None:
            if margen_data["margen_pct"] < 0:
                alerta_margen = "NEGATIVO"
            elif margen_data["margen_pct"] < MARGEN_MINIMO_PCT:
                alerta_margen = "BAJO"

        # SEO: keywords de la categoría vs cobertura en el título
        cat_keywords = seo_keywords_cat.get(item.get("category_id", ""), [])
        seo_gaps     = _check_seo_gaps(item["titulo"], set(), cat_keywords)

        resultados.append({
            "id":           item_id,
            "titulo":       item["titulo_corto"],  # display
            "precio":       precio,
            "stock":        stock,
            "costo":        costo,
            "listing_type": item["listing_type"],
            "free_shipping": item["free_shipping"],
            "velocidad":    velocidad,
            "dias_stock":   dias_stock,
            "fee_rate":      margen_data["fee_rate"],
            "fee_source":    margen_data["fee_source"],
            "ventas_30d":    ventas_30d,
            "visitas_30d":   visitas_30d,
            "conversion_pct": conversion_pct,
            "neto":          margen_data["neto"],
            "ganancia":     margen_data["ganancia"],
            "margen_pct":   margen_data["margen_pct"],
            "comision":     margen_data["comision"],
            "envio_est":    0,
            "alerta_stock":      alerta_stock,
            "alerta_margen":     alerta_margen,
            "seo_gaps":          seo_gaps,
            "seo_all_keywords":  cat_keywords,
        })

    # Mostrar tabla
    _show_stock_table(resultados, mostrar_todos)

    # Alertas críticas
    _show_alertas(resultados)

    # Diagnóstico SEO
    _show_seo_diagnostico(resultados)

    # Ranking rentabilidad
    _show_ranking(resultados)

    # Guardar snapshot
    _save_snapshot(alias, resultados)

    return resultados


def _dias_label(dias: float | None) -> str:
    if dias is None:
        return "[dim]—[/dim]"
    if dias <= DIAS_STOCK_CRITICO:
        return f"[red]{dias:.0f}d[/red]"
    elif dias <= DIAS_STOCK_ADVERTENCIA:
        return f"[yellow]{dias:.0f}d[/yellow]"
    return f"[green]{dias:.0f}d[/green]"


def _margen_label(pct: float | None) -> str:
    if pct is None:
        return "[dim]—[/dim]"
    val = pct * 100
    if val < 0:
        return f"[red]{val:.1f}%[/red]"
    elif val < MARGEN_MINIMO_PCT * 100:
        return f"[yellow]{val:.1f}%[/yellow]"
    return f"[green]{val:.1f}%[/green]"


def _show_stock_table(resultados: list[dict], mostrar_todos: bool):
    # Filtrar si solo queremos los que necesitan atención
    if not mostrar_todos:
        filtrados = [r for r in resultados if r["alerta_stock"] or r["alerta_margen"]]
        if not filtrados:
            console.print("[green]✓ Sin alertas de stock ni margen. Usá --todos para ver todo.[/green]\n")
            return
        console.print(f"[yellow]{len(filtrados)} publicaciones requieren atención:[/yellow]\n")
    else:
        filtrados = resultados

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("Publicación",  ratio=3, no_wrap=True)
    table.add_column("Precio",       justify="right", min_width=9)
    table.add_column("Stock",        justify="right", min_width=6)
    table.add_column("Vel/día",      justify="right", min_width=7)
    table.add_column("Días",         justify="right", min_width=6)
    table.add_column("Conv%",        justify="right", min_width=7)
    table.add_column("Neto",         justify="right", min_width=9)
    table.add_column("Margen",       justify="right", min_width=8)

    # Ordenar: sin stock primero, luego por días de stock asc
    def sort_key(r):
        if r["stock"] == 0:
            return -1
        return r["dias_stock"] if r["dias_stock"] is not None else 9999

    for r in sorted(filtrados, key=sort_key):
        titulo = r["titulo"][:45] + "…" if len(r["titulo"]) > 45 else r["titulo"]

        # Stock
        if r["stock"] == 0:
            stock_str = "[red bold]0 ⚠[/red bold]"
        elif r["alerta_stock"] == "CRITICO":
            stock_str = f"[red]{r['stock']}[/red]"
        elif r["alerta_stock"] == "ADVERTENCIA":
            stock_str = f"[yellow]{r['stock']}[/yellow]"
        else:
            stock_str = str(r["stock"])

        vel_str  = f"{r['velocidad']:.1f}" if r["velocidad"] > 0 else "[dim]0[/dim]"
        conv_pct = r.get("conversion_pct")
        if conv_pct is None:
            conv_str = "[dim]—[/dim]"
        elif conv_pct >= 3:
            conv_str = f"[green]{conv_pct:.1f}%[/green]"
        elif conv_pct >= 1:
            conv_str = f"[yellow]{conv_pct:.1f}%[/yellow]"
        else:
            conv_str = f"[red]{conv_pct:.1f}%[/red]"

        table.add_row(
            titulo,
            f"${r['precio']:,.0f}",
            stock_str,
            vel_str,
            _dias_label(r["dias_stock"]),
            conv_str,
            f"${r['neto']:,.0f}",
            _margen_label(r["margen_pct"]),
        )

    console.print(table)


def _show_alertas(resultados: list[dict]):
    sin_stock   = [r for r in resultados if r["stock"] == 0]
    criticos    = [r for r in resultados if r["alerta_stock"] == "CRITICO"]
    advertencia = [r for r in resultados if r["alerta_stock"] == "ADVERTENCIA"]
    margen_neg  = [r for r in resultados if r["alerta_margen"] == "NEGATIVO"]
    margen_bajo = [r for r in resultados if r["alerta_margen"] == "BAJO"]

    lineas = []
    if sin_stock:
        lineas.append(f"  [red bold]⛔ SIN STOCK ({len(sin_stock)}):[/red bold]")
        for r in sin_stock:
            lineas.append(f"     • {r['titulo'][:55]}")
    if criticos:
        lineas.append(f"\n  [red]⚠ STOCK CRÍTICO — menos de {DIAS_STOCK_CRITICO} días ({len(criticos)}):[/red]")
        for r in criticos:
            lineas.append(f"     • {r['titulo'][:50]}  → {r['dias_stock']:.0f} días")
    if advertencia:
        lineas.append(f"\n  [yellow]⚡ STOCK BAJO — menos de {DIAS_STOCK_ADVERTENCIA} días ({len(advertencia)}):[/yellow]")
        for r in advertencia:
            lineas.append(f"     • {r['titulo'][:50]}  → {r['dias_stock']:.0f} días")
    if margen_neg:
        lineas.append(f"\n  [red]💸 MARGEN NEGATIVO ({len(margen_neg)}):[/red]")
        for r in margen_neg:
            pct = r["margen_pct"] * 100
            lineas.append(f"     • {r['titulo'][:50]}  → {pct:.1f}%")
    if margen_bajo:
        lineas.append(f"\n  [yellow]📉 MARGEN BAJO <{MARGEN_MINIMO_PCT*100:.0f}% ({len(margen_bajo)}):[/yellow]")
        for r in margen_bajo:
            pct = r["margen_pct"] * 100
            lineas.append(f"     • {r['titulo'][:50]}  → {pct:.1f}%")

    if lineas:
        console.print(Panel(
            "\n".join(lineas),
            title="[bold red]Alertas[/bold red]",
            border_style="red",
            padding=(0, 2),
        ))


def _show_ranking(resultados: list[dict]):
    con_margen = [r for r in resultados if r["margen_pct"] is not None]
    if not con_margen:
        console.print("\n[dim]Cargá costos para ver el ranking de rentabilidad (python main.py costos).[/dim]")
        return

    ordenados = sorted(con_margen, key=lambda r: r["margen_pct"] or 0, reverse=True)

    table = Table(
        title="Ranking de rentabilidad",
        box=box.SIMPLE,
        show_header=True,
        header_style="bold dim",
        expand=True,
    )
    table.add_column("#",       width=3)
    table.add_column("Publicación", ratio=3, no_wrap=True)
    table.add_column("Precio",  justify="right", min_width=9)
    table.add_column("Costo",   justify="right", min_width=9)
    table.add_column("Neto",    justify="right", min_width=9)
    table.add_column("Ganancia",justify="right", min_width=9)
    table.add_column("Margen",  justify="right", min_width=8)

    for i, r in enumerate(ordenados, 1):
        titulo = r["titulo"][:45] + "…" if len(r["titulo"]) > 45 else r["titulo"]
        ganancia_str = f"${r['ganancia']:,.0f}" if r["ganancia"] is not None else "—"
        costo_str    = f"${r['costo']:,.0f}"    if r["costo"]    is not None else "—"
        table.add_row(
            str(i), titulo,
            f"${r['precio']:,.0f}",
            costo_str,
            f"${r['neto']:,.0f}",
            ganancia_str,
            _margen_label(r["margen_pct"]),
        )

    console.print(table)


# ── Diagnóstico SEO ───────────────────────────────────────────────────────────

def _keywords_from_titles(titulos: list[str]) -> list[str]:
    """
    Extrae las palabras más frecuentes de una lista de títulos.
    Fallback cuando la API de búsqueda no está disponible.
    Devuelve las top 15 palabras clave ordenadas por frecuencia.
    """
    from collections import Counter
    counter: Counter = Counter()
    for t in titulos:
        for w in set(_tokenize(t)):
            counter[w] += 1
    total = len(titulos) if titulos else 1
    # Solo palabras que aparecen en al menos 20% de los títulos
    return [w for w, c in counter.most_common(20) if c / total >= 0.2][:15]


def _get_seo_keywords_por_categoria(items: list[dict], client) -> dict:
    """
    Para cada categoría presente en los items, obtiene keywords top.
    Estrategia:
      1. Trends de ML por categoría (lo mejor: búsquedas reales del mercado)
      2. Frecuencia de palabras en los propios títulos del vendedor en esa categoría
    Devuelve {category_id: [keyword, ...]} con las top 15 keywords.
    """
    client._ensure_token()
    token = client.account.access_token
    categorias: dict[str, list[str]] = {}

    # Agrupar items por categoría para el fallback
    items_por_cat: dict[str, list[str]] = {}
    for item in items:
        cat_id = item.get("category_id", "")
        if cat_id:
            items_por_cat.setdefault(cat_id, []).append(item.get("titulo", ""))

    cats_vistas = set()
    for item in items:
        cat_id = item.get("category_id", "")
        if not cat_id or cat_id in cats_vistas:
            continue
        cats_vistas.add(cat_id)

        # Nombre real de la categoría
        cat_name = ""
        try:
            rc = _req.get(
                f"https://api.mercadolibre.com/categories/{cat_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=6,
            )
            if rc.ok:
                cat_name = rc.json().get("name", "")
        except Exception:
            pass

        # Análisis de frecuencia sobre los títulos propios del vendedor en esa categoría.
        # Esto es más relevante que los trends globales de ML, que no filtran por categoría.
        # Las palabras que aparecen en VARIOS de tus propios títulos exitosos son las más importantes.
        titulos_cat = items_por_cat.get(cat_id, [])
        kws = _keywords_from_titles(titulos_cat) if len(titulos_cat) > 1 else _tokenize(titulos_cat[0] if titulos_cat else "")

        if kws:
            console.print(f"  [dim]  {cat_name or cat_id}: {len(kws)} keywords[/dim]")
        categorias[cat_id] = kws

    return categorias


def _kw_in_titulo(kw: str, titulo_words: set) -> bool:
    """
    Verifica si una keyword (palabra suelta o frase) está cubierta en el título.
    Para frases: verifica que TODAS las palabras de la frase estén en el título.
    """
    kw_words = set(_tokenize(kw))
    if not kw_words:
        return False
    return kw_words.issubset(titulo_words)


def _check_seo_gaps(titulo: str, description_words: set, top_keywords: list[str]) -> list[str]:
    """
    Devuelve las keywords que rankean bien en la categoría pero no están
    cubiertas en el título ni en la descripción del item.
    Funciona tanto con palabras sueltas como con frases completas.
    """
    titulo_words = set(_tokenize(titulo)) | description_words
    return [kw for kw in top_keywords if not _kw_in_titulo(kw, titulo_words)]


def _show_seo_diagnostico(resultados: list[dict]):
    """
    Muestra el diagnóstico SEO completo por publicación:
      - Score de cobertura de keywords (X/Y)
      - Keywords presentes en el título ✓
      - Keywords faltantes que rankean en ML ✗
    Siempre se muestra, aunque no haya gaps.
    """
    # Mostrar todos los items que tienen datos SEO (aunque la lista esté vacía)
    con_seo = [r for r in resultados if "seo_all_keywords" in r]
    if not con_seo:
        console.print("[dim]SEO: no se pudieron obtener keywords de la categoría.[/dim]\n")
        return

    # Si todas las listas de keywords están vacías, avisar con más detalle
    if all(len(r.get("seo_all_keywords", [])) == 0 for r in con_seo):
        console.print("[yellow]SEO: las keywords de la categoría no pudieron obtenerse (verificar conexión ML).[/yellow]\n")
        return

    lineas = []
    for r in con_seo:
        all_kw     = r.get("seo_all_keywords", [])
        gaps       = r.get("seo_gaps", [])
        titulo     = r["titulo"][:52]
        presentes  = [k for k in all_kw if k not in gaps]
        total      = len(all_kw)
        score      = len(presentes)

        if total == 0:
            continue

        # Score con color
        pct = score / total
        if pct >= 0.7:
            score_color = "green"
        elif pct >= 0.4:
            score_color = "yellow"
        else:
            score_color = "red"

        lineas.append(f"  [bold]{titulo}[/bold]  [{score_color}]{score}/{total} keywords[/{score_color}]")

        # Keywords presentes
        if presentes:
            pres_str = "  ".join(f"[green]✓ {k}[/green]" for k in presentes[:6])
            lineas.append(f"    {pres_str}")

        # Keywords faltantes
        if gaps:
            gaps_str = "  ".join(f"[yellow]✗ {k}[/yellow]" for k in gaps[:6])
            lineas.append(f"    {gaps_str}")

        lineas.append("")

    if not lineas:
        return

    console.print(Panel(
        "\n".join(lineas).rstrip(),
        title="[bold cyan]Diagnóstico SEO — Cobertura de keywords por publicación[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))


def _save_snapshot(alias: str, resultados: list[dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    safe = alias.replace(" ", "_").replace("/", "-")
    path = os.path.join(DATA_DIR, f"stock_{safe}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"fecha": datetime.now().strftime("%Y-%m-%d %H:%M"), "items": resultados},
            f, indent=2, ensure_ascii=False,
        )
    console.print(f"\n[dim]Guardado en data/stock_{safe}.json[/dim]")
