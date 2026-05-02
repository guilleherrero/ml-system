#!/usr/bin/env python3
"""
Sistema ML — CLI principal

Cuentas:
  python main.py agregar              Agregar una cuenta nueva
  python main.py listar               Listar cuentas configuradas
  python main.py verificar            Verificar conexión de todas las cuentas
  python main.py eliminar             Eliminar una cuenta

Módulos:
  python main.py posiciones           Módulo 1 — Monitor de posicionamiento
  python main.py posiciones "Cuenta 2"  (cuenta específica)

  python main.py competencia          Módulo 2 — Análisis de competencia
  python main.py competencia "Cuenta 2" 5   (cuenta específica, max 5 categorías)

  python main.py todo                 Corre módulo 1 y 2 en todas las cuentas
"""

# Timezone Argentina — debe setearse ANTES de cualquier import de datetime/time
# para que datetime.now() y time.localtime() devuelvan hora local de ART.
# Render corre por defecto en UTC; sin esto las pantallas muestran +3h.
import os
os.environ['TZ'] = 'America/Argentina/Buenos_Aires'
import time as _tz_time
try:
    _tz_time.tzset()  # Unix/Mac/Linux — aplica el cambio al proceso actual
except AttributeError:
    pass  # Windows no tiene tzset (no usado en producción)

import sys
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich import box

from core.account_manager import AccountManager
from core.ml_client import MLApiError

console = Console()
manager = AccountManager()


# ── Cuentas ───────────────────────────────────────────────────────────────────

def cmd_agregar():
    console.print("\n[bold cyan]Agregar cuenta de MercadoLibre[/bold cyan]")
    alias = Prompt.ask("Nombre/alias (ej: Fajas y ortopedia)")
    client_id = Prompt.ask("Client ID (App ID)")
    client_secret = Prompt.ask("Client Secret", password=True)
    refresh_token = Prompt.ask("Refresh Token")
    try:
        manager.add_account(alias, client_id, client_secret, refresh_token)
        console.print(f"\n[green]✓ Cuenta '{alias}' guardada.[/green]")
        if Confirm.ask("¿Verificar conexión ahora?", default=True):
            _verificar_cuenta(alias)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")


def cmd_listar():
    accounts = manager.list_accounts()
    if not accounts:
        console.print("[yellow]No hay cuentas. Usá 'agregar' para añadir una.[/yellow]")
        return
    table = Table(title="Cuentas configuradas", box=box.ROUNDED)
    table.add_column("#", style="dim", width=3)
    table.add_column("Alias", style="bold")
    table.add_column("User ID", style="cyan")
    table.add_column("Nickname", style="green")
    table.add_column("Estado", justify="center")
    for i, acc in enumerate(accounts, 1):
        estado = "[green]Activa[/green]" if acc.active else "[dim]Inactiva[/dim]"
        table.add_row(str(i), acc.alias, str(acc.user_id or "—"), acc.nickname or "—", estado)
    console.print(table)


def _verificar_cuenta(alias: str):
    console.print(f"  Conectando con '{alias}'...", end=" ")
    try:
        client = manager.get_client(alias)
        me = client.get_me()
        account = manager.get_account(alias)
        account.nickname = me.get("nickname", "")
        account.user_id = me.get("id")
        manager._save()
        console.print(f"[green]✓ {me['nickname']} (ID: {me['id']})[/green]")
    except MLApiError as e:
        console.print(f"[red]✗ {e}[/red]")


def cmd_verificar():
    accounts = manager.list_accounts()
    if not accounts:
        console.print("[yellow]No hay cuentas configuradas.[/yellow]")
        return
    console.print("\n[bold]Verificando conexión...[/bold]")
    for acc in accounts:
        _verificar_cuenta(acc.alias)


