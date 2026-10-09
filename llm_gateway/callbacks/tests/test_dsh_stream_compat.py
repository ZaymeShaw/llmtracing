"""Proxy SSE serialization must retain tool identity across argument deltas."""
import asyncio
import json

from litellm.types.utils import ModelResponseStream
from callbacks.trace_callback import EvalTraceLogger


def chunk(tool):
    return ModelResponseStream(id="request", model="fixture", choices=[{
        "index": 0, "delta": {"tool_calls": [tool]}, "finish_reason": None,
    }])


def test_dsh_stream_identity_survives_empty_continuations(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_OMIT_EMPTY_TOOL_DELTAS", "1")
    original = [
        chunk({"index": 0, "id": "call-a", "type": "function", "function": {"name": "bash", "arguments": ""}}),
        chunk({"index": 1, "id": "call-b", "type": "function", "function": {"name": "read", "arguments": ""}}),
        chunk({"index": 0, "id": "", "function": {"name": "", "arguments": '{"command":'}}),
        chunk({"index": 1, "id": "", "function": {"name": "", "arguments": "{}"}}),
        chunk({"index": 0, "id": "", "function": {"name": "", "arguments": '"pwd"}'}}),
    ]

    async def run():
        async def stream():
            for row in original:
                yield row
        return [json.loads(row.model_dump_json(exclude_none=True, exclude_unset=True))
                async for row in EvalTraceLogger().async_post_call_streaming_iterator_hook(None, stream(), {})]

    wire = asyncio.run(run())
    tools = {}
    for row in wire:
        for delta in row["choices"][0]["delta"]["tool_calls"]:
            state = tools.setdefault(delta["index"], {"arguments": ""})
            if "id" in delta:
                state["id"] = delta["id"]
            if "name" in delta["function"]:
                state["name"] = delta["function"]["name"]
            state["arguments"] += delta["function"].get("arguments", "")
    assert tools == {0: {"id": "call-a", "name": "bash", "arguments": '{"command":"pwd"}'},
                     1: {"id": "call-b", "name": "read", "arguments": "{}"}}
    assert original[2].choices[0].delta.tool_calls[0].id == ""
    assert original[2].choices[0].delta.tool_calls[0].function.name == ""


def test_other_gateways_keep_original_stream(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_OMIT_EMPTY_TOOL_DELTAS", raising=False)
    original = chunk({"index": 0, "id": "", "function": {"name": "", "arguments": "{}"}})

    async def run():
        async def stream():
            yield original
        return [c async for c in EvalTraceLogger().async_post_call_streaming_iterator_hook(None, stream(), {})]

    assert asyncio.run(run())[0] is original
