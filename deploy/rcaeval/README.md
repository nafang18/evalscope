# RCAEval EvalScope Deployment Runbook

This directory is a code-only deployment workspace for evaluating RCA agents on
RCAEval data through EvalScope.

It intentionally does not include:

- RCAEval raw data
- generated benchmark JSONL/index files
- Parquet offline stores
- evaluation outputs
- logs or PID files

Use this document as the handoff runbook for a fresh machine.

## 1. Recommended Environment

Use Ubuntu or WSL Ubuntu. The scripts are bash scripts and were validated in WSL.

Recommended resources:

- Python 3.10+
- 16 GB memory or more
- 80 GB free disk if generating the full offline store from raw RCAEval data

The examples below assume the repository has been cloned and you are in:

```bash
cd deploy/rcaeval
```

All generated runtime files will be created under this directory.

## 2. Directory Layout

Expected layout after setup:

```text
evalscope/
  deploy/
    rcaeval/
      README.md
      data/                         # user-provided raw RCAEval data
      generated/
        rcaeval_cases.jsonl
        rcaeval_data_index.json
        rcaeval_data_index_store.json
        store_v1/
      outputs/
      logs/
      .venv/
```

The raw RCAEval data must be placed here:

```text
deploy/rcaeval/data
```

The data directory should contain suites like:

```text
RE1-OB/
RE1-SS/
RE1-TT/
RE2-OB/
RE2-SS/
RE2-TT/
RE3-OB/
RE3-SS/
RE3-TT/
```

Each case directory should look similar to:

```text
RE2-OB/checkoutservice_delay/1/simple_metrics.csv
RE2-OB/checkoutservice_delay/1/logs.csv
RE2-OB/checkoutservice_delay/1/traces.csv
RE2-OB/checkoutservice_delay/1/inject_time.txt
```

## 3. Install Dependencies

From `deploy/rcaeval`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install -e ../..
pip install fastapi uvicorn pandas pyarrow
```

Quick check:

```bash
.venv/bin/python --version
.venv/bin/python -c "import pyarrow, fastapi; print('deps ok')"
```

## 4. Generate EvalScope Benchmark Files

This step scans raw RCAEval data and creates EvalScope cases plus an offline
data index.

```bash
mkdir -p generated

.venv/bin/python ../../evalscope/benchmarks/rcaeval_rca/generate_benchmark.py \
  --raw-root data \
  --cases-output generated/rcaeval_cases.jsonl \
  --index-output generated/rcaeval_data_index.json \
  --endpoint-base http://127.0.0.1:18080
```

Expected output for the complete RCAEval dataset:

```text
Generated 735 RCAEval cases
```

Verify:

```bash
wc -l generated/rcaeval_cases.jsonl
```

The expected count is `735`.

## 5. Generate The Offline Data Store

This converts large CSV telemetry files into Parquet and creates
`generated/rcaeval_data_index_store.json`, which is used by the Offline Data
API.

```bash
.venv/bin/python ../../evalscope/benchmarks/rcaeval_rca/prepare_offline_store.py \
  --index generated/rcaeval_data_index.json \
  --store-root generated/store_v1 \
  --output-index generated/rcaeval_data_index_store.json
```

This can take a while for the full dataset. It is safe to rerun without
`--force`; existing Parquet files will be reused.

Use `--limit` for a smoke conversion:

```bash
.venv/bin/python ../../evalscope/benchmarks/rcaeval_rca/prepare_offline_store.py \
  --index generated/rcaeval_data_index.json \
  --store-root generated/store_v1 \
  --output-index generated/rcaeval_data_index_store.json \
  --limit 10
```

If you regenerated `rcaeval_data_index.json` but already have `store_v1`, you
can rebuild only the store metadata:

```bash
.venv/bin/python ../../evalscope/benchmarks/rcaeval_rca/merge_store_index.py \
  --new-index generated/rcaeval_data_index.json \
  --store-root generated/store_v1 \
  --output-index generated/rcaeval_data_index_store.json
