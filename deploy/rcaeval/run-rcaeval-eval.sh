#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC=${EVALSCOPE_SRC:-"$(cd "$ROOT/../.." && pwd)"}
VENV=${EVALSCOPE_VENV:-"$ROOT/.venv"}
CASES=${RCA_CASES:-$ROOT/generated/rcaeval_cases.jsonl}
TASK_NAME=${TASK_NAME:-rcaeval_full_mock}
WORK_DIR=${WORK_DIR:-$ROOT/outputs/$TASK_NAME}
AGENT_MODE=${AGENT_MODE:-mock}
AGENT_URL=${RCA_AGENT_URL:-${AGENT_URL:-}}
MODEL_ID=${MODEL_ID:-rca-mock-agent}
LIMIT=${LIMIT:-}

DATASET_ARGS=$(cat <<JSON
{"rcaeval_rca":{"local_path":"$CASES","extra_params":{"agent_mode":"$AGENT_MODE","agent_url":"$AGENT_URL"}}}
JSON
)

args=(
  eval
  --datasets rcaeval_rca
  --dataset-args "$DATASET_ARGS"
  --eval-type mock_llm
  --model-id "$MODEL_ID"
  --work-dir "$WORK_DIR"
  --no-timestamp
  --ignore-errors
)

if [[ -n "$LIMIT" ]]; then
  args+=(--limit "$LIMIT")
fi

cd "$ROOT"
PYTHONPATH="$SRC" "$VENV/bin/evalscope" "${args[@]}"
