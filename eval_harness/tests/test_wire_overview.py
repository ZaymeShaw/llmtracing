"""Regressions: preserve user turns and measured streaming timing without adapters."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "llm_gateway"))
from callbacks import trace_callback
from eval_harness.llm_trace_html import _normalize_call, build_payload, materialize_wire_run
from eval_harness.wire_turns import observed_turns


def call(users, answer, **extra):
    return dict(messages=[dict(role="user", content=u) for u in users], assistant=answer, **extra)


def test_title_tool_loop_repeated_prompt_and_failed_turn():
    calls = [call(["Generate title", "Q"], "Title", call_id="title"),
             call(["Q"], "working", call_id="1"),
             call(["Q"], "answer 1", call_id="2"),
             call(["Q", "Q"], "answer 2", call_id="3"),
             call(["Q", "Q", "failed question"], "", call_id="4")]
    turns, main, others = observed_turns(calls)
    assert [t["prompt"] for t in turns] == ["Q", "Q", "failed question"]
    assert [t["final_text"] for t in turns] == ["answer 1", "answer 2", ""]
    assert others == 1 and len(main) == 4 and len(calls) == 5


def test_anthropic_tool_result_is_not_user_turn():
    first = call(["Q"], "")
    second = call(["Q", [{"type": "tool_result", "tool_use_id": "t", "content": "result"}]], "answer")
    third = call(["Q", [{"type": "tool_result", "tool_use_id": "t", "content": "result"}],
                  [{"type": "text", "text": "Q2"}]], "answer 2")
    turns, _, others = observed_turns([first, second, third])
    assert [t["prompt"] for t in turns] == ["Q", "Q2"]
    assert [t["final_text"] for t in turns] == ["answer", "answer 2"]
    assert others == 0


def test_responses_incremental_history():
    raw = [
        {"request": {"input": "Q"}, "response": {"id": "r1", "output": []}},
        {"request": {"previous_response_id": "r1", "input": [
            {"type": "function_call_output", "call_id": "t", "output": "result"}]},
         "response": {"id": "r2", "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}]}},
        {"request": {"previous_response_id": "r2", "input": "Q"},
         "response": {"id": "r3", "output": [{"type": "message", "content": [{"type": "output_text", "text": "again"}]}]}},
    ]
    turns, _, others = observed_turns([_normalize_call(c) for c in raw])
    assert [(t["prompt"], t["final_text"]) for t in turns] == [("Q", "answer"), ("Q", "again")]
    assert others == 0


@pytest.mark.parametrize("chunk,kind", [
    ({"choices": [{"delta": {"content": "hello"}}]}, "text"),
    ({"choices": [{"delta": {"reasoning_content": "think"}}]}, "thinking"),
    ({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}, "text"),
    ({"type": "response.output_text.delta", "delta": "hello"}, "text"),
])
def test_first_frame_roundtrip_and_case_isolation(tmp_path, monkeypatch, chunk, kind):
    log = tmp_path / "gateway.jsonl"
    monkeypatch.setattr(trace_callback, "LOG_FILE", log)
    logger = trace_callback.EvalTraceLogger()
    pending = []
    for cid in ("A", "B"):
        kwargs = {"litellm_call_id": cid, "optional_params": {"stream": True}, "litellm_params": {
            "proxy_server_request": {"headers": {"X-Eval-Run-Id": "batch", "X-Eval-Case-Id": cid,
                "X-Eval-Execution-Id": cid}, "body": {"messages": [{"role": "user", "content": cid}]}}}}
        logger.log_pre_api_call("model", [], kwargs)
        kwargs["_eval_trace_t0"] = 1000
        pending.append(kwargs)
    for kwargs, delay in zip(reversed(pending), (0.8, 0.2)):
        def emit(body, offset):
            asyncio.run(logger.async_log_stream_event(kwargs, body, None, datetime.fromtimestamp(1000 + offset, timezone.utc)))
        emit({"choices": [{"delta": {"role": "assistant"}}]}, 0.1)
        emit({"choices": [{"delta": {"tool_calls": [{"id": "t", "function": {"arguments": "{}"}}]}}]}, 0.12)
        emit({"type": "content_block_start", "content_block": {"type": "tool_use", "id": "t"}}, 0.13)
        emit({"choices": [{"delta": {"content": " \n"}}]}, 0.14)
        emit(chunk, delay)
        emit(chunk, 1.5)
        # Failure after first content must keep the measured timing.
        asyncio.run(logger.async_log_failure_event(kwargs, {"error": "interrupted"}, None, None))
    materialize_wire_run(gateway_log=log, out_run_dir=tmp_path / "report")
    cases = build_payload(tmp_path / "report")["cases"]
    timings = {c["identity"]["case_id"]: c["overview"]["metrics"]["first_frame_ms"] for c in cases}
    assert timings == {"A": 200, "B": 800}
    assert all(c["overview"]["metrics"]["first_frame_kind"] == kind for c in cases)
    assert sum(json.loads(l)["event"] == "first_frame" for l in log.read_text().splitlines()) == 2


def test_no_fabricated_first_frame_from_response_latency(tmp_path):
    case = tmp_path / "cases/A"
    case.mkdir(parents=True)
    (case / "llm_calls.jsonl").write_text(json.dumps({
        "call_id": "c", "ts": "2026-09-29T00:00:00Z", "latency_ms": 9999,
        "request": {"messages": [{"role": "user", "content": "Q"}]},
        "response": {"choices": [{"message": {"content": "answer"}}]},
    }) + "\n")
    overview = build_payload(tmp_path)["cases"][0]["overview"]
    assert overview["metrics"]["first_frame_ms"] is None
    assert overview["turns"][0]["final_text"] == "answer"


def test_proxy_stream_iterator_preserves_chunks_and_observes_arrival(tmp_path, monkeypatch):
    from types import SimpleNamespace
    log = tmp_path / "gateway.jsonl"
    monkeypatch.setattr(trace_callback, "LOG_FILE", log)
    logger = trace_callback.EvalTraceLogger()
    kwargs = {"litellm_call_id": "stream", "optional_params": {}, "litellm_params": {}}
    logger.log_pre_api_call("model", [], kwargs)
    chunks = [{"choices": [{"delta": {"role": "assistant"}}]},
              {"choices": [{"delta": {"content": "hello"}}]},
              {"choices": [{"delta": {"content": "world"}}]}]
    async def source():
        for chunk in chunks:
            yield chunk
    async def run():
        return [chunk async for chunk in logger.async_post_call_streaming_iterator_hook(
            None, source(), {"litellm_logging_obj": SimpleNamespace(model_call_details=kwargs)})]
    assert asyncio.run(run()) == chunks
    events = [json.loads(line) for line in log.read_text().splitlines()]
    frame, = [e for e in events if e["event"] == "first_frame"]
    assert frame["first_frame_ms"] >= 0 and frame["first_frame_kind"] == "text"


def test_case_first_character_includes_tool_roundtrip_and_rejects_legacy_timing(tmp_path):
    from eval_harness.llm_trace_html import _infer_first_frame
    calls = [dict(call_id="tool", ts_start="2026-09-29T00:00:00Z", assistant="", thinking="",
                  first_frame_ms=100, first_frame_kind="tool_use"),
             dict(call_id="answer", ts_start="2026-09-29T00:00:02Z", assistant="hello",
                  first_frame_ms=450, first_frame_kind="text")]
    assert _infer_first_frame(tmp_path, calls, {}) == (2450, "text")
    assert _infer_first_frame(tmp_path, calls[:1], {}) == (None, None)
    # The old collector stopped at a tool delta even if text arrived later.
    calls[0]["assistant"] = "historical text without its timestamp"
    assert _infer_first_frame(tmp_path, calls, {}) == (None, None)
    assert _infer_first_frame(tmp_path, [], {"metrics": {
        "first_frame_ms": 100, "first_frame_kind": "tool_use"}}) == (None, None)


@pytest.mark.parametrize("kind", ["text", "thinking"])
def test_cli_first_character_skips_tools_and_empty_blocks(kind):
    from eval_harness.adapters.claude import _detect_first_frame as claude
    from eval_harness.adapters.pi import _detect_first_frame as pi
    claude_events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "t"}}},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text", "text": ""}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": kind + "_delta", kind: "字"}}},
    ]
    pi_events = [{"type": "tool_execution_start"},
                 {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": " "}},
                 {"type": "message_update", "assistantMessageEvent": {"type": kind + "_delta", "delta": "字"}}]
    for detect, events in ((claude, claude_events), (pi, pi_events)):
        assert detect(events, 1000, [1000.1, 1000.2, 1000.7]) == (700, kind)
        assert detect(events[:2], 1000, [1000.1, 1000.2]) == (None, None)
