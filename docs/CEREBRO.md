# CEREBRO — Capa de aprendizaje del Sistema ML

> Documento de diseño. Define qué observa el sistema, cómo decide, cómo registra
> resultados y cómo cambia su comportamiento a partir de ellos. Es la base de los
> Sprints A–D. Se actualiza a medida que se van definiendo los bloques.

Estado: **Sprint A implementado** (bloques 1.1–1.4, 2.3–2.4 y 4.1 en codigo; 3, 4.2, 5–11 definidos o pendientes)
Última actualización: 2026-09-07

---

## 0. Principio

El sistema ya observa (9 crons) y ya actúa (Aplicar TODO, repricing, respuestas).
Lo que le falta es **memoria de resultados**: nadie le dice si lo que hizo o
recomendó funcionó. Cerebro agrega esa memoria y cierra el loop:

```
observar → proponer → (aprobar) → ejecutar → REGISTRAR → EVALUAR a 7/14 días
      ↑                                                         │
      └──────────── APRENDIZAJES consolidados ◄─────────────────┘
```

Cada acción queda registrada con su estado previo e hipótesis. A los 7 y 14 días
se compara contra un control (publicaciones hermanas no tocadas, o el movimiento
de los competidores). El veredicto (funcionó / neutra / empeoró) se acumula por
tipo de acción y tipo de producto. Las recomendaciones del día siguiente se
ordenan por ese historial. No hay modelos de ML: con ~70 publicaciones y datos
diarios, alcanza con estadística simple y memoria estructurada.

Restricciones que ordenan el diseño:
- El **título se congela** tras la primera venta → se aprende en las viejas, se
  aplica en publicaciones nuevas y clones.
