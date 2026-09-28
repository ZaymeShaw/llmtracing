from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from eval_harness.adapters.pi import case_id_injection_env


def test_case_id_injection_mints_execution_id():
    env = case_id_injection_env("PI_A01", {"PATH": "/bin"})
    assert env["PI_EVAL_CASE_ID"] == "PI_A01"
    assert env["PI_EVAL_EXECUTION_ID"]
    assert len(env["PI_EVAL_EXECUTION_ID"]) >= 16


def test_case_id_injection_respects_passed_execution_id():
    env = case_id_injection_env("PI_A01", {"PATH": "/bin"}, execution_id="fixed-exec-1")
    assert env["PI_EVAL_EXECUTION_ID"] == "fixed-exec-1"


def test_none_case_skips_execution_id():
    env = case_id_injection_env(None, {"PATH": "/bin"})
    assert "PI_EVAL_CASE_ID" not in env
    assert "PI_EVAL_EXECUTION_ID" not in env


def test_pi_models_json_forwards_execution_id():
    """Outside-repo Pi models.json must map $PI_EVAL_EXECUTION_ID → header for local-relay*."""
    path = Path.home() / ".pi" / "agent" / "models.json"
    if not path.is_file():
        import pytest
        pytest.skip("Pi models.json not present on this machine")
    raw = path.read_text(encoding="utf-8")
    # models.json may contain // comments; check required wire lines literally.
    assert '"X-Eval-Execution-Id": "$PI_EVAL_EXECUTION_ID"' in raw
    assert '"X-Eval-Case-Id": "$PI_EVAL_CASE_ID"' in raw
    # Both local-relay profiles must appear (bailian uses :4002).
    assert "local-relay" in raw
    assert "local-relay-bailian" in raw
