"""
Gestión centralizada de comisiones ML.

Las tasas se obtienen directamente de la API de MercadoLibre y se guardan en
config/fees.json. Si el archivo tiene más de 7 días se refresca automáticamente.
Los valores hardcodeados solo se usan si la API no está disponible.
"""

import json
import os
from datetime import datetime, timedelta

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
FEES_PATH  = os.path.join(CONFIG_DIR, "fees.json")

LISTING_TYPES = ["gold_pro", "gold_special", "gold_premium", "gold", "silver", "bronze"]
REFRESH_DAYS  = 7

# Fallback hardcodeado — solo si la API falla
_FALLBACK = {
    "gold_pro":     0.34,
    "gold_special": 0.31,
    "gold_premium": 0.31,
    "gold":         0.31,
    "silver":       0.31,
    "bronze":       0.31,
    "_default":     0.31,
}


def _load() -> dict:
    if not os.path.exists(FEES_PATH):
        return {}
    with open(FEES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save(fees: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(FEES_PATH, "w", encoding="utf-8") as f:
        json.dump(fees, f, indent=2, ensure_ascii=False)


def _is_stale(fees: dict) -> bool:
    updated_at = fees.get("_updated_at")
    if not updated_at:
        return True
    age = datetime.now() - datetime.fromisoformat(updated_at)
    return age > timedelta(days=REFRESH_DAYS)


def fetch_from_api(client) -> dict:
    """
    Consulta /sites/MLA/listing_prices para cada tipo de publicación
    y guarda los resultados en config/fees.json.
    Devuelve el dict con las tasas actualizadas.
    """
    fees = {}
    ref_price = 10000  # precio de referencia para calcular la tasa porcentual
    for lt in LISTING_TYPES:
        rate = client.get_listing_fee_rate(lt, ref_price)
        if rate is not None:
            fees[lt] = rate
    fees["_updated_at"] = datetime.now().isoformat()
    fees["_default"] = fees.get("gold_special", _FALLBACK["_default"])
    _save(fees)
    return fees


def get_fee_rates(client=None, force_refresh: bool = False) -> dict:
    """
    Retorna el dict completo de tasas {listing_type: rate}.
    - Si force_refresh=True y client disponible: consulta la API ahora.
    - Si el archivo tiene más de 7 días y client disponible: auto-refresca.
    - Si no hay datos ni client: devuelve valores de fallback.
    """
    fees = _load()

    if force_refresh and client:
        fees = fetch_from_api(client)
    elif client and _is_stale(fees):
        fees = fetch_from_api(client)

    if not fees or not any(k for k in fees if not k.startswith("_")):
        return dict(_FALLBACK)

    return fees


def get_rate(listing_type: str, fees: dict | None = None) -> float:
    """
    Devuelve la tasa efectiva para un tipo de publicación.
    Si no se pasa un dict de fees, lee del archivo (sin refrescar).
    """
    if fees is None:
        fees = _load() or _FALLBACK
    return fees.get(listing_type, fees.get("_default", _FALLBACK["_default"]))
