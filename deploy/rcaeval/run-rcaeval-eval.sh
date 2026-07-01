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
AGENT_POLL_URL=${RCA_AGENT_POLL_URL:-${AGENT_POLL_URL:-}}
AGENT_POLL_METHOD=${AGENT_POLL_METHOD:-GET}
POLL_INTERVAL_SECONDS=${POLL_INTERVAL_SECONDS:-60}
MAX_WAIT_SECONDS=${MAX_WAIT_SECONDS:-3600}
TERMINAL_STATUSES=${TERMINAL_STATUSES:-completed,complete,finished,succeeded,success,done}
FAILED_STATUSES=${FAILED_STATUSES:-failed,error,cancelled,canceled,timeout}
TASK_ID_PATH=${TASK_ID_PATH:-}
STATUS_PATH=${STATUS_PATH:-}
ANSWER_PATH=${ANSWER_PATH:-}
TRACE_PATH=${TRACE_PATH:-}
NORMALIZE_WITH_AI=${NORMALIZE_WITH_AI:-false}
NORMALIZER_API_URL=${RCA_NORMALIZER_API_URL:-${NORMALIZER_API_URL:-}}
NORMALIZER_API_KEY=${RCA_NORMALIZER_API_KEY:-${NORMALIZER_API_KEY:-}}
NORMALIZER_MODEL=${RCA_NORMALIZER_MODEL:-${NORMALIZER_MODEL:-}}
MODEL_ID=${MODEL_ID:-rca-mock-agent}
LIMIT=${LIMIT:-}

DATASET_ARGS=$(cat <<JSON
{"rcaeval_rca":{"local_path":"$CASES","extra_params":{"agent_mode":"$AGENT_MODE","agent_url":"$AGENT_URL","agent_poll_url":"$AGENT_POLL_URL","agent_poll_method":"$AGENT_POLL_METHOD","poll_interval_seconds":$POLL_INTERVAL_SECONDS,"max_wait_seconds":$MAX_WAIT_SECONDS,"terminal_statuses":"$TERMINAL_STATUSES","failed_statuses":"$FAILED_STATUSES","task_id_path":"$TASK_ID_PATH","status_path":"$STATUS_PATH","answer_path":"$ANSWER_PATH","trace_path":"$TRACE_PATH","normalize_with_ai":$NORMALIZE_WITH_AI,"normalizer_api_url":"$NORMALIZER_API_URL","normalizer_api_key":"$NORMALIZER_API_KEY","normalizer_model":"$NORMALIZER_MODEL"}}}
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
