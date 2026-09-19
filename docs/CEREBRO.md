# CEREBRO — Capa de aprendizaje del Sistema ML

## Proposito

Escrito con Guille el 2026-09-08. Va primero a proposito: el resto del documento
explica la mecanica, y la mecanica sin el para que se termina desviando.

**Lo que se busca no es un tablero, es un sistema que se corrija solo.** Que
aprenda de sus propios resultados y mejore lo que propone, con impacto real y no
como ensayo. El ciclo completo, sin cortes:

    detectar -> proponer -> aplicar -> MEDIR -> APRENDER -> proponer mejor

La parte que faltaba, y por la que existe Cerebro, son las dos ultimas. El
sistema ya observaba y ya actuaba; nadie le decia nunca si lo que hizo sirvio.

### Los criterios que no se negocian

**1. No mentir.** Un numero que no se puede explicar no sale a pantalla. Si la
evidencia no alcanza, se dice "no se" en vez de inventar una causa. Un sistema
que mide mal y recomienda con seguridad es peor que no tener sistema, porque
hace ejecutar el error mas rapido. Todo lo que se muestra tiene que ser
auditable: de donde sale cada cifra y contra que se comparo.

**2. El margen primero.** Piso duro de 15%: no se propone nada que lo perfore.
Toda sugerencia de baja muestra el margen actual y el resultante, y dice cuanto
mas hay que vender para no perder plata. Crecer en unidades perdiendo margen no
es crecer.

**3. Las palancas baratas antes que el precio.** Bajar el precio es publico,
universal y dificil de revertir: lo ven todos los compradores y los repricers de
la competencia. Antes van las keywords y la ficha (no cuestan nada), y despues
las cuotas y las condiciones. El precio es el ultimo recurso, no el primero.

**4. Autosuggest es la fuente de verdad de la demanda.** Es lo unico que dice
que busca la gente de verdad. Todo analisis y toda correccion se hacen contra el
universo de busquedas reales del producto, nunca contra lo que uno supone que se
busca.

**5. La causa, no el sintoma.** Saber que un competidor gana no sirve: hay que
saber por que. Si es por keywords, el precio no lo arregla. Si es por
condiciones, tampoco. Y el diagnostico tambien se evalua despues, para saber si
acierta.

**6. Saber donde NO meterse.** Con unas 70 publicaciones, el resultado esta en
unos 10 SKU. Un sistema que las trata a todas por igual reparte la atencion
donde no rinde. Decir "aca no hay nada que ganar" vale tanto como encontrar una
oportunidad.

**7. Los cimientos antes que las funcionalidades.** Un dato mal medido envenena
todo lo que se construya arriba. Primero que el dato sea cierto y el criterio
honesto; despues, mas capacidades.

**8. El usuario decide lo que importa.** El precio arranca en propone-y-apruebo.
La autonomia se gana con evidencia: un tipo de accion pasa a automatico recien
con 10 evaluaciones y 70% de acierto, y el usuario puede ver esos numeros.

**9. Una sola bandeja.** Telegram y el panel son dos puertas al mismo estado.
Aprobar en uno es aprobar en el otro. Nunca dos colas que puedan contradecirse.

### Como desempatar cuando hay dudas

- Entre prometer de mas y prometer de menos: **de menos**.
- Entre una funcionalidad nueva y un dato mal medido: **el dato**.
- Entre bajar el precio y cualquier otra palanca: **la otra palanca**.
- Entre avisar y no molestar: al telefono solo lo que se pudre si no se mira
  hoy; el resto al resumen.
- Entre una regla linda y la evidencia propia: **la evidencia propia**.
- Entre actuar sin datos y esperar: **esperar**, y decir que falta para decidir.

### El objetivo de negocio

Crecer. Por eso se excluyo explicitamente frenar el volumen por capacidad
operativa: queda la alerta de reputacion, que avisa y no decide.

---


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

### 2.0 Cambio de contexto (2026-09-08): la deteccion automatica ya no existe

Verificado: `/sites/MLA/search` devuelve 403 tambien desde IP residencial y
navegador real. ML lo desactivo para trafico programatico; no es una caida
temporal. Con eso se cae la deteccion automatica de competidores y la posicion
por keyword de las publicaciones que no son de catalogo.

Lo que queda en pie:
- **Catalogo**: `/products/{catalog_product_id}/items` da la lista exacta de
  vendedores del mismo producto, y `price_to_win` el estado y los boosts. Es la
  fuente mas confiable que hay, y no depende de la busqueda.
- **Captura manual** (bookmarklet): el usuario marca al competidor desde la
  pagina de ML. Ahora persiste y queda asociado a una publicacion propia.
- **Seguimiento** de un competidor ya conocido: `/items/{id}` da precio,
  `available_quantity` y `status` sin restricciones.
- **Autosuggest** y `/trends`: siguen sirviendo para keywords y demanda, no para
  identificar competidores.

Consecuencia para el diseno: la semilla del bloque 2.2 deja de ser "opcional
pero recomendada" y pasa a ser **la unica via** para publicaciones que no son de
catalogo. La huella y el puntaje siguen valiendo, pero para clasificar lo que el
usuario captura y lo que aparece en catalogo, no para descubrir de cero.

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

## 2.6 Diagnostico diferencial: POR QUE te esta ganando (implementado)

Saber que un competidor te pasa no sirve por si solo: lo que cambia la decision
es la causa, porque bajar el precio cuando el problema son las keywords es tirar
margen sin arreglar nada.

El embudo tiene dos etapas y cada una falla distinto:

    ¿ME ENCUENTRAN?  -> posicion en la busqueda -> keywords, relevancia, ranking
    ¿ME ELIGEN?      -> conversion              -> precio, fotos, envio, cuotas,
                                                   reputacion, reviews

De ahi salen las cuatro causas, con las senales que las distinguen:

| Senal | Causa | Respuesta |
|---|---|---|
| Visitas organicas caen **y** posicion cae | `keywords` — no te encuentran | Terminos que su titulo usa y el tuyo no; completar ficha. **No tocar precio** |
| Visitas estables **y** conversion cae, con el 5%+ mas barato | `precio` — te encuentran pero eligen al mas barato | Primero cuotas, despues precio |
| Visitas estables **y** conversion cae, con precios parecidos | `condiciones` — Full, cuotas, fotos, reputacion | Igualar la condicion que falta |
| Cae todo pero la demanda del rubro cae parecido | `mercado` | Esperar: no es tuyo |
| Nada de lo anterior | `sin_causa_clara` | No tocar nada. **No inventar una causa** |

Las acciones se ordenan por lo que cuestan en margen: primero lo que no cuesta
(keywords, fotos, ficha), despues lo barato (cuotas, condiciones), y el precio
al final — es publico, lo ven los repricers ajenos y cuesta revertirlo. Las
acciones de precio pasan por `precio_motor`, asi que respetan el piso de 15% y
dicen cuanto mas hay que vender para no perder plata.

**Que aprende de esto.** Cada correccion se registra en Cerebro con el
diagnostico como hipotesis (`registrar_para_aprender`). A los 7 y 14 dias no se
evalua solo "funciono el cambio" sino **si acerto el diagnostico**. Con los casos
acumulados el sistema aprende que causa suele ser la correcta en cada rubro, y
deja de proponer respuestas que en ese producto nunca sirvieron.

Modulo: `modules/competencia_diagnostico.py`. Tests: `tests/test_competencia_diagnostico.py`.

Pendiente para cerrarlo: la captura de resultados de busqueda desde el
bookmarklet (que alimenta la comparacion con datos frescos) y el disparador
automatico del bloque 4.2, que avisa cuando conviene mirar una keyword.

## 2.7 Defensa de la publicacion — autosuggest en el centro (implementado)

Cerebro detecta y aprende; este bloque responde la otra mitad: que hacer para
que no te ganen. Una publicacion no se defiende con una sola cosa: tiene varias
puertas, y el competidor entra por la que quedo abierta.

**Autosuggest es el centro.** Es la unica fuente que quedo con demanda real —
las frases que la gente efectivamente escribe en ML, ordenadas por popularidad.
Todo el analisis se hace contra el universo de busquedas reales del producto y
no contra lo que uno cree que se busca. Como autosuggest devuelve pocas frases
por consulta, se usan varias semillas (una por familia de terminos) y se unen.

### Seis dimensiones

| Dimension | Peso | Que mide |
|---|---|---|
| Cobertura | 30 | Por cuantas busquedas reales te encuentran |
| Concentracion | 15 | Si dependes de una sola keyword, sos fragil |
| Ficha | 20 | Atributos completos: te mete en los filtros |
| Contenido | 15 | Fotos y video contra los competidores |
| Condiciones | 10 | Envio, cuotas, Full |
| Reputacion | 10 | Rating y reviews |

Sale un indice 0-100 (solida / aceptable / expuesta / muy expuesta) y la lista
de puertas abiertas ordenadas por lo que cuesta cerrarlas.

### Deteccion de errores de escritura

Cada palabra del titulo se compara contra el vocabulario real de autosuggest: si
se parece demasiado a una busqueda real sin serlo, es un error que cuesta una
keyword entera.

**Caso encontrado el 2026-09-08**: el Cortador Ender Pro (MLA1481911017 y
MLA1932975847) dice **"Abriertas"** donde la gente busca **"abiertas"**. Una
letra que deja afuera "cortador de puntas abiertas", de las mas buscadas del
rubro.

### Medicion real del Cortador Ender Pro

- Cobertura: **21,5%** — te encuentran por 2 de 11 busquedas reales
- Indice de defensa: **48,8 / 100 — publicacion EXPUESTA**
- Lo que mas rinde agregar: `florecidas` (+5 busquedas), `corta` (+3),
  `maquina` (+3), `abiertas` (+2)
- El 75% de las visitas llega por una sola busqueda: si pierde esa posicion,
  pierde casi todo

### El circulo virtuoso

Cada refuerzo se registra en Cerebro con el indice de defensa como hipotesis. A
los 7 y 14 dias se sabe si sirvio. Asi el sistema no supone que completar la
ficha ayuda: lo sabe, con casos propios y por rubro.

Restriccion que ordena todo: ML congela el titulo despues de la primera venta.
En una publicacion con ventas se corrige por descripcion y ficha, y la forma
correcta se aplica a las publicaciones nuevas y a los clones (bloque 8).

Modulo: `modules/defensa_publicacion.py`.

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

## 8.6 Estrategia de cartera dentro del grupo — *definido 2026-09-12*

Un grupo de producto (8.7) junta las publicaciones propias que son el mismo
producto. Hoy esas publicaciones hacen todas lo mismo: mismo precio aproximado,
mismas cuotas, mismas keywords, todas peleando las mismas busquedas. Eso produce
dos daños a la vez: **se canibalizan entre si** —cinco publicaciones compitiendo
por la misma keyword— y **ante una baja de la competencia bajan las cinco**,
cuando alcanzaria con que baje una.

Caso real (Novara, 12/09/2026): cinco cortadores de puntas a $60.578, $60.578,
$72.000, $75.000 y $75.000, contra PATIOSCRAFTS a $32.550 ocupando tres de los
cuatro primeros lugares de la busqueda.

**La idea: cada publicacion del grupo tiene un ROL distinto, y del rol salen las
palancas.** La consecuencia practica es la que importa: cuando un competidor
baja, el sistema no dice "baja el precio". Dice *"baja la atacante hasta X y no
toques las otras cuatro"*, y calcula si esa sola baja alcanza usando el margen
del GRUPO, no el de una publicacion suelta.

### 8.6.1 Roles

| Rol | Que hace | Quien deberia serlo |
|---|---|---|
| Atacante | Pelea precio de frente | Menor costo unitario o menor comision (Clasica antes que Premium); idealmente la de stock mas clavado |
| Margen | No persigue. Precio alto, estable | Mejor conversion y mejor reputacion: quien llega ahi ya decidio |
| Volumen / Ads | Concentra publicidad y promociones | Solo la de mejor conversion — pagar visitas para una que convierte 1,6% es tirar plata |
| Cuotas | Absorbe el costo financiero | Donde el comprador realmente las usa (`cuotas_breakdown`, `pct_contado`) |
| Full | Unica del grupo en Full | La de mejor rotacion: Full da visibilidad pero cobra almacenamiento, y en una publicacion clavada se come la ganancia |
| Nicho | Cubre un cluster de autosuggest que las otras no tocan | La de menor solapamiento de keywords |
| A rehacer | Candidata a reconstruir (8.5) o pausar | La que no gana en ninguna dimension |

