"""
Test de regresión RÁPIDO — validación estructural del motor de Optimizar IA.

NO hace llamadas a Claude API ni a MercadoLibre.
Costo: $0 · Duración: ~2 segundos.

Valida que:
  1. modules/seo_optimizer.py se importa sin errores
  2. Las 12 funciones públicas (M1 a M7 + auditor + score) existen y son callables
  3. Las constantes críticas (_ML_SCORE_WEIGHTS, _CATEGORY_CONTEXT,
     _TRANSACTIONAL_SIGNALS, _ATTRIBUTE_WORDS) están definidas
  4. _CATEGORY_CONTEXT tiene exactamente los 10 nichos esperados
  5. Los pesos de _ML_SCORE_WEIGHTS suman exactamente 100
  6. El hash MD5 del archivo coincide con el guardado en .optimizer_hash
     (warning si difiere, no fail)

Uso:
    python3 tests/test_regresion_rapido.py
    bash   tests/run_regresion_rapido.sh

Exit code:
    0 = todas las aserciones pasaron
    1 = al menos una aserción falló
"""

import hashlib
import os
import sys
import time

# Permitir importar el módulo desde la raíz del proyecto
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Evitar fallar la importación si no hay API key del entorno (este test no la usa)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-no-llamadas-reales")

OPTIMIZER_PATH = os.path.join(ROOT, "modules", "seo_optimizer.py")
HASH_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".optimizer_hash")

GREEN = "\033[92m"
RED   = "\033[91m"
YEL   = "\033[93m"
RESET = "\033[0m"
DIM   = "\033[2m"

failures = []
warnings = []
start    = time.time()


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {GREEN}✓{RESET} {name}")
    else:
        print(f"  {RED}✗ {name}{RESET}{(' — ' + detail) if detail else ''}")
        failures.append(name)


def warn(name: str, detail: str) -> None:
    print(f"  {YEL}⚠ {name}{RESET} — {detail}")
    warnings.append(name)


# ── 1. Importación del módulo ───────────────────────────────────────────────
print(f"\n{DIM}── 1. Importación del módulo ──{RESET}")
try:
    from modules import seo_optimizer as opt
    check("seo_optimizer importado sin errores", True)
except Exception as e:
    check("seo_optimizer importado sin errores", False, f"excepción: {e}")
    print(f"\n{RED}Importación falló — abortando resto del test.{RESET}\n")
    sys.exit(1)


# ── 2. Funciones públicas (M1 a M7 + score + auditor + entrypoint) ──────────
print(f"\n{DIM}── 2. Funciones públicas ──{RESET}")
EXPECTED_FUNCTIONS = [
    "get_autosuggest_keywords",      # M1
    "score_and_classify_keywords",   # M1
    "track_positions",               # M2
    "fetch_competitors_full",        # M3
    "fetch_competitor_qa",           # M3.5
    "analyze_qa_insights",           # M3.5
    "analyze_competitor_patterns",   # M3
    "analyze_root_causes",           # M4
    "calculate_difficulty",          # M5
    "calculate_ml_score",            # Score
    "audit_title",                   # Auditor
    "run_full_optimization",         # Orquestador
]

for fn_name in EXPECTED_FUNCTIONS:
    has_fn = hasattr(opt, fn_name) and callable(getattr(opt, fn_name))
    check(f"función {fn_name}() existe y es callable", has_fn)


# ── 3. Constantes críticas presentes ────────────────────────────────────────
print(f"\n{DIM}── 3. Constantes críticas presentes ──{RESET}")
EXPECTED_CONSTANTS = [
    "_ML_SCORE_WEIGHTS",
    "_CATEGORY_CONTEXT",
    "_TRANSACTIONAL_SIGNALS",
    "_ATTRIBUTE_WORDS",
]
for const_name in EXPECTED_CONSTANTS:
    has_const = hasattr(opt, const_name)
    check(f"constante {const_name} definida", has_const)


# ── 4. Pesos del score suman 100 ────────────────────────────────────────────
print(f"\n{DIM}── 4. Pesos del score ML ──{RESET}")
weights = getattr(opt, "_ML_SCORE_WEIGHTS", {})
total = sum(weights.values()) if isinstance(weights, dict) else -1
check(f"_ML_SCORE_WEIGHTS suma 100 (suma actual: {total})", total == 100)

