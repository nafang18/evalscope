# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import re
from statistics import mean
from typing import Any


def normalize(value: Any) -> str:
    if value is None:
        return ''
    text = str(value).lower().strip()
    return re.sub(r'[\s\-_]+', '', text)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def pick_prediction_field(prediction: dict[str, Any], field: str) -> Any:
    if field in prediction:
        return prediction[field]
    diagnosis = prediction.get('diagnosis')
    if isinstance(diagnosis, dict):
        return diagnosis.get(field)
    return None


def contains_match(candidate: Any, expected: Any) -> bool:
    expected_norm = normalize(expected)
    if not expected_norm:
        return False
    for item in as_list(candidate):
        if normalize(item) == expected_norm:
            return True
    return False


def loose_contains_match(candidate: Any, expected: Any) -> bool:
    expected_norm = normalize(expected)
    if not expected_norm:
        return False
    for item in as_list(candidate):
        candidate_norm = normalize(item)
        if candidate_norm == expected_norm:
            return True
        if expected_norm in candidate_norm or candidate_norm in expected_norm:
            return True
    return False


def contains_any_match(candidate: Any, expected_values: list[Any]) -> bool:
    return any(contains_match(candidate, expected) for expected in expected_values)


def contains_any_loose_match(candidate: Any, expected_values: list[Any]) -> bool:
    return any(loose_contains_match(candidate, expected) for expected in expected_values)


def contains_specific_indicator_match(candidate: Any, expected_values: list[Any], component: Any) -> bool:
    component_norm = normalize(component)
    for item in as_list(candidate):
        candidate_norm = normalize(item)
        if not candidate_norm:
            continue
        for expected in expected_values:
            expected_norm = normalize(expected)
            if not expected_norm:
                continue
            if candidate_norm == expected_norm:
                return True
            if component_norm and component_norm not in candidate_norm:
                continue
            if expected_norm in candidate_norm or candidate_norm in expected_norm:
                return True
    return False


def first_item(value: Any) -> Any:
    values = as_list(value)
    return values[0] if values else None


def service_from_indicator(value: Any) -> str:
    text = str(value or '').strip()
    if '_' not in text:
        return text
    return text.split('_', 1)[0]


def type_from_indicator(value: Any) -> str | None:
    text = str(value or '').strip()
    if '_' not in text:
        return None
    return text.rsplit('_', 1)[-1]


def canonical_indicator_family(value: Any) -> str:
    text = str(value or '').lower().strip()
    compact = normalize(text)
    if not compact:
        return ''
    if any(token in compact for token in ['stacktrace', 'exception', 'faultyfunction', 'codelocation']):
        return 'code'
    if 'stack' in compact:
        return 'stack'
    if any(token in compact for token in ['traceerror', 'statuscode', 'responsecode']):
        return 'trace'
    if any(token in compact for token in ['trace latency', 'tracelatency', 'duration']):
        return 'latency'
    if 'latency' in compact or 'delay' in compact:
        return 'latency'
    if any(token in compact for token in ['networkloss', 'packetloss', 'dropped', 'drop', 'networkerror']):
        return 'network_loss'
    if 'error' in compact:
        return 'error'
    if 'diskio' in compact or 'blkio' in compact or 'fswrite' in compact or 'fsread' in compact:
        return 'diskio'
    if 'socket' in compact:
        return 'socket'
    if 'memory' in compact or compact == 'mem' or compact.endswith('mem') or 'rss' in compact:
        return 'memory'
    if 'cpu' in compact:
        return 'cpu'
    return type_from_indicator(text) or text


def pick_first_prediction_field(prediction: dict[str, Any], fields: list[str]) -> Any:
    for field in fields:
        value = pick_prediction_field(prediction, field)
        if value is not None:
            return value
    return None


