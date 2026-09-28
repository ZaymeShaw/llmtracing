"""Exercise callback -> export -> HTML without adapters or CaseRunResult."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'llm_gateway'))
sys.path.insert(0, str(ROOT / 'scripts'))
from callbacks import trace_callback
from audit_tracing_sources import protocol_calls
from eval_harness.llm_gateway_ingest import load_gateway_pairs
from eval_harness.llm_trace_html import build_payload, materialize_wire_run, _normalize_call
from eval_harness.trace_identity import execution_context, current_identity, trace_headers, archive_case, write_identity, writer_lock
from eval_harness.trace_store import export_executions
from eval_harness.adapters.pi import prepare_trace_agent_dir
from eval_harness.relay_inject import anthropic_cli_env, openai_compatible_kwargs
from eval_harness.adapters.base import CaseRunResult, TurnResult
from eval_harness.normalize_trace import build_trace, extract_tool_rows


@pytest.mark.parametrize('protocol', ['chat_completions', 'anthropic_messages', 'responses'])
def test_native_protocol_without_runner(protocol, tmp_path, monkeypatch):
    log = tmp_path / 'gateway.jsonl'
    monkeypatch.setattr(trace_callback, 'LOG_FILE', log)
    callback = trace_callback.EvalTraceLogger()
    calls = protocol_calls(protocol)
    path = {'chat_completions': '/v1/chat/completions', 'anthropic_messages': '/v1/messages', 'responses': '/v1/responses'}[protocol]
    for i, fixture in enumerate(calls):
        req = fixture['request']
        body = {**req.get('optional_params', {}), **{k: v for k, v in req.items() if k != 'optional_params'},
                'api_key': 'DO-NOT-LOG-BODY-KEY'}
        with execution_context(run_id='batch', harness='sdk', case_id='A01', execution_id='try-1'):
            headers = {**trace_headers(), 'X-Eval-Case-Id': 'A01', 'Authorization': 'DO-NOT-LOG-HEADER'}
        kwargs = {'litellm_call_id': str(i), 'optional_params': {}, 'litellm_params': {
            'proxy_server_request': {'headers': headers, 'body': body, 'url': path}}}
        callback.log_pre_api_call('model', [], kwargs)
        response = {**fixture['response']['response'], 'usage': fixture['response']['usage']}
        asyncio.run(callback.async_log_success_event(kwargs, response, None, None))
        # Also support previously captured native thin-proxy responses, not only callbacks.
        native = _normalize_call({**fixture, 'request': body, 'response': response})
        assert native['usage']['prompt_tokens'] == 11
        assert native['assistant'] == ('MODEL_FINAL' if i else '')
    materialize_wire_run(gateway_log=log, out_run_dir=tmp_path / 'report')
    case, = build_payload(tmp_path / 'report')['cases']
    assert case['identity']['run_id'] == 'batch'
    assert case['n_calls'] == 2
    first, last = case['calls']
    assert first['tool_calls'][0]['id'] == 'tool-1'
    assert first['tool_calls'][0]['name'] == 'lookup'
    assert last['system'] == 'SYSTEM'
    assert 'TOOL_RESULT' in json.dumps(last['messages'])
    assert last['assistant'] == 'MODEL_FINAL'
    assert last['usage']['prompt_tokens'] == 11
    assert last['usage']['completion_tokens'] == 7
    assert last['stop_reason'] or last['response_status'] == 'completed'
    assert case['overview']['success'] is None
    assert case['overview']['metrics']['wall_ms'] is None
    assert case['overview']['metrics']['num_turns'] is None
    assert case['overview']['metrics']['first_frame_ms'] is None
    assert 'DO-NOT-LOG-HEADER' not in log.read_text()
    assert 'DO-NOT-LOG-BODY-KEY' not in log.read_text()
    assert not list((tmp_path / 'report').rglob('trace.json'))
    assert (tmp_path / 'report/llm_trace.html').is_file()


def test_concurrent_batches_and_retries(tmp_path, monkeypatch):
    log = tmp_path / 'gateway.jsonl'
    monkeypatch.setattr(trace_callback, 'LOG_FILE', log)
    identities = [
        dict(run_id='morning', harness='sdk', case_id='A01', execution_id='old', started_at='2026-09-28T08:00:00Z'),
        dict(run_id='morning', harness='sdk', case_id='A01', execution_id='retry', started_at='2026-09-28T17:00:00+08:00'),
        dict(run_id='afternoon', harness='sdk', case_id='A01', execution_id='new-batch', started_at='2026-09-28T14:00:00Z'),
        dict(run_id='morning', harness='other', case_id='A01', execution_id='other-harness', started_at='2026-09-28T08:00:00Z'),
    ]
    barrier = threading.Barrier(len(identities))
    def worker(identity):
        with execution_context(**identity):
            barrier.wait(timeout=5)
            assert current_identity() == identity
            sdk = openai_compatible_kwargs('A01')['extra_headers']
            assert sdk['X-Eval-Execution-Id'] == identity['execution_id']
            assert identity['run_id'] in anthropic_cli_env('A01', {}, execution_id=identity['execution_id'])['ANTHROPIC_CUSTOM_HEADERS']
            callback = trace_callback.EvalTraceLogger()
            pending = []
            for i in range(3):
                kwargs = {'litellm_call_id': f"{identity['execution_id']}-{i}", 'optional_params': {},
                          'litellm_params': {'proxy_server_request': {'headers': sdk, 'body': {'messages': [{'role': 'user', 'content': identity['execution_id']}]}}}}
                callback.log_pre_api_call('model', [], kwargs)
                pending.append(kwargs)
            for kwargs in reversed(pending):
                asyncio.run(callback.async_log_success_event(kwargs, {'choices': [{'message': {'content': identity['execution_id']}}]}, None, None))
        assert not current_identity()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, identities))
    # Late old events cannot become the latest attempt (or overwrite another batch).
    materialize_wire_run(gateway_log=log, out_run_dir=tmp_path / 'report')
    cases = build_payload(tmp_path / 'report')['cases']
    by_id = {c['identity']['execution_id']: c for c in cases}
    assert len(cases) == 4 and all(c['n_calls'] == 3 for c in cases)
    assert not by_id['old']['latest'] and by_id['retry']['latest']
    assert by_id['new-batch']['latest'] and by_id['other-harness']['latest']
    for eid, case in by_id.items():
        assert {c['assistant'] for c in case['calls']} == {eid}
    # Repeat export is idempotent; request-only snapshots cannot erase terminal data.
    pairs = load_gateway_pairs(log)
    export_executions([{k: v for k, v in p.items() if k not in ('response', 'status_code', 'latency_ms')} for p in pairs], tmp_path / 'report')
    assert sum(c['n_calls'] for c in build_payload(tmp_path / 'report')['cases']) == 12
    assert all(c['assistant'] for case in build_payload(tmp_path / 'report')['cases'] for c in case['calls'])
    materialize_wire_run(gateway_log=log, out_run_dir=tmp_path / 'selected', run_id='afternoon')
    selected, = build_payload(tmp_path / 'selected')['cases']
    assert selected['identity']['execution_id'] == 'new-batch'


def test_legacy_conflicts_empty_export_and_archive(tmp_path):
    pairs = [dict(case_id='A01', call_id=str(i), ts=f'2026-09-28T0{i}:00:00Z', request={'messages': []}) for i in range(2)]
    export_executions(pairs, tmp_path / 'legacy')
    cases = build_payload(tmp_path / 'legacy')['cases']
    assert len(cases) == 2 and all(c['latest'] for c in cases)
    assert all(c['identity']['run_id'] is None for c in cases)
    with pytest.raises(ValueError, match='Conflicting'):
        export_executions([{**p, 'execution_id': 'same', 'run_id': str(i)} for i, p in enumerate(pairs)], tmp_path / 'conflict')
    log = tmp_path / 'empty.jsonl'; log.touch()
    materialize_wire_run(gateway_log=log, out_run_dir=tmp_path / 'empty')
    assert build_payload(tmp_path / 'empty')['cases'] == []
    case = tmp_path / 'run/cases/A01'
    for i in range(3):
        archive_case(case)
        write_identity(case, dict(case_id='A01', execution_id=str(i), run_id='run', harness='sdk', started_at=f'2026-09-28T0{i}:00:00Z'))
        (case / 'meta.json').write_text(json.dumps({'case_id': 'A01', 'success': False, 'error': 'timeout', 'attribution_status': 'explicit', 'answer': f'business-{i}'}))
    assert len(list((case / 'attempts').glob('*/identity.json'))) == 2
    cases = build_payload(tmp_path / 'run')['cases']
    assert len(cases) == 3
    assert all(c['overview']['runner_only'] for c in cases)
    assert {c['overview']['turns'][0]['final_text'] for c in cases} == {'business-0', 'business-1', 'business-2'}
    assert [c['identity']['execution_id'] for c in cases if c['latest']] == ['2']
    with writer_lock(tmp_path / 'locked'):
        with pytest.raises(RuntimeError, match='Another writer'):
            with writer_lock(tmp_path / 'locked'):
                pass


def test_pi_provider_headers_are_per_execution(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    models = {'providers': {'local': {'apiKey': 'LOCAL_KEY', 'baseUrl': 'http://127.0.0.1:4001/v1'}, 'unrelated': {'apiKey': 'do-not-copy'}}}
    (source / 'models.json').write_text(json.dumps(models))
    (source / 'settings.json').write_text('{}')
    for execution in ('one', 'two'):
        cwd = tmp_path / execution; cwd.mkdir()
        with execution_context(run_id='batch', harness='pi', case_id='A01', execution_id=execution):
            target = prepare_trace_agent_dir(cwd, 'local', source)
        copied = json.loads((target / 'models.json').read_text())
        assert list(copied['providers']) == ['local']
        assert copied['providers']['local']['headers']['X-Eval-Execution-Id'] == execution
    assert json.loads((source / 'models.json').read_text()) == models


def test_cli_stream_and_final_tool_block_count_once(tmp_path):
    events = [
        {'type': 'stream_event', 'event': {'type': 'content_block_start',
         'content_block': {'type': 'tool_use', 'id': 'tool-1', 'name': 'lookup'}}},
        {'type': 'assistant', 'message': {'content': [
         {'type': 'tool_use', 'id': 'tool-1', 'name': 'lookup', 'input': {'q': 'Q1'}}]}},
        {'type': 'user', 'message': {'content': [
         {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'result'}]}},
    ]
    result = CaseRunResult('A01', 'session', [TurnResult(1, 'Q1', 0, tmp_path / 'stream', raw_events=events)], True, 0, None, 1)
    trace = build_trace(result)
    assert trace['metrics']['num_tool_calls'] == 1
    row, = extract_tool_rows(trace['events'], 'A01')
    assert row['input_preview'] and row['output_preview'] == 'result'