### 8.6.2 Restricciones que el sistema ya puede conocer

- **El stock manda sobre el rol.** Una publicacion con 467 dias de stock no
  puede ser la de margen: tiene capital inmovilizado y necesita rotar. La que
  rota rapido protege precio; la clavada ataca o liquida. Conecta con el
  semaforo de portafolio (bloque 11).
- **La de catalogo tiene el rol casi forzado.** No se le puede optimizar
  contenido —titulo, ficha y descripcion los controla la ficha— asi que solo
  puede ser atacante de precio o candidata a Full. Nunca la de nicho por
  keywords. El sistema no deberia proponer lo imposible.
- **Las promociones de ML se coordinan.** Entra una del grupo, no las cinco;
  aceptarlas todas es descontarse cinco veces.

### 8.6.3 Lo que NO se hace

El señuelo de precio —una publicacion barata que no se piensa cumplir— no entra.
ML lo cobra por reputacion y el costo aparece meses despues.

### 8.6.4 Condiciones para que esto sea confiable

- **Sin costos cargados no hay estrategia.** Sin `costo` no hay margen, y sin
  margen no se puede decidir quien ataca sin perder plata. Al 12/09 buena parte
  del catalogo tiene `costo` y `margen_pct` en null.
- **Cada rol asignado es una hipotesis que se mide.** Este es el riesgo central:
  una asignacion equivocada no produce un error visible, produce meses de
  decisiones coherentes en la direccion equivocada. El rol entra a Cerebro como
  cualquier otra accion y recibe veredicto a 7 y 14 dias. Sin eso, es un tablero
  que da confianza sin haberla ganado.

### 8.6.5 Multi-cuenta — pendiente de verificar con ML

El usuario planteo sumar las cuentas de sus hijos al mismo analisis para no
canibalizarse entre cuentas. Tecnicamente el sistema ya es multi-tenant y seria
una extension natural del grupo. **No se construye hasta verificarlo con ML**:
hay reglas sobre cuentas relacionadas y publicaciones duplicadas entre ellas, y
el riesgo no es simetrico — el upside es evitar canibalizacion, el downside es
una sancion sobre varias cuentas a la vez.

---

## 8.7 Grupos de producto — *implementado 2026-09-12*

Junta las publicaciones propias que son el mismo producto en distintas
variantes. Los competidores se cargan **una vez por grupo** y los ven todas las
hermanas; el analisis y las sugerencias siguen siendo **por publicacion**, que
es donde separar tiene sentido.

- `modules/grupos_producto.py`: propuesta automatica por parecido de titulo
  ignorando tokens de variante (color, talle, pack), union por transitividad,
  persistencia, y `competidores_del_grupo` / `directos_del_grupo`.
- La propuesta no se aplica sola: dos productos pueden tener titulos casi
  iguales y ser cosas distintas, y agrupar mal significa analizar contra la
  competencia equivocada.
- Un item pertenece a un solo grupo. Un competidor cargado en dos hermanas se
  cuenta una vez, con el mejor puntaje.
- Optimizar IA pide los directos del grupo; el bookmarklet ofrece grupos antes
  que publicaciones sueltas.

Grupos detectados en Novara: Trusa Modeladora Biobella (6), Cortador Puntas (5),
Delineador Cejas Regina (4), Cepillo Secador Alisador (3), Rizador Pestañas (2).

---

## 8.5 Reconstructor de publicación — *definido 2026-09-10*

El motor que arma titulo, ficha y "descripcion superadora" a partir de
competidores, autosuggest y Q&A ya existe: es `seo_optimizer`, la pantalla
Optimizar IA. Lo que no existe es el criterio para dispararlo solo, ni la
conexion con lo que Cerebro ya sabe. Hoy hay que entrar a mano y elegir una
publicacion; el diagnostico y la solucion viven en dos pantallas que no se
hablan.

