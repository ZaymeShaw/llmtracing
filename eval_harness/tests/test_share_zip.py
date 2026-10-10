import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from eval_harness.secret_scan import SecretScanError
from eval_harness.share_zip import MANIFEST, pack_share_zip


def fixture_report(tmp_path):
    root = tmp_path / "report"
    root.mkdir()
    (root / "index.html").write_text('<a href="nested/llm_trace.html#raw">报告</a>')
    (root / "nested").mkdir()
    data = {"calls": [{"request": "完整请求", "response": "完整回复", "tools": ["工具结果"]}]}
    (root / "nested/llm_trace.html").write_text(
        '<a href="../linked%20calls.jsonl?download=1">附件</a>'
        '<a href="#raw">Raw</a><a href="https://example.com">外部</a>'
        '<script id="DATA" type="application/json">' + json.dumps(data, ensure_ascii=False) + '</script>')
    (root / "linked calls.jsonl").write_text('{"required":true}\n')
    (root / "raw.jsonl").write_text(json.dumps(data))
    (root / "export.log").write_text("unused log")
    (root / "metadata.json").write_text('{"success":true}')
    return root


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("source_kind", ["directory", "zip"])
def test_viewer_and_linked_attachment_unchanged(tmp_path, source_kind):
    root = fixture_report(tmp_path)
    before = snapshot(root)
    src = root
    if source_kind == "zip":
        src = tmp_path / "full.zip"
        with zipfile.ZipFile(src, "w") as z:
            for p in root.rglob("*"):
                if p.is_file():
                    z.write(p, "report/" + p.relative_to(root).as_posix())
    source_hash = hashlib.sha256(src.read_bytes()).hexdigest() if src.is_file() else None
    out = tmp_path / "share.zip"
    report = pack_share_zip(src, out)
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        for name in ["index.html", "nested/llm_trace.html", "linked calls.jsonl", "metadata.json"]:
            assert z.read("report/" + name) == (root / name).read_bytes()
        assert "report/raw.jsonl" not in z.namelist()
        assert "report/export.log" not in z.namelist()
        manifest = json.loads(z.read("report/" + MANIFEST))
        assert len(manifest["local_links"]) == 2
        assert manifest["external_links"] == 1
        assert manifest["omitted_files"] == ["report/export.log", "report/raw.jsonl"]
    assert report["html_count"] == 2 and report["all_html_bytes_unchanged"]
    assert snapshot(root) == before
    if source_hash:
        assert hashlib.sha256(src.read_bytes()).hexdigest() == source_hash


def test_broken_link_and_no_html_produce_no_zip(tmp_path):
    root = fixture_report(tmp_path)
    out = tmp_path / "share.zip"
    (root / "index.html").write_text('<a href="missing.jsonl">missing</a>')
    with pytest.raises(ValueError, match="broken local link"):
        pack_share_zip(root, out)
    assert not out.exists()
    for p in root.rglob("*.html"):
        p.unlink()
    with pytest.raises(ValueError, match="no existing HTML"):
        pack_share_zip(root, out)
    assert not out.exists()


def test_secret_gate_checks_uncompressed_html_and_cleans_temporary_file(tmp_path):
    root = fixture_report(tmp_path)
    (root / "index.html").write_text("sk-" + "a" * 32)
    out = tmp_path / "share.zip"
    with pytest.raises(SecretScanError):
        pack_share_zip(root, out)
    assert not out.exists()
    assert not list(tmp_path.glob("share.zip.*"))


def test_existing_output_and_source_are_never_overwritten(tmp_path):
    root = fixture_report(tmp_path)
    out = tmp_path / "share.zip"
    out.write_bytes(b"existing archive")
    with pytest.raises(FileExistsError):
        pack_share_zip(root, out)
    assert out.read_bytes() == b"existing archive"
    with pytest.raises(ValueError, match="outside the source"):
        pack_share_zip(root, root / "share.zip")


@pytest.mark.parametrize("member", ["../index.html", "/index.html", "report/../index.html"])
def test_unsafe_archive_paths_rejected(tmp_path, member):
    src = tmp_path / "full.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr(member, "viewer")
    with pytest.raises(ValueError, match="unsafe archive path"):
        pack_share_zip(src, tmp_path / "share.zip")


def test_cli_dispatches_share_without_rebuilding_html(monkeypatch):
    cli_path = Path(__file__).resolve().parents[2] / "llmtrace"
    from importlib.machinery import SourceFileLoader
    spec = importlib.util.spec_from_loader("share_cli", SourceFileLoader("share_cli", str(cli_path)))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    calls = []
    monkeypatch.setattr(cli, "_harness", lambda module, args: calls.append((module, args)) or 0)
    assert cli.report(["share", "run", "--out", "share.zip"]) == 0
    assert calls == [("eval_harness.share_zip", ["run", "--out", "share.zip"])]


