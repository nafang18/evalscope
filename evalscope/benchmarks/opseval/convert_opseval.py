# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


SUPPORTED_SUFFIXES = {'.jsonl', '.json', '.csv', '.tsv'}


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        records: list[dict[str, Any]] = []
        for file_path in sorted(path.rglob('*')):
            if file_path.suffix.lower() in SUPPORTED_SUFFIXES:
                records.extend(load_records(file_path))
        return records

    suffix = path.suffix.lower()
    if suffix == '.jsonl':
        rows = []
        with path.open('r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    if suffix == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ('data', 'records', 'examples', 'questions'):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
    if suffix in {'.csv', '.tsv'}:
        delimiter = '\t' if suffix == '.tsv' else ','
        with path.open('r', encoding='utf-8-sig', newline='') as fh:
            return list(csv.DictReader(fh, delimiter=delimiter))
    return []


def first_value(record: dict[str, Any], keys: list[str], default: Any = '') -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ''):
            return value
    return default


def infer_task_type(record: dict[str, Any]) -> str:
    value = str(first_value(record, ['task_type', 'type', 'question_type', 'format'], '')).lower()
    if value in {'mcq', 'mcp', 'choice', 'multiple_choice', 'multiple-choice'}:
        return 'mcq'
    if value in {'qa', 'open_qa', 'question_answering', 'open-ended'}:
        return 'qa'
    if first_value(record, ['choices', 'options'], None) is not None:
        return 'mcq'
    if any(letter in record and record[letter] for letter in 'ABCDEFGHIJ'):
        return 'mcq'
    return 'qa'


def normalize_choices(record: dict[str, Any]) -> dict[str, str]:
    raw = first_value(record, ['choices', 'options'], None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [part.strip() for part in raw.split('|') if part.strip()]
    if isinstance(raw, dict):
        return {str(k).strip().upper(): str(v).strip() for k, v in raw.items() if str(v).strip()}
    if isinstance(raw, list):
        return {chr(ord('A') + idx): str(v).strip() for idx, v in enumerate(raw) if str(v).strip()}

    choices = {}
    for letter in 'ABCDEFGHIJ':
        value = record.get(letter) or record.get(letter.lower())
        if value not in (None, ''):
            choices[letter] = str(value).strip()
    return choices


def normalize_reference_points(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            pass
        delimiter = '|' if '|' in value else '\n'
        return [part.strip() for part in value.split(delimiter) if part.strip()]
    return [str(value).strip()]


def convert_record(record: dict[str, Any], index: int) -> dict[str, Any]:
    task_type = infer_task_type(record)
    question = first_value(record, ['question', 'query', 'prompt', 'instruction'])
    answer = first_value(record, ['answer', 'target', 'label', 'response', 'reference_answer'])
    converted = {
        'id': str(first_value(record, ['id', 'qid', 'question_id'], f'opseval_{task_type}_{index:06d}')),
        'task_type': task_type,
        'domain': str(first_value(record, ['domain', 'category', 'task', 'area'], 'unknown')),
        'language': str(first_value(record, ['language', 'lang'], 'unknown')),
        'question': str(question),
        'answer': answer,
        'source': str(first_value(record, ['source'], 'opseval')),
    }
    if task_type == 'mcq':
        converted['choices'] = normalize_choices(record)
    else:
        points = first_value(record, ['reference_points', 'key_points', 'rubric'], None)
        converted['reference_points'] = normalize_reference_points(points)

    for key in ('ability', 'difficulty', 'subdomain'):
        value = record.get(key)
        if value not in (None, ''):
            converted[key] = value
    return converted


def main() -> None:
    parser = argparse.ArgumentParser(description='Convert OpsEval raw files to mixed EvalScope JSONL.')
    parser.add_argument('--input', required=True, help='Raw OpsEval file or directory.')
    parser.add_argument('--output', required=True, help='Output mixed JSONL path.')
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    records = load_records(input_path)
    converted = [convert_record(record, idx) for idx, record in enumerate(records)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8') as fh:
        for record in converted:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')

    counts = {'mcq': 0, 'qa': 0}
    for record in converted:
        counts[record['task_type']] = counts.get(record['task_type'], 0) + 1
    print(f'Wrote {len(converted)} OpsEval records to {output_path} ({counts})')


if __name__ == '__main__':
    main()
