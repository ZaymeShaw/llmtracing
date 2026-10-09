"""P3: query captured calls by id, and rotate a gateway's call log."""
from __future__ import annotations

import fcntl
import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval_harness" / "src"))

from eval_harness.gateways import gatewayctl
from eval_harness.query import main as query_main
from eval_harness.query import query_calls
from test_multi_lane import _gateway_dir


def _rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "at", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _call(cid: str, ex: str, case: str, run: str, ts: str, gateway: str = "g") -> list[dict]:
    # success rows carry no ids: the query must join them by call_id.
    return [{"event": "pre_api_call", "call_id": cid, "execution_id": ex, "case_id": case, "run_id": run,
             "gateway": gateway, "ts": ts, "model": "m", "protocol": "chat_completions"},
            {"event": "success", "call_id": cid, "latency_ms": 5, "response": {"choices": []}}]


def test_query_filters_joins_and_orders(tmp_path: Path, capsys):
    logs = tmp_path / "logs"
    _rows(logs / "gateways" / "b" / "llm_calls.jsonl", _call("c2", "e1", "C1", "r1", "2026-01-01T00:00:02", "b"))
    _rows(logs / "gateways" / "a" / "llm_calls.jsonl",
          _call("c1", "e1", "C1", "r1", "2026-01-01T00:00:01", "a") + _call("c3", "e2", "C2", "r1", "2026-01-01T00:00:03", "a"))
    _rows(logs / "llm_calls.jsonl", _call("c9", "e9", "C9", "r2", "2026-01-01T00:00:00") + ["not json"])
    _rows(logs / "gateways" / "a" / "llm_calls_20260101_000000.jsonl.gz", _call("c0", "e1", "C1", "r1", "2025-12-31T00:00:00", "a"))

    got = query_calls(execution_ids=["e1"], log=logs)
    assert [p["call_id"] for p in got] == ["c1", "c2"]
    assert all(p["status_code"] == 200 and p["latency_ms"] == 5 for p in got)  # success row joined
    assert [p["call_id"] for p in query_calls(execution_ids=["e1"], log=logs, include_rotated=True)] == ["c0", "c1", "c2"]
    assert [p["call_id"] for p in query_calls(run_ids=["r1"], case_ids=["C2"], log=logs)] == ["c3"]
    # lane leases record "<eval_runs_dir>/<run_id>"; the bare run id must still match
    _rows(logs / "gateways" / "c" / "llm_calls.jsonl", _call("c7", "e7", "C7", "eval_runs/r7", "2026-01-01T00:00:07", "c"))
    assert [p["call_id"] for p in query_calls(run_ids=["r7"], log=logs)] == ["c7"]
    assert [p["call_id"] for p in query_calls(run_ids=["eval_runs/r7"], log=logs)] == ["c7"]
    assert query_calls(run_ids=["s/r7"], log=logs) == []
    assert [p["call_id"] for p in query_calls(execution_ids=["e1", "e2"], log=logs / "gateways" / "a")] == ["c1", "c3"]
    assert query_calls(execution_ids=["nope"], log=logs) == []
    with pytest.raises(ValueError, match="at least one"):
        query_calls(log=logs)

    assert query_main(["--execution-id", "e1,e2", "--log", str(logs), "--format", "jsonl"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(r["case_id"], r["seq"]) for r in rows] == [("C1", 1), ("C1", 2), ("C2", 1)]


def test_query_show_detail_and_html(tmp_path: Path, capsys):
    logs = tmp_path / "logs"
    _rows(logs / "llm_calls.jsonl", [
        {"event": "pre_api_call", "call_id": "k1", "execution_id": "e1", "case_id": "C1", "run_id": "r1",
         "gateway": "g", "ts": "2026-01-01T00:00:01", "model": "m", "protocol": "chat_completions",
         "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hello there"}],
         "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]},
        {"event": "success", "call_id": "k1", "latency_ms": 7, "response": {
            "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "checking",
                          "tool_calls": [{"function": {"name": "lookup", "arguments": "{\"q\": 1}"}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3}}},
    ] + _call("k2", "e2", "C2", "r1", "2026-01-01T00:00:02"))

    assert [p["call_id"] for p in query_calls(call_ids=["k1"], log=logs)] == ["k1"]
    assert query_main(["show", "k1", "--log", str(logs)]) == 0
    out = capsys.readouterr().out
    for needle in ("case=C1", "tokens in=11 out=3", "-- system (3 chars): SYS", "tools offered (1): lookup",
                   "[1] user: hello there", "-- assistant: checking", 'tool_call lookup({"q": 1})'):
        assert needle in out, needle
    assert query_main(["show", "missing", "--log", str(logs)]) == 1
    capsys.readouterr()

    # table carries call_id + tokens so a row can be drilled into with `query show`
    assert query_main(["--run-id", "r1", "--log", str(logs)]) == 0
    table = capsys.readouterr().out.splitlines()
    assert table[0].split("\t")[:2] == ["ts", "call_id"] and "tok_in" in table[0]
    assert table[1].split("\t")[1] == "k1" and "\t11\t3\t" in table[1]

    out_dir = tmp_path / "view"
    assert query_main(["--run-id", "r1", "--log", str(logs), "--format", "detail", "--html", str(out_dir)]) == 0
    assert (out_dir / "llm_trace.html").is_file()
    assert sorted(p.name for p in (out_dir / "cases").iterdir() if p.is_dir())  # both executions exported


def test_query_by_gateway_name(tmp_path: Path):
    gw = _gateway_dir(tmp_path, {"ins_a": (4101, "http://h/a")})
    g = gatewayctl(gw).load("ins_a", gw)
    _rows(g.log_file, _call("c1", "e1", "C1", "r1", "2026-01-01T00:00:01", "ins_a"))
    assert [p["gateway"] for p in query_calls(execution_ids=["e1"], gateway="ins_a", gateway_dir=gw)] == ["ins_a"]
    with pytest.raises(ValueError, match="unknown gateway"):
        query_calls(execution_ids=["e1"], gateway="missing", gateway_dir=gw)


def test_rotate_archives_refuses_while_leased_and_prunes(tmp_path: Path, monkeypatch):
    gw = _gateway_dir(tmp_path, {"ins_a": (4101, "http://h/a")})
    ctl = gatewayctl(gw)
    g = ctl.load("ins_a", gw)
    _rows(g.log_file, _call("c1", "e1", "C1", "r1", "2026-01-01T00:00:01"))
    original = g.log_file.read_bytes()

    g.registry_dir.mkdir(parents=True, exist_ok=True)
    with (g.registry_dir / "ins_a.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ctl.GatewayError, match="leased"):
            ctl.rotate(g)
    (g.registry_dir / "ins_a.json").write_text(json.dumps({"lane_id": "ins_a", "state": "blocked"}))
    with pytest.raises(ctl.GatewayError, match="blocked"):
        ctl.rotate(g)
    (g.registry_dir / "ins_a.json").write_text(json.dumps({"lane_id": "ins_a", "state": "idle"}))

    stamps = iter(["20260101_000001", "20260101_000002", "20260101_000003"])
    monkeypatch.setattr(ctl.time, "strftime", lambda *_: next(stamps))
    monkeypatch.setattr(ctl.time, "sleep", lambda *_: None)
    first = ctl.rotate(g)
    assert gzip.decompress(first.read_bytes()) == original and not g.log_file.exists()
    assert ctl.rotate(g) is None  # nothing new to rotate
    for _ in range(2):
        _rows(g.log_file, _call("cx", "e", "C", "r", "t"))
        ctl.rotate(g, keep=2)
    assert [p.name for p in ctl.rotated_logs(g)] == ["llm_calls_20260101_000002.jsonl.gz", "llm_calls_20260101_000003.jsonl.gz"]
    assert not list(g.log_dir.glob(".*"))  # no staged/partial files left
