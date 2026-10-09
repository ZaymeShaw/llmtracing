#!/usr/bin/env python3
"""Tracing grader pilot: real local LiteLLM, controlled upstream, public CLI.

No provider credentials or external API calls. Product source is never edited.
The five pilot scenarios are T01/T04/T10/T14/T15 of the approved input proposal.
Other approved scenarios remain pending, not implicitly counted as passing.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
PYTHON = ROOT / 'llm_gateway/.venv/bin/python'
PILOT = ('T01', 'T04', 'T10', 'T14', 'T15')
METRICS = ('attribution_exact', 'capture_complete', 'content_faithful',
           'state_truthful', 'provenance_truthful', 'redaction_correct')
FAKE_KEY = 'sk-synthetic-eval-only-0123456789'
TOOL = {'type': 'function', 'function': {'name': 'lookup', 'description': 'Synthetic lookup',
        'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}}


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def http(url, body=None, headers=None):
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=35) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Upstream:
    """Independent oracle: captures received bytes and its own emitted response."""
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self.pending_seen, self.release_pending = threading.Event(), threading.Event()
        self.rows = []

    def append(self, row):
        with self.lock:
            self.rows.append(row)
            with self.path.open('a') as f:
                f.write(json.dumps(row) + '\n')

    def handler(self):
        upstream = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                messages = body.get('messages', [])
                last = messages[-1] if messages else {}
                marker = str(last.get('content', ''))
                upstream.append({'event': 'received', 'path': self.path, 'body': body,
                                 'marker': marker, 'monotonic': time.monotonic()})
                if marker == 'PENDING':
                    upstream.pending_seen.set()
                    upstream.release_pending.wait(25)
                if marker.startswith('CONCURRENT:'):
                    time.sleep((3 - int(marker.split(':')[1])) * 0.04)
                status = 400 if marker == 'ERROR' else 200
                if status == 400:
                    response = {'error': {'message': 'CONTROLLED_UPSTREAM_ERROR',
                                         'type': 'invalid_request_error', 'code': 'invalid_request_error'}}
                else:
                    msg = {'role': 'assistant', 'content': 'ANSWER:' + marker}
                    finish = 'stop'
                    if marker == 'Q1':
                        msg = {'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': 'tool-1', 'type': 'function', 'function':
                             {'name': 'lookup', 'arguments': '{"q":"Q1"}'}}]}
                        finish = 'tool_calls'
                    response = {'id': 'fixture-' + hashlib.sha256(marker.encode()).hexdigest()[:12],
                                'object': 'chat.completion', 'created': 1700000000, 'model': 'trace-fixture',
                                'choices': [{'index': 0, 'message': msg, 'finish_reason': finish}],
                                'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}}
                upstream.append({'event': 'sent', 'marker': marker, 'status': status,
                                 'body': response, 'monotonic': time.monotonic()})
                raw = json.dumps(response).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        return Handler


@contextmanager
def gateway(out, *, upstream_type=Upstream, extra_env=None, runtime_setup=None):
    runtime = out / '_runtime'
    runtime.mkdir(parents=True)
    upstream = upstream_type(runtime / 'upstream.jsonl')
    server = ThreadingHTTPServer(('127.0.0.1', 0), upstream.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = free_port()
    gateway_dir = runtime / 'gateway'
    gateway_dir.mkdir()
    for name in ('start_litellm.sh', 'custom_callbacks.py'):
        shutil.copy2(ROOT / 'llm_gateway' / name, gateway_dir / name)
    shutil.copytree(ROOT / 'llm_gateway/callbacks', gateway_dir / 'callbacks',
                    ignore=shutil.ignore_patterns('__pycache__', 'tests'))
    (gateway_dir / '.venv').symlink_to(ROOT / 'llm_gateway/.venv', target_is_directory=True)
    log = runtime / 'logs/llm_calls.jsonl'
    config = gateway_dir / 'config.yaml'
    config.write_text(f'''model_list:
  - model_name: trace-fixture
    litellm_params:
      model: openai/trace-fixture
      api_base: http://127.0.0.1:{server.server_port}/v1
      api_key: synthetic-upstream-key
litellm_settings:
  callbacks: custom_callbacks.proxy_handler_instance
  drop_params: true
  num_retries: 0
router_settings:
  num_retries: 0
''')
    (gateway_dir / '.env.pilot').write_text('\n'.join([
        f'UPSTREAM_API_BASE=http://127.0.0.1:{server.server_port}/v1',
        'UPSTREAM_API_KEY=synthetic-upstream-key', 'UPSTREAM_PROTOCOL=openai', 'UPSTREAM_MODEL=trace-fixture',
        'LITELLM_HOST=127.0.0.1', f'LITELLM_PORT={port}', f'LITELLM_MASTER_KEY={FAKE_KEY}',
        f'LLM_GATEWAY_LOG_DIR={log.parent}', f'LITELLM_CONFIG={config}', 'GATEWAY_DIRECT_UPSTREAM=1']) + '\n')
    (runtime / 'home').mkdir()
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(runtime / 'home'),
           'TMPDIR': '/tmp', 'LANG': 'en_US.UTF-8', 'LITELLM_MODE': 'PRODUCTION',
           'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'NO_PROXY': '*', 'no_proxy': '*',
           'DO_NOT_TRACK': '1'}
    env.update(extra_env or {})
    if runtime_setup:
        runtime_setup(gateway_dir)
    proc = None
    try:
        with (runtime / 'gateway-process.log').open('w') as stdout:
            proc = subprocess.Popen(['bash', str(gateway_dir / 'start_litellm.sh'),
                                     '--profile', 'pilot', '--foreground'], cwd=gateway_dir,
                                    env=env, stdout=stdout, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f'Gateway exited {proc.returncode}; see {stdout.name}')
                try:
                    if http(f'http://127.0.0.1:{port}/health/liveliness')[0] == 200:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError('Gateway startup timed out')
            yield f'http://127.0.0.1:{port}', log, upstream
    finally:
        upstream.release_pending.set()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class Pilot:
    def __init__(self, out, url, log, upstream):
        self.out, self.url, self.log, self.upstream = out, url, log, upstream
        self.run_id = out.name
        self.clients = []
        self.lock = threading.Lock()

    def request(self, case, execution, marker, *, messages=None, secret=False):
        headers = {'X-Eval-Run-Id': self.run_id, 'X-Eval-Case-Id': case,
                   'X-Eval-Execution-Id': execution, 'X-Eval-Harness': 'build-eval-pilot',
                   'Authorization': 'Bearer ' + FAKE_KEY}
        body = {'model': 'trace-fixture', 'messages': messages or [
                    {'role': 'system', 'content': 'SYSTEM'}, {'role': 'user', 'content': marker}],
                'max_tokens': 100}
        if marker in ('Q1', 'Q2'):
            body['tools'] = [TOOL]
        if secret:
            headers['Authorization'] = 'Bearer ' + FAKE_KEY
            body['metadata'] = {'api_key': FAKE_KEY, 'note': 'PUBLIC_MARKER'}
        start = time.monotonic()
        status, response = http(self.url + '/v1/chat/completions', body, headers)
        row = {'case_id': case, 'execution_id': execution, 'marker': marker,
               'request': body, 'headers': headers, 'http_status': status,
               'response': response, 'latency_s': time.monotonic() - start}
        with self.lock:
            self.clients.append(row)
        # An unexpected HTTP outcome invalidates the fixture, not product tracing.
        if status != (400 if marker == 'ERROR' else 200):
            raise RuntimeError(f'{case}/{marker}: unexpected HTTP {status}: {response}')
        return row

    def cli(self, case, mode='json', *, report=None):
        args = [str(PYTHON), str(ROOT / 'llmtrace'), 'query', 'calls', '--log', str(self.log),
                '--run-id', self.run_id, '--case-id', case, '--format', mode]
        if mode == 'json':
            args += ['--events']
        if report:
            args += ['--html', str(report)]
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=20)
        if result.returncode not in (0, 1, 3):
            raise RuntimeError(f'Query CLI failed: {result.stderr}')
        if mode == 'pairs':
            return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return json.loads(result.stdout)

    def snapshot(self, case, expected_count, *, name='final'):
        # The caller has finished; give asynchronous logging a bounded chance.
        deadline = time.monotonic() + 7
        while True:
            envelope = self.cli(case)
            if envelope.get('counts', {}).get('total', 0) >= expected_count and not envelope.get('pending_call_ids'):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        case_dir = self.out / 'baseline/artifacts' / case
        case_dir.mkdir(parents=True, exist_ok=True)
        dump(case_dir / f'{name}-query.json', envelope)
        pairs = self.cli(case, mode='pairs', report=case_dir / name)
        dump(case_dir / f'{name}-pairs.json', pairs)
        # Exercise the README's report entry point independently of query export.
        direct = subprocess.run([str(PYTHON), str(ROOT / 'llmtrace'), 'report', 'html',
                  '--log', str(self.log), '--out', str(case_dir / 'direct-report'),
                  '--run-id', self.run_id, '--cases', case], cwd=ROOT, text=True,
                  capture_output=True, timeout=20)
        if direct.returncode:
            raise RuntimeError('Report CLI failed: ' + direct.stderr)
        direct_html = (case_dir / 'direct-report/llm_trace.html').read_text()
        html_path = case_dir / name / 'llm_trace.html'
        html = html_path.read_text()
        match = re.search(r'<script id="DATA" type="application/json">(.*?)</script>', html, re.S)
        if not match:
            raise RuntimeError('HTML export missing DATA payload')
        return {'envelope': envelope, 'pairs': pairs, 'payload': json.loads(match.group(1)), 'html': html,
                'direct_html': direct_html, 'gateway_log': self.log.read_text()}


def check(checks, metric, label, expected, actual):
    checks.append({'metric': metric, 'label': label, 'expected': expected, 'actual': actual,
                   'passed': expected == actual})


def grade(expected, observation):
    """Independent contract checks; reads CLI/HTML artifacts, never SUT helpers."""
    checks = []
    calls = observation['envelope'].get('calls', [])
    actual_counts = {}
    for call in calls:
        eid = call.get('execution_id')
        actual_counts[eid] = actual_counts.get(eid, 0) + 1
    check(checks, 'capture_complete', '调用按 execution 分组后的数量', expected['executions'], actual_counts)
    for call in calls:
        identity = {k: call.get(k) for k in ('run_id', 'case_id', 'harness')}
        check(checks, 'attribution_exact', f"{call['call_id']} 身份", expected['identity'], identity)
    check(checks, 'state_truthful', '调用状态计数', expected['states'],
          {s: sum(c.get('state') == s for c in calls) for s in expected['states']})
    check(checks, 'capture_complete', '每个 call_id 唯一', len(calls), len({c['call_id'] for c in calls}))
    check(checks, 'capture_complete', 'HTML 中调用数量', sum(expected['executions'].values()),
          sum(c['n_calls'] for c in observation['payload']['cases']))
    normalized = [c for case in observation['payload']['cases'] for c in case['calls']]
    expected_by_execution = expected.get('answers_by_execution')
    if expected_by_execution is not None:
        actual_by_execution = {case['identity']['execution_id']:
                               sorted(c['assistant'] for c in case['calls'] if c.get('assistant'))
                               for case in observation['payload']['cases']}
        check(checks, 'attribution_exact', '每个执行只能拥有自己的答复',
              {k: sorted(v) for k, v in expected_by_execution.items()}, actual_by_execution)
    report_owners = {c['call_id']: case['identity']['execution_id']
                     for case in observation['payload']['cases'] for c in case['calls']}
    check(checks, 'attribution_exact', '查询与报告的逐调用归属一致',
          {c['call_id']: c['execution_id'] for c in calls}, report_owners)
    # Compare a multiset, not arrival order: concurrent responses may finish out of order.
    check(checks, 'content_faithful', '非空模型答复与独立上游清单一致', sorted(expected['answers']),
          sorted(c['assistant'] for c in normalized if c.get('assistant')))
    for c in calls:
        if c.get('state') == 'succeeded':
            check(checks, 'content_faithful', f"{c['call_id']} usage", [11, 7],
                  [(c.get('usage') or {}).get('prompt_tokens'), (c.get('usage') or {}).get('completion_tokens')])
    for c in normalized:
        check(checks, 'content_faithful', f"{c['call_id']} 请求模型", 'trace-fixture', c.get('model'))
        check(checks, 'content_faithful', f"{c['call_id']} system 提示词", 'SYSTEM', c.get('system'))
    # Failure is a terminal outcome with evidence, not merely an empty answer.
    # Expected errors come from the independently observed client/upstream, not SUT state.
    if expected.get('failure_contract'):
        contract = expected['failure_contract']
        failed = [c for c in calls if c.get('state') == 'failed']
        check(checks, 'state_truthful', '失败调用的已知 HTTP 状态码',
              contract['status_codes'], sorted(c.get('status_code') or 0 for c in failed))
        for c in failed:
            check(checks, 'content_faithful', '查询应保留受控错误原因', True,
                  contract['reason'] in json.dumps(c.get('error'), ensure_ascii=False))
            matching = [n for n in normalized if n['call_id'] == c['call_id']]
            check(checks, 'capture_complete', '失败调用可在报告中找到', 1, len(matching))
            if matching:
                n = matching[0]
                check(checks, 'state_truthful', '失败终止时间在报告中保留', True, bool(n.get('ts_end')))
                check(checks, 'content_faithful', '报告应保留受控错误原因', True,
                      contract['reason'] in json.dumps([n.get('error'), n.get('response_error')], ensure_ascii=False))
        # UI verdict comes from an actual browser observation, never string search in JS source.
        ui = observation.get('ui_review')
        if ui:
            check(checks, 'state_truthful', '失败调用在模型回复页明确显示为失败', True,
                  ui.get('failure_visible'))
    if expected.get('tool'):
        tools = [t for c in normalized for t in (c.get('tool_calls') or [])]
        check(checks, 'content_faithful', '工具调用 ID / 名称 / 参数',
              [('tool-1', 'lookup', {'q': 'Q1'})],
              [(t.get('id'), t.get('name'), json.loads(t.get('arguments') or '{}')) for t in tools])
        check(checks, 'content_faithful', '工具结果可见', True,
              any('TOOL_RESULT_1' in json.dumps(c.get('messages', [])) for c in normalized))
    for case in observation['payload']['cases']:
        overview = case['overview']
        check(checks, 'provenance_truthful', 'wire-only 不推断业务成功', None, overview.get('success'))
        if overview.get('turns') and any(t.get('final_text') for t in overview['turns']):
            check(checks, 'provenance_truthful', 'wire-only 最终答案标注推断', 'inferred',
                  overview.get('provenance', {}).get('final_text'))
    if expected.get('secret'):
        for surface in ('html', 'direct_html', 'gateway_log', 'pairs', 'envelope'):
            text = observation[surface] if isinstance(observation[surface], str) else json.dumps(observation[surface])
            check(checks, 'redaction_correct', f'{surface} 不含假密钥', False, FAKE_KEY in text)
        check(checks, 'redaction_correct', '普通内容保留', True, 'PUBLIC_MARKER' in observation['html'])
    scores = {m: int(all(c['passed'] for c in checks if c['metric'] == m)) for m in METRICS
              if any(c['metric'] == m for c in checks)}
    scores['all_checks_pass'] = int(all(c['passed'] for c in checks))
    return {'grade': scores, 'checks': checks, 'not_applicable': [m for m in METRICS if m not in scores]}


def expectation(pilot, case, executions, answers, **extra):
    result = {'identity': {'run_id': pilot.run_id, 'case_id': case, 'harness': 'build-eval-pilot'},
            'executions': executions, 'answers': answers,
            'states': {'succeeded': sum(executions.values()), 'failed': 0, 'pending': 0}, **extra}
    if len(executions) == 1:
        result['answers_by_execution'] = {next(iter(executions)): answers}
    return result


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    dump(out / '_state.json', {'phase': 'grader-review-pilot', 'inputs_approved': True,
         'grader_approved': False, 'approved_scenarios': [f'T{i:02}' for i in range(1, 16)],
         'pilot_scenarios': list(PILOT), 'metrics': [{'id': 'all_checks_pass', 'kind': 'binary'}],
         'perf_fields': [], 'train_ids': list(PILOT)})
    baseline = out / 'baseline'
    baseline.mkdir()
    (baseline / 'change.md').write_text('# 评分器审阅样例：5/15 场景，尚非正式基线\n')
    dump(baseline / 'summary.json', {'model': 'controlled HTTP upstream (no paid model)'})
    results = []
    with gateway(out) as (url, log, upstream):
        pilot = Pilot(out, url, log, upstream)
        def finish(case, exp, prompt, extra=None):
            started = time.monotonic()
            observation = pilot.snapshot(case, sum(exp['executions'].values()))
            graded = grade(exp, observation)
            if extra:
                extra(graded, observation)
                graded['grade']['all_checks_pass'] = int(all(c['passed'] for c in graded['checks']))
            dump(baseline / 'artifacts' / case / 'expected.json', exp)
            dump(baseline / 'artifacts' / case / 'grading.json', graded)
            client_rows = [r for r in pilot.clients if r['case_id'] == case]
            markers = {r['marker'] for r in client_rows}
            upstream_rows = [r for r in upstream.rows if r['marker'] in markers]
            dump(baseline / 'artifacts' / case / 'client.json', client_rows)
            dump(baseline / 'artifacts' / case / 'upstream.json', upstream_rows)
            exchanges = []
            for row in client_rows:
                exchanges += [
                    {'role': 'tool_call', 'name': 'HTTP client -> LiteLLM',
                     'content': json.dumps({'execution_id': row['execution_id'], 'body': row['request']}, ensure_ascii=False, indent=2)},
                    {'role': 'tool_result', 'name': 'LiteLLM -> HTTP client',
                     'content': json.dumps({'status': row['http_status'], 'body': row['response']}, ensure_ascii=False, indent=2)}]
            dump(baseline / 'traces' / f'{case}_rep0.json', [
                {'role': 'user', 'content': prompt + '\n\nExpected:\n' + json.dumps(exp, ensure_ascii=False, indent=2)},
                *exchanges,
                {'role': 'tool_result', 'name': 'llmtrace query / HTML', 'content': json.dumps(observation['envelope'], ensure_ascii=False, indent=2)},
                {'role': 'assistant', 'content': json.dumps(graded, ensure_ascii=False, indent=2)}])
            row = {'prompt_id': case, 'prompt': prompt, 'rep': 0, 'status': 'ok',
                   'tags': ['grader-review', 'local-real-proxy'], 'grade': graded['grade'],
                   'trace': f'traces/{case}_rep0.json', 'model': 'controlled HTTP upstream',
                   'query_export_wall_s': time.monotonic() - started,
                   'client_wall_s': sum(r['latency_s'] for r in client_rows),
                   'explanation': graded, 'attachments': [
                       {'kind': 'html', 'ref': f'baseline/artifacts/{case}/final/llm_trace.html', 'alt': '产品实际追踪报告'}]}
            with (baseline / 'results.jsonl').open('a') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            results.append(row)
            print(case, graded['grade'], flush=True)

        first = pilot.request('T01', 'T01-E1', 'Q1')
        messages = [{'role': 'system', 'content': 'SYSTEM'}, {'role': 'user', 'content': 'Q1'},
                    first['response']['choices'][0]['message'],
                    {'role': 'tool', 'tool_call_id': 'tool-1', 'content': 'TOOL_RESULT_1'},
                    {'role': 'user', 'content': 'Q2'}]
        pilot.request('T01', 'T01-E1', 'Q2', messages=messages)
        finish('T01', expectation(pilot, 'T01', {'T01-E1': 2}, ['ANSWER:Q2'], tool=True),
               'T01：真实 HTTP 中转的工具往返，2 次调用，usage=11/7。')

        def concurrent(i):
            for j in range(3):
                pilot.request('T04', f'T04-E{i}', f'CONCURRENT:{i}:{j}')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(concurrent, range(4)))
        finish('T04', expectation(pilot, 'T04', {f'T04-E{i}': 3 for i in range(4)},
                                  [f'ANSWER:CONCURRENT:{i}:{j}' for i in range(4) for j in range(3)],
                                  answers_by_execution={f'T04-E{i}': [f'ANSWER:CONCURRENT:{i}:{j}' for j in range(3)]
                                                        for i in range(4)}),
               'T04：4 个 execution 并发，各 3 次调用，控制上游延迟造成响应乱序。')

        pilot.request('T10', 'T10-E1', 'SUCCESS')
        pilot.request('T10', 'T10-E1', 'ERROR')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(pilot.request, 'T10', 'T10-E1', 'PENDING')
            if not upstream.pending_seen.wait(10):
                raise RuntimeError('Controlled pending request never reached upstream')
            during = pilot.cli('T10')
            dump(baseline / 'artifacts/T10/pending-query.json', during)
            upstream.release_pending.set()
            pending.result(timeout=15)
        def pending_check(graded, observation):
            check(graded['checks'], 'state_truthful', '受控上游尚未返回时必须有 pending', 1,
                  during.get('counts', {}).get('pending'))
            graded['grade']['state_truthful'] = int(all(c['passed'] for c in graded['checks'] if c['metric'] == 'state_truthful'))
        finish('T10', expectation(pilot, 'T10', {'T10-E1': 3}, ['ANSWER:SUCCESS', 'ANSWER:PENDING'],
                                   failure_contract={'status_codes': [400], 'reason': 'CONTROLLED_UPSTREAM_ERROR'},
                                   states={'succeeded': 2, 'failed': 1, 'pending': 0}),
               'T10：一次成功、一次明确 HTTP 400、一次受控 pending；同时检查中间快照和最终状态。', pending_check)

        pilot.request('T14', 'T14-E1', 'MODEL_DRAFT')
        finish('T14', expectation(pilot, 'T14', {'T14-E1': 1}, ['ANSWER:MODEL_DRAFT']),
               'T14 pilot 子例：wire-only；模型草稿标注推断，业务成功保持未知。runner 混合子例待全量。')

        pilot.request('T15', 'T15-E1', 'PUBLIC_MARKER', secret=True)
        finish('T15', expectation(pilot, 'T15', {'T15-E1': 1}, ['ANSWER:PUBLIC_MARKER'], secret=True),
               'T15：请求头及 metadata.api_key 注入已知假密钥，保留 PUBLIC_MARKER，检查查询与 HTML。')
        dump(out / '_runtime/client-requests.json', pilot.clients)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    try:
        rows = run(out)
    except Exception as exc:
        dump(out / '_pilot-error.json', {'class': 'pilot_infrastructure', 'error': str(exc)})
        raise
    files = [Path(__file__), ROOT / 'llmtrace', ROOT / 'llm_gateway/callbacks/trace_callback.py',
             ROOT / 'eval_harness/src/eval_harness/query.py', ROOT / 'eval_harness/src/eval_harness/llm_trace_html.py']
    dump(out / '_manifest.json', {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
         'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
         'dependencies': {p: importlib.metadata.version(p) for p in ('litellm', 'httpx', 'uvicorn')},
         'passed': sum(r['grade']['all_checks_pass'] for r in rows), 'scored': len(rows),
         'scope': 'local controlled upstream; grader-review pilot, not full baseline'})


if __name__ == '__main__':
    main()
