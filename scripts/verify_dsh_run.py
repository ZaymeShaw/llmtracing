#!/usr/bin/env python3
"""Read-only DSH integrity audit; output contains identifiers and checks, not payloads.

Validates native SDK results against gateway histories and the unchanged shared
HTML report. This is transport/execution verification, not an answer accuracy score.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval_harness/src"))
from eval_harness.llm_gateway_ingest import pairs_from_lines, pairs_to_trace_calls
from eval_harness.llm_trace_html import _normalize_call
from eval_harness.protocol_trace import text_content
from eval_harness.wire_turns import observed_turns
from eval_harness.case_artifacts import read_calls_bytes


def load(path):
    return json.loads(path.read_text())


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def payload_digest(row):
    return digest(json.dumps({k: row.get(k) for k in ("request", "response")}, ensure_ascii=False, sort_keys=True))


def verify_case(status_path, expected, run_id, persona):
    status = load(status_path)
    directory = status_path.parent
    capture = load(directory / "gateway_calls.json")
    sdk = load(directory / "sdk_result.json")
    calls = capture["calls"]
    events = sorted((e for c in calls for e in c["events"]), key=lambda e: e.get("ts", ""))
    pairs = pairs_from_lines([(str(directory), i, i, json.dumps(e)) for i, e in enumerate(events)], sort_by_ts=True)
    rows = pairs_to_trace_calls(pairs, case_id=status["case_id"])
    normalized = [_normalize_call(row) for row in rows]
    turns, _, others = observed_turns(normalized)
    requests = [e for e in events if e.get("event") == "pre_api_call"]
    messages = [m for req in requests for m in req.get("messages", [])]
    returned = {}
    for message in messages:
        if message.get("role") == "tool":
            returned.setdefault(message.get("tool_call_id"), set()).add(digest(text_content(message.get("content"))))
    native_calls, native_results = {}, {}
    for line in (directory / "notifications.jsonl").read_text().splitlines():
        notification = json.loads(line)
        event = notification.get("payload", {}).get("event", {})
        data = event.get("data", {})
        if event.get("type") == "tool/call":
            native_calls[data["callId"]] = data["name"]
        if event.get("type") == "tool/result":
            for block in data.get("message", {}).get("content", []):
                if block.get("type") == "tool-result":
                    native_results[block["toolCallId"]] = digest(text_content(block.get("content")))
    discovered = set(load(directory / "runtime_ready.json")["tools"])
    tool_sets = [{t.get("function", {}).get("name") for t in req.get("tools", [])} for req in requests]
    checks = {
        "capture_complete": capture["status"] == "complete" and not capture["conflicts"],
        "physical_requests_closed": bool(requests) and all(
            sum(e.get("event") == "pre_api_call" for e in c["events"])
            == sum(e.get("event") in ("success", "failure") for e in c["events"])
            for c in calls),
        "all_llm_calls_have_terminal_records": bool(calls) and all(
            c["state"] in ("succeeded", "failed") and c.get("ended_at")
            and (c["state"] != "failed" or c.get("error")) for c in calls),
        "unique_call_ids": len({c["call_id"] for c in calls}) == len(calls),
        "lane_attribution": bool(calls) and all(
            c.get("run_id") == run_id and c.get("case_id") == status["case_id"]
            and c.get("execution_id") == status["execution_id"]
            and c.get("lane_id") == status["gateway"] and c.get("case_id_source") == "lane_registry"
            for c in calls),
        "authorized_model": bool(requests) and all(r.get("model") == "deepseek-v4-flash-0731" for r in requests),
        "response_model_matches": all(isinstance(e.get("response"), dict)
            and e["response"].get("model") == "deepseek-v4-flash-0731" for e in events if e.get("event") == "success"),
        "usage_present": all(isinstance((c.get("usage") or {}).get("prompt_tokens"), int)
            and isinstance((c.get("usage") or {}).get("completion_tokens"), int) for c in calls if c["state"] == "succeeded"),
        "thinking_off": bool(requests) and all(r.get("thinking_effective") == "off" for r in requests),
        "all_seven_tools_available": len(discovered) == 7 and bool(tool_sets) and all(t == discovered for t in tool_sets),
        "shared_business_prompt": bool(requests) and all(any(
            m.get("role") == "system" and persona in text_content(m.get("content")) for m in r.get("messages", [])) for r in requests),
        "sdk_completed_all_turns": sdk.get("success") is True and len(sdk["turns"]) == len(expected)
            and all(t.get("finish_reason") == "completed" and t.get("final_response", "").strip() for t in sdk["turns"]),
        "wire_has_all_original_turns": [t["prompt"] for t in turns] == expected and others == 0,
        "sdk_answers_match_wire": [t["final_text"] for t in turns] == [t["final_response"] for t in sdk["turns"]],
        "native_tool_results_complete": native_calls.keys() == native_results.keys(),
        "native_tool_results_returned_unchanged": all(h in returned.get(k, set()) for k, h in native_results.items()),
        "text_first_frame_captured": all(isinstance(c.get("first_frame_ms"), (int, float))
            and c["first_frame_ms"] >= 0 for c in normalized if c.get("assistant") or c.get("thinking")),
    }
    return {"case_id": status["case_id"], "execution_id": status["execution_id"],
            "gateway": status["gateway"], "runner_success": status["success"],
            "llm_requests": len(calls), "physical_requests": len(requests),
            "model_failures": sum(e.get("event") == "failure" for e in events),
            "input_tokens": sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in calls),
            "output_tokens": sum((c.get("usage") or {}).get("completion_tokens", 0) for c in calls),
            "mcp_calls": len(native_calls), "turns": len(turns),
            "wall_seconds": status["wall_seconds"], "checks": checks,
            "integrity_pass": all(checks.values()), "mcp_health": status.get("mcp_health"),
            "payload_sha256": {r["call_id"]: payload_digest(r) for r in rows}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--partial", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    inputs = load(args.run_dir / "inputs.json")
    bundle = [json.loads(line) for line in Path(inputs["bundle"]["path"]).read_text().splitlines() if line.strip()]
    expected = {c["case_id"]: c["turns"] for c in bundle if c["case_id"] in inputs["case_ids"]}
    persona = Path(inputs["agent_prompt"]["path"]).read_text()
    checks = {f"{key}_hash_unchanged": hashlib.sha256(Path(inputs[key]["path"]).read_bytes()).hexdigest() == inputs[key]["sha256"]
              for key in ("bundle", "agent_prompt", "mcp_config")}
    results, errors = [], []
    for path in sorted(args.run_dir.glob("raw/*/*/status.json")):
        status = load(path)
        if "wall_seconds" not in status:
            continue
        try:
            results.append(verify_case(path, expected[status["case_id"]], inputs["run_id"], persona))
        except (KeyError, ValueError, OSError) as exc:
            errors.append({"case_id": status["case_id"], "error_type": type(exc).__name__})
    if not args.partial:
        checks["all_requested_cases_executed_once"] = Counter(r["case_id"] for r in results) == Counter(expected.keys())
        checks["unique_execution_ids"] = len({r["execution_id"] for r in results}) == len(results)
        call_ids = [cid for r in results for cid in r["payload_sha256"]]
        checks["unique_call_ids_across_cases"] = len(set(call_ids)) == len(call_ids)
        html_path = args.run_dir / "llm_trace.html"
        if html_path.exists():
            match = re.search(r'<script id="DATA" type="application/json">(.*?)</script>', html_path.read_text(), re.S)
            payload = json.loads(match.group(1)) if match else {}
            report_cases = payload.get("cases", [])
            checks["html_has_all_requested_cases"] = Counter(c["identity"]["case_id"] for c in report_cases) == Counter(expected.keys())
            checks["html_turns_and_calls_match_capture"] = all(
                c["overview"]["turns"] == [{"index": i, "prompt": p, "final_text": a["final_response"]}
                    for i, (p, a) in enumerate(zip(expected[c["identity"]["case_id"]], load(args.run_dir / "raw" / c["identity"]["case_id"] / c["identity"]["execution_id"] / "sdk_result.json")["turns"]), 1)]
                and c["n_calls"] == next(r["llm_requests"] for r in results if r["execution_id"] == c["identity"]["execution_id"])
                for c in report_cases)
            exported = {}
            readable = True
            for p in (args.run_dir / "cases").glob("*/identity.json"):
                raw = read_calls_bytes(p.parent)
                if raw is None:
                    readable = False
                    continue
                identity = load(p)
                exported[identity["execution_id"]] = {row["call_id"]: payload_digest(row)
                    for line in raw.decode("utf-8").splitlines() if line.strip() for row in [json.loads(line)]}
            checks["case_gzip_readable"] = readable
            checks["exported_payloads_match_captured_payloads"] = exported == {r["execution_id"]: r["payload_sha256"] for r in results}
        else:
            checks["html_exists"] = False
    failed = [{"case_id": r["case_id"], "checks": [k for k, ok in r["checks"].items() if not ok]} for r in results if not r["integrity_pass"]]
    report = {"run_id": inputs["run_id"], "partial": args.partial,
              "integrity_pass": not errors and not failed and all(checks.values()),
              "business_accuracy_evaluated": False, "checks": checks,
              "completed_cases": len(results), "requested_cases": len(expected),
              "llm_requests": sum(r["llm_requests"] for r in results),
              "physical_requests": sum(r["physical_requests"] for r in results),
              "model_failures": sum(r["model_failures"] for r in results),
              "input_tokens": sum(r["input_tokens"] for r in results),
              "output_tokens": sum(r["output_tokens"] for r in results),
              "mcp_calls": sum(r["mcp_calls"] for r in results),
              "failed_checks": failed, "audit_errors": errors, "cases": results}
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))
    return 0 if report["integrity_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
