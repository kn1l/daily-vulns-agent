#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${DAILY_VULNS_TMUX_SESSION:-daily-vulns-web}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${DAILY_VULNS_HOST:-0.0.0.0}"
PORT="${DAILY_VULNS_PORT:-8000}"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "tmux session already exists: $SESSION_NAME"
  exit 0
fi

tmux new-session -d -s "$SESSION_NAME" -c "$APP_DIR" \
  "PYTHONPATH=src uv run uvicorn daily_vulns_agent.web:app --host '$HOST' --port '$PORT'"

echo "started tmux session: $SESSION_NAME"
