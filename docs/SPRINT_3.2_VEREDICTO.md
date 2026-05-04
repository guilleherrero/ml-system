# Sprint 3.2 — Veredicto IA sobre optimizaciones

**Estado:** completado · **Branch:** `feature/auditoria-mejoras-2026` · **Fecha:** 2026-05-04

---

## Qué resuelve

Cierra el loop **Hacer → Medir → Aprender** del sistema ML. Hasta el Sprint 3.1
sabíamos QUÉ se había aplicado y teníamos baseline T0 capturado, pero no había
forma automática de evaluar si la optimización funcionó. Este sprint:

1. Captura snapshots diarios automáticos de cada optimización (extensión 3.1).
2. Cada lunes 06:15 ART evalúa con Claude Opus las optimizaciones de ≥7 días.
3. Emite veredicto **ganadora / neutra / perdedora** con score 0-100.
4. Recomienda siguiente paso: **replicar / mantener / regenerar / revertir**.
5. Renderiza banner destacado en el Monitor de Evolución.
6. Hard cap de costo $30/mes con auto-pausa.

---

## Flujo end-to-end

```
Día 0   → Usuario aplica optimización en Optimizar IA → baseline T0 capturado
Día 1+  → Cron daily_snapshots (04:00 ART) captura snapshot ligero
…       → Acumulación de timeline en monitor_evolucion.json
Día 7   → Primer lunes >= T+7 → cron weekly_veredictos (06:15 ART)
        → Detecta opt elegible (≥7d, ≥3 snapshots, sin veredicto vigente)
        → Llama a Claude Opus con prompt accionable + deltas
        → Persiste veredicto en data/veredictos_optimizacion.json
        → Loggea costo en data/token_log.json
Visita → Usuario abre Monitor → fetch /api/veredicto/<alias>/<item_id>
        → Banner verde/amarillo/rojo con razonamiento + recomendación + CTA
```

---

## Archivos del sprint

### Creados
- `modules/veredicto_optimizacion.py` — motor principal (~700 líneas)
- `tests/test_veredicto_optimizacion.py` — 40 tests con mocks
- `docs/SPRINT_3.2_VEREDICTO.md` — este documento

### Modificados
- `web/app.py` — 4 endpoints nuevos + 2 jobs nuevos en scheduler
- `web/templates/monitor_evolucion.html` — banner del veredicto + CSS

### NO modificados (verificado)
- `modules/seo_optimizer.py` — hash MD5 `1e7272662f0761fba99bdb07f23fa1cc` igual al baseline
- `modules/baseline_capture.py` — solo se LEE el baseline que produce
- `modules/optimizador_publicaciones.py`, `modules/lanzador_productos.py`
- `core/impact_calculator.py`, `modules/alertas_estado.py` — solo se IMPORTA `fingerprint`
- `modules/top_acciones_diarias.py` — punto de inserción documentado pero NO tocado (Sprint 3.3)

---

## API pública del módulo

```python
from modules import veredicto_optimizacion as vo

# Función principal: evalúa una optimización (idempotente, safeguards adentro)
vo.evaluar_optimizacion(item_id, alias, data_dir, force=False) -> dict
# Retorna estado in {veredicto, ya_existe, data_insuficiente, no_encontrado, error}

# Lookup read-only del veredicto persistido
vo.obtener_veredicto(item_id, alias, data_dir, incluir_no_vigentes=False) -> dict | None

# Marcar como descartado (idempotente)
vo.descartar_veredicto(item_id, alias, data_dir, razon="") -> bool

# Listado para el cron
vo.listar_pendientes(data_dir, dias_minimos=7) -> list[dict]

# Historial agregado para futuro UI
vo.historial_veredictos(alias, data_dir, days=90, incluir_descartados=False) -> dict

# Cost guard
vo.costo_mes_actual_usd(data_dir) -> float
vo.supera_cap_mensual(data_dir) -> tuple[bool, float]

# Helpers
vo.fingerprint_veredicto(alias, item_id, fecha_optimizacion) -> str
vo.threshold_aplicado(snapshots_usados) -> float
```

### Constantes configurables

```python
DIAS_MINIMOS_VEREDICTO         = 7      # safeguard duro
SNAPSHOTS_MINIMOS              = 3      # safeguard duro
TTL_VEREDICTO_DIAS             = 30     # tras eso, re-evaluable
TTL_DESCARTADO_DIAS            = 30     # purga del store
THRESHOLD_DEFAULT_PCT          = 15.0   # ≥5 snapshots
THRESHOLD_DATA_RUIDOSA         = 20.0   # <5 snapshots
SNAPSHOTS_PARA_THRESHOLD_BAJO  = 5
HARD_CAP_USD_MENSUAL           = 30.0
MAX_VEREDICTOS_POR_CORRIDA     = 100
PROMPT_VERSION                 = "1.0"
MODELO_DEFAULT                 = "claude-opus-4-6"
```

