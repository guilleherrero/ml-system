# Biobella — Storefront + Integración MercadoLibre + MercadoPago

Storefront e-commerce que vive como extensión del Flask app `ml_system`. Sincroniza
el catálogo automáticamente desde una cuenta MercadoLibre y procesa pagos vía
MercadoPago.

---

## Arquitectura rápida

```
ml_system/
├─ core/                       (existente — ML OAuth + multi-cuenta)
├─ modules/
│  ├─ tienda_sync.py           NEW — sync catálogo desde ML "novara" cada 6h
│  └─ checkout_mp.py           NEW — crear preference MP + procesar webhook
└─ web/
   ├─ app.py                   MODIFIED — rutas /tienda/*, /admin/{productos,resenas,ordenes,integraciones}
   ├─ db.py                    NEW — SQLAlchemy engine (Postgres en Render, SQLite local)
   ├─ models_tienda.py         NEW — Product, ProductLock, SyncLog, Review, Order, OrderItem, AppSetting
   ├─ seed_demo.py             NEW — 6 productos demo para preview local
   └─ templates/
      ├─ tienda_*.html         NEW — storefront público (base, home, producto, carrito, checkout*)
      └─ admin_*.html          NEW — productos, producto_editar, resenas, ordenes, orden_detalle, integraciones
```

**Stack:** Flask + SQLAlchemy 2 + APScheduler + Tailwind (CDN). Diseño tomado de
Stitch ("Aura & Essence"): Playfair Display + Montserrat, paleta blush/charcoal/gold.

---

## Variables de entorno

| Variable | Obligatoria | Uso |
|---|---|---|
| `DATABASE_URL` | sí en Render | Postgres connection string. Si está vacía, fallback SQLite en `data/biobella.db` (dev local). |
| `FLASK_SECRET` | sí en prod | Llave para `session` y cookies. Si falta, warning + clave de dev. |
| `APP_URL` | recomendada | URL pública (`https://biobella.com.ar`) para `back_urls` y `notification_url` de MP. Si no se setea, se deriva de `request.url_root` (ojo si estás detrás de proxy). |
| `ANTHROPIC_API_KEY` | opcional | Solo para los módulos IA del ml_system existente — Biobella no la necesita. |
| `MCP_API_TOKEN` | opcional | Bypass de login para wrapper MCP — herencia del ml_system. |

---

## Alta de la app en ML Developers

1. Entrá a https://developers.mercadolibre.com.ar/devcenter
2. Crear aplicación
3. Permisos mínimos: **read** (catálogo), **offline_access** (refresh tokens)
4. **Redirect URI**: `https://<tu-dominio>/oauth/callback` (la que ya usás en el ml_system)
5. Copiá **client_id** y **client_secret**
6. En el admin del ml_system: `/admin/cuentas` → crear cuenta con alias **`novara`**, pegar client_id + client_secret
7. Click "Conectar" → completar el OAuth → quedan persistidos `refresh_token` + `access_token`
8. Ir a `/admin/integraciones` → estado debe figurar "Conectado"
9. Click **"Sincronizar ahora"** — importa todas las publicaciones activas. La segunda corrida se ejecuta automática cada 6h.

> El alias **debe ser exactamente `novara`** — está hardcodeado en `modules/tienda_sync.py:ML_ACCOUNT_ALIAS`. Si querés usar otra cuenta, cambiá esa constante.

---

## Alta de la app en MercadoPago

1. Entrá a https://www.mercadopago.com.ar/developers/panel/app
2. Crear aplicación
3. Tipo de integración: **Checkout Pro** (preferencias)
4. Copiá:
   - **Access token de prueba** (`TEST-…`) — para sandbox
   - **Access token de producción** (`APP_USR-…`) — para cobrar de verdad
5. En `/admin/integraciones` → tarjeta **MercadoPago** → pegar access token + (opcional) public key → Guardar
6. **Configurá la URL de notificaciones** en el panel de MP: `https://<tu-dominio>/api/mp/webhook`

> El sistema detecta automáticamente si el token es sandbox (`TEST-…`) o producción (`APP_USR-…`) y usa la URL de checkout correspondiente.

---

## Pricing model

```
precio_tienda = round(precio_ml × (1 + margen / 100))
```

- **Margen global** editable en `/admin/integraciones` (default 35%)
- **Margen por producto** editable en `/admin/productos/<id>` — pisa el global
- **Precio fijo override** en el mismo editor — pisa el cálculo del margen

