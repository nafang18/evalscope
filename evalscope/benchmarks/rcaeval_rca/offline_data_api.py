# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import argparse
import csv
import json
import operator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds


ROOT = Path(__file__).resolve().parent
DEFAULT_INDEX = ROOT / 'data' / 'rca_data_index.json'

app = FastAPI(title='RCA Offline Data API', version='0.1.0')
INDEX_PATH = DEFAULT_INDEX
INDEX: dict[str, dict[str, Any]] = {}
MODALITIES = {'metrics', 'logs', 'traces'}
MAX_LIMIT = 10000
MAX_AGG_LIMIT = 1000
OPS = {
    '=': operator.eq,
    '==': operator.eq,
    '!=': operator.ne,
    '>': operator.gt,
    '>=': operator.ge,
    '<': operator.lt,
    '<=': operator.le,
}


class QueryRequest(BaseModel):
    select: list[str] | None = None
    where: list[list[Any]] = Field(default_factory=list)
    order_by: list[list[str]] = Field(default_factory=list)
    limit: int = Field(default=200, ge=1, le=MAX_LIMIT)
    cursor: int | None = Field(default=None, ge=0)


class AggregateMetric(BaseModel):
    field: str
    op: str
    alias: str | None = None


class AggregateRequest(BaseModel):
    where: list[list[Any]] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    metrics: list[AggregateMetric] = Field(default_factory=list)
    order_by: list[list[str]] = Field(default_factory=list)
    limit: int = Field(default=200, ge=1, le=MAX_AGG_LIMIT)


QueryRequest.model_rebuild(_types_namespace={'Any': Any})
AggregateMetric.model_rebuild(_types_namespace={'Any': Any})
AggregateRequest.model_rebuild(_types_namespace={'Any': Any, 'AggregateMetric': AggregateMetric})


def resolve_data_path(path_value: str | None, base_dir: Path) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    return base_dir / path


def load_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    base_dir = path.parent
    data = json.loads(path.read_text(encoding='utf-8'))
    for record in data.values():
        files = record.get('files', {})
        for key in ('metrics', 'logs', 'traces', 'inject_time'):
            resolved = resolve_data_path(files.get(key), base_dir)
            files[key] = str(resolved) if resolved else None
        store = record.get('store', {})
        for key in ('manifest', 'stats'):
            resolved = resolve_data_path(store.get(key), base_dir)
            store[key] = str(resolved) if resolved else None
        modalities = store.get('modalities', {})
        for modality_store in modalities.values():
            for key in ('parquet', 'stats'):
                resolved = resolve_data_path(modality_store.get(key), base_dir)
                modality_store[key] = str(resolved) if resolved else None
    return data


def public_case(record: dict[str, Any]) -> dict[str, Any]:
    store = record.get('store', {})
    capabilities = {}
    for modality in ('metrics', 'logs', 'traces'):
        if get_store_path(record, modality):
            capabilities[modality] = ['schema', 'stats', 'query', 'aggregate']
    return {
        'case_id': record['case_id'],
        'input': record['input'],
        'available_modalities': [
            key for key in ('metrics', 'logs', 'traces') if record['files'].get(key)
        ],
        'store_available': bool(store),
        'capabilities': capabilities,
    }


def get_case(case_id: str) -> dict[str, Any]:
    record = INDEX.get(case_id)
    if not record:
        raise HTTPException(status_code=404, detail=f'unknown case_id: {case_id}')
    return record


def read_csv_sample(path: Path, limit: int) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f'missing telemetry file: {path}')
    rows = []
    with path.open('r', encoding='utf-8', newline='') as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
            if len(rows) >= limit:
                break
        return {
            'path': str(path),
            'columns': reader.fieldnames or [],
            'rows': rows,
        }


def read_json_file(path_value: str | None) -> dict[str, Any]:
    if not path_value:
        raise HTTPException(status_code=404, detail='metadata unavailable')
    path = Path(path_value)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f'missing metadata file: {path}')
    return json.loads(path.read_text(encoding='utf-8'))


def get_store_path(record: dict[str, Any], modality: str) -> str | None:
    if modality not in MODALITIES:
        raise HTTPException(status_code=400, detail='modality must be metrics, logs, or traces')
    return record.get('store', {}).get('modalities', {}).get(modality, {}).get('parquet')


def get_stats_path(record: dict[str, Any], modality: str) -> str | None:
    return record.get('store', {}).get('modalities', {}).get(modality, {}).get('stats')


def get_dataset(record: dict[str, Any], modality: str) -> ds.Dataset:
    path_value = get_store_path(record, modality)
    if not path_value:
        raise HTTPException(status_code=404, detail=f'{modality} parquet store unavailable')
    path = Path(path_value)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f'missing parquet file: {path}')
    return ds.dataset(str(path), format='parquet')


def validate_columns(schema_names: set[str], columns: list[str] | None, label: str) -> None:
    if not columns:
        return
    missing = [column for column in columns if column not in schema_names]
    if missing:
        raise HTTPException(status_code=400, detail=f'unknown {label} columns: {missing}')