- **JSON storage**, no Postgres (Regla #5). Todo Cerebro vive en `data/cerebro_<Alias>/`.
- `seo_optimizer.py` no se toca (Regla #1).
- Sin emojis en nada que vaya a ML (Regla #3).
- Costo IA adicional casi nulo: la evaluación es estadística; solo el resumen
  diario y el juez de competidores usan Haiku.

---

## 1. Memoria (Sprint A)

Módulo nuevo: `modules/cerebro.py`. Tres registros por cuenta:

### 1.1 `acciones.json`
Una entrada por cada cambio que el sistema ejecuta o propone.

| Campo | Contenido |
|---|---|
| `id`, `ts`, `item_id` | identificación |
| `tipo` | `precio`, `descripcion`, `ficha`, `respuesta`, `pausa`, `clon`, `publicacion_nueva`, `ads_presupuesto`, `postura` |
| `origen` | `sistema_auto`, `sistema_propuesto`, `usuario` |
| `hipotesis` | texto corto: qué se espera que pase y por qué |
| `estado_previo` | snapshot del ítem al momento de la acción (precio, visitas 7d, conversión 7d, posición, stock, rating) |
| `detalle` | el cambio concreto (precio_antes/después, campos tocados, etc.) |
| `estado` | `pendiente` → `aprobada`/`rechazada` → `aplicada` → `evaluada_7d` → `evaluada_14d` |
| `evaluacion` | se completa en 1.2 |

Se enchufa automáticamente en: Aplicar TODO, repricing, respuestas a preguntas,
pausas desde detector de duplicados, lanzador. Nada requiere que el usuario
registre a mano.

### 1.2 `evaluaciones` (dentro de cada acción)
Job diario `cerebro_evaluar` (04:30 ART, después del snapshot): busca acciones
aplicadas hace 7 y 14 días y calcula:

- Δ visitas/día, Δ conversión, Δ posición, Δ margen/día, Δ unidades/día.
- **Control**: media de las publicaciones hermanas (misma categoría) que no
  recibieron acciones en la ventana. Si no hay hermanas, control = la propia
  publicación en los 14 días previos.
- **Veredicto**: `funciono` (mejora sobre control mayor al ruido histórico de
  esa publicación), `neutra`, `empeoro`. Se guarda también la magnitud.

### 1.3 `aprendizajes.json`
Reglas consolidadas que el sistema escribe solo. Ejemplo:

```json
{
  "clave": "precio.bajar.cortadores",
  "texto": "En cortadores, bajar 3-5% no movió conversión (0/4 casos). Mejorar ficha sí (3/3).",
  "tipo_accion": "precio.bajar",
  "segmento": "categoria:MLA-cortadores",
  "casos": 4, "acierto": 0.0, "confianza": "media",
  "actualizado": "2026-10-15"
}
```

Se recalcula cada vez que cierra una evaluación. Confianza: baja (<3 casos),
media (3–9), alta (≥10). Las recomendaciones leen este archivo (bloque 5).

### 1.4 Snapshot enriquecido
`daily_snapshots` (04:00) suma por publicación: visitas del día
(`/items/{id}/visits/time_window`), ventas del día (órdenes), conversión,
precio, stock, posición en sus 2–3 keywords principales, rating, preguntas sin
responder; y por cada competidor directo confirmado: precio, `available_quantity`,
`status`, posición. Se guarda con retención 180 días.

---

## 2. Competidores (Sprint A/B)

### 2.1 Problema actual
La detección trae "competidores de categoría" (best sellers vía highlights +
búsqueda con las primeras 4 palabras del título), sin verificar que sea el
mismo producto. Repricing los usa a todos por igual → puede bajar precio contra
algo que no compite.

Además, "Enviar a Herramienta ML" (bookmarklet) guarda en memoria del servidor
(`_pending_competitors`, se pierde al reiniciar) y asocia a la cuenta, no a una
publicación.

### 2.2 Diseño: semilla + huella + confirmación

1. **Semilla**: el usuario marca 3–4 competidores por publicación (bookmarklet
   o búsqueda desde la pantalla). Es opcional pero recomendado.
2. **Huella del producto**: a partir de la publicación propia + semilla:
   atributos compartidos de la ficha (marca, modelo, potencia, tipo, tamaño,
   cantidad), banda de precio (mediana ± 40%), palabras presentes en todos /
   ausentes en todos, `catalog_product_id` si existe.
3. **Puntaje de candidatos** (0–1): mismo producto de catálogo (+0.5),
   coincidencia de atributos clave (+0.3), dentro de banda de precio (+0.1),
   keywords (+0.1). Empate o zona gris (0.4–0.7) → juez Haiku con título, ficha
   y foto principal: "¿mismo producto, sustituto o ruido?".
4. **Tres clases**:
   - `directo` — mismo producto. **Único que se usa para precio.**
   - `sustituto` — misma necesidad, otro producto. Sirve para keywords y posición.
   - `ruido` — descartado, no vuelve.
5. **Confirmación**: candidatos con puntaje ≥ 0.5 van a la bandeja. El usuario
   confirma/rechaza con un toque. Cada decisión ajusta los pesos de la huella
   (aprendizaje supervisado mínimo: qué atributos pesan en ese rubro).
6. **Sin semilla**: el sistema opera en modo cauteloso. Todo lo detectado entra
   como candidato; para precio solo se usan `directo` confirmados o automáticos
   con puntaje ≥ 0.85 **y** mismo producto de catálogo. Nunca mueve precio
   contra un candidato sin confirmar.

### 2.3 Bookmarklet
- Persistir en `data/cerebro_<Alias>/competidores.json`.
- Al capturar, proponer automáticamente la publicación propia asociada (por
  similitud); si acierta, un toque; si no, lista corta.
- Capturar también la señal de stock visible en la página ("Última disponible",
  "Últimas N unidades") y el vendedor.

### 2.4 Stock del competidor (inferido, no exacto)
No se conoce el número, pero se detecta lo que importa para la estrategia:
- `available_quantity` + `status` de `/items/{id}` (API pública; ML lo difumina
  en cantidades altas, suele ser exacto cuando queda poco; pausado o 0 = sin
  stock). **Validar el nivel de difuminado con competidores reales antes de
  apoyarse en esta señal.**
- Señal de página capturada por el bookmarklet.
- Patrón de precio en el snapshot: bajas fuertes repetidas = liquidando (no
  perseguir); suba o pausa = se queda sin mercadería (ventana para subir).

Estados derivados por competidor: `normal`, `poco_stock`, `sin_stock`, `liquidando`.

### 2.5 Pantalla (una sola vista por publicación, sin tablas)
- Arriba: la publicación propia (foto, precio, margen, stock en días, postura).
- Fila "Directos confirmados": tarjetas con foto, precio, vendedor, estado de stock.
- Fila "Candidatos" (máx. 5): tarjeta con puntaje y dos botones: *es competidor* /
  *no lo es* (y opción "sustituto").
- "Descartados": colapsado.
- Bandeja general y Telegram solo muestran: "3 candidatos nuevos para Cortador
  Ender Pro" → se confirman desde ahí también.

---

## 3. Precio (Sprint C)

### 3.1 Regla de fondo
Maximizar **margen total** (margen unitario × unidades), nunca debajo del piso
de `costos.json`. Dirección y tamaño de cada movimiento los decide la situación,
no un porcentaje fijo. Reemplaza la regla actual de `competidor × 0.99` / `× 1.02`.

### 3.2 Postura de precio (parámetro por publicación)
Define cómo se para la publicación frente a sus competidores **directos**:

| Postura | Regla |
|---|---|
| `lider` | siempre el más barato entre los directos, hasta donde el margen permita |
| `paridad` | dentro de ±3% del directo más barato; no lo persigue más abajo |
| `premium` | permitido hasta X% arriba del directo más barato (justificado por reputación, reviews, envío) |

El usuario la define en 2 minutos por producto, o el sistema la propone según
margen y posición. El sistema **aprende** qué postura dejó más margen total en
cada SKU y propone cambiarla cuando la evidencia lo justifica.

### 3.3 Situaciones y respuesta

| Situación detectada | Respuesta |
|---|---|
| Directo más barato **y** conversión cayendo | Evaluar menú de palancas (9.6): precio, cupón, cuotas, envío, esperar. Si precio: alcanzar/pasar según postura. Brecha chica → igualar o −1/−2%. Brecha grande → no perseguir hasta el piso (en ML posición ≠ solo precio; dentro del 5% suele alcanzar). Directo con baja reputación o `poco_stock` → esperar. |
| Conversión bien **y** stock corto (días de stock < días de reposición) | Subir 3–8% para estirar stock con más margen. |
| Sin directo, o directo `sin_stock`, o primero con conversión alta | Subir en escalones de 3–5%, observar 5–7 días; si la conversión cae más de lo que compensa el margen, revertir solo. |
| Caída de **tráfico** (no de conversión) | No tocar precio. Derivar a keywords/ficha/Ads (bloque 4). |
| Sobrestock o venta lenta | Descuento/promoción calculada para llegar a la velocidad de venta objetivo. |
| Directo `liquidando` | No perseguir; esperar a que salga. |

### 3.4 Límites de seguridad
- Máx. 5% por movimiento; máx. 12% acumulado por semana en el mismo ítem.
- Mínimo 5 días entre movimientos en la misma publicación (para poder medir).
- Nunca debajo del piso de margen. Margen resultante < umbral → requiere confirmación explícita aunque la acción esté en automático.

### 3.5 Aprendizaje en precio
- Los escalones (3/5/8%) son valores iniciales. Con las evaluaciones se estima la
  elasticidad por SKU y reemplaza los escalones por lo observado.
- Un SKU donde bajar no movió conversión deja de recibir propuestas de baja.
- Un SKU donde subir no bajó conversión sigue subiendo hasta encontrar el techo.
- Arranca en modo **propone → usuario aprueba**. Se promueve a automático por
  tipo de acción cuando acumula ≥10 evaluaciones con ≥70% de acierto (bloque 6).

---

### 3.6 Publicaciones de catálogo (caso especial, más simple)

Gran parte de las publicaciones son de catálogo (producto creado por otro
vendedor o por ML; todos compiten en la misma página por la buy box).

**Competidores**: definidos por ML. `/products/{catalog_product_id}/items`
devuelve todos los vendedores del producto. No aplica huella ni confirmación
(bloque 2); la lista de directos es automática y exacta. `buybox_6h` ya usa
este endpoint.

**Palancas disponibles** (no hay título/fotos/descripción): precio, cuotas sin
interés, envío gratis, Full / tiempo de despacho, stock, reputación.

**Endpoint clave a incorporar**: `GET /items/{id}/price_to_win?siteId=MLA`.
Devuelve:
- `status`: `winning` / `competing` / `sharing_first_place` / `listed`
- `price_to_win`: precio exacto para ganar
- `boosts`: qué falta o sobra frente al ganador que NO es precio (fulfillment,
  free_shipping, installments, reputation, handling_time, stock…)
- ganador actual y competidores que comparten el primer lugar

Con `boosts`, "compito solo por precio" pasa a ser un diagnóstico: si el ganador
gana por Full o cuotas, la respuesta es cambiar esa condición, no bajar precio.
El sistema propone la condición con menor costo de margen.

**Decisión de fondo: ganar la buy box no siempre conviene.** `price_to_win`
dice cuánto cuesta ganar; Cerebro decide si vale la pena:
- Con los snapshots de buy box (cada 6h) + ventas diarias, se estima por
  producto el **premio por ganar**: unidades/día ganando vs. compartiendo vs.
  segundo.
- Posturas en catálogo: `ganar_siempre` (bajar hasta el piso si hace falta),
  `ganar_si_conviene` (solo cuando margen × premio supera quedar segundo),
  `podio` (quedar dentro del X% del ganador, priorizar margen).
- Arranca proponiendo según margen; ajusta la postura cuando mide el premio real.
- Se registra cada cambio de precio/condición como acción (1.1) y se evalúa
  con buy box share y unidades, no solo con visitas.

**Par catálogo + tradicional**: al optar por catálogo la publicación tradicional
sigue existiendo enlazada. Se juegan los dos partidos: precio y condiciones en
catálogo, SEO (título/ficha/descripción) en la tradicional. El detector de
duplicados v2 ya las reconoce como legítimas; Cerebro las trata como un par y
evalúa el resultado del producto sumando ambas.

## 4. Tráfico vs. conversión — *parcialmente definido*

### 4.1 Separar tráfico pago de orgánico (definido)
Las visitas de una publicación pautada no son orgánicas. Si no se separan, el
sistema atribuye a una ficha o descripción lo que compró la pauta.

- Snapshot diario trae por publicación: visitas totales (`/items/{id}/visits`)
  **y** clics de Product Ads del ítem (API de Ads: impresiones, clics, costo,
  ventas directas/indirectas; acceso ya existe en `meli_ads_engine.py`).
- `visitas_organicas = visitas_totales − clics_ads`; conversión orgánica y
  conversión Ads se guardan por separado.
- Acciones de posicionamiento (ficha, descripción, keywords, clon, título en
  nuevas) se evalúan **solo** sobre métricas orgánicas.
- Acciones de Ads (presupuesto, pausa, activación, puja) se registran como
  acción (1.1) y se evalúan con ACOS y ventas atribuidas.
- **Contaminación**: si dentro de la ventana de evaluación de una acción hubo
  otra acción sobre el mismo ítem (cambio de Ads, precio, promo), la evaluación
  se marca `contaminada` y no alimenta `aprendizajes.json`. Mejor "no sé" que
  aprender una mentira.

### 4.2 Demanda del mercado (definido) — umbrales y curva normal *pendiente*
Antes de atribuir una caída a la publicación, verificar si cayó el mercado.

- Snapshot diario guarda por cada keyword principal: posición en tendencias de
  la categoría (`/trends/MLA/{category_id}`) y presencia/orden en autosuggest.
- Índice de demanda por keyword (media móvil 7d vs 28d).
- Atribución: visitas orgánicas caen **y** demanda cae → `mercado` (no accionar
  sobre la publicación; esperar o rotar producto). Visitas caen **y** demanda
  estable/sube → `participacion` (acción: keywords, ficha, Ads, competencia).
- Las evaluaciones (1.2) descuentan el movimiento del mercado: una acción que
  mantuvo ventas en un mercado que cayó 30% cuenta como `funciono`.

Umbrales de alerta y curva normal por publicación: pendiente.

## 9. Promociones, Central de Promociones y cupones (Sprint C/D)

Es una palanca de precio con reglas propias. Entra al mismo motor de margen
total y al mismo registro de acciones.

### 9.1 Fuentes
- `/seller-promotions/users/{user_id}`: campañas de la Central disponibles y
  publicaciones candidatas, con descuento pedido y **cofinanciación** (qué
  porcentaje pone ML y qué porcentaje pone el vendedor).
- `/seller-promotions/items/{item_id}`: promociones activas del ítem.
- Cupones del vendedor, descuento por cantidad, precio Mercado Pago: mismas
  reglas, otras preguntas (9.4).

### 9.2 ¿Conviene participar? (decisión ex ante)
Participar si: margen total (promo + 14 días posteriores) > margen total sin
participar. Se calcula por publicación con:

| Insumo | Origen |
|---|---|
| Línea base de unidades/día | semanas previas, ajustada por estacionalidad y `calendario_comercial` |
| Multiplicador de ventas de la promo | inicial: por categoría y tipo de campaña; luego aprendido de promos propias |
| Margen unitario con descuento | `costos.json`; nunca debajo del piso |
| Resaca posterior | ventas adelantadas que faltan después; aprendida por producto |
| Halo | mejora de ranking/reviews que sigue vendiendo después; aprendida por producto |
| Stock en días | sobrestock → favorece participar; stock corto → desaconseja |
| Etapa del producto | lanzamiento → necesita velocidad y reviews, promo justificada aun con margen flojo |
| Cofinanciación ML | ML paga parte → casi siempre conviene; se detecta y prioriza sola |

Salida: por campaña, lista de publicaciones "participar / no participar / participar
con descuento X", con impacto esperado y confianza. Va a la bandeja como propuesta.

### 9.3 ¿Fue bueno o malo? (evaluación ex post)
Al cerrar la promo y a los 14 días:
- Unidades y margen real (durante + post) vs. **línea base propia** y vs.
  **hermanas que no participaron**.
- Veredicto = margen total incremental contando la resaca, **no** "vendí más".
- Se registra confianza según volumen (productos de pocas ventas/semana → ruido alto).

### 9.4 Qué aprende
- Por producto: multiplicador de promo, resaca, halo.
- Por tipo de campaña (Hot Sale, Oferta del día, Ofertas de la semana, cupón,
  descuento por cantidad): rentabilidad en la cuenta.
- La próxima campaña en la Central llega con la propuesta calibrada por historia
  propia. Requiere varias promos por producto antes de recomendar con confianza alta.

### 9.5 Cupones y descuentos propios
Pregunta distinta: no "cuánto vendo" sino "qué compradores capto que sin cupón
no compraban". Se mide por canje y ventas incrementales en la ventana.
Casos de uso que el sistema propone: reactivar publicación con conversión caída,
mover sobrestock sin tocar precio de lista (cuida buy box y percepción de
precio), lanzamientos. Descuento por cantidad y precio Mercado Pago entran al
mismo motor.

### 9.6 Cupón vs. baja de precio (menú de palancas ante competencia)
Cuando un directo baja precio, el motor **no** propone solo "bajar X%". Evalúa
un menú y elige la palanca más barata en margen que logra el objetivo:

| Palanca | Cuándo conviene | Cuándo no |
|---|---|---|
| Igualar / pasar precio | catálogo (buy box se decide por precio de lista); postura `lider`; competidor sólido con stock | competidor `liquidando` o `poco_stock` |
| Cupón X% por N días | tradicional; baja del competidor parece temporal; probar sensibilidad antes de comprometer precio; tráfico sano + conversión floja; reactivar sin tocar ancla de precio; no dispara repricers ajenos | **catálogo** (no entra en la comparación de buy box) |
| Cuotas sin interés | ticket alto; boost `installments` faltante en `price_to_win` | ticket bajo (costo relativo alto) |
| Envío gratis / Full | boost `free_shipping` / `fulfillment` faltante | ya lo tiene el ítem |
| Descuento por cantidad | sobrestock; consumibles | producto de compra única |
| Esperar | competidor `liquidando` / `poco_stock` / baja reputación | conversión cayendo fuerte |

Diferencias de fondo: la baja de precio es pública y universal (la ven todos
los compradores y los repricers de la competencia) y cuesta revertirla; el
cupón es selectivo, no mueve el precio de lista y se apaga sin rastro.
Las evaluaciones enseñan qué palanca funciona en cada producto.

**Carritos abandonados**: ML no expone al vendedor carritos abandonados (ni API
ni panel) según lo conocido; ML envía sus propios recordatorios. Verificar al
construir; no diseñar nada apoyado en eso. Sustituto: "visitas sin compra"
(tráfico orgánico normal + conversión bajo su normal) → momento de cupón visible
en la publicación.

### 9.7 Advertencia
ML no da contrafáctico: la línea base es una estimación. El sistema expresa la
confianza de cada veredicto y no generaliza con un solo caso.

## 5. Cierre del loop en recomendaciones — *pendiente de definir*
(`top_acciones` y Veredicto leen `aprendizajes.json`; impacto esperado por acción)

## 6. Autonomía graduada y precisión del sistema — *pendiente de definir*
(niveles: automático / propuesto / solo alerta; promoción por evidencia; indicador de precisión)

## 7. Bandeja única + Telegram — *pendiente de definir*
(una sola cola de propuestas; aprobar en Telegram = aprobar en ml-system)

## 8. Laboratorio de títulos (publicaciones nuevas y clones) — *pendiente de definir*

---

## 10. Variables de contexto que entran al motor

Variables que cambian decisiones y que ningún bloque anterior tenía.
(Capacidad operativa / reputación como freno: **excluida por decisión del
usuario** — el objetivo es crecer; queda solo la alerta de reputación existente,
que avisa y no toca ninguna decisión.)

### 10.1 Umbrales de precio de ML
El margen no es lineal con el precio: umbral de envío gratis obligatorio (el
vendedor paga parte del envío), cargo fijo por unidad debajo de cierto precio,
comisión Clásica vs Premium, costo de cuotas. Bajar 3% puede costar 10 puntos de
margen si cruza un umbral; subir 2% puede regalar margen si lo cruza para arriba.
- Fuente: `config/fees.json` + reglas de la skill `costos-importacion`.
- El motor de precio (3) y el de promos (9) calculan el margen **después** de
  umbrales y proponen siempre precios del lado correcto del escalón.
- Se registran los umbrales vigentes con fecha; cuando ML los cambia, se actualizan.

### 10.2 Costo de reposición, no costo histórico
Con dólar e inflación, vender con margen sobre lo que se pagó hace meses puede
ser perder contra lo que cuesta volver a traerlo.
- Piso de margen calculado sobre **costo de reposición estimado**: FOB actual,
  dólar del día, tarifa de flete vigente (motor de `costos-importacion`).
- Alerta "no se recompra a este precio" cuando el precio de venta no cubre la
  reposición aunque tenga margen histórico.
- Conecta con caja inmediata vs margen real y con el módulo 10 (reposición China).

### 10.3 Capital inmovilizado y rotación
Cada decisión de precio/promo es también financiera.
- Insumos por SKU: valor en stock (a costo de reposición), días de rotación,
  fecha estimada de la próxima importación.
- Un SKU con rotación muy lenta justifica promo aunque el margen sea flojo
  (el capital parado también cuesta). Entra como peso en 9.2 y en 3.3
  (sobrestock).
- Vista: capital total inmovilizado y cuánto libera cada propuesta.

### 10.4 Palancas de conversión que no son texto ni precio
- **Fotos**: cantidad, primera foto vs. la de los directos, fondo blanco, video.
  Hoy el sistema solo cuenta fotos; pasa a comparar contra directos y proponer.
- **Variantes**: color/talle sin stock mata conversión y no aparece en ningún
  análisis de precio. Snapshot por variante; alerta "variante agotada en
  publicación con tráfico".
- Ambas se registran como acciones (1.1) y se evalúan sobre conversión orgánica.

### 10.5 Devoluciones y reclamos en el margen real
Margen real por SKU = margen − costo de devoluciones y reclamos (tasa × costo
unitario de logística inversa + producto perdido). Un SKU que vende mucho pero
vuelve seguido no es el que parece. Entra al ranking de rentabilidad y a 9.2.

### 10.6 Menores (registradas, no prioritarias)
- Estacionalidad: cubierta por `calendario_comercial`.
- Costos de Full (almacenamiento, stock envejecido): relevante si se usa Full en
  volumen; se agrega cuando corresponda.

## 11. Semáforo de portafolio (qué reponer, qué no, qué defender)

Decisión a nivel producto, no publicación. Se apoya en `stock_rentabilidad.py`
y `reposicion.py` y en todas las variables del bloque 10. Recalculado a diario.

### 11.1 Estados

| Estado | Criterio | Qué hace el sistema |
|---|---|---|
| **Estrella** | Líder sostenido (buy box o top posiciones), margen real sano sobre costo de reposición, rotación alta, demanda estable o creciente | **Defender el liderazgo**: postura `lider` / `ganar_siempre`, prioridad máxima en alertas de competencia, stock de seguridad más alto, aviso de reposición anticipado (un estrella sin stock es la forma más cara de perder posición) |
| **Recomendado** | Margen real positivo sobre reposición, rotación aceptable, demanda estable | Propone **reponer** y con qué cantidad (velocidad × tránsito + seguridad) |
| **En observación** | Alguna señal en contra: margen apretándose, demanda bajando, rating cayendo, competencia intensificándose | No reponer todavía; explicita qué tiene que pasar para subir o bajar de estado |
| **No recomendable** | No se recompra al precio de reposición, rotación lenta, demanda en caída o devoluciones altas | **No reponer** + plan de salida: liquidar stock con promo/cupón al mejor margen posible, pausar duplicados |
| **Nuevo** | Sin datos suficientes (ventana mínima no cumplida) | No se clasifica; se monitorea como lanzamiento (bloque 8) |

Un producto con varias publicaciones (catálogo + tradicional, variantes) se
clasifica una sola vez sumando todas.

### 11.2 Señales de entrada
Margen real (10.2, 10.5), rotación y capital (10.3), demanda de mercado (4.2),
posición / buy box share (3.6), rating y reclamos, intensidad competitiva
(cantidad de directos, frecuencia de bajas), tendencia de conversión orgánica.

### 11.3 Qué aprende
- Los umbrales entre estados no son fijos. Cada cambio de estado guarda qué
  señales lo anticiparon; el sistema aprende cuáles predicen mejor en la cuenta
  (ej. caída de rating anticipa caída de ventas semanas antes que el precio) y
  ajusta umbrales.
- Cada clasificación es una acción evaluable: un "recomendado" que se pinchó, o
  un "no recomendable" que después vendió, se registra como error y calibra.

### 11.4 Salida
- Vista de portafolio en Cerebro: productos por estado, con margen real, días de
  stock, capital inmovilizado y la razón principal del estado.
- Propuestas a la bandeja: "reponer X unidades de Y", "no reponer Z, liquidar
  con cupón 15%", "Estrella W: competidor bajó, defender".
- Alimenta el plan de reposición desde China (módulo 10) con cantidades por SKU.

## 12. Cimientos — la ingenieria antes de las funcionalidades

Decision del usuario (2026-09-07): antes de seguir con el Sprint C, un sprint de
cimientos. El razonamiento es simple: en un solo dia de trabajo aparecieron
diecisiete correcciones sin buscarlas — una formula que prometia 5 millones de
pesos que no existian, el repricing leyendo su configuracion de un lado mientras
el panel la escribia en otro, la buy box calculada asumiendo que el mas barato
gana, snapshots imposibles de comparar, un endpoint abierto, un hash de
proteccion que fallaba siempre. Ese ritmo de hallazgos no es mala suerte: es lo
que pasa cuando nada verifica nada, y cada modulo nuevo agrega superficie para
que se repita.

Un sistema que mide mal y recomienda con seguridad es peor que no tener sistema,
porque hace ejecutar el error mas rapido. Lo que hace superadora a una
herramienta asi no es la cantidad de cosas que hace: es que el dato sea cierto y
el criterio honesto.

### 12.1 Una sola fuente de verdad
Hoy la misma informacion vive en `stock_<alias>.json`, `posiciones_<alias>.json`,
`competencia_<alias>.json`, `monitor_evolucion.json` y ahora los snapshots de
Cerebro, escrita por modulos distintos con formatos distintos. De ahi salieron
las correcciones 7, 8, 9 y 11. Cerebro es el candidato natural a ser la unica
serie temporal: todos leen de ahi y nadie mas guarda su propia version. Meta:
ningun modulo abre archivos con `open()`; todo pasa por `core/db_storage`.

**Estado: hecho (2026-09-07).** Se ruteo por `core/db_storage` todo lo que era
estado del sistema y se escribia con `open()` al disco:

| Modulo | Que se perdia en cada deploy |
|---|---|
| `conv_history.py` | El historial de conversion por item — la base para detectar tendencias |
| `baseline_capture.py` | El baseline de cada optimizacion, que alimenta al Veredicto IA y al backtest |
| `permisos_checker.py` | El cache de permisos de la API |
| `detector_duplicados.py` | El cache de attributes: se re-consultaba ML de cero cada vez |
| `meli_ads_engine.py` | `actions.json`: que acciones de Ads aprobo o ejecuto el usuario |
| `core/scheduler_manager.py` | El historial de corridas de los crons **y los overrides**: un job que el usuario pauso volvia a encenderse solo en el siguiente deploy |
| `dashboard.py`, `historial.py`, `multicuenta.py`, `optimizador_publicaciones.py`, `lanzador_productos.py` | Leian del disco archivos que la web escribe en el kv_store: veian vacio o desactualizado |

Quedan con `open()` a proposito: `core/db_storage.py` (es la capa) y los CSV de
entrada y los export de `meli_ads_engine.py` (son archivos que entran y salen,
no estado).

Auditoria repetible: ningun modulo debe abrir archivos de datos por su cuenta.

### 12.2 Un solo motor de decision
Hay logica de precio en `repricing.py`, `pricing_strategy.py`,
`top_acciones_diarias.py` y `web/app.py`. Cuatro lugares que pueden
contradecirse y ninguno sabe de los otros. Debe quedar uno, y los demas
llamarlo.

**Estado: hecho (2026-09-07).** Nuevo `modules/precio_motor.py`: el unico lugar
donde se define que es el margen, cual es el piso y que pasa al cruzar un umbral
de ML. Lo usan `top_acciones_diarias` y `repricing`; `pricing_strategy` queda
como analisis avanzado sobre las mismas constantes.

Lo que estaba mal repartido:

| Donde | Que hacia por su cuenta |
|---|---|
| `repricing.py` | competidor x 0.99 / +2%, con un piso que solo cubria costo + comision: dejaba pasar precios de margen cero |
| `top_acciones_diarias.py` | bajaba 8% fijo con `MARGEN_MIN = -0.10`, es decir, podia **proponer vender perdiendo 10% en cada venta** |
| `pricing_strategy.py` | el unico que conocia el umbral de envio gratis y el costo de cuotas — y nadie mas lo usaba |
| `web/app.py` | aplicaba los cambios sin volver a validar nada |

Ahora, en un solo lugar: margen unitario y porcentual, piso de precio con margen
minimo del 10%, deteccion de cruce del umbral de envio gratis ($33.000), e
impacto estimado con freno de realismo.

Lo mas util para decidir es `unidades_para_compensar()`: cuantas unidades mas
hay que vender para que una baja no sea una perdida. No es una prediccion, es
aritmetica exacta sobre el margen, y hasta ahora ningun modulo la hacia.

Efecto en el caso real de la Faja Reductora (bajar 8%, de $29.000 a $26.680):
- piso de precio: de $10.335 (margen -10%) a $13.935 (margen 10%)
- impacto prometido: de $30.578 a $6.692 por mes
- y ahora dice lo que importa: "necesitas vender 16% mas solo para no perder
  plata" y "la conversion tendria que mejorar 205% para llegar al promedio"

#### Piso de margen y palanca de cuotas (definido por el usuario, 2026-09-08)

**Piso: 15%.** No se propone ninguna baja que deje el margen debajo de 15%.
Toda sugerencia muestra el margen actual y el margen resultante, en la
descripcion y en el snapshot.

**Reducir cuotas antes que bajar el precio.** Bajar el precio es publico,
universal e incomodo de revertir: lo ven todos los compradores y los repricers
de la competencia. Reducir las cuotas sin interes recupera margen sin mover el
precio de lista y solo afecta al segmento que realmente las usaba.

La cuenta clave: bajar de 12 a 6 cuotas NO molesta a quien ya compraba en 6.
Solo afecta a los que necesitaban entre 7 y 12. Si ese segmento es chico, es
margen casi gratis. El dato no se estima: sale de las ordenes reales
(`payments[0].installments`), agrupado en buckets 1 / 2-3 / 4-6 / 7-12 / 13+.

Riesgo por porcentaje de compradores afectados: <=3% muy bajo, <=8% bajo,
<=20% medio, mas alto. Solo se recomiendan los pasos de riesgo bajo o muy bajo.

El ahorro se traduce a "descuento equivalente" para poder compararlo de frente
con una baja de precio, y `menu_de_palancas()` las pone lado a lado y elige la
que consigue el objetivo costando menos margen.

Esta logica existia dentro de `pricing_strategy`, encerrada en una pantalla que
nada mas usaba; ahora vive en el motor y entra en cada propuesta de precio.

**Pendiente de calibrar (correccion 25):** el costo de financiamiento se modela
como `(cuotas promedio ponderado - 1) x 0.9%`, un valor heredado. Si ML en
realidad cobra por OFRECER cuotas sin interes y no en proporcion a las que cada
comprador usa, el ahorro real de reducirlas es bastante mayor que el que hoy
muestra el sistema. Se puede calibrar contra el `fee_rate` real que
`stock_rentabilidad` ya calcula desde las ordenes.

### 12.3 El sistema tiene que dudar de si mismo
Un impacto de 5 millones sobre 1.072 visitas tendria que haber disparado una
alarma automatica, no llegar a la pantalla del usuario. Cada numero que sale a
pantalla pasa por una validacion de sensatez: si el resultado es absurdo se
marca, se topea y se explica; nunca se publica como si fuera cierto.

Primer caso implementado: el impacto de pausar duplicados no puede superar el
margen que el producto ya genera en el mes (no se puede mas que duplicar), la
conversion se capea al 12% (por encima habla de la medicion, no del producto) y
sin costo cargado se dice que falta el dato en vez de estimar con el precio.

### 12.4 Tests de las reglas de negocio, no del codigo
`tests/test_reglas_negocio.py`: casos con numeros concretos y un porque. Cubre
el impacto de duplicados, el margen unitario y el veredicto de Cerebro. El
primer dia ya encontro un agujero real: la deteccion de contaminacion solo
miraba la ventana posterior, asi que una accion caida en la ventana previa
—que mueve la linea de base— pasaba desapercibida y generaba un aprendizaje
falso. Corregido a la ventana completa.

Si un cambio futuro rompe una formula, estos tests tienen que fallar.

### 12.5 Trazabilidad: si no se puede explicar, no se muestra
Toda recomendacion viaja con el detalle de como se calculo. Implementado en el
backtest y en el impacto de duplicados (`impacto_detalle`). Debe ser la norma.

### 12.6 Los costos reales en el centro
Reposicion, devoluciones y umbrales de comision y envio (bloques 10.1, 10.2 y
10.5) no son un refinamiento: sin ellos, optimizar el precio es optimizar una
ficcion prolija.

### 12.7 Saber donde no meterse
Con 68 publicaciones, el resultado esta en unos 10 SKU. Un sistema que trata las
68 por igual reparte la atencion donde no rinde. La version superadora no es la
que optimiza todo: es la que dice "aca no hay nada que ganar, anda a otro lado".
Se apoya en el semaforo de portafolio (bloque 11) y en la confianza de los
aprendizajes (1.3).

## Correcciones detectadas (checklist)

Cosas encontradas al revisar el código que no funcionan bien o son riesgosas.
Se corrigen en el sprint indicado. Se van agregando a medida que aparecen.

| # | Problema | Dónde | Impacto | Sprint |
|---|---|---|---|---|
| 1 | Competidores capturados por el bookmarklet se guardan en memoria del proceso (`_pending_competitors`) → se pierden en cada reinicio de Render | `web/app.py` `api_capturar_competidor` | Trabajo manual perdido | A |
| 2 | Competidor capturado se asocia a la cuenta, no a una publicación propia | idem | No sirve para precio por ítem | A |
| 3 | Detección de competidores no verifica que sea el mismo producto (categoría + primeras 4 palabras del título) y repricing los usa a todos | `modules/analisis_competencia.py`, `modules/repricing.py` | Riesgo de bajar precio contra un producto que no compite | A/C |
| 4 | Regla de repricing fija: competidor −1% / +2% sin competidor. No considera margen total, stock propio, brecha ni stock del competidor | `modules/repricing.py` | Deja margen en la mesa o persigue de más | C |
| 5 | `buybox_6h` no usa `price_to_win` (estado, precio para ganar, boosts); solo mira quién está primero | `web/app.py` `_job_buybox_check` | Sin diagnóstico de por qué se pierde la buy box | C |
| 6 | `buybox_6h` procesa solo las primeras 50 publicaciones por cuenta | idem | Publicaciones de catálogo fuera del monitoreo si hay más de 50 | A |
| 7 | `repricing.py` lee y escribe `config/repricing.json` con `open()` directo, sin pasar por `core/db_storage`. `web/app.py` lo hace con `db_load`/`db_save` (kv_store en Postgres) | `modules/repricing.py` `_load_config`/`_save_config` | Las reglas guardadas desde la UI son invisibles para el cron `repricing_hourly` y viceversa; en Render el disco es efímero, la config del módulo se pierde en cada deploy. Bloqueante para todo el bloque 3 | A |
| 8 | Historial de cambios de precio (`data/acciones_automaticas_<alias>.json`) también con `open()` directo | `modules/repricing.py` `_log_price_change`, `_calculate_24h_drop` | El breaker "bajó más de 5% en 24h" nunca dispara tras un reinicio, y Cerebro perdería el registro de acciones de precio | A |
| 9 | `_save_report` de competencia escribe a disco, no al kv_store | `modules/analisis_competencia.py` | El optimizador (Módulo 3) puede leer un reporte inexistente o viejo en producción | A |
| 10 | El ganador de buy box se asume como `results[0]` de `/products/{cpid}/items`, sin orden ni campo de ganador. `repricing` busca `winner_item_id` / `catalog_winner`, que ese endpoint no devuelve, y cae a "el más barato gana" | `web/app.py` `_job_buybox_check`, `modules/repricing.py` `_get_competitor_info` | Alertas de buy box perdida falsas o ausentes; en ML el ganador no se define solo por precio (Full, reputación, cuotas). Se resuelve junto con la corrección 5 usando `price_to_win` | A |
| 11 | El snapshot diario guarda `visitas_7d` (ventana móvil) y `ventas_total` (acumulado histórico), y hace rolling de 30 snapshots | `web/app.py` `_capturar_snapshot_simple`, `_job_daily_snapshots` | No se pueden derivar visitas/día ni ventas/día, que es exactamente lo que Cerebro necesita para evaluar a 7 y 14 días. Retención insuficiente (se pide 180 días) | A |
| 12 | El snapshot solo cubre publicaciones del monitor con optimización de 1 a 90 días | idem | El resto del catálogo no tiene serie histórica, así que no hay hermanas de control para el bloque 1.2 | A |
| 13 | La purga de cuentas borra archivos con `glob` del filesystem | `web/app.py` `_job_purga_cuentas_pausadas` | En Render los datos viven en `kv_store`; la data de una cuenta purgada queda para siempre | B |
| 14 | `GET /api/pending-competidores` vacía la cola al leer | `web/app.py` | Si la respuesta se corta, los competidores capturados se pierden. Queda resuelto al persistir (corrección 1) | A |
| 15 | `/api/capturar-competidor` es público: CORS `*` y sin token | idem | Cualquiera que conozca la URL y el alias puede inyectar competidores falsos que después mueven precio | A |
| 16 | El hash MD5 de `seo_optimizer.py` en la Regla #1 y en `docs/ARQUITECTURA_OPTIMIZAR_IA.md` es `74783469...`; el archivo real es `0389b93a8c4ff11c8eaa97327a6f54c1` (cambió en commits legítimos de julio) | Regla #1 | La verificación previa a cada push falla siempre, y una regla que siempre falla deja de proteger. Requiere decisión del usuario para re-basar el hash | A |
| 17 | `seo_optimizer.py` usa f-strings anidadas (PEP 701) y solo compila en Python 3.12+. Render corre 3.12.7 por `runtime.txt`, asi que produccion esta bien, pero cualquier entorno con 3.10 u 3.11 falla al importar el modulo | `modules/seo_optimizer.py` linea 2519 | El sistema no arranca fuera de 3.12 y nada lo advierte: el error aparece como SyntaxError en un import lejano (`stock_rentabilidad`), que no dice nada sobre la causa. Encontrado al correr las pruebas del Sprint B | B |
| 18 | El impacto de pausar duplicados se calculaba como visitas x conversion x PRECIO: usaba facturacion en vez de margen, asumia que el 100% de las visitas se transfieren y no descontaba lo que los duplicados ya venden | `modules/detector_duplicados.py` `_calcular_impacto_monetario` | Prometia $5.071.784/mes por pausar un duplicado del Cortador; el numero real es ~$362.000. El Top 3 se ordena por impacto, asi que priorizaba mal. **Resuelto** | A |
| 19 | La deteccion de contaminacion solo miraba la ventana posterior a la accion | `modules/cerebro.py` `evaluar_accion` | Una accion en la ventana previa mueve la linea de base y generaba un aprendizaje falso. Lo encontro un test. **Resuelto** | A |
| 20 | `meli_ads_connector` consulta `/advertising/product_ads/items/{id}` para todas las publicaciones y ML devuelve 404 en las que no estan en campana | `modules/meli_ads_engine.py` | Cientos de llamadas inutiles por corrida y logs tan ruidosos que tapan un error real | B |
| 21 | Persistencia partida: doce modulos escribian estado del sistema con `open()` al disco mientras el panel usaba el kv_store | `conv_history`, `baseline_capture`, `permisos_checker`, `detector_duplicados`, `meli_ads_engine`, `scheduler_manager`, y cinco modulos CLI | En Render el disco es efimero: se perdia en cada deploy. Lo mas grave, los overrides del scheduler (un cron pausado por el usuario se re-encendia solo) y el baseline de las optimizaciones. **Resuelto** | A |
| 22 | `top_acciones_diarias` usaba `MARGEN_MIN = -0.10` como piso de precio | `modules/top_acciones_diarias.py` | Permitia proponer bajas que dejaban el margen en -10%: el sistema podia recomendar vender perdiendo plata en cada venta. **Resuelto**, ahora el piso es el del motor unico (10%) | A |
| 23 | El impacto de una baja de precio asumia que la conversion saltaba sola al promedio del catalogo y no descontaba lo que el item ya gana | idem | En la Faja Reductora prometia $30.578/mes cuando exigia triplicar la conversion. **Resuelto**: $6.692 con freno de realismo | A |
| 24 | El umbral de envio gratis de ML solo lo conocia `pricing_strategy` | `repricing`, `top_acciones_diarias` | Se podian proponer bajas que cruzaban el umbral sin avisar que el margen se mueve de golpe, no de a poco. **Resuelto** | A |
| 25 | El costo de las cuotas sin interes se modela como (cuotas promedio - 1) x 0.9%, un valor heredado y sin validar | `modules/precio_motor.py` `COSTO_POR_CUOTA` | Determina si reducir cuotas vale $300 o $3.000 por mes, o sea si la palanca sirve o no. Si ML cobra por OFRECER cuotas y no por las que se usan, el ahorro real es mucho mayor. Calibrable contra el `fee_rate` real de las ordenes | B |

### Estado al cierre del Sprint A

| # | Estado | Donde quedo |
|---|---|---|
| 1 | Resuelta | `data/cerebro_<Alias>/competidores.json` via `core/db_storage`; sobrevive reinicios y deploys |
| 2 | Resuelta | Clave `(item_propio, competidor)`; el bookmarklet propone la publicacion propia por similitud |
| 3 | Pendiente (C) | La huella y el juez Haiku son del bloque 2.2; ya existe `puntuar_candidato()` con los pesos definidos |
| 4 | Pendiente (C) | Motor de margen total con postura — bloque 3 completo |
| 5 | Resuelta en el monitoreo | `buybox_6h` ya usa `price_to_win` (estado, precio para ganar y `boosts`); falta que el motor de precio lo consuma para decidir (3.6) |
| 6 | Resuelta | Sin tope: recorre todas las publicaciones activas y filtra las de catalogo |
| 7 | Resuelta | `repricing.py` ruteado por `core/db_storage` |
| 8 | Resuelta | Historial de precios por `db_storage`, rolling 1000 entradas |
| 9 | Resuelta | `_save_report` por `db_storage` |
| 10 | Resuelta | El ganador sale de `price_to_win`, no de asumir que el primero de la lista gana |
| 11 | Resuelta | Serie diaria real (visitas del dia, unidades del dia), retencion 180 dias |
| 12 | Resuelta | Cubre todas las publicaciones activas, no solo las del monitor |
| 13 | Pendiente (B) | Purga de cuentas en `kv_store` |
| 14 | Resuelta | `GET /api/pending-competidores` dejo de vaciar la cola |
| 15 | Resuelta | Token opcional `CEREBRO_BOOKMARKLET_TOKEN`; sin token definido sigue abierto y avisa en el log |
| 16 | Resuelta | Hash re-basado a `0389b93a8c4ff11c8eaa97327a6f54c1` en `docs/ARQUITECTURA_OPTIMIZAR_IA.md`. Falta actualizarlo tambien en la skill `meli-reglas` |

Lo construido en el Sprint A:

- `modules/cerebro.py` — memoria completa: registro de acciones (1.1), evaluacion
  a 7 y 14 dias contra control de hermanas (1.2), aprendizajes consolidados
  (1.3), snapshots (1.4) y competidores con clases y puntaje (2.x).
- `modules/cerebro_snapshot.py` — captura diaria contra la API: visitas del dia,
  clics de Ads, organicas, unidades, precio, stock, posicion y competidores
  directos. Honestidad del dato de Ads: ML no da metricas por item, solo por
  campania; con un item la cifra es exacta (`ads_scope='item'`), con varios se
  prorratea y queda marcado `ads_scope='campaign'`.
- Job `cerebro_evaluar` a las 04:30 ART, despues del snapshot de las 04:00.
- Hooks de registro en: cambio de precio manual, Aplicar TODO (titulo,
  descripcion y ficha por separado), respuesta a preguntas, pausa de duplicados
  y repricing automatico.
- API: `/api/cerebro/bandeja`, `/acciones`, `/aprendizajes`, `/serie`,
  `/resumen`, `/evaluar`, y `/competidores` con clasificar, asociar y eliminar.

Lo que falta para cerrar el loop (Sprint B en adelante): la pantalla de Cerebro,
el bot de Telegram sobre la misma bandeja, y que `top_acciones` lea
`aprendizajes.json` para ordenar por historial propio (bloque 5).

## Sprints

| Sprint | Contenido | Estado |
|---|---|---|
| A | Memoria (1.1–1.4) + competidores persistentes y huella (2.2–2.4) | codigo listo, falta deploy y validacion |
| B | Bandeja única, sección Cerebro en UI (2.5 + 7), bot Telegram | pendiente |
| C | Motor de precio con postura (3) + cierre del loop (5) + tráfico vs conversión (4) + promociones ex ante (9.2) | pendiente |
| D | Disparadores de competencia, laboratorio de títulos (8), precisión del sistema (6), evaluación de promos y cupones (9.3–9.5), semáforo de portafolio (11) | pendiente |

Flujo de trabajo: Claude edita y pushea a `main` desde la sesión (repo agregado
como fuente); Render auto-deploya; el usuario valida con el checklist de
`meli-validar`. Regla #4 (protocolo de 60s) se respeta en cada push.
