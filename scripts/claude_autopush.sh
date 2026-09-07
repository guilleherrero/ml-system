#!/bin/bash
REPO="$HOME/Desktop/claude/ml_system"
LOG="$REPO/scripts/autopush.log"
cd "$REPO" || exit 0
echo "tick $(date '+%H:%M:%S')" >> "$LOG"
[ -f .claude_push ] || exit 0
MSG="$(cat .claude_push)"
rm -f .claude_push
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
  git checkout -q main 2>&1
  git add -A 2>&1
  git commit -m "$MSG" 2>&1 | tail -2
  git pull --rebase origin main 2>&1 | tail -2
  git push origin main 2>&1 | tail -3
  echo "--- fin ---"
} >> "$LOG" 2>&1
