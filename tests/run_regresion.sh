#!/usr/bin/env bash
# Wrapper para ejecutar el test de regresión rápido.
# Devuelve exit code 0 si pasa, 1 si falla.
# Costo: $0 · Duración: ~2 segundos.

set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

cd "$ROOT"
exec python3 tests/test_regresion_rapido.py
