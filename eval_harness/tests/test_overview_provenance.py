"""Overview metrics must say whether they were measured by the harness or inferred from the gateway."""
from __future__ import annotations

import json
from pathlib import Path

from eval_harness.llm_trace_html import build_payload, build_llm_trace_html


def _call(case_id: str, user: str, reply: str, *, first_frame_ms: int | None = None) -> str:
    row = {
        "seq": 1, "call_id": f"call-{case_id}", "case_id": case_id,
        "ts": "2026-09-30T12:00:00+00:00", "latency_ms": 900,
        "request": {"messages": [{"role": "user", "content": user}]},
        "response": {"response": {"choices": [{"message": {"role": "assistant", "content": reply}}]}},
    }
    if first_frame_ms is not None:
        row.update(first_frame_ms=first_frame_ms, first_frame_kind="text")
    return json.dumps(row, ensure_ascii=False) + "\n"


def _overview(run_dir: Path) -> dict:
    return build_payload(run_dir)["cases"][0]["overview"]


def test_wire_only_marks_answer_and_first_frame_inferred(tmp_path: Path):
    case = tmp_path / "cases" / "OC_A01"
    case.mkdir(parents=True)
    (case / "llm_calls.jsonl").write_text(_call("OC_A01", "问题", "最后一次模型回复", first_frame_ms=300),
                                          encoding="utf-8")
    ov = _overview(tmp_path)
    assert ov["turns"][0]["final_text"] == "最后一次模型回复"
    assert ov["provenance"] == {
        "final_text": "inferred", "first_frame_ms": "inferred", "wall_ms": "inferred", "success": None,
    }


def test_insurance_business_answer_is_harness_but_first_frame_inferred(tmp_path: Path):
    case = tmp_path / "cases" / "INS_A01"
    case.mkdir(parents=True)
    (case / "meta.json").write_text(json.dumps({
        "case_id": "INS_A01", "prompt": "问题", "answer": "业务接口最终答案",
        "success": True, "wall_ms": 42, "attribution_status": "attributed",
    }, ensure_ascii=False), encoding="utf-8")
    (case / "llm_calls.jsonl").write_text(_call("INS_A01", "问题", "模型草稿", first_frame_ms=200),
                                          encoding="utf-8")
    ov = _overview(tmp_path)
    assert ov["turns"][0]["final_text"] == "业务接口最终答案"
    assert ov["provenance"] == {
        "final_text": "harness", "first_frame_ms": "inferred", "wall_ms": "harness", "success": "harness",
    }


def test_full_trace_is_harness(tmp_path: Path):
    case = tmp_path / "cases" / "A01"
    case.mkdir(parents=True)
    (case / "trace.json").write_text(json.dumps({
        "case_id": "A01", "success": True, "exit_code": 0,
        "metrics": {"wall_ms": 5000, "first_frame_ms": 800, "first_frame_kind": "text"},
        "turns": [{"index": 1, "prompt": "问题", "final_text": "CLI 最终输出"}],
        "events": [{"kind": "text", "payload": {"text": "CLI 最终输出"}}],
    }, ensure_ascii=False), encoding="utf-8")
    ov = _overview(tmp_path)
    assert ov["provenance"] == {
        "final_text": "harness", "first_frame_ms": "harness", "wall_ms": "harness", "success": "harness",
    }


def test_minimal_adapter_status_is_harness_but_turns_inferred(tmp_path: Path):
    case = tmp_path / "cases" / "X01"
    case.mkdir(parents=True)
    (case / "trace.json").write_text(json.dumps({
        "case_id": "X01", "success": False, "exit_code": 1, "metrics": {"wall_ms": 1234},
    }), encoding="utf-8")
    (case / "llm_calls.jsonl").write_text(_call("X01", "问题", "模型回复"), encoding="utf-8")
    ov = _overview(tmp_path)
    assert ov["provenance"]["final_text"] == "inferred"
    assert ov["provenance"]["success"] == "harness"
    assert ov["provenance"]["wall_ms"] == "harness"
    # No measured stream timing on the call: never fabricate a first frame.
    assert ov["provenance"]["first_frame_ms"] is None and ov["metrics"]["first_frame_ms"] is None


def test_html_renders_provenance_badges(tmp_path: Path):
    case = tmp_path / "cases" / "OC_A01"
    case.mkdir(parents=True)
    (case / "llm_calls.jsonl").write_text(_call("OC_A01", "问题", "回复", first_frame_ms=300), encoding="utf-8")
    html = build_llm_trace_html(tmp_path).read_text(encoding="utf-8")
    assert "srcBadge(ov, 'final_text')" in html and "srcBadge(ov, 'first_frame_ms')" in html
    assert '"provenance"' in html
