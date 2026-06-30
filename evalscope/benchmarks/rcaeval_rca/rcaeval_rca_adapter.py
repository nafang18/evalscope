# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from statistics import mean
from typing import Any

from evalscope.api.benchmark import BenchmarkMeta, DefaultDataAdapter
from evalscope.api.dataset import DatasetDict, MemoryDataset, Sample
from evalscope.api.evaluator import TaskState
from evalscope.api.messages import ChatMessageAssistant, ChatMessageUser
from evalscope.api.metric import AggScore, SampleScore, Score
from evalscope.api.model import ModelOutput
from evalscope.api.registry import register_benchmark
from evalscope.constants import Tags

from .scorer import score_one


DATASET_NAME = 'rcaeval_rca'
SUBSET_NAME = 'default'
ROOT = Path(__file__).resolve().parent
DEFAULT_CASES = ROOT / 'data' / 'rca_cases_sample.jsonl'

RCA_DESCRIPTION = """
## Overview

RCAEval RCA is a local offline root-cause-analysis benchmark for evaluating SRE agents against RCAEval-derived cases.

## Task Description

- **Task Type**: Operations root cause analysis
- **Input**: Incident case metadata and an offline telemetry data endpoint
- **Output**: Structured RCA JSON including root-cause component, type, indicator, evidence, causal path, and recommended fix

## Evaluation Notes

- The agent should query offline telemetry instead of receiving ground truth.
- The scorer evaluates structured RCA fields rather than plain exact match.
"""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    records = []
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def http_json(url: str, payload: dict[str, Any], timeout: int = 120,
              headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json', **(headers or {})},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {exc.code} from {url}: {body}') from exc
    return json.loads(body)


def read_json_url(url: str, timeout: int = 30) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def mock_agent(case: dict[str, Any]) -> dict[str, Any]:
    """Deterministic agent for smoke testing the RCA pipeline."""
    truth_hint = {
        'legacy-simple-metrics': {
            'root_cause_component': 'emailservice',
            'root_cause_type': 'mem',
            'root_cause_indicator': 'emailservice_mem',
            'ranked_root_cause_components': [
                'emailservice',
                'recommendationservice',
                'cartservice',
            ],
        }
    }
    base = truth_hint.get(case['input'].get('dataset'), {})
    return {
        'case_id': case['case_id'],
        'root_cause_component': base.get('root_cause_component', ''),
        'root_cause_type': base.get('root_cause_type', ''),
        'root_cause_indicator_family': '',
        'root_cause_indicator': base.get('root_cause_indicator', ''),
        'ranked_root_cause_components': base.get('ranked_root_cause_components', []),
        'ranked_root_cause_indicators': [],
        'confidence': 0.78,
        'evidence': [{
            'source': 'metrics',
            'query': f"GET {case['input']['data_endpoint']}/metrics",
            'finding': 'Mock agent used the RCAEval smoke-case metric schema to identify the root-cause indicator.',
        }],
        'causal_path': [],
        'recommended_fix': 'Inspect emailservice memory pressure and resize or restart it if saturation is confirmed.',
    }


def http_agent(case: dict[str, Any], agent_url: str, timeout: int) -> dict[str, Any]:
    payload = {
        'case_id': case['case_id'],
        **case['input'],
    }
    result = http_json(agent_url, payload, timeout=timeout)
    result.setdefault('case_id', case['case_id'])
    return result


def build_openai_prompt(case: dict[str, Any], metrics_limit: int) -> list[dict[str, str]]:
    metrics = read_json_url(f"{case['input']['data_endpoint']}/metrics?limit={metrics_limit}")
    user_payload = {
        'case': case['input'],
        'metrics_sample': {
            'columns': metrics.get('columns', []),
            'rows': metrics.get('rows', []),
        },
        'required_output_schema': {
            'case_id': case['case_id'],
            'root_cause_component': 'string',
            'root_cause_type': 'string',
            'root_cause_indicator_family': 'string',
            'root_cause_indicator': 'string',
            'ranked_root_cause_components': ['string'],
            'ranked_root_cause_indicators': ['string'],
            'confidence': 'number between 0 and 1',
            'evidence': [{'source': 'metrics|logs|traces', 'query': 'string', 'finding': 'string'}],
            'causal_path': ['string'],
            'recommended_fix': 'string',
        },
    }
    return [
        {
            'role': 'system',
            'content': (
                'You are an SRE root-cause-analysis agent. Diagnose the incident '
                'from the provided offline telemetry sample. Return JSON only. '
                'Do not include markdown fences or commentary.'
            ),
        },
        {'role': 'user', 'content': dump_json(user_payload)},
    ]


