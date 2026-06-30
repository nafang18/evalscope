# RCAEval EvalScope Deployment

This folder contains a code-only RCAEval deployment helper. It does not include
raw RCAEval data, generated benchmark files, Parquet stores, logs, reports, or
PID files.

## Layout

Clone this repository, then use this folder as the deployment workspace:

```bash
cd deploy/rcaeval
```

Place raw RCAEval data under:

```text
deploy/rcaeval/data
```

The raw data directory should contain suites such as `RE1-OB`, `RE2-OB`, and
`RE3-OB`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ../..
pip install fastapi uvicorn pandas pyarrow
```

## Generate Benchmark Files

```bash
mkdir -p generated

python ../../evalscope/benchmarks/rcaeval_rca/generate_benchmark.py \
  --raw-root data \
  --cases-output generated/rcaeval_cases.jsonl \
  --index-output generated/rcaeval_data_index.json \
  --endpoint-base http://127.0.0.1:18080
```

## Generate Offline Store

```bash
python ../../evalscope/benchmarks/rcaeval_rca/prepare_offline_store.py \
  --index generated/rcaeval_data_index.json \
  --store-root generated/store_v1 \
  --output-index generated/rcaeval_data_index_store.json
```

## Start Services

```bash
./start-offline-data-api.sh
./start-evalscope.sh
```

Open:

```text
http://127.0.0.1:9000/dashboard
```

Check service health:

```bash
./status.sh
```

## Run A Smoke Evaluation

```bash
LIMIT=10 ./run-rcaeval-eval.sh
```
