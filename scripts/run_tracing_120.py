#!/usr/bin/env python3
"""Run Claude/Insurance/Pi and adapter-free OpenCode on the shared 0922 dataset.

Default is a local configuration check (no model calls). Full runs first pass a
three-case smoke gate. The existing suite owns preflight, traces and reports.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from urllib.parse import urlsplit
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "eval_harness"
sys.path[:0] = [str(HARNESS / "src"), str(ROOT / "llm_gateway")]
import gatewayctl as ctl
from eval_harness.run import _load_mapping, _merge_profile

BUNDLE = HARNESS / "bundles/120_prompt_only_0922_shared.jsonl"
PI_BUNDLE = HARNESS / "bundles/120_prompt_only_0922_shared_pi.jsonl"
SMOKE = ["A01", "C20", "F13"]
SUITE_AGENTS = ["claude", "insurance", "pi"]
AGENTS = [*SUITE_AGENTS, "opencode"]
OPENCODE_GATEWAY = "opencode_4003"
QA_ROOT = Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent-eval")
QA_PYTHON = Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent/.feval_venv/bin/python")


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check_bundles():
    base, pi = rows(BUNDLE), rows(PI_BUNDLE)
    ids = [r["case_id"] for r in base]
    expected = [f"{letter}{i:02}" for letter in "ABCDEF" for i in range(1, 21)]
    if sorted(ids) != expected:
        raise ValueError("Shared bundle must contain exactly A01–F20 (120 unique cases)")
    canonical = rows(HARNESS / "bundles/120_prompt_only_0922.jsonl")
    questions = {r["case_id"]: r["turns"] for r in base}
    if questions != {r["case_id"]: r["turns"] for r in canonical}:
        raise ValueError("Shared questions differ from authoritative 0922 bundle")
    if len(pi) != 120 or {r["case_id"]: r["turns"] for r in pi} != {"PI_" + k: v for k, v in questions.items()}:
        raise ValueError("Pi bundle must differ only by its case ID prefix in question mapping")
    return ids


def configuration(concurrency):
    active = ROOT / "llm_gateway/run/active_env_file"
    env_path = Path(active.read_text().strip()) if active.exists() else ROOT / "llm_gateway/.env"
    if env_path.parent.resolve() != (ROOT / "llm_gateway").resolve():
        raise ValueError("Active relay profile is outside this checkout")
    primary = ctl.load("default" if env_path.name == ".env" else env_path.name.removeprefix(".env."))
    lanes = ctl.select(["insurance"])
    lanes = [g for g in lanes if g.is_lane]
    if not lanes or not any(g.name == "insurance_4002" for g in lanes):
        raise ValueError("Existing suite requires insurance_4002 / :18063 plus any extra insurance lanes")
    configs = {}
    for name, filename in (("claude", "claude_0922.yaml"), ("pi", "pi_0922_shared.yaml")):
        p = HARNESS / "configs/experiments" / filename
        cfg = _merge_profile(_load_mapping(p), p.parent)
        cfg.pop("profile_path", None)
        cfg.pop("profile", None)
        cfg["bundle_path"] = str(PI_BUNDLE if name == "pi" else BUNDLE)
        for key in ("dataset_path", "gold_dataset_path"):
            if cfg.get(key):
                cfg[key] = str((p.parent / cfg[key]).resolve())
        cfg["concurrency"] = concurrency
        isolation_key = "claude_config_dir" if name == "claude" else "pi_agent_dir"
        cfg[isolation_key] = "per_case"
        cfg["_adapter_options"][isolation_key] = "per_case"
        if name == "pi":
            cfg["thinking"] = "off"
            # Keep Pi's registered client model alias. The relay's wildcard route
            # maps it to UPSTREAM_MODEL; inventing an unregistered ID breaks Pi.
        configs[name] = cfg
    return primary, lanes, configs


def local_checks(primary, lanes, configs, agents=AGENTS):
    errors = []
    for name, cfg in configs.items():
        if not shutil.which(cfg["harness_bin"]):
            errors.append(f"Missing CLI: {cfg['harness_bin']}")
        for filename in (cfg["agent_md"], cfg["mcp_config"]):
            p = Path(cfg["project_cwd"]) / filename
            if not p.is_file():
                errors.append(f"Missing {p}")
    for p in (QA_PYTHON, QA_ROOT / "app/main.py", ROOT / "llm_gateway/.venv/bin/litellm"):
        if not p.is_file():
            errors.append(f"Missing {p}")
    from eval_harness.adapters.pi import _load_jsonc, source_pi_agent_dir
    models = _load_jsonc(source_pi_agent_dir() / "models.json")
    provider = models.get("providers", {}).get("local-relay", {})
    if urlsplit(provider.get("baseUrl", "")).port != int(primary.port):
        errors.append("Pi local-relay endpoint differs from the selected primary gateway")
    settings = Path(configs["claude"]["project_cwd"]) / ".claude/settings.local.json"
    claude_env = json.loads(settings.read_text()).get("env", {}) if settings.exists() else {}
    if urlsplit(claude_env.get("ANTHROPIC_BASE_URL", "")).port != int(primary.port):
        errors.append("Claude ANTHROPIC_BASE_URL differs from the selected primary gateway")
    if claude_env.get("ANTHROPIC_AUTH_TOKEN") != primary.env.get("LITELLM_MASTER_KEY"):
        errors.append("Claude local auth token differs from primary relay master key; align local settings first")
    if configs["pi"]["model"] not in [m.get("id") for m in provider.get("models", [])]:
        errors.append(f"Pi local-relay models.json does not contain {configs['pi']['model']}")
    for g in (primary, *lanes):
        if g.host not in ("localhost", "127.0.0.1"):
            errors.append(f"{g.name}: expected a local gateway")
        if g.is_lane:
            url = urlsplit(g.target_url)
            if url.hostname not in ("127.0.0.1", "localhost") or not url.port:
                errors.append(f"{g.name}: expected a local LANE_TARGET_URL with explicit port")
        errs, _ = ctl.check([g])
        errors.extend(errs)
    opencode = ctl.load(OPENCODE_GATEWAY)
    errors.extend(ctl.check([opencode])[0])
    for key in ('OPENCODE_BINARY', 'OPENCODE_MCP_CONFIG', 'OPENCODE_AGENT_PROMPT', 'OPENCODE_BUNDLE'):
        if not Path(opencode.env.get(key, '')).expanduser().is_file():
            errors.append(f'OpenCode missing {key} file')
    configured_bundle = Path(opencode.env.get('OPENCODE_BUNDLE', '')).expanduser()
    if configured_bundle.is_file() and rows(configured_bundle) != rows(BUNDLE):
        errors.append('OpenCode must use the same shared 0922 cases, including all turns')
    print(f"Dataset: 120 cases × {len(agents)} harnesses = {120 * len(agents)} case executions; smoke: {','.join(SMOKE)}")
    print(f"Claude/Pi/OpenCode concurrency: {configs['claude']['concurrency']} each; Insurance: {len(lanes)} lanes")
    print(f"Relay: {primary.name} :{primary.port}, model={primary.env['UPSTREAM_MODEL']}")
    print(f"OpenCode: native CLI, no adapter, dedicated relay :{opencode.port}")
    print("Selected harnesses: " + ' → '.join(agents) + "; only selected services will start")
    print("Thinking off at relay; Claude/Pi user config isolated per case")
    if errors:
        raise ValueError("Local check failed:\n- " + "\n- ".join(errors))


def health(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def start_services(primary, lanes, root, env, restart, children, agents=AGENTS):
    insurance_lanes = lanes if 'insurance' in agents else []
    for g in insurance_lanes:
        busy = ctl.lane_busy(g)
        if busy:
            raise RuntimeError(f"Lane {g.name} is busy: {busy}")
    if any(a in agents for a in ('claude', 'pi')):
        cmd = ["bash", str(ROOT / "llm_gateway/start_litellm.sh")]
        if primary.name != "default":
            cmd += ["--profile", primary.name]
        if restart:
            cmd += ["--restart"]
        subprocess.run(cmd, env=env, check=True)
    if 'opencode' in agents:
        subprocess.run([sys.executable, str(ROOT / 'llmtrace'), 'gateway',
                        'restart' if restart else 'up', OPENCODE_GATEWAY], env=env, check=True)
    for g in insurance_lanes:
        subprocess.run([sys.executable, str(ROOT / "llmtrace"), "gateway",
                        "restart" if restart else "up", g.name], env=env, check=True)
        port = urlsplit(g.target_url).port
        base = f"http://127.0.0.1:{g.port}/v1"
        if health(port):
            meta_path = Path.home() / f".insurance-qa-agent-real/server-{port}.meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            if meta.get("model_base_url_override") != base or meta.get("root") != str(QA_ROOT):
                raise RuntimeError(f"Healthy QA :{port} has no matching instance metadata; verify its root/relay before reusing")
            print(f"Reuse verified Insurance instance :{port} → :{g.port}")
            continue
        qa_env = {**env, "ENV_TYPE": "dev", "INSURANCE_QA_ROOT": str(QA_ROOT),
                  "INSURANCE_QA_PORT": str(port), "INSURANCE_QA_MODEL_BASE_URL": base,
                  "INSURANCE_UPSTREAM_ALIGN": "0"}  # Never auto-edit the external checkout.
        with (root / f"qa-{port}.log").open("ab") as log:
            proc = subprocess.Popen([str(QA_PYTHON), str(ROOT / "scripts/start_insurance_qa_local.py")],
                                    env=qa_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        children.append(proc)
        for _ in range(90):
            if proc.poll() is not None:
                break
            if health(port):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"QA :{port} startup timed out; see qa-{port}.log")
        if not health(port):
            raise RuntimeError(f"QA :{port} failed; see qa-{port}.log")


def selected_agents(root):
    return json.loads((root / 'launch.json').read_text())['harnesses']


def stage_directory(root, stage):
    agents = selected_agents(root)
    kind = 'triple_datasetA' if 'pi' in agents or agents == ['opencode'] else 'dual_datasetA'
    return root / f'{kind}_{stage}'


def write_index(root, phase):
    links = []
    for stage in ("smoke", "full"):
        run = stage_directory(root, stage)
        status = "尚未开始"
        if (run / "manifest.json").exists():
            try:
                status = json.loads((run / "manifest.json").read_text()).get("status", "unknown")
            except ValueError:
                status = "写入中"
        if (run / 'four-harness-status.json').exists():
            status = json.loads((run / 'four-harness-status.json').read_text())['status']
        items = []
        for name in selected_agents(root):
            p = run / name / "llm_trace.html"
            items.append(f'<li><a href="{p.relative_to(root).as_posix()}">{name} · 完整 Trace 报告</a></li>' if p.exists()
                         else f"<li>{name} · 报告尚未生成</li>")
        links.append(f"<section><h2>{stage} · {html.escape(status)}</h2><ul>{''.join(items)}</ul></section>")
    page = ('<!doctype html><html lang="zh"><meta charset="utf-8"><meta http-equiv="refresh" content="15">'
            '<title>120 题 · 四 Harness Trace</title><style>body{font:17px system-ui;max-width:900px;'
            'margin:50px auto;padding:24px;background:#f5f7fb;color:#182534}section{background:white;'
            'padding:16px 24px;margin:20px 0;border-radius:12px}li{margin:12px}a{color:#235ac3}</style>'
            f'<h1>120 题 · Claude / Insurance / Pi / OpenCode</h1><p>{html.escape(phase)}</p>'
            '<p>每 15 秒刷新。各 harness 收尾后可进入报告，查看题目、业务回复、模型输入输出、工具调用与归属。</p>'
            + ''.join(links) + '<p>运行完成不代表业务答案正确；失败和工具异常请在详情中查看。</p></html>')
    tmp = root / "index.tmp.html"
    tmp.write_text(page)
    tmp.replace(root / "index.html")


def monitored(cmd, root, stage, env, log_path=None):
    log = log_path.open('a') if log_path else None
    proc = subprocess.Popen(cmd, cwd=HARNESS, env=env, start_new_session=True,
                            stdout=log, stderr=subprocess.STDOUT if log else None)
    try:
        while proc.poll() is None:
            write_index(root, f"正在运行 {stage}；日志见终端或本批目录")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return proc.returncode
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        if log:
            log.close()


def opencode_capture_errors(statuses, calls, case_ids):
    """Validate each latest CLI attempt has its own completed gateway records."""
    latest = {}
    for row in sorted(statuses, key=lambda r: r.get('started_at', '')):
        if row.get('case_id') in case_ids:
            latest[row['case_id']] = row
    errors = []
    for cid in case_ids:
        status = latest.get(cid, {})
        own = [c for c in calls if c.get('execution_id') == status.get('execution_id')]
        if not status.get('success') or not own or any(c.get('state') != 'succeeded' or c.get('case_id') != cid for c in own):
            errors.append(f'{cid}: CLI failed, missing capture, wrong attribution or incomplete/failed model call')
    return errors


def opencode_report_errors(payload, expected_turns):
    """Keep the previous UI contract: every case, all turns, text first-frame."""
    latest = {c.get('identity', {}).get('case_id'): c for c in payload.get('cases', []) if c.get('latest', True)}
    errors = []
    for cid, count in expected_turns.items():
        case = latest.get(cid, {})
        turns = case.get('overview', {}).get('turns', [])
        if len(turns) != count:
            errors.append(f'{cid}: report has {len(turns)} turns, expected {count}')
        textual = [c for c in case.get('calls', []) if c.get('assistant') or c.get('thinking')]
        if not any(isinstance(c.get('first_frame_ms'), (int, float)) and c['first_frame_ms'] >= 0
                   and c.get('first_frame_kind') in ('text', 'thinking') for c in textual):
            errors.append(f'{cid}: missing text/thinking first-frame measurement')
    return errors


def run_opencode(root, stage, cases, env):
    plan = json.loads((root / 'launch.json').read_text())
    run_id = plan['opencode_run_prefix'] + '_' + stage
    out = stage_directory(root, stage) / 'opencode'
    out.mkdir(parents=True, exist_ok=True)
    gateway = ctl.load(OPENCODE_GATEWAY)
    cmd = [sys.executable, str(ROOT / 'scripts/run_opencode_wire.py'), '--relay-env', str(gateway.env_file),
           '--run-id', run_id, '--out-dir', str(out), '--cases', ','.join(cases),
           '--concurrency', str(plan['concurrency'])]
    previous = out / 'validation.json'
    if previous.exists():
        retry = set(json.loads(previous.read_text()).get('retry_case_ids', [])) & set(cases)
        if retry:
            cmd += ['--rerun-cases', ','.join(sorted(retry))]
    print(f'[opencode] {len(cases)} cases; log: {out / "console.log"}', flush=True)
    cli_rc = monitored(cmd, root, stage + ' / OpenCode', env, out / 'console.log')
    query = subprocess.run([sys.executable, str(ROOT / 'llmtrace'), 'query', 'calls',
                            '--gateway', OPENCODE_GATEWAY, '--run-id', run_id,
                            '--include-rotated', '--settle', '30', '--format', 'json'],
                           env=env, capture_output=True, text=True)
    (out / 'capture-query.json').write_text(query.stdout)
    data = json.loads(query.stdout)
    # Reuse the public query/export path. Keep every attempt in HTML for history.
    report_rc = monitored([sys.executable, str(ROOT / 'llmtrace'), 'query', 'calls',
                           '--gateway', OPENCODE_GATEWAY, '--run-id', run_id, '--include-rotated',
                           '--html', str(out), '--format', 'pairs'], root, stage, env, out / 'export.log')
    statuses = [json.loads(p.read_text()) for p in (out / 'cli').glob('*/*/status.json')]
    errors = opencode_capture_errors(statuses, data.get('calls', []), cases)
    if report_rc or not (out / 'llm_trace.html').is_file():
        errors.append('HTML export failed')
    else:
        doc = (out / 'llm_trace.html').read_text()
        match = re.search(r'<script id="DATA" type="application/json">(.*?)</script>', doc, re.S)
        if not match:
            errors.append('HTML report has no DATA payload')
        else:
            expected = {c['case_id']: len(c['turns']) for c in rows(BUNDLE) if c['case_id'] in cases}
            errors.extend(opencode_report_errors(json.loads(match.group(1)), expected))
    result = {'run_id': run_id, 'n_cases': len(cases), 'cli_exit_code': cli_rc,
              'retry_case_ids': [cid for cid in cases if any(e.startswith(cid + ':') for e in errors)],
              'capture_errors': errors, 'status': 'success' if cli_rc == 0 and not errors else 'failed'}
    (out / 'validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'success' else 2


def run_stage(root, stage, cases, env, resume):
    agents = selected_agents(root)
    suite_agents = [a for a in SUITE_AGENTS if a in agents]
    run = stage_directory(root, stage)
    run.mkdir(parents=True, exist_ok=True)
    summary = run / 'four-harness-status.json'
    summary.write_text(json.dumps({'status': 'running', 'harnesses': agents}))
    cmd = [sys.executable, "-m", "eval_harness.suite", "--agents", ",".join(suite_agents),
           "--bundle", str(BUNDLE), "--cases", ",".join(cases), "--stamp", stage,
           "--eval-runs-dir", str(root), "--claude-config", str(root / "claude.json"),
           "--pi-config", str(root / "pi.json"), "--itools-sync", "check"]
    if resume:
        cmd.append("--resume")
    print(f"[{stage}] {len(cases)} cases per harness; console log in triple_datasetA_{stage}/console.log", flush=True)
    suite_rc = monitored(cmd, root, stage, env) if suite_agents else 0
    # A failed preflight must not be followed by model calls via another harness.
    opencode_rc = run_opencode(root, stage, cases, env) if suite_rc == 0 and 'opencode' in agents else None
    rc = suite_rc or opencode_rc or 0
    summary.write_text(json.dumps({'status': 'success' if rc == 0 else 'failed', 'harnesses': agents,
                                  'suite_exit_code': suite_rc, 'opencode_exit_code': opencode_rc}))
    return rc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="local checks only; default")
    mode.add_argument("--smoke", action="store_true", help="A01,C20,F13 × four harnesses")
    mode.add_argument("--all", action="store_true", help="smoke gate, then 120 × four harnesses")
    parser.add_argument("--concurrency", type=int, default=4, help="Claude/Pi/OpenCode case workers, default 4")
    parser.add_argument("--harness", choices=AGENTS, help="run only this harness and start only its services")
    parser.add_argument("--out", type=Path, help="new batch directory")
    parser.add_argument("--resume", type=Path, help="resume an existing batch, preserving its mode/config")
    parser.add_argument("--restart-gateways", action="store_true", help="restart selected gateways to load current callback/transport; use only when idle")
    parser.add_argument("--no-open", action="store_true", help="do not open the local frontend in the default browser")
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.resume and (args.out or args.smoke or args.all or args.check or args.harness):
        parser.error("--resume uses saved mode; do not combine with --out/--smoke/--all/--check")
    ids = check_bundles()
    saved = None
    if args.resume:
        saved = json.loads((args.resume / "launch.json").read_text())
        if saved.get('harnesses') != AGENTS and saved.get('harnesses') not in [[a] for a in AGENTS]:
            raise ValueError('This batch used a different harness set; start a new four-harness batch')
        args.concurrency = saved["concurrency"]
    primary, lanes, configs = configuration(args.concurrency)
    agents = saved['harnesses'] if saved else ([args.harness] if args.harness else AGENTS)
    local_checks(primary, lanes, configs, agents)
    if not (args.smoke or args.all or args.resume):
        print("Local check passed. No services started and no model requests sent.\nRun --smoke or --all to proceed.")
        return 0
    root = (args.resume or args.out or ROOT / "eval_runs" / time.strftime("tracing120_%Y%m%d_%H%M%S")).resolve()
    plan = {"mode": saved["mode"] if saved else ("all" if args.all else "smoke"),
            'harnesses': agents, 'opencode_gateway': OPENCODE_GATEWAY,
            'opencode_run_prefix': saved['opencode_run_prefix'] if saved else 'opencode_' + uuid.uuid4().hex,
            "concurrency": args.concurrency, "primary": primary.name, "lanes": [g.name for g in lanes],
            "bundle_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (BUNDLE, PI_BUNDLE)}}
    if saved and plan != saved:
        raise ValueError("Dataset/gateway selection changed since this batch; start a new batch")
    root.mkdir(parents=True, exist_ok=bool(args.resume))
    with (root / ".launch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name, cfg in configs.items():
            p = root / f"{name}.json"
            if saved:
                if json.loads(p.read_text()) != cfg:
                    raise ValueError(f"{name} config changed; start a new batch")
            else:
                p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        (root / "launch.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        env = {**os.environ, "PYTHONPATH": str(HARNESS / "src"), "INSURANCE_LANES": ",".join(plan["lanes"]),
               "DISABLE_AIOHTTP_TRANSPORT": "True", "LITELLM_MASTER_KEY": primary.env["LITELLM_MASTER_KEY"],
               "EVAL_IDLE_TIMEOUT_SEC": "600"}
        # A developer shell may carry real provider credentials/model overrides.
        # Agents must use the workspace's local relay configuration instead.
        for key in list(env):
            if key.startswith("ANTHROPIC_") or key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "UPSTREAM_API_KEY"):
                env.pop(key)
        env.pop("EVAL_PREFLIGHT_RESULT", None)
        write_index(root, "准备依赖服务")
        print(f"Frontend: {root / 'index.html'}\nResume: bash scripts/run_tracing_120.sh --resume {root}", flush=True)
        if not args.no_open:
            webbrowser.open((root / "index.html").as_uri())
        children = []
        try:
            start_services(primary, lanes, root, env, args.restart_gateways, children, agents)
            rc = run_stage(root, "smoke", SMOKE, env, bool(saved))
            if rc == 0 and plan["mode"] == "all":
                rc = run_stage(root, "full", ids, env, bool(saved))
            write_index(root, "运行完成" if rc == 0 else f"运行失败（退出码 {rc}），保留详情供排查；全量仅在 smoke 成功后执行")
            return rc
        except BaseException:
            write_index(root, "运行中断或依赖失败；查看终端和日志，可使用 --resume 续跑")
            raise
        finally:
            # Only stop QA instances created by this invocation. Gateways remain up.
            for proc in children:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