```

## 6. Start Offline Data API

```bash
./start-offline-data-api.sh
```

Health check:

```bash
curl http://127.0.0.1:18080/health
```

Expected:

```json
{"status":"ok","cases":735}
```

Inspect one case:

```bash
curl http://127.0.0.1:18080/cases/rcaeval_re1-ob_0001 | python3 -m json.tool
```

Inspect available data for one case:

```bash
curl http://127.0.0.1:18080/cases/rcaeval_re1-ob_0001/catalog | python3 -m json.tool
```

Query metrics:

```bash
curl -s -X POST \
  http://127.0.0.1:18080/cases/rcaeval_re1-ob_0001/metrics/query \
  -H 'Content-Type: application/json' \
  -d '{"limit": 2}' | python3 -m json.tool
```

## 7. Start EvalScope Web Service

```bash
./start-evalscope.sh
```

Open:

```text
http://127.0.0.1:9000/dashboard
```

Check status:

```bash
./status.sh
```

Stop services:

```bash
./stop-evalscope.sh
./stop-offline-data-api.sh
```

## 8. Run A Smoke Evaluation From CLI

The default script uses the built-in mock RCA agent. It validates that EvalScope
can load the dataset, call the adapter, score predictions, and write reports.

```bash
LIMIT=10 ./run-rcaeval-eval.sh
```

Outputs are written under:

```text
deploy/rcaeval/outputs/rcaeval_full_mock
```

## 9. Run An External Async HTTP RCA Agent

EvalScope calls the external RCA agent with a JSON body that includes:

```json
{
  "case_id": "rcaeval_re1-ob_0001",
  "source": "RCAEval",
  "dataset": "RE1-OB",
  "suite": "RE1-OB",
  "system": "online-boutique",
  "inject_time": 1685202688,
  "start_time": 1685202388,
  "end_time": 1685202988,
  "modalities": ["metrics"],
  "data_endpoint": "http://127.0.0.1:18080/cases/rcaeval_re1-ob_0001",
  "question": "..."
}
```

For `AGENT_MODE=async_http`, EvalScope starts an external agent task, then polls
for its trajectory and final answer. The start endpoint receives the same case
payload shown above and should return a task id:

```json
{
  "task_id": "rcaeval_re1-ob_0001-20260701",
  "status": "running"
}
```

The poll endpoint may be either a templated URL such as
`http://127.0.0.1:7000/api/v1/diagnose/{task_id}` or a plain URL. For a plain
GET URL, EvalScope appends `task_id` and `case_id` as query parameters.

Each poll response should include `status`, and may include `trace` while the
agent is still running:

```json
{
  "task_id": "rcaeval_re1-ob_0001-20260701",
  "status": "running",
  "trace": [
    {
      "event": "query",
      "source": "metrics",
      "operation": "aggregate",
      "finding": "adservice CPU usage increased"
    }
  ]
}
```

When finished, return a terminal status and the final answer under `answer`,
`result`, `final_answer`, `output`, or `prediction`:

```json
{
  "task_id": "rcaeval_re1-ob_0001-20260701",
  "status": "completed",
  "trace": [],
  "answer": {
    "case_id": "rcaeval_re1-ob_0001",
    "root_cause_component": "adservice",
    "root_cause_type": "cpu",
    "root_cause_indicator_family": "cpu",
    "root_cause_indicator": "adservice_cpu",
    "ranked_root_cause_components": ["adservice"],
    "ranked_root_cause_indicators": ["adservice_cpu"],
    "confidence": 0.9,
    "evidence": [],
    "causal_path": [],
    "recommended_fix": "..."
  }
}
```

Run with async HTTP mode:

```bash
AGENT_MODE=async_http \
RCA_AGENT_URL=http://127.0.0.1:7000/api/v1/diagnose/start \
RCA_AGENT_POLL_URL=http://127.0.0.1:7000/api/v1/diagnose/status \
POLL_INTERVAL_SECONDS=60 \
MAX_WAIT_SECONDS=3600 \
LIMIT=10 \
./run-rcaeval-eval.sh
```

If your response fields are nested, configure dotted paths:

```bash
TASK_ID_PATH=data.task_id \
STATUS_PATH=data.status \
ANSWER_PATH=data.answer \
TRACE_PATH=data.trace \
./run-rcaeval-eval.sh
```

If the external agent returns a free-form or framework-specific answer, enable
the chat-completions-compatible normalizer. The normalizer receives the case metadata,
poll snapshots, trace, and raw agent answer, then emits the standard RCA JSON
used by the scorer.

```bash
NORMALIZE_WITH_AI=true \
RCA_NORMALIZER_API_URL=http://127.0.0.1:8000/v1 \
RCA_NORMALIZER_API_KEY=YOUR_KEY \
RCA_NORMALIZER_MODEL=YOUR_MODEL \
./run-rcaeval-eval.sh
```

