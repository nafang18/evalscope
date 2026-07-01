# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from statistics import mean
from typing import Any

from evalscope.api.agent.trace import AgentTrace, EventType
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


def http_request_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 120,
                      headers: dict[str, str] | None = None, method: str = 'POST') -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json', **(headers or {})},
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {exc.code} from {url}: {body}') from exc
    return json.loads(body) if body.strip() else {}


def http_json(url: str, payload: dict[str, Any], timeout: int = 120,
              headers: dict[str, str] | None = None) -> dict[str, Any]:
    return http_request_json(url, payload=payload, timeout=timeout, headers=headers, method='POST')


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


def bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def split_csv(value: Any, default: list[str]) -> set[str]:
    if value is None:
        return {item.lower() for item in default}
    if isinstance(value, list):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    return {item.strip().lower() for item in str(value).split(',') if item.strip()}


def truncate_value(value: Any, max_chars: int = 8000) -> Any:
    text = value if isinstance(value, str) else dump_json(value)
    if len(text) <= max_chars:
        return value
    return {'truncated': True, 'chars': len(text), 'text': text[:max_chars]}


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return extract_json_object(value)
    except Exception:
        return value


def get_nested_value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def find_field(payload: dict[str, Any], names: list[str], configured_path: str | None = None) -> Any:
    if configured_path:
        value = get_nested_value(payload, configured_path)
        if value is not None:
            return value
    containers = [payload]
    data = payload.get('data')
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for name in names:
            if name in container:
                return container[name]
    return None


def format_url_template(url: str, case_id: str, task_id: Any | None = None) -> str:
    try:
        return url.format(case_id=case_id, task_id=task_id or '')
    except (KeyError, IndexError, ValueError):
        return url


def add_query_params(url: str, params: dict[str, Any]) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is not None:
            query.setdefault(key, str(value))
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urllib.parse.urlencode(query),
        parsed.fragment,
    ))


