"""Case-level tool-health judging: exit 0 is not success when the tool layer is broken."""
from __future__ import annotations

import json

import pytest

from eval_harness.excel_export import case_row_from_trace
from eval_harness.run import _classify_error
from eval_harness.tool_health import (
    ERROR_CLASS, apply_tool_health, assess_events, classify_tool_result, rejudge_run_dir,
)

PYD = ("PydanticUserError: `HarnessConf` is not fully defined; you should define `Literal`, "
       "then call `HarnessConf.model_rebuild()`.")
ENV_OK = json.dumps({"ok": True, "status": "SUCCESS", "facts": [{"fact_id": "F1"}],
                     "source": {"system": "policy_svc", "endpoint": "/customer/search"}, "request_id": "r1"})
ENV_EMPTY = json.dumps({"ok": False, "status": "EMPTY", "facts": [], "hint": "本次条件查询未命中客户记录",
                        "source": {"system": "policy_svc", "endpoint": "/customer/search"}, "request_id": "r2"})
ENV_LOCAL = json.dumps({"ok": False, "status": "EMPTY", "facts": [], "hint": "未提供客户中心 ID、姓名或手机号",
                        "source": None, "request_id": None})
ENV_UPSTREAM = json.dumps({"ok": False, "status": "ERROR", "hint": "后端服务暂时不可用",
                           "error": {"code": "UPSTREAM", "retryable": True},
                           "source": {"system": "policy_svc"}, "request_id": "r3"})


def _claude(*results):
    ev = []
    for i, (content, is_error) in enumerate(results):
        tid = f"toolu_{i}"
        ev.append({"kind": "tool_use", "payload": {"id": tid, "name": "mcp__insurance-tools__customer_search",
                                                   "input": {"query": "x"}}})
        ev.append({"kind": "tool_result", "payload": {"tool_use_id": tid, "content": content, "is_error": is_error}})
    return {"case_id": "A01", "harness": "claude_code", "success": True, "exit_code": 0, "error": None,
            "events": ev, "turns": [], "metrics": {}}


def _pi_block(text):
    return {"content": [{"type": "text", "text": text}], "details": {"server": "insurance-tools"}}


def test_all_pydantic_errors_flag_case() -> None:
    tr = apply_tool_health(_claude((PYD, True), (PYD, True)))
    assert tr["success"] is False and tr["error_class"] == ERROR_CLASS
    assert tr["error"].startswith(ERROR_CLASS) and tr["tool_health"]["infra"] == 2
    assert _classify_error(tr["error"]) == ERROR_CLASS


def test_business_not_found_stays_success() -> None:
    tr = apply_tool_health(_claude((ENV_EMPTY, False)))
    assert tr["success"] is True and "error_class" not in tr
    assert tr["tool_health"] == {**tr["tool_health"], "ok": 1, "infra": 0, "flagged": False}


def test_one_success_among_infra_errors_is_not_flagged() -> None:
    tr = apply_tool_health(_claude((PYD, True), (ENV_OK, False)))
    assert tr["success"] is True and tr["tool_health"]["flagged"] is False


def test_local_precheck_envelope_is_neutral() -> None:
    tr = apply_tool_health(_claude((PYD, True), (ENV_LOCAL, False)))
    assert tr["tool_health"]["local_only"] == 1 and tr["success"] is False


def test_no_insurance_tool_calls_not_flagged() -> None:
    tr = _claude()
    tr["events"] = [{"kind": "tool_use", "payload": {"id": "b1", "name": "Bash", "input": {}}},
                    {"kind": "tool_result", "payload": {"tool_use_id": "b1", "content": "Traceback (most recent call last)", "is_error": True}}]
    assert apply_tool_health(tr)["success"] is True


def test_pi_direct_and_proxy_shapes() -> None:
    ev = [
        {"kind": "tool_use", "payload": {"id": "c1", "name": "insurance-tools_policy_search", "input": {"query": "q"}}},
        {"kind": "tool_use", "payload": {"id": "c1", "name": "insurance-tools_policy_search", "input": {"query": "q"}}},
        {"kind": "tool_result", "payload": {"tool_use_id": "c1", "content": _pi_block("Error: " + PYD), "is_error": True}},
        {"kind": "tool_use", "payload": {"id": "c2", "name": "mcp", "input": {}}},
        {"kind": "tool_result", "payload": {"tool_use_id": "c2", "content": _pi_block("MCP: 1/1 servers, 7 tools"), "is_error": False}},
        {"kind": "tool_use", "payload": {"id": "c3", "name": "mcp", "input": {"tool": "policy_search", "args": {}}}},
        {"kind": "tool_use", "payload": {"id": "c3", "name": "policy_search", "input": {"tool": "policy_search"}}},
        {"kind": "tool_result", "payload": {"tool_use_id": "c3", "content": _pi_block("Error: " + PYD), "is_error": True}},
    ]
    s = assess_events(ev)
    assert s["infra"] == 2 and s["ok"] == 0 and s["flagged"] is True  # mcp status call ignored
    tr = apply_tool_health({"harness": "pi_coding", "success": True, "events": ev})
    assert tr["success"] is False


