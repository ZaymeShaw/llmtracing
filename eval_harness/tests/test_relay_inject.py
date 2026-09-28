from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from eval_harness.relay_inject import (
    anthropic_cli_env,
    apply_openai_compatible_case_id,
    openai_compatible_kwargs,
    text_marker,
    THINKING_HEADER,
)


def test_openai_kwargs():
    kw = openai_compatible_kwargs("A01")
    assert kw["extra_headers"]["X-Eval-Case-Id"] == "A01"
    assert kw["extra_body"]["eval_case_id"] == "A01"
    assert "X-Eval-Execution-Id" not in kw["extra_headers"]


def test_openai_kwargs_with_execution_id():
    kw = openai_compatible_kwargs("A01", execution_id="exec-9")
    assert kw["extra_headers"]["X-Eval-Execution-Id"] == "exec-9"
    assert kw["extra_body"]["eval_execution_id"] == "exec-9"
    assert kw["extra_body"]["metadata"]["eval_execution_id"] == "exec-9"


def test_apply_mutates_model():
    m = SimpleNamespace(extra_headers={"A": "1"}, extra_body={"k": 2}, default_headers={})
    apply_openai_compatible_case_id(m, "B02")
    assert m.extra_headers["X-Eval-Case-Id"] == "B02"
    assert m.extra_headers["A"] == "1"
    assert m.extra_body == {"k": 2, "eval_case_id": "B02"}
    assert m.default_headers["X-Eval-Case-Id"] == "B02"


def test_apply_mutates_model_with_execution_id():
    m = SimpleNamespace(extra_headers={}, extra_body={}, default_headers={})
    apply_openai_compatible_case_id(m, "B02", execution_id="exec-b")
    assert m.extra_headers["X-Eval-Execution-Id"] == "exec-b"
    assert m.extra_body["eval_execution_id"] == "exec-b"
    assert m.default_headers["X-Eval-Execution-Id"] == "exec-b"


def test_anthropic_env_and_marker():
    env = anthropic_cli_env("C03", {"PATH": "/bin"})
    assert "X-Eval-Case-Id: C03" in env["ANTHROPIC_CUSTOM_HEADERS"]
    assert json.loads(env["CLAUDE_CODE_EXTRA_BODY"])["eval_case_id"] == "C03"
    assert "C03" in text_marker("C03")
    # No execution_id unless passed
    assert "X-Eval-Execution-Id:" not in env["ANTHROPIC_CUSTOM_HEADERS"]


def test_anthropic_env_with_execution_id():
    env = anthropic_cli_env("C03", {"PATH": "/bin"}, execution_id="exec-c03")
    lines = env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert "X-Eval-Case-Id: C03" in lines
    assert "X-Eval-Execution-Id: exec-c03" in lines
    body = json.loads(env["CLAUDE_CODE_EXTRA_BODY"])
    assert body["eval_execution_id"] == "exec-c03"
    assert body["metadata"]["eval_execution_id"] == "exec-c03"




def test_anthropic_env_thinking_on():
    env = anthropic_cli_env("C03", {"PATH": "/bin"}, execution_id="e1", thinking=True)
    assert f"{THINKING_HEADER}: on" in env["ANTHROPIC_CUSTOM_HEADERS"]


def test_anthropic_env_thinking_default_omits_header():
    env = anthropic_cli_env("C03", {"PATH": "/bin"}, execution_id="e1")
    assert THINKING_HEADER not in env["ANTHROPIC_CUSTOM_HEADERS"]


def test_openai_kwargs_thinking_on():
    kw = openai_compatible_kwargs("A01", execution_id="e", thinking=True)
    assert kw["extra_headers"][THINKING_HEADER] == "on"
    assert kw["extra_body"]["enable_thinking"] is True

if __name__ == "__main__":
    test_openai_kwargs()
    test_openai_kwargs_with_execution_id()
    test_apply_mutates_model()
    test_apply_mutates_model_with_execution_id()
    test_anthropic_env_and_marker()
    test_anthropic_env_with_execution_id()
    test_anthropic_env_thinking_on()
    test_anthropic_env_thinking_default_omits_header()
    test_openai_kwargs_thinking_on()
    print("ALL_PASS")
