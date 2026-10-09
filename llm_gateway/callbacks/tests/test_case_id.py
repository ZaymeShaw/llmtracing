from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from callbacks.case_id import detect_protocol, extract_case_id, strip_eval_case_id_from_body


def test_priority_header_over_body():
    cid, src, warns = extract_case_id(
        headers={"X-Eval-Case-Id": "HDR"},
        body={"eval_case_id": "BODY", "messages": []},
    )
    assert cid == "HDR" and src == "header"
    assert warns and "conflict" in warns[0]


def test_body_field_chat():
    cid, src, warns = extract_case_id(body={"eval_case_id": "PROBE_CC", "messages": [{"role": "user", "content": "x"}]})
    assert cid == "PROBE_CC" and src == "body" and not warns


def test_optional_params():
    cid, src, _ = extract_case_id(optional_params={"eval_case_id": "PROBE_CC"})
    assert cid == "PROBE_CC" and src == "optional_params"


def test_responses_metadata():
    cid, src, _ = extract_case_id(body={"input": "ping", "metadata": {"eval_case_id": "PROBE_RESP"}})
    assert cid == "PROBE_RESP" and src == "body"


def test_text_marker_fallback():
    cid, src, _ = extract_case_id(messages=[{"role": "system", "content": "<!--eval_case_id:A01--> hi"}])
    assert cid == "A01" and src == "text"


def test_no_file_fallback_returns_none():
    cid, src, _ = extract_case_id(body={"messages": [{"role": "user", "content": "ping"}]})
    assert cid is None and src is None


def test_strip_body():
    out = strip_eval_case_id_from_body({"model": "m", "eval_case_id": "X", "metadata": {"eval_case_id": "X", "k": 1}})
    assert "eval_case_id" not in out
    assert out["metadata"] == {"k": 1}


def test_detect_protocol():
    assert detect_protocol(path="/v1/chat/completions") == "chat_completions"
    assert detect_protocol(path="/v1/responses") == "responses"
    assert detect_protocol(path="/v1/messages") == "anthropic_messages"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL_PASS")


# --- W3C traceparent / baggage -------------------------------------------------
from callbacks.case_id import (  # noqa: E402
    baggage_entries,
    extract_trace_labels,
    resolve_execution_id,
    trace_id_from_traceparent,
)

EID = "8f385dd2b3a2468aa9161121eae9c688"
TP = f"00-{EID}-00f067aa0ba902b7-01"


def test_traceparent_validation():
    assert trace_id_from_traceparent({"traceparent": TP}) == EID
    assert trace_id_from_traceparent({"Traceparent": TP.upper()}) is None  # lowercase hex only
    for bad in ("00-" + "0" * 32 + "-00f067aa0ba902b7-01", f"00-{EID}-" + "0" * 16 + "-01",
                f"ff-{EID}-00f067aa0ba902b7-01", f"00-{EID[:-1]}-00f067aa0ba902b7-01", "junk"):
        assert trace_id_from_traceparent({"traceparent": bad}) is None, bad


def test_baggage_parsing():
    h = {"baggage": "other=1, eval.case_id=INS_A01;prop=x,eval.run_id=runs%2Fr1 ,eval.case_id=dup"}
    assert baggage_entries(h) == {"eval.case_id": "INS_A01", "eval.run_id": "runs/r1"}
    assert baggage_entries({"baggage": "eval.case_id=" + "x" * 9000}) == {}


def test_execution_id_precedence_and_conflicts():
    bag = f"eval.execution_id={EID},eval.case_id=C1"
    assert resolve_execution_id(headers={"traceparent": TP}) == (EID, "traceparent", [])
    other = "a" * 32
    # OTel client instrumentation re-rooted the trace: baggage still wins.
    eid, src, warns = resolve_execution_id(headers={"baggage": bag, "traceparent": f"00-{other}-00f067aa0ba902b7-01"})
    assert (eid, src) == (EID, "baggage") and warns and other in warns[0]
    eid, src, _ = resolve_execution_id(headers={"x-eval-execution-id": "hdr1", "baggage": bag, "traceparent": TP})
    assert (eid, src) == ("hdr1", "header")
    assert resolve_execution_id(headers={}) == (None, None, [])


def test_case_id_from_baggage_below_header():
    assert extract_case_id(headers={"baggage": "eval.case_id=C1"}) == ("C1", "baggage", [])
    cid, src, warns = extract_case_id(headers={"x-eval-case-id": "C2", "baggage": "eval.case_id=C1"})
    assert (cid, src) == ("C2", "header") and warns


def test_trace_labels_from_baggage_and_traceparent():
    labels = extract_trace_labels(headers={"baggage": "eval.run_id=r1,eval.harness=h", "traceparent": TP})
    assert labels == {"run_id": "r1", "harness": "h", "trace_id": EID}
    assert extract_trace_labels(headers={"x-eval-run-id": "r2", "baggage": "eval.run_id=r1"})["run_id"] == "r2"


def test_verifier_labels_are_independent_of_business_ids():
    from callbacks.case_id import extract_trace_labels
    result = extract_trace_labels(headers={
        'X-Eval-Harness': 'verifier', 'X-Eval-Run-Id': 'job-1',
        'X-Eval-Project-Id': 'llm_probe', 'X-Eval-Role': 'judge',
        'X-Eval-Stage': 'assessment', 'X-Eval-Verifier-Trace-Id': 'business-trace-ref',
    })
    assert result == dict(harness='verifier', run_id='job-1', project_id='llm_probe',
                          role='judge', stage='assessment', verifier_trace_id='business-trace-ref')
    assert 'execution_id' not in result