---

## Lock por campo (sync-aware)

Cada producto tiene una tabla `product_locks` que registra qué campos están bajo
control del admin. La sync de cada 6h NO toca esos campos.

Campos lockeables:
- `titulo`
- `descripcion`
- `fotos`
- `variantes`
- `stock`
- `precio` (aplica al `precio_tienda_override`; el `precio_ml` siempre se actualiza)

En `/admin/productos/<id>` cada campo tiene un toggle 🔒. Activarlo crea un row
en `product_locks`; desactivarlo lo borra.

---

## Endpoints

### Públicos (sin login)

| Ruta | Método | Propósito |
|---|---|---|
| `/tienda` | GET | Home con grid de productos |
| `/tienda/p/<slug-o-mla>` | GET | Detalle producto con galería + reseñas |
| `/tienda/carrito` | GET | Carrito (session-based) |
| `/tienda/checkout` | GET | Form de checkout |
| `/tienda/checkout/<success\|failure\|pending>` | GET | Landing post-pago |
| `/api/carrito/{add,update,remove}` | POST | Manipulación carrito |
| `/api/reviews/<product_id>` | POST | Submit reseña (pending de moderación) |
| `/api/checkout/crear-preferencia` | POST | Crea Order + MP preference + devuelve init_point |
| `/api/mp/webhook` | POST/GET | IPN MercadoPago — actualiza Order |

### Admin (requieren login + is_admin)

| Ruta | Método | Propósito |
|---|---|---|
| `/admin/integraciones` | GET | Estado ML + MP + sync manual + configs |
| `/admin/productos` | GET | Listado paginado con filtros |
| `/admin/productos/<id>` | GET | Editor con locks por campo |
| `/admin/resenas?filtro=...` | GET | Moderar reseñas |
| `/admin/ordenes?estado=...` | GET | Listado de órdenes |
| `/admin/ordenes/<id>` | GET | Detalle de orden |
| `/api/admin/productos/<id>` | POST | Guardar cambios |
| `/api/admin/productos/<id>/lock` | POST | Toggle lock por campo |
| `/api/admin/resenas/<id>/{aprobar,eliminar}` | POST | Moderación |
| `/api/integraciones/{sincronizar,margen,store-name,mp-credentials}` | POST | Configs |

---

## Tablas SQL (creadas automáticamente al primer arranque)

```
products              catálogo (mla_id único)
product_locks         (product_id, field) → "este campo no lo toca la sync"
sync_log              historial de sincronizaciones (auto cada 6h + manuales)
reviews               reseñas de clientes (con aprobado bool)
orders                órdenes de checkout
order_items           items de cada orden
app_settings          k/v store (store_name, margen_global_default, mp_access_token, etc.)
```

Conviven con el `kv_store` del `core/db_storage.py` existente. No hay conflicto.

---

## Cómo correrlo localmente

```bash
# Instalar dependencias
pip install -r requirements.txt

# Levantar el server
python3 web/app.py

# (Opcional) Seed con productos demo si la cuenta novara no está conectada
PYTHONPATH=. python3 web/seed_demo.py
```

Abrir http://localhost:8080/tienda para ver el storefront público.
Login en http://localhost:8080/login para entrar al admin.

---

## Cómo testear el flujo de checkout en local

1. `/admin/integraciones` → pegar un **access token sandbox** de MP (`TEST-…`)
2. Ir a `/tienda`, agregar productos al carrito
3. `/tienda/carrito` → "Continuar al pago" → llenar datos → "Pagar con MercadoPago"
4. Se redirige a la sandbox de MP — usar tarjetas de prueba:
   - **Aprobada**: `5031 7557 3453 0604`, CVV 123, vto 11/30, nombre `APRO`
   - **Rechazada**: cualquiera con nombre `OTHE`
5. Después del pago vuelve a `/tienda/checkout/success` y se ejecuta el webhook
6. Verificá la orden en `/admin/ordenes`

> Las tarjetas de prueba están documentadas en https://www.mercadopago.com.ar/developers/es/docs/checkout-pro/additional-content/your-integrations/test/cards

---

## Cron de sincronización

Registrado como **Job 10** en `web/app.py:_start_scheduler()`:

```python
jm.register_job(
    'biobella_catalog_sync', _job_biobella_catalog_sync,
    IntervalTrigger(hours=6, timezone='America/Argentina/Buenos_Aires'),
    name='Sync catálogo Biobella',
)
```