def cmd_eliminar():
    accounts = manager.list_accounts()
    if not accounts:
        console.print("[yellow]No hay cuentas configuradas.[/yellow]")
        return
    cmd_listar()
    alias = Prompt.ask("\nAlias de la cuenta a eliminar")
    if Confirm.ask(f"¿Seguro que querés eliminar '{alias}'?", default=False):
        try:
            manager.remove_account(alias)
            console.print(f"[green]✓ Cuenta '{alias}' eliminada.[/green]")
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")


# ── Módulos ───────────────────────────────────────────────────────────────────

def _resolve_accounts(alias_arg: str | None) -> list:
    """Devuelve lista de cuentas activas. Si se especifica alias, solo esa."""
    accounts = manager.list_accounts()
    if not accounts:
        console.print("[yellow]No hay cuentas configuradas.[/yellow]")
        return []
    if alias_arg:
        acc = manager.get_account(alias_arg)
        if not acc:
            console.print(f"[red]Cuenta '{alias_arg}' no encontrada.[/red]")
            return []
        return [acc]
    return [a for a in accounts if a.active]


def cmd_posiciones():
    from modules.monitor_posicionamiento import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias)


def cmd_competencia():
    from modules.analisis_competencia import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    max_cats = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias, max_categories=max_cats)


def cmd_lanzar():
    from modules.lanzador_productos import run
    product_idea = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
    accounts = manager.list_accounts()
    if not accounts:
        console.print("[yellow]No hay cuentas configuradas.[/yellow]")
        return
    client = manager.get_client(accounts[0].alias)
    run(client=client, product_idea=product_idea)


def cmd_optimizar():
    from modules.optimizador_publicaciones import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    max_items = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias, max_items=max_items)


def cmd_preguntas():
    from modules.preguntas_reputacion import run_preguntas
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    auto = "--auto" in sys.argv
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run_preguntas(client, acc.alias, auto_responder=auto)


def cmd_reputacion():
    from modules.preguntas_reputacion import run_reputacion
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run_reputacion(client, acc.alias)


def cmd_repricing_setup():
    from modules.repricing import setup
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        setup(client, acc.alias)


def cmd_repricing():
    from modules.repricing import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    dry_run = "--apply" not in sys.argv
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias, dry_run=dry_run)


def cmd_dashboard():
    from modules.dashboard import run
    run(manager)


def cmd_historial():
    from modules.historial import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    # El tercer arg puede ser "reputacion", "posiciones" o un número de días
    modulo = None
    dias   = 7
    if len(sys.argv) > 3:
        arg3 = sys.argv[3]
        if arg3.isdigit():
            dias = int(arg3)
        else:
            modulo = arg3
    if len(sys.argv) > 4 and sys.argv[4].isdigit():
        dias = int(sys.argv[4])

    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        run(acc.alias, modulo=modulo, dias=dias)


def cmd_multicuenta():
    from modules.multicuenta import run
    modo = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("resumen", "cruzada") else None
    run(manager, modo=modo)


def cmd_radar():
    from modules.radar_oportunidades import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    modo = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] in ("radar", "calendario") else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias, modo=modo)


def cmd_full():
    from modules.full_manager import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias)


def cmd_full_setup():
    from modules.full_manager import setup
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        setup(client, acc.alias)


def cmd_reposicion():
    from modules.reposicion import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias)


def cmd_reposicion_setup():
    from modules.reposicion import setup
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        setup(client, acc.alias)


def cmd_stock_rentabilidad():
    from modules.stock_rentabilidad import run
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    mostrar_todos = "--todos" in sys.argv
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run(client, acc.alias, mostrar_todos=mostrar_todos)


def cmd_costos():
    from modules.stock_rentabilidad import setup_costos
    alias_arg = sys.argv[2] if len(sys.argv) > 2 else None
    accounts = _resolve_accounts(alias_arg)
    for acc in accounts:
        client = manager.get_client(acc.alias)
        setup_costos(client, acc.alias)


