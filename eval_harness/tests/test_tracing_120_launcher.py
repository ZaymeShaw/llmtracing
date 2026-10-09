"""Launcher gates: never spend a full run after smoke failure or overwrite history."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("tracing120", ROOT / "scripts/run_tracing_120.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def isolated(monkeypatch):
    calls = []
    primary = SimpleNamespace(name="test", env={"LITELLM_MASTER_KEY": "fake"})
    monkeypatch.setattr(launcher, "configuration", lambda c: (primary, [], {"claude": {"concurrency": c}, "pi": {"concurrency": c}}))
    monkeypatch.setattr(launcher, "local_checks", lambda *a: None)
    monkeypatch.setattr(launcher, "start_services", lambda *a: calls.append("services"))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda *a: calls.append("browser"))
    return calls


def test_check_does_not_start_services_or_open_browser(isolated):
    assert launcher.main(["--check"]) == 0
    assert isolated == []


def test_smoke_failure_blocks_full_and_can_resume(monkeypatch, tmp_path, isolated):
    stages = []
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "upstream-secret-from-shell")
    def run(root, stage, cases, env, resume):
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        stages.append((stage, len(cases), resume))
        return 2
    monkeypatch.setattr(launcher, "run_stage", run)
    out = tmp_path / "batch"
    assert launcher.main(["--all", "--out", str(out), "--no-open"]) == 2
    assert stages == [("smoke", 3, False)]
    assert "运行失败" in (out / "index.html").read_text()
    def ok(root, stage, cases, env, resume):
        stages.append((stage, len(cases), resume))
        return 0
    monkeypatch.setattr(launcher, "run_stage", ok)
    assert launcher.main(["--resume", str(out), "--no-open"]) == 0
    assert stages[-2:] == [("smoke", 3, True), ("full", 120, True)]
    with pytest.raises(FileExistsError):
        launcher.main(["--all", "--out", str(out), "--no-open"])


def test_resume_refuses_changed_configuration(monkeypatch, tmp_path, isolated):
    monkeypatch.setattr(launcher, "run_stage", lambda *a: 0)
    out = tmp_path / "batch"
    launcher.main(["--smoke", "--out", str(out), "--no-open"])
    (out / "pi.json").write_text(json.dumps({"unexpected": True}))
    isolated.clear()
    with pytest.raises(ValueError, match="config changed"):
        launcher.main(["--resume", str(out), "--no-open"])
    assert not isolated


def test_suite_check_mode_reaches_preflight(monkeypatch, tmp_path):
    from eval_harness import suite, preflight
    seen = {}
    def run(specs, **kwargs):
        seen.update(kwargs)
        return {"ok": True}
    monkeypatch.setattr(preflight, "run_preflight", run)
    monkeypatch.setenv(preflight.RESULT_ENV, "unused")
    suite.mandatory_preflight(agents=["insurance"], claude_config=None, pi_config=None,
                             out_dir=tmp_path, itools_sync_mode="check")
    assert seen["itools_sync_mode"] == "check" and seen["include_insurance"]


def test_fourth_harness_failure_blocks_full(monkeypatch, tmp_path, isolated):
    suite_commands, open_stages = [], []
    monkeypatch.setattr(launcher, 'monitored', lambda cmd, *a, **k: suite_commands.append(cmd) or 0)
    monkeypatch.setattr(launcher, 'run_opencode', lambda root, stage, cases, env: open_stages.append(stage) or 2)
    out = tmp_path / 'four'
    assert launcher.main(['--all', '--out', str(out), '--no-open']) == 2
    assert open_stages == ['smoke']
    assert suite_commands[0][suite_commands[0].index('--agents') + 1] == 'claude,insurance,pi'
    status = json.loads((out / 'triple_datasetA_smoke/four-harness-status.json').read_text())
    assert status['status'] == 'failed' and status['harnesses'] == ['claude', 'insurance', 'pi', 'opencode']
    assert 'OpenCode' in (out / 'index.html').read_text()


def test_single_harness_preflight_and_full_stay_selected(monkeypatch, tmp_path, isolated):
    commands = []
    monkeypatch.setattr(launcher, 'monitored', lambda cmd, *a, **k: commands.append(cmd) or 0)
    monkeypatch.setattr(launcher, 'run_opencode', lambda *a: pytest.fail('OpenCode was not selected'))
    out = tmp_path / 'claude-only'
    assert launcher.main(['--all', '--harness', 'claude', '--out', str(out), '--no-open']) == 0
    assert [cmd[cmd.index('--agents') + 1] for cmd in commands] == ['claude', 'claude']
    for stage in ['smoke', 'full']:
        result = json.loads((out / f'dual_datasetA_{stage}/four-harness-status.json').read_text())
        assert result['status'] == 'success' and result['harnesses'] == ['claude']
    assert launcher.main(['--resume', str(out), '--no-open']) == 0


def test_noninsurance_services_do_not_inspect_or_start_insurance(monkeypatch, tmp_path):
    commands = []
    primary = SimpleNamespace(name='test')
    lanes = [SimpleNamespace(name='insurance_busy')]
    monkeypatch.setattr(launcher.ctl, 'lane_busy', lambda *a: pytest.fail('Touched an unselected insurance lane'))
    monkeypatch.setattr(launcher, 'health', lambda *a: pytest.fail('Probed unselected QA'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda cmd, **k: commands.append(cmd))
    launcher.start_services(primary, lanes, tmp_path, {}, False, [], ['claude'])
    assert len(commands) == 1 and 'start_litellm.sh' in commands[0][1]
    commands.clear()
    launcher.start_services(primary, lanes, tmp_path, {}, False, [], ['opencode'])
    assert len(commands) == 1 and commands[0][-1] == launcher.OPENCODE_GATEWAY


def test_insurance_busy_blocks_before_service_mutation(monkeypatch, tmp_path):
    primary = SimpleNamespace(name='test')
    lanes = [SimpleNamespace(name='insurance_busy')]
    monkeypatch.setattr(launcher.ctl, 'lane_busy', lambda *a: 'another execution')
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: pytest.fail('Busy lane was modified'))
    with pytest.raises(RuntimeError, match='busy'):
        launcher.start_services(primary, lanes, tmp_path, {}, False, [], ['insurance'])


def test_opencode_only_does_not_run_suite(monkeypatch, tmp_path, isolated):
    monkeypatch.setattr(launcher, 'monitored', lambda *a, **k: pytest.fail('Suite was not selected'))
    seen = []
    monkeypatch.setattr(launcher, 'run_opencode', lambda root, stage, cases, env: seen.append(stage) or 0)
    assert launcher.main(['--all', '--harness', 'opencode', '--out', str(tmp_path / 'open'), '--no-open']) == 0
    assert seen == ['smoke', 'full']


def test_capture_latest_attempt_and_ui_multiturn_first_word():
    statuses = [{'case_id': 'E01', 'execution_id': 'old', 'success': False, 'started_at': '1'},
                {'case_id': 'E01', 'execution_id': 'new', 'success': True, 'started_at': '2'}]
    calls = [{'case_id': 'E01', 'execution_id': 'old', 'state': 'failed'},
             {'case_id': 'E01', 'execution_id': 'new', 'state': 'succeeded'}]
    assert not launcher.opencode_capture_errors(statuses, calls, ['E01'])
    calls[-1]['state'] = 'pending'
    assert launcher.opencode_capture_errors(statuses, calls, ['E01'])
    case = {'identity': {'case_id': 'E01'}, 'latest': True, 'overview': {'turns': [{}, {}, {}]},
            'calls': [{'assistant': 'answer', 'first_frame_ms': 250, 'first_frame_kind': 'text'}]}
    old = {'identity': {'case_id': 'E01'}, 'latest': False}
    assert not launcher.opencode_report_errors({'cases': [case, old]}, {'E01': 3})
    case['overview']['turns'].pop()
    case['calls'][0]['first_frame_kind'] = 'tool_use'
    assert len(launcher.opencode_report_errors({'cases': [case]}, {'E01': 3})) == 2


def test_opencode_resume_counts_only_selected_latest_success(tmp_path):
    spec = importlib.util.spec_from_file_location('opencode_wire', ROOT / 'scripts/run_opencode_wire.py')
    wire = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wire)
    for cid, attempt, success in [('A01', '1', True), ('A01', '2', False), ('E01', '1', True), ('F01', '1', True)]:
        p = tmp_path / 'cli' / cid / attempt / 'status.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'run_id': 'run', 'case_id': cid, 'started_at': attempt, 'success': success}))
    assert wire.completed_cases(tmp_path, 'run', {'A01', 'E01'}) == {'E01'}


def test_opencode_cli_multiturn_outdir_and_resume(tmp_path):
    fake = tmp_path / 'opencode'
    fake.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
cfg = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
h = cfg['provider']['relay']['options']['headers']
assert h['X-Eval-Run-Id'] == 'synthetic_run'
assert h['X-Eval-Execution-Id'] and h['X-Eval-Harness'] == 'opencode'
assert os.environ['XDG_CONFIG_HOME'].startswith(os.getcwd())
if sys.argv[-1] == 'second':
    assert '--session' in sys.argv
    assert sys.argv[sys.argv.index('--session') + 1] == 'synthetic-session'
print(json.dumps({'type': 'text', 'sessionID': 'synthetic-session', 'part': {'text': 'answer'}}))
''')
    fake.chmod(0o755)
    bundle = tmp_path / 'bundle.jsonl'
    bundle.write_text(json.dumps({'case_id': 'E01', 'turns': ['first', 'second']}) + '\n')
    prompt = tmp_path / 'agent.md'
    prompt.write_text('synthetic fixture')
    mcp = tmp_path / 'mcp.json'
    mcp.write_text(json.dumps({'mcpServers': {'insurance-tools': {'command': 'unused', 'env': {}}}}))
    env = tmp_path / '.env.fixture'
    values = {'LITELLM_HOST': '127.0.0.1', 'LITELLM_PORT': '4999', 'LITELLM_MASTER_KEY': 'synthetic',
              'LLM_GATEWAY_LOG_DIR': str(tmp_path / 'logs'), 'UPSTREAM_MODEL': 'fixture',
              'OPENCODE_BINARY': str(fake), 'OPENCODE_SANDBOX_ROOT': str(tmp_path / 'sandbox'),
              'OPENCODE_BUNDLE': str(bundle), 'OPENCODE_MCP_CONFIG': str(mcp), 'OPENCODE_AGENT_PROMPT': str(prompt)}
    env.write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
    out = tmp_path / 'batch' / 'opencode'
    cmd = [sys.executable, str(ROOT / 'scripts/run_opencode_wire.py'), '--run-id', 'synthetic_run',
           '--relay-env', str(env), '--out-dir', str(out), '--cases', 'E01']
    for _ in range(2):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, proc.stderr
    statuses = list((out / 'cli').glob('*/*/status.json'))
    assert len(statuses) == 1
    result = json.loads(statuses[0].read_text())
    assert result['success'] and len(result['turns']) == 2 and result['sandbox_removed']
    proc = subprocess.run([*cmd, '--rerun-cases', 'E01'], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    assert len(list((out / 'cli').glob('*/*/status.json'))) == 2
