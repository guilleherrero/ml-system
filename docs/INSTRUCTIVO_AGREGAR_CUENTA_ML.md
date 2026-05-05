# Instructivo: cómo agregar una nueva cuenta ML al sistema

Este documento explica el proceso paso a paso para conectar una nueva cuenta de
MercadoLibre al Sistema ML (multi-tenant — sin límite de cuentas).

> **Requisito**: ser usuario administrador del sistema.

---

## Paso 1 — Crear app en developers de MercadoLibre

Cada cuenta ML necesita su propia app en el centro de developers de MercadoLibre.

1. Andá a **https://developers.mercadolibre.com.ar/devcenter**
2. Iniciá sesión con la cuenta ML del vendedor que vas a conectar
3. Click en **"Crear nueva app"** (o "Create app")
4. Llená los datos básicos de la app:
   - **Nombre**: ej. "Sistema ML — [Nombre del vendedor]"
   - **Descripción**: breve descripción del uso
   - **Sitio web**: la URL del sistema (`https://ml-system-rr81.onrender.com`)
5. **Configurá la Redirect URI** (IMPORTANTE):
   ```
   https://ml-system-rr81.onrender.com/oauth/exchange
   ```
6. **Activá los permisos (scopes) que necesite** la cuenta:
   - **Items y búsqueda** ✓ (obligatorio para optimizar publicaciones)
   - **Órdenes** ✓ (obligatorio para ver ventas)
   - **Postventa** ✓ (obligatorio para reclamos)
   - **Publicidad — Read** ✓ (si la cuenta usa Mercado Ads)
   - **Publicidad — Write** ✓ (si querés pausar/modificar campañas)
7. Guardá la app
8. Anotá los datos que te da MercadoLibre:
   - **Client ID** (números, ej: `1234567890123456`)
   - **Client Secret** (string largo)

---

## Paso 2 — Agregar la cuenta en el sistema

1. Andá a **Sistema ML → sidebar → "Gestión de cuentas"** (`/admin/cuentas`)
2. Bajá hasta **"Agregar nueva cuenta"**
3. Llená el formulario:
   - **Alias**: identificador interno corto (ej: `Cliente_Acme`, `NovaraSur`).
     Solo letras, números, guiones bajos y guiones. **Único en el sistema.**
   - **Nickname** (opcional): nombre legible de la cuenta (ej: "Acme S.A. — principal")
   - **Client ID**: lo que obtuviste en el Paso 1
   - **Client Secret**: lo que obtuviste en el Paso 1
4. Click **"Crear y conectar OAuth"**

---

## Paso 3 — Autorizar OAuth

1. El sistema te redirige automáticamente a MercadoLibre
2. Si no tenés sesión iniciada en ML con esa cuenta, ML te pide login
3. Te muestra una pantalla con la lista de permisos que la app pide
4. Click **"Permitir"** (autorizar)
5. ML te redirige automáticamente al sistema
6. La cuenta ya queda **activa** en el panel

---

## Verificación post-conexión

1. Volvé a **"Gestión de cuentas"** — la cuenta nueva aparece con badge ✓ Conectada
2. Andá a **"Permisos API"** — verificá que los 5 permisos están en verde para la nueva cuenta
3. La cuenta nueva aparece automáticamente en el selector de cuentas del sistema

---

## Asignar usuarios a la cuenta nueva

Si tenés usuarios estándar (no-admin) que solo deben ver ciertas cuentas:

1. Andá a **sidebar → "Usuarios"** (`/settings/usuarios`)
2. Click en el usuario que querés modificar (o creá uno nuevo)
3. Marcá las cuentas a las que tendrá acceso
4. Guardar

El usuario solo verá esas cuentas en el sidebar y dropdowns.

---

## Pausar / eliminar una cuenta

### Pausar (soft delete — recomendado)
- En "Gestión de cuentas", botón naranja **"Pausar"** en la fila de la cuenta
- La cuenta queda inactiva pero **conserva todos sus datos**
- Podés reactivarla cuando quieras (botón verde "Reactivar")
- **Auto-purga**: si pasan 90 días sin reactivar, se elimina automáticamente

### Eliminar (hard delete — irreversible)
- En "Gestión de cuentas", botón rojo **"Eliminar"** en la fila de la cuenta
- Pide confirmación: tenés que escribir el alias exacto
- La cuenta sale del sistema inmediatamente
- **No borra los JSON históricos** (eso lo hace el cron 90 días después)

---

## Troubleshooting

### "An account with alias 'X' already exists"
Ya existe una cuenta con ese alias. Elegí otro nombre.

### "redirect_uri_mismatch" cuando autorizo OAuth
La Redirect URI configurada en developers ML no coincide con la del sistema.
Verificá que sea exactamente:
```
https://ml-system-rr81.onrender.com/oauth/exchange
```
(sin slash al final, todo en minúsculas).

### La cuenta queda creada pero "Sin OAuth"
Algo falló en el callback de OAuth. Click en **"Reconectar OAuth"** en la fila
de la cuenta para reintentar el flujo.

### Permisos en rojo después de conectar
- Verificá que activaste TODOS los toggles necesarios en developers ML
- Si activaste alguno DESPUÉS de conectar OAuth, click "Reconectar OAuth"
  para que el nuevo scope tome efecto

---

## Notas de seguridad

- **Solo admin** puede agregar/pausar/eliminar cuentas
- El **Client Secret** se guarda en `config/accounts.json` (igual que el resto
  de credenciales OAuth)
- El sistema **no expone** el Client Secret en ninguna pantalla después de creado
- Cada acción queda registrada en `data/audit.log` con username del admin

---

## Costo por cuenta agregada

Cada cuenta nueva consume el mismo presupuesto del sistema:
- **Optimización IA**: $0.50-1.50 por publicación optimizada
- **Veredicto IA**: $0.10 por veredicto (cap $30/mes total)
- **Replicador IA**: $0.005 por preview (cap $5/mes total)

Los caps son **globales del sistema**, no por cuenta. Si tenés 5 cuentas que
generan veredictos, el cap mensual sigue siendo $30.

---

**Última actualización**: 05/05/2026 (Sprint Admin)