def cmd_comisiones():
    from core.fees import fetch_from_api, get_fee_rates, LISTING_TYPES
    accounts = manager.list_accounts()
    if not accounts:
        console.print("[yellow]No hay cuentas configuradas.[/yellow]")
        return
    force = "--refresh" in sys.argv
    client = manager.get_client(accounts[0].alias)
    if force:
        console.print("[dim]Consultando la API de MercadoLibre...[/dim]")
        fees = fetch_from_api(client)
        console.print("[green]✓ Comisiones actualizadas desde la API de ML.[/green]\n")
    else:
        fees = get_fee_rates(client)
    table = Table(title="Comisiones actuales — MercadoLibre Argentina", box=box.ROUNDED)
    table.add_column("Tipo de publicación", style="bold")
    table.add_column("Tasa efectiva", justify="right", style="cyan")
    table.add_column("Ejemplo $10.000", justify="right")
    for lt in LISTING_TYPES:
        rate = fees.get(lt)
        if rate:
            table.add_row(lt, f"{rate*100:.2f}%", f"${10000*rate:,.0f}")
    console.print(table)
    updated_at = fees.get("_updated_at", "desconocido")
    src = "API de ML" if fees.get("_updated_at") else "valores de fallback"
    console.print(f"\n[dim]Fuente: {src} | Última actualización: {updated_at}[/dim]")
    console.print("[dim]Usá 'comisiones --refresh' para forzar actualización.[/dim]")


def cmd_todo():
    from modules.monitor_posicionamiento import run as run_pos
    from modules.analisis_competencia import run as run_comp
    accounts = [a for a in manager.list_accounts() if a.active]
    if not accounts:
        console.print("[yellow]No hay cuentas activas.[/yellow]")
        return
    for acc in accounts:
        client = manager.get_client(acc.alias)
        run_pos(client, acc.alias)
        run_comp(client, acc.alias)


# ── Dispatcher ────────────────────────────────────────────────────────────────

COMMANDS = {
    "dashboard":        cmd_dashboard,
    "historial":        cmd_historial,
    "agregar":          cmd_agregar,
    "listar":           cmd_listar,
    "verificar":        cmd_verificar,
    "eliminar":         cmd_eliminar,
    "posiciones":       cmd_posiciones,
    "competencia":      cmd_competencia,
    "lanzar":           cmd_lanzar,
    "optimizar":        cmd_optimizar,
    "repricing-setup":  cmd_repricing_setup,
    "repricing":        cmd_repricing,
    "preguntas":        cmd_preguntas,
    "reputacion":       cmd_reputacion,
    "multicuenta":        cmd_multicuenta,
    "radar":              cmd_radar,
    "full":               cmd_full,
    "full-setup":         cmd_full_setup,
    "reposicion":         cmd_reposicion,
    "reposicion-setup":   cmd_reposicion_setup,
    "stock-rentabilidad": cmd_stock_rentabilidad,
    "costos":           cmd_costos,
    "comisiones":       cmd_comisiones,
    "todo":             cmd_todo,
}

