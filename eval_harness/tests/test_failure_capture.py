"""Regression: real callback shapes -> ingest -> query and HTML normalization."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "llm_gateway"), str(ROOT / "eval_harness/src")]
from callbacks import trace_callback as callback
from eval_harness.llm_gateway_ingest import pairs_from_lines, pairs_to_trace_calls
from eval_harness.llm_trace_html import _normalize_call
from eval_harness.query import call_record


def views(events):
    pairs = pairs_from_lines(("log", i, i + 1, json.dumps(e)) for i, e in enumerate(events))
    pair = pairs[0]
    return call_record(pair, events), _normalize_call(pairs_to_trace_calls(pairs, case_id="C")[0])


@pytest.mark.parametrize("status", [400, 429, None])
def test_exception_not_response_is_failure_source(monkeypatch, status):
    events = []
    monkeypatch.setattr(callback, "_append", events.append)
    monkeypatch.setenv("UPSTREAM_API_KEY", "synthetic-private-key")
    exc = RuntimeError("CONTROLLED_ERROR synthetic-private-key Bearer token-secret")
    exc.status_code = status
    exc.request = {"headers": {"Authorization": "never-serialize-request"}}
    start = datetime.now(timezone.utc)
    kwargs = {"litellm_call_id": "c", "exception": exc, "_eval_attribution": {"case_id": "C"},
              "_eval_protocol": "chat_completions"}
    asyncio.run(callback.EvalTraceLogger().async_log_failure_event(kwargs, None, start, start + timedelta(milliseconds=25)))
    query, html = views(events)
    assert query["state"] == html["state"] == "failed"
    assert query["status_code"] == html["status_code"] == status
    assert query["latency_ms"] == 25
    assert query["ended_at"] == html["ts_end"]
    assert "CONTROLLED_ERROR" in query["error"]["message"]
    text = json.dumps(events)
    assert all(s not in text for s in ("synthetic-private-key", "token-secret", "never-serialize-request"))


@pytest.mark.parametrize("reverse", [False, True])
def test_stream_failure_retains_partial_tools_and_text(monkeypatch, reverse):
    events = [{"event": "pre_api_call", "call_id": "c", "ts": "2026-01-01T00:00:00+00:00"}]
    monkeypatch.setattr(callback, "_append", events.append)
    details = {"litellm_call_id": "c"}
    class Broken:
        logging_obj = SimpleNamespace(model_call_details=details)
        async def __aiter__(self):
            for delta in [
                {"tool_calls": [{"index": 0, "id": "t", "function": {"name": "lookup", "arguments": '{"x":'}}]},
                {"content": "PARTIAL_", "tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]},
                {"content": "TEXT"},
            ]:
                yield {"choices": [{"index": 0, "delta": delta}]}
            raise ConnectionError("truncated stream")
    async def consume():
        logger = callback.EvalTraceLogger()
        with pytest.raises(ConnectionError):
            async for _ in logger.async_post_call_streaming_iterator_hook(None, Broken(), {}):
                pass
        await logger.async_log_failure_event({**details, "exception": ConnectionError("truncated stream")}, None, None, None)
    asyncio.run(consume())
    if reverse:
        events[1:] = reversed(events[1:])
    query, html = views(events)
    assert query["state"] == "failed" and query["status_code"] is None
    assert html["incomplete"] and html["assistant"] == "PARTIAL_TEXT"
    assert html["tool_calls"][0]["name"] == "lookup"
    assert html["tool_calls"][0]["arguments"] == '{"x":1}'
    assert not html["stop_reason"]


@pytest.mark.parametrize("reverse", [False, True])
def test_failure_dominates_conflicting_success(reverse):
    events = [{"event": "failure", "call_id": "c", "ts": "2026-01-01T00:00:01+00:00", "status_code": 400,
               "error": {"message": "bad request"}},
              {"event": "success", "call_id": "c", "ts": "2026-01-01T00:00:02+00:00", "response": {}}]
    if reverse:
        events.reverse()
    query, html = views(events)
    assert query["state"] == html["state"] == "failed"
    assert query["status_code"] == 400
    assert query["ended_at"] == html["ts_end"] == "2026-01-01T00:00:01+00:00"
