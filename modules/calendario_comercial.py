"""
Calendario comercial — fechas clave de Argentina con conteo regresivo.

Extraído de modules/radar_oportunidades.py en Sprint 5.4 al eliminar el
Radar de Nichos. La parte de calendario sigue siendo usada por:
  - web/app.py → build_calendario() helper (Dashboard, /calendario, sugerencias)
  - CLI: python main.py calendario
"""

from datetime import date, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box

console = Console()


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Devuelve el n-ésimo día de la semana del mes. weekday: 0=lunes, 6=domingo."""
    d = date(year, month, 1)
    first_wd = d.weekday()
    delta = (weekday - first_wd) % 7
    target = d + timedelta(days=delta + (n - 1) * 7)
    return target


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Último día de la semana en el mes dado."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


def _build_calendario(year: int) -> list[dict]:
    """Genera las fechas comerciales clave para el año dado."""
    return [
        {
            "nombre":  "Día de los Enamorados",
            "fecha":   date(year, 2, 14),
            "icono":   "💝",
            "consejo": "Regalos, accesorios, indumentaria. Activá descuentos 10 días antes.",
        },
        {
            "nombre":  "Día de la Mujer",
            "fecha":   date(year, 3, 8),
            "icono":   "💜",
            "consejo": "Indumentaria, cuidado personal, accesorios. Publicaciones específicas.",
        },
        {
            "nombre":  "Hot Sale",
            "fecha":   _nth_weekday_of_month(year, 5, 0, 2),  # 2do lunes de mayo
            "icono":   "🔥",
            "consejo": "El evento más grande del año. Preparar stock 45 días antes. Activar descuentos.",
        },
        {
            "nombre":  "Día del Padre",
            "fecha":   _nth_weekday_of_month(year, 6, 6, 3),  # 3er domingo de junio
            "icono":   "👔",
            "consejo": "Herramientas, indumentaria, electrónica. Preparar stock 30 días antes.",
        },
        {
            "nombre":  "Día del Amigo",
            "fecha":   date(year, 7, 20),
            "icono":   "🤝",
            "consejo": "Regalos variados, juegos. Oportunidad de liquidar stock acumulado.",
        },
        {
            "nombre":  "Día del Niño",
            "fecha":   _nth_weekday_of_month(year, 8, 6, 2),  # 2do domingo de agosto
            "icono":   "🧸",
            "consejo": "Juguetes, ropa infantil, accesorios bebé. Mayor evento del 2do semestre.",
        },
        {
            "nombre":  "Día de la Madre",
            "fecha":   _nth_weekday_of_month(year, 10, 6, 3),  # 3er domingo de octubre
            "icono":   "💐",
            "consejo": "Accesorios, cuidado personal, ropa. Preparar stock y publicidad 3 semanas antes.",
        },
        {
            "nombre":  "Cyber Monday",
            "fecha":   _last_weekday_of_month(year, 11, 0),   # último lunes de noviembre
            "icono":   "💻",
            "consejo": "Segundo evento más grande. Precios agresivos. Stock asegurado.",
        },
        {
            "nombre":  "Navidad",
            "fecha":   date(year, 12, 24),
            "icono":   "🎄",
            "consejo": "Regalos en todas las categorías. Pico de ventas. Envíos Full recomendados.",
        },
    ]


def _acciones_por_dias(dias: int) -> str:
    if dias < 0:
        return "[dim]ya pasó[/dim]"
    if dias == 0:
        return "[red bold]¡HOY![/red bold]"
    if dias <= 7:
        return "[red]Revisá stock y precios AHORA[/red]"
    if dias <= 15:
        return "[yellow]Activar publicidad y revisar precios[/yellow]"
    if dias <= 30:
        return "[cyan]Preparar stock y optimizar publicaciones[/cyan]"
    if dias <= 60:
        return "[green]Planificar pedidos a proveedores[/green]"
    return "[dim]Sin acción inmediata[/dim]"


def _dias_label(dias: int) -> str:
    if dias < 0:
        return f"[dim]hace {abs(dias)}d[/dim]"
    if dias == 0:
        return "[red bold]HOY[/red bold]"
    if dias <= 7:
        return f"[red bold]{dias} días[/red bold]"
    if dias <= 15:
        return f"[red]{dias} días[/red]"
    if dias <= 30:
        return f"[yellow]{dias} días[/yellow]"
    return f"[green]{dias} días[/green]"


def run_calendario():
    today = date.today()
    year  = today.year

    console.print(Rule("[bold cyan]Calendario de ventas — Argentina[/bold cyan]"))
    console.print()

    eventos = []
    for ev in _build_calendario(year):
        dias = (ev["fecha"] - today).days
        if dias < -7:
            proximas = [e for e in _build_calendario(year + 1) if e["nombre"] == ev["nombre"]]
            if proximas:
                prox = proximas[0]
                dias = (prox["fecha"] - today).days
                eventos.append({**prox, "dias": dias})
            else:
                eventos.append({**ev, "dias": dias})
        else:
            eventos.append({**ev, "dias": dias})

    eventos.sort(key=lambda e: e["dias"])

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
    table.add_column("",          width=3)
    table.add_column("Evento",    min_width=22)
    table.add_column("Fecha",     min_width=12, justify="center")
    table.add_column("Faltan",    min_width=10, justify="right")
    table.add_column("Acción recomendada", ratio=2)

    for ev in eventos:
        dias = ev["dias"]
        table.add_row(
            ev["icono"],
            ev["nombre"],
            ev["fecha"].strftime("%d/%m/%Y"),
            _dias_label(dias),
            _acciones_por_dias(dias),
        )

    console.print(table)

    urgentes = [e for e in eventos if 0 <= e["dias"] <= 30]
    if urgentes:
        ev = urgentes[0]
        console.print(Panel(
            f"  {ev['icono']}  [bold]{ev['nombre']}[/bold] — {ev['dias']} días\n\n"
            f"  {ev['consejo']}",
            title="[bold yellow]⚡ Próximo evento importante[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))

    console.print()
