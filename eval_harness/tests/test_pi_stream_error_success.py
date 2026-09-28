"""Pi adapter: stream errorMessage must force success=False."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval_harness" / "src"))

from eval_harness.adapters import pi as pi_adapter
from eval_harness.adapters.base import TurnResult


def _err_stream_events():
    err = "API key auth failed for provider local-relay-bailian: boom"
    return [
        {"type": "session", "id": "sess-1"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": err,
            },
        },
        {"type": "agent_end", "messages": [
            {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": err},
        ]},
    ]


def test_extract_stream_error_from_message():
    assert "auth failed" in (pi_adapter._extract_stream_error(_err_stream_events()) or "")


def test_pi_events_to_claude_like_marks_result_is_error():
    mapped = pi_adapter.pi_events_to_claude_like(_err_stream_events())
    result = [e for e in mapped if e.get("type") == "result"][-1]
    assert result["is_error"] is True
    assert "auth failed" in result["result"]


def test_run_case_success_false_when_stream_has_error_message(tmp_path: Path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    err = "API key auth failed for provider local-relay-bailian: boom"
    mapped = pi_adapter.pi_events_to_claude_like(_err_stream_events())

    fake = TurnResult(
        index=1,
        prompt="hi",
        exit_code=0,
        stream_path=case_dir / "stream_turn1.jsonl",
        session_id="sess-1",
        final_text="",
        wall_ms=10,
        first_frame_ms=None,
        first_frame_kind=None,
        raw_events=mapped,
        error=err,
        cost_usd=None,
        api_ms=None,
    )
    (case_dir / "stream_turn1.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _err_stream_events()) + "\n", encoding="utf-8"
    )

    with patch.object(pi_adapter, "run_turn", return_value=fake):
        result = pi_adapter.run_case(
            case_id="PI_ERR",
            turns=["hi"],
            case_dir=case_dir,
            project_cwd=tmp_path,
            timeout_sec=5,
        )
    assert result.success is False
    assert result.error and "auth failed" in result.error


def test_run_case_success_true_without_stream_error(tmp_path: Path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    mapped = [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}]}},
        {"type": "result", "session_id": "s", "is_error": False, "result": "好"},
    ]
    fake = TurnResult(
        index=1,
        prompt="hi",
        exit_code=0,
        stream_path=case_dir / "stream_turn1.jsonl",
        session_id="s",
        final_text="好",
        wall_ms=10,
        first_frame_ms=1,
        first_frame_kind="text",
        raw_events=mapped,
        error=None,
        cost_usd=None,
        api_ms=None,
    )
    (case_dir / "stream_turn1.jsonl").write_text("{}\n", encoding="utf-8")
    with patch.object(pi_adapter, "run_turn", return_value=fake):
        result = pi_adapter.run_case(
            case_id="PI_OK",
            turns=["hi"],
            case_dir=case_dir,
            project_cwd=tmp_path,
            timeout_sec=5,
        )
    assert result.success is True
    assert result.error is None
