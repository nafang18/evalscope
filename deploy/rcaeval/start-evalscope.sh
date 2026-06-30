#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC=${EVALSCOPE_SRC:-"$(cd "$ROOT/../.." && pwd)"}
VENV=${EVALSCOPE_VENV:-"$ROOT/.venv"}
HOST=${EVALSCOPE_HOST:-0.0.0.0}
PORT=${EVALSCOPE_PORT:-9000}
OUTPUTS=${EVALSCOPE_OUTPUTS:-$ROOT/outputs}
LOG=$ROOT/logs/evalscope-service.log
PIDFILE=$ROOT/evalscope-service.pid

mkdir -p "$ROOT/logs" "$OUTPUTS"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "EvalScope already running with PID $(cat "$PIDFILE")"
  exit 0
fi

cd "$ROOT"
PYTHONPATH="$SRC" nohup "$VENV/bin/evalscope" service --host "$HOST" --port "$PORT" --outputs "$OUTPUTS" >"$LOG" 2>&1 &
echo $! >"$PIDFILE"
echo "Started EvalScope service PID $(cat "$PIDFILE") on http://127.0.0.1:$PORT/dashboard"
