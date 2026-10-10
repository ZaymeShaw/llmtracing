#!/usr/bin/env python3
"""Run DSH SDK sessions through exclusive gateway lanes and export wire reports.

Uses a local DSH checkout's built JSON-RPC runtime and Python SDK. Each lane
owns one worker process group at a time; no X-Eval headers are injected in DSH.
"""
from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval_harness/src"))
from eval_harness.attribution_lane import LaneLease
from eval_harness.gateways import LaneSpec, gatewayctl, lane_not_ready
from eval_harness.query import collect, redact, snapshot_cursor
from eval_harness.tool_health import classify_tool_result_ex, result_text


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(data), ensure_ascii=False, indent=2) + "\n")


def smoke_cases():
    return [
        {"case_id": "D01", "turns": ["Reply with exactly DSH_LANE_OK. Do not use tools."],
         "expected": "DSH_LANE_OK"},
        {"case_id": "D02", "turns": [
            "Remember this test code: LANE_MEMORY_739. Reply only ACK. Do not use tools.",
            "What test code did I ask you to remember? Reply only with that code. Do not use tools."],
         "expected": "LANE_MEMORY_739"},
        {"case_id": "D03", "turns": [
            "Use the bash tool to run exactly: cat lane_fixture.txt . "
            "Then reply with the file's exact contents. Do not modify any files."],
         "expected_contains": "DSH_TOOL_RESULT_8675309", "require_tool": "bash",
         "expected_tool_output": "DSH_TOOL_RESULT_8675309"},
    ]