If the CLI script is not yet wired to your desired agent URL, pass dataset args
directly:

```bash
.venv/bin/evalscope eval \
  --datasets rcaeval_rca \
  --dataset-args '{"rcaeval_rca":{"local_path":"generated/rcaeval_cases.jsonl","extra_params":{"agent_mode":"async_http","agent_url":"http://127.0.0.1:7000/api/v1/diagnose/start","agent_poll_url":"http://127.0.0.1:7000/api/v1/diagnose/status","poll_interval_seconds":60,"max_wait_seconds":3600,"timeout":120}}}' \
  --eval-type mock_llm \
  --model-id web-rca-async-agent \
  --work-dir outputs/rcaeval_async_smoke \
  --no-timestamp \
  --ignore-errors \
  --limit 10
```

## 10. Run From EvalScope Web

1. Start Offline Data API.
2. Start EvalScope Web.
3. Open `http://127.0.0.1:9000/dashboard`.
4. Create an eval task.
5. In the normal dataset field, enter `rcaeval_rca`.
6. Open More Params and set `dataset_args` with `agent_mode=async_http`,
   `agent_url`, `agent_poll_url`, `poll_interval_seconds`, and
   `max_wait_seconds`.
7. Optionally enable `normalize_with_ai` and configure the normalizer model in
   `dataset_args`.
8. Submit the task and inspect the report.

## 11. Scoring Semantics

The scorer separates injected fault type from observable indicators.

For a delay case:

```json
{
  "root_cause_component": "adservice",
  "root_cause_type": "delay",
  "root_cause_indicator_family": "latency",
  "root_cause_indicator": "adservice_latency-90"
}
```

For a code-level RE3 case:

```json
{
  "root_cause_component": "cartservice",
  "root_cause_type": "f1",
  "root_cause_indicator_family": "code",
  "root_cause_indicator": "cartservice_stack"
}
```

Important metrics:

- `root_cause_component_top1`: exact root service match
- `root_cause_type_accuracy`: injected fault type match, such as `cpu`,
  `delay`, `loss`, or `f1`
- `root_cause_indicator_family_accuracy`: observable family match, such as
  `latency`, `network_loss`, `diskio`, `code`, `stack`, or `trace`
- `root_cause_indicator_accuracy`: specific service-level indicator match
- `overall_minimal`: service + type + indicator family
- `overall_strict`: service + type + indicator family + specific indicator

## 12. Common Issues

### `ModuleNotFoundError: pyarrow`

Install runtime dependencies inside `deploy/rcaeval/.venv`:

```bash
source .venv/bin/activate
pip install pyarrow fastapi uvicorn pandas
```

### Offline Data API shows fewer than 735 cases

Regenerate the benchmark files from the complete raw data:

```bash
.venv/bin/python ../../evalscope/benchmarks/rcaeval_rca/generate_benchmark.py \
  --raw-root data \
  --cases-output generated/rcaeval_cases.jsonl \
  --index-output generated/rcaeval_data_index.json \
  --endpoint-base http://127.0.0.1:18080
```

Then regenerate the store index:

```bash
.venv/bin/python ../../evalscope/benchmarks/rcaeval_rca/prepare_offline_store.py \
  --index generated/rcaeval_data_index.json \
  --store-root generated/store_v1 \
  --output-index generated/rcaeval_data_index_store.json
```

### `store_available=false`

The case exists in the benchmark index, but its Parquet store was not generated.
Run `prepare_offline_store.py` for the full dataset or remove `--limit` if a
smoke conversion was used.

### Port already in use

Override ports:

```bash
RCA_DATA_PORT=18081 ./start-offline-data-api.sh
EVALSCOPE_PORT=9001 ./start-evalscope.sh
```

When changing `RCA_DATA_PORT`, regenerate benchmark cases with the same endpoint
base, or set the endpoint to the port that the agent can actually reach.

### Web task cannot find `rcaeval_rca`

Make sure EvalScope is started with this source tree on `PYTHONPATH`. The
provided script already does this:

```bash
./start-evalscope.sh
```

If starting manually:

```bash
PYTHONPATH="$(cd ../.. && pwd)" .venv/bin/evalscope service --host 0.0.0.0 --port 9000
```
