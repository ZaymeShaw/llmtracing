"""Lossless archive publication, incremental exports and existing consumers."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import threading

import pytest

from eval_harness import case_artifacts
from eval_harness.case_artifacts import CaseArtifactError, read_calls_bytes
from eval_harness.llm_gateway_ingest import (
    attach_llm_calls_to_trace, read_case_llm_jsonl_text, read_trace_llm_calls,
    update_case_llm_jsonl, write_case_llm_jsonl,
)
from eval_harness.llm_trace_html import build_llm_trace_html, build_payload
from eval_harness.run import _llm_rows_from_case_file
from eval_harness.trace_identity import archive_case
from eval_harness.trace_store import export_executions


def call(cid="one", **extra):
    return {"call_id": cid, "case_id": "A01", "execution_id": "execution-1", "run_id": "batch",
            "harness": "fixture", "ts": "2026-10-09T10:00:00Z", "protocol": "chat_completions",
            "request": {"path": "/原始路径/../v1/chat/completions", "messages": [
                {"role": "user", "content": "保单\\path <script>\r\n" * 100}],
                "tools": [{"type": "function", "function": {"name": "mcp_lookup", "parameters": {"type": "object"}}}]},
            "response": {"response": {"choices": [{"message": {"role": "assistant", "content": "答复"}}]}},
            **extra}


def serialized(calls):
    return "".join(json.dumps(c, ensure_ascii=False, default=str) + "\n" for c in calls).encode()


def test_new_archive_exact_bytes_and_deterministic_gzip(tmp_path):
    rows = [call(), call("failed", response=None, error="timeout"), call("pending", response=None)]
    target = write_case_llm_jsonl(tmp_path / "llm_calls.jsonl", rows)
    assert target.name == "llm_calls.jsonl.gz"
    assert not (tmp_path / "llm_calls.jsonl").exists()
    assert gzip.decompress(target.read_bytes()) == serialized(rows) == read_calls_bytes(tmp_path)
    first = target.read_bytes()
    assert len(first) < len(serialized(rows))
    write_case_llm_jsonl(tmp_path / "llm_calls.jsonl", rows)
    assert target.read_bytes() == first
    trace = attach_llm_calls_to_trace({}, rows, embed=False)
    assert read_trace_llm_calls(trace, tmp_path) == rows


def test_legacy_plain_stays_plain_and_wins_over_stale_gzip(tmp_path):
    path = tmp_path / "llm_calls.jsonl"
    path.write_bytes(serialized([call("old")]))
    stale = tmp_path / "llm_calls.jsonl.gz"
    stale.write_bytes(gzip.compress(serialized([call("stale")])))
    write_case_llm_jsonl(path, [call("new")])
    assert read_calls_bytes(tmp_path) == path.read_bytes() == serialized([call("new")])
    assert b"stale" in gzip.decompress(stale.read_bytes())


def test_empty_case_and_missing_are_distinct(tmp_path):
    assert read_calls_bytes(tmp_path) is None
    write_case_llm_jsonl(tmp_path / "llm_calls.jsonl", [])
    assert read_calls_bytes(tmp_path) == b""
    assert read_case_llm_jsonl_text(tmp_path) == ""


@pytest.mark.parametrize("failure", ["serialize", "fsync", "publish", "verify"])
def test_failed_snapshot_keeps_previous_complete_archive(tmp_path, monkeypatch, failure):
    path = tmp_path / "llm_calls.jsonl"
    target = write_case_llm_jsonl(path, [call("old")])
    before = target.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected disk failure")

    if failure == "fsync":
        monkeypatch.setattr(case_artifacts.os, "fsync", fail)
    elif failure == "publish":
        monkeypatch.setattr(case_artifacts.os, "replace", fail)
    elif failure == "verify":
        monkeypatch.setattr(case_artifacts.gzip, "open", fail)
    rows = [call("new")]
    if failure == "serialize":
        recursive = {}
        recursive["self"] = recursive
        rows.append(recursive)
    with pytest.raises((ValueError, OSError)):
        write_case_llm_jsonl(path, rows)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".llm-calls-*"))


def test_corruption_stops_html_excel_and_incremental_export(tmp_path):
    run = tmp_path / "run"
    export_executions([call()], run)
    case_dir, = (run / "cases").iterdir()
    target = case_dir / "llm_calls.jsonl.gz"
    target.write_bytes(target.read_bytes()[:-5])
    (run / "llm_trace.html").write_text("keep previous report")
    for reader in (lambda: read_calls_bytes(case_dir), lambda: build_llm_trace_html(run),
                   lambda: _llm_rows_from_case_file(case_dir, "A01"),
                   lambda: export_executions([call("two")], run)):
        with pytest.raises(CaseArtifactError):
            reader()
    assert (run / "llm_trace.html").read_text() == "keep previous report"


def test_incremental_pending_failure_retry_and_repeated_export(tmp_path):
    run = tmp_path / "run"
    export_executions([call("one", response=None)], run)
    export_executions([call("one"), call("failure", response=None, error="upstream failure")], run)
    export_executions([call("retry", execution_id="retry-execution")], run)
    export_executions([call("one", response=None)], run)
    payload = build_payload(run)
    assert sorted(c["n_calls"] for c in payload["cases"]) == [1, 2]
    saved = [r for p in (run / "cases").iterdir() for r in json.loads("[" + ",".join(read_case_llm_jsonl_text(p).splitlines()) + "]")]
    assert next(r for r in saved if r["call_id"] == "one")["response"] is not None
    assert next(r for r in saved if r["call_id"] == "failure")["error"] == "upstream failure"
    assert len(list(run.glob("cases/*/llm_calls.jsonl.gz"))) == 2


def test_case_transaction_rejects_competing_writer_without_losing_updates(tmp_path):
    path = tmp_path / "llm_calls.jsonl"
    write_case_llm_jsonl(path, [call("old")])
    acquired, release = threading.Event(), threading.Event()

    def slow_update(rows):
        acquired.set()
        assert release.wait(5)
        return rows + [call("first")]

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(update_case_llm_jsonl, path, slow_update)
        assert acquired.wait(5)
        try:
            with pytest.raises(RuntimeError, match="Another writer"):
                update_case_llm_jsonl(path, lambda rows: rows + [call("second")])
            assert read_calls_bytes(tmp_path) == serialized([call("old")])
        finally:
            release.set()
        first.result()
    update_case_llm_jsonl(path, lambda rows: rows + [call("second")])
    assert [r["call_id"] for r in map(json.loads, read_case_llm_jsonl_text(tmp_path).splitlines())] == ["old", "first", "second"]


def test_retry_archive_preserves_compressed_original(tmp_path):
    write_case_llm_jsonl(tmp_path / "llm_calls.jsonl", [call("failed", error="failure")])
    before = read_calls_bytes(tmp_path)
    archive_case(tmp_path)
    write_case_llm_jsonl(tmp_path / "llm_calls.jsonl", [call("retry")])
    old, = (tmp_path / "attempts").iterdir()
    assert read_calls_bytes(old) == before
    assert b'"retry"' in read_calls_bytes(tmp_path)


def test_new_and_legacy_storage_generate_identical_html_and_excel_rows(tmp_path):
    run = tmp_path / "batch"
    directory = run / "cases/A01"
    directory.mkdir(parents=True)
    rows = [call()]
    plain = directory / "llm_calls.jsonl"
    plain.write_bytes(serialized(rows))
    before_html = build_llm_trace_html(run).read_bytes()
    before_excel = _llm_rows_from_case_file(directory, "A01")
    plain.unlink()  # Only our test fixture, never a real source archive.
    write_case_llm_jsonl(plain, rows)
    assert build_llm_trace_html(run).read_bytes() == before_html
    assert _llm_rows_from_case_file(directory, "A01") == before_excel