@pytest.mark.parametrize("text,is_error,kind", [
    ("Error: McpError: MCP error -32000: Connection closed", True, "infra"),
    ("httpx.ConnectError: [Errno 61] Connection refused", True, "infra"),
    ("本次工具调用失败，未能获取相关信息，请稍后再试。", False, "upstream"),
    ("参数不符合工具签名:<root>: 'query' is a required property", True, "error"),
    (ENV_UPSTREAM, False, "upstream"),
    (ENV_EMPTY, False, "ok"),
])
def test_classify(text, is_error, kind) -> None:
    assert classify_tool_result(text, is_error)[0] == kind


def test_upstream_knob_strict_mode_demotes(monkeypatch) -> None:
    monkeypatch.setenv("EVAL_TOOL_HEALTH_UPSTREAM_IS_INFRA", "1")
    tr = apply_tool_health(_claude((ENV_UPSTREAM, False)))
    assert tr["success"] is False and tr["error_class"] == ERROR_CLASS


def test_idempotent_and_non_agent_untouched() -> None:
    tr = apply_tool_health(apply_tool_health(_claude((PYD, True))))
    assert tr["success"] is False and tr["error"].count(ERROR_CLASS) == 1
    ins = {"harness": "insurance_qa_agno", "success": True, "events": _claude((PYD, True))["events"]}
    assert apply_tool_health(ins)["success"] is True and "tool_health" not in ins


def test_excel_row_exposes_error_class() -> None:
    tr = apply_tool_health(_claude((PYD, True)))
    row = case_row_from_trace(tr, class_letter="A", n_turns=1, trace_relpath="cases/A01/trace.json")
    assert row["success"] is False and row["error_class"] == ERROR_CLASS
    assert row["tool_infra_errors"] == 1 and row["tool_ok_calls"] == 0


def test_rejudge_run_dir_is_read_only(tmp_path) -> None:
    d = tmp_path / "run" / "cases" / "A01"
    d.mkdir(parents=True)
    raw = json.dumps(_claude((PYD, True)))
    (d / "trace.json").write_text(raw, encoding="utf-8")
    rep = rejudge_run_dir(tmp_path / "run")
    assert rep["success_before"] == 1 and rep["success_after"] == 0 and rep["flagged_tool_infra_error"] == 1
    assert (d / "trace.json").read_text(encoding="utf-8") == raw


# ---- upstream/backend errors: exposed, never our failure -------------------------------------
from pathlib import Path  # noqa: E402

from eval_harness.tool_health import (  # noqa: E402
    UPSTREAM_BLOCKED, UPSTREAM_DEGRADED, UpstreamMonitor, format_status, status_line, summarize_traces,
)

ENV_CONTRACT = json.dumps({"ok": False, "status": "ERROR", "hint": "上游响应格式不符合接口契约，请联系服务维护人员",
                           "error": {"code": "UPSTREAM", "retryable": False},
                           "source": {"system": "policy_svc", "endpoint": "/policy/search"}, "request_id": "r4"})
ENV_OVERLOADED = json.dumps({"ok": False, "status": "ERROR", "hint": "查询服务暂时繁忙，请稍后重试。",
                             "error": {"code": "OVERLOADED"}, "source": {"system": "policy_svc"}, "request_id": "r5"})
ENV_TIMEOUT = json.dumps({"ok": False, "status": "TIMEOUT", "hint": "后端查询超时,可稍后重试一次",
                          "error": {"code": "TIMEOUT"}, "source": {"system": "policy_svc"}, "request_id": "r6"})
FIX = Path(__file__).parent / "fixtures" / "tool_health"


def test_default_upstream_is_not_infra(monkeypatch) -> None:
    monkeypatch.delenv("EVAL_TOOL_HEALTH_UPSTREAM_IS_INFRA", raising=False)
    tr = apply_tool_health(_claude((ENV_UPSTREAM, False), (ENV_CONTRACT, False)))
    assert tr["success"] is True and "error_class" not in tr
    assert tr["upstream_status"] == UPSTREAM_BLOCKED
    assert tr["upstream_errors"] == {"UPSTREAM(后端服务暂时不可用)": 1, "UPSTREAM(上游响应格式不符合接口契约，请联系服务维护人员)": 1}
    assert tr["tool_health"]["upstream"] == 2 and tr["tool_health"]["infra"] == 0


def test_upstream_degraded_and_codes() -> None:
    tr = apply_tool_health(_claude((ENV_OK, False), (ENV_OVERLOADED, False), (ENV_TIMEOUT, False)))
    assert tr["success"] is True and tr["upstream_status"] == UPSTREAM_DEGRADED
    assert set(tr["upstream_errors"]) == {"OVERLOADED(查询服务暂时繁忙，请稍后重试)", "TIMEOUT(后端查询超时,可稍后重试一次)"}


