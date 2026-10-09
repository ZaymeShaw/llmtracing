#!/usr/bin/env python3
"""Run DSH SDK sessions through exclusive gateway lanes and export wire reports.

Uses a local DSH checkout's built JSON-RPC runtime and Python SDK. Each lane
owns one worker process group at a time; no X-Eval headers are injected in DSH.
"""
from __future__ import annotations

import argparse
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
         "expected": "DSH_TOOL_RESULT_8675309", "require_tool": "bash"},
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
        with (out / "notifications.jsonl").open("w") as events:
            def emit(notification):
                events.write(json.dumps(redact(asdict(notification)), ensure_ascii=False) + "\n")
                events.flush()

            with DeepSeekHarness(
                provider="deepseek-official", model=job["model"], max_tokens=job["max_tokens"],
                cwd=str(out / "workspace"), runtime_cwd=str(out / "workspace"),
                session_root=str(out / "sessions"), cordis=job["cordis"],
                base_url=job["base_url"], api_key=os.environ["DSH_GATEWAY_KEY"],
                launch_args_override=(job["node"], str(checkout / "packages/examples/jsonrpc-demo/lib/bin.js")),
                env={"DSH_HOME": str(out / "home"), "DSH_TELEMETRY_DISABLED": "1"},
                request_timeout_seconds=30, shutdown_timeout_seconds=10,
            ) as harness:
                session = harness.start_session(job["execution_id"])
                for prompt in job["case"]["turns"]:
                    result = session.run(prompt, on_notification=emit)
                    status["turns"].append({"finish_reason": result.finish_reason,
                                            "final_response": result.final_response})
                    save(out / "sdk_result.json", status)
                    if result.finish_reason != "completed":
                        raise RuntimeError(f"DSH turn ended: {result.finish_reason}")
            status["success"] = True
    except Exception as exc:
        status["error"] = str(exc)
    save(out / "sdk_result.json", status)
    return 0 if status["success"] else 1


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
                   "base_url": f"http://127.0.0.1:{gateway.port}/v1", "case": case, "execution_id": eid}
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
                    and c.get("execution_id") == eid for c in calls)
                status["success"] &= status["attribution_ok"]
                final = status.get("turns", [{}])[-1].get("final_response", "") if status.get("turns") else ""
                if "expected" in case:
                    status["answer_ok"] = final.strip() == case["expected"]
                    status["success"] &= status["answer_ok"]
                if "require_tool" in case:
                    notifications = [json.loads(line) for line in (out / "notifications.jsonl").read_text().splitlines()]
                    status["tool_ok"] = any(n.get("payload", {}).get("event", {}).get("type") == "tool/call"
                                            and n["payload"]["event"].get("data", {}).get("name") == case["require_tool"]
                                            for n in notifications)
                    status["success"] &= status["tool_ok"]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--dsh-checkout", type=Path)
    parser.add_argument("--node", default="node")
    parser.add_argument("--cordis", type=Path)
    parser.add_argument("--lanes", default="dsh_smoke")
    parser.add_argument("--bundle", type=Path, help="JSONL of case_id and turns; default: three smoke cases")
    parser.add_argument("--cases", help="comma-separated case ids")
    parser.add_argument("--run-id", default="dsh_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if not args.dsh_checkout:
        parser.error("--dsh-checkout is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) or args.timeout <= 0 or args.max_tokens <= 0:
        parser.error("safe run-id and positive timeout/max-tokens required")
    args.dsh_checkout = args.dsh_checkout.resolve()
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
