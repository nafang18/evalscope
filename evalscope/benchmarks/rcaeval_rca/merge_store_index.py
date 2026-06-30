# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MODALITIES = ('metrics', 'logs', 'traces')


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')


def build_store_metadata(store_root: Path, case_id: str) -> dict[str, Any] | None:
    case_dir = store_root / case_id
    manifest = case_dir / 'manifest.json'
    stats = case_dir / 'stats.json'
    if not manifest.exists() or not stats.exists():
        return None

    modalities: dict[str, dict[str, str]] = {}
    for modality in MODALITIES:
        parquet = case_dir / f'{modality}.parquet'
        modality_stats = case_dir / f'{modality}.stats.json'
        if parquet.exists() and modality_stats.exists():
            modalities[modality] = {
                'parquet': str(parquet.resolve()),
                'stats': str(modality_stats.resolve()),
            }
    if not modalities:
        return None

    return {
        'manifest': str(manifest.resolve()),
        'stats': str(stats.resolve()),
        'modalities': modalities,
    }


def merge_store_metadata(
    new_index_path: Path,
    old_store_index_path: Path | None,
    output_path: Path,
    store_root: Path | None,
) -> dict[str, int]:
    new_index = read_json(new_index_path)
    old_index = read_json(old_store_index_path) if old_store_index_path and old_store_index_path.exists() else {}
    merged = dict(new_index)
    copied = 0
    built = 0
    missing = 0

    for case_id, record in merged.items():
        old_record = old_index.get(case_id, {})
        store = old_record.get('store')
        if store:
            record['store'] = store
            copied += 1
        elif store_root:
            store = build_store_metadata(store_root, case_id)
            if store:
                record['store'] = store
                built += 1
            else:
                missing += 1
        else:
            missing += 1

    write_json(output_path, merged)
    return {'total': len(merged), 'copied': copied, 'built': built, 'missing': missing}


def main() -> int:
    parser = argparse.ArgumentParser(description='Copy existing RCAEval store metadata onto a regenerated index.')
    parser.add_argument('--new-index', type=Path, required=True, help='Regenerated RCAEval index JSON.')
    parser.add_argument('--old-store-index', type=Path, help='Previous store index JSON with store metadata.')
    parser.add_argument('--store-root', type=Path, help='Existing RCAEval store directory.')
    parser.add_argument('--output-index', type=Path, required=True, help='Merged output store index JSON.')
    args = parser.parse_args()

    summary = merge_store_metadata(args.new_index, args.old_store_index, args.output_index, args.store_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary['missing'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
