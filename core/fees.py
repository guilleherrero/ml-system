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


# ── Comisión por publicación (cuotas incluidas) ──────────────────────────────
# En MLA las cuotas sin interés se activan con un tag del ítem, solo en Premium
# (gold_pro): sin tag = 6 cuotas; 3x/9x/12x_campaign = 3, 9 o 12 cuotas. Cada
# nivel tiene otro costo, así que la tasa por tipo de publicación (calculada a
# $10.000, sin categoría ni cuotas) no alcanza para una publicación puntual.
CUOTA_TAGS = {"3x_campaign": 3, "9x_campaign": 9, "12x_campaign": 12}


def cuotas_de_item(listing_type: str, tags: list | None) -> int:
    """Cuotas sin interés que ofrece la publicación (1 = sin cuotas)."""
    if listing_type != "gold_pro":
        return 1
    for t in tags or []:
        if t in CUOTA_TAGS:
            return CUOTA_TAGS[t]
    return 6


def comision_item(client, precio: float, listing_type: str, category_id: str = "",
                  tags: list | None = None) -> float | None:
    """Tasa que ML cobra hoy a ESTA publicación: su precio, categoría y cuotas."""
    if not precio or precio <= 0:
        return None
    params = {"price": precio, "listing_type_id": listing_type}
    if category_id:
        params["category_id"] = category_id
    tag = next((t for t in tags or [] if t in CUOTA_TAGS), None)
    if tag:
        params["tags"] = tag
    try:
        raw = client._get("/sites/MLA/listing_prices", params=params)
    except Exception:
        return None
    data = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
    fee = data.get("sale_fee_amount")
    return round(float(fee) / precio, 4) if fee else None


def costo_envio_item(client, item: dict) -> float | None:
    """Lo que paga el vendedor por cada envío gratis de la publicación (0 si no ofrece).

    ML lo informa por ítem (peso facturable, logística, reputación). Comparado con
    envíos reales queda igual o un poco arriba: nunca muestra un margen mejor.
    """
    sh = item.get("shipping") or {}
    if not sh.get("free_shipping"):
        return 0.0
    uid = item.get("seller_id") or getattr(getattr(client, "account", None), "user_id", None)
    try:
        data = client._get(f"/users/{uid}/shipping_options/free", params={"item_id": item["id"]})
        return float(((data.get("coverage") or {}).get("all_country") or {}).get("list_cost") or 0)
    except Exception:
        return None
