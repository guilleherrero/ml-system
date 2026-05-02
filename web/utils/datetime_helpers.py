"""
Helpers de timezone para Argentina (America/Argentina/Buenos_Aires).

Uso recomendado:
    from web.utils.datetime_helpers import now_ar, format_ar, to_ar

    print(now_ar())                    # 2026-05-01 19:30:00-03:00
    print(format_ar(now_ar()))         # 2026-05-01 19:30
    print(to_ar(some_utc_datetime))    # convierte UTC a ART

Notas:
    - El proceso ya tiene TZ='America/Argentina/Buenos_Aires' seteado en el
      arranque (main.py / web/app.py), así que datetime.now() ya devuelve
      hora ART. Estos helpers son para casos donde se necesita explicitar
      tz-awareness o formatear de manera consistente.
    - to_ar() es útil cuando se reciben timestamps UTC desde la API de ML
      (orders, claims, snapshots) y hay que mostrarlos al usuario.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ART = ZoneInfo("America/Argentina/Buenos_Aires")


def now_ar() -> datetime:
    """Datetime actual en zona horaria Argentina (timezone-aware)."""
    return datetime.now(tz=ART)


def format_ar(dt: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Formatea un datetime en hora Argentina.

    Si el datetime es naive (sin tz), se asume que ya está en ART.
    Si tiene tz, se convierte primero a ART.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        # naive — se asume ART (consistente con TZ del proceso)
        return dt.strftime(fmt)
    return dt.astimezone(ART).strftime(fmt)


def to_ar(dt_utc: datetime) -> datetime:
    """Convierte un datetime UTC a Argentina.

    Si el input es naive, se asume UTC.
    Si tiene tz, se convierte directamente.
    """
    if dt_utc is None:
        return None
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(ART)
