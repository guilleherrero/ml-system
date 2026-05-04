# Investigación — Permisos de escritura en Mercado Ads (Novara)

**Síntoma reportado:** el usuario no puede modificar presupuesto en Meli ADS. El sistema responde *"Sin permiso para escribir en la API de Advertising. Reconectá tu cuenta con permisos de escritura ampliados."*

**Estado:** investigación completada. **NO se modificó código.**

---

## 1. Estado actual del código

### URL de autorización OAuth ([web/app.py:11401-11427](web/app.py#L11401))

```python
@app.route('/oauth/connect/<alias>')
def oauth_connect(alias):
    ...
    params = {
        'response_type':         'code',
        'client_id':             acc['client_id'],
        'redirect_uri':          ML_REDIRECT_URI,
        'state':                 alias,
        'code_challenge':        challenge,
        'code_challenge_method': 'S256',
        'scope':                 'offline_access read write',   # ← scopes pedidos
    }
    auth_url = 'https://auth.mercadolibre.com.ar/authorization?' + urllib.parse.urlencode(params)
    return redirect(auth_url)
```

**Scopes solicitados:** `offline_access read write` — **el máximo posible** según los valores válidos del parámetro `scope` en MercadoLibre OAuth ([fuente](https://developers.mercadolibre.com.ar/en_us/authentication-and-authorization)).

> **Hallazgo clave:** los scopes en la URL OAuth de ML solo aceptan tres valores literales: `offline_access`, `read`, `write`. **No existe un scope específico tipo `advertising_write` o `product_ads_write`** que se pueda agregar a la URL.

### Endpoint de escritura que falla ([modules/meli_ads_engine.py:2375](modules/meli_ads_engine.py#L2375))

```python
def update_campaign_budget(token: str, campaign_id: int, new_budget: float) -> dict:
    r = _ads_put(
        f'/advertising/product_ads/campaigns/{campaign_id}',
        token,
        {'daily_budget': new_budget},
    )
    if r['ok']:
        return {'ok': True, ...}

    if r['status'] == 401:
        return {'ok': False, 'status': 401,
                'message': 'Sin permiso para escribir en la API de Advertising. '
                           'Reconectá tu cuenta con permisos de escritura ampliados.'}
```

→ La PUT llega a la API y vuelve con **401 Unauthorized**. El mensaje que ve el usuario lo genera este branch del código.

### Botón "Reconectar / Activar publicidad" ([web/templates/meli_ads.html:140](web/templates/meli_ads.html#L140))

```html
<a class="btn-action" href="/oauth/connect/{{ account_alias }}" ...>
  <i class="bi bi-key-fill"></i> Reconectar / Activar publicidad
</a>
```

Apunta al **mismo flujo OAuth genérico**. Re-pide los mismos 3 scopes (`offline_access read write`). No hay un flujo especial diferenciado para "habilitar ads".

### Otros endpoints relevantes

- `_ads_get('/advertising/product_ads/campaigns', ...)` — funciona (retorna las campañas)
- `_ads_get('/advertising/product_ads/campaigns/{id}/metrics', ...)` — funciona (retorna métricas)
- `_ads_put('/advertising/product_ads/campaigns/{id}', {'daily_budget': X})` — **falla con 401**

→ La cuenta tiene permiso de **lectura** (todos los GET funcionan), pero **NO de escritura**.

---

## 2. Hallazgos de la documentación oficial de ML

Aplicada por las búsquedas web a la documentación oficial de MercadoLibre Developers:

1. **Los scopes en la URL OAuth son genéricos**: solo `offline_access`, `read`, `write` ([fuente](https://developers.mercadolibre.com.ar/en_us/authentication-and-authorization)).

2. **Los permisos por API se configuran en el panel de la app** ("Permisos Funcionales" / "Functional Permissions"). Cada API tiene un toggle independiente con dos niveles ([fuente](https://developers.mercadolibre.com.ar/devsite/functional-permissions)):
   > - **Read only**: allows the use of API GET HTTPS methods
   > - **Read and write**: allows the use of API PUT, POST and DELETE HTTPS methods

3. **Existe un permiso funcional "Advertising" / "Publicidad"** que controla acceso a `/advertising/*`:
   > "There is a permission that allows your application to access, create and manage advertising campaigns, allowing access to Advertising resources."

4. **Después de cambiar permisos en el panel, los usuarios deben reconectar** (autorizar de nuevo) para que su token incluya los nuevos permisos. Los tokens emitidos antes del cambio mantienen los permisos viejos hasta que expiren o se refresquen con reconexión.

5. **Error específico documentado**: si la cuenta del **vendedor** (no la app) no tiene Product Ads activado, da 404 *"No permissions found for user_id"* — pero acá vemos 401, así que **no es ese caso** (Novara claramente tiene campañas activas, las vemos en la lista).

---

## 3. Las cinco hipótesis

### ⭐ HIPÓTESIS 1 — App sin permiso "Publicidad" Read+Write activado en el panel (LA MÁS PROBABLE)

**Tesis:** la aplicación del usuario, en `developers.mercadolibre.com.ar/applications`, tiene en el panel de "Permisos Funcionales" el toggle de **Publicidad** activado pero **solo en modo Read**, no en Read+Write. Por eso los GET funcionan y los PUT vuelven con 401.

**Evidencia que sostiene esta hipótesis:**
- Los GET a `/advertising/product_ads/campaigns` funcionan → tiene permiso de lectura.
- Los PUT a `/advertising/product_ads/campaigns/{id}` vuelven con 401 → **no tiene permiso de escritura**.
- La doc oficial dice que read y write se controlan por separado por cada API.
- El scope `write` en la URL OAuth está bien (es el genérico requerido), pero **no alcanza** sin el toggle por API.

**Cómo validar (acción manual del usuario):**

1. Entrar a https://developers.mercadolibre.com.ar/devcenter (o `.com` global)
2. Ir a **"Mis aplicaciones"** y abrir la aplicación que está usando para Novara (la del `client_id` configurado)
3. Buscar la sección **"Permisos funcionales"** / **"Functional permissions"**
4. Localizar el permiso **"Publicidad"** / **"Advertising"**
5. Verificar el modo: si dice **"Solo lectura"** o solo está marcado **Read** → ahí está el bug
6. Cambiar a **"Lectura y escritura"** / **"Read and write"** y guardar
7. **Hacer click en "Reconectar / Activar publicidad"** desde la app → el token nuevo va a tener el permiso ampliado
8. Intentar modificar el presupuesto otra vez

**Cambio en el código si esta es la causa:** **NINGUNO**. El código ya pide el scope correcto, falta config del panel.

**Complejidad:** **BAJA**. 3-5 minutos de configuración + 1 reconexión.

**Probabilidad subjetiva:** **~85%**. El síntoma (read sí, write no) coincide exactamente con este caso.

---

### HIPÓTESIS 2 — Vendedor (cuenta Novara) no tiene Product Ads activado en su perfil ML

**Tesis:** la cuenta NOVARA en mercadolibre.com.ar no activó la funcionalidad de Publicidad en su perfil de vendedor. Esto generaría error 404 *"No permissions found for user_id"*.

**Evidencia en contra:** el sistema reporta **401**, no 404. Y muestra **4 campañas activas** con métricas reales (TOP 2, Hot Sale, TOP, IMPULSO). Si Product Ads no estuviera activado, no habría campañas que listar. Esta hipótesis está **descartada por la evidencia observable**.

**Cómo validar (defensivo, por si acaso):** entrar a www.mercadolibre.com.ar > Mi perfil > Publicidad / Listings management → Advertising campaign. Confirmar que está activo.

**Cambio en el código:** ninguno. Es activación del lado del vendedor.

**Complejidad:** trivial.

**Probabilidad subjetiva:** **~5%** (descartada por evidencia, vale chequear igual).

---

### HIPÓTESIS 3 — Aprobación manual de ML (tipo "Advertiser App" registrada formalmente)

**Tesis:** algunos endpoints sensibles de ML requieren que la app esté registrada formalmente en un programa (ej. partners de "Postventa" para `/users/{user_id}/claims`). Quizás Advertising write requiere lo mismo.

**Evidencia:** la doc oficial **no menciona** un programa de partners certificados para Advertising. Solo habla del toggle Read/Write en Permisos Funcionales. El precedente de "Postventa" (que sí requiere acuerdo formal) sugiere que ML lo documenta cuando aplica — la ausencia de mención sugiere que Advertising no es así.

**Cómo validar:** después de hacer la HIPÓTESIS 1, si sigue dando 401 → contactar soporte ML developers y preguntar si hay un proceso adicional.

**Cambio en el código:** ninguno hasta tener respuesta de ML.

**Complejidad:** ALTA si fuera el caso (puede tardar semanas el acuerdo formal).

**Probabilidad subjetiva:** **~5%**.

---

### HIPÓTESIS 4 — Scope correcto pero con nombre/formato distinto en la URL OAuth

**Tesis:** quizás existe un scope tipo `advertising_write` que hay que agregar al parámetro `scope` de la URL OAuth.

**Evidencia en contra:** la doc oficial es explícita — solo `offline_access`, `read`, `write` son aceptados. Probar otros valores en la URL OAuth genera 400 Bad Request en el endpoint de autorización, no falla en silencio. Esta hipótesis está **descartada**.

**Cambio en el código:** ninguno.

**Probabilidad subjetiva:** **~0%**.

---

### HIPÓTESIS 5 — Workaround técnico (usar otro endpoint o método)

**Tesis:** quizás se puede modificar presupuesto sin write directo a `/advertising/product_ads/campaigns/{id}`.

**Análisis de opciones:**
- **PATCH en lugar de PUT:** la API ML usa PUT documentado para este recurso. PATCH no está documentado, probablemente da 405 Method Not Allowed.
- **Endpoint alternativo:** no existe. El presupuesto vive en el recurso `campaign` y solo se modifica con PUT/POST sobre ese recurso.
- **Manual desde la UI de ML:** el vendedor entra a www.mercadolibre.com.ar > Mi publicidad > edita el presupuesto. **Funciona seguro** pero no es solución, es bypass.
- **CSV upload o batch tools:** ML tiene herramientas de carga masiva pero requieren los mismos permisos OAuth.

**Conclusión:** el único "workaround" real es modificar manualmente desde la web de ML mientras se resuelve la causa real. No hay ruta API alternativa.

**Cambio en el código:** ninguno.

**Probabilidad subjetiva como solución estable:** **0%** (es bypass, no fix).

---

## 4. Conclusión y plan recomendado

### Diagnóstico

El **scope OAuth en el código es correcto** (`offline_access read write`). El problema **no es de código** — es que la **aplicación de developers** del usuario no tiene activado el toggle de **Publicidad → Read AND write** en el panel de Permisos Funcionales (Hipótesis 1).

### Pasos en orden de menor a mayor esfuerzo

| Paso | Acción | Quién | Tiempo |
|------|--------|-------|--------|
| 1 | Confirmar Hipótesis 2: cuenta NOVARA tiene Publicidad activada en su perfil ML | Usuario | 30 seg |
| 2 | Verificar Hipótesis 1: panel `developers.mercadolibre.com.ar/applications` → app Novara → Permisos Funcionales → Publicidad → cambiar a "Lectura y escritura" | Usuario | 3-5 min |
| 3 | Reconectar la cuenta Novara desde el botón de la app (`/oauth/connect/Cuenta%201`) — esto refresca el token con el nuevo permiso | Usuario | 30 seg |
| 4 | Probar modificar presupuesto en una campaña (idealmente con un cambio mínimo, ej +$10) | Usuario | 30 seg |
| 5 | Si paso 4 funciona → bug resuelto, sin cambios de código necesarios | — | — |
| 6 | Si paso 4 sigue dando 401 → escalar a Hipótesis 3 (contactar soporte ML developers) | Usuario | días/semanas |

### Cambios de código necesarios

**Ninguno**, en el escenario más probable. El sistema ya está bien configurado del lado código.

### Mejora menor opcional (post-fix)

Si la Hipótesis 1 se confirma, vale la pena mejorar el mensaje de error 401 actual:

```python
# en update_campaign_budget()
if r['status'] == 401:
    return {'ok': False, 'status': 401,
            'message': 'Sin permiso para escribir en Advertising. '
                       'Verificá que tu app en developers.mercadolibre.com.ar '
                       'tenga el permiso "Publicidad" en modo Read+Write, '
                       'y luego reconectá la cuenta.'}
```

Esto guía al usuario al lugar correcto de configuración. Es un cambio chico, **opcional**, y solo después de confirmar el diagnóstico.

---

## Sources

- [Authentication and Authorization — Mercado Libre Developers](https://developers.mercadolibre.com.ar/en_us/authentication-and-authorization)
- [Functional Permissions — Mercado Libre Developers](https://developers.mercadolibre.com.ar/devsite/functional-permissions)
- [Create application — Mercado Libre Developers](https://global-selling.mercadolibre.com/devsite/create-application)
- [Application and permissions — Mercado Libre Developers](https://global-selling.mercadolibre.com/devsite/application-manager-gs)
- [Product Ads — Mercado Libre Developers](https://developers.mercadolibre.com.ar/en_us/product-ads-us-read)
