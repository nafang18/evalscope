#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for name in evalscope-service offline-data-api; do
  pidfile="$ROOT/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name running PID $(cat "$pidfile")"
  else
    echo "$name stopped"
  fi
done

curl -fsS --max-time 3 http://127.0.0.1:9000/health || true
echo
curl -fsS --max-time 3 http://127.0.0.1:18080/health || true
echo
