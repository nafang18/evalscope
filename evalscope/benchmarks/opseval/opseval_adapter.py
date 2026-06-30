# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import mean
from typing import Any

from evalscope.api.benchmark import BenchmarkMeta, DefaultDataAdapter
from evalscope.api.dataset import DatasetDict, MemoryDataset, Sample
from evalscope.api.evaluator import TaskState
from evalscope.api.messages import ChatMessageUser
from evalscope.api.metric import AggScore, SampleScore, Score
from evalscope.api.registry import register_benchmark
from evalscope.constants import Tags

from .scorer import JudgeConfig, dump_json, score_mcq, score_qa


DATASET_NAME = 'opseval'
SUBSET_NAME = 'default'
ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = ROOT / 'data' / 'opseval_sample.jsonl'

OPSEVAL_DESCRIPTION = """
## Overview

OpsEval is a mixed IT operations benchmark for evaluating models on operational
multiple-choice questions and open-ended question answering in one dataset.

## Task Description

- **Task Type**: IT operations MCQ + QA
- **Input**: Operations question, optional choices, and metadata such as domain/language
- **Output**: MCQ answer letter or open-ended operational answer

## Evaluation Notes

- MCQ samples are scored by rule-based answer extraction and exact match.
- QA samples are scored by FAE: fluency, accuracy, and evidence using a configurable judge model.
- Overall score balances the MCQ and QA subsets instead of letting the larger subset dominate.
"""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    records: list[dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def normalize_task_type(record: dict[str, Any]) -> str:
    task_type = str(record.get('task_type') or record.get('type') or '').strip().lower()
    if task_type in {'mcq', 'mcp', 'multiple_choice', 'multiple-choice', 'choice'}:
        return 'mcq'
    if task_type in {'qa', 'question_answering', 'open_qa', 'open-ended'}:
        return 'qa'
    if record.get('choices') or any(k in record for k in ('A', 'B', 'C', 'D')):
        return 'mcq'
    return 'qa'


def normalize_choices(record: dict[str, Any]) -> dict[str, str]:
    raw_choices = record.get('choices')
    if isinstance(raw_choices, dict):
        return {str(k).upper(): str(v) for k, v in raw_choices.items()}
    if isinstance(raw_choices, list):
        return {chr(ord('A') + idx): str(choice) for idx, choice in enumerate(raw_choices)}

    choices = {}
    for letter in 'ABCDEFGHIJ':
        if letter in record:
            choices[letter] = str(record[letter])
    return choices


def format_mcq_prompt(record: dict[str, Any], choices: dict[str, str]) -> str:
    lines = [
        'Answer the following IT operations multiple-choice question.',
        'Return only the final answer letter, for example: Answer: B.',
        '',
        f"Question: {record.get('question') or record.get('query') or ''}",
        '',
        'Choices:',
    ]
    for letter, text in choices.items():
        lines.append(f'{letter}. {text}')
    return '\n'.join(lines)


def format_qa_prompt(record: dict[str, Any]) -> str:
    return '\n'.join([
        'Answer the following IT operations question.',
        'Be concise, accurate, and include operational evidence or reasoning when useful.',
        '',
        f"Question: {record.get('question') or record.get('query') or ''}",
    ])


def record_to_case(record: dict[str, Any], index: int) -> dict[str, Any]:
    task_type = normalize_task_type(record)
    choices = normalize_choices(record)
    question = str(record.get('question') or record.get('query') or '')
    case_id = str(record.get('id') or f'opseval_{task_type}_{index:06d}')
    answer = record.get('answer') or record.get('target') or record.get('response') or ''
    reference_points = record.get('reference_points') or record.get('key_points') or record.get('rubric') or []
    if isinstance(reference_points, str):
        reference_points = [p.strip() for p in reference_points.split('\n') if p.strip()]
    if not isinstance(reference_points, list):
        reference_points = []

    prompt = format_mcq_prompt(record, choices) if task_type == 'mcq' else format_qa_prompt(record)
    metadata = {
        'case_id': case_id,
        'task_type': task_type,
        'domain': record.get('domain') or record.get('category') or record.get('task') or 'unknown',
        'language': record.get('language') or record.get('lang') or 'unknown',
        'source': record.get('source') or 'opseval',
    }
    for key in ('ability', 'difficulty', 'subdomain'):
        if key in record:
            metadata[key] = record[key]

    return {
        'case_id': case_id,
        'task_type': task_type,
        'question': question,
        'choices': choices,
        'answer': answer,
        'reference_answer': str(answer),
        'reference_points': [str(p) for p in reference_points],
        'prompt': prompt,
        'metadata': metadata,
        'raw': record,
    }


@register_benchmark(
    BenchmarkMeta(
        name=DATASET_NAME,
        dataset_id=str(DEFAULT_DATASET),
        pretty_name='OpsEval',
        description=OPSEVAL_DESCRIPTION,
        tags=[Tags.QA, Tags.MULTIPLE_CHOICE, 'Ops', 'AIOps'],
        subset_list=[SUBSET_NAME],
        default_subset=SUBSET_NAME,
        metric_list=[
            'overall_score',
            'mcq_accuracy',
            'mcq_partial_f1',
            'qa_score',
            'qa_accuracy',
            'qa_fluency',
            'qa_evidence',
            'sample_score',
        ],
        extra_params={
            'mcq_weight': {
                'type': 'float',
                'description': 'Overall score weight for the MCQ subset.',
                'value': 0.5,
            },
            'qa_weight': {
                'type': 'float',
                'description': 'Overall score weight for the QA subset.',
                'value': 0.5,
            },
            'judge_api_url': {
                'type': 'str',
                'description': 'OpenAI-compatible base URL for FAE QA judge.',
                'value': os.getenv('OPSEVAL_JUDGE_API_URL', os.getenv('OPENAI_API_BASE', '')),
            },
            'judge_api_key': {
                'type': 'str',
                'description': 'OpenAI-compatible API key for FAE QA judge.',
                'value': os.getenv('OPSEVAL_JUDGE_API_KEY', os.getenv('OPENAI_API_KEY', '')),
            },
            'judge_model': {
                'type': 'str',
                'description': 'Judge model name for FAE QA scoring. Empty uses keypoint fallback.',
                'value': os.getenv('OPSEVAL_JUDGE_MODEL', ''),
            },
            'judge_timeout': {
                'type': 'int',
                'description': 'FAE judge request timeout in seconds.',
                'value': 120,
            },
            'qa_fluency_weight': {
                'type': 'float',
                'description': 'Fluency weight inside QA FAE score.',
                'value': 0.2,
            },
            'qa_accuracy_weight': {
                'type': 'float',
                'description': 'Accuracy weight inside QA FAE score.',
                'value': 0.6,
            },
            'qa_evidence_weight': {
                'type': 'float',
                'description': 'Evidence weight inside QA FAE score.',
                'value': 0.2,
            },
        },
    )
)
class OpsEvalAdapter(DefaultDataAdapter):
    """Mixed MCQ + QA adapter for OpsEval."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_aggregation_name = False
        self.save_metadata = True
        self.category_map = {SUBSET_NAME: ['ops']}

    def load_dataset(self) -> DatasetDict:
        records = load_jsonl(Path(self.dataset_id))
        if self.limit is not None:
            if isinstance(self.limit, float):
                records = records[:max(1, int(len(records) * self.limit))]
            else:
                records = records[:int(self.limit)]

        samples: list[Sample] = []
        for index, record in enumerate(records):
            case = record_to_case(record, index)
            sample = Sample(
                id=index,
                group_id=index,
                input=[ChatMessageUser(content=case['prompt'])],
                target=dump_json({
                    'task_type': case['task_type'],
                    'answer': case['answer'],
                    'reference_points': case['reference_points'],
                }),
                metadata={
                    **case['metadata'],
                    'case': case,
                },
            )
            samples.append(sample)

        dataset = MemoryDataset(samples=samples, name=SUBSET_NAME, location=str(self.dataset_id))
        return DatasetDict({SUBSET_NAME: dataset})

    def calculate_metrics(self, task_state: TaskState) -> SampleScore:
        assert task_state.completed, 'TaskState must be completed before calculating metrics.'
        case = task_state.metadata.get('case') if isinstance(task_state.metadata, dict) else None
        if not isinstance(case, dict):
            raise ValueError(f'OpsEval sample {task_state.sample_id} is missing case metadata.')

        prediction = task_state.output.completion if task_state.output is not None else ''
        params = self.extra_params
        if case['task_type'] == 'mcq':
            report = score_mcq(
                prediction=prediction,
                answer=case.get('answer'),
                max_choices=max(1, len(case.get('choices') or {})),
            )
        else:
            report = score_qa(
                question=case.get('question', ''),
                reference_answer=case.get('reference_answer', ''),
                reference_points=case.get('reference_points', []),
                prediction=prediction,
                judge_config=JudgeConfig(
                    api_url=str(params.get('judge_api_url') or ''),
                    api_key=str(params.get('judge_api_key') or ''),
                    model=str(params.get('judge_model') or ''),
                    timeout=int(params.get('judge_timeout') or 120),
                ),
                weights={
                    'fluency': float(params.get('qa_fluency_weight') or 0.2),
                    'accuracy': float(params.get('qa_accuracy_weight') or 0.6),
                    'evidence': float(params.get('qa_evidence_weight') or 0.2),
                },
            )

        values = dict(report['scores'])
        values['overall_score'] = float(values.get('sample_score', 0.0))
        score = Score(
            value=values,
            extracted_prediction=dump_json(report.get('prediction_extract', {})),
            prediction=prediction,
            explanation=report.get('explanation', ''),
            metadata={
                'case_id': case['case_id'],
                'task_type': case['task_type'],
                **report.get('metadata', {}),
            },
            main_score_name='sample_score',
        )
        return SampleScore(
            score=score,
            sample_id=task_state.sample_id,
            group_id=task_state.group_id,
            sample_metadata=task_state.metadata,
        )

    def aggregate_scores(self, sample_scores: list[SampleScore]) -> list[AggScore]:
        params = self.extra_params
        mcq_weight = max(0.0, float(params.get('mcq_weight') or 0.5))
        qa_weight = max(0.0, float(params.get('qa_weight') or 0.5))
        total_weight = mcq_weight + qa_weight or 1.0

        def values_for(metric_name: str, task_type: str | None = None) -> list[float]:
            values = []
            for sample_score in sample_scores:
                if task_type and (sample_score.sample_metadata or {}).get('task_type') != task_type:
                    continue
                if metric_name in sample_score.score.value:
                    values.append(float(sample_score.score.value[metric_name]))
            return values

        mcq_values = values_for('mcq_accuracy', 'mcq')
        qa_values = values_for('qa_score', 'qa')
        mcq_score = mean(mcq_values) if mcq_values else 0.0
        qa_score = mean(qa_values) if qa_values else 0.0
        if mcq_values and qa_values:
            overall = (mcq_weight * mcq_score + qa_weight * qa_score) / total_weight
        elif mcq_values:
            overall = mcq_score
        elif qa_values:
            overall = qa_score
        else:
            overall = 0.0

        agg_scores: list[AggScore] = [
            AggScore(metric_name='overall_score', aggregation_name='weighted_mean', score=overall, num=len(sample_scores)),
            AggScore(metric_name='mcq_count', aggregation_name='count', score=float(len(mcq_values)), num=len(mcq_values)),
            AggScore(metric_name='qa_count', aggregation_name='count', score=float(len(qa_values)), num=len(qa_values)),
        ]

        metric_specs = [
            ('mcq_accuracy', 'mcq'),
            ('mcq_partial_f1', 'mcq'),
            ('qa_score', 'qa'),
            ('qa_accuracy', 'qa'),
            ('qa_fluency', 'qa'),
            ('qa_evidence', 'qa'),
            ('sample_score', None),
        ]
        for metric_name, task_type in metric_specs:
            values = values_for(metric_name, task_type)
            if values:
                agg_scores.append(
                    AggScore(
                        metric_name=metric_name,
                        aggregation_name='mean',
                        score=mean(values),
                        num=len(values),
                        ids=[sample_score.sample_id for sample_score in sample_scores],
                    )
                )

        group_fields = ['task_type', 'domain', 'language']
        for field in group_fields:
            groups = sorted({str((s.sample_metadata or {}).get(field, 'unknown')) for s in sample_scores})
            for group in groups:
                group_scores = [
                    s for s in sample_scores
                    if str((s.sample_metadata or {}).get(field, 'unknown')) == group
                ]
                values = [float(s.score.value.get('sample_score', 0.0)) for s in group_scores]
                if values:
                    agg_scores.append(
                        AggScore(
                            metric_name=f'{field}:{group}',
                            aggregation_name='mean_sample_score',
                            score=mean(values),
                            num=len(values),
                            ids=[s.sample_id for s in group_scores],
                        )
                    )

        return agg_scores
