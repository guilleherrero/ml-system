# Guía de Regresión — Optimizar IA

Test estructural para detectar cambios involuntarios en el motor SEO
(`modules/seo_optimizer.py`) y sus constantes críticas.

**Costo:** $0 · **Duración:** ~2 segundos · **Llamadas externas:** ninguna.

---

## Cómo correrlo

Un solo comando desde la raíz del proyecto:

```bash
bash tests/run_regresion.sh
```

O directamente:

```bash
python3 tests/test_regresion_rapido.py
```

**Exit code:**

- `0` → todas las aserciones pasaron (PASS o PASS-with-warnings)
- `1` → al menos una aserción crítica falló

---

## Qué valida exactamente

1. `modules/seo_optimizer.py` se importa sin errores
2. Las **12 funciones públicas** de M1 a M7 existen y son callables:
   `get_autosuggest_keywords`, `score_and_classify_keywords`,
   `track_positions`, `fetch_competitors_full`, `fetch_competitor_qa`,
   `analyze_qa_insights`, `analyze_competitor_patterns`,
   `analyze_root_causes`, `calculate_difficulty`, `calculate_ml_score`,
   `audit_title`, `run_full_optimization`
3. Las **constantes críticas** están definidas:
   `_ML_SCORE_WEIGHTS`, `_CATEGORY_CONTEXT`,
   `_TRANSACTIONAL_SIGNALS`, `_ATTRIBUTE_WORDS`
4. `_ML_SCORE_WEIGHTS` suma exactamente **100** y tiene las 6 claves
   esperadas: `attrs_required`, `attrs_optional`, `title_keywords`,
   `photos`, `free_shipping`, `catalog_match`
5. `_CATEGORY_CONTEXT` tiene los **10 nichos** esperados
   (moda, electrónica, hogar, deporte, belleza, bebé, auto,
   herramienta, salud, mascota), cada uno con `kws + faqs + objections`
6. **Hash MD5** de `seo_optimizer.py` se compara contra
   `tests/.optimizer_hash`. Si cambió, alerta como **WARNING**
   (no fail) y pide confirmar que fue intencional.

---

## Cuándo correrlo — OBLIGATORIO

### 🟢 SIEMPRE en estos momentos

- **Antes de empezar cualquier cambio** que toque:
  - `modules/seo_optimizer.py`
  - `web/app.py` (en especial las rutas `/api/optimizar-pub*`)
  - `web/templates/optimizaciones.html`
  - prompts de Claude
  - cualquier helper que el motor SEO use

- **Después de cada cambio en esos archivos**, antes de comitear

- **Antes de mergear a main** una branch que haya tocado el motor

- **Después de un `git pull`** si bajaste cambios de otros

### Si el test falla

1. **No comitees.** El motor está roto.
2. Leé el output: indica qué aserción falló y por qué.
3. Si fue cambio involuntario → **rollback al backup**:

   ```bash
   git checkout backup-pre-auditoria-20260501 -- modules/seo_optimizer.py
   ```

4. Si fue intencional → corregí la aserción del test antes de mergear.

### Si el hash cambió (warning, no fail)

El warning aparece cuando `seo_optimizer.py` se modificó (aunque las
aserciones sigan pasando). Tres opciones:

- **Cambio intencional y aprobado** → actualizá el hash:
  ```bash
  rm tests/.optimizer_hash
  bash tests/run_regresion.sh   # regenera el hash
  ```
- **Cambio involuntario** → `git diff modules/seo_optimizer.py` y revertir.
- **No estás seguro** → preguntar antes de continuar.

---

## Validación manual post-sprint (cuando se tocó Optimizar IA)

El test rápido **no** ejecuta una optimización real. Para validar que
el flujo end-to-end sigue funcionando después de un sprint que tocó
Optimizar IA, hacer **validación manual en el sistema en producción**:

1. Abrir el sistema (Render/local) e ir a **Optimizar IA**
2. Elegir cualquier publicación de Novara y ejecutar la optimización completa
3. Verificar que el output sigue mostrando todos estos bloques:

   - [ ] **3 títulos optimizados** con justificación de cuál recomienda la IA
   - [ ] **Score actual** y **score proyectado** (ambos numéricos, 0-100)
   - [ ] **Análisis de catálogo** (si aplica al producto)
   - [ ] **Recomendaciones de fotos** con cantidad y tipos
   - [ ] **Ficha técnica completa** con atributos
   - [ ] **Descripción superadora** larga y estructurada
   - [ ] **Acciones recomendadas** (correcciones de título, precio, etc.)

4. Si **todo se ve bien** → mergear el sprint.
5. Si **falta algún bloque o se ve raro** → no mergear, investigar primero.

**Cuándo hacer validación manual obligatoria:**

- Al cerrar un sprint que tocó `seo_optimizer.py`, `app.py` (rutas de
  Optimizar IA), `optimizaciones.html`, o cualquier prompt de Claude.
- Antes de cada deploy a producción si la branch tocó esos archivos.

**Cuándo no hace falta:**

- Cambios solo en CSS o textos UI fuera de `optimizaciones.html`
- Cambios en módulos no relacionados (Monitor, repricing, preguntas, etc.)
- Refactors de código no productivo

---

## Por qué no hay test "completo" automatizado

Un test que ejecute `run_full_optimization()` real costaría
**~$0.50–$1.50 USD por corrida** (Claude Opus + 60-180 segundos).
Correrlo después de cada commit no es viable económicamente.

La estrategia adoptada:

- **Regresión estructural automatizada** (este test, gratis) → captura
  cambios accidentales en funciones, constantes y hash del motor.
- **Validación funcional manual** post-sprint → confirma que el flujo
  end-to-end sigue produciendo output válido, sin gastar API ni tiempo
  en cada commit.

Esta combinación cubre el riesgo real (regresión silenciosa del motor)
sin generar costos recurrentes innecesarios.