Este bloque define **cuando** se propone reconstruir y **con que entradas**.
No modifica `seo_optimizer` (Regla #1): es un modulo que prepara la entrada y
post-procesa la salida.

### 8.5.1 Cuando se dispara — señales, de mas fuerte a mas debil

Cada reconstruccion cuesta una llamada a la IA, asi que no se dispara por
calendario sino por evidencia de que **el contenido** es el problema. No de que
hay un problema: si no la encuentran, eso es keywords y se arregla mas barato.

1. **Hermana que convierte mucho mejor.** Mismo producto, mismo precio, misma
   cuenta, conversiones muy distintas y el precio no lo explica. Es la señal mas
   fuerte porque el control existe: la mejor descripcion ya esta en la cuenta.
   Caso de referencia: MLA1481911017 (438 visitas, 7,76%, desc 2449 car.) contra
   MLA1932975847 (1336 visitas, 1,65%, desc 192 car.).
2. **Preguntas repetidas.** La mas limpia de todas: si la misma pregunta aparece
   N veces, la descripcion no contesta algo que hace falta para comprar, y cada
   repeticion es una compra frenada. Requiere agrupar preguntas por tema, que
   hoy no existe.
3. **Reseñas negativas que coinciden en una objecion.** Si se repite, la
   descripcion tiene que resolverla antes de la compra y no despues.
4. **Trafico normal con conversion baja** contra el propio catalogo: la
   encuentran y no compran.
5. **Cobertura de demanda baja con trafico suficiente** (barrido diario). La mas
   debil: suele arreglarse sumando keywords, sin reescribir todo.
6. **Un cambio anterior salio neutro o perdedor** en la evaluacion a 7/14 dias.
   Reintentar con otro enfoque — esta es la señal que hace que el sistema
   aprenda en vez de repetir.

### 8.5.2 Cuando NO se dispara

Pesa tanto como lo anterior.

- **Sin trafico suficiente.** Con pocas visitas no hay evidencia y no se gasta IA.
- **Mientras se mide otro cambio de esa publicacion.** Reescribir durante la
  ventana de evaluacion destruye la medicion: no se puede saber cual de los dos
  cambios hizo que. `cerebro` ya detecta contaminacion; el reconstructor tiene
  que respetar la ventana y esperar.
- **Publicaciones que no mueven la aguja.** El resultado esta en unos 10 SKU
  (criterio 6: saber donde NO meterse).
- **Titulo congelado por ventas** → solo ficha y descripcion; el titulo corregido
  queda para clones y publicaciones nuevas.
- **Tope de costo mensual**, como el Veredicto IA.

### 8.5.3 Entradas que hoy faltan

- **Las reseñas y preguntas propias.** `fetch_competitor_qa` recibe solo
  `comp_ids`; el `item_id` propio se excluye explicitamente. Las reseñas propias
  —incluidas las negativas— y las preguntas propias nunca entran a armar la
  descripcion.
- **El diagnostico de Cerebro**: keywords faltantes, cobertura, largo de
  descripcion, cantidad de fotos contra la hermana.
- **El filtro de marcas ajenas.** La salida generada tiene que pasar por el
  mismo filtro que las recomendaciones de keywords, o puede meter una marca de
  terceros en la descripcion.

### 8.5.4 Cadencia, canal y trazabilidad

El barrido diario marca candidatos. El reconstructor propone unas pocas por
semana, ordenadas por plata, siempre en **propone-y-apruebo**: es contenido
publico y no se toca solo. Entra al Top 3 y a la bandeja; al telefono solo lo
que mueve plata de verdad.

Cada cambio propuesto dice **de donde salio**: que pregunta repetida lo motiva,
que reseña, que busqueda real cubre. Un cambio que no se puede explicar no va
(criterio 1).

Al aplicarse, `aplicar_o_registrar` adopta la propuesta pendiente y conserva la
hipotesis original, que es lo que se contrasta a 7 y 14 dias.

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
| 26 | `/sites/MLA/search` devuelve 403 tambien desde IP residencial y navegador real (verificado 2026-09-08). No es un bug temporal de ML ni un bloqueo a IPs de datacenter: ML lo desactivo para trafico programatico | `analisis_competencia`, `repricing`, `monitor_posicionamiento`, `find_item_position` | Se cae la fuente principal de competidores y de posiciones por keyword. Los banners del sistema dicen que es un problema de ML que se va a resolver solo, y eso ya no es cierto: hay que rediseñar la estrategia de competencia sobre las fuentes que quedan | A |
| 27 | Sin `/sites/MLA/search`, `search_competitors` cae a buscar en el catalogo de PRODUCTOS y devuelve resultados sin relacion con el rubro, todos con `price: 0`, `sold: 0` y `no_active_listings: true` | `web/app.py` busqueda de competidores | Para "cortador de puntas cabello" devuelve cortadores de micas, cortadores de unas acrilicas, brocas de taladro y un cortador de chapas metalicas. Si el optimizador de titulos usa esto como referencia de competencia, esta aprendiendo del rubro equivocado | A |
| 28 | El titulo del Cortador Ender Pro dice "Abriertas" en vez de "Abiertas" en las dos publicaciones del cluster (MLA1481911017 y MLA1932975847) | Publicaciones de la cuenta Novara | Pierde "cortador de puntas abiertas" y "cepillo cortador de puntas abiertas", ambas entre las mas buscadas del rubro. El titulo ya esta congelado por ventas: se corrige por descripcion y ficha, y bien escrito en las publicaciones nuevas | A |
| 29 | `get_mp_resumen` leia como maximo 500 pagos y no paginaba. Pedir 30 dias y pedir 90 dias devolvia EXACTAMENTE los mismos totales (9.998.566 de cobros ML, 8.878.892 de otros, 500 movimientos), mientras `get_mp_movimientos` reportaba 646 movimientos solo en 30 dias | MCP local `ml_system_mcp/server.py` | Cualquier rango con mas de 500 movimientos devolvia un total parcial en silencio, sin avisar que el dato estaba incompleto. El filtro de fecha en si funcionaba bien (verificado con `dias=1`, cuadra al peso): el problema era el tope. **Resuelto** en `modules/contabilidad_import.py` con paginacion por ventanas de fecha + offset | A |
| 30 | `get_mp_resumen` no filtraba por `collector_id`, asi que contaba como ingreso los pagos que el usuario HACIA con MP | idem | Entraban como facturacion: `Compra en DIA` $12.450, `Producto de BARI PIZZAS Y EMPANADAS` $22.000, `JOSFRA SRL` $126.997, `Edenor` $83.622, `SUBE` $20.000, mas un `Bank Transfer` de $600.000 y otro sin descripcion de $2.000.000. Los 8,8M de "cobros_otro" eran casi todo ruido. **Resuelto**: la direccion se resuelve comparando collector.id / payer.id contra el user id de la cuenta | A |
| 31 | `get_mp_resumen` sumaba pagos `rejected` y `refunded` con el mismo peso que los aprobados | idem | Infla los cobros con ventas que nunca se cobraron (ej. un Ender Pro de $60.578 rechazado y otro devuelto en la misma semana). **Resuelto**: se guardan con `computable=False`, quedan auditables y fuera de los totales | A |
| 32 | `get_mp_resumen` topea el parametro `dias` en 90, asi que no habia forma de pedir la facturacion del anio | idem | Imposible responder "cuanto facturé del 1 de enero a hoy". **Resuelto**: los importadores toman `desde`/`hasta` arbitrarios | A |
| 33 | `list_accounts` del MCP devuelve `{"aliases": []}` | MCP local | Deja inutilizables `get_my_items`, `get_item_health` y todo lo que pide alias. **Pendiente de diagnostico** | A |
| 34 | El importador de facturacion usaba una pausa de 0,35 s entre llamadas (unas 170 por minuto), pero `/billing/integration/*` permite **5 requests por minuto**. La API responde `{"status":429,"type":"TOO_MANY_REQUESTS_ERROR","message":"Rate limit exceeded: 5 requests per minute. Available tokens: 0"}` | `modules/contabilidad_import.py` | En la primera corrida real del 12/09 la importacion de enero-a-septiembre murio con 429 en la sexta pagina. Se habia leido la advertencia de la documentacion sobre los 429 y aun asi la pausa se calibro a ojo en vez de al limite real. **Resuelto**: limitador con lock (13 s entre llamadas) mas reintentos con espera creciente desde 45 s, y tests que fijan el comportamiento | A |
| 35 | Los tres importadores envolvian TODA la corrida en un solo `session_scope`, asi que una excepcion al final descartaba todo lo ya leido | idem | En la misma corrida se perdieron los 2.056 movimientos de facturacion que ya estaban leidos: el historial decia "2056 nuevos" y el panel mostraba Plataforma en $0, porque el rollback se los llevo. Un import de varios minutos no puede ser todo-o-nada. **Resuelto**: una transaccion por pagina en ordenes, facturacion, percepciones y MP | A |
| 36 | El formulario de cuentas de MP pedia el *nombre de la variable de entorno* en un campo que invitaba a pegar el token, y despues reportaba un "falta token" incomprensible. Peor: el valor se renderizaba en la tabla de la pantalla | `web/contabilidad_routes.py`, `contabilidad_importar.html`, `listar_cuentas_mp` | El usuario pego el token de produccion completo y quedo guardado en `token_env` y visible en pantalla. **Resuelto**: `_parece_token` distingue token de nombre de variable y lo guarda donde va avisando que interpreto; el listado ya no devuelve nunca el valor ni el nombre del token, solo su origen; `migrar_tokens_mp_mal_guardados` corrige en el arranque las filas que ya quedaron mal | A |
| 37 | 1.830 de los 2.056 detalles de facturacion leidos cayeron en SIN_CLASIF: el mapa `SUBTIPO_ML_RUBRO` cubre muchos menos subtipos de los que ML devuelve en la practica | `modules/contabilidad.py` | El desglose por rubro queda casi vacio y el resultado no refleja los cargos de plataforma. **Pendiente**: se resuelve con datos reales, no adivinando — reimportar un mes con el rate limit ya corregido, mirar que subtipos y que `transaction_detail` devuelve ML, y ampliar el mapa y las reglas semilla desde ahi | A |
| 38 | Un access token de ML estaba commiteado en `.claude/settings.local.json` desde el primer commit del 22/04, en un repo **publico** (verificado: la API de GitHub reporta `visibility: public` y un clone anonimo funciona) | `.claude/settings.local.json` | Un barrido de los 168 archivos trackeados encontro ese unico secreto real; `config/accounts.json` y `.env` ya estaban bien ignorados, asi que el refresh token y el client_secret nunca se expusieron. Los access token de ML expiran a las 6 h, asi que el valor esta con certeza practica muerto; queda visible el client_id. **Resuelto** el tracking (git rm --cached + .gitignore). **Pendiente de decision del usuario**: el valor sigue en el historial de git, y si el repo debe seguir siendo publico | A |
| 39 | El importador de percepciones adivinaba los nombres de los campos de la respuesta. La documentacion (Resumen de Percepciones) define `summary[]` con `amount`, `taxable_amount`, `aliquot`, `tax_type`, `regimen_tax_type`, `document_id`, `bill_date` y `status`; el codigo buscaba `type`, `label`, `name` y `tax_id`, que NO existen | `modules/contabilidad_import.py` `importar_ml_percepciones` | Todas las filas quedaban con la etiqueta genérica "Percepción", la deteccion de IIBB nunca disparaba (buscaba la palabra en una etiqueta que siempre era la misma) y todo caia en PERCEP. **Resuelto**: se leen los campos documentados y `_clasificar_percepcion` decide por `tax_type` / `regimen_tax_type` | A |
| 40 | El external_id de las percepciones era la POSICION en la respuesta (`2026-01-01-0`, `-1`, ...) porque los campos identificatorios que buscaba no existian | idem | Al reimportar, si ML devolvia las filas en otro orden o en otra cantidad, las percepciones se mezclaban entre si y se acumulaban. En produccion quedaron 19 movimientos por 12,7 millones contra ventas de 146 millones: un 8,7% de percepciones, imposible. Lo detecto el usuario mirando el panel. **Resuelto**: la clave es `document_id` + `tax_type`, y `limpiar_percepciones_con_id_posicional` borra en el arranque las filas viejas para que no sumen doble contra las nuevas | A |
| 41 | Las percepciones no miraban `status` y se fechaban el dia 1 del periodo, ignorando `bill_date` | idem | Sumaba percepciones que ML no aplico, y cargaba un periodo entero contra los pocos dias del mes en curso — septiembre daba resultado negativo con 12 dias de ventas. **Resuelto**: solo las APPLIED son computables (el resto queda registrado y fuera de los totales) y la fecha sale de `bill_date` | A |
| 42 | **Las percepciones se contaban DOS VECES.** Llegan por el detalle de facturacion (`/group/ML/details`, subtipos CIVA, CIRE, IBNQ, IBCA, IBCF, IIBB, CGMV, CIBT, IBSA, IBLP, IBTU, IBCO...) y ademas se importaban del resumen `/perceptions/summary`. Yo asumi que eran fuentes distintas: el resumen es un resumen de ESOS MISMOS cargos | `modules/contabilidad_import.py` | Todo lo impositivo quedo exactamente al doble: 25,4 millones de percepciones donde habia 12,7 y 10,6 de IIBB donde habia 5,3, sobre 146 millones de ventas (17,3% de percepciones). Lo detecto el usuario mirando el libro; se confirma viendo el mismo importe con el mismo codigo de impuesto en dos filas, una `ml_billing` y otra `ml_percepcion`. **Resuelto**: los subtipos de percepcion se mapean en el detalle (fuente granular y buena) y el resumen pasa al rubro neutro CONCIL_PERCEP, no computable, para poder conciliar sin sumar. `neutralizar_resumen_percepciones` arregla en el arranque lo ya importado | A |
| 43 | **La serie mensual se agrupaba por `periodo` (el periodo de FACTURACION de ML), no por el mes calendario de la fecha.** Un cargo del 30 de agosto puede venir facturado en el periodo de septiembre, asi que el grafico comparaba las ventas de un mes contra los cargos de otro | `modules/contabilidad.py` (`resumen`) | Es lo que hacia que "el ultimo mes" diera negativo: en produccion septiembre mostraba **-3.140.168** en el grafico cuando el septiembre real era **+330.225**. Pasaba en los nueve meses (enero +3,9 M de diferencia, marzo +2,1 M, abril -1,8 M...). El total anual SIEMPRE estuvo bien: las diferencias entre meses sumaban exactamente cero, lo que estaba mal era el reparto. **Resuelto**: `_mes_de_fecha()` agrupa por `extract(year)`/`extract(month)` de `fecha`; `periodo` se conserva en la tabla porque es lo que permite cruzar contra la factura de ML | A |
| 44 | **El debito automatico de la factura mensual de ML en Mercado Pago habria duplicado toda la factura.** ML cobra cargo por cargo en la facturacion y ademas, antes del 10, debita el resumen (IIBB, percepciones, publicidad) por MP. Ese debito como gasto es la misma plata dos veces; peor, las reglas de texto (AFIP, Rentas) lo habrian clasificado como impuesto real | `modules/contabilidad_import.py` | Lo planteo el usuario: "puede ser que se contabilizo dos veces todo por que volvio a tomar esas facturas mas uno por uno de cada venta". Todavia no habia pasado porque la importacion de MP nunca corrio (`origen=mp_payment`: 0 movimientos). **Resuelto antes de que pase**: `_es_pago_de_factura_ml` detecta el debito y lo manda al rubro neutro CONCIL_FACT_ML, no computable y marcado para revisar. `rubro_sugerido` tiene prioridad sobre las reglas de texto, que era el agujero real | A |
| 45 | Ampliar `SUBTIPO_ML_RUBRO` no servia para lo ya importado hasta que alguien apretaba "Reclasificar" a mano | `modules/contabilidad.py`, `web/app.py` | Quedaron 250 pendientes en produccion con subtipos que YA estaban en el mapa (IBCO, IBNQ, CGMV, IBCF, IBLP, IBSA...). Reimportar el año son ~1 hora por el limite de 5 pedidos/minuto. **Resuelto**: `reclasificar_billing_por_subtipo()` corre en cada arranque, solo sobre facturacion sin rubro y sin decision humana. Subtipos nuevos mapeados: CFBA/CFRS/CFPB (Full), CPOPC (cobrar con MP), CRIA (adelanto de dinero), CSERRE (restaurantes, ambito personal), y las anulaciones provinciales BIB/BBNQ/BBCA/BBSA/BIBME, que no empiezan con C sino con I (IBNQ ↔ BBNQ): el fallback ahora prueba B→C y B→I | A |
| 46 | **ML no factura por mes calendario: CIERRA EL 10.** El estado de cuenta con cierre 10/ago cubre del 11/jul al 10/ago y vence el 17. Confirmado con el estado de cuenta real de Guille | `modules/contabilidad_cierre.py` | Es la causa de fondo de la correccion 43. Se agrego `conciliar_estado_cuenta(cierre)`, que reproduce el estado de cuenta desde el libro propio, linea por linea, para poder ponerlo al lado de la pantalla de ML. Verificado contra el cierre 10/ago/2026: **Cargos por venta 4.386.865 vs 4.386.887,82 · Envios 1.957.915 vs 1.957.915,05 · Publicidad 589.827 vs 589.828,97 · Envios full 22.892 vs 22.892,75 · Mi pagina 15.999 vs 15.999,00 · Percepciones 1.875.361 vs 1.875.361,32**. Coincide a los pesos | A |
| 47 | Dos agrupamientos de ML que no se deducen de los subtipos: **CDSD** ("Cargo por devolucion") va DENTRO de "Cargos de envios de Mercado Libre", no en una linea de cancelaciones; y **CFWA + CFCB** son los "Cargos de envios full" | `modules/contabilidad_cierre.py` | Sin esto la linea de envios parecia faltar 133.660, que era exactamente el CDSD del periodo. No era plata perdida: era una diferencia de agrupamiento. El total del libro siempre estuvo bien | A |
| 48 | **Las percepciones no caen dentro de la ventana del cierre: ML las imputa TODAS JUNTAS el dia siguiente** (11/ago para el cierre del 10/ago) | `modules/contabilidad_cierre.py` | Sumarlas dentro de la ventana 11/jul-10/ago traia las del cierre anterior y daba 2.700.170 en vez de 1.875.361 — casi el doble, el mismo sintoma que la correccion 42 pero por otra causa. Fijado con un test contra los importes reales | A |
| 49 | El importador de costos solo leia `config/costos.json` e ignoraba la **columna costo del snapshot de Stock y Rentabilidad** (`data/stock_<Alias>.json`), que es la otra fuente donde el usuario tiene costos cargados | `modules/contabilidad_cierre.py` | Quedaban 121 publicaciones con ventas y sin costo, con solo 140 de 3.538 ventas valuadas: el resultado mostraba margen sobre plataforma, no ganancia. **Resuelto**: se leen las dos fuentes, `costos.json` manda porque es lo que el usuario carga a mano | A |
| 50 | La pantalla de costos listaba las publicaciones sin costo pero no habia forma de bajarlas: habia que copiar 121 IDs a mano de la tabla | `web/contabilidad_routes.py` | La columna costo del snapshot de Stock y Rentabilidad resulto NO ser una fuente independiente (sale de `costos.json`, y el snapshot cubre 80 publicaciones de las que 40 tienen costo), asi que la correccion 49 no sumo costos nuevos: el faltante es dato que Guille todavia no cargo. **Resuelto**: `/contabilidad/costos-faltantes.csv` baja las faltantes con la columna `costo` vacia, separador `;` y BOM para que Excel en español la abra bien; se completa y se pega de vuelta en el cargador masivo. Un test verifica que lo que sale vuelve a entrar sin tocar nada | A |
| 51 | `discover_competitors` responde 400 "No se encontraron competidores" para MLA1932975847, que si tiene competencia | `web/app.py` `/api/descubrir-competidores` (la tool del MCP solo hace de proxy — la logica real esta aca, no en `ml_system_mcp`) | Investigado 2026-09-18: la fuente primaria (`/highlights/MLA/category/{id}`) devuelve **403 `PA_UNAUTHORIZED_RESULT_FROM_POLICIES`**, verificado en vivo contra la cuenta real. No es un bug de codigo — es el mismo bloqueo de politica de ML que ya afecta `/sites/MLA/search` (correccion 26), ahora confirmado que tambien alcanza a `/highlights`. Sin fuente primaria, cae en el fallback por keywords (`/sites/MLA/search`), que tambien 403. **No hay fix de codigo posible con las fuentes actuales** — el rediseño de correccion 26 ("estrategia de competencia sobre las fuentes que quedan") sigue pendiente y ahora es mas urgente: highlights tambien esta bloqueado | A — investigado, no resuelto (bloqueo externo) |
| 52 | `get_competitor_details` con un `item_id` de catalogo devuelve datos cruzados de otro producto (probado con MLA2089950644: devolvio titulo y precio 0 de otro articulo) | idem | Re-probado 2026-09-18: `GET /products/MLA2089950644` en la API real de ML devuelve HOY un producto de tela/textil coreano sin relacion — el ID en si ya resuelve a otro catalogo, no es el codigo de `ml-system` cruzando datos. **No se pudo reproducir como bug de codigo** — probablemente el ID original que motivo este item ya estaba mal desde donde se capturo (posiblemente de `discover_competitors`, que via la correccion 51 esta rindiendo mal) | A — no reproducido, posible causa raiz: correccion 51 |
| 53 | `get_competitor_details` con un `product_id` tipo MLAU devuelve HTTP 500: `cannot access local variable 'no_active_listings' where it is not associated with a value` — la variable se usa antes de asignarse | `web/app.py` `/api/detalle-competidor` | Peor de lo que decia el reporte original: la variable no se asignaba en **tres** caminos distintos (Intento 2 `/items/{id}` directo, Intento 3 con catalogo encontrado y listings activos, e Intento 3 sin catalogo encontrado), no solo uno. **Resuelto**: se inicializa `no_active_listings = False` una sola vez al principio de la funcion. Probado en vivo contra los dos casos reales que antes rompian (MLA2089950644 y un MLAU inventado) — ambos devuelven 200 ahora | A — **Resuelto** 2026-09-18 |
| 54 | Falta una funcion para listar las publicaciones de un vendedor a partir de su `seller_id` | idem | Mismo bloqueo que la correccion 51: no hay endpoint de busqueda vivo para armarla. Sigue limitado al bookmarklet | A — sin resolver (bloqueo externo, mismo que 51) |

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


## Sistema contable (Fase 1 — 2026-09-12)

El usuario planteo el problema asi: "vendo pero nunca se si estoy ganando
realmente". Lo que pidio no es un reporte de facturacion sino contabilidad
completa: todos los cobros de ML (ventas, cancelaciones, lo pagado a la
plataforma, impuestos, publicidad) y todos los cobros y gastos de MP,
separados por rubros, para saber cuanto gana por mes y por anio, con carga
manual de lo que no pasa por las plataformas y vista global mas vista por
cuenta.

### Decisiones tomadas con el usuario

- Los movimientos personales de la cuenta MP van a un rubro aparte
  (`PERSONAL`), visibles y auditables pero **fuera** del calculo de
  rentabilidad. La cuenta mezcla personal y negocio y contarlos deformaba el
  margen.
- Criterio **caja pura** (todo bruto, tal como se movio la plata) como vista
  principal, no gerencial neto de IVA. Aun asi el IVA y las percepciones se
  guardan en columnas aparte, para poder agregar la vista neta despues sin
  reimportar un anio entero.
- Orden de trabajo: motor de importacion y conciliacion primero, UI despues.
- Se suma carga masiva de costos de mercaderia con pegado desde Excel, porque
  sin COGS el numero no es ganancia.

### Principios de diseno

1. **Un solo libro.** Todo movimiento — venta, comision, envio, Ads, Full,
   percepcion, cobro o gasto de MP, gasto cargado a mano — va a
   `cont_movimientos`. No hay tablas paralelas por fuente.
2. **Nada se descarta en silencio.** Lo que el clasificador no reconoce queda
   con rubro `SIN_CLASIF` y aparece en la bandeja de pendientes. Un subtipo
   nuevo de ML no hace que el total quede mal sin aviso: hace que aparezca un
   pendiente. Es el mecanismo que sostiene el "ni un dato sin contabilizar".
3. **Las ventas se cuentan una sola vez.** El ingreso sale de `/orders`. El
   cobro espejo en MP se importa igual (hace falta para conciliar y para caja)
   pero cae en el rubro neutro `CONCIL_MP`, que no afecta resultado.
4. **Signo en el dato.** `monto` viene firmado: + entra, - sale. El resultado
   es una suma, no un armado de restas por rubro.
5. **Idempotencia.** La clave `(cuenta_alias, origen, external_id)` permite
   reimportar el mismo periodo N veces sin duplicar. Verificado por test.

### Endpoints confirmados contra la documentacion oficial

No se adivinaron rutas. Se leyo la doc de Reportes de Facturacion y Provisiones:

- `GET /billing/integration/monthly/periods?document_type=Bill` — periodos.
  La key es SIEMPRE el primer dia del mes (`YYYY-MM-01`), asi que se construye
  y no se consulta este endpoint repetidamente.
- `GET /billing/integration/periods/key/{key}/summary/details?group=ML|MP` —
  resumen con `charges[]` (`type`: CV, CXD, PADS...). No usar en batch.
- `GET /billing/integration/periods/key/{key}/group/{ML|MP}/details` —
  detalle. `document_type` es obligatorio (BILL / CREDIT_NOTE).
- `GET /billing/integration/periods/key/{key}/perceptions/summary` —
  percepciones, exclusivo MLA.
- `GET /billing/integration/group/ML/order/details?order_ids=` — por orden,
  maximo 60 ids. Trae `tax_details` con retencion de ganancias, de IVA,
  debitos/creditos y SIRTAC.

**Paginacion obligatoria por `from_id`, no por `offset`.** `offset` topea en
10.000 y no garantiza integridad en listados largos. El patron es: primera
pagina `from_id=0&limit=1000&sort_by=ID&order_by=ASC`, y despues el `last_id`
de la respuesta anterior como `from_id`.

**Consumo secuencial, nunca batch.** La doc advierte que el paralelismo es la
causa tipica de los 429 en billing. Los importadores corren en serie con pausa
de 0,35 s y cachean todo en la base.

### La conciliacion es la prueba, no un extra

ML publica su propia regla de conciliacion: la suma de `detail_amount` por
`detail_sub_type` del detalle tiene que dar igual al `amount` del mismo `type`
en el resumen. `conciliar_billing()` la aplica y, si no cuadra, no dice "todo
bien": informa la diferencia y en que subtipo esta. Mas
`conciliar_ml_mp()`, que cruza ventas contra cobros y reporta ventas sin cobro,
cobros sin venta y diferencias.

### Archivos

| Archivo | Responsabilidad |
|---|---|
| `web/models_contabilidad.py` | Modelos: rubros, libro unico, reglas, corridas de importacion, cuentas MP, costos |
| `modules/contabilidad.py` | Plan de rubros, clasificador, upsert, agregacion, bandeja de pendientes |
| `modules/contabilidad_import.py` | Importadores de MP, billing ML, percepciones y ordenes |
| `modules/contabilidad_cierre.py` | CMV, carga masiva de costos, conciliacion, cierre de periodo |
| `tests/test_contabilidad.py` | 71 aserciones sobre la forma real de los datos de la cuenta |

### Comandos

```bash
python main.py contabilidad importar <alias> 2026-01-01 2026-09-12 [alias_mp]
python main.py contabilidad resumen  [desde] [hasta] [alias]
python main.py contabilidad anual    [anio] [alias]
python main.py contabilidad cierre   <alias> 2026-09
python main.py contabilidad pendientes [alias]
python main.py contabilidad sin-costo  [alias]
python main.py contabilidad cmv      <desde> <hasta> [alias]
```

### Lo que falta para que el numero sea confiable

1. **Cargar el token de MP en Render** como variable de entorno
   (`MP_ACCESS_TOKEN_PROD`) y dar de alta la cuenta en `cont_cuentas_mp`. Hoy
   el token vive solo en el MCP local, que no persiste nada.
2. **Cuanto tarda un import.** Por el limite de 5 requests por minuto, la
   facturacion de un año son unos 8 minutos (9 periodos x 2 grupos x 2 tipos de
   documento = 36 llamadas a 13 s) y las percepciones otros 2. No es lentitud
   del codigo: es el limite de ML. Corre en segundo plano y la pantalla de
   Importar refresca el avance sola.

3. **Cargar los costos de mercaderia.** Buena parte del catalogo tiene `costo`
   y `margen_pct` en `null`. Mientras falten, `resumen()` devuelve una
   advertencia de nivel critico que dice explicitamente que lo que se ve es
   margen sobre plataforma y NO ganancia. El listado de que falta sale con
   `contabilidad sin-costo`.
4. **Cerrar el circuito con el MCP.** Exponer `resumen`, `pendientes` y
   `cerrar_periodo` como tools del MCP, para poder preguntar "cuanto gane en
   agosto" desde el chat sin abrir el panel.

### Fase 2 — la UI (2026-09-12, misma sesion)

Siete pantallas bajo `/contabilidad`, como Blueprint en
`web/contabilidad_routes.py`. Va en su propio archivo y no dentro de
`web/app.py`: el registro alla son dos lineas y app.py ya tiene 20.000. El
`before_request` global las protege igual que al resto.

| Ruta | Que hace |
|---|---|
| `/contabilidad` | Resultado del periodo, KPIs, grafico mensual, desglose por rubro, vista por cuenta, publicaciones sin costo |
| `/contabilidad/movimientos` | El libro completo con filtros por fecha, cuenta, rubro, origen, ambito y texto libre, mas la suma del filtro aplicado |
| `/contabilidad/pendientes` | Bandeja de lo no clasificado, con asignacion inline y creacion de regla |
| `/contabilidad/gastos` | Alta manual: proveedores, impuestos, sueldos, servicios |
| `/contabilidad/costos` | Carga masiva de costos pegando desde Excel o subiendo CSV |
| `/contabilidad/cierre` | Conciliacion del periodo contra el resumen de facturacion de ML y contra MP |
| `/contabilidad/importar` | Disparar importaciones (en thread), historial de corridas y alta de cuentas MP |

Decisiones de UI:

- **El resultado tiene su propia jerarquia visual.** Es el numero que el
  usuario vino a buscar, asi que no es un KPI mas en la fila: es una card
  aparte, con borde de color segun signo y tipografia `--text-display`.
- **El signo se muestra explicito.** El filtro `pesos` escribe `-$1.500.000`
  en vez de rojo solo: en una pantalla contable confundir un ingreso con un
  egreso es el peor error posible, y el color por si solo no alcanza.
- **El desglose se lee como un estado de resultados**, no alfabeticamente:
  Ingresos, Costos, Plataforma, Impuestos, Operativos, y los neutros
  (traspasos, personal, sin clasificar) al final. El orden lo da el campo
  `orden` del plan de rubros. Hay un test que lo fija.
- **Las advertencias viajan con el numero.** Si faltan costos, el dashboard
  dice en un banner critico que lo que se ve es margen sobre plataforma y NO
  ganancia, con un boton directo a cargarlos. Un numero lindo y falso es peor
  que no tener numero.
- **Los estilos compartidos van en `_contabilidad_estilos.html`**, un parcial
  Jinja, no en `components.css`: el patron se repite solo dentro de esta
  seccion y no hacia falta tocar un archivo global (Regla #2).
- Todo por tokens `var(--*)`, Bootstrap Icons, clases `.app-*`. Sin hex
  hardcodeado.

Entrada en la sidebar: `Contabilidad` en el bloque `nav-admin`, arriba de
Multicuenta, siempre visible porque es una vista global y no por cuenta.

### Pruebas

- `tests/test_contabilidad.py` — 74 aserciones de logica de negocio sobre la
  forma real de los datos de la cuenta.
- `tests/test_contabilidad_ui.py` — 57 aserciones de renderizado: las siete
  pantallas vacias y con datos, filtros, formularios, la API de asignar rubro,
  y robustez de parametros (rango invertido, fecha basura, pagina fuera de
  rango). Un template Jinja roto no lo detecta un chequeo de sintaxis Python:
  solo se ve renderizando.
- `tests/demo_contabilidad_screens.py` — no es un test: siembra nueve meses de
  operacion con la forma real de la cuenta y saca capturas con Playwright,
  para revisar la UI sin deployar. Ojo: el CDN de Bootstrap/Chart.js no se
  alcanza desde el contenedor de desarrollo, asi que el script intercepta esas
  requests y sirve copias locales; sin eso la grilla se apila y el grafico sale
  vacio, y parece un bug del codigo cuando no lo es.


## Estado al 2026-09-12 — donde retomar

Lo construido y deployado en esta sesion, en orden:

- **Duplicados**: 8vo eje `catalogo`. La publicacion de catalogo y la tradicional
  del mismo producto ya no se marcan como duplicado — ese falso positivo pedia
  pausar una de las dos por $273.936/mes.
- **Preguntas**: el job marcaba como vista toda pregunta que veia aunque el envio
  fallara; habia una del 01/06 sin responder hace 100 dias que nunca iba a
  llegar. Ademas, pasada una semana una pregunta deja de ir al telefono: el
  comprador ya no esta.
- **`modules/diagnostico_keywords.py`** + pantalla `/keywords/<alias>` + job
  `keywords_diario` (05:00). Cruza titulo, ficha y descripcion contra
  autosuggest. Filtra marcas ajenas antes de calcular: recomendaba "suma
  maybelline al titulo", que es infraccion de ML. Resultado real: 39
  publicaciones, 8 errores de escritura, ~$1,57M identificados.
- **`modules/catalogo_competencia.py`**: quien mas vende en una ficha de
  catalogo y por que no la ganas.
- **`cerebro.aplicar_o_registrar`**: cierra propone -> aplica. Las propuestas de
  `defensa_publicacion` y `competencia_diagnostico` quedaban pendientes para
  siempre y no se evaluaban nunca.
- **`price_to_win` con backoff**: un 429 se descartaba igual que "no compite".
- **Pantalla `/cerebro/<alias>`**: bandeja, midiendo, veredictos, aprendizajes y
  competidores. Existian 14 endpoints y ninguna pantalla.
- **Bookmarklet "Enviar a Cerebro"** (`/bookmarklet/<alias>`): panel con foto,
  precio y vendedor para elegir competidores uno por uno. Unica via que queda.
- **`modules/grupos_producto.py`** (bloque 8.7).
- **Optimizar IA usa los competidores confirmados del grupo.**

### Lo que se descubrio y conviene no volver a averiguar

- `/sites/MLA/search`, el mismo con `category`, y `/highlights` devuelven **403**
  desde Render. Responden `/trends` (terminos, no publicaciones) y
  `/products/search` (fichas). **No hay camino por API a competidores fuera de
  catalogo**: el bookmarklet no es una opcion, es la unica.
- `/items/{id}` de **otro vendedor** devuelve 403. Cargar un competidor pegando
  su MLA no funciona.
- Por eso `fetch_competitors_full` devolvia lista vacia y **Optimizar IA venia
  generando titulos y descripciones sin dato alguno de competencia**, y sin
  preguntas ni reseñas de competidores. Falla en silencio: no rompe, se degrada.
- `/products/{cpid}/items` **si** responde: es como se resuelve una ficha a la
  publicacion que gana la buy box.
- El nombre del producto de catalogo de Biobella dice **"Abriertas"**. El typo
  esta en la ficha, no en el titulo, y por eso se propaga.

### Lo que el usuario tiene que hacer antes de seguir

1. Cargar **costos** (al menos cortadores y trusas). Sin `costo` no hay margen y
   la mitad de lo construido no puede calcular nada.
2. Marcar **directos** entre los competidores capturados. Solo los directos se
   usan.
3. Aplicar los **8 errores de escritura**: gratis y el cambio mas limpio de medir.
4. Dejar correr **dos semanas** para los primeros veredictos.

### Lo siguiente, en orden

1. Agrupar preguntas por tema (entrada mas fuerte del reconstructor 8.5).
2. Alerta de vendedor ajeno en ficha de marca propia (`catalogo_competencia` ya
   lo detecta, falta correrlo en el job de buy box).
3. Reconstructor de publicacion (8.5).
4. Estrategia de cartera (8.6) — recien con costos cargados y veredictos reales.
5. Mejorar Salud del Catalogo con el diagnostico de `catalogo_competencia`.

Pendientes viejos que siguen abiertos: el lanzador no registra en Cerebro;
`ads`, `full` y `repricing` devuelven cero candidatos en el Top 3 y nadie
investigo por que; la skill `meli-reglas` sigue con el hash viejo.

## 12. Calculadora de Estrategia de Precios + Generador de Trio (spec 2026-09-18)

Spec aparte de Guille, con su propio plan de 5 sprints. Objetivo: para un
producto, calcular precios por estrategia (rentabilidad / competir / velocidad),
decir cuantas ventas por dia hacen falta para que convenga, aplicar el cambio y
medirlo. Mas un generador de las 3 publicaciones del "trio" (Batalla / Medio /
Compensa). Detalle completo de la spec fuera de este doc (se la paso Guille
directamente); acá solo el estado de avance y las decisiones que no estan en la
spec original.

### Decision: un solo motor, no dos

La spec original pedia un archivo nuevo `modules/pricing_engine.py`. Antes de
crearlo se encontro que ya existe `modules/precio_motor.py` (bloque 12.2,
seccion 3 de este doc no — es el modulo real, no la spec de Cerebro), motor
unico usado por `repricing.py`, `top_acciones_diarias.py`,
`competencia_diagnostico.py` y `diagnostico_keywords.py`, con el mismo umbral
de envio gratis ($33.000) y el mismo piso de margen (15%) que la nueva spec
necesitaba. Crear un archivo aparte hubiera recreado el problema que
`precio_motor.py` se creo para resolver (bloque 12.2: "cuatro logicas de precio
que no se conocian entre si"). **Decision de Guille: ampliar `precio_motor.py`
en vez de crear un archivo nuevo.** Las funciones de la spec (Cargos,
Publicacion, Objetivo, `estrategia()`, `escalera_meta()`, etc.) viven ahi,
reusando `UMBRAL_ENVIO_GRATIS_ARS` y `MARGEN_MINIMO_ACEPTABLE`. Tests en
`tests/test_precio_motor.py` (no en un archivo aparte), clases
`TestEstrategiaTresPublicaciones` y `TestEstrategiaBordes`.

Cuando el bloque 3 de Cerebro (Precio, Sprint C — pendiente) se construya, tiene
que reusar `estrategia()`/estas mismas funciones para la postura por
publicacion, no reinventar la aritmetica de margen una tercera vez.

### Decision: percepcion de IVA SI se resta (corrige la spec original)

La spec original (§2) decia que la percepcion de IVA se recupera como credito
fiscal y no se resta del precio — solo IIBB. Guille confirmo el 2026-09-18 que
para esta cuenta eso es incorrecto: en la practica no se recupera, y tanto IIBB
como percepcion de IVA son costo real y van siempre restados. `Cargos` tiene un
campo `percepcion_iva` ademas de `iibb`; `tasa_cargos()` resta ambos. Los
numeros de referencia del §8 de la spec original (Casos A/B/C) NO sirven tal
cual porque asumian solo IIBB — se recalcularon corriendo el mismo codigo con
percepcion incluida (7% ilustrativo en los tests, no la tasa real medida de
7,08% — ver [memoria] ml-system-pricing-fees-real para la tasa real verificada).

### Verificado antes de sprint 1, corrige algo que se le dijo mal a Guille

Se le dijo a Guille que el hash MD5 de la Regla #1 (`seo_optimizer.py`) estaba
roto. **Eso ya no es cierto** — quedo resuelto en el Sprint A de Cerebro
(correccion 16 de la checklist de arriba): el hash actual del archivo
(`0389b93a8c4ff11c8eaa97327a6f54c1`, verificado 2026-09-18) coincide con el
re-baseado en `docs/ARQUITECTURA_OPTIMIZAR_IA.md`. Falta nada mas actualizarlo
en la skill `meli-reglas` (detalle menor, no bloquea nada). Sigue en pie para
el Sprint 5 (generador de titulos, llama a `seo_optimizer.py`).

### Sprint 1 — hecho (2026-09-18)

- `modules/precio_motor.py` ampliado: `Cargos`, `Publicacion`, `Situacion`,
  `Objetivo`, `tasa_cargos`, `cargo_fijo`, `ganancia_publicacion`,
  `redondear_arriba/abajo`, `precio_para_ganancia`, `precio_para_margen`,
  `precio_objetivo`, `precio_piso_objetivo`, `precio_batalla`, `estrategia`,
  `ganancia_hoy`, `ventas_para_empatar`, `mapa_decision`, `escalera_meta`.
- `tests/test_precio_motor.py`: 11 tests nuevos (Caso A ROI, Caso B margen,
  Caso C escalera + efecto umbral, 7 casos borde). 42/42 tests pasan (los 4
  modulos que ya usaban `precio_motor.py` siguen sin romperse).
- Pantalla Modo B (producto nuevo, sin publicacion todavia): `/pricing/nuevo`
  + `POST /api/pricing/calcular`. Prueba manual en local: carga sin ML, calcula
  las 3 estrategias, respeta el bloqueo cuando falta costo y marca el faltante
  cuando no hay precio de competidor. Nav agregado bajo "🚀 Lanzamientos".
- No incluido todavia (no lo pedia el criterio de aceptacion del sprint 1):
  persistencia en `pricing_config` — la pantalla no guarda cargos/perfiles
  entre cargas, se recalcula con lo que hay en el formulario cada vez. Se suma
  cuando Modo A (Sprint 2) lo necesite de verdad.

### Sprint 2 — hecho (2026-09-18)

- Tabla nueva `pricing_config` (`web/models_pricing.py`): cargos + 3 perfiles
  por cuenta, override opcional por item_id. Las otras 3 tablas del §4 de la
  spec (`precio_experimentos`, `snapshots_diarios`, `curva_demanda`) quedan
  para el sprint 3/4, cuando haya algo que escribir ahi.
- `GET/PUT /api/pricing/config`, `GET /api/pricing/contexto` (arma
  precio/listing_type/stock real, ventas/dia y `fee_rate` real de ordenes de
  30 dias via `stock_rentabilidad._compute_item_stats`, costo de
  `costos.json`, comision base real via ML, competidores confirmados via
  `cerebro.competidores_para_precio`, panel de faltantes).
- `/api/pricing/calcular` ahora tambien acepta `situacion_hoy` (opcional) y
  devuelve `empate` (ventas/dia para igualar hoy) + `mapa_decision` cuando la
  estrategia es "comp" y la diferencia entre publicaciones supera el 3%
  (regla de presentacion del §3.4 de la spec).
- `POST /api/pricing/recomendar`: mismo motor + Claude (`claude-opus-4-7`,
  mismo patron que `/api/evaluar-producto`) devolviendo el JSON del §6.
  Degrada con gracia si Claude falla (probado en vivo: la cuenta de prueba
  tenia el credito de API agotado, el endpoint respondio igual con
  `confianza: "baja"` y el motivo en `datos_que_faltan`, no un 500).
- Pantalla `/pricing/existente`: elegir cuenta + publicacion (reusa
  `/api/costos-items/<alias>`, ya existente), trae contexto real, muestra
  faltantes (bloquea el calculo si falta costo), calcula, y "Pedirle a Claude
  que recomiende una".
- **Bug real encontrado y corregido probando contra la cuenta NOVARA en
  vivo**: `comision_pct` iba a salir de `core.fees.get_rate()`, que lee
  `config/fees.json` calculado a precio de referencia $10.000 (por debajo del
  umbral de envio gratis) — a ese precio el cargo fijo infla la tasa
  (gold_special daba 26,3% en vez de ~13%, gold_pro 39,7% en vez de ~26,4%).
  Usarlo tal cual hubiera duplicado el cargo fijo, que el motor ya calcula
  por separado. Ademas se confirmo en vivo que la comision publicada de
  `gold_pro` (Premium) viene MEZCLADA con el costo de cuotas, sin forma de
  separarlos en `/sites/MLA/listing_prices` — por eso `/contexto` siempre
  pide la tasa de `gold_special` (Clasica, sin cuotas propias) a un precio
  por encima del umbral, y la etiqueta aclara que no tiene `category_id` (la
  falta de `category_id` en `core/fees.py` ya estaba anotada como problema en
  una nota previa de Novara/fees; sigue sin `category_id` real, asi que el
  numero puede no calzar exacto con la categoria del producto — verificado
  13% contra el 16% real medido para MLA5411 en la investigacion previa,
  diferencia esperable sin category_id).
- Verificado en vivo (cuenta NOVARA, item MLA1932975847): trae precio real
  $60.578, `fee_rate_real` 0,2916 medido de ordenes, stock 98, cuotas_breakdown
  real, y confirma el typo "Abriertas" del titulo (correccion 28) via el dato
  real de la API.

### Incognita nueva para el §10 de la spec (no estaba en la lista original)

**Comision publicada de `gold_pro` incluye el costo de cuotas, sin
desagregar.** No hay endpoint que separe "comision pura" de "costo de
cuotas" para una publicacion Premium — solo se puede aislar la comision pura
usando `gold_special` (que no tiene cuotas propias) como proxy. Si en algun
momento se necesita el costo de cuotas real y exacto por escalon (3/6/9/12)
en vez de la tabla de la spec, hay que investigar mas a fondo la respuesta de
`/sites/MLA/listing_prices` para `gold_pro` con distintos parametros, o pedirlo
directo a soporte de ML.

### Sprint 3 — hecho (2026-09-18)

- Tablas nuevas `precio_experimentos` y `snapshots_diarios` (`web/models_pricing.py`).
- `POST /api/pricing/aplicar`: escribe el precio nuevo en ML
  (`client.update_item`) y abre el experimento. Exige `confirmado: true`
  explicito en el body — nada se aplica como efecto secundario de un calculo.
  **Por ahora solo edita la publicacion existente** (precio); crear las dos
  publicaciones nuevas del trio queda para el sprint 5, `item_ids` del
  experimento tiene una sola entrada hasta entonces.
- `GET /api/pricing/experimentos`: lista los experimentos de una cuenta/producto.
- Job `pricing_snapshots_diarios` (06:05 ART, registrado en `_start_scheduler`):
  para cada publicacion con un experimento abierto, captura precio, stock,
  `ventas_dia` (delta de `sold_quantity` vs el snapshot de ayer — `None` el
  primer dia, no se inventa), `fee_rate` real (reusa
  `stock_rentabilidad._compute_item_stats`, una sola llamada a ordenes por
  cuenta, no por publicacion) y competidor_min. No manda nada por Telegram.
  No recalcula veredicto todavia — es sprint 4.
- **Probado sin tocar el precio real de nada**: se creo un experimento de
  prueba directo por ORM (sin pasar por `/aplicar`, para no escribir en ML) y
  se corrio la funcion del job contra el item real MLA1932975847 — trajo
  precio, stock, fee_rate real y `sold_quantity` real correctamente. El path
  de escritura de `/aplicar` (`client.update_item`) se reviso pero
  **no se probo en vivo**: cambiar el precio de una publicacion real de la
  cuenta de Guille no es algo para probar como efecto colateral, solo con
  autorizacion explicita para un producto puntual.
- De paso: el log `[scheduler] Activo — N jobs registrados` tenia el numero
  de jobs hardcodeado en 12 (quedo desactualizado apenas se agrego el job 13).
  Ahora sale de `len(scheduler.get_jobs())`.

### Enriquecimiento de pantallas tras comparar contra el artifact original (2026-09-18)

Guille había armado un prototipo standalone (Claude Artifact) antes de escribir
la spec formal. Al comparar contra lo construido en `ml-system` aparecieron
gaps reales de funcionalidad (no solo visuales — el estilo Bootstrap/sidebar
de `ml-system` se mantiene a propósito, decisión explícita del usuario).
Decisiones tomadas:

- **Las 3 estrategias se siguen mostrando juntas** (no un selector que
  muestra una sola), pero cada publicación ahora trae más información por
  fila: ROI%, margen%, "contra hoy por venta" (delta vs. lo que gana hoy la
  publicación real), y banderas (`ok`/`warn`/`bad`): gana o no al competidor
  más barato, si supera el competidor más caro (`competidor_max`, nuevo,
  faltaba en el formulario), si hay publicidad cargada, y una advertencia
  específica de "efecto umbral" (si el precio queda justo debajo de $33.000,
  sugiere el precio redondeado arriba del umbral y cuánto ganarías ahí).
- **`Publicacion.publicidad_dia` por perfil** (existía en el dataclass desde
  el sprint 1, nunca se expuso en el formulario): ahora hay una columna en
  "Las 3 publicaciones" y `_pricing_calcular_core` suma automáticamente el
  total en vez de pedirlo aparte (evita que un `publicidad_total` suelto no
  coincida con la suma real de los 3 perfiles).
- **`resultado_prueba()`** nuevo en `precio_motor.py` + `POST
  /api/pricing/verificar_prueba`: veredicto manual simple ("cargá lo medido,
  te dice si conviene") — adelanta una porción chica del sprint 4 sin
  necesitar el snapshot diario automático ni un experimento abierto. Reusa la
  misma fórmula de reparto Batalla/Medio/Compensa que ya tenía
  `mapa_decision()` (se extrajo a `_ganancia_promedio_ponderada()` para no
  duplicarla). Solo en `/pricing/existente` (Modo A) — en Modo B no hay
  publicación real contra la cual medir una prueba.
- Pendiente, no agregado todavía: la visualización tipo "línea numérica"
  (ladder) del artifact, y la guía de uso inline (qué es Dato/Supuesto/
  Medido, qué hace cada estrategia) — quedan para cuando haya lugar, no son
  bloqueantes.

### Sprint 4 — hecho (2026-09-18)

- Tabla `curva_demanda`.
- `modules.precio_motor.veredicto_experimento(dias_medidos, ganancia_dia_promedio, ganancia_dia_previa)`:
  `pendiente` antes de 7 días; `inconcluso` si la diferencia contra la
  ganancia previa es menor al 5% (aunque hayan pasado 14 días — no fuerza una
  decisión con una diferencia chica) o si todavía no llegó a 14 días con una
  diferencia clara; `conviene`/`no_conviene` recién a partir de 14 días con
  diferencia clara. 7 tests nuevos.
- `GET /api/pricing/evolucion?experimento_id=`: arma la serie diaria desde
  `snapshots_diarios` (recalcula ganancia/día con `margen_unitario()` sobre
  el fee_rate real medido de cada snapshot, resta `publicidad_dia` fija del
  experimento) y devuelve el veredicto.
- `POST /api/pricing/cerrar`: fija `cerrado_en` + `veredicto`, escribe el
  punto en `curva_demanda`. Bloquea cerrar dos veces.
- `GET /api/pricing/curva_demanda`: los puntos medidos del producto.
- `GET /api/pricing/escalera`: expone `escalera_meta()` del motor con el
  "precio a probar" (el más alto que le gana al competidor, no rompe el piso
  y el stock alcanza).
- Pantalla `/pricing/existente` ampliada: tarjeta "Ganar el doble" (escalera
  con banderas), tarjeta "Experimentos de este producto" (lista + evolución +
  botón cerrar), y botón "Aplicar" en cada fila viable de cada estrategia
  (llama a `/api/pricing/aplicar` con confirmación nativa del navegador antes
  de escribir).
- **Bug encontrado y corregido de paso**: `/api/pricing/config` (guardado de
  cargos por cuenta, sprint 2) nunca se leía de verdad en `/pricing/existente`
  — el formulario traía `cfg.perfiles` pero no `cfg.cargos`, así que el motor
  siempre calculaba con los defaults del sistema sin importar lo guardado.
  Se agregó el editor de "Cargos fijos e impuestos" (igual que en Modo B) con
  botón para guardar, y ahora sí se manda `cargos` en cada cálculo.
- **Probado end-to-end en local con datos sintéticos** (no se tocó ningún
  precio real): se insertó un experimento y 15 días de snapshots directo por
  ORM (sin pasar por `/aplicar` ni por el job real) para poder probar
  `/evolucion` y `/cerrar` sin esperar 15 días reales ni escribir en ML.
  `/escalera` se probó con los mismos números del caso de referencia del
  motor (coincide con el §8 de la spec). El botón "Aplicar" de la pantalla
  **no se probó en vivo** — dispara un `confirm()` nativo del navegador antes
  de escribir, así que solo se ejecuta si Guille lo confirma manualmente
  probando la pantalla.

### Incognita §10.1 resuelta (2026-09-18, via documentacion oficial de ML)

El escalon de cuotas **SI se puede fijar por API**, de forma deterministica,
no depende de que ML apruebe una campana por seller (salvo `pcj-co-funded`,
que si es opt-in pero universal). Se controla con el campo `tags` al crear o
editar la publicacion, combinado con `listing_type_id`:

| Perfil | listing_type_id | tag |
|---|---|---|
| Sin cuotas propias (Batalla) | `gold_special` | (ninguno) |
| Interes bajo, 3-12 cuotas (comprador elige, vendedor paga 4% fijo) | `gold_special` | `pcj-co-funded` |
| 3 cuotas al mismo precio | `gold_pro` | `3x_campaign` |
| 6 cuotas al mismo precio (default de gold_pro) | `gold_pro` | (ninguno) |
| 9 cuotas al mismo precio | `gold_pro` | `9x_campaign` |
| 12 cuotas al mismo precio | `gold_pro` | `12x_campaign` |

"Todos los sellers de MLA tienen habilitadas 3x, 9x y 12x_campaign" (doc
oficial) — no hace falta validar por seller, solo por categoria:
`POST /special_installments/$TAG/categories/$CATEGORY_ID/enabled`.

**Ademas resuelve algo que quedo pendiente del sprint 2**: `GET
/sites/MLA/listing_prices?price=&listing_type_id=&tags=&domain_id=` devuelve
`sale_fee_details.percentage_fee` (comision pura) y
`sale_fee_details.financing_add_on_fee` (costo de cuotas) **separados** — la
comision de `gold_pro` no esta "mezclada sin forma de separarla" como se
penso en el sprint 2, solo hacia falta pasar `tags` y `domain_id`
(`core.fees`/`core/ml_client.get_listing_fee_rate` no los soportaba). El
costo de cuotas real tambien varia por dominio/categoria, no es un numero
fijo — los valores de la memoria del usuario (8,9/13,4/17,8/21,6% para
Cortadoras de Pelo) son de ESA categoria puntual, no universales.

Pendiente de implementar con esto: extender `get_listing_fee_rate` para
aceptar `tags`/`domain_id`, y usarlo en `/api/pricing/contexto` y en la
creacion del trio para fijar el `tags` correcto por perfil.

### Sprint 5 — hecho (2026-09-18)

- `modules/trio_generador.py` (archivo nuevo, `seo_optimizer.py` no se tocó):
  `generar_titulos_trio(item_id, client)` genera hasta 3 títulos, uno por
  cluster de búsqueda real (nunca inventa un cluster de más — si el
  autosuggest solo da 1-2 con volumen, avisa y genera esos nomás). No llama a
  `run_full_optimization` (esa función genera UN título óptimo, no varios por
  cluster distinto a propósito) — llama directo a las piezas de más abajo del
  pipeline v2 (`_build_synthesis_prompt`, `_call_claude`, `_parse_synthesis`,
  `validar_sintesis`, ya existentes y confirmadas stateless) empujando el
  representante de cada cluster a TIER 1 antes de armar el prompt. Reusa
  `_cluster_keywords` (ya existe, Jaccard sobre tokens) para el agrupamiento
  — no hizo falta escribir clustering nuevo.
- **Corrección a la spec original**: el "caché de 24h" de autosuggest que
  pedía el §9.2 no existe en el código real (se buscó a fondo, no está) — se
  documenta la diferencia en vez de inventar que existe.
- `CUOTAS_A_TAGS` mapea escalón de cuotas → `(listing_type_id, tags)` usando
  la tabla oficial de ML encontrada arriba. `perfiles_duplicados()` bloquea 2
  publicaciones con el mismo tipo+cuotas. `ficha_faltante()` compara contra
  los atributos obligatorios reales de la categoría (`_get_category_attributes`).
- `POST /api/pricing/trio/preview`: genera todo, no escribe nada en ML.
- `POST /api/pricing/trio/crear`: exige `confirmado: true`, crea las
  publicaciones **pausadas**, con las fotos de la publicación existente
  reusadas por id, bloquea si falta la ficha o hay duplicado de tipo+cuotas,
  suma los `item_id` creados al experimento abierto si se pasa
  `experimento_id`, y devuelve el recordatorio de dar de alta en AppSeller.
- Pantalla: tarjeta "Generador de trío" en `/pricing/existente` — precio y
  escalón de cuotas por publicación, vista previa editable (título/
  descripción), y botón de creación con confirmación nativa mostrando el
  resumen completo antes de escribir.
- **Probado en vivo, sin escribir nada en ML**: `generar_titulos_trio` corrido
  contra el item real MLA1932975847 — trajo clusters reales (encontró 1 con
  volumen para este producto puntual, y avisó en vez de inventar 2 más),
  categoría, ficha requerida (Marca, Modelo) y las 6 fotos reales de la
  publicación. La llamada a Claude fallo por el mismo problema de crédito de
  cuenta que ya se vio antes (no es un bug) — se corrigió que ahora degrada
  con gracia (antes tiraba una excepción sin capturar) en vez de romper el
  endpoint. `/api/pricing/trio/crear` se probó solo en sus validaciones
  (confirmado faltante, duplicados) — la creación real de publicaciones
  **no se probó en vivo**, exactamente el mismo criterio que `/aplicar`.

### Pendiente

Incógnitas del §10 que siguen sin resolver: categorías con catálogo
obligatorio (verificar antes de crear títulos propios — no hay código que lo
chequee todavía), costo de envío real por producto (sigue estimado en $5.000).
`/aplicar`, el botón "Aplicar" y la creación del trío están implementados
pero **ninguno se probó escribiendo de verdad en ML** — probarlos con Guille,
mirando la pantalla, antes de confiar en ellos para uso real.

Con esto terminan los 5 sprints de la Calculadora de Estrategia de Precios +
Generador de Trío. Recordarle a Guille los bugs diferidos del §11
(items 51-54 de la checklist) — quedó pactado avisar cuando se terminara
este bloque completo.

### Auditoría del generador de trío pedida por Guille (2026-09-19)

Tras dos correcciones puntuales (paralelizar, sacar el reintento) el mismo
error seguía apareciendo — "SyntaxError: Unexpected token '<'" — y Guille
gastó cerca de USD 2 en un par de pruebas. Pidió explícitamente una
auditoría de toda la sección en vez de otro parche puntual.

**Causa raíz real, no encontrada antes**: la generación del trío llama a
Claude Opus con prompts grandes y legítimamente tarda 1-3 minutos (con 1
solo cluster, que es lo más común para sus productos). Cualquier pedido
HTTP que se queda esperando eso corre riesgo de timeout — el de gunicorn
(ya se había subido a 180s), pero también el del proxy de Render, que no
se puede configurar desde este repo. Cuando cualquiera de los dos corta el
pedido a mitad de camino, el navegador recibe una pagina de error HTML en
vez del JSON esperado (de ahí el "Unexpected token '<'") — pero el llamado
a Claude ya se había facturado. Ajustar timeouts era tratar el síntoma:
mientras la generación siga siendo un pedido HTTP bloqueado, siempre hay
algún límite de tiempo en algún punto de la cadena (servidor propio o
infraestructura de Render) contra el que se puede chocar.

**Arreglo estructural**: se volvió asíncrono. `POST
/api/pricing/trio/preview/start` valida y arranca la generación en un
hilo de background (`_threading.Thread`), devolviendo un `job_id` al
instante (probado en vivo: 28ms) — el pedido HTTP nunca queda esperando,
así que no hay timeout posible en ningún punto. `GET
/api/pricing/trio/preview/status?job_id=` consulta el progreso; la
pantalla hace polling cada 3s hasta que termina. Estado en memoria del
proceso (`_TRIO_JOBS`, con lock) — alcanza con `--workers 1`, no se
justifica sumar Redis/Celery para esto. Se limpian solos los jobs de más
de 30 minutos. El endpoint sincrónico viejo (`/api/pricing/trio/preview`)
queda para compatibilidad, pero la pantalla ya no lo usa.

Efecto secundario bueno: con `--workers 1`, antes el resto de la app
quedaba bloqueada para cualquiera mientras corría el trío; con la
generación en un hilo aparte, el worker principal queda libre para
atender otros pedidos mientras tanto.

### Auditoría completa de la sección (2026-09-19, pedida explícitamente)

Después de arreglar el trío, Guille pidió auditar toda la calculadora, no
solo esa parte. Revisión línea por línea de los 15 endpoints de
`/api/pricing/*` buscando el mismo tipo de problema (excepción sin
capturar, supuesto silencioso con un número engañoso). Hallazgos, de más a
menos grave:

1. **El más serio de toda la auditoría**: en `/api/pricing/trio/crear`,
   `float(pub.get('precio') or 0)` creaba la publicación REAL en ML con
   **precio $0** en silencio si el campo llegaba vacío. Corregido: bloquea
   esa publicación puntual con error claro en vez de crear a $0.
2. En `/api/pricing/aplicar` (el que escribe el precio real en ML): el
   registro del experimento se armaba DESPUÉS de escribir en ML — si
   cualquier campo fallaba al parsear, el precio ya había cambiado pero
   quedaba sin registrar, y el usuario veía un error genérico sin saber que
   el cambio real sí había pasado. Corregido: ahora se valida y arma todo
   ANTES de tocar ML; si algo falla después de escribir (el registro en sí),
   el mensaje dice explícitamente "el precio SÍ se cambió, anotalo a mano".
3. En `_pricing_calcular_core` (el motor de `/calcular` y `/recomendar`):
   la comisión de una publicación, si llegaba vacía, calculaba en silencio
   con **0% de comisión** — un número materialmente engañoso (infla la
   ganancia mostrada), no un error. Corregido: comisión vacía bloquea con
   mensaje claro, no se inventa un valor. Los demás campos (cuotas,
   publicidad, cargos de IIBB/envío/etc.) sí tienen un default razonable
   (0, o el default del motor) porque para esos sí existe un valor
   "neutro" honesto — no es el caso de la comisión.
4. Varios endpoints (`/calcular`, `/config` PUT, `/escalera`) construían
   `Cargos`/`Publicacion` con `float(x.get(campo, default))` sin
   try/except — un campo vacío en el formulario (no ausente: `.get()` con
   default solo aplica si falta la clave, no si está vacía) rompía con una
   excepción sin capturar → Flask devolvía su página de error HTML por
   defecto → el frontend esperaba JSON y explotaba con
   `SyntaxError: Unexpected token '<'` — el mismo síntoma que venía del
   timeout del trío, pero por una causa totalmente distinta. Corregido en
   los tres.
5. **Red de seguridad agregada**: `@app.errorhandler(Exception)` scopeado a
   `/api/pricing/*` (no toca el resto de la app) — cualquier excepción no
   capturada que aparezca en el futuro en estos endpoints devuelve JSON
   limpio con el error en vez de la página HTML de Flask. No reemplaza
   arreglar la causa real cuando se encuentra, pero asegura que un bug
   nuevo nunca vuelva a manifestarse como el mismo `SyntaxError` confuso.

Probado en vivo tras los arreglos: `/calcular` normal sigue funcionando
igual, comisión vacía bloquea con mensaje claro (no rompe), y el flujo de
`/trio/crear` sigue bloqueando correctamente por ficha/calidad antes de
llegar al chequeo de precio nuevo.

### Ajustes pedidos por Guille probando la pantalla en vivo (2026-09-18)

- El botón "Pedirle a Claude que recomiende una" fallaba en producción por
  falta de crédito en la cuenta de Anthropic asociada a la `ANTHROPIC_API_KEY`
  de Render (key terminada en `d33U1AAA`) — no era un bug de código, se
  confirmó reproduciendo el mismo error localmente. Guille cargó crédito;
  pendiente de reconfirmar en pantalla que ya funciona.
- El número de empate ("Para que convenga hacen falta entre X y Y ventas/día")
  no se entendía como rango. **Agregado** `pm.ventas_para_empatar_parejo()`:
  un solo número asumiendo que las 3 publicaciones se reparten las ventas por
  igual, marcado como "Supuesto". El rango sigue disponible como detalle
  secundario, y el mapa de decisión para cuando el reparto real no sea parejo.
- Faltaba ver el desglose de costos por publicación (comisión, IIBB,
  percepción, envío vs. cargo fijo según el umbral de $33.000). **Agregado**
  `pm.desglose_costos()` — línea por línea de qué se descuenta a un precio
  dado, expuesto en `/calcular` como `publicaciones[i].desglose` y con un
  toggle "(ver costos)" en ambas pantallas (Modo A y Modo B).
- 8 tests nuevos (60 en total).
- **Bug real encontrado en "Resultado de la prueba"**: si faltaba cargar
  "Ventas/día en la prueba" Y además no había "ganancia de hoy" calculada
  (costo o fee_rate real sin datos), el chequeo de ganancia-de-hoy corría
  primero y escribía un mensaje chico en pantalla en vez del `alert()` claro
  de "falta cargar ventas/día" — parecía que el botón no hacía nada. Se
  reordenaron los chequeos (ventas/día vacío primero, con alert nativo) y se
  destacó más el mensaje de ganancia-de-hoy faltante.
- **Aclaración importante, no bug**: "Ver veredicto" (Resultado de la prueba)
  es matemática pura, nunca llama a Claude — se verificó en el código. El
  gasto real de crédito de Anthropic que reportó Guille salió del generador
  de trío, que llama a Claude hasta 6 veces por click (hasta 3 títulos, con
  reintento si falla la validación) con `claude-opus-4-7`. Se agregó una
  confirmación nativa antes de generar la vista previa del trío avisando el
  costo, para que no vuelva a pasar sin querer.
- **Bug real encontrado en el generador de trío**: `SyntaxError: Unexpected
  token '<'` en el navegador — el servidor tiene `gunicorn --timeout 120`
  (`Procfile`) y con hasta 6 llamadas a Claude EN SERIE el trío podia superar
  los 120s, gunicorn mataba el worker a la mitad y devolvia una pagina HTML
  de error en vez de JSON. **Resuelto**: las llamadas por cluster ahora
  corren en paralelo (`ThreadPoolExecutor`, no secuencial) — el tiempo total
  pasa a ser el del cluster mas lento, no la suma de los tres. Se subio
  ademas el timeout de gunicorn a 180s de margen. Ojo: sigue siendo
  `--workers 1`, asi que mientras el trio corre (mas corto ahora, pero no
  instantaneo) el resto de la app queda bloqueada para cualquiera que la use
  — no se toco eso, cambiar el numero de workers tiene implicancias de
  costo/recursos en el plan de Render que no correspondia decidir sin
  preguntar.
- **Pregunta de Guille: "¿puede usar un modelo más económico sin perder
  calidad?"** — `seo_optimizer.py` ya tiene soporte para Haiku
  (`_call_claude(..., fast=True)`), pero su propio docstring dice que Haiku
  es para "análisis/validación estructurada" y Opus para "síntesis creativa,
  títulos y descripciones finales" — el archivo protegido ya decidió que
  para escribir títulos hace falta Opus. En vez de bajar el modelo por mi
  cuenta, se agregó un checkbox opt-in ("Usar modelo económico") en la
  pantalla, apagado por default, para que Guille lo prenda y juzgue la
  calidad él mismo caso por caso. Prueba real con Haiku: fallo 4 validaciones
  (keyword principal ausente, descripcion 4529 vs 1000-1800 esperado, frase
  TIER1 ausente, simbolos prohibidos) — evidencia a favor de la decision
  original del archivo protegido de reservar Opus para esto.
- **Reforzado tras la respuesta de Guille** ("no quiero perder calidad,
  quiero que los títulos estén bien"): la vista previa ya mostraba los
  errores de `validar_sintesis` en rojo, pero `/api/pricing/trio/crear` no
  volvía a chequearlos — se podía crear una publicación real con un título
  que no cumplía las reglas si el usuario ignoraba el aviso. Ahora se
  revalida en el momento de crear (sobre el texto final, por si se editó a
  mano en la vista previa) y se bloquea esa publicación puntual si sigue sin
  cumplir.
- Mapa de decisión con etiquetas dentro de la tabla (antes solo en una frase
  arriba) y celdas con signo + $ explícito en vez de solo color.
- Equivalente mensual ("≈X por mes") al lado de todos los números de
  ventas/día — la fracción diaria no era intuitiva.
- **`ventas_dia_7d`** agregado a `/api/pricing/contexto`: promedio real de
  los últimos 7 días (filtra las mismas órdenes que ya trae `_get_all_orders_30d`,
  sin pedir nada de más a ML) además del de 30 días que ya existía. Se
  muestra en pantalla y se usa para calcular "cuántas ventas MÁS por día
  hacen falta" respecto al ritmo real de la última semana, no un número
  absoluto — pedido explícito de Guille.
- **Bug real en `/api/pricing/recomendar`**: `max_tokens=700` se quedaba
  corto — si Opus escribe algo de preámbulo antes del JSON (pasa aunque el
  prompt pida "solo JSON"), el JSON queda cortado a mitad y `json.loads()`
  explota, pero el llamado ya se facturó igual. Guille reportó justo eso:
  "me descuenta crédito pero no funciona". Subido a 2000 tokens, y agregado
  logging de la respuesta cruda si vuelve a fallar el parseo (antes fallaba
  en silencio con el mismo mensaje genérico).
- **El timeout del trío volvió a pasar** (mismo `SyntaxError: Unexpected
  token '<'`) probando el mismo producto de siempre (MLA1932975847), que
  solo tiene 1 cluster de búsqueda con volumen — la paralelización de la
  corrección anterior no ayuda nada cuando hay un solo cluster, no hay nada
  que paralelizar. Lo que sí duplicaba tiempo Y costo en ese único cluster
  era el **reintento automático** cuando fallaba `validar_sintesis`
  (llamaba a Claude una segunda vez sin preguntar). **Eliminado**: ahora es
  una sola llamada por cluster, sin reintento — si la validación falla, se
  devuelve igual con los errores marcados (ya bloqueados en la creación por
  la corrección anterior), y el usuario decide si vuelve a generar. Pedido
  explícito: "me consumió créditos muy caros".
- **Bug real en el prompt de `/api/pricing/recomendar`**: se le mandaba a
  Claude el JSON interno crudo de `resultado['estrategias']`, con campos
  como `motivo_batalla` que solo tienen sentido para "comp"/"vel" (describen
  por qué bajó la Batalla para pelear precio). Para "rent" ese campo se
  computa igual (por como está hecho `estrategia()`, siempre corre
  `precio_batalla()`) pero describe un precio HIPOTÉTICO que "rent" ni
  siquiera usa — Claude lo leyó como si describiera el precio real de
  "rent" y se contradijo solo en la respuesta ("queda muy por debajo del
  competidor... en realidad arriba, pero el motor marca 'debajo_del_competidor'
  por lógica interna"). Reportado por Guille: "esto no es serio". Reescrito
  el armado del prompt: ahora manda un resumen legible en español (sin
  nombres de campos internos) y la nota de "por qué la Batalla quedó en ese
  precio" solo aparece para comp/vel, donde sí aplica.
- De paso: la `situacion_hoy` (ventas reales de 7 y 30 días, ganancia
  actual) nunca se mandaba al prompt aunque el backend ya la tenía — por
  eso Claude decía "sin dato de ventas reales" cuando sí había dato. Ahora
  se incluye explícitamente.
- **Guille prefería la claridad del artifact/prototipo original** por
  sobre lo construido — específicamente: una tarjeta "Tu situación hoy"
  prominente arriba de todo (ganás por venta / ganás por día, en grande),
  y el "medidor" — una línea numérica mostrando dónde cae cada precio de
  la estrategia contra hoy y contra la banda de competencia. Se portó esa
  parte del artifact a `/pricing/existente` (manteniendo Bootstrap, no el
  CSS propio del artifact — esa decisión de estilo ya estaba tomada antes):
  tarjeta "Tu situación hoy" full-width con las dos métricas grandes, y un
  medidor dentro de cada una de las 3 tarjetas de estrategia.

Bugs del §11 de la spec ya sumados a la checklist de arriba (items 51-54); el
de `search_competitors` con `price:0`/`no_active_listings` ya estaba
registrado como correccion 27. Todos **diferidos a pedido de Guille** hasta
terminar los 5 sprints de este bloque — recordarle entonces.

### Cuotas y comisión REALES por producto, no el ejemplo genérico de la spec (2026-09-19)

Guille preguntó de dónde salían los porcentajes de cuotas de la calculadora
y, al enterarse de que eran el ejemplo ilustrativo de la spec original
(comisión 17%, cuotas 0/5,75/10,75%) y no un dato real por producto/rubro,
lo marcó como un problema serio: **"de que me sirve tener una calculadora
que arroje datos falsos... creo que estoy haciendo todo mal"**. Con razón —
esos números nunca fueron reales, eran el placeholder de §10.1 antes de
resolverse la incógnita.

Se verificó en vivo contra la cuenta real (dominio
`MLA-HAIR_CLIPPERS_ELECTRIC_SHAVERS_AND_HAIR_TRIMMERS`, vía
`/sites/MLA/listing_prices` con `domain_id` real) algo que también corrige
un supuesto propio de sprints anteriores: **`percentage_fee` NO es la
comisión pura** — ya viene sumada con el costo de financiar las cuotas.
`sale_fee_details` trae tres campos distintos:

- `meli_percentage_fee`: comisión pura, **constante** en los 6 escalones
  para un mismo `domain_id` (16% para este dominio). Este es el dato
  correcto para "comisión".
- `financing_add_on_fee`: costo real de cuotas por escalón — para este
  dominio: 0% (sin cuotas), 5% (interés bajo — el 4% de la spec/docs
  genéricos de ML NO aplica a este rubro, es 5%), 8,9% (3 cuotas), 13,4%
  (6 cuotas), 17,8% (9 cuotas), 21,6% (12 cuotas).
- `percentage_fee`: la suma de los dos anteriores — sirve para mostrar el
  costo total, no para desglosar comisión vs. cuotas.

Se agregó `tasas_reales_por_escalon(client, domain_id, precio_referencia)`
en `modules/trio_generador.py`, que consulta los 6 escalones de
`CUOTAS_A_TAGS` contra `/sites/MLA/listing_prices` para el `domain_id` real
del producto y devuelve `{escalon: {comision, costo_cuotas}}`.
`/api/pricing/contexto` ahora llama esa función con el `domain_id` del
item, usa el escalón 0 (`meli_percentage_fee`) como `comision_pct` (con
fallback a `get_listing_fee_rate` y por último al caché local si la
llamada falla), y devuelve `domain_id`, `cuotas_reales` y
`cuotas_reales_etiqueta` en la respuesta.

En `/pricing/existente`, la tarjeta "Las 3 publicaciones" ahora tiene un
selector de escalón real (Sin cuotas / Interés bajo / 3/6/9/12 cuotas) por
cada publicación del trío — al elegirlo, completa comisión y cuotas con el
dato real de ML para ese producto en vez del ejemplo genérico. Si no hay
`domain_id` o falla la consulta, se avisa explícitamente con la etiqueta en
amarillo ("Sin datos reales de ML... cargalo a mano") en vez de mostrar el
ejemplo genérico sin aclarar que es un placeholder. Verificado en vivo
contra MLA1932975847: `comision_pct: 16`, los 6 escalones completos y
coincidiendo con la verificación manual previa.

Pendiente: el mismo dato real no está conectado en `pricing_nuevo.html`
(Modo B) porque ahí no hay un item existente del cual sacar `domain_id`
todavía — cuando el usuario elige categoría/dominio en Modo B habría que
repetir esta misma consulta.

### "Vendés hoy (unidades)" como tile propio (2026-09-19)

Guille preguntó por qué no aparecía en ningún lado cuánto vendía por
día/semana, ni contra qué comparaba la calculadora. El dato ya se traía
(`situacion_hoy.ventas_dia_7d`) pero se mostraba como texto chico gris
debajo de "Ganás por día" — fácil de pasar por alto, y si faltaba el costo
esa tarjeta quedaba en "-" y el dato de ventas se perdía ahí abajo. Ahora
es un tile propio en "Tu situación hoy", con el número de ventas/día como
valor principal; si no hubo ventas en 30 días lo dice explícitamente en vez
de mostrar "0.00/día" sin contexto.

### "Resultado de la prueba" rehecho: ventas reales, no a mano (2026-09-19)

Guille: "no se entiende bien... quiero que sea realmente útil para hacer
pruebas". El problema real no era el cálculo (`resultado_prueba()`, ya
testeado) sino la UX: pedía escribir a mano "ventas/día en la prueba", un
número que el usuario tendría que ir a calcular por su cuenta contando
ventas en ML; el botón fallaba con un `alert()` silencioso si todavía no
se habían calculado las 3 estrategias en esta misma carga de página (el
resultado vive en la variable JS `PE_ULTIMO_RESULTADO`, se pierde al
recargar); y el campo "% que se llevó la Batalla" no tenía ninguna
explicación de qué significaba ni cuándo importaba.

Cambios:
- `/api/pricing/verificar_prueba` ahora acepta `alias`+`item_id`+
  `fecha_desde` opcionales: si vienen, trae las órdenes reales de ML
  (mismo `_get_all_orders_30d`/`_compute_item_stats` que usa `/contexto`),
  las filtra desde esa fecha, y calcula `ventas_dia_prueba` real — el
  usuario ya no cuenta nada a mano. Si falla o no se manda fecha, cae al
  campo manual como antes (`fuente_ventas: "manual"` en la respuesta para
  que la UI lo aclare). Ventana de 30 días (mismo límite que
  `_get_all_orders_30d`) — si el precio se probó hace más de 30 días no
  hay con qué medir vía este camino y hay que cargarlo a mano.
- La UI reemplaza "Ventas/día en la prueba" + "Días medidos" (dos campos a
  calcular a mano) por un único selector de fecha "¿Desde cuándo tenés
  este precio?"; el campo manual queda oculto y solo aparece si el cálculo
  automático falla.
- El botón "Ver veredicto" y el selector de estrategia arrancan
  deshabilitados con el texto "Calculá las 3 estrategias arriba primero"
  en vez de fallar con un `alert()` al clickear sin datos.
- El campo de mix ahora tiene un tooltip explicando cuándo y por qué
  importa (solo en "Competir", porque ahí cada publicación del trío deja
  una ganancia distinta).
- El resultado ahora aclara la fuente del dato ("Calculado con ventas
  reales de ML desde el DD/MM (N unidades en M días)" vs. "cargaste a
  mano"), y agrega una nota de confianza si hay menos de 7 días medidos o
  si la diferencia es chica (<5% de la ganancia de hoy) con menos de 14
  días — mismo criterio de cautela que ya usa `veredicto_experimento()`
  para el experimento trackeado automático, aplicado acá al chequeo
  puntual.
- Se aclaró en el texto de la tarjeta la diferencia con "Experimentos de
  este producto" (el tracking automático vía `/aplicar` + snapshots
  diarios, sprint 3/4): esta tarjeta es para un chequeo rápido puntual,
  incluso si el precio se cambió a mano fuera de la app; el otro es el
  seguimiento medido día a día una vez que se aplica un precio desde acá.

Verificado en vivo contra MLA1932975847 (10 días atrás): 22 unidades reales
detectadas automáticamente, `ventas_dia_prueba: 2.2`. Fallback manual
también probado sin alias/item_id.

## Sprints

| Sprint | Contenido | Estado |
|---|---|---|
| A | Memoria (1.1–1.4) + competidores persistentes y huella (2.2–2.4) | codigo listo, falta deploy y validacion |
| B | Bandeja única, sección Cerebro en UI (2.5 + 7), bot Telegram | pendiente |
| C | Motor de precio con postura (3) + cierre del loop (5) + tráfico vs conversión (4) + promociones ex ante (9.2) | pendiente |
| D | Disparadores de competencia, laboratorio de títulos (8), reconstructor de publicación (8.5), precisión del sistema (6), evaluación de promos y cupones (9.3–9.5), semáforo de portafolio (11) | pendiente |

Flujo de trabajo: Claude edita y pushea a `main` desde la sesión (repo agregado
como fuente); Render auto-deploya; el usuario valida con el checklist de
`meli-validar`. Regla #4 (protocolo de 60s) se respeta en cada push.