def build_filter(where: list[list[Any]], schema_names: set[str]) -> tuple[ds.Expression | None, list[list[Any]]]:
    expression = None
    post_filters: list[list[Any]] = []
    for condition in where:
        if len(condition) != 3:
            raise HTTPException(status_code=400, detail=f'invalid where condition: {condition}')
        column, op, value = condition
        if column not in schema_names:
            raise HTTPException(status_code=400, detail=f'unknown where column: {column}')
        field = ds.field(column)
        if op in OPS:
            condition_expr = OPS[op](field, value)
        elif op == 'in':
            if not isinstance(value, list):
                raise HTTPException(status_code=400, detail='in operator requires list value')
            condition_expr = field.isin(value)
        elif op == 'not in':
            if not isinstance(value, list):
                raise HTTPException(status_code=400, detail='not in operator requires list value')
            condition_expr = ~field.isin(value)
        elif op == 'contains':
            post_filters.append(condition)
            continue
        else:
            raise HTTPException(status_code=400, detail=f'unsupported operator: {op}')
        expression = condition_expr if expression is None else expression & condition_expr
    return expression, post_filters


def apply_post_filters(table: pa.Table, post_filters: list[list[Any]]) -> pa.Table:
    for column, op, value in post_filters:
        if op != 'contains':
            raise HTTPException(status_code=400, detail=f'unsupported post-filter operator: {op}')
        mask = pc.match_substring(table[column].cast(pa.string()), str(value))
        table = table.filter(mask)
    return table


def apply_order(table: pa.Table, order_by: list[list[str]]) -> pa.Table:
    if not order_by:
        return table
    sort_keys = []
    schema_names = set(table.column_names)
    for item in order_by:
        if not item:
            raise HTTPException(status_code=400, detail='empty order_by item')
        column = item[0]
        direction = item[1].lower() if len(item) > 1 else 'asc'
        if column not in schema_names:
            raise HTTPException(status_code=400, detail=f'unknown order_by column: {column}')
        if direction not in {'asc', 'desc'}:
            raise HTTPException(status_code=400, detail=f'unsupported order direction: {direction}')
        sort_keys.append((column, 'ascending' if direction == 'asc' else 'descending'))
    indices = pc.sort_indices(table, sort_keys=sort_keys)
    return pc.take(table, indices)


def table_to_rows(table: pa.Table) -> list[dict[str, Any]]:
    return table.to_pylist()


def query_table(dataset: ds.Dataset, request: QueryRequest) -> tuple[pa.Table, int | None]:
    schema_names = set(dataset.schema.names)
    validate_columns(schema_names, request.select, 'select')
    validate_columns(schema_names, [item[0] for item in request.order_by if item], 'order_by')
    filter_expr, post_filters = build_filter(request.where, schema_names)
    requested_columns = request.select[:] if request.select else None
    columns = requested_columns[:] if requested_columns else None
    if columns is not None:
        for condition in post_filters:
            if condition[0] not in columns:
                columns.append(condition[0])
        for item in request.order_by:
            if item and item[0] not in columns:
                columns.append(item[0])
    # Filtering is pushed down to Parquet. Ordering and cursor are applied after scan.
    table = dataset.to_table(columns=columns, filter=filter_expr)
    table = apply_post_filters(table, post_filters)
    table = apply_order(table, request.order_by)
    offset = request.cursor or 0
    sliced = table.slice(offset, request.limit)
    if requested_columns:
        sliced = sliced.select(requested_columns)
    next_cursor = offset + request.limit if offset + request.limit < table.num_rows else None
    return sliced, next_cursor


def aggregate_table(dataset: ds.Dataset, request: AggregateRequest) -> pa.Table:
    schema_names = set(dataset.schema.names)
    validate_columns(schema_names, request.group_by, 'group_by')
    validate_columns(schema_names, [metric.field for metric in request.metrics], 'metric')
    filter_expr, post_filters = build_filter(request.where, schema_names)
    needed_columns = sorted(set(request.group_by + [metric.field for metric in request.metrics]))
    for condition in post_filters:
        if condition[0] not in needed_columns:
            needed_columns.append(condition[0])
    table = dataset.to_table(columns=needed_columns or None, filter=filter_expr)
    table = apply_post_filters(table, post_filters)

    if not request.group_by:
        result: dict[str, list[Any]] = {}
        for metric in request.metrics:
            name = metric.alias or f'{metric.op}_{metric.field}'
            result[name] = [compute_aggregate(table[metric.field], metric.op)]
        return pa.table(result)

    aggregations = []
    rename_map: dict[str, str] = {}
    for metric in request.metrics:
        alias = metric.alias or f'{metric.op}_{metric.field}'
        function = aggregate_function(metric.op)
        aggregations.append((metric.field, function))
        rename_map[f'{metric.field}_{function}'] = alias
    grouped = table.group_by(request.group_by).aggregate(aggregations)
    renamed_columns = [rename_map.get(column, column) for column in grouped.column_names]
    grouped = grouped.rename_columns(renamed_columns)
    grouped = apply_order(grouped, request.order_by)
    return grouped.slice(0, request.limit)


