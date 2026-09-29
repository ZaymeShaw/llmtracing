#!/usr/bin/env python3
"""Run OpenCode cases using only its CLI/config and gateway headers (no adapter).

Example: python3 scripts/run_opencode_wire.py --relay-env llm_gateway/.env.opencode_4003 --run-id opencode_test --cases A01,E01
Gateway credentials and MCP settings stay local; upstream credentials never enter OpenCode.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def env_file(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        name, value = line.removeprefix('export ').split('=', 1)
        parts = shlex.split(value, comments=True)
        values[name.strip()] = ' '.join(parts)
    return values


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def run_case(case, args, relay, mcp):
    started = datetime.now(timezone.utc).isoformat()
    eid = uuid.uuid4().hex
    cid = case['case_id']
    output = ROOT / 'eval_runs' / args.run_id / 'cli' / cid / eid
    sandbox = Path(args.sandbox_root) / args.run_id / cid / eid
    output.mkdir(parents=True)
    sandbox.mkdir(parents=True)
    headers = {'X-Eval-Run-Id': args.run_id, 'X-Eval-Case-Id': cid,
               'X-Eval-Execution-Id': eid, 'X-Eval-Harness': 'opencode',
               'X-Eval-Started-At': started}
    if args.minimal_headers:
        headers.pop('X-Eval-Harness')
        headers.pop('X-Eval-Started-At')
    model = relay['UPSTREAM_MODEL']
    child = {k: v for k, v in os.environ.items() if k in
             ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
    child.update(NO_PROXY='127.0.0.1,localhost,::1', no_proxy='127.0.0.1,localhost,::1',
                 OPENCODE_DISABLE_AUTOUPDATE='true', OPENCODE_DISABLE_CLAUDE_CODE='true',
                 OPENCODE_DISABLE_DEFAULT_PLUGINS='true',
                 GATEWAY_API_KEY=relay['LITELLM_MASTER_KEY'])
    for kind in ('CONFIG', 'DATA', 'STATE'):
        child[f'XDG_{kind}_HOME'] = str(sandbox / kind.lower())
    child['XDG_CACHE_HOME'] = str(Path(args.sandbox_root) / 'cache')
    mcp_environment = {}
    for key, value in mcp.get('env', {}).items():
        # Provider credentials are not passed to the MCP tool service.
        child[key] = str(value)
        mcp_environment[key] = '{env:' + key + '}'
    child['ITOOLS_AGENT_ID'] = 'opencode'
    config = {
        '$schema': 'https://opencode.ai/config.json',
        'model': 'relay/' + model, 'small_model': 'relay/' + model,
        'enabled_providers': ['relay'], 'autoupdate': False, 'share': 'disabled',
        'snapshot': False,
        'provider': {'relay': {'npm': '@ai-sdk/openai-compatible', 'name': 'Local LiteLLM',
            'options': {'baseURL': args.base_url, 'apiKey': '{env:GATEWAY_API_KEY}',
                        'headers': headers},
            'models': {model: {'name': model, 'limit': {'context': 131072, 'output': 8192}}}}},
        'mcp': {'insurance': {'type': 'local', 'command': [mcp['command'], *mcp.get('args', [])],
                             'environment': mcp_environment, 'enabled': True, 'timeout': 30000}},
        'permission': {'*': 'deny', 'insurance_*': 'allow'},
        'agent': {'insurance_eval': {'mode': 'primary', 'description': 'Insurance MCP evaluation',
            'prompt': args.agent_prompt.read_text(),
            'steps': 40}},
        'default_agent': 'insurance_eval',
    }
    child['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    write_json(output / 'opencode.config.json', config)
    status = dict(run_id=args.run_id, case_id=cid, execution_id=eid, started_at=started,
                  sandbox=str(sandbox), session_id=None, success=False, turns=[])
    write_json(output / 'status.json', status)
    session = None
    t0 = time.monotonic()
    try:
        for index, prompt in enumerate(case['turns'], 1):
            cmd = [args.binary, 'run', '--format', 'json', '--agent', 'insurance_eval']
            if session:
                cmd += ['--session', session]
            cmd += [prompt]
            with (output / f'turn{index}.jsonl').open('w') as out, (output / f'turn{index}.stderr').open('w') as err:
                p = subprocess.Popen(cmd, cwd=sandbox, env=child, stdout=out, stderr=err, start_new_session=True)
                try:
                    code = p.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGTERM)
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.wait()
                    raise TimeoutError(f'turn {index} exceeded {args.timeout}s')
            events = []
            for line in (output / f'turn{index}.jsonl').read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            sessions = {e.get('sessionID') for e in events if e.get('sessionID')}
            if len(sessions) != 1 or (session and session not in sessions):
                raise RuntimeError(f'turn {index}: missing/changed session ID')
            session = sessions.pop()
            failures = [e for e in events if e.get('type') == 'error']
            text = ''.join(e.get('part', {}).get('text', '') for e in events if e.get('type') == 'text')
            tool_events = [e for e in events if e.get('type') == 'tool_use']
            status['turns'].append(dict(index=index, exit_code=code, text=text,
                                        tool_events=len(tool_events), errors=failures))
            status['session_id'] = session
            write_json(output / 'status.json', status)
            if code or failures or not text.strip():
                raise RuntimeError(f'turn {index}: exit={code}, errors={len(failures)}, answer={bool(text.strip())}')
        status['success'] = True
    except Exception as exc:
        status['error'] = f'{type(exc).__name__}: {exc}'
    status['wall_ms'] = int((time.monotonic() - t0) * 1000)
    write_json(output / 'status.json', status)
    # CLI streams and status are saved above; sandbox state is transient and can
    # grow by gigabytes across a batch. Reclaim it after each completed attempt.
    try:
        shutil.rmtree(sandbox)
        status['sandbox_removed'] = True
        write_json(output / 'status.json', status)
    except OSError as exc:
        print(f'case {cid}: sandbox cleanup failed: {exc}', flush=True)
    print(json.dumps({k: status.get(k) for k in ('case_id', 'execution_id', 'success', 'wall_ms', 'error')}, ensure_ascii=False), flush=True)
    return status


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--cases', help='Comma-separated case IDs; default all 120')
    p.add_argument('--concurrency', type=int, default=4)
    p.add_argument('--timeout', type=int, default=600)
    p.add_argument('--minimal-headers', action='store_true', help='Send only batch, case and execution headers')
    p.add_argument('--relay-env', type=Path, required=True, help='LiteLLM profile used to start this relay')
    args = p.parse_args()
    relay_path = args.relay_env.resolve()
    if not relay_path.is_file():
        p.error(f'relay profile missing: {relay_path}')
    relay = env_file(relay_path)
    for key in ('LITELLM_HOST', 'LITELLM_PORT', 'LITELLM_MASTER_KEY',
                'LLM_GATEWAY_LOG_DIR', 'UPSTREAM_MODEL', 'OPENCODE_BINARY',
                'OPENCODE_SANDBOX_ROOT', 'OPENCODE_BUNDLE',
                'OPENCODE_MCP_CONFIG', 'OPENCODE_AGENT_PROMPT'):
        if not relay.get(key):
            p.error(f'{key} missing from {relay_path}')
    args.base_url = f'http://{relay["LITELLM_HOST"]}:{relay["LITELLM_PORT"]}/v1'
    args.binary = str(Path(relay['OPENCODE_BINARY']).expanduser())
    args.sandbox_root = str(Path(relay['OPENCODE_SANDBOX_ROOT']).expanduser())
    args.bundle = Path(relay['OPENCODE_BUNDLE']).expanduser()
    args.mcp_config = Path(relay['OPENCODE_MCP_CONFIG']).expanduser()
    args.agent_prompt = Path(relay['OPENCODE_AGENT_PROMPT']).expanduser()
    for path in (Path(args.binary), args.bundle, args.mcp_config, args.agent_prompt):
        if not path.is_file():
            p.error(f'configured file missing: {path}')
    if not args.run_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in args.run_id):
        p.error('run-id must contain only letters, digits, underscores or hyphens')
    cases = [json.loads(line) for line in args.bundle.read_text().splitlines() if line.strip()]
    if args.cases:
        wanted = set(args.cases.split(','))
        cases = [c for c in cases if c['case_id'] in wanted]
        if {c['case_id'] for c in cases} != wanted:
            p.error('unknown case IDs')
    mcp = json.loads(args.mcp_config.read_text())['mcpServers']['insurance-tools']
    print(f'relay profile={relay_path} url={args.base_url} log_dir={relay["LLM_GATEWAY_LOG_DIR"]}', flush=True)
    run_root = ROOT / 'eval_runs' / args.run_id
    completed = set()
    for path in (run_root / 'cli').glob('*/*/status.json'):
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if saved.get('run_id') == args.run_id and saved.get('success') is True:
            completed.add(saved.get('case_id'))
    pending = [case for case in cases if case['case_id'] not in completed]
    print(f'run_id={args.run_id} total={len(cases)} already_ok={len(completed)} pending={len(pending)}', flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for future in as_completed([pool.submit(run_case, case, args, relay, mcp) for case in pending]):
            results.append(future.result())
    summary = dict(run_id=args.run_id, n_cases=len(cases), n_ok=len(completed) + sum(r['success'] for r in results),
                   n_attempted=len(results), n_skipped=len(completed),
                   executions=[{k:r.get(k) for k in ('case_id','execution_id','success','error')} for r in results])
    write_json(ROOT / 'eval_runs' / args.run_id / f'batch_{uuid.uuid4().hex[:8]}.json', summary)
    print(json.dumps({key: summary[key] for key in ('run_id', 'n_cases', 'n_ok')}, ensure_ascii=False), flush=True)
    return 0 if summary['n_ok'] == summary['n_cases'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
