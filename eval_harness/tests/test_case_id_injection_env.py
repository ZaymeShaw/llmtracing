from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from eval_harness.adapters.claude import case_id_injection_env


def test_injects_header_and_body():
    env = case_id_injection_env("A01", {"PATH": "/bin", "ANTHROPIC_CUSTOM_HEADERS": "Foo: bar"})
    assert "X-Eval-Case-Id: A01" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert "Foo: bar" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    body = json.loads(env["CLAUDE_CODE_EXTRA_BODY"])
    assert body["eval_case_id"] == "A01"
    # Dual-id: execution_id is minted and sent on the wire header + body.
    assert env["EVAL_EXECUTION_ID"]
    assert len(env["EVAL_EXECUTION_ID"]) >= 16
    assert f"X-Eval-Execution-Id: {env['EVAL_EXECUTION_ID']}" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert body["eval_execution_id"] == env["EVAL_EXECUTION_ID"]
    assert body["metadata"]["eval_execution_id"] == env["EVAL_EXECUTION_ID"]


def test_replaces_prior_case_header_and_merges_extra_body():
    env = case_id_injection_env(
        "B02",
        {
            "ANTHROPIC_CUSTOM_HEADERS": "X-Eval-Case-Id: OLD\nX-Eval-Execution-Id: OLD_EXEC\nKeep: 1",
            "CLAUDE_CODE_EXTRA_BODY": '{"anthropic_beta":["x"]}',
        },
        execution_id="fixed-exec-b02",
    )
    lines = env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert lines.count("X-Eval-Case-Id: B02") == 1
    assert "X-Eval-Case-Id: OLD" not in lines
    assert "X-Eval-Execution-Id: OLD_EXEC" not in lines
    assert "X-Eval-Execution-Id: fixed-exec-b02" in lines
    assert "Keep: 1" in lines
    body = json.loads(env["CLAUDE_CODE_EXTRA_BODY"])
    assert body["eval_case_id"] == "B02"
    assert body["eval_execution_id"] == "fixed-exec-b02"
    assert body["anthropic_beta"] == ["x"]
    assert env["EVAL_EXECUTION_ID"] == "fixed-exec-b02"


def test_respects_passed_execution_id():
    env = case_id_injection_env("C03", {"PATH": "/bin"}, execution_id="fixed-exec-1")
    assert env["EVAL_EXECUTION_ID"] == "fixed-exec-1"
    assert "X-Eval-Execution-Id: fixed-exec-1" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()


def test_none_case_passthrough():
    base = {"FOO": "1"}
    env = case_id_injection_env(None, base)
    assert env == base
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env
    assert "EVAL_EXECUTION_ID" not in env


if __name__ == "__main__":
    test_injects_header_and_body()
    test_replaces_prior_case_header_and_merges_extra_body()
    test_respects_passed_execution_id()
    test_none_case_passthrough()
    print("ALL_PASS")