HELP = """
[bold cyan]Sistema ML[/bold cyan] — Comandos disponibles

[bold]Cuentas:[/bold]
  [cyan]agregar[/cyan]                  Agregar una cuenta nueva
  [cyan]listar[/cyan]                   Ver cuentas configuradas
  [cyan]verificar[/cyan]                Verificar conexión con ML
  [cyan]eliminar[/cyan]                 Eliminar una cuenta

[bold]Resumen:[/bold]
  [cyan]dashboard[/cyan]                Vista de salud de todas las cuentas (preguntas, rep, stock, posiciones)
  [cyan]historial "Cuenta 1"[/cyan]     Tendencias de reputación y posiciones
  [cyan]historial "Cuenta 1" reputacion[/cyan]  Solo reputación
  [cyan]historial "Cuenta 1" posiciones 14[/cyan]  Posiciones, últimos 14 días

[bold]Módulos:[/bold]
  [cyan]posiciones[/cyan]               Módulo 1 — Monitor de posicionamiento (todas las cuentas)
  [cyan]posiciones "Cuenta 1"[/cyan]    Módulo 1 — Solo una cuenta
  [cyan]competencia[/cyan]              Módulo 2 — Análisis de competencia (todas)
  [cyan]competencia "Cuenta 1" 5[/cyan] Módulo 2 — Una cuenta, máx. 5 categorías
  [cyan]lanzar[/cyan]                   Módulo 4 — Analizar y planificar el lanzamiento de un producto nuevo
  [cyan]lanzar "faja postparto"[/cyan] Módulo 4 — Lanzamiento directo con idea específica
  [cyan]optimizar[/cyan]                Módulo 3 — Optimizar títulos y descripciones con IA (5 pubs)
  [cyan]optimizar "Cuenta 1" 10[/cyan] Módulo 3 — Una cuenta, hasta 10 publicaciones
  [cyan]preguntas[/cyan]                Módulo 6 — Responder preguntas con IA (revisa antes de enviar)
  [cyan]preguntas --auto[/cyan]         Módulo 6 — Responder automáticamente sin confirmar
  [cyan]reputacion[/cyan]               Módulo 6 — Ver métricas de reputación y alertas
  [cyan]repricing-setup[/cyan]          Módulo 5 — Configurar precios mín/máx por categoría
  [cyan]repricing-setup "Cuenta 1"[/cyan] Módulo 5 — Configurar una cuenta específica
  [cyan]repricing[/cyan]                Módulo 5 — Simular repricing (sin aplicar cambios)
  [cyan]repricing --apply[/cyan]        Módulo 5 — Aplicar cambios de precios en ML
  [cyan]stock-rentabilidad[/cyan]       Módulo 7 — Stock, velocidad de ventas y margen por producto
  [cyan]stock-rentabilidad --todos[/cyan] Módulo 7 — Mostrar todas las publicaciones (no solo alertas)
  [cyan]costos[/cyan]                   Módulo 7 — Cargar costos por producto para calcular margen real
  [cyan]comisiones[/cyan]               Ver comisiones actuales obtenidas de la API de ML
  [cyan]comisiones --refresh[/cyan]     Forzar actualización de comisiones desde ML
  [cyan]multicuenta[/cyan]              Módulo 11 — Resumen consolidado + inteligencia cruzada entre cuentas
  [cyan]multicuenta resumen[/cyan]      Módulo 11 — Solo vista consolidada de todas las cuentas
  [cyan]multicuenta cruzada[/cyan]      Módulo 11 — Solo auto-competencia y oportunidades cruzadas
  [cyan]radar[/cyan]                    Módulo 8 — Radar de nichos + calendario de ventas
  [cyan]radar "Cuenta 1" radar[/cyan]   Módulo 8 — Solo radar de nichos
  [cyan]radar "Cuenta 1" calendario[/cyan] Módulo 8 — Solo calendario de fechas comerciales
  [cyan]full-setup[/cyan]               Módulo 9 — Configurar lead times Full por producto
  [cyan]full[/cyan]                     Módulo 9 — Gestión Mercado Envíos Full (alertas, muertos, escalar)
  [cyan]reposicion-setup[/cyan]         Módulo 10 — Configurar tránsito China y depósito propio
  [cyan]reposicion[/cyan]               Módulo 10 — Plan de reposición desde China
  [cyan]todo[/cyan]                     Corre módulo 1 + 2 en todas las cuentas
"""


def main():
    if len(sys.argv) < 2:
        console.print(HELP)
        return
    cmd = sys.argv[1].lower()
    if cmd in COMMANDS:
        COMMANDS[cmd]()
    else:
        console.print(f"[red]Comando desconocido: '{cmd}'[/red]")
        console.print(HELP)


if __name__ == "__main__":
    main()
