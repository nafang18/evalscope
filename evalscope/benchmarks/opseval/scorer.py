# Copyright (c) Alibaba, Inc. and its affiliates.

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


LETTER_RE = re.compile(
    r'(?:答案|answer|option|选项|选择|choose|choice)\s*[:：]?\s*[\(\[]?\s*([A-J])\s*[\)\]]?',
    re.IGNORECASE,
)
BARE_LETTER_RE = re.compile(r'(?<![A-Za-z])([A-J])(?![A-Za-z])', re.IGNORECASE)


@dataclass
class JudgeConfig:
    api_url: str = ''
    api_key: str = ''
    model: str = ''
    timeout: int = 120


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalise_letters(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        letters = []
        for item in value:
            letters.extend(normalise_letters(item))
        return sorted(set(letters))
    text = str(value).strip().upper()
    if not text:
        return []
    explicit = re.match(r'^\s*[\(\[]?\s*([A-J])\s*[\)\]\.、:：-]?(?:\s|$)', text)
    if explicit:
        return [explicit.group(1)]
    letters = [m.group(1).upper() for m in BARE_LETTER_RE.finditer(text)]
    return sorted(set(letters))


def extract_mcq_letters(prediction: str, max_choices: int = 10) -> list[str]:
    text = (prediction or '').strip()
    if not text:
        return []

    valid_letters = set(chr(ord('A') + i) for i in range(max(1, max_choices)))
    matches = [m.group(1).upper() for m in LETTER_RE.finditer(text)]
    letters = [m for m in matches if m in valid_letters]
    if letters:
        return sorted(set(letters))

    first_line = text.splitlines()[0] if text.splitlines() else text
    if len(first_line.strip()) <= 8:
        letters = [m.group(1).upper() for m in BARE_LETTER_RE.finditer(first_line)]
        letters = [m for m in letters if m in valid_letters]
        if letters:
            return sorted(set(letters))

    # Fall back to the last visible standalone letter; models often conclude with it.
    letters = [m.group(1).upper() for m in BARE_LETTER_RE.finditer(text)]
    letters = [m for m in letters if m in valid_letters]
    return sorted(set(letters[-1:]))


def score_mcq(prediction: str, answer: Any, max_choices: int = 10) -> dict[str, Any]:
    pred_letters = extract_mcq_letters(prediction, max_choices=max_choices)
    gold_letters = normalise_letters(answer)
    exact = float(pred_letters == gold_letters and bool(gold_letters))

    pred_set = set(pred_letters)
    gold_set = set(gold_letters)
    if pred_set and gold_set:
        precision = len(pred_set & gold_set) / len(pred_set)
        recall = len(pred_set & gold_set) / len(gold_set)
        partial_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    else:
        precision = 0.0
        recall = 0.0
        partial_f1 = 0.0

    return {
        'scores': {
            'sample_score': exact,
            'mcq_accuracy': exact,
            'mcq_partial_f1': partial_f1,
        },
        'prediction_extract': {
            'predicted_answer': pred_letters,
            'gold_answer': gold_letters,
        },
        'explanation': 'MCQ exact-match scorer.',
        'metadata': {
            'mcq_precision': precision,
            'mcq_recall': recall,
        },
    }


def _contains_point(answer: str, point: str) -> bool:
    answer_norm = re.sub(r'\s+', '', answer).lower()
    point_norm = re.sub(r'\s+', '', point).lower()
    if not point_norm:
        return False
    if point_norm in answer_norm:
        return True

    # Conservative token overlap fallback for English-like points.
    answer_tokens = set(re.findall(r'[A-Za-z0-9_./:-]+', answer.lower()))
    point_tokens = set(re.findall(r'[A-Za-z0-9_./:-]+', point.lower()))
    if len(point_tokens) >= 2:
        return len(answer_tokens & point_tokens) / len(point_tokens) >= 0.75
    return False


def keypoint_score(prediction: str, reference_points: list[str]) -> dict[str, Any]:
    points = [str(p).strip() for p in reference_points if str(p).strip()]
    if not points:
        return {
            'accuracy': 0.0,
            'evidence': 0.0,
            'fluency': 1.0 if prediction.strip() else 0.0,
            'matched_points': [],
            'missing_points': [],
            'reason': 'No reference_points were provided; keypoint fallback cannot score accuracy.',
        }

    matched = [point for point in points if _contains_point(prediction, point)]
    missing = [point for point in points if point not in matched]
    accuracy = len(matched) / len(points)
    evidence = min(1.0, accuracy + 0.15) if matched else 0.0
    fluency = 1.0 if len(prediction.strip()) >= 8 else 0.0
    return {
        'accuracy': accuracy,
        'evidence': evidence,
        'fluency': fluency,
        'matched_points': matched,
        'missing_points': missing,
        'reason': 'Fallback keypoint coverage scorer.',
    }


def extract_json_object(text: str) -> dict[str, Any]:
    text = (text or '').strip()
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


def chat_completions_url(api_url: str) -> str:
    url = api_url.rstrip('/')
    if url.endswith('/chat/completions'):
        return url
    return url + '/chat/completions'


def judge_fae(
    *,
    question: str,
    reference_answer: str,
    reference_points: list[str],
    prediction: str,
    config: JudgeConfig,
) -> dict[str, Any]:
    if not config.api_url or not config.model:
        return keypoint_score(prediction, reference_points or [reference_answer])

    payload = {
        'model': config.model,
        'messages': [
            {
                'role': 'system',
                'content': (
                    'You are an impartial IT operations evaluation judge. '
                    'Score the candidate answer using FAE: fluency, accuracy, evidence. '
                    'Return JSON only with fields fluency, accuracy, evidence, reason, '
                    'matched_points, missing_points. Scores must be numbers from 0 to 1.'
                ),
            },
            {
                'role': 'user',
                'content': dump_json({
                    'question': question,
                    'reference_answer': reference_answer,
                    'reference_points': reference_points,
                    'candidate_answer': prediction,
                    'scoring_rubric': {
                        'fluency': 'Clear, coherent, and operationally readable.',
                        'accuracy': 'Correctly covers the reference answer and required key points.',
                        'evidence': 'Provides concrete operational rationale, signals, commands, or checks.',
                    },
                }),
            },
        ],
        'temperature': 0,
        'response_format': {'type': 'json_object'},
    }
    headers = {'Authorization': f'Bearer {config.api_key}'} if config.api_key else {}
    response = http_json(chat_completions_url(config.api_url), payload, timeout=config.timeout, headers=headers)
    content = response['choices'][0]['message']['content']
    result = extract_json_object(content)
    return {
        'fluency': _clamp01(result.get('fluency', 0.0)),
        'accuracy': _clamp01(result.get('accuracy', 0.0)),
        'evidence': _clamp01(result.get('evidence', 0.0)),
        'reason': str(result.get('reason', '')),
        'matched_points': result.get('matched_points', []),
        'missing_points': result.get('missing_points', []),
    }


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


def score_qa(
    *,
    question: str,
    reference_answer: str,
    reference_points: list[str],
    prediction: str,
    judge_config: JudgeConfig,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    weights = weights or {'fluency': 0.2, 'accuracy': 0.6, 'evidence': 0.2}
    fae = judge_fae(
        question=question,
        reference_answer=reference_answer,
        reference_points=reference_points,
        prediction=prediction,
        config=judge_config,
    )
    fluency = _clamp01(fae.get('fluency', 0.0))
    accuracy = _clamp01(fae.get('accuracy', 0.0))
    evidence = _clamp01(fae.get('evidence', 0.0))
    total_weight = sum(max(0.0, float(v)) for v in weights.values()) or 1.0
    qa_score = (
        max(0.0, float(weights.get('fluency', 0.0))) * fluency
        + max(0.0, float(weights.get('accuracy', 0.0))) * accuracy
        + max(0.0, float(weights.get('evidence', 0.0))) * evidence
    ) / total_weight

    return {
        'scores': {
            'sample_score': qa_score,
            'qa_score': qa_score,
            'qa_fluency': fluency,
            'qa_accuracy': accuracy,
            'qa_evidence': evidence,
        },
        'prediction_extract': {
            'matched_points': fae.get('matched_points', []),
            'missing_points': fae.get('missing_points', []),
        },
        'explanation': str(fae.get('reason', 'FAE scorer.')),
        'metadata': {
            'reference_points': reference_points,
        },
    }