def aggregate_function(op: str) -> str:
    mapping = {
        'count': 'count',
        'sum': 'sum',
        'avg': 'mean',
        'mean': 'mean',
        'min': 'min',
        'max': 'max',
    }
    if op not in mapping:
        raise HTTPException(status_code=400, detail=f'unsupported aggregate op: {op}')
    return mapping[op]


def compute_aggregate(array: pa.ChunkedArray, op: str) -> Any:
    if op == 'count':
        return len(array) - array.null_count
    if op == 'sum':
        return pc.sum(array).as_py()
    if op in {'avg', 'mean'}:
        return pc.mean(array).as_py()
    if op == 'min':
        return pc.min(array).as_py()
    if op == 'max':
        return pc.max(array).as_py()
    raise HTTPException(status_code=400, detail=f'unsupported aggregate op: {op}')


@app.on_event('startup')
def startup() -> None:
    global INDEX
    INDEX = load_index(INDEX_PATH)


@app.get('/health')
def health() -> dict[str, Any]:
    return {'status': 'ok', 'cases': len(INDEX)}


@app.get('/cases')
def list_cases() -> dict[str, Any]:
    return {'cases': [public_case(record) for record in INDEX.values()]}


@app.get('/cases/{case_id}')
def describe_case(case_id: str) -> dict[str, Any]:
    return public_case(get_case(case_id))


@app.get('/cases/{case_id}/catalog')
def get_catalog(case_id: str) -> dict[str, Any]:
    record = get_case(case_id)
    manifest_path = record.get('store', {}).get('manifest')
    if manifest_path:
        return read_json_file(manifest_path)
    return public_case(record)


@app.get('/cases/{case_id}/{modality}/schema')
def get_schema(case_id: str, modality: str) -> dict[str, Any]:
    record = get_case(case_id)
    stats = read_json_file(get_stats_path(record, modality))
    return {
        'case_id': case_id,
        'modality': modality,
        'schema': stats.get('schema', []),
        'columns': stats.get('columns', []),
    }


@app.get('/cases/{case_id}/{modality}/stats')
def get_stats(case_id: str, modality: str) -> dict[str, Any]:
    record = get_case(case_id)
    return read_json_file(get_stats_path(record, modality))


@app.post('/cases/{case_id}/{modality}/query')
def query_modality(case_id: str, modality: str, request: QueryRequest) -> dict[str, Any]:
    record = get_case(case_id)
    dataset = get_dataset(record, modality)
    table, next_cursor = query_table(dataset, request)
    return {
        'case_id': case_id,
        'modality': modality,
        'columns': table.column_names,
        'rows': table_to_rows(table),
        'row_count': table.num_rows,
        'next_cursor': next_cursor,
    }


@app.post('/cases/{case_id}/{modality}/aggregate')
def aggregate_modality(case_id: str, modality: str, request: AggregateRequest) -> dict[str, Any]:
    record = get_case(case_id)
    dataset = get_dataset(record, modality)
    table = aggregate_table(dataset, request)
    return {
        'case_id': case_id,
        'modality': modality,
        'columns': table.column_names,
        'rows': table_to_rows(table),
        'row_count': table.num_rows,
    }


@app.get('/cases/{case_id}/metrics')
def get_metrics(case_id: str, limit: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
    record = get_case(case_id)
    metrics = record['files'].get('metrics')
    if not metrics:
        raise HTTPException(status_code=404, detail='metrics unavailable')
    return read_csv_sample(Path(metrics), limit)


@app.get('/cases/{case_id}/logs')
def get_logs(case_id: str, limit: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
    record = get_case(case_id)
    logs = record['files'].get('logs')
    if not logs:
        raise HTTPException(status_code=404, detail='logs unavailable')
    return read_csv_sample(Path(logs), limit)


@app.get('/cases/{case_id}/traces')
def get_traces(case_id: str, limit: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
    record = get_case(case_id)
    traces = record['files'].get('traces')
    if not traces:
        raise HTTPException(status_code=404, detail='traces unavailable')
    return read_csv_sample(Path(traces), limit)


@app.get('/cases/{case_id}/raw/{modality}')
def raw_file(case_id: str, modality: str) -> PlainTextResponse:
    record = get_case(case_id)
    if modality not in {'metrics', 'logs', 'traces'}:
        raise HTTPException(status_code=400, detail='modality must be metrics, logs, or traces')
    path_value = record['files'].get(modality)
    if not path_value:
        raise HTTPException(status_code=404, detail=f'{modality} unavailable')
    path = Path(path_value)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f'missing telemetry file: {path}')
    return PlainTextResponse(path.read_text(encoding='utf-8'))


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description='Read-only offline telemetry API for RCA agent evaluation cases.')
    parser.add_argument('--index', type=Path, default=DEFAULT_INDEX)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18080)
    args = parser.parse_args()

    global INDEX_PATH
    INDEX_PATH = args.index
    uvicorn.run(app, host=args.host, port=args.port, log_level='info')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
