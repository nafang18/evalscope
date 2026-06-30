# OpsEval EvalScope Deployment Runbook

This directory documents how to run the mixed `opseval` benchmark in EvalScope.

`opseval` is a single EvalScope benchmark that contains both:

- MCQ samples, scored by rule-based answer extraction and exact match.
- QA samples, scored by FAE: fluency, accuracy, and evidence.

The final `overall_score` balances the MCQ and QA subsets with configurable
weights, so the larger subset does not dominate the benchmark.

## 1. Data Layout

Prepare one mixed JSONL file:

```text
deploy/opseval/generated/opseval.jsonl
```

Each line is either an MCQ sample:

```json
{
  "id": "opseval_mcq_000001",
  "task_type": "mcq",
  "domain": "Fault Analysis and Diagnostics",
  "language": "zh",
  "question": "...",
  "choices": {
    "A": "...",
    "B": "...",
    "C": "...",
    "D": "..."
  },
  "answer": "B"
}
```

or a QA sample:

```json
{
  "id": "opseval_qa_000001",
  "task_type": "qa",
  "domain": "Monitoring and Alerting",
  "language": "zh",
  "question": "...",
  "answer": "...",
  "reference_points": [
    "latency and error metrics",
    "deployment timeline",
    "application logs"
  ]
}
```

## 2. Convert Raw OpsEval Data

From `deploy/opseval`:

```bash
mkdir -p generated

python ../../evalscope/benchmarks/opseval/convert_opseval.py \
  --input raw \
  --output generated/opseval.jsonl
```

The converter accepts JSONL, JSON, CSV, TSV, or a directory containing these
files. It normalizes both MCQ and QA records into the single mixed JSONL format.

## 3. Web Evaluation

Start EvalScope service, then open:

```text
http://127.0.0.1:9000/dashboard
```

In the Eval task page, use the normal EvalScope form:

- `Datasets`: `opseval`
- `Dataset Args`: paste the JSON below

The benchmark is registered as a native EvalScope benchmark, so no dedicated
OpsEval web button is required.

Default dataset args:

```json
{
  "opseval": {
    "local_path": "generated/opseval.jsonl",
    "extra_params": {
      "mcq_weight": 0.5,
      "qa_weight": 0.5,
      "judge_api_url": "https://api.openai.com/v1",
      "judge_api_key": "",
      "judge_model": "",
      "judge_timeout": 120
    }
  }
}
```

For FAE scoring, configure an OpenAI-compatible judge:

```json
{
  "opseval": {
    "local_path": "generated/opseval.jsonl",
    "extra_params": {
      "mcq_weight": 0.5,
      "qa_weight": 0.5,
      "judge_api_url": "http://127.0.0.1:8000/v1",
      "judge_api_key": "EMPTY",
      "judge_model": "qwen-plus",
      "judge_timeout": 120
    }
  }
}
```

If `judge_model` or `judge_api_url` is empty, QA falls back to local keypoint
coverage scoring. Formal OpsEval QA runs should use a judge model.

## 4. CLI Smoke Run

```bash
JUDGE_API_URL=http://127.0.0.1:8000/v1 \
JUDGE_API_KEY=EMPTY \
JUDGE_MODEL=qwen-plus \
LIMIT=10 \
./run-opseval-eval.sh
```

Outputs are written under:

```text
deploy/opseval/outputs/opseval_mixed
```
