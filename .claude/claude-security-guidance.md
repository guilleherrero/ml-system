# Reglas de seguridad — ml-system

Sistema en producción (Render) con acceso de escritura a la cuenta real de MercadoLibre y a Mercado Pago. Estas reglas aplican a cualquier cambio de código, script o comando. Las marcadas con ⚠️ hoy **no se cumplen** en el código: no copiar ese patrón y corregirlo cuando se toque esa parte (relevamiento del 2026-10-03).

## 1. Secretos y tokens

- **Nunca loguear, imprimir, devolver en una respuesta ni mostrar en pantalla** tokens de MercadoLibre (`access_token`, `refresh_token`, `client_secret`), Mercado Pago (`MP_ACCESS_TOKEN`, `mp_access_token`), Telegram (`TELEGRAM_BOT_TOKEN`, secreto del webhook), `ANTHROPIC_API_KEY`, `MCP_API_TOKEN`, `CEREBRO_BOOKMARKLET_TOKEN`, `FLASK_SECRET` ni `DATABASE_URL`. Si hace falta identificar uno en un log, alcanza con los últimos 4 caracteres.
- Tampoco ponerlos en mensajes de commit, en descripciones de PR, en issues, en el chat con Claude ni en archivos de `docs/`.
- No pasar tokens en la URL (`?access_token=`): siempre en el header `Authorization: Bearer`.
- Los secretos se leen de variables de entorno o del store (`core/db_storage.py`), nunca hardcodeados en el código.
- Las respuestas de error al cliente no deben incluir `str(e)` crudo cuando la excepción puede contener URLs, headers o el payload de una API externa. Loguear el detalle del lado del servidor y devolver un mensaje genérico. ⚠️ Hoy lo hacen el handler de `/api/pricing/*` y `/api/mp/webhook`.

## 2. Qué nunca se sube al repo

- `.env` y cualquier `.env.*` con valores reales (solo `.env.example`, con placeholders).
- `.claude/settings.local.json` y `**/.claude/settings.local.json`: los permisos de Claude Code pueden guardar comandos con tokens adentro (ya pasó con dos `curl` a la API de ML).
- `config/accounts.json` (refresh tokens de ML), `config/costos.json`, `config/repricing.json`, `config/scheduler_overrides.json`, `data/` y `backups/`.
- Antes de cada commit: `git status` y `git diff --cached`, y agregar archivos por nombre, nunca con `git add -A` o `git add .`. Si un secreto llega a subirse, se rota (se genera uno nuevo) además de borrarlo: queda en el historial de git.
- No agregar tokens en `permissions.allow` de los settings de Claude Code: usar patrones (`Bash(curl:*)`) o variables de entorno.

## 3. Webhooks y datos externos

Todo lo que llega de afuera (webhooks, bookmarklet, respuestas de APIs de terceros) es no confiable hasta validarlo.

- **Verificar el origen**: secreto en el path o header comparado con `secrets.compare_digest`, nunca con `==` o `!=`. ⚠️ El webhook de Telegram compara el secreto con `!=`.
- **Mercado Pago (`/api/mp/webhook`)**: no confiar en el payload. El patrón correcto, que ya se usa, es tomar solo el `payment_id` y consultar el pago a la API de MP con nuestro token. Además:
  - ⚠️ Antes de marcar una orden como pagada, verificar que `transaction_amount` y `currency_id` del pago coincidan con el total de la orden. Hoy solo se mira el `external_reference`.
  - ⚠️ Validar la firma `x-signature` de MP cuando esté configurado el secreto de webhooks.
  - Ser idempotente: el mismo aviso puede llegar varias veces.
- **Telegram**: el bot solo debe obedecer a los `chat_id` autorizados. ⚠️ Hoy cualquier persona que encuentre el bot y le mande `/start` queda registrada y recibe avisos con botones: puede ver datos del negocio (`/bandeja`, `/estado`, `/resumen`), aprobar o rechazar cambios de precio y responder preguntas de compradores en ML. El alta de un chat nuevo tiene que requerir algo que solo tenga el dueño (código de un solo uso generado desde el panel, o lista fija en variable de entorno).
- Validar tipos, rangos y largo de cada campo antes de usarlo: `float()`/`int()` dentro de `try`, IDs con regex (`MLA\d+`), textos con largo máximo. Nunca pasar datos externos a `eval`, `exec`, `subprocess`, SQL armado con strings, rutas de archivo ni plantillas sin escapar.
- Ante datos inválidos, responder 4xx sin efectos secundarios. A Telegram responder 200 aunque se ignore el update, para que no reintente en loop.

## 4. Autenticación de endpoints

- El `before_request` global (`require_login` en `web/app.py`) protege todo por defecto. Cualquier excepción nueva (`_AUTH_EXEMPT` o prefijos públicos en `require_login`) tiene que estar justificada en un comentario y tener su propia validación adentro del endpoint.
- No sumar rutas públicas que lean o escriban datos de una cuenta. ⚠️ Hoy son públicas `/api/pending-competidores` (devuelve la cola de competidores de cualquier alias) y `/api/list-aliases` (marcada como "Temporal"). Pasarlas detrás de login o del token MCP.
- ⚠️ `/api/capturar-competidor` acepta pedidos sin token si `CEREBRO_BOOKMARKLET_TOKEN` no está configurado. En producción tiene que estar configurado siempre.
- El bypass por `MCP_API_TOKEN` es acceso total de administrador: el token tiene que ser largo y aleatorio, vivir solo en las variables de entorno de Render y en el `.env` del MCP, y rotarse si se expone.
- `FLASK_SECRET` tiene que estar configurada en producción; la clave de desarrollo por defecto solo sirve en local.
- `needs_setup()` deja entrar sin login mientras no haya ningún admin creado: nunca desplegar una base vacía expuesta a internet.
- Los endpoints `/api/debug-*`, `/api/full-debug/*`, `/api/test-claude` y similares tienen que ser solo para admin (hoy alcanza con estar logueado) y no devolver tokens ni headers. No crear endpoints de diagnóstico nuevos sin esa protección.
- Los usuarios no admin solo acceden a sus alias: todo endpoint nuevo que reciba `alias` tiene que respetar `user_can_access`.

## 5. Escrituras en MercadoLibre y Mercado Pago

- Toda acción que modifica la cuenta real (precio, cuotas, stock, publicar, pausar, responder preguntas, crear publicaciones) exige `confirmado: true` explícito del usuario. Nunca dispararla desde un job automático sin aprobación.
- Al probar, usar solo llamadas de lectura contra la cuenta real (alias `Novara` en producción, `Cuenta 1` en local). Ninguna escritura sin que el dueño lo pida para un producto concreto.
- Importar `web/app.py` localmente arranca el scheduler, que lee la cuenta real. Para tests usar las funciones puras (`modules/precio_motor.py`) o la app de prueba de `tests/`.

## 6. Dependencias y despliegue

- Todo push a `main` se publica en producción. Fijar versiones con tope en `requirements.txt` (el 2026-09-30 SQLAlchemy 2.1 rompió la conexión a la base por no tener tope).
- Después de cada push, verificar un endpoint que use la base (`/api/pricing/config?alias=Novara` con el token MCP).
- No agregar dependencias de paquetes desconocidos o con poco uso sin revisarlos.

## 7. Datos personales

- Los mensajes y órdenes de ML tienen nombre, email, teléfono y dirección de clientes: no loguearlos completos ni copiarlos a archivos fuera de la base, y no mostrarlos en endpoints públicos.
- No enviar datos de clientes a servicios externos (IA incluida) salvo lo mínimo para la tarea.
