#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC=${EVALSCOPE_SRC:-"$(cd "$ROOT/../.." && pwd)"}
VENV=${EVALSCOPE_VENV:-"$ROOT/.venv"}
DATASET=${OPSEVAL_DATASET:-$ROOT/generated/opseval.jsonl}
TASK_NAME=${TASK_NAME:-opseval_mixed}
WORK_DIR=${WORK_DIR:-$ROOT/outputs/$TASK_NAME}
MODEL_ID=${MODEL_ID:-ops-eval-model}
MODEL_API_URL=${MODEL_API_URL:-${API_URL:-}}
MODEL_API_KEY=${MODEL_API_KEY:-${API_KEY:-}}
JUDGE_API_URL=${JUDGE_API_URL:-}
JUDGE_API_KEY=${JUDGE_API_KEY:-}
JUDGE_MODEL=${JUDGE_MODEL:-}
MCQ_WEIGHT=${MCQ_WEIGHT:-0.5}
QA_WEIGHT=${QA_WEIGHT:-0.5}
LIMIT=${LIMIT:-}

DATASET_ARGS=$(cat <<JSON
{"opseval":{"local_path":"$DATASET","extra_params":{"mcq_weight":$MCQ_WEIGHT,"qa_weight":$QA_WEIGHT,"judge_api_url":"$JUDGE_API_URL","judge_api_key":"$JUDGE_API_KEY","judge_model":"$JUDGE_MODEL","judge_timeout":120}}}
JSON
)

args=(
  eval
  --datasets opseval
  --dataset-args "$DATASET_ARGS"
  --model-id "$MODEL_ID"
  --work-dir "$WORK_DIR"
  --no-timestamp
)

if [[ -n "$MODEL_API_URL" ]]; then
  args+=(--api-url "$MODEL_API_URL")
fi
if [[ -n "$MODEL_API_KEY" ]]; then
  args+=(--api-key "$MODEL_API_KEY")
fi
if [[ -n "$LIMIT" ]]; then
  args+=(--limit "$LIMIT")
fi

cd "$ROOT"
PYTHONPATH="$SRC" "$VENV/bin/evalscope" "${args[@]}"
