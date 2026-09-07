#!/bin/bash
# Vigilante de push controlado.
# SEGURIDAD: NO usa "git add -A" (barria trabajo a medio hacer de otra sesion).
# Marcador .claude_push: 1ra linea = rutas a commitear (separadas por espacios),
# de la 2da linea en adelante = mensaje del commit.
REPO="$HOME/Desktop/claude/ml_system"
LOG="$REPO/scripts/autopush.log"
cd "$REPO" || exit 0
echo "tick $(date '+%H:%M:%S')" >> "$LOG"
[ -f .claude_push ] || exit 0
if [ -f .git/index.lock ]; then echo "lock presente, espero" >> "$LOG"; exit 0; fi
PATHS="$(head -1 .claude_push)"
MSG="$(tail -n +2 .claude_push)"
rm -f .claude_push
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') === paths: $PATHS"
  git add -- $PATHS 2>&1 | tail -2
  git commit -m "$MSG" 2>&1 | tail -2
  git push origin main 2>&1 | tail -3
  echo "--- fin ---"
} >> "$LOG" 2>&1
