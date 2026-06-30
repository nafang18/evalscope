# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable


SUITE_PATTERN = re.compile(r'^(RE[123]|TORAI)-(OB|SS|TT)$', re.IGNORECASE)
SYSTEM_BY_SUFFIX = {
    'OB': 'online-boutique',
    'SS': 'sock-shop',
    'TT': 'train-ticket',
}
METRIC_FILENAMES = ('data.csv', 'simple_metrics.csv', 'metrics.json')
CODE_FAULT_PATTERN = re.compile(r'^f\d+$', re.IGNORECASE)
FAULT_TO_INDICATOR_FAMILY = {
    'cpu': 'cpu',
    'mem': 'memory',
    'memory': 'memory',
    'disk': 'diskio',
    'socket': 'socket',
    'delay': 'latency',
    'loss': 'network_loss',
}


def read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding='utf-8').strip()
    return text or None


def safe_id_part(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', value).strip('-')


def detect_suite(parts: Iterable[str]) -> str | None:
    for part in parts:
        if SUITE_PATTERN.match(part):
            return part.upper()
    return None


def system_for_suite(suite: str | None) -> str:
    if not suite:
        return 'unknown'
    suffix = suite.rsplit('-', 1)[-1].upper()
    return SYSTEM_BY_SUFFIX.get(suffix, 'unknown')


def unique_values(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        key = normalize_token(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def normalize_token(value: str) -> str:
    return re.sub(r'[\s\-_]+', '', value.lower().strip())


def read_metric_header(metric_path: Path) -> list[str]:
    if metric_path.suffix.lower() != '.csv':
        return []
    try:
        with metric_path.open('r', encoding='utf-8', newline='') as fh:
            return next(csv.reader(fh), [])
    except (OSError, StopIteration, UnicodeDecodeError):
        return []


def first_existing(candidates: Iterable[str], columns: Iterable[str]) -> str | None:
    by_key = {normalize_token(column): column for column in columns}
    for candidate in candidates:
        match = by_key.get(normalize_token(candidate))
        if match:
            return match
    return None


def prefixed(service: str, suffixes: Iterable[str]) -> list[str]:
    return [f'{service}_{suffix}' for suffix in suffixes]


def is_code_fault(suite: str | None, fault: str) -> bool:
    return bool(suite and suite.upper().startswith('RE3')) or bool(CODE_FAULT_PATTERN.match(fault))


def indicator_spec(service: str, fault: str, suite: str | None, metric_path: Path,
                   modalities: list[str]) -> dict[str, Any]:
    columns = read_metric_header(metric_path)
    if is_code_fault(suite, fault):
        candidates = prefixed(service, [
            'stack',
            'error',
            'trace_status',
            'trace_error',
            'trace_latency',
        ])
        if 'logs' in modalities:
            candidates.extend(['stack_trace', 'exception'])
        if 'traces' in modalities:
            candidates.extend(prefixed(service, ['statusCode', 'duration']))
            candidates.extend(['response_code', 'trace_status_code'])

        primary = first_existing(candidates, columns) or f'{service}_stack'
        return {
            'root_cause_indicator': primary,
            'root_cause_indicator_family': 'code',
            'root_cause_indicator_accepted_families': ['code', 'stack', 'error', 'trace'],
            'root_cause_indicator_aliases': unique_values([primary, *candidates]),
            'root_cause_indicator_granularity': 'broad',
            'rcaeval_legacy_metric_answer': f'{service}_{fault}',
        }

    fault_key = fault.lower()
    family = FAULT_TO_INDICATOR_FAMILY.get(fault_key, fault_key)
    candidates_by_fault = {
        'cpu': prefixed(service, [
            'cpu',
            'container-cpu-usage-seconds-total',
            'container-cpu-system-seconds-total',
            'container-cpu-user-seconds-total',
        ]),
        'mem': prefixed(service, [
            'mem',
            'memory',
            'container-memory-usage-bytes',
            'container-memory-working-set-bytes',
            'container-memory-rss',
        ]),
        'memory': prefixed(service, [
            'mem',
            'memory',
            'container-memory-usage-bytes',
            'container-memory-working-set-bytes',
            'container-memory-rss',
        ]),
        'disk': prefixed(service, [
            'diskio',
            'container-fs-writes-total',
            'container-fs-writes-bytes-total',
            'container-fs-reads-total',
            'container-fs-reads-bytes-total',
            'container-blkio-device-usage-total',
        ]),
        'socket': prefixed(service, [
            'socket',
            'sockets',
            'container-sockets',
        ]),
        'delay': prefixed(service, [
            'latency-90',
            'latency',
            'istio-latency-90',
            'trace_latency',
            'duration',
        ]),
        'loss': prefixed(service, [
            'container-network-receive-packets-dropped-total',
            'container-network-transmit-packets-dropped-total',
            'network-receive-drop-total',
            'network-transmit-drop-total',
            'network_receive_drop',
            'network_transmit_drop',
            'error',
            'istio-error-total',
            'latency-90',
            'latency',
            'trace_error',
            'trace_latency',
        ]),
    }
    candidates = candidates_by_fault.get(fault_key, prefixed(service, [family]))
    primary = first_existing(candidates, columns) or candidates[0]
    accepted_families = [family]
    if fault_key == 'loss':
        accepted_families = ['network_loss', 'latency', 'error']

    legacy_overall_metric = {
        'delay': f'{service}_latency',
        'loss': f'{service}_latency',
        'disk': f'{service}_diskio',
    }.get(fault_key, f'{service}_{fault}')

    return {
        'root_cause_indicator': primary,
        'root_cause_indicator_family': family,
        'root_cause_indicator_accepted_families': accepted_families,
        'root_cause_indicator_aliases': unique_values([primary, *candidates, legacy_overall_metric]),
        'root_cause_indicator_granularity': 'specific',
        'rcaeval_legacy_metric_answer': f'{service}_{fault}',
        'rcaeval_overall_metric_answer': legacy_overall_metric,
    }


def parse_case(metric_path: Path, raw_root: Path) -> dict[str, Any] | None:
    case_dir = metric_path.parent
    rel_parts = metric_path.relative_to(raw_root).parts
    suite = detect_suite(rel_parts)

    # Structured RCAEval layout:
    #   RE2-SS/carts_cpu/1/simple_metrics.csv
    #   RE1-OB/adservice_cpu/1/data.csv
    parent_name = case_dir.parent.name
    if '_' in parent_name:
        service, fault = parent_name.rsplit('_', 1)
        instance = case_dir.name
        return {
            'suite': suite,
            'system': system_for_suite(suite),
            'service': service,
            'fault': fault,
            'instance': instance,
            'case_dir': case_dir,
            'case_name': parent_name,
        }

    # Older flat layout seen in some archives:
    #   re1ss_user_disk_1/metrics.json
    if '_' in case_dir.name:
        tokens = case_dir.name.split('_')
        if len(tokens) >= 3:
            instance = tokens[-1]
            fault = tokens[-2]
            service = '_'.join(tokens[1:-2]) if len(tokens) > 3 else tokens[0]
            if not suite:
                prefix = tokens[0].upper()
                suite_match = re.match(r'(RE[123])([A-Z]{2})', prefix)
                if suite_match:
                    suite = f'{suite_match.group(1)}-{suite_match.group(2)}'
            return {
                'suite': suite,
                'system': system_for_suite(suite),
                'service': service,
                'fault': fault,
                'instance': instance,
                'case_dir': case_dir,
                'case_name': case_dir.name,
            }

    return None


def iter_metric_files(raw_root: Path) -> list[Path]:
    files: list[Path] = []
    for filename in METRIC_FILENAMES:
        files.extend(raw_root.rglob(filename))
    return sorted(set(files), key=lambda path: path.as_posix())


def relative_or_absolute(path: Path, output_dir: Path, relative_paths: bool) -> str:
    resolved = path.resolve()
    if not relative_paths:
        return str(resolved)
    try:
        return str(resolved.relative_to(output_dir.resolve()))
    except ValueError:
        return str(resolved)


def build_question(case: dict[str, Any], inject_time: int | None, data_endpoint: str) -> str:
    time_text = str(inject_time) if inject_time is not None else 'the recorded injection time'
    modalities = ', '.join(case['modalities'])
    return (
        f'An incident occurred in the {case["system"]} microservice system '
        f'around Unix timestamp {time_text}. Use the offline telemetry API at '
        f'{data_endpoint} to inspect available {modalities} data and diagnose '
        'the root cause. Return JSON only with fields: case_id, '
        'root_cause_component, root_cause_type, root_cause_indicator_family, '
        'root_cause_indicator, ranked_root_cause_components, '
        'ranked_root_cause_indicators, confidence, evidence, causal_path, '
        'recommended_fix.'
    )


def build_records(
    raw_root: Path,
    cases_output: Path,
    index_output: Path,
    endpoint_base: str,
    window_seconds: int,
    suites: set[str] | None,
    limit: int | None,
    relative_paths: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    raw_root = raw_root.resolve()
    output_dir = index_output.resolve().parent
    records: list[dict[str, Any]] = []
    index: dict[str, Any] = {}
    warnings: list[str] = []

    for metric_path in iter_metric_files(raw_root):
        parsed = parse_case(metric_path, raw_root)
        if not parsed:
            warnings.append(f'skip unparsable metric file: {metric_path}')
            continue

        suite = parsed['suite'] or 'unknown'
        if suites and suite.upper() not in suites:
            continue

        case_dir: Path = parsed['case_dir']
        logs_path = case_dir / 'logs.csv'
        traces_path = case_dir / 'traces.csv'
        inject_time_path = case_dir / 'inject_time.txt'
        inject_time_text = read_text(inject_time_path)
        inject_time = int(inject_time_text) if inject_time_text and inject_time_text.isdigit() else None
        half_window = max(1, window_seconds // 2)
        start_time = inject_time - half_window if inject_time is not None else None
        end_time = inject_time + half_window if inject_time is not None else None

        modalities = ['metrics']
        if logs_path.exists():
            modalities.append('logs')
        if traces_path.exists():
            modalities.append('traces')

        ordinal = len(records) + 1
        case_id = f'rcaeval_{safe_id_part(suite.lower())}_{ordinal:04d}'
        data_endpoint = f'{endpoint_base.rstrip("/")}/cases/{case_id}'
        fault = parsed['fault']
        service = parsed['service']
        indicator_truth = indicator_spec(service, fault, suite, metric_path, modalities)

        ground_truth: dict[str, Any] = {
            'root_cause_component': service,
            'root_cause_type': fault,
            'causal_path': [],
            **indicator_truth,
        }

        input_payload: dict[str, Any] = {
            'source': 'RCAEval',
            'dataset': suite,
            'suite': suite,
            'system': parsed['system'],
            'case_id': case_id,
            'inject_time': inject_time,
            'start_time': start_time,
            'end_time': end_time,
            'modalities': modalities,
            'data_endpoint': data_endpoint,
            'alert': (
                f'{parsed["system"]} has an injected incident around {inject_time}; '
                'diagnose the root cause from offline telemetry.'
            ),
        }
        input_payload['question'] = build_question({**parsed, 'modalities': modalities}, inject_time, data_endpoint)

        metadata = {
            'ordinal': ordinal,
            'rcaeval_suite': suite,
            'rcaeval_system': parsed['system'],
            'rcaeval_case_name': parsed['case_name'],
            'rcaeval_case_instance': parsed['instance'],
            'rcaeval_case_dir': str(case_dir.resolve()),
            'rcaeval_metric_file': metric_path.name,
        }
        record = {
            'case_id': case_id,
            'input': input_payload,
            'ground_truth': ground_truth,
            'metadata': metadata,
        }
        files = {
            'metrics': relative_or_absolute(metric_path, output_dir, relative_paths),
            'logs': relative_or_absolute(logs_path, output_dir, relative_paths) if logs_path.exists() else None,
            'traces': relative_or_absolute(traces_path, output_dir, relative_paths) if traces_path.exists() else None,
            'inject_time': (
                relative_or_absolute(inject_time_path, output_dir, relative_paths)
                if inject_time_path.exists()
                else None
            ),
        }

        records.append(record)
        index[case_id] = {
            'case_id': case_id,
            'input': input_payload,
            'ground_truth': ground_truth,
            'metadata': metadata,
            'files': files,
        }

        if limit and len(records) >= limit:
            break

    cases_output.parent.mkdir(parents=True, exist_ok=True)
    index_output.parent.mkdir(parents=True, exist_ok=True)
    with cases_output.open('w', encoding='utf-8') as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    index_output.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return records, index, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate EvalScope RCA benchmark cases from raw RCAEval data.')
    parser.add_argument('--raw-root', type=Path, required=True, help='Root directory containing RE1/RE2/RE3 RCAEval data.')
    parser.add_argument('--cases-output', type=Path, required=True, help='Output EvalScope JSONL cases file.')
    parser.add_argument('--index-output', type=Path, required=True, help='Output offline data API index JSON file.')
    parser.add_argument('--endpoint-base', default='http://127.0.0.1:18080', help='Public base URL of offline data API.')
    parser.add_argument('--window-seconds', type=int, default=600, help='Incident window centered around inject_time.')
    parser.add_argument('--suite', action='append', help='Optional suite filter, e.g. RE2-SS. Can be passed multiple times.')
    parser.add_argument('--limit', type=int, help='Optional maximum number of generated cases.')
    parser.add_argument(
        '--relative-paths',
        action='store_true',
        help='Write file paths relative to the index output directory when possible.',
    )
    args = parser.parse_args()

    suites = {suite.upper() for suite in args.suite} if args.suite else None
    records, _, warnings = build_records(
        raw_root=args.raw_root,
        cases_output=args.cases_output,
        index_output=args.index_output,
        endpoint_base=args.endpoint_base,
        window_seconds=args.window_seconds,
        suites=suites,
        limit=args.limit,
        relative_paths=args.relative_paths,
    )
    print(f'Generated {len(records)} RCAEval cases')
    print(f'Cases JSONL: {args.cases_output}')
    print(f'Offline API index: {args.index_output}')
    if warnings:
        print(f'Warnings: {len(warnings)}')
        for warning in warnings[:10]:
            print(f'- {warning}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