def test_ours_plus_upstream_without_ok_is_still_infra() -> None:
    tr = apply_tool_health(_claude((PYD, True), (ENV_UPSTREAM, False)))
    assert tr["success"] is False and tr["error_class"] == ERROR_CLASS
    assert tr["upstream_status"] == UPSTREAM_BLOCKED


def test_rejudge_restores_old_upstream_demotion() -> None:
    tr = _claude((ENV_UPSTREAM, False))
    tr.update(success=False, error_class=ERROR_CLASS,
              error=f"{ERROR_CLASS}: 1 infra-class tool error(s), 0 successful insurance tool calls; e.g. 'x'")
    apply_tool_health(tr)
    assert tr["success"] is True and tr["error"] is None and "error_class" not in tr
    assert tr["upstream_status"] == UPSTREAM_BLOCKED
    # and the flag goes away when upstream errors vanish
    tr["events"] = _claude((ENV_OK, False))["events"]
    apply_tool_health(tr)
    assert "upstream_status" not in tr and "upstream_errors" not in tr


def test_invalid_run_fixtures_still_tool_infra_error() -> None:
    for fp in sorted(FIX.glob("invalid_*.json")):
        tr = apply_tool_health(json.loads(fp.read_text(encoding="utf-8")))
        assert tr["success"] is False and tr["error_class"] == ERROR_CLASS, fp.name
        assert "upstream_status" not in tr


def test_excel_upstream_columns() -> None:
    tr = apply_tool_health(_claude((ENV_UPSTREAM, False), (ENV_UPSTREAM, False)))
    row = case_row_from_trace(tr, class_letter="A", n_turns=1, trace_relpath="x")
    assert row["success"] is True and row["upstream_status"] == UPSTREAM_BLOCKED
    assert row["upstream_errors"] == "UPSTREAM(后端服务暂时不可用) x2"
    ok = case_row_from_trace(apply_tool_health(_claude((ENV_OK, False))), class_letter="A", n_turns=1, trace_relpath="x")
    assert ok["upstream_status"] == "none" and ok["upstream_errors"] is None


def test_summarize_traces() -> None:
    a = apply_tool_health(_claude((ENV_UPSTREAM, False)))
    b = dict(apply_tool_health(_claude((ENV_OK, False), (ENV_UPSTREAM, False))), case_id="A02")
    c = dict(apply_tool_health(_claude((PYD, True))), case_id="A03")
    s = summarize_traces([a, b, c])
    assert s[UPSTREAM_BLOCKED] == 1 and s[UPSTREAM_DEGRADED] == 1 and s["tool_infra_error"] == 1
    assert s["blocked_ids"] == ["A01"] and s["upstream_errors"] == {"UPSTREAM(后端服务暂时不可用)": 2}


def test_monitor_warns_and_alerts_on_window(tmp_path) -> None:
    lines, rows = [], []
    mon = UpstreamMonitor(tmp_path, "claude", window=10, threshold=3, emit=lines.append, progress_writer=rows.append)
    blocked = apply_tool_health(_claude((ENV_UPSTREAM, False)))
    healthy = apply_tool_health(_claude((ENV_OK, False)))
    mon.observe("A01", blocked)
    mon.observe("A02", healthy)
    mon.observe("A03", blocked)
    assert not (tmp_path / "upstream_alert.json").exists()
    assert sum("[upstream] WARN" in l for l in lines) == 2 and "case=A01" in lines[0]
    rec = mon.observe("A04", blocked)
    assert rec["alert"] is True and rec["recent_blocked_ids"] == ["A01", "A03", "A04"]
    assert any("[upstream] ALERT side=claude 3/4" in l for l in lines)
    alert = json.loads((tmp_path / "upstream_alert.json").read_text(encoding="utf-8"))
    assert alert["alert"] and alert["totals"][UPSTREAM_BLOCKED] == 3 and alert["alerts"] == 1
    assert rows and rows[-1]["action"] == "upstream_alert"
    for i in range(8):  # window slides -> alert clears
        mon.observe(f"B{i}", healthy)
    assert any("alert cleared" in l for l in lines)


def test_status_line_from_progress(tmp_path) -> None:
    rows = [
        {"case_id": "A01", "action": "run", "success": True, "upstream_status": UPSTREAM_BLOCKED},
        {"case_id": "A02", "action": "run", "success": True, "upstream_status": UPSTREAM_DEGRADED},
        {"case_id": "A03", "action": "run", "success": False, "error_class": ERROR_CLASS},
        {"case_id": "A01", "action": "upstream_alert", "alert": True},
    ]
    (tmp_path / "progress.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    st = status_line(tmp_path)
    assert st["done"] == 3 and st[UPSTREAM_BLOCKED] == ["A01"] and st[UPSTREAM_DEGRADED] == 1
    line = format_status(st, "claude")
    assert "upstream_blocked=1 A01" in line and "tool_infra_error=1" in line