EXPECTED_WEIGHT_KEYS = {
    "attrs_required", "attrs_optional", "title_keywords",
    "photos", "free_shipping", "catalog_match",
}
actual_keys = set(weights.keys()) if isinstance(weights, dict) else set()
check(
    "_ML_SCORE_WEIGHTS contiene las 6 claves esperadas",
    actual_keys == EXPECTED_WEIGHT_KEYS,
    f"diferencia: {actual_keys ^ EXPECTED_WEIGHT_KEYS}",
)


# ── 5. _CATEGORY_CONTEXT tiene los 10 nichos esperados ──────────────────────
print(f"\n{DIM}── 5. Nichos en _CATEGORY_CONTEXT ──{RESET}")
ctx = getattr(opt, "_CATEGORY_CONTEXT", [])
check(f"_CATEGORY_CONTEXT tiene 10 nichos (cantidad actual: {len(ctx)})", len(ctx) == 10)

# Validar por palabra-ancla característica de cada nicho (robusto a variantes
# menores de redacción). Cada tuple es (nombre del nicho, lista de kws ancla;
# basta con que aparezca alguna en los kws del nicho)
EXPECTED_NICHOS = [
    ("moda",         ["moda", "ropa", "calzado"]),
    ("electronica",  ["celular", "notebook", "computadora", "tv", "audio"]),
    ("hogar",        ["hogar", "mueble", "deco"]),
    ("deporte",      ["deporte", "fitness", "running"]),
    ("belleza",      ["belleza", "cosmetico", "maquillaje"]),
    ("bebe",         ["bebe", "niño", "juguete", "infantil"]),
    ("auto",         ["auto", "moto", "vehiculo"]),
    ("herramienta",  ["herramienta", "construccion", "taladro"]),
    ("salud",        ["salud", "medico", "ortopedico"]),
    ("mascota",      ["mascota", "perro", "gato"]),
]

# Para cada nicho esperado, buscar al menos un nicho en CTX que contenga
# alguna de las kws-ancla. Esto valida cobertura sin acoplarse al orden o
# a redacciones literales.
for nicho_nombre, ancla_kws in EXPECTED_NICHOS:
    encontrado = any(
        any(ak in (n.get("kws") or []) for ak in ancla_kws)
        for n in ctx
        if isinstance(n, dict)
    )
    check(f"nicho '{nicho_nombre}' presente en _CATEGORY_CONTEXT", encontrado)

# Validar que cada nicho tiene faqs y objections (estructura esperada)
ctx_estructura_ok = all(
    isinstance(n, dict) and n.get("kws") and n.get("faqs") and n.get("objections")
    for n in ctx
)
check("cada nicho tiene kws + faqs + objections", ctx_estructura_ok)


# ── 6. Hash MD5 del archivo seo_optimizer.py ────────────────────────────────
print(f"\n{DIM}── 6. Hash MD5 de seo_optimizer.py ──{RESET}")
with open(OPTIMIZER_PATH, "rb") as f:
    actual_hash = hashlib.md5(f.read()).hexdigest()

if not os.path.exists(HASH_FILE):
    with open(HASH_FILE, "w") as f:
        f.write(actual_hash)
    print(f"  {DIM}primer corrida: hash guardado en .optimizer_hash{RESET}")
    check("hash baseline creado", True)
else:
    with open(HASH_FILE) as f:
        saved_hash = f.read().strip()
    if actual_hash == saved_hash:
        check(f"hash sin cambios ({actual_hash[:12]}...)", True)
    else:
        warn(
            "hash de seo_optimizer.py CAMBIÓ",
            f"anterior {saved_hash[:12]}... → actual {actual_hash[:12]}... "
            "→ ejecutá test_regresion_completo.py para validar comportamiento",
        )

print(f"\n  {DIM}hash actual: {actual_hash}{RESET}")


# ── Resultado final ─────────────────────────────────────────────────────────
elapsed = time.time() - start
print()
print("─" * 60)
if failures:
    print(f"{RED}✗ FAIL{RESET} — {len(failures)} aserción(es) fallaron en {elapsed:.2f}s")
    for f in failures:
        print(f"   · {f}")
    if warnings:
        print(f"{YEL}{len(warnings)} warning(s):{RESET} {', '.join(warnings)}")
    sys.exit(1)
elif warnings:
    print(f"{GREEN}✓ PASS{RESET} con {YEL}{len(warnings)} warning(s){RESET} en {elapsed:.2f}s")
    for w in warnings:
        print(f"   · {w}")
    sys.exit(0)
else:
    print(f"{GREEN}✓ PASS{RESET} — todas las aserciones pasaron en {elapsed:.2f}s")
    sys.exit(0)