def path_f1(predicted: list[Any], expected: list[Any]) -> float | None:
    expected_set = {normalize(item) for item in expected if normalize(item)}
    if not expected_set:
        return None
    predicted_set = {normalize(item) for item in predicted if normalize(item)}
    if not predicted_set:
        return 0.0
    overlap = len(predicted_set & expected_set)
    precision = overlap / len(predicted_set)
    recall = overlap / len(expected_set)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def score_one(case: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    truth = case['ground_truth']
    component_pred = pick_prediction_field(prediction, 'root_cause_component')
    type_pred = pick_prediction_field(prediction, 'root_cause_type')
    indicator_pred = pick_prediction_field(prediction, 'root_cause_indicator')
    indicator_family_pred = pick_first_prediction_field(prediction, [
        'root_cause_indicator_family',
        'indicator_family',
        'root_cause_metric_family',
    ])
    ranked_indicators = (
        pick_prediction_field(prediction, 'ranked_root_cause_indicators')
        or pick_prediction_field(prediction, 'ranked_root_causes')
        or pick_prediction_field(prediction, 'ranks')
    )
    ranked_components = pick_prediction_field(prediction, 'ranked_root_cause_components')
    causal_path_pred = as_list(pick_prediction_field(prediction, 'causal_path'))
    evidence = as_list(pick_prediction_field(prediction, 'evidence'))

    if indicator_pred is None and ranked_indicators:
        indicator_pred = first_item(ranked_indicators)
    if component_pred is None and indicator_pred is not None:
        component_pred = service_from_indicator(indicator_pred)
    if type_pred is None and indicator_pred is not None:
        type_pred = type_from_indicator(indicator_pred)
    if indicator_family_pred is None and indicator_pred is not None:
        indicator_family_pred = canonical_indicator_family(indicator_pred)
    if ranked_components is None and ranked_indicators:
        ranked_components = [service_from_indicator(item) for item in as_list(ranked_indicators)]

    component_top1 = float(contains_match(component_pred, truth.get('root_cause_component')))
    indicator_expected = [
        truth.get('root_cause_indicator'),
        *as_list(truth.get('root_cause_indicator_aliases')),
    ]
    type_acc = float(contains_match(type_pred, truth.get('root_cause_type')))
    if truth.get('root_cause_indicator_granularity') == 'broad':
        indicator_acc = float(contains_any_loose_match(indicator_pred, indicator_expected))
    else:
        indicator_acc = float(
            contains_specific_indicator_match(
                indicator_pred,
                indicator_expected,
                truth.get('root_cause_component'),
            )
        )
    expected_families = [
        truth.get('root_cause_indicator_family'),
        *as_list(truth.get('root_cause_indicator_accepted_families')),
    ]
    predicted_family = canonical_indicator_family(indicator_family_pred)
    if not predicted_family and indicator_pred is not None:
        predicted_family = canonical_indicator_family(indicator_pred)
    family_acc = float(contains_any_match(predicted_family, expected_families))

    ranked = as_list(ranked_components) or as_list(component_pred)
    top3 = float(any(contains_match(item, truth.get('root_cause_component')) for item in ranked[:3]))
    ranked_indicator_values = as_list(ranked_indicators) or as_list(indicator_pred)
    if truth.get('root_cause_indicator_granularity') == 'broad':
        indicator_top3 = float(any(contains_any_loose_match(item, indicator_expected) for item in ranked_indicator_values[:3]))
    else:
        indicator_top3 = float(
            any(
                contains_specific_indicator_match(item, indicator_expected, truth.get('root_cause_component'))
                for item in ranked_indicator_values[:3]
            )
        )
    causal_f1 = path_f1(causal_path_pred, truth.get('causal_path', []))
    evidence_present = float(bool(evidence))
    evidence_text = ' '.join(str(item) for item in evidence)
    evidence_family_acc = float(
        evidence_present and contains_any_match(canonical_indicator_family(evidence_text), expected_families)
    )
    overall = mean([component_top1, type_acc, family_acc])
    overall_strict = mean([component_top1, type_acc, family_acc, indicator_acc])

    scores = {
        'overall_strict': overall_strict,
        'root_cause_component_top1': component_top1,
        'root_cause_component_top3': top3,
        'root_cause_type_accuracy': type_acc,
        'root_cause_indicator_family_accuracy': family_acc,
        'root_cause_indicator_accuracy': indicator_acc,
        'root_cause_indicator_top3': indicator_top3,
        'root_cause_evidence_family_accuracy': evidence_family_acc,
        'evidence_present': evidence_present,
        'overall_minimal': overall,
    }
    if causal_f1 is not None:
        scores['causal_path_f1'] = causal_f1

    return {
        'case_id': case['case_id'],
        'scores': scores,
        'prediction_extract': {
            'root_cause_component': component_pred,
            'root_cause_type': type_pred,
            'root_cause_indicator_family': predicted_family,
            'root_cause_indicator': indicator_pred,
            'ranked_root_cause_indicators': ranked_indicator_values,
            'ranked_root_cause_components': ranked,
        },
        'ground_truth': truth,
    }
