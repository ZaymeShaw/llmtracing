"""External-consumer query contract: state, settle, cursor, rotation, window, redaction, JSON envelope."""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval_harness" / "src"))

from eval_harness import query as q


def _append(path: Path, *rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def _pre(cid, ex="e1", case="C1", ts="2026-01-01T00:00:01+00:00", **extra):
    return {"event": "pre_api_call", "call_id": cid, "execution_id": ex, "case_id": case, "run_id": "r1",
            "ts": ts, "model": "m", "protocol": "chat_completions", **extra}


def _ok(cid, ts="2026-01-01T00:00:02+00:00"):
    return {"event": "success", "call_id": cid, "ts": ts, "latency_ms": 5,
            "usage": {"prompt_tokens": 3, "completion_tokens": 1}, "response": {"choices": []}}


def _fail(cid):
    return {"event": "failure", "call_id": cid, "ts": "2026-01-01T00:00:03+00:00", "error": "upstream 500"}


def test_states_counts_conflicts_and_exit_codes(tmp_path: Path, capsys):
    log = tmp_path / "llm_calls.jsonl"
    _append(log, _pre("a"), _ok("a"), _pre("b"), _fail("b"), _pre("c"),
            _pre("d", case="OTHER"), _ok("d"))
    env = q.collect(execution_ids=["e1"], log=log)
    assert env["schema"] == "llmtrace.calls/v1" and env["status"] == "partial"
    assert env["counts"] == {"total": 4, "pending": 1, "succeeded": 2, "failed": 1}
    assert env["pending_call_ids"] == ["c"]
    assert env["conflicts"] == [{"execution_id": "e1", "case_ids": ["C1", "OTHER"]}]
    a = next(c for c in env["calls"] if c["call_id"] == "a")
    assert a["state"] == "succeeded" and a["usage"]["prompt_tokens"] == 3 and a["ended_at"].endswith("02+00:00")
    assert "events" not in a
    assert q.main(["calls", "--execution-id", "e1", "--log", str(log), "--format", "json"]) == 3
    assert q.main(["calls", "--execution-id", "nope", "--log", str(log), "--format", "json"]) == 1
    capsys.readouterr()
    assert q.main(["calls", "--call-id", "a", "--log", str(log), "--format", "json", "--events"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["event"] for e in out["calls"][0]["events"]] == ["pre_api_call", "success"]
    assert q.main(["calls", "--log", str(log), "--format", "json"]) == 2  # no selector
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_thin_proxy_http_error_is_failed(tmp_path: Path):
    log = tmp_path / "llm_calls.jsonl"
    _append(log, {"event": "request", "call_id": "t", "execution_id": "e1", "ts": "2026-01-01T00:00:01+00:00",
                  "request": {"model": "m"}},
            {"event": "response", "call_id": "t", "status_code": 429, "response": {"error": "rate"}})
    assert q.collect(execution_ids=["e1"], log=log)["calls"][0]["state"] == "failed"


def test_trace_id_matches_execution_query(tmp_path: Path):
    log = tmp_path / "llm_calls.jsonl"
    _append(log, _pre("x", ex="lane-exec", trace_id="8f385dd2b3a2468aa9161121eae9c688"), _ok("x"))
    assert [p["call_id"] for p in q.query_calls(execution_ids=["8f385dd2b3a2468aa9161121eae9c688"], log=log)] == ["x"]


def test_settle_waits_for_pending_then_quiet(tmp_path: Path):
    log = tmp_path / "llm_calls.jsonl"
    _append(log, _pre("a"))
    scanner = q.open_scanner(execution_ids=["e1"], log=log)
    now = [0.0]
    steps = iter([lambda: _append(log, _ok("a")), lambda: None, lambda: None, lambda: None, lambda: None])

    def sleep(dt):
        now[0] += dt
        next(steps, lambda: None)()

    assert q.settle(scanner, timeout=10, quiet=1.0, interval=0.5, clock=lambda: now[0], sleep=sleep) == "complete"
    assert 1.0 <= now[0] <= 2.0  # ended at 0.5s, then 1s quiet

    _append(log, _pre("b"))
    scanner = q.open_scanner(execution_ids=["e1"], log=log)
    now[0] = 0.0
    assert q.settle(scanner, timeout=3, quiet=1.0, clock=lambda: now[0],
                    sleep=lambda dt: now.__setitem__(0, now[0] + dt)) == "partial"
    empty = q.open_scanner(execution_ids=["none"], log=log)
    assert q.settle(empty, timeout=1, clock=lambda: now[0], sleep=lambda dt: now.__setitem__(0, now[0] + dt)) == "no_calls"


def test_partial_line_is_left_for_next_poll(tmp_path: Path):
    log = tmp_path / "llm_calls.jsonl"
    _append(log, _pre("a"))
    with log.open("a") as f:
        f.write(json.dumps(_ok("a"))[:20])  # record being written
    scanner = q.open_scanner(call_ids=["a"], log=log)
    assert q.call_state(scanner.events()["a"]) == "pending"
    with log.open("a") as f:
        f.write(json.dumps(_ok("a"))[20:] + "\n")
    assert scanner.poll() and q.call_state(scanner.events()["a"]) == "succeeded"


def test_cursor_reads_only_new_and_survives_rotation(tmp_path: Path, capsys):
    logs = tmp_path / "logs"
    live = logs / "gateways" / "g" / "llm_calls.jsonl"
    _append(live, _pre("old", ex="e1"), _ok("old"))
    cursor = q.snapshot_cursor(log=logs)
    assert list(cursor["files"].values())[0]["offset"] == live.stat().st_size
    _append(live, _pre("new", ex="e1"), _ok("new"))
    assert [p["call_id"] for p in q.query_calls(execution_ids=["e1"], log=logs, cursor=cursor)] == ["new"]

    # Cursor round-trips through the CLI (file or a previous envelope).
    cfile = tmp_path / "c.json"
    cfile.write_text(json.dumps({"cursor": cursor}))
    assert q.main(["calls", "--execution-id", "e1", "--log", str(logs), "--cursor", str(cfile), "--format", "json"]) == 0
    assert [c["call_id"] for c in json.loads(capsys.readouterr().out)["calls"]] == ["new"]
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert q.main(["calls", "--execution-id", "e1", "--log", str(logs), "--cursor", str(bad)]) == 2

    # Rotate after the cursor: the tail now lives in the archive, the live file is new.
    _append(live, _pre("tail", ex="e1"))
    archive = live.with_name("llm_calls_20260101_000000.jsonl.gz")
    with live.open("rb") as src, gzip.open(archive, "wb") as dst:
        dst.write(src.read())
    live.unlink()
    _append(live, _ok("tail"), _pre("after", ex="e1"), _ok("after"))
    env = q.collect(execution_ids=["e1"], log=logs, cursor=cursor)
    assert env["rotated"] is True
    got = {c["call_id"]: c["state"] for c in env["calls"]}
    assert got["tail"] == "succeeded" and got["after"] == "succeeded" and "new" in got


def test_rotation_during_settle_is_joined(tmp_path: Path):
    logs = tmp_path / "logs"
    live = logs / "llm_calls.jsonl"
    _append(live, _pre("a"))
    scanner = q.open_scanner(execution_ids=["e1"], log=logs)
    staged = live.with_name(".llm_calls_20260101_000000.jsonl.rotating")
    os.rename(live, staged)
    _append(staged, _ok("a"))  # late write through the old descriptor
    _append(live, _pre("b"), _ok("b"))
    scanner.poll()
    assert scanner.rotated
    assert {p["call_id"]: q.call_state(scanner.events()[p["call_id"]]) for p in scanner.pairs()} == \
        {"a": "succeeded", "b": "succeeded"}


def test_time_window_without_ids(tmp_path: Path, capsys):
    log = tmp_path / "llm_calls.jsonl"
    _append(log, _pre("early", ts="2026-01-01T00:00:00+00:00"), _ok("early", ts="2026-01-01T00:10:00+00:00"),
            _pre("mid", ts="2026-01-01T00:05:00+00:00"), _ok("mid"),
            _pre("late", ts="2026-01-01T01:00:00+00:00"))
    got = q.query_calls(since="2026-01-01T00:01:00+00:00", until="2026-01-01T00:30:00Z", log=log)
    assert [p["call_id"] for p in got] == ["mid"]
    assert [p["call_id"] for p in q.query_calls(case_ids=["C1"], since="1767225900", log=log)] == ["mid", "late"]
    with pytest.raises(ValueError):
        q.parse_when("yesterday")
    assert q.parse_when("10m") < q.parse_when("1m")


def test_output_is_redacted(tmp_path: Path, capsys):
    log = tmp_path / "llm_calls.jsonl"
    secret = "sk-" + "a" * 24
    _append(log, _pre("a", client_request={"headers": {"Authorization": "Bearer " + "t" * 20},
                                           "api_key": secret, "max_tokens": 9, "note": f"key={secret}"}), _ok("a"))
    for fmt in ("pairs", "json"):
        args = ["calls", "--call-id", "a", "--log", str(log), "--format", fmt] + (["--events"] if fmt == "json" else [])
        q.main(args)
        out = capsys.readouterr().out
        assert secret not in out and "t" * 20 not in out and '"max_tokens": 9' in out, fmt


def test_include_rotated_joins_call_split_by_rotation(tmp_path: Path):
    d = tmp_path / "logs"
    d.mkdir()
    with gzip.open(d / "llm_calls_20260101_000000.jsonl.gz", "wt") as f:
        f.write(json.dumps(_pre("split")) + "\n")
    _append(d / "llm_calls.jsonl", _ok("split"))
    env = q.collect(execution_ids=["e1"], log=d, include_rotated=True)
    assert [(c["call_id"], c["state"]) for c in env["calls"]] == [("split", "succeeded")]


def test_verifier_labels_survive_query_and_export(tmp_path):
    from eval_harness.llm_gateway_ingest import pairs_to_trace_calls
    labels = dict(project_id='llm_probe', role='judge', stage='assessment', verifier_trace_id='business-ref', harness='verifier')
    log = tmp_path / 'llm_calls.jsonl'
    _append(log, _pre('v', **labels), _ok('v'))
    call = q.collect(execution_ids=['e1'], log=log)['calls'][0]
    assert all(call[k] == v for k, v in labels.items())
    pairs = q.query_calls(execution_ids=['e1'], log=log)
    exported = pairs_to_trace_calls(pairs, case_id='C1')[0]
    assert all(exported[k] == v for k, v in labels.items())