---

## Endpoints HTTP

| Método | Path | Qué hace |
|---|---|---|
| GET  | `/api/veredicto/<alias>/<item_id>` | Read-only. NO llama a Claude. Devuelve persistido o estado evaluado. |
| POST | `/api/veredicto/<alias>/<item_id>/generar` | Override manual. Body `{force: bool}`. NO salta el gate de 7 días. |
| POST | `/api/veredicto/<alias>/<item_id>/descartar` | Marca descartado. Body opcional `{razon: str}`. |
| GET  | `/api/veredictos/<alias>?days=90&incluir_descartados=0` | Historial agregado. |

---

## Cron jobs nuevos

### `daily_snapshots` — diario 04:00 ART
Captura snapshot ligero de cada optimización del monitor (1-90 días) usando
`_capturar_snapshot_simple`. Anti-duplicado por día (reemplaza si ya hay uno
del mismo día). Rolling 30 snapshots por item. Si tiene republicación,
captura también la MLA original.

### `weekly_veredictos` — lunes 06:15 ART
Llama a `vo.listar_pendientes()`, hace re-check de hard cap entre llamadas,
procesa hasta 100 opts por corrida. Cada veredicto loggea `~$0.04` en
`data/token_log.json`.

### Orden del scheduler los lunes
```
04:00  daily_snapshots         (snapshots T+N de todas las opts)
06:00  top_acciones_daily      (Top 3 acciones del día)
06:15  weekly_veredictos       (Veredicto IA solo lunes)
07:00  daily_update            (refresh principal de data)
08:00+ resto del día (repricing horario, preguntas 15min, buybox 6h)
```

---

## Estructura del JSON `data/veredictos_optimizacion.json`

```json
{
  "items": [
    {
      "fingerprint":         "a3f9b2c4d1e8f7a6",
      "alias":               "Novara",
      "item_id":             "MLA1234567",
      "titulo_producto":     "Lampara LED Solar 100W ...",
      "fecha_optimizacion":  "2026-04-25",
      "fecha_veredicto":     "2026-05-02 06:15:23",
      "dias_transcurridos":  7,
      "veredicto":           "ganadora",
      "score_exito":         82,
      "razonamiento":        "Visitas 7d +40%, ventas 30d +50%, posición #12→#7...",
      "recomendacion":       "replicar",
      "razon_recomendacion": "El patrón es claro y robusto en las tres métricas...",
      "metricas_clave":      ["ventas_30d", "conv_pct", "visitas_7d"],
      "alertas":             ["data parcial: solo 4 snapshots (si aplica)"],
      "deltas": {
        "visitas_7d":   {"t0": 100, "actual": 140, "delta_abs": 40, "delta_pct": 40.0},
        "ventas_30d":   {"t0": 8,   "actual": 12,  "delta_abs": 4,  "delta_pct": 50.0},
        "ventas_total": {"t0": 480, "actual": 520, "delta_abs": 40, "delta_pct": 8.3},
        "conv_pct":     {"t0": 2.5, "actual": 3.5, "delta_abs": 1.0,"delta_pct": 40.0},
        "posicion":     {"t0": 12,  "actual": 7,   "delta_abs": 5,  "delta_pct": null}
      },
      "snapshots_usados":    7,
      "estado":              "vigente",
      "_debug": {
        "timestamp":             "2026-05-02T06:15:23",
        "tokens_in":             1029,
        "tokens_out":            346,
        "cost_usd":              0.041385,
        "duracion_ms":           9290,
        "threshold_aplicado":    15.0,
        "snapshots_usados":      7,
        "snapshots_disponibles": 7,
        "prompt_version":        "1.0",
        "modelo":                "claude-opus-4-6"
      }
    }
  ]
}
```

Estados: `vigente`, `descartado`, `obsoleto`.

---

## Decisiones de diseño y matices

### Threshold dinámico
Los primeros días tras una optimización la data es ruidosa (un solo cliente
casual puede mover ventas_7d 50%). Con `<5 snapshots` aplicamos threshold
del **20%** para considerar un cambio significativo. Con `≥5 snapshots`
bajamos a **15%**. El threshold aplicado se persiste en `_debug.threshold_aplicado`
para calibración futura.

### Idempotencia
Un fingerprint = `sha1("veredicto:alias:item_id:fecha_opt")[:16]`. Una
optimización tiene UN veredicto vigente. El cron skip-ea si ya existe vigente
<30 días. A los 30 días el sistema lo marca obsoleto y permite re-evaluar.