def has_standard_rca_fields(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    diagnosis = value.get('diagnosis')
    containers = [value]
    if isinstance(diagnosis, dict):
        containers.append(diagnosis)
    fields = {
        'root_cause_component',
        'root_cause_type',
        'root_cause_indicator_family',
        'root_cause_indicator',
    }
    return any(any(field in container for field in fields) for container in containers)


def normalize_prediction_dict(value: Any, case_id: str) -> dict[str, Any]:
    value = parse_jsonish(value)
    if not isinstance(value, dict):
        value = {'raw_answer': value}
    diagnosis = value.get('diagnosis')
    if isinstance(diagnosis, dict):
        for key, item in diagnosis.items():
            value.setdefault(key, item)
    value.setdefault('case_id', case_id)
    value.setdefault('ranked_root_cause_components', [])
    value.setdefault('ranked_root_cause_indicators', [])
    value.setdefault('evidence', [])
    value.setdefault('causal_path', [])
    return value


def extract_answer_payload(response: dict[str, Any]) -> Any | None:
    candidates: list[Any] = [response]
    data = response.get('data')
    if isinstance(data, dict):
        candidates.append(data)
    for container in list(candidates):
        if not isinstance(container, dict):
            continue
        for key in ('answer', 'result', 'final_answer', 'output', 'prediction', 'diagnosis', 'rca'):
            if key in container:
                candidates.append(container[key])

    for candidate in candidates:
        parsed = parse_jsonish(candidate)
        if isinstance(parsed, dict) and has_standard_rca_fields(parsed):
            return parsed
    for candidate in candidates[1:]:
        if candidate is not None:
            return parse_jsonish(candidate)
    return None


def extract_trace_payload(response: dict[str, Any]) -> Any | None:
    containers = [response]
    data = response.get('data')
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for key in ('trace', 'trajectory', 'events', 'steps', 'logs', 'messages'):
            if key in container:
                return container[key]
    return None


def build_agent_payload(case: dict[str, Any]) -> dict[str, Any]:
    return {
        'case_id': case['case_id'],
        **case['input'],
    }


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


def build_async_trace(trace_payload: list[dict[str, Any]]) -> AgentTrace:
    trace = AgentTrace(framework='async-http', max_steps=len(trace_payload))
    for index, snapshot in enumerate(trace_payload):
        trace.add_event(
            step=index,
            type=EventType.MODEL_GENERATE,
            payload={
                'status': snapshot.get('status'),
                'task_id': snapshot.get('task_id'),
                'trace': truncate_value(snapshot.get('trace')),
                'answer': truncate_value(snapshot.get('answer')),
            },
        )
    return trace


def async_http_agent(
    case: dict[str, Any],
    *,
    start_url: str,
    poll_url: str,
    timeout: int,
    poll_interval_seconds: float,
    max_wait_seconds: float,
    terminal_statuses: set[str],
    failed_statuses: set[str],
    task_id_path: str | None = None,
    status_path: str | None = None,
    answer_path: str | None = None,
    trace_path: str | None = None,
    poll_method: str = 'GET',
) -> tuple[dict[str, Any], list[dict[str, Any]], AgentTrace]:
    payload = build_agent_payload(case)
    start_time = time.monotonic()
    start_response = http_json(start_url, payload, timeout=timeout)
    task_id = find_field(start_response, ['task_id', 'taskId', 'id', 'run_id', 'runId'], task_id_path)
    if not task_id:
        raise RuntimeError(f'Async RCA agent start response did not contain task_id: {dump_json(start_response)}')

    snapshots: list[dict[str, Any]] = [{
        'phase': 'start',
        'task_id': task_id,
        'status': find_field(start_response, ['status', 'state'], status_path) or 'started',
        'response': start_response,
        'trace': find_field(start_response, ['trace', 'trajectory', 'events'], trace_path),
        'answer': extract_answer_payload(start_response),
    }]

    final_answer = snapshots[0]['answer']
    final_status = str(snapshots[0]['status']).lower()
    while final_status not in terminal_statuses:
        if final_status in failed_statuses:
            raise RuntimeError(f'Async RCA agent task {task_id} failed with status={final_status}: {dump_json(snapshots[-1])}')
        if not final_status and final_answer is not None and has_standard_rca_fields(parse_jsonish(final_answer)):
            break
        elapsed = time.monotonic() - start_time
        if elapsed >= max_wait_seconds:
            raise TimeoutError(f'Async RCA agent task {task_id} timed out after {int(elapsed)} seconds')

        time.sleep(max(0.0, poll_interval_seconds))
        status_url = format_url_template(poll_url, case['case_id'], task_id)
        poll_payload = {
            'case_id': case['case_id'],
            'task_id': task_id,
        }
        method = poll_method.upper()
        if method == 'GET' and '{task_id}' not in poll_url and '{case_id}' not in poll_url:
            status_url = add_query_params(status_url, poll_payload)
        poll_response = http_request_json(
            status_url,
            payload=poll_payload if method != 'GET' else None,
            timeout=timeout,
            method=method,
        )
        final_status = str(find_field(poll_response, ['status', 'state'], status_path) or '').lower()
        final_answer = find_field(poll_response, ['answer', 'result', 'final_answer', 'output', 'prediction'], answer_path)
        if final_answer is None:
            final_answer = extract_answer_payload(poll_response)
        trace_payload = find_field(poll_response, ['trace', 'trajectory', 'events', 'steps', 'logs'], trace_path)
        if trace_payload is None:
            trace_payload = extract_trace_payload(poll_response)
        snapshots.append({
            'phase': 'poll',
            'task_id': task_id,
            'status': final_status,
            'response': poll_response,
            'trace': trace_payload,
            'answer': final_answer,
        })

    if final_answer is None:
        final_answer = extract_answer_payload(snapshots[-1]['response'])
    if final_answer is None:
        raise RuntimeError(f'Async RCA agent task {task_id} completed without answer: {dump_json(snapshots[-1])}')

    prediction = normalize_prediction_dict(final_answer, case['case_id'])
    prediction['_async_agent'] = {
        'task_id': task_id,
        'status': final_status,
        'poll_count': max(0, len(snapshots) - 1),
    }
    return prediction, snapshots, build_async_trace(snapshots)


def build_normalizer_prompt(case: dict[str, Any], prediction: dict[str, Any],
                            snapshots: list[dict[str, Any]]) -> list[dict[str, str]]:
    compact_snapshots = []
    for snapshot in snapshots:
        compact_snapshots.append({
            'phase': snapshot.get('phase'),
            'task_id': snapshot.get('task_id'),
            'status': snapshot.get('status'),
            'trace': truncate_value(snapshot.get('trace'), max_chars=6000),
            'answer': truncate_value(snapshot.get('answer'), max_chars=6000),
        })

    user_payload = {
        'case': {
            'case_id': case['case_id'],
            **case['input'],
        },
        'raw_agent_prediction': prediction,
        'agent_poll_snapshots': compact_snapshots,
        'required_output_schema': {
            'case_id': case['case_id'],
            'root_cause_component': 'string',
            'root_cause_type': 'string',
            'root_cause_indicator_family': 'cpu|memory|diskio|socket|latency|network_loss|error|code|stack|trace|other',
            'root_cause_indicator': 'specific service-level indicator if available',
            'ranked_root_cause_components': ['string'],
            'ranked_root_cause_indicators': ['string'],
            'confidence': 'number between 0 and 1',
            'evidence': [{'source': 'metrics|logs|traces|agent_trace', 'query': 'string', 'finding': 'string'}],
            'causal_path': ['string'],
            'recommended_fix': 'string',
        },
    }
    return [
        {
            'role': 'system',
            'content': (
                'You normalize an SRE RCA agent result for deterministic scoring. '
                'Use only the supplied case metadata, agent trace, and raw answer. '
                'Return JSON only with exactly the requested RCA fields. '
                'Do not invent root-cause labels that are not supported by the raw answer or trace.'
            ),
        },
        {'role': 'user', 'content': dump_json(user_payload)},
    ]


def normalize_with_llm(case: dict[str, Any], prediction: dict[str, Any],
                       snapshots: list[dict[str, Any]], api_url: str, api_key: str,
                       model: str, timeout: int) -> dict[str, Any]:
    payload = {
        'model': model,
        'messages': build_normalizer_prompt(case, prediction, snapshots),
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
    normalized = normalize_prediction_dict(extract_json_object(content), case['case_id'])
    normalized['_normalizer'] = {
        'model': model,
        'source': 'chat_completions',
    }
    normalized['_raw_agent_prediction'] = prediction
    return normalized


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
                'description': 'RCA agent invocation mode: mock or async_http.',
                'value': 'mock',
                'choices': ['mock', 'async_http'],
            },
            'agent_url': {
                'type': 'str',
                'description': 'Async HTTP start endpoint for agent_mode=async_http.',
                'value': '',
            },
            'agent_poll_url': {
                'type': 'str',
                'description': 'Async HTTP poll endpoint. Supports {task_id} and {case_id} templates.',
                'value': '',
            },
            'agent_poll_method': {
                'type': 'str',
                'description': 'Async HTTP poll method.',
                'value': 'GET',
                'choices': ['GET', 'POST'],
            },
            'poll_interval_seconds': {
                'type': 'float',
                'description': 'Seconds between async agent polling requests.',
                'value': 60,
            },
            'max_wait_seconds': {
                'type': 'float',
                'description': 'Maximum wall-clock seconds to wait for one async RCA task.',
                'value': 3600,
            },
            'terminal_statuses': {
                'type': 'str',
                'description': 'Comma-separated async terminal statuses.',
                'value': 'completed,complete,finished,succeeded,success,done',
            },
            'failed_statuses': {
                'type': 'str',
                'description': 'Comma-separated async failed statuses.',
                'value': 'failed,error,cancelled,canceled,timeout',
            },
            'task_id_path': {
                'type': 'str',
                'description': 'Optional dotted path for task id in async start response.',
                'value': '',
            },
            'status_path': {
                'type': 'str',
                'description': 'Optional dotted path for task status in async poll response.',
                'value': '',
            },
            'answer_path': {
                'type': 'str',
                'description': 'Optional dotted path for final answer in async poll response.',
                'value': '',
            },
            'trace_path': {
                'type': 'str',
                'description': 'Optional dotted path for execution trace in async poll response.',
                'value': '',
            },
            'normalize_with_ai': {
                'type': 'bool',
                'description': 'Normalize async agent raw answer plus trace into standard RCA JSON with a chat-completions model.',
                'value': False,
            },
            'normalizer_api_url': {
                'type': 'str',
                'description': 'Chat-completions base URL used by the RCA result normalizer.',
                'value': os.getenv('RCA_NORMALIZER_API_URL', os.getenv('NORMALIZER_API_URL', '')),
            },
            'normalizer_api_key': {
                'type': 'str',
                'description': 'Optional API key used by the RCA result normalizer.',
                'value': os.getenv('RCA_NORMALIZER_API_KEY', os.getenv('NORMALIZER_API_KEY', '')),
            },
            'normalizer_model': {
                'type': 'str',
                'description': 'Model used by the RCA result normalizer.',
                'value': os.getenv('RCA_NORMALIZER_MODEL', os.getenv('NORMALIZER_MODEL', '')),
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
        agent_trace = None
        async_snapshots: list[dict[str, Any]] = []

        if agent_mode == 'mock':
            prediction = mock_agent(case)
        elif agent_mode == 'async_http':
            agent_url = params.get('agent_url')
            poll_url = params.get('agent_poll_url')
            if not agent_url:
                raise ValueError('agent_url is required when agent_mode=async_http')
            if not poll_url:
                raise ValueError('agent_poll_url is required when agent_mode=async_http')
            prediction, async_snapshots, agent_trace = async_http_agent(
                case,
                start_url=agent_url,
                poll_url=poll_url,
                timeout=timeout,
                poll_interval_seconds=float(params.get('poll_interval_seconds', 60)),
                max_wait_seconds=float(params.get('max_wait_seconds', 3600)),
                terminal_statuses=split_csv(
                    params.get('terminal_statuses'),
                    ['completed', 'complete', 'finished', 'succeeded', 'success', 'done'],
                ),
                failed_statuses=split_csv(
                    params.get('failed_statuses'),
                    ['failed', 'error', 'cancelled', 'canceled', 'timeout'],
                ),
                task_id_path=params.get('task_id_path') or None,
                status_path=params.get('status_path') or None,
                answer_path=params.get('answer_path') or None,
                trace_path=params.get('trace_path') or None,
                poll_method=params.get('agent_poll_method', 'GET'),
            )
        else:
            raise ValueError(f'Unsupported RCA agent_mode: {agent_mode}')

        if bool_param(params.get('normalize_with_ai'), default=False) and agent_mode == 'async_http':
            normalizer_key = (
                params.get('normalizer_api_key')
                or os.getenv('RCA_NORMALIZER_API_KEY')
                or os.getenv('NORMALIZER_API_KEY', '')
            )
            normalizer_url = (
                params.get('normalizer_api_url')
                or os.getenv('RCA_NORMALIZER_API_URL')
                or os.getenv('NORMALIZER_API_URL', '')
            )
            normalizer_model = (
                params.get('normalizer_model')
                or os.getenv('RCA_NORMALIZER_MODEL')
                or os.getenv('NORMALIZER_MODEL', '')
            )
            if not normalizer_url or not normalizer_model:
                raise ValueError('normalizer_api_url and normalizer_model are required when normalize_with_ai=true')
            prediction = normalize_with_llm(
                case,
                normalize_prediction_dict(prediction, case['case_id']),
                async_snapshots,
                api_url=normalizer_url,
                api_key=normalizer_key,
                model=normalizer_model,
                timeout=timeout,
            )

        prediction.setdefault('case_id', case['case_id'])
        completion = dump_json(prediction)
        model_name = getattr(self._task_config, 'model_id', None) or 'rca-agent'
        model_output = ModelOutput.from_content(model=model_name, content=completion)
        task_state = TaskState(
            model=model_name,
            sample=sample,
            messages=list(sample.input) + [ChatMessageAssistant(content=completion, model=model_name)],
            output=model_output,
            completed=True,
        )
        if agent_trace is not None:
            task_state.agent_trace = agent_trace
        return task_state

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
