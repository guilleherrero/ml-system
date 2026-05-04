# Design System — Sistema ML Novara

**Sprint 5.2 / 2026-05-04**
**Estado:** infraestructura completa. Migración template-por-template pendiente (Sprint 5.2-bis).

---

## Filosofía

**REFACTOR, no rediseño.**

El design system se construyó AUDITANDO los valores ya en uso en el sistema (33 templates, ~2.000 LOC de CSS embebido) y nombrándolos. Los hex, tamaños y spacings de los tokens fueron calibrados para coincidir EXACTAMENTE con los valores más usados en producción — pasar a tokens NO debería cambiar la apariencia de NINGUNA pantalla.

Reglas de oro:

1. **No inventar componentes** sin validar que el uso real existe. El audit detectó "13 templates definen `.active`" como falsa duplicación — en realidad eran 13 componentes compound (`.step-pill.active`, `.filter-pill.active`, etc.) con su propio active state. Generalizarlos sería forzar un patrón inexistente.
2. **Tokens primero, componentes después.** Si un patrón se repite 5+ veces en distintos templates con valores idénticos, candidato a `components.css`. Si no, dejarlo local.
3. **Coexistir con Bootstrap.** Bootstrap 5.3 sigue siendo la base. Componentes propios usan prefijo `.app-*` para evitar choque con `.btn`, `.card`, `.badge`, `.alert` de Bootstrap.
4. **No tocar `modules/seo_optimizer.py`.** Regla #1 inmutable del PLAN.

---

## Archivos del sistema

| Archivo | Responsabilidad |
|---|---|
| `web/static/css/tokens.css` | Variables CSS (`--color-*`, `--text-*`, `--spacing-*`, `--radius-*`, `--shadow-*`) |
| `web/static/css/components.css` | Componentes reusables con prefijo `.app-*` |
| `web/templates/base.html` | Linkea ambos en `<head>` después del Bootstrap CDN |

Carga en cada page: `Bootstrap → Bootstrap Icons → tokens → components → <style> interno del template`.

---

## Tokens — `tokens.css`

### Colores semánticos

Escala 50-100-400-500-700:
- **50** = fondo muy claro (banner soft)
- **100** = fondo light (badge bg, alert bg)
- **400** = brillante (KPIs, números — corresponde a tailwind-500)
- **500** = principal (texto, borde — corresponde a tailwind-600)
- **700** = oscuro (énfasis sobre fondo light)

| Token | Hex | Uso |
|---|---|---|
| `--color-critical-50` | `#fef2f2` | Banner critical soft |
| `--color-critical-100` | `#fee2e2` | Badge crit fondo |
| `--color-critical-400` | `#ef4444` | KPIs crit, borders left |
| `--color-critical-500` | `#dc2626` | Texto crit, badges |
| `--color-critical-700` | `#991b1b` | Texto sobre fondo crit-100 |
| `--color-warning-50` | `#fffbeb` | — |
| `--color-warning-100` | `#fef3c7` | Badge warn fondo |
| `--color-warning-400` | `#f59e0b` | KPIs warn |
| `--color-warning-500` | `#d97706` | Texto warn |
| `--color-warning-700` | `#92400e` | Texto sobre fondo warn-100 |
| `--color-success-50` | `#f0fdf4` | — |
| `--color-success-100` | `#dcfce7` | Badge ok fondo |
| `--color-success-400` | `#10b981` | KPIs ok |
| `--color-success-500` | `#16a34a` | Texto success |
| `--color-success-700` | `#15803d` | Texto sobre fondo success-100 |
| `--color-info-100` | `#dbeafe` | Badge info fondo |
| `--color-info-300` | `#93c5fd` | Hover/border info |
| `--color-info-400` | `#60a5fa` | Iconos info |
| `--color-info-500` | `#3b82f6` | Acento principal |
| `--color-info-600` | `#2563eb` | Acción primaria |
| `--color-info-700` | `#1d4ed8` | Pressed |
| `--color-neutral-0` | `#ffffff` | Fondo cards |
| `--color-neutral-50` | `#f8fafc` | Fondo muy claro |
| `--color-neutral-100` | `#f1f5f9` | Fondo cards alt |
| `--color-neutral-200` | `#e2e8f0` | Bordes/dividers |
| `--color-neutral-400` | `#94a3b8` | Texto secundario |
| `--color-neutral-500` | `#6b7280` | Texto neutro |
| `--color-neutral-600` | `#64748b` | Texto neutro alt |
| `--color-neutral-700` | `#374151` | Texto oscuro |
| `--color-neutral-800` | `#1e293b` | Fondo oscuro |
| `--color-neutral-900` | `#0f1117` | Sidebar bg |
| `--color-sidebar-bg` | `#0f1117` | Sidebar fondo |
| `--color-sidebar-bg-alt` | `#161920` | Sidebar fondo alt |
| `--color-sidebar-border` | `#1e2128` | Sidebar bordes |
| `--color-sidebar-text` | `#6b7280` | Sidebar texto |
| `--color-sidebar-text-hover` | `#e5e7eb` | Sidebar texto hover |
| `--color-sidebar-text-active` | `#ffffff` | Sidebar texto activo |
| `--color-sidebar-link-hover-bg` | `#1a1d26` | Sidebar link hover |
| `--color-sidebar-link-active-bg` | `#1a1d26` | Sidebar link activo |

