from __future__ import annotations

from callbacks.trace_callback import _apply_thinking_policy
import copy
import pytest


@pytest.mark.parametrize("params", [{}, {"reasoning_effort": "high"}, {"thinking": {"type": "adaptive"}},
                                   {"extra_body": {"enable_thinking": True}}])
def test_observer_gateway_preserves_caller_params(monkeypatch, params):
    monkeypatch.setenv("LLM_GATEWAY_THINKING_POLICY", "passthrough")
    monkeypatch.delenv("UPSTREAM_PROTOCOL", raising=False)
    data = _openai_req(**params)
    original = copy.deepcopy(data)
    assert _apply_thinking_policy(data, "acompletion") == "passthrough"
    assert data.pop("metadata") == {"eval_thinking_policy": "passthrough"}
    assert data == original


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_openai_observer_preserves_thinking_in_wire_extra_body(monkeypatch, thinking):
    monkeypatch.setenv("LLM_GATEWAY_THINKING_POLICY", "passthrough")
    monkeypatch.setenv("UPSTREAM_PROTOCOL", "openai")
    data = _openai_req(thinking={"type": thinking}, reasoning_effort="max",
                       extra_body={"vendor_option": 7})
    _apply_thinking_policy(data, "completion")
    assert "thinking" not in data  # must not become an OpenAI SDK keyword
    assert data["extra_body"] == {"vendor_option": 7, "thinking": {"type": thinking}, "reasoning_effort": "max"}
    assert "reasoning_effort" not in data


def test_anthropic_observer_keeps_native_thinking(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_THINKING_POLICY", "passthrough")
    monkeypatch.setenv("UPSTREAM_PROTOCOL", "openai")
    data = {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    _apply_thinking_policy(data, "anthropic_messages")
    assert data["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "extra_body" not in data


def test_observer_thinking_reaches_openai_transport(monkeypatch):
    """Use real LiteLLM and SDK serialization, with an in-memory HTTP endpoint."""
    import asyncio
    import json
    import httpx
    import litellm
    from openai import AsyncOpenAI

    monkeypatch.setenv("LLM_GATEWAY_THINKING_POLICY", "passthrough")
    monkeypatch.setenv("UPSTREAM_PROTOCOL", "openai")
    received = []

    def endpoint(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "deepseek-fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        })

    async def run():
        async with AsyncOpenAI(api_key="synthetic", base_url="http://fixture.test/v1",
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint))) as client:
            for mode in ("enabled", "disabled"):
                data = _openai_req(thinking={"type": mode}, max_tokens=42)
                if mode == "enabled":
                    data["reasoning_effort"] = "max"
                _apply_thinking_policy(data, "completion")
                data.pop("metadata")
                data["model"] = "openai/deepseek-fixture"
                await litellm.acompletion(**data, client=client)

    asyncio.run(run())
    assert [r["thinking"] for r in received] == [{"type": "enabled"}, {"type": "disabled"}]
    assert received[0]["reasoning_effort"] == "max"
    assert "reasoning_effort" not in received[1]
    assert all(r["max_tokens"] == 42 for r in received)
    assert all("extra_body" not in r for r in received)


def _openai_req(**extra):
    return {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], **extra}


def test_openai_caller_on_anthropic_upstream_forces_thinking_disabled(monkeypatch):
    monkeypatch.setenv("UPSTREAM_PROTOCOL", "anthropic")
    data = _openai_req(allowed_openai_params=["tools"])
    assert _apply_thinking_policy(data, "acompletion") == "relay_default_off"
    assert data["thinking"] == {"type": "disabled"}
    assert data["allowed_openai_params"] == ["tools", "thinking"]
    assert data["extra_body"]["enable_thinking"] is False


def test_openai_caller_on_openai_upstream_unchanged(monkeypatch):
    monkeypatch.setenv("UPSTREAM_PROTOCOL", "openai")
    data = _openai_req()
    assert _apply_thinking_policy(data, "acompletion") == "relay_default_off"
    assert "thinking" not in data
    assert "allowed_openai_params" not in data
    assert data["extra_body"]["enable_thinking"] is False


def test_optin_header_keeps_thinking_on_anthropic_upstream(monkeypatch):
    monkeypatch.setenv("UPSTREAM_PROTOCOL", "anthropic")
    data = _openai_req(proxy_server_request={"headers": {"x-eval-thinking": "on"}})
    assert _apply_thinking_policy(data, "acompletion") == "optin"
    assert "thinking" not in data
