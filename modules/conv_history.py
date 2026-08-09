"""Historial de métricas de conversión por item para detectar tendencias."""
import json
import os
from datetime import date

MAX_SNAPSHOTS = 16  # ~4 meses con frecuencia semanal


def registrar_snapshot(alias: str, items: list, data_dir: str) -> None:
    """Guarda un snapshot diario de conversión/ventas/visitas por item."""
    safe = alias.replace(" ", "_").replace("/", "-")
    path = os.path.join(data_dir, f"conv_history_{safe}.json")

    try:
        with open(path, encoding="utf-8") as f:
            historial = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        historial = {}

    hoy = date.today().isoformat()

    for it in items:
        iid = it.get("id") or it.get("item_id")
        if not iid:
            continue

        entry = {
            "fecha":       hoy,
            "conv_pct":    float(it.get("conversion_pct") or 0),
            "ventas_30d":  int(it.get("ventas_30d") or 0),
            "visitas_30d": int(it.get("visitas_30d") or 0),
            "precio":      float(it.get("precio") or 0),
        }

        snaps = historial.get(iid, [])

        # Si ya hay una entrada de hoy, actualizarla con datos más frescos
        if snaps and snaps[-1].get("fecha") == hoy:
            snaps[-1] = entry
        else:
            snaps.append(entry)

        historial[iid] = snaps[-MAX_SNAPSHOTS:]

    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(historial, f, ensure_ascii=False)


def cargar_tendencias(alias: str, data_dir: str, item_ids: list = None) -> dict:
    """Calcula tendencias de conversión por item comparando snapshots.

    Devuelve dict {item_id: {...tendencia...}} con campos:
      tendencia:      'bajando' | 'subiendo' | 'estable' | 'sin_datos'
      delta_conv_pp:  variación en puntos porcentuales (negativo = bajó)
      conv_inicial:   conversión al inicio del período
      conv_final:     conversión actual
      fecha_inicial:  fecha del snapshot de referencia
      fecha_final:    fecha del snapshot más reciente
      semanas:        semanas transcurridas entre ambos snapshots
      precio_cambio:  bool — el precio varió más del 1% en el período
      n_snapshots:    cantidad de snapshots disponibles para este item
    """
    safe = alias.replace(" ", "_").replace("/", "-")
    path = os.path.join(data_dir, f"conv_history_{safe}.json")

    try:
        with open(path, encoding="utf-8") as f:
            historial = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    target_ids = set(item_ids) if item_ids else set(historial.keys())
    tendencias = {}

    for iid in target_ids:
        snaps = historial.get(iid, [])
        if len(snaps) < 2:
            continue

        reciente = snaps[-1]
        # Comparar contra ~4 semanas atrás (índice -5 con frecuencia semanal)
        idx_ref = max(0, len(snaps) - 5)
        pasado = snaps[idx_ref]

        conv_ini = pasado.get("conv_pct", 0)
        conv_fin = reciente.get("conv_pct", 0)
        delta_pp = round(conv_fin - conv_ini, 2)

        try:
            d_ini = date.fromisoformat(pasado["fecha"])
            d_fin = date.fromisoformat(reciente["fecha"])
            semanas = max(1, (d_fin - d_ini).days // 7)
        except Exception:
            semanas = len(snaps) - 1

        # Umbral mínimo de significancia: 0.3 pp de cambio
        if abs(delta_pp) < 0.3:
            tendencia = "estable"
        elif delta_pp < 0:
            tendencia = "bajando"
        else:
            tendencia = "subiendo"

        precio_ini = pasado.get("precio", 0)
        precio_fin = reciente.get("precio", 0)
        precio_cambio = bool(precio_ini and abs(precio_fin - precio_ini) / precio_ini > 0.01)

        tendencias[iid] = {
            "tendencia":     tendencia,
            "delta_conv_pp": delta_pp,
            "conv_inicial":  round(conv_ini, 2),
            "conv_final":    round(conv_fin, 2),
            "fecha_inicial": pasado.get("fecha", ""),
            "fecha_final":   reciente.get("fecha", ""),
            "semanas":       semanas,
            "precio_cambio": precio_cambio,
            "n_snapshots":   len(snaps),
        }

    return tendencias
