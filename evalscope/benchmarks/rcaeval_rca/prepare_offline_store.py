# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq


MODALITIES = ('metrics', 'logs', 'traces')
PRIMARY_TIME_COLUMNS = {
    'metrics': ('time',),
    'logs': ('timestamp',),
    'traces': ('startTimeMillis', 'startTime'),
}
SERVICE_COLUMNS = {
    'logs': ('container_name',),
    'traces': ('serviceName',),
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')


def jsonable(value: Any) -> Any:
    if hasattr(value, 'as_py'):
        value = value.as_py()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def resolve_path(path_value: str | None, base_dir: Path) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    return base_dir / path


def csv_to_table(path: Path) -> pa.Table:
    table = pacsv.read_csv(
        path,
        read_options=pacsv.ReadOptions(block_size=8 * 1024 * 1024, use_threads=True),
        parse_options=pacsv.ParseOptions(newlines_in_values=True),
        convert_options=pacsv.ConvertOptions(strings_can_be_null=True),
    )
    return table.rename_columns(make_unique_columns(table.column_names))


def make_unique_columns(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique = []
    for column in columns:
        count = seen.get(column, 0)
        seen[column] = count + 1
        unique.append(column if count == 0 else f'{column}.{count}')
    return unique


def write_parquet(table: pa.Table, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    for compression in ('zstd', 'snappy', None):
        try:
            pq.write_table(table, path, compression=compression, use_dictionary=True)
            return compression or 'none'
        except Exception:
            if path.exists():
                path.unlink()
    raise RuntimeError(f'failed to write parquet: {path}')


def min_max(table: pa.Table, column: str) -> dict[str, Any] | None:
    if column not in table.column_names:
        return None
    values = table[column]
    if values.null_count == len(values):
        return None
    return {
        'min': jsonable(pc.min(values)),
        'max': jsonable(pc.max(values)),
    }


def limited_unique(table: pa.Table, column: str, limit: int = 200) -> list[Any]:
    if column not in table.column_names:
        return []
    values = pc.unique(table[column]).to_pylist()
    clean = [jsonable(value) for value in values if value is not None]
    return sorted(clean, key=lambda item: str(item))[:limit]


def metric_services(columns: list[str]) -> list[str]:
    services = set()
    for column in columns:
        if column == 'time' or '_' not in column:
            continue
        services.add(column.split('_', 1)[0])
    return sorted(services)


def modality_stats(modality: str, table: pa.Table, source_path: Path, parquet_path: Path,
                   compression: str) -> dict[str, Any]:
    schema = [{'name': field.name, 'type': str(field.type)} for field in table.schema]
    time_columns = [column for column in PRIMARY_TIME_COLUMNS.get(modality, ()) if column in table.column_names]
    service_columns = [column for column in SERVICE_COLUMNS.get(modality, ()) if column in table.column_names]
    stats: dict[str, Any] = {
        'modality': modality,
        'row_count': table.num_rows,
        'column_count': table.num_columns,
        'columns': table.column_names,
        'schema': schema,
        'source_file': str(source_path.resolve()),
        'source_size_bytes': source_path.stat().st_size,
        'parquet_file': str(parquet_path.resolve()),
        'parquet_size_bytes': parquet_path.stat().st_size,
        'parquet_compression': compression,
        'time_columns': time_columns,
    }
    if time_columns:
        stats['time_range'] = {column: min_max(table, column) for column in time_columns}
    if modality == 'metrics':
        stats['services'] = metric_services(table.column_names)
    for column in service_columns:
        stats[f'{column}_values'] = limited_unique(table, column)
    if modality == 'logs':
        stats['level_values'] = limited_unique(table, 'level')
    if modality == 'traces':
        stats['statusCode_values'] = limited_unique(table, 'statusCode')
    return stats


def prepare_modality(case_id: str, modality: str, source_path: Path, case_store_dir: Path,
                     force: bool) -> dict[str, Any]:
    parquet_path = case_store_dir / f'{modality}.parquet'
    stats_path = case_store_dir / f'{modality}.stats.json'
    if parquet_path.exists() and stats_path.exists() and not force:
        return read_json(stats_path)

    table = csv_to_table(source_path)
    compression = write_parquet(table, parquet_path)
    stats = modality_stats(modality, table, source_path, parquet_path, compression)
    stats['case_id'] = case_id
    write_json(stats_path, stats)
    return stats


def public_manifest(record: dict[str, Any], modality_stats_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    input_payload = record.get('input', {})
    manifest = {
        'case_id': record['case_id'],
        'source': input_payload.get('source'),
        'dataset': input_payload.get('dataset'),
        'suite': input_payload.get('suite'),
        'system': input_payload.get('system'),
        'inject_time': input_payload.get('inject_time'),
        'start_time': input_payload.get('start_time'),
        'end_time': input_payload.get('end_time'),
        'modalities': sorted(modality_stats_map),
        'capabilities': {
            modality: ['schema', 'stats', 'query', 'aggregate']
            for modality in sorted(modality_stats_map)
        },
        'modalities_info': {
            modality: {
                'row_count': stats['row_count'],
                'column_count': stats['column_count'],
                'columns': stats['columns'],
                'time_columns': stats.get('time_columns', []),
                'time_range': stats.get('time_range', {}),
            }
            for modality, stats in sorted(modality_stats_map.items())
        },
    }
    return manifest


def prepare_store(index_path: Path, store_root: Path, output_index: Path, force: bool,
                  limit: int | None, case_ids: set[str] | None) -> dict[str, Any]:
    base_dir = index_path.resolve().parent
    index = read_json(index_path)
    output_index_data = dict(index)
    processed = 0
    skipped = 0

    for case_id in sorted(index):
        if case_ids and case_id not in case_ids:
            continue
        if limit is not None and processed >= limit:
            break

        record = dict(index[case_id])
        files = record.get('files', {})
        case_store_dir = store_root / case_id
        if force and case_store_dir.exists():
            shutil.rmtree(case_store_dir)
        case_store_dir.mkdir(parents=True, exist_ok=True)

        modality_stats_map: dict[str, dict[str, Any]] = {}
        modality_store: dict[str, dict[str, Any]] = {}
        for modality in MODALITIES:
            source_path = resolve_path(files.get(modality), base_dir)
            if not source_path or not source_path.exists():
                continue
            stats = prepare_modality(case_id, modality, source_path, case_store_dir, force)
            modality_stats_map[modality] = stats
            modality_store[modality] = {
                'parquet': stats['parquet_file'],
                'stats': str((case_store_dir / f'{modality}.stats.json').resolve()),
            }

        if not modality_stats_map:
            skipped += 1
            continue

        manifest = public_manifest(record, modality_stats_map)
        manifest_path = case_store_dir / 'manifest.json'
        stats_path = case_store_dir / 'stats.json'
        write_json(manifest_path, manifest)
        write_json(stats_path, {'case_id': case_id, 'modalities': modality_stats_map})

        record['store'] = {
            'manifest': str(manifest_path.resolve()),
            'stats': str(stats_path.resolve()),
            'modalities': modality_store,
        }
        output_index_data[case_id] = record
        processed += 1
        if processed % 25 == 0:
            print(f'Prepared {processed} cases...')

    write_json(output_index, output_index_data)
    return {
        'processed': processed,
        'skipped': skipped,
        'output_index': str(output_index.resolve()),
        'store_root': str(store_root.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Prepare a Parquet-backed offline store for RCAEval telemetry.')
    parser.add_argument('--index', type=Path, required=True, help='Input RCAEval data index JSON.')
    parser.add_argument('--store-root', type=Path, required=True, help='Output store directory.')
    parser.add_argument('--output-index', type=Path, required=True, help='Output index with store metadata.')
    parser.add_argument('--force', action='store_true', help='Regenerate existing parquet and stats files.')
    parser.add_argument('--limit', type=int, help='Maximum number of cases to prepare.')
    parser.add_argument('--case-id', action='append', help='Specific case_id to prepare. Can be used multiple times.')
    args = parser.parse_args()

    result = prepare_store(
        index_path=args.index,
        store_root=args.store_root,
        output_index=args.output_index,
        force=args.force,
        limit=args.limit,
        case_ids=set(args.case_id) if args.case_id else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