def openai_agent(case: dict[str, Any], api_url: str, api_key: str, model: str,
                 metrics_limit: int, timeout: int) -> dict[str, Any]:
    payload = {
        'model': model,
        'messages': build_openai_prompt(case, metrics_limit),
        'temperature': 0,
        'response_format': {'type': 'json_object'},
    }
    response = http_json(
        api_url.rstrip('/') + '/chat/completions',
        payload,
        timeout=timeout,
        headers={'Authorization': f'Bearer {api_key}'} if api_key else {},
    )
    content = response['choices'][0]['message']['content']
    result = extract_json_object(content)
    result.setdefault('case_id', case['case_id'])
    return result


def get_case_from_sample(sample: Sample) -> dict[str, Any]:
    case = sample.metadata.get('case')
    if not isinstance(case, dict):
        raise ValueError(f'Sample {sample.id} is missing RCA case metadata')
    return case


@register_benchmark(
    BenchmarkMeta(
        name=DATASET_NAME,
        dataset_id=str(DEFAULT_CASES),
        pretty_name='RCAEval RCA',
        description=RCA_DESCRIPTION,
        tags=[Tags.AGENT, 'RCA', 'SRE', 'Offline'],
        subset_list=[SUBSET_NAME],
        default_subset=SUBSET_NAME,
        metric_list=[
            'overall_minimal',
            'overall_strict',
            'root_cause_component_top1',
            'root_cause_component_top3',
            'root_cause_type_accuracy',
            'root_cause_indicator_family_accuracy',
            'root_cause_indicator_accuracy',
            'root_cause_indicator_top3',
            'root_cause_evidence_family_accuracy',
            'evidence_present',
            'causal_path_f1',
        ],
        extra_params={
            'agent_mode': {
                'type': 'str',
                'description': 'RCA agent invocation mode: mock, http, or openai.',
                'value': 'mock',
                'choices': ['mock', 'http', 'openai'],
            },
            'agent_url': {
                'type': 'str',
                'description': 'HTTP RCA agent diagnose endpoint for agent_mode=http.',
                'value': '',
            },
            'openai_api_url': {
                'type': 'str',
                'description': 'OpenAI-compatible base URL for agent_mode=openai.',
                'value': os.getenv('OPENAI_API_BASE', 'https://api.openai.com/v1'),
            },
            'openai_api_key': {
                'type': 'str',
                'description': 'OpenAI-compatible API key for agent_mode=openai.',
                'value': os.getenv('OPENAI_API_KEY', ''),
            },
            'openai_model': {
                'type': 'str',
                'description': 'OpenAI-compatible model name for agent_mode=openai.',
                'value': os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
            },
            'metrics_limit': {
                'type': 'int',
                'description': 'Maximum metric rows embedded into OpenAI-mode prompts.',
                'value': 80,
            },
            'timeout': {
                'type': 'int',
                'description': 'Agent HTTP request timeout in seconds.',
                'value': 120,
            },
        },
    )
)
class RCAEvalRCAAdapter(DefaultDataAdapter):
    """EvalScope-native adapter for RCAEval offline RCA cases."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_aggregation_name = False
        self.category_map = {SUBSET_NAME: ['rca']}
        self.save_metadata = True

    def load_dataset(self) -> DatasetDict:
        records = load_jsonl(Path(self.dataset_id))
        if self.limit is not None:
            if isinstance(self.limit, float):
                records = records[:max(1, int(len(records) * self.limit))]
            else:
                records = records[:int(self.limit)]

        samples: list[Sample] = []
        for index, case in enumerate(records):
            user_payload = {
                'case_id': case['case_id'],
                **case['input'],
            }
            sample = Sample(
                id=index,
                group_id=index,
                input=[ChatMessageUser(content=dump_json(user_payload))],
                target=dump_json(case['ground_truth']),
                metadata={
                    'case_id': case['case_id'],
                    'ground_truth': case['ground_truth'],
                    'input': case['input'],
                    'case': case,
                    **case.get('metadata', {}),
                },
            )
            samples.append(sample)

        dataset = MemoryDataset(samples=samples, name=SUBSET_NAME, location=str(self.dataset_id))
        return DatasetDict({SUBSET_NAME: dataset})

    def run_inference(self, model, sample: Sample, output_dir: str, **kwargs) -> TaskState:
        case = get_case_from_sample(sample)
        params = self._benchmark_meta.get_extra_params()
        agent_mode = params.get('agent_mode', 'mock')
        timeout = int(params.get('timeout', 120))

        if agent_mode == 'mock':
            prediction = mock_agent(case)
        elif agent_mode == 'http':
            agent_url = params.get('agent_url')
            if not agent_url:
                raise ValueError('agent_url is required when agent_mode=http')
            prediction = http_agent(case, agent_url=agent_url, timeout=timeout)
        elif agent_mode == 'openai':
            api_key = params.get('openai_api_key') or os.getenv('OPENAI_API_KEY', '')
            if not api_key:
                raise ValueError('openai_api_key or OPENAI_API_KEY is required when agent_mode=openai')
            prediction = openai_agent(
                case,
                api_url=params.get('openai_api_url', 'https://api.openai.com/v1'),
                api_key=api_key,
                model=params.get('openai_model', 'gpt-4.1-mini'),
                metrics_limit=int(params.get('metrics_limit', 80)),
                timeout=timeout,
            )
        else:
            raise ValueError(f'Unsupported RCA agent_mode: {agent_mode}')

        prediction.setdefault('case_id', case['case_id'])
        completion = dump_json(prediction)
        model_name = getattr(self._task_config, 'model_id', None) or 'rca-agent'
        model_output = ModelOutput.from_content(model=model_name, content=completion)
        return TaskState(
            model=model_name,
            sample=sample,
            messages=list(sample.input) + [ChatMessageAssistant(content=completion, model=model_name)],
            output=model_output,
            completed=True,
        )

    def calculate_metrics(self, task_state: TaskState) -> SampleScore:
        assert task_state.completed, 'TaskState must be completed before calculating metrics.'
        case = get_case_from_sample(task_state._sample)
        prediction = json.loads(task_state.output.completion)
        score_report = score_one(case, prediction)
        score = Score(
            value=score_report['scores'],
            extracted_prediction=dump_json(score_report.get('prediction_extract', {})),
            prediction=task_state.output.completion,
            explanation='RCA structured field scorer.',
            metadata={
                'case_id': case['case_id'],
                'ground_truth': case['ground_truth'],
            },
            main_score_name='overall_minimal',
        )
        return SampleScore(
            score=score,
            sample_id=task_state.sample_id,
            group_id=task_state.group_id,
            sample_metadata=task_state.metadata,
        )

    def aggregate_scores(self, sample_scores: list[SampleScore]) -> list[AggScore]:
        metric_names = []
        for metric in self.metric_list:
            if isinstance(metric, str):
                metric_names.append(metric)
            elif isinstance(metric, dict):
                metric_names.extend(metric.keys())

        agg_scores: list[AggScore] = []
        for metric_name in metric_names:
            values = [
                float(sample_score.score.value[metric_name])
                for sample_score in sample_scores
                if metric_name in sample_score.score.value
            ]
            if not values:
                continue
            agg_scores.append(
                AggScore(
                    metric_name=metric_name,
                    aggregation_name='mean',
                    score=mean(values),
                    num=len(values),
                    ids=[sample_score.sample_id for sample_score in sample_scores],
                )
            )
        return agg_scores
