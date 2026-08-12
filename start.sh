#!/bin/bash
# Arranca el servidor Flask con hot-reload activado.
# Cada vez que se guarda un .py o .html, el servidor se reinicia solo.

cd "$(dirname "$0")"

export TZ=America/Argentina/Buenos_Aires
export FLASK_ENV=development

exec python3 -m flask --app web/app.py run \
  --host 0.0.0.0 \
  --port 5000 \
  --reload \
  --exclude-patterns "data/*" \
  --exclude-patterns "*.json" \
  2>&1