### Tipografía

| Token | Valor | Uso |
|---|---|---|
| `--font-base` | `'Segoe UI', system-ui, ...` | Familia base |
| `--text-xxs` | `0.65rem` | Leyendas diminutas |
| `--text-xs` | `0.72rem` | Labels, hints (top: 141 usos) |
| `--text-sm` | `0.78rem` | Texto comprimido |
| `--text-md` | `0.85rem` | Body comprimido |
| `--text-base` | `0.875rem` | Estándar |
| `--text-lg` | `1rem` | Body grande |
| `--text-h3` | `1.1rem` | Subtítulos |
| `--text-h2` | `1.3rem` | Títulos sección |
| `--text-h1` | `1.75rem` | Números KPI |
| `--text-display` | `2rem` | Display titles |
| `--line-height-tight` | `1.2` | Headers |
| `--line-height-base` | `1.5` | Body |
| `--line-height-relaxed` | `1.7` | Lectura larga |
| `--font-weight-regular` | `400` | Body |
| `--font-weight-medium` | `500` | Énfasis suave |
| `--font-weight-semibold` | `600` | Headers menores |
| `--font-weight-bold` | `700` | Headers, KPIs |

### Espaciado, radios, sombras, iconos

| Token | Valor |
|---|---|
| `--spacing-xxs` | `2px` |
| `--spacing-xs` | `4px` |
| `--spacing-sm` | `8px` |
| `--spacing-md` | `16px` |
| `--spacing-lg` | `24px` |
| `--spacing-xl` | `32px` |
| `--spacing-2xl` | `48px` |
| `--radius-sm` | `4px` (botones chicos, badges) |
| `--radius-md` | `8px` (cajas, botones) |
| `--radius-lg` | `10px` (cards estándar) |
| `--radius-xl` | `12px` (cards extra-redondeadas, base.html) |
| `--radius-pill` | `999px` (pills, avatares) |
| `--shadow-sm` | `0 1px 4px rgba(0,0,0,0.07)` (elevation-100) |
| `--shadow-md` | `0 4px 12px rgba(0,0,0,0.15)` (elevation-200) |
| `--shadow-lg` | `0 12px 32px rgba(0,0,0,0.20)` (elevation-300) |
| `--icon-xs` | `12px` |
| `--icon-sm` | `16px` |
| `--icon-md` | `20px` |
| `--icon-lg` | `24px` |
| `--icon-xl` | `32px` |
| `--sidebar-width` | `240px` |

---

## Componentes — `components.css`

Todos con prefijo `.app-*` para coexistir con Bootstrap.

### `.app-card`

```html
<div class="app-card">
  <div class="app-card-header">Título</div>
  <div style="padding:16px">Contenido</div>
</div>
```

### `.app-stat-box` (KPI grandes)

```html
<div class="app-stat-box ok">
  <div class="num">$11.5M</div>
  <div class="lbl">Impacto detectado</div>
</div>
```