Sincronización manual desde el panel — botón "Sincronizar ahora" en `/admin/integraciones`.

**Rate limit interno**: máx 10 req/seg, retry exponencial (1s/3s/9s) en 429 y 5xx.

---

## SEO + Google Shopping

El storefront tiene SEO automatizado end-to-end:

### Automático en cada sync
Cuando se sincroniza un producto nuevo desde ML, el sistema genera `meta_title` y
`meta_description` con un template determinístico (`modules/tienda_sync.py:generate_seo_template`).
También se autogenera el `slug` URL-friendly.

### Regenerar con IA (calidad superior)
- **Individual**: en `/admin/productos/<id>` → botón **"Regenerar con IA"**.
  Usa Claude Haiku ($0.0003 por producto).
- **Bulk**: en `/admin/integraciones` → card "SEO con IA" → **"Regenerar todos"** o
  **"Solo los vacíos"**. Corre en background. Progreso en pantalla.

Necesita `ANTHROPIC_API_KEY` en env.

### Lo que se inyecta automáticamente

**En `/tienda/p/<slug>`:**
- `<title>` con meta_title optimizado
- `<meta name="description">` con meta_description
- `<link rel="canonical">`
- Open Graph (`og:title`, `og:description`, `og:image`, `og:url`, `og:type=product`, `product:price:amount`)
- Twitter Card
- **JSON-LD Schema.org Product** — lo que Google Shopping necesita:
  - name, description, sku, mpn, brand, image[]
  - offers (priceCurrency=ARS, price, availability InStock/OutOfStock, itemCondition NewCondition)
  - aggregateRating (si hay reseñas aprobadas)
  - review[] (las 5 mejores)
- JSON-LD BreadcrumbList

**En `/tienda` (home):**
- JSON-LD Organization
- JSON-LD ItemList con todos los productos

**En `/sitemap.xml`** — auto-generado con todos los productos activos. Lo deberías
enviar a Google Search Console: https://search.google.com/search-console

**En `/robots.txt`** — permite crawler en `/tienda`, bloquea `/admin` y `/api`,
referencia el sitemap.

### Setup Google Search Console (5 min)

1. Entrá a https://search.google.com/search-console
2. Agregar propiedad → tipo "Prefijo de URL" → `https://ml-system-rr81.onrender.com`
   (o tu dominio propio cuando lo tengas)
3. Verificar propiedad: copiá el meta-tag de verificación y pegalo en el `<head>` de
   `web/templates/tienda_base.html` (`<meta name="google-site-verification" ...>`)
4. Una vez verificada → Sitemaps → agregar `https://<tu-dominio>/sitemap.xml`
5. Google va a indexar tus productos en 1-7 días

### Setup Google Merchant Center (para Google Shopping)

1. Entrá a https://merchants.google.com → crear cuenta
2. **Verificar dominio** (mismo que en Search Console)
3. **Crear feed de productos**: hay dos formas:
   - **A — Crawl automático**: Merchant Center crawlea tu sitio y lee el JSON-LD Product
     de cada `/tienda/p/<slug>`. Necesita que el sitemap esté indexado.
   - **B — Feed XML/CSV**: subir manualmente. Para automatizar esto en el futuro,
     se puede agregar una ruta `/feed/google-shopping.xml` que genere el formato
     que Google espera (no implementado, contactame si lo querés).
4. Una vez aprobada la cuenta y los productos pasen revisión (24-72h), aparecen en
   búsquedas de Google Shopping y la pestaña de Shopping.

### Estructura del Schema.org Product (verificá con Rich Results Test)

Después del deploy, validá que el JSON-LD esté bien:
1. https://search.google.com/test/rich-results
2. Pegá una URL de producto, ej. `https://ml-system-rr81.onrender.com/tienda/p/<slug>`
3. Tiene que detectar **"Product"** y **"BreadcrumbList"** sin errores

## TODOs / nice-to-haves no implementados

- [ ] Cálculo real de envío (hoy: gratis arriba de $25.000, sin cargo abajo)
- [ ] Email transaccional confirmación de compra (Resend/SES/SendGrid)
- [ ] Cuentas de cliente con historial (hoy: checkout como invitado)
- [ ] Cupones/descuentos
- [ ] Soft delete vs hard delete de productos sincronizados
- [ ] Migración a Alembic cuando aparezca primera schema change real
- [ ] Feed XML específico para Google Merchant Center (hoy se sirve via JSON-LD crawl)
