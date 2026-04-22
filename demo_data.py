"""
Script de demo — genera snapshots de prueba para ver la UI completa.
Crea datos realistas en data/ para reputación y stock.
Corré una sola vez: python3 demo_data.py
"""

import json
import os
from datetime import datetime, timedelta
import random

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
os.makedirs(DATA_DIR, exist_ok=True)

ALIAS = "Cuenta_1"

# ── Reputación: 8 snapshots de los últimos 2 meses ───────────────────────────

rep_snapshots = []
base_date = datetime.now() - timedelta(days=56)
reclamos  = 0.8
demoras   = 7.2
cancels   = 1.1

for i in range(8):
    fecha = (base_date + timedelta(days=i * 7)).strftime("%Y-%m-%d %H:%M")
    # Simular leve mejora con alguna variación
    reclamos  = round(max(0.3, reclamos + random.uniform(-0.15, 0.2)), 2)
    demoras   = round(max(3.0, demoras  + random.uniform(-0.8, 1.0)), 2)
    cancels   = round(max(0.5, cancels  + random.uniform(-0.1, 0.15)), 2)
    rep_snapshots.append({
        "fecha":              fecha,
        "nivel":              "4_rojo",
        "power_seller":       "silver",
        "ventas_total":       120 + i * 8,
        "reclamos_pct":       reclamos,
        "demoras_pct":        demoras,
        "cancelaciones_pct":  cancels,
    })

with open(os.path.join(DATA_DIR, f"reputacion_{ALIAS}.json"), "w", encoding="utf-8") as f:
    json.dump(rep_snapshots, f, indent=2, ensure_ascii=False)
print(f"✓ reputacion_{ALIAS}.json — {len(rep_snapshots)} snapshots")

# ── Stock: snapshot con variedad de alertas ───────────────────────────────────

productos = [
    ("MLA001", "Faja postparto premium talle M",        8900,  0,   0.0,  None, "SIN_STOCK",    None),
    ("MLA002", "Faja lumbar deportiva talle L",         6500,  4,   0.6,   6.7, "CRITICO",      None),
    ("MLA003", "Faja embarazo talle S",                 7200,  11,  0.4,  27.5, "ADVERTENCIA",  None),
    ("MLA004", "Faja postparto talle L",                8900,  38,  1.2,  31.7, None,           None),
    ("MLA005", "Faja lumbar clásica talle M",           5800,  62,  0.9,  68.9, None,           None),
    ("MLA006", "Cinturilla reductora neoprene M",       4200,  25,  0.3,  83.3, None,           None),
    ("MLA007", "Faja postparto talle XL",               9200,   8,  0.0,  None, None,           "BAJO"),
    ("MLA008", "Corsé ortopédico lumbar talle M",      12500,  15,  0.2,  75.0, None,           "NEGATIVO"),
    ("MLA009", "Faja embarazo talle M",                 7200,  55,  1.8,  30.6, None,           None),
    ("MLA010", "Soporte rodilla deportivo talle L",     3900,   3,  0.1,  30.0, None,           "BAJO"),
]

items_stock = []
for item_id, titulo, precio, stock, vel, dias, al_stock, al_margen in productos:
    neto = round(precio * 0.84, 0)
    items_stock.append({
        "id":            item_id,
        "titulo":        titulo,
        "precio":        precio,
        "stock":         stock,
        "costo":         None,
        "velocidad":     vel,
        "dias_stock":    dias,
        "neto":          neto,
        "ganancia":      None,
        "margen_pct":    None,
        "comision":      round(precio * 0.13, 2),
        "envio_est":     700,
        "alerta_stock":  al_stock,
        "alerta_margen": al_margen,
    })

stock_snap = {
    "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
    "items": items_stock,
}
with open(os.path.join(DATA_DIR, f"stock_{ALIAS}.json"), "w", encoding="utf-8") as f:
    json.dump(stock_snap, f, indent=2, ensure_ascii=False)
print(f"✓ stock_{ALIAS}.json — {len(items_stock)} publicaciones")

# ── Costos demo (para ver margen real en stock) ───────────────────────────────

costos = {
    "MLA001": {"alias": "Cuenta 1", "titulo": "Faja postparto premium talle M",   "costo": 4200, "updated": "2026-04-01"},
    "MLA002": {"alias": "Cuenta 1", "titulo": "Faja lumbar deportiva talle L",    "costo": 3100, "updated": "2026-04-01"},
    "MLA003": {"alias": "Cuenta 1", "titulo": "Faja embarazo talle S",            "costo": 3400, "updated": "2026-04-01"},
    "MLA004": {"alias": "Cuenta 1", "titulo": "Faja postparto talle L",           "costo": 4200, "updated": "2026-04-01"},
    "MLA005": {"alias": "Cuenta 1", "titulo": "Faja lumbar clásica talle M",      "costo": 2600, "updated": "2026-04-01"},
    "MLA006": {"alias": "Cuenta 1", "titulo": "Cinturilla reductora neoprene M",  "costo": 2100, "updated": "2026-04-01"},
    "MLA007": {"alias": "Cuenta 1", "titulo": "Faja postparto talle XL",          "costo": 4800, "updated": "2026-04-01"},
    "MLA008": {"alias": "Cuenta 1", "titulo": "Corsé ortopédico lumbar talle M",  "costo": 11200,"updated": "2026-04-01"},
    "MLA009": {"alias": "Cuenta 1", "titulo": "Faja embarazo talle M",            "costo": 3400, "updated": "2026-04-01"},
    "MLA010": {"alias": "Cuenta 1", "titulo": "Soporte rodilla deportivo talle L","costo": 1900, "updated": "2026-04-01"},
}
os.makedirs(CONFIG_DIR, exist_ok=True)
with open(os.path.join(CONFIG_DIR, "costos.json"), "w", encoding="utf-8") as f:
    json.dump(costos, f, indent=2, ensure_ascii=False)
print(f"✓ config/costos.json — {len(costos)} costos cargados")

print("\n✅ Datos de demo listos. Ahora podés correr:")
print("   python3 main.py dashboard")
print("   python3 main.py historial")
print("   python3 main.py stock-rentabilidad --todos")