Variantes: `.crit` (#ef4444) · `.warn` (#f59e0b) · `.ok` (#10b981) · `.blue` (#3b82f6).

### `.app-badge-*`

```html
<span class="app-badge-ok">Activa</span>
<span class="app-badge-warn">Pendiente</span>
<span class="app-badge-crit">Crítico</span>
<span class="app-badge-info">Info</span>
<span class="app-badge-neutral">—</span>
```

### `.app-btn` + variantes

```html
<button class="app-btn app-btn-primary">Aplicar</button>
<button class="app-btn app-btn-secondary">Cancelar</button>
<button class="app-btn app-btn-danger">Eliminar</button>
<button class="app-btn app-btn-ghost">Ver más</button>
<button class="app-btn app-btn-primary app-btn-xs">Mini</button>
```

### `.app-pill` (chips/tags)

```html
<span class="app-pill app-pill-info">Top 3</span>
<span class="app-pill app-pill-success">+12%</span>
<span class="app-pill app-pill-warn">Atención</span>
<span class="app-pill app-pill-crit">Bloqueado</span>
```

### `.app-alert-banner`

```html
<div class="app-alert-banner app-alert-banner-crit">
  <i class="bi bi-exclamation-triangle"></i>
  <div>Mensaje de error crítico</div>
</div>
```

Variantes: `app-alert-banner-crit/-warn/-success/-info`.

### `.app-data-table`

```html
<table class="table app-data-table">
  <thead>
    <tr><th>Producto</th><th>Stock</th></tr>
  </thead>
  <tbody>
    <tr class="row-crit"><td>...</td></tr>
    <tr class="row-warn"><td>...</td></tr>
  </tbody>
</table>
```

### `.app-ins-card` y `.app-act-card` (insights / actions)

```html
<div class="app-act-card">
  <h6>Pausá Hot Sale ADS</h6>
  <p>Sangrando $1K/día sin conversión</p>
  <button class="app-btn app-btn-primary app-btn-xs">Hacer ahora</button>
</div>
```

### `.app-pos-*` (position pills)

```html
<span class="app-pos-top">Top 1</span>
<span class="app-pos-mid">12</span>
<span class="app-pos-low">48+</span>
<span class="app-pos-none">—</span>
```

---

## Aliases retrocompatibles

Mantienen el nombre viejo de variables que estaban en `<style>` interno de `base.html`. Los templates que usen `var(--sb)`, `var(--sbw)`, `var(--accent)`, etc. siguen funcionando durante la transición.

| Alias | Equivale a | Plan |
|---|---|---|
| `--sb` | `var(--color-sidebar-bg)` | Eliminar tras Sprint 5.2-bis |
| `--sb2` | `var(--color-sidebar-bg-alt)` | Eliminar tras Sprint 5.2-bis |
| `--sbw` | `var(--sidebar-width)` | Eliminar tras Sprint 5.2-bis |
| `--accent` | `var(--color-info-500)` | Eliminar tras Sprint 5.2-bis |
| `--accent2` | `#6366f1` (indigo, único uso) | Mantener o reemplazar inline |

---

## Cómo agregar un componente nuevo

**Antes de crear:**

1. ¿El patrón se repite **5+ veces** en distintos templates con valores **idénticos o casi idénticos**?
   - SÍ → candidato a componente
   - NO → mantenelo local en el `<style>` del template
2. ¿El patrón ya existe como clase de Bootstrap?
   - SÍ → usá Bootstrap (`.btn`, `.alert`, `.card`, `.badge`, etc.)
   - NO → seguí
3. ¿Hace falta una clase compound (`.X-pill.active`, `.Y-tab.active`) o un estado específico para un componente local?
   - SÍ → mantenelo local. La duplicación SUPERFICIAL no siempre es duplicación REAL.

**Si decidís crear:**

1. Definí la clase en `web/static/css/components.css` con prefijo `.app-`.
2. Usá tokens de `tokens.css` para todos los valores. NO hardcodear hex.
3. Documentá en este archivo (sección "Componentes") con ejemplo de uso.
4. Reemplazá los usos locales por `.app-*` en commits chicos (uno por template) — no big bang.

---

## Anti-patterns

**No hagas esto:**

❌ **Crear un componente para "duplicación aparente" sin validar uso real.**
Ejemplo concreto: el audit reportó `.active` definido en 13 templates como duplicación. Investigación reveló que eran 13 componentes compound DIFERENTES (`.step-pill.active`, `.filter-pill.active`, `.f-pill.sube.active`, `.tab-pane.active`, etc.) con su propio active state. Generalizarlos en `.app-active` habría obligado a refactorizar cada template para acomodar un patrón que no existe.

❌ **Hacer reemplazo masivo sin entender impactos en JS.**
Ejemplo concreto: parecía obvio reemplazar los 56 × `style="display:none"` por `class="d-none"` (Bootstrap utility). Pero `.d-none` lleva `!important`, y el JS actual del sistema toggea visibilidad con `el.style.display = 'block'` — cambio masivo habría dejado banners, badges y modales ocultos para siempre. La migración correcta requiere actualizar el JS también, en sprint dedicado.

❌ **Romper el orden de carga del CSS.**
Bootstrap → Bootstrap Icons → tokens → components → `<style>` interno. Si tokens carga DESPUÉS de un `<style>` que los usa, el navegador resuelve `var(--color-info-500)` como inválido y aplica el fallback `initial`.

❌ **Hardcodear hex en `components.css`.**
Si un componente necesita un color que no está en tokens, AGREGÁ EL TOKEN antes que hardcodear. Cada hex en components.css es deuda futura.

❌ **Cambiar un token sin auditar uso.**
`--radius-lg` está en 10px porque el audit detectó 85 usos de `border-radius:10px`. Cambiarlo a 12px alteraría visualmente 85 lugares. Si querés cambiar un token, primero leé qué impacto visual tiene.

❌ **Sobrescribir Bootstrap.**
Si necesitás un botón distinto del `.btn` de Bootstrap, creá `.app-btn` separado. NO hagas `.btn { ... }` redefiniendo Bootstrap — afectaría a TODOS los `.btn` de la app.

---

## Pendientes conocidos

### Sprint 5.2-bis — Migrar 32 templates restantes a tokens

Solo `base.html` migró en Sprint 5.2. Los otros 32 templates tienen su propio `<style>` con hex hardcodeados:

- monitor_evolucion.html (257 LOC) — el más cargado
- posiciones.html (134), optimizaciones.html (124), meli_ads.html (123), preguntas.html (117)
- reputacion.html (104), multicuenta.html (90), lanzar_nuevo.html (86), settings_permisos.html (84)
- Más 24 templates chicos

**Plan:** un commit por template grande, validación visual antes del próximo. Los chicos se pueden agrupar de a 3-4.

### Sprint dedicado — Cleanup de `display:none` / `style.display` JS

Hay 56 × `style="display:none"` inline + 30+ líneas de JS toggleando visibilidad con `el.style.display = ...`. Migrarlos a `.d-none` requiere también pasar el JS a `el.classList.toggle('d-none')`.

Templates afectados con JS toggle: `base.html`, `dashboard.html`, `alertas.html`, `competencia.html`, `duplicados.html`, `funnel.html`, `monitor_evolucion.html`, `analisis_experto.html`, `evaluar_producto.html`, `optimizaciones.html`, `repricing.html`, y otros.

**Plan:** sprint dedicado con validación funcional individual de cada toggle (banner permisos, badges alertas/preguntas/monitor, modales, filtros que muestran/ocultan filas, wizard pasos, loading states).

### Sprint 5.2-bis (al final) — Eliminar aliases retrocompatibles

Cuando los 32 templates dejen de usar `var(--sb)`, `var(--sbw)`, `var(--accent)`, `var(--accent2)`, eliminar los aliases del final de `tokens.css`.

---

## Validación

Después de cualquier cambio en `tokens.css`, `components.css` o `base.html`:

```bash
bash tests/run_regresion.sh
```

Validación visual obligatoria (manual) para sprints que toquen base.html o componentes:
1. Abrir local/producción
2. Revisar que estas pantallas se ven idénticas:
   - Command Center
   - Optimizar IA
   - Stock y Rentabilidad
   - Monitor de Evolución
   - Alertas
   - Settings
   - Salud de Catálogo
   - Repricing wizard

Si alguna se ve significativamente distinta, no mergear sin investigar.
