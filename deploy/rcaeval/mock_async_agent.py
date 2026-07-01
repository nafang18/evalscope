#!/usr/bin/env python3
"""Tiny async RCA agent mock for validating the EvalScope RCAEval adapter."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


TASKS: dict[str, dict] = {}


def default_answer(case_id: str) -> dict:
    return {
        'case_id': case_id,
        'root_cause_component': 'adservice',
        'root_cause_type': 'cpu',
        'root_cause_indicator_family': 'cpu',
        'root_cause_indicator': 'adservice_cpu',
        'ranked_root_cause_components': ['adservice'],
        'ranked_root_cause_indicators': ['adservice_cpu'],
        'confidence': 0.9,
        'evidence': [{
            'source': 'metrics',
            'query': 'POST /metrics/aggregate',
            'finding': 'adservice CPU usage is abnormal after the injection time.',
        }],
        'causal_path': [],
        'recommended_fix': 'Inspect adservice CPU saturation and scale or restart the service.',
    }


class Handler(BaseHTTPRequestHandler):
    def _read_json(self) -> dict:
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def _send_json(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        return

    def do_POST(self) -> None:
        if self.path.startswith('/status'):
            payload = self._read_json()
            self._send_status(str(payload.get('task_id') or ''))
            return

        payload = self._read_json()
        case_id = str(payload.get('case_id') or 'unknown-case')
        task_id = f'{case_id}-task'
        TASKS[task_id] = {
            'case_id': case_id,
            'polls': 0,
            'trace': [{
                'event': 'start',
                'case_id': case_id,
                'data_endpoint': payload.get('data_endpoint'),
            }],
        }
        self._send_json({
            'task_id': task_id,
            'status': 'running',
            'trace': TASKS[task_id]['trace'],
        })

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        self._send_status(str(query.get('task_id', [''])[0]))

    def _send_status(self, task_id: str) -> None:
        task = TASKS.setdefault(task_id, {'case_id': 'unknown-case', 'polls': 0, 'trace': []})
        task['polls'] += 1
        task['trace'].append({
            'event': 'query',
            'source': 'metrics',
            'operation': 'aggregate',
            'poll': task['polls'],
        })
        status = 'running' if task['polls'] == 1 else 'completed'
        response = {
            'task_id': task_id,
            'status': status,
            'trace': task['trace'],
        }
        if status == 'completed':
            response['answer'] = default_answer(task['case_id'])
        self._send_json(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=17070)
    args = parser.parse_args()
    HTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
