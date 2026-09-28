"""Offline audit: remove runner artifacts, exercise three protocol shapes.

No model requests, service restarts, or changes to production artifacts.
Synthetic HTML contains no user data; real-case comparisons emit only counts/flags.
Run from the repository: python3 scripts/audit_tracing_sources.py
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval_harness/src"))
from eval_harness.adapters.base import CaseRunResult, TurnResult
from eval_harness.normalize_trace import build_trace
from eval_harness.llm_trace_html import (
    build_payload, build_llm_trace_html, materialize_wire_run,
)

OUT = ROOT / "var/tracing_source_audit"


def protocol_calls(protocol):
    tool = {"name": "lookup", "description": "Synthetic lookup"}
    if protocol == "chat_completions":
        use = {"id": "tool-1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"Q1"}'}}
        request = {"messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": None, "tool_calls": [use]},
            {"role": "tool", "tool_call_id": "tool-1", "content": "TOOL_RESULT"},
            {"role": "user", "content": "Q2"},
        ], "tools": [{"type": "function", "function": {**tool, "parameters": {"type": "object"}}}]}
        first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [use]}, "finish_reason": "tool_calls"}]}
        last = {"choices": [{"message": {"role": "assistant", "content": "MODEL_FINAL"}, "finish_reason": "stop"}]}
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    elif protocol == "anthropic_messages":
        use = {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {"q": "Q1"}}
        request = {"messages": [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": [use]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "TOOL_RESULT"}, {"type": "text", "text": "Q2"}]},
        ], "optional_params": {"system": "SYSTEM"}, "tools": [{**tool, "input_schema": {"type": "object"}}]}
        first = {"type": "message", "role": "assistant", "content": [use], "stop_reason": "tool_use"}
        last = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "MODEL_FINAL"}], "stop_reason": "end_turn"}
        usage = {"input_tokens": 11, "output_tokens": 7}
    else:
        use = {"type": "function_call", "id": "fc-1", "call_id": "tool-1", "name": "lookup", "arguments": '{"q":"Q1"}'}
        request = {"optional_params": {"instructions": "SYSTEM", "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "Q1"}]},
            use, {"type": "function_call_output", "call_id": "tool-1", "output": "TOOL_RESULT"},
            {"role": "user", "content": [{"type": "input_text", "text": "Q2"}]},
        ]}, "tools": [{"type": "function", **tool, "parameters": {"type": "object"}}]}
        first = {"id": "resp-1", "object": "response", "status": "completed", "output": [use]}
        last = {"id": "resp-2", "object": "response", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "MODEL_FINAL"}]}]}
        usage = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    calls = []
    for i, response in enumerate((first, last), 1):
        req = copy.deepcopy(request)
        if i == 1:
            if "messages" in req:
                req["messages"] = [m for m in req["messages"] if m.get("role") == "system"] + [{"role": "user", "content": "Q1"}]
            else:
                req["optional_params"]["input"] = [{"role": "user", "content": "Q1"}]
        calls.append({"seq": i, "call_id": f"{protocol}-{i}", "case_id": "A01", "execution_id": "exec-1", "protocol": protocol,
                      "ts": f"2026-09-28T10:00:0{(i-1)*3}+08:00", "latency_ms": 1000, "status_code": 200,
                      "request": req, "response": {"ts": f"2026-09-28T10:00:0{(i-1)*3+1}+08:00", "response": response, "usage": usage}})
    return calls


def write_calls(root, calls):
    case = root / "cases/A01"
    case.mkdir(parents=True, exist_ok=True)
    (case / "llm_calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
    return case


def runner_trace(case):
    # Deliberately includes facts outside model requests: postprocessing and wall time.
    result = CaseRunResult("A01", "session-1", [
        TurnResult(1, "Q1", 0, case / "stream1.jsonl", final_text="FIRST_BUSINESS_ANSWER"),
        TurnResult(2, "Q2", 0, case / "stream2.jsonl", final_text="POSTPROCESSED_FINAL"),
    ], True, 0, None, 9000)
    trace = build_trace(result)
    trace["metrics"]["first_frame_ms"] = 125
    (case / "trace.json").write_text(json.dumps(trace))


def compare(with_case, without_case):
    a, b = with_case["overview"], without_case["overview"]
    keys = ("wall_ms", "first_frame_ms", "num_turns", "num_tool_calls")
    return {"call_count": [with_case["n_calls"], without_case["n_calls"]],
            "normalized_calls_equal": with_case["calls"] == without_case["calls"],
            "success": [a.get("success"), b.get("success")],
            "exit_code": [a.get("exit_code"), b.get("exit_code")],
            "final_answers_equal": [t.get("final_text") for t in a.get("turns", [])] == [t.get("final_text") for t in b.get("turns", [])],
            "metrics": {k: [a.get("metrics", {}).get(k), b.get("metrics", {}).get(k)] for k in keys}}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"scope": "Offline fixture/record replay, not live upstream or browser interaction. No exhaustive protocol/streaming claim.", "protocols": {}, "real_cases": []}
    for protocol in ("chat_completions", "anthropic_messages", "responses"):
        root = OUT / protocol
        calls = protocol_calls(protocol)
        wire_dir = root / "wire_only"
        full_dir = root / "with_runner"
        write_calls(wire_dir, calls)
        runner_trace(write_calls(full_dir, calls))
        a, b = build_payload(full_dir)["cases"][0], build_payload(wire_dir)["cases"][0]
        parsed = b["calls"]
        row = compare(a, b)
        row["extraction"] = {
            "system": parsed[-1]["system"] == "SYSTEM",
            "user_input": "Q2" in json.dumps(parsed[-1]["messages"]),
            "tool_result_visible": "TOOL_RESULT" in json.dumps(parsed[-1]["messages"]),
            "tool_name_and_arguments": parsed[0]["tool_calls"] == [{"name": "lookup", "arguments": '{"q":"Q1"}', "id": "tool-1"}] if protocol != "anthropic_messages" else bool(parsed[0]["tool_calls"]),
            "tool_call_id_preserved_in_normalized_tool": any(t.get("id") == "tool-1" or t.get("call_id") == "tool-1" for t in parsed[0]["tool_calls"]),
            "model_final": parsed[-1]["assistant"] == "MODEL_FINAL",
            "input_output_tokens": parsed[-1]["usage"].get("prompt_tokens") == 11 and parsed[-1]["usage"].get("completion_tokens") == 7,
            "stop_reason_in_normalized_fields": any(k in parsed[-1] for k in ("stop_reason", "finish_reason", "response_status")),
        }
        row["raw_evidence_retained"] = "TOOL_RESULT" in json.dumps(parsed[-1]["raw_request"]) and "MODEL_FINAL" in json.dumps(parsed[-1]["raw_response"])
        assert row["call_count"] == [2, 2] and row["normalized_calls_equal"]
        assert all(row["extraction"].values()) and row["raw_evidence_retained"]
        assert row["success"] == [True, None] and row["metrics"]["num_turns"] == [2, None]
        for d in (wire_dir, full_dir):
            build_llm_trace_html(d)
            assert (d / "llm_trace.html").stat().st_size > 10000
        report["protocols"][protocol] = row

    samples = [
        ROOT / "eval_runs/layer1_smoke_claude_A14_20260927/cases/A14",
        ROOT / "eval_runs/layer1_smoke_pi_A14_20260927/cases/PI_A14",
        ROOT / "eval_runs/triple_datasetA_20260926_163640_bailian4002_conc120/insurance/cases/INS_A04",
    ]
    for source in samples:
        if not source.is_dir():
            report["real_cases"].append({"source": str(source.relative_to(ROOT)), "skipped": "not present"})
            continue
        with tempfile.TemporaryDirectory(prefix="trace-source-audit-") as temp:
            base = Path(temp)
            full_case = base / "full/cases" / source.name
            full_case.parent.mkdir(parents=True)
            full_case.symlink_to(source, target_is_directory=True)
            stripped = base / "stripped/cases" / source.name
            stripped.mkdir(parents=True)
            for name in ("llm_calls.jsonl", "llm_calls.jsonl.gz"):
                if (source / name).is_file():
                    shutil.copyfile(source / name, stripped / name)
            a = build_payload(base / "full")["cases"][0]
            b = build_payload(base / "stripped")["cases"][0]
            row = compare(a, b)
            row["source"] = str(source.relative_to(ROOT))
            row["protocols"] = sorted({str(c.get("protocol")) for c in b["calls"]})
            row["changed_call_fields"] = sorted({k for ca, cb in zip(a["calls"], b["calls"]) for k in ca.keys() | cb.keys() if ca.get(k) != cb.get(k)})
            report["real_cases"].append(row)
            # Only synthetic inputs are persisted as HTML; actual user data stays in source.

    # Rerun grouping: distinct executions remain distinct.
    with tempfile.TemporaryDirectory(prefix="trace-attempt-audit-") as temp:
        root = Path(temp)
        records = []
        for attempt in ("morning", "afternoon"):
            records += [
                {"event": "pre_api_call", "call_id": attempt, "case_id": "A01", "execution_id": attempt,
                 "ts": "2026-09-28T10:00:00+08:00", "messages": [{"role": "user", "content": "synthetic"}]},
                {"event": "success", "call_id": attempt, "response": {"choices": [{"message": {"content": attempt}}]}},
            ]
        log = root / "gateway.jsonl"
        log.write_text("\n".join(json.dumps(r) for r in records))
        materialize_wire_run(gateway_log=log, out_run_dir=root / "export")
        cases = build_payload(root / "export")["cases"]
        report["rerun_export"] = {"case_count": len(cases), "call_counts": [c["n_calls"] for c in cases],
                                  "execution_ids": sorted(c["identity"]["execution_id"] for c in cases)}
        assert len(cases) == 2 and all(c["n_calls"] == 1 for c in cases)
    # A shareable, synthetic-only page for checking batch/history controls.
    from eval_harness.trace_store import export_executions
    rows = []
    for batch, execution, hour in (("morning", "first", 8), ("morning", "retry", 9), ("afternoon", "new", 14)):
        for call in protocol_calls("responses"):
            rows.append({**call, "run_id": batch, "harness": "sdk", "execution_id": execution,
                         "started_at": f"2026-09-28T{hour:02}:00:00Z", "call_id": execution + call["call_id"]})
    export_executions(rows, OUT / "batch_history")
    build_llm_trace_html(OUT / "batch_history")
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