### Force=True
Override manual desde la UI. Salta el check "ya_existe" pero **NO** el de
7 días (regla dura, sin disclaimer ni opción de forzar).

### Hard cap $30/mes con auto-pausa
Si el costo acumulado del mes en token_log.json filtrado por
`funcion='Veredicto IA'` alcanza el cap, el cron `weekly_veredictos` se
auto-pausa vía `JobManager.pause_job`. El usuario lo ve pausado en `/settings`
y decide si reanudar manualmente. Re-check entre llamadas dentro de la misma
corrida para no pasarse.

### Manejo de errores de Claude
Si la API falla (timeout, JSON inválido, score fuera de rango, etc.), el
módulo NO persiste un veredicto roto. `evaluar_optimizacion` captura la
excepción y devuelve `{estado: "error"}`. El cron sigue procesando los
demás items.

### Parser tolerante
La respuesta de Claude puede venir en JSON pelado, envuelta en ```` ```json ```` ,
con texto antes/después. El parser extrae el primer objeto balanceado y
valida los campos enum (veredicto, recomendación) y el rango del score.

### Snapshot ligero vs baseline completo
El cron diario reusa `_capturar_snapshot_simple` (5 campos planos, ~3 calls
ML). NO captura el baseline completo (14 métricas, ~14 calls). Para el
veredicto alcanza con visitas, ventas, posición y conversión. El baseline
completo solo se captura al aplicar la optimización (ya implementado en
Sprint 3.1).

---

## CTAs por recomendación (estado Sprint 3.2)

| Recomendación | Botón | Estado | Acción |
|---|---|---|---|
| `regenerar` | 🔄 Regenerar con aprendizajes | **funcional** | Redirige a `/optimizar/<alias>?item=<id>` |
| `replicar`  | 🚀 Replicar patrón (Sprint 3.3) | disabled | tooltip "Disponible en Sprint 3.3" |
| `revertir`  | ↩ Revertir cambios (Sprint 3.3) | disabled | tooltip "Disponible en Sprint 3.3" |
| `mantener`  | (sin CTA) | informativo | — |
| Cualquiera  | 🗑 Descartar veredicto | **funcional** | POST `/descartar` con razón opcional |

---

## Punto de inserción para Sprint 3.3

Cuando se implemente la replicación de patrones ganadores:

1. En `modules/top_acciones_diarias.py`, agregar a `_DETECTORS`:
   ```python
   _DETECTORS = [
       ...,
       ("veredicto_ia", _candidates_veredicto_ia),  # NUEVO
   ]
   ```

2. `_candidates_veredicto_ia` debe:
   - Importar `from modules.veredicto_optimizacion import historial_veredictos`
   - Filtrar `veredicto == "ganadora"` y `recomendacion == "replicar"`
   - Construir `Oportunidad` por cada uno con descripción tipo
     "Replicar patrón ganador de 'X' a publicaciones similares"

3. Habilitar el botón "Replicar patrón" del banner en `monitor_evolucion.html`
   (cambiar `disabled` → `data-action="cta-replicar"` con handler real).

---

## Operación

### Ver costo acumulado del mes
```python
from modules import veredicto_optimizacion as vo
vo.costo_mes_actual_usd("data")   # → 7.8
vo.supera_cap_mensual("data")     # → (False, 7.8)
```

O en el filesystem: `grep '"Veredicto IA"' data/token_log.json | jq '.usd'`.

### Pausar / reanudar el cron manualmente
- UI: `/settings` → Scheduler → fila `weekly_veredictos` → botón pausar/reanudar.
- API: `POST /api/scheduler-toggle/weekly_veredictos {action: 'pause'|'resume'}`.

### Forzar veredicto manualmente (sin esperar al lunes)
```bash
curl -X POST http://localhost:8000/api/veredicto/Novara/MLA1234567/generar
# o con force=true para regenerar uno existente
curl -X POST http://localhost:8000/api/veredicto/Novara/MLA1234567/generar \
     -H 'Content-Type: application/json' -d '{"force": true}'
```

### Disparar el cron weekly manualmente (debug)
```python
# En consola del server
from web import app
app._job_veredictos_weekly()
```

---

## Métricas del sprint

- **Commits:** 8 (todos con regresión verde)
- **Tests:** 40/40 pasando (19 → 40)
- **Costo desarrollo:** $0.041 USD (1 llamada real a Claude para validar prompt)
- **Costo operativo estimado:** $5-15/mes en peor caso (50-100 veredictos/semana × $0.04)
- **Hard cap:** $30/mes
- **Hash MD5 `seo_optimizer.py`:** `1e7272662f0761fba99bdb07f23fa1cc` (sin cambios)
