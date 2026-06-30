#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC=${EVALSCOPE_SRC:-"$(cd "$ROOT/../.." && pwd)"}
VENV=${EVALSCOPE_VENV:-"$ROOT/.venv"}
HOST=${RCA_DATA_HOST:-127.0.0.1}
PORT=${RCA_DATA_PORT:-18080}
INDEX=${RCA_DATA_INDEX:-$ROOT/generated/rcaeval_data_index_store.json}
LOG=$ROOT/logs/offline-data-api.log
PIDFILE=$ROOT/offline-data-api.pid

mkdir -p "$ROOT/logs"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Offline Data API already running with PID $(cat "$PIDFILE")"
  exit 0
fi

cd "$ROOT"
API_SCRIPT="$SRC/evalscope/benchmarks/rcaeval_rca/offline_data_api.py"
if [[ -f "$API_SCRIPT" ]]; then
  nohup "$VENV/bin/python" "$API_SCRIPT" --index "$INDEX" --host "$HOST" --port "$PORT" >"$LOG" 2>&1 &
else
  PYTHONPATH="$SRC" nohup "$VENV/bin/python" -m evalscope.benchmarks.rcaeval_rca.offline_data_api --index "$INDEX" --host "$HOST" --port "$PORT" >"$LOG" 2>&1 &
fi
echo $! >"$PIDFILE"
echo "Started Offline Data API PID $(cat "$PIDFILE") on http://127.0.0.1:$PORT"
