"""Unit tests for thinking-at-relay policy (P1)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "llm_gateway"))
sys.path.insert(0, str(ROOT / "eval_harness" / "src"))

from callbacks import trace_callback
from eval_harness.adapters import claude as claude_adapter
from eval_harness.adapters import pi as pi_adapter


def test_apply_thinking_policy_anthropic_default_off():
    data = {
        "model": "deepseek-v4-flash-0731",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
        "thinking": {"type": "adaptive"},
        "proxy_server_request": {"headers": {}},
        "metadata": {},
    }
    policy = trace_callback._apply_thinking_policy(data, "text_completion")
    assert policy == "relay_forced_off"
    assert data["thinking"] == {"type": "disabled"}
    assert data["metadata"]["eval_thinking_policy"] == "relay_forced_off"


def test_apply_thinking_policy_anthropic_optin_header():
    data = {
        "model": "deepseek-v4-flash-0731",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
        "thinking": {"type": "adaptive"},
        "proxy_server_request": {"headers": {"X-Eval-Thinking": "on"}},
        "metadata": {},
    }
    policy = trace_callback._apply_thinking_policy(data, "text_completion")
    assert policy == "optin"
    assert data["thinking"] == {"type": "adaptive"}


def test_apply_thinking_policy_openai_default_off():
    data = {
        "model": "deepseek-v4-flash-0731",
        "messages": [{"role": "user", "content": "hi"}],
        "proxy_server_request": {"headers": {}},
        "metadata": {},
    }
    policy = trace_callback._apply_thinking_policy(data, "completion")
    assert policy == "relay_default_off"
    assert data["extra_body"]["enable_thinking"] is False


def test_apply_thinking_policy_openai_optin_and_think_alias():
    data = {
        "model": "deepseek-v4-flash-0731-think",
        "messages": [{"role": "user", "content": "hi"}],
        "proxy_server_request": {"headers": {}},
        "metadata": {},
    }
    policy = trace_callback._apply_thinking_policy(data, "completion")
    assert policy == "optin"
    assert data["model"] == "deepseek-v4-flash-0731"
    assert data["enable_thinking"] is True
    assert data["extra_body"]["enable_thinking"] is True


def test_apply_thinking_policy_openai_caller_keeps_false():
    """Insurance upstream enable_thinking:false must remain caller, not confused with relay."""
    data = {
        "model": "deepseek-v4-flash-0731",
        "messages": [{"role": "user", "content": "hi"}],
        "enable_thinking": False,
        "extra_body": {"enable_thinking": False},
        "proxy_server_request": {"headers": {}},
        "metadata": {},
    }
    policy = trace_callback._apply_thinking_policy(data, "completion")
    assert policy == "caller"
    assert data["enable_thinking"] is False


def test_pre_call_hook_and_log_fields(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setattr(trace_callback, "LOG_FILE", log)
    monkeypatch.delenv("LLM_ATTRIBUTION_CONFIG", raising=False)
    cb = trace_callback.EvalTraceLogger()
    data = {
        "model": "deepseek-v4-flash-0731",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32,
        "thinking": {"type": "adaptive"},
        "proxy_server_request": {
            "headers": {"X-Eval-Case-Id": "SMOKE_T", "X-Eval-Execution-Id": "exec1"},
            "body": {},
            "url": "/v1/messages",
        },
        "metadata": {},
    }
    asyncio.run(cb.async_pre_call_hook(None, None, data, "text_completion"))
    assert data["thinking"]["type"] == "disabled"
    kwargs = {
        "litellm_call_id": "c1",
        "optional_params": {"thinking": data["thinking"]},
        "litellm_params": {
            "proxy_server_request": data["proxy_server_request"],
            "metadata": data["metadata"],
        },
        "metadata": data["metadata"],
    }
    cb.log_pre_api_call(data["model"], data["messages"], kwargs)
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["thinking_effective"] == "off"
    assert rec["thinking_source"] == "relay_forced_off"
    assert rec["case_id"] == "SMOKE_T"
    assert rec["execution_id"] == "exec1"


def test_claude_injection_thinking_on():
    env = claude_adapter.case_id_injection_env("A01", {"PATH": "/bin"}, thinking=True)
    assert "X-Eval-Thinking: on" in env["ANTHROPIC_CUSTOM_HEADERS"]


def test_pi_injection_thinking_on_sets_env():
    env = pi_adapter.case_id_injection_env("PI_A01", {"PATH": "/bin"}, thinking="high")
    assert env.get("PI_EVAL_THINKING") == "on"
    assert env.get("PI_EVAL_CASE_ID") == "PI_A01"
    assert env.get("PI_EVAL_EXECUTION_ID")


def test_pi_injection_thinking_off_sets_off():
    env = pi_adapter.case_id_injection_env("PI_A01", {"PATH": "/bin"}, thinking="off")
    assert env.get("PI_EVAL_THINKING") == "off"
