# CEREBRO — Mensajes para pedir cada sprint

Copiar y pegar de a UNO. Terminar, deployar, validar, y recien ahi el siguiente.
Si se pide todo junto, sale superficial.

Regla comun a todos: la sesion tiene que leer primero `docs/CEREBRO.md` (diseno)
y `docs/CEREBRO_HANDOFF.md` (estado real). Nunca tocar `modules/seo_optimizer.py`.

---

## 1) Completar Sprint A (memoria + competidores)

Lee docs/CEREBRO.md y docs/CEREBRO_HANDOFF.md antes de tocar nada. El Sprint A
esta a medias. Completa estas tres cosas: (1) engancha el registro automatico de
acciones en los flujos que ya ejecutan cambios — Aplicar TODO, repricing,
respuestas a preguntas, pausas por duplicados y lanzador — usando
cerebro.registrar_accion y cerebro.marcar_aplicada, sin cambiar el comportamiento
actual de esos flujos; (2) en el snapshot diario separa visitas organicas de
visitas pagas restando los clics de Product Ads (bloque 4.1), y guarda conversion
organica y conversion Ads por separado; (3) verifica que el job cerebro_evaluar
corre y que price_to_win no dispare rate limit al recorrer todas las
publicaciones de catalogo, con backoff si hace falta. Al terminar decime que
quedo implementado, que no, y que tengo que validar en produccion. No toques
seo_optimizer.py.

---

## 2) Sprint B (bandeja unica + seccion Cerebro + Telegram)

Lee docs/CEREBRO.md y docs/CEREBRO_HANDOFF.md. Implementa el Sprint B: (1) una
sola cola de propuestas con estados pendiente/aprobada/rechazada/aplicada, que es
la misma para la web y para Telegram — aprobar en un lado tiene que quedar
aprobado en el otro porque es el mismo registro; (2) seccion "Cerebro" en
ml-system respetando el design system existente, con tres vistas: bandeja de
propuestas, historial de acciones con su veredicto, y panel de aprendizajes; mas
la pantalla de competidores por publicacion del bloque 2.5 (tarjetas, sin tablas,
maximo 5 candidatos, botones es competidor / no lo es / sustituto); (3) bot de
Telegram con resumen matutino y botones aprobar/rechazar, token en variable de
entorno TELEGRAM_BOT_TOKEN. Al terminar decime que valido en produccion.

---

## 3) Sprint C (motor de precio + loop cerrado + promos ex ante)

Lee docs/CEREBRO.md y docs/CEREBRO_HANDOFF.md. Implementa el Sprint C: (1) motor
de precio del bloque 3 con postura por publicacion (lider / paridad / premium, y
en catalogo ganar_siempre / ganar_si_conviene / podio), las situaciones de 3.3,
los limites de seguridad de 3.4 y el menu de palancas de 9.6 — cupon, cuotas,
envio, esperar — eligiendo la mas barata en margen; respeta umbrales de precio de
ML y costo de reposicion (bloques 10.1 y 10.2); todo en modo propuesta, nada
automatico; (2) que top_acciones y el Veredicto lean aprendizajes.json y ordenen
por impacto observado en la cuenta (bloque 5); (3) diagnostico automatico trafico
vs conversion con demanda de mercado (4.2); (4) propuesta de participacion en
Central de Promociones (9.2). Al terminar decime que valido.

---

## 4) Sprint D (competencia, titulos, promos ex post, portafolio)

Lee docs/CEREBRO.md y docs/CEREBRO_HANDOFF.md. Implementa el Sprint D: (1)
disparadores de competencia (bajo precio, entrante nuevo, sin stock, oportunidad
de subir) con accion sugerida asociada; (2) laboratorio de titulos para
publicaciones nuevas y clones, con hipotesis explicita y evaluacion a 14 dias
(bloque 8); (3) evaluacion ex post de promos y cupones (9.3-9.5); (4) semaforo de
portafolio del bloque 11 (estrella / recomendado / en observacion / no
recomendable / nuevo) con su vista y sus propuestas; (5) indicador de precision
del sistema y promocion de acciones a automatico con >=10 evaluaciones y >=70% de
acierto (bloque 6). Al terminar decime que valido.

---

## 5) Para revisar trabajo entregado

Compara lo que implementaste contra docs/CEREBRO.md bloque por bloque y decime,
sin adornos: que quedo completo, que quedo parcial, que no se hizo y por que.
Agrega a la tabla "Correcciones detectadas" de docs/CEREBRO.md cualquier cosa que
hayas encontrado rota o riesgosa en el sistema mientras trabajabas.
