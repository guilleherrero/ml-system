# CEREBRO — Handoff para la sesion que continua

Escrito por la sesion de Claude en la nube (claude.ai). Sirve para que la sesion
de Cowork en la Mac retome sin repetir trabajo ni pisar lo hecho.
Fecha: 2026-09-07.

## 1. Estado real de `main`

Commit `7c8113a`. **Ojo**: el mensaje dice "docs: agrega CEREBRO.md" pero el
commit arrastro tambien todo el codigo del Sprint A que estaba sin commitear en
la carpeta local. Contenido real:

| Archivo | Lineas | Que es |
|---|---|---|
| `docs/CEREBRO.md` | +510 | Diseno completo acordado con el usuario (bloques 1-4, 9, 10, 11 + checklist de correcciones). **Leerlo antes de tocar codigo.** |
| `modules/cerebro.py` | +1195 | Nucleo de la capa de aprendizaje (memoria, evaluacion, aprendizajes, competidores) |
| `modules/cerebro_snapshot.py` | +360 | Captura diaria por item (helpers `_items_activos`, `_detalle_items`) |
| `web/app.py` | +460/-104 | 12 endpoints `/api/cerebro/*`, job `cerebro_evaluar`, `_job_buybox_check` reescrito con `price_to_win` |
| `web/templates/optimizaciones.html` | +34/-… | UI de captura de competidor |
| `scripts/` | +40 | Mecanismo de auto-push (ver seccion 3) |

Verificado: sintaxis Python OK en los tres archivos; `seo_optimizer.py` NO fue
modificado por este commit.

**Pendiente de validar en produccion**: Render deploya `main` solo. Nadie
confirmo todavia que el sitio levanto bien despues de este deploy. Es lo primero
que hay que chequear (checklist de `meli-validar`), sobre todo:
- que la app arranca (import de `modules.cerebro` en `web/app.py`),
- `/optimizaciones` con la captura de competidor,
- que `_job_buybox_check` no rompa: ahora recorre TODAS las publicaciones de
  catalogo (antes tope 50) y llama `price_to_win` por item -> verificar que el
  token tenga permiso y que no dispare rate limit (429).

## 2. Que quedo implementado y que falta (Sprint A)

Implementado (segun lectura del codigo, sin correr en produccion):
- Registro de acciones con ciclo `pendiente -> aprobada/rechazada -> aplicada ->
  evaluada_7d/14d` (`registrar_accion`, `aprobar_accion`, `marcar_aplicada`).
- Evaluacion estadistica a 7/14 dias con control por hermanas, ruido historico y
  deteccion de contaminacion (`_hubo_otra_accion`) — bloque 4.1 del diseno.
- Consolidacion de aprendizajes con confianza baja/media/alta.
- Competidores: puntaje de candidato, huella, clasificacion directo/sustituto/
  ruido, asociacion a publicacion propia, estado de stock inferido, deteccion de
  liquidacion. Persistencia en `data/cerebro_<Alias>/competidores.json`
  (corrige las fallas 1 y 2 del checklist: antes era memoria del proceso).
- `price_to_win` en buy box (corrige fallas 5 y 6).
- Cron `cerebro_evaluar` registrado.

Falta / verificar:
- Enganche automatico del registro de acciones en los flujos que YA ejecutan
  cambios: "Aplicar TODO", repricing, respuestas a preguntas, pausas por
  duplicados, lanzador. Sin eso la memoria queda vacia.
- Snapshot enriquecido con **visitas organicas = visitas totales - clics de Ads**
  (bloque 4.1). Confirmar si `cerebro_snapshot.py` ya separa Ads; si no, es lo
  mas importante que falta, porque sin eso el sistema aprende mentiras.
- UI: seccion "Cerebro" (bandeja unica + pantalla de competidores del bloque
  2.5). Hoy solo hay endpoints.

## 3. Como se sube a produccion (importante)

El usuario NO quiere hacer pull/commit/push. No hay conector de GitHub
disponible en la sesion de la nube, asi que se instalo un vigilante local:

- `scripts/claude_autopush.sh` corre en loop cada 60s (lanzado desde Terminal con
  `nohup`; el LaunchAgent no sirve porque macOS bloquea el acceso a `~/Desktop`
  a procesos de fondo — falla con exit 126).
- Actua **solo** si existe el archivo `.claude_push` en la raiz del repo, cuyo
  contenido es el mensaje del commit. Hace `git add -A`, commit, `pull --rebase`
  y `push origin main`. Log en `scripts/autopush.log` (gitignored).
- **Riesgo conocido**: `git add -A` sube TODO lo que haya en la carpeta. Si dos
  sesiones trabajan a la vez, una puede subir el trabajo a medio hacer de la
  otra. Ya paso una vez (este commit).
- Si reinician la Mac, el loop muere. Se revive pegando en Terminal:
  `nohup bash -c 'while true; do bash ~/Desktop/claude/ml_system/scripts/claude_autopush.sh; sleep 60; done' >/dev/null 2>&1 &`
- Si esta sesion de Cowork puede pushear por si misma, mejor: que use su propio
  push y deje el marcador sin usar.

## 4. Correccion pendiente en la skill de reglas

`meli-reglas` documenta el hash MD5 de `modules/seo_optimizer.py` como
`74783469aeae1d4c12bec279d034dff3`. El archivo real hoy es
`0389b93a8c4ff11c8eaa97327a6f54c1` y **no** fue modificado por este trabajo (viene
de un hotfix anterior). Hay que actualizar el hash en la skill, si no la
verificacion de la Regla #1 da falsa alarma en cada push.

## 5. Decisiones del usuario ya cerradas (no volver a preguntar)

- Precio: modo **propone -> el usuario aprueba**. Se promueve a automatico por
  tipo de accion recien con >=10 evaluaciones y >=70% de acierto.
- Alertas: Telegram **y** ml-system, con **una sola bandeja** (aprobar en uno =
  aprobado en el otro). Token de bot todavia no creado.
- Capacidad operativa/reputacion como freno al volumen: **excluida** a pedido del
  usuario (el objetivo es crecer). Queda solo la alerta de reputacion existente.
- Storage JSON, sin Postgres relacional (Regla #5).
- Orden de sprints: A memoria+competidores, B bandeja+Telegram+UI, C precio con
  postura + loop cerrado + promos ex ante, D disparadores, titulos, promos ex
  post, semaforo de portafolio.