def basic_report(tmp_path):
    root = fixture_report(tmp_path)
    (root / "llm_trace.html").write_text(
        '<title>Existing report</title><a href="linked%20calls.jsonl">attachment</a>'
        '<script id="DATA" type="application/json">'
        '{"request":{"path":"../raw.jsonl","messages":["repeat","repeat"]}}'
        '</script>')
    return root


def test_basic_pack_uses_share_policy_without_excel_or_html_rebuild(tmp_path, monkeypatch):
    from eval_harness import llm_trace_html
    root = basic_report(tmp_path)
    before = snapshot(root)
    monkeypatch.setattr(llm_trace_html, "build_llm_trace_html",
                        lambda *a, **kw: pytest.fail("packing must not regenerate HTML"))
    out = llm_trace_html.pack_run_zip(root)
    for _ in range(2):
        with zipfile.ZipFile(out) as z:
            assert z.read("llm_trace.html") == (root / "llm_trace.html").read_bytes()
            assert z.read("linked calls.jsonl") == (root / "linked calls.jsonl").read_bytes()
            assert "nested/llm_trace.html" in z.namelist()
            assert "raw.jsonl" not in z.namelist()
            assert "export.log" not in z.namelist()
            assert not any(n.endswith(".zip") for n in z.namelist())
            assert MANIFEST in z.namelist()
        assert all(snapshot(root)[name] == digest for name, digest in before.items())
        previous_bytes = out.read_bytes()
        out = llm_trace_html.pack_run_zip(root)
        assert out.with_suffix(".prev.zip").read_bytes() == previous_bytes


def test_failed_basic_repack_preserves_previous_output(tmp_path):
    from eval_harness.llm_trace_html import pack_run_zip
    root = basic_report(tmp_path)
    out = pack_run_zip(root)
    before = out.read_bytes()
    (root / "llm_trace.html").write_text('<a href="missing.csv">broken</a>')
    with pytest.raises(ValueError, match="broken local link"):
        pack_run_zip(root)
    assert out.read_bytes() == before
    assert not list(tmp_path.glob(".share-build-*"))


def test_basic_cli_zip_preserves_existing_html(tmp_path):
    root = basic_report(tmp_path)
    before = snapshot(root)
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, str(repo / "llmtrace"), "report", "html", str(root), "--zip"],
                          env={**os.environ, "PYTHONPATH": str(repo / "eval_harness/src")},
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert all(snapshot(root)[name] == digest for name, digest in before.items())
    with zipfile.ZipFile(root / "report_share.zip") as z:
        assert z.read("llm_trace.html") == (root / "llm_trace.html").read_bytes()
        assert "raw.jsonl" not in z.namelist()


def test_gateway_cli_zip_exports_then_uses_share_policy(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    log = tmp_path / "gateway.jsonl"
    records = [
        {"event": "request", "call_id": "call-1", "case_id": "A01", "run_id": "batch",
         "execution_id": "exec-1", "ts": "2026-10-09T10:00:00Z", "protocol": "chat_completions",
         "request": {"model": "fixture", "messages": [{"role": "user", "content": "../original/path"}]}},
        {"event": "response", "call_id": "call-1", "ts": "2026-10-09T10:00:01Z", "status_code": 200,
         "response": {"choices": [{"message": {"role": "assistant", "content": "reply"}}]}},
    ]
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    root = tmp_path / "wire"
    proc = subprocess.run([sys.executable, str(repo / "llmtrace"), "report", "html",
                           "--log", str(log), "--out", str(root), "--run-id", "batch", "--zip"],
                          env={**os.environ, "PYTHONPATH": str(repo / "eval_harness/src")},
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    html_bytes = (root / "llm_trace.html").read_bytes()
    assert b"../original/path" in html_bytes
    assert list(root.glob("cases/*/llm_calls.jsonl.gz"))
    with zipfile.ZipFile(root / "wire_share.zip") as z:
        assert z.read("llm_trace.html") == html_bytes
        assert not any(n.endswith((".jsonl", ".jsonl.gz")) for n in z.namelist())


def test_product_pack_covers_all_harnesses_and_linked_spreadsheets(tmp_path):
    from eval_harness.product_zip import pack_product_zip
    root = tmp_path / "multi"
    for agent in ["pi", "opencode", "dsh"]:
        folder = root / agent
        folder.mkdir(parents=True)
        (folder / "llm_trace.html").write_text('<a href="linked.xlsx">attachment</a>')
        (folder / "linked.xlsx").write_bytes(b"linked spreadsheet")
        (folder / "results.xlsx").write_bytes(b"unlinked spreadsheet")
        (folder / "llm_calls.jsonl").write_text('{"raw":true}')
    out = pack_product_zip(root, tmp_path / "product.zip")
    with zipfile.ZipFile(out) as z:
        for agent in ["pi", "opencode", "dsh"]:
            assert z.read(f"{agent}/llm_trace.html") == (root / agent / "llm_trace.html").read_bytes()
            assert f"{agent}/linked.xlsx" in z.namelist()
            assert f"{agent}/results.xlsx" not in z.namelist()
            assert f"{agent}/llm_calls.jsonl" not in z.namelist()