def load_cases(path: Path | None):
    rows = smoke_cases() if path is None else [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    seen = set()
    for row in rows:
        cid = row.get("case_id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", cid) or cid in seen:
            raise ValueError("case_id must be unique and contain only letters, digits, '_' or '-'")
        turns = row.get("turns")
        if not isinstance(turns, list) or not turns or any(not isinstance(t, str) or not t.strip() for t in turns):
            raise ValueError(f"{cid}: nonempty text turns required")
        seen.add(cid)
    if not rows:
        raise ValueError("no cases selected")
    return rows


def worker(job_path: Path) -> int:
    """Runs inside a dedicated process group, with only the gateway credential."""
    job = json.loads(job_path.read_text())
    checkout, out = Path(job["checkout"]), job_path.parent
    sys.path.insert(0, str(checkout / "python/sdk/src"))
    from deepseek_harness import DeepSeekHarness
    status = {"session_id": job["execution_id"], "success": False, "turns": []}
    try:
        runtime_env = {"DSH_HOME": str(out / "home"), "DSH_TELEMETRY_DISABLED": "1",
                       "DSH_READY_FILE": str(out / "runtime_ready.json"),
                       "DSH_BOOT_MODULE": str(checkout / "packages/boot/app-boot/lib/index.js")}
        cordis = job["cordis"]
        if job.get("mcp_config"):
            cordis, mcp_env = insurance_composition(job, out)
            runtime_env["DSH_INSURANCE_MCP_ENV"] = json.dumps(mcp_env)
            runtime_env["DSH_REQUIRE_INSURANCE_MCP"] = "1"
        with (out / "notifications.jsonl").open("w") as events:
            def emit(notification):
                events.write(json.dumps(redact(asdict(notification)), ensure_ascii=False) + "\n")
                events.flush()

            harness = DeepSeekHarness(
                provider="deepseek-official", model=job["model"], max_tokens=job["max_tokens"],
                cwd=str(out / "workspace"), runtime_cwd=str(out / "workspace"),
                session_root=str(out / "sessions"), cordis=str(cordis),
                base_url=job["base_url"], api_key=os.environ["DSH_GATEWAY_KEY"],
                launch_args_override=(job["node"], str(ROOT / "scripts/dsh_jsonrpc_ready.mjs")),
                env=runtime_env,
                request_timeout_seconds=job.get("turn_timeout", 300), shutdown_timeout_seconds=10,
            )
            try:
                harness.client.start()
                status["runtime_ready"] = wait_runtime_ready(out / "runtime_ready.json", timeout=90)
                harness.start()
                session = harness.start_session(job["execution_id"])
                for prompt in job["case"]["turns"]:
                    result = session.run(prompt, on_notification=emit)
                    status["turns"].append({"finish_reason": result.finish_reason,
                                            "final_response": result.final_response})
                    save(out / "sdk_result.json", status)
                    if result.finish_reason != "completed":
                        raise RuntimeError(f"DSH turn ended: {result.finish_reason}")
                    if not result.final_response.strip():
                        raise RuntimeError("DSH completed without a visible answer")
            finally:
                harness.close()
            status["success"] = True
    except Exception as exc:
        status["error"] = str(exc)
    save(out / "sdk_result.json", status)
    return 0 if status["success"] else 1


def wait_runtime_ready(path: Path, timeout: float):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("DSH plugin tree did not report readiness")
        time.sleep(0.05)
    ready = json.loads(path.read_text())
    if ready.get("ready") is not True:
        raise RuntimeError(ready.get("error", "DSH plugin tree failed startup"))
    return ready


def insurance_composition(job, out):
    """Mount the existing stdio insurance MCP, without local model-facing tools."""
    import yaml
    checkout = Path(job["checkout"])
    server = json.loads(Path(job["mcp_config"]).read_text())["mcpServers"]["insurance-tools"]
    env = {k: str(v) for k, v in server.get("env", {}).items()}
    env["ITOOLS_AGENT_ID"] = "dsh"
    class Expression(str):
        pass
    class Dumper(yaml.SafeDumper):
        pass
    Dumper.add_representer(Expression, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:js", value))

    def plugin(id, path, config=None):
        target = checkout / "packages" / path / "lib/index.js"
        if not target.is_file():
            raise FileNotFoundError(f"DSH plugin needs build: {target}")
        return {"id": id, "name": str(target), **({"config": config} if config is not None else {})}

    rows = [
        plugin("sdk", "sdk/server"),
        plugin("agent", "examples/agent-spine-demo", {
            "persona": Path(job["agent_prompt"]).read_text(), "workspaceContext": False,
            "includeRuntimeContext": False, "skills": {"enabled": False},
            "toolBash": False, "toolJobs": False,
        }),
        plugin("llm", "llm/llm-deepseek", {
            "thinking": "disabled" if job["thinking"] == "off" else "enabled",
            "reasoningEffort": "off" if job["thinking"] == "off" else "max",
        }),
        plugin("sessions", "session/session-persistence-jsonl", {"root": str(out / "sessions")}),
        plugin("checkpoints", "session/session-checkpoint-policy"),
        plugin("mcp-insurance", "mcp/mcp-client", {
            "serverName": "insurance-tools", "transport": "stdio", "command": server["command"],
            "args": server.get("args", []), "env": Expression("JSON.parse(process.env.DSH_INSURANCE_MCP_ENV)"),
            "cwd": server.get("cwd", str(out / "workspace")), "failOnStartupError": True,
            "toolCallTimeoutMs": 60000,
        }),
    ]
    config = out / "insurance.cordis.yml"
    config.write_text(yaml.dump(rows, Dumper=Dumper, allow_unicode=True, sort_keys=False))
    return config, env


def mcp_health(notifications):
    """Join native MCP calls/results and classify backend errors separately."""
    calls, results = {}, []
    for n in notifications:
        payload = n.get("payload", {})
        event = payload.get("event", {})
        data = event.get("data", {})
        if event.get("type") == "tool/call" and data.get("name", "").startswith("mcp__insurance-tools__"):
            calls[(payload.get("sessionId"), data.get("callId"))] = data["name"]
        if event.get("type") == "tool/result":
            for block in data.get("message", {}).get("content", []):
                name = calls.get((payload.get("sessionId"), block.get("toolCallId")))
                if name and block.get("type") == "tool-result":
                    kind, reason, upstream = classify_tool_result_ex(result_text(block.get("content")), bool(block.get("isError")))
                    results.append({"tool": name, "kind": kind, "reason": reason, "upstream": upstream})
    counts = {k: sum(r["kind"] == k for r in results) for k in ("ok", "neutral", "infra", "upstream", "error")}
    return {"call_count": len(calls), "result_count": len(results), "counts": counts,
            "tools": sorted(set(calls.values())), "results": results,
            "healthy": bool(calls) and len(calls) == len(results) and counts["ok"] > 0
                       and counts["infra"] == 0 and counts["error"] == 0 and counts["upstream"] == 0}


def terminate_group(proc) -> None:
    """Terminate the owned SDK worker and DSH runtime together on interruption."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run_lane(gateway, cases, args):
    results = []
    with LaneLease(gateway.registry_dir, gateway.name) as lease:
        for case in cases:
            eid = uuid.uuid4().hex
            out = args.output / "raw" / case["case_id"] / eid
            (out / "workspace").mkdir(parents=True)
            (out / "workspace/lane_fixture.txt").write_text("DSH_TOOL_RESULT_8675309\n")
            job = {"checkout": str(args.dsh_checkout), "node": args.node, "cordis": str(args.cordis),
                   "model": gateway.env["UPSTREAM_MODEL"], "max_tokens": args.max_tokens,
                   "turn_timeout": getattr(args, "turn_timeout", 300),
                   "base_url": f"http://127.0.0.1:{gateway.port}/v1", "case": case, "execution_id": eid}
            if getattr(args, "mcp_config", None):
                job.update(mcp_config=str(args.mcp_config), agent_prompt=str(args.agent_prompt), thinking=args.thinking)
            save(out / "job.json", job)
            cursor = snapshot_cursor(gateway=gateway.name)
            status = {"run_id": args.run_id, "case_id": case["case_id"], "execution_id": eid,
                      "gateway": gateway.name, "started_at": datetime.now(timezone.utc).isoformat(),
                      "success": False}
            save(out / "status.json", status)
            child = {k: v for k, v in os.environ.items() if k in
                     ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")}
            child.update(DSH_GATEWAY_KEY=gateway.env["LITELLM_MASTER_KEY"],
                         NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
            lease.start(run_id=args.run_id, case_id=case["case_id"], execution_id=eid)
            started, proc, blocked = time.monotonic(), None, False
            try:
                with (out / "worker.stdout.log").open("w") as stdout, (out / "worker.stderr.log").open("w") as stderr:
                    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(out / "job.json")],
                                            env=child, cwd=ROOT, stdout=stdout, stderr=stderr, start_new_session=True)
                    proc.wait(timeout=args.timeout)
                sdk_path = out / "sdk_result.json"
                sdk = json.loads(sdk_path.read_text()) if sdk_path.exists() else {"success": False, "error": "worker produced no result"}
                status.update(sdk)
                # An abnormal worker may leave descendants. Quarantine its lane.
                if proc.returncode != 0:
                    blocked = True
                    terminate_group(proc)
                captured = collect(gateway=gateway.name, execution_ids=[eid], cursor=cursor,
                                   settle_timeout=20, quiet=2, include_events=True)
                save(out / "gateway_calls.json", captured)
                status["capture_status"], status["counts"] = captured["status"], captured["counts"]
                status["success"] = bool(status["success"] and proc.returncode == 0
                                         and captured["status"] == "complete"
                                         and captured["counts"]["total"] > 0
                                         and captured["counts"]["failed"] == 0 and not captured["conflicts"])
                if captured["status"] == "partial":
                    blocked = True
                calls = captured["calls"]
                status["attribution_ok"] = bool(calls) and all(
                    c.get("case_id") == case["case_id"] and c.get("run_id") == args.run_id
                    and c.get("execution_id") == eid and c.get("case_id_source") == "lane_registry"
                    and c.get("lane_id") == gateway.name for c in calls)
                status["success"] &= status["attribution_ok"]
                final = status.get("turns", [{}])[-1].get("final_response", "") if status.get("turns") else ""
                if "expected" in case:
                    status["answer_ok"] = final.strip() == case["expected"]
                    status["success"] &= status["answer_ok"]
                if "expected_contains" in case:
                    status["answer_ok"] = case["expected_contains"] in final
                    status["success"] &= status["answer_ok"]
                if "require_tool" in case:
                    notifications = [json.loads(line) for line in (out / "notifications.jsonl").read_text().splitlines()]
                    status["tool_ok"] = tool_succeeded(notifications, case["require_tool"], case.get("expected_tool_output"))
                    status["success"] &= status["tool_ok"]
                if job.get("mcp_config"):
                    notifications = [json.loads(line) for line in (out / "notifications.jsonl").read_text().splitlines()]
                    status["mcp_health"] = mcp_health(notifications)
                    status["success"] &= status["mcp_health"]["healthy"]
            except BaseException as exc:
                blocked = True
                status["success"], status["error"] = False, str(exc) or type(exc).__name__
                if proc is not None:
                    terminate_group(proc)
                if not isinstance(exc, Exception):
                    raise
            finally:
                status["wall_seconds"] = round(time.monotonic() - started, 3)
                status["lane_blocked"] = blocked
                save(out / "status.json", status)
                lease.finish(blocked=blocked, reason="worker failed or capture incomplete" if blocked else None)
            results.append(status)
            print(f"{case['case_id']} {gateway.name}: {'PASS' if status['success'] else 'FAIL'} ({status['wall_seconds']}s)", flush=True)
            if blocked:
                break
    return results


def tool_succeeded(notifications, name, expected_output=None):
    """Require a matched successful native tool result, not model-authored text."""
    calls = set()
    for notification in notifications:
        payload = notification.get("payload", {})
        event = payload.get("event", {})
        data = event.get("data", {})
        if event.get("type") == "tool/call" and data.get("name") == name and data.get("callId"):
            calls.add((payload.get("sessionId"), data["callId"]))
        if event.get("type") == "tool/result":
            for block in data.get("message", {}).get("content", []):
                key = (payload.get("sessionId"), block.get("toolCallId"))
                if block.get("type") != "tool-result" or key not in calls or block.get("isError"):
                    continue
                text = "".join(c.get("text", "") for c in block.get("content", []) if c.get("type") == "text")
                if expected_output is None or text.strip() == expected_output:
                    return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--dsh-checkout", type=Path)
    parser.add_argument("--node", default="node")
    parser.add_argument("--cordis", type=Path)
    parser.add_argument("--mcp-config", type=Path, help="Claude/Pi mcpServers config containing insurance-tools (stdio)")
    parser.add_argument("--agent-prompt", type=Path, help="Shared business system prompt, required with --mcp-config")
    parser.add_argument("--thinking", choices=("on", "off"), default="off", help="Insurance MCP composition only")
    parser.add_argument("--lanes", default="dsh_smoke")
    parser.add_argument("--bundle", type=Path, help="JSONL of case_id and turns; default: three smoke cases")
    parser.add_argument("--cases", help="comma-separated case ids")
    parser.add_argument("--run-id", default="dsh_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--turn-timeout", type=float, default=300, help="SDK turn RPC timeout; case worker is also bounded by --timeout")
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if not args.dsh_checkout:
        parser.error("--dsh-checkout is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) or args.timeout <= 0 or args.turn_timeout <= 0 or args.max_tokens <= 0:
        parser.error("safe run-id and positive timeout/max-tokens required")
    args.dsh_checkout = args.dsh_checkout.resolve()
    if args.mcp_config:
        if not args.agent_prompt or not args.bundle or args.cordis:
            parser.error("--mcp-config needs --agent-prompt and --bundle, and generates its own --cordis")
        args.mcp_config, args.agent_prompt = args.mcp_config.resolve(), args.agent_prompt.resolve()
        for path in (args.mcp_config, args.agent_prompt):
            if not path.is_file():
                parser.error(f"missing: {path}")
    args.cordis = (args.cordis or args.dsh_checkout / "examples/jsonrpc-agent/cordis.yml").resolve()
    args.output = ROOT / "eval_runs" / args.run_id
    if args.output.exists():
        parser.error("run directory already exists; choose a fresh --run-id")
    cases = load_cases(args.bundle)
    if args.cases:
        selected = set(args.cases.split(","))
        if selected - {c["case_id"] for c in cases}:
            parser.error("unknown case id")
        cases = [c for c in cases if c["case_id"] in selected]
    # CLI/SDK lanes have no HTTP business target; use named gateway selection
    # instead of the Insurance-specific lanes() target-URL requirement.
    ctl = gatewayctl()
    gateways = ctl.select([args.lanes])[:len(cases)]
    if not gateways:
        parser.error("no lanes selected")
    for g in gateways:
        if not g.is_lane or g.env.get("LLM_GATEWAY_THINKING_POLICY") != "passthrough":
            parser.error(f"{g.name}: need LANE_POOL and LLM_GATEWAY_THINKING_POLICY=passthrough")
        spec = LaneSpec(g.name, g.lane_pool, g.target_url or "", g.port, g.log_file,
                        g.registry_dir, g.ready_marker, g.env_file)
        if reason := lane_not_ready(spec):
            parser.error(reason)
    args.output.mkdir(parents=True)
    provenance = {"run_id": args.run_id, "case_ids": [c["case_id"] for c in cases],
                  "max_tokens": args.max_tokens, "case_timeout_seconds": args.timeout,
                  "turn_timeout_seconds": args.turn_timeout, "lanes": [g.name for g in gateways],
                  "thinking": args.thinking if args.mcp_config else "composition", "mcp_agent_id": "dsh" if args.mcp_config else None}
    for label, path in (("bundle", args.bundle), ("agent_prompt", args.agent_prompt), ("mcp_config", args.mcp_config)):
        if path:
            provenance[label] = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    save(args.output / "inputs.json", provenance)
    with ThreadPoolExecutor(max_workers=len(gateways)) as pool:
        futures = [pool.submit(run_lane, g, cases[i::len(gateways)], args) for i, g in enumerate(gateways)]
        results = [row for future in futures for row in future.result()]
    success = len(results) == len(cases) and all(r["success"] for r in results)
    save(args.output / "summary.json", {"run_id": args.run_id, "success": success,
                                       "requested_cases": [c["case_id"] for c in cases], "cases": results})
    # Scope by run id across all gateway logs to produce one concurrent report.
    completed = subprocess.run([sys.executable, str(ROOT / "llmtrace"), "report", "html",
                               "--log", str(ROOT / "llm_gateway/logs"), "--out", str(args.output),
                               "--run-id", args.run_id], cwd=ROOT)
    return 0 if success and completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
