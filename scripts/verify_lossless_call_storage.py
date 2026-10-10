#!/usr/bin/env python3
"""Replay real archived calls through production gzip storage, without model calls.

Sources are never rewritten. Unchanged files are shared in a temporary view and
counted at full logical size; calls are actually written through the production
writer and the unmodified HTML renderer reads them through production readers.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import tempfile
import time

from eval_harness.case_artifacts import read_calls_bytes
from eval_harness.llm_gateway_ingest import write_case_llm_jsonl
from eval_harness.llm_trace_html import build_llm_trace_html
from eval_harness.run import _llm_rows_from_case_file


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def excel_rows_hash(case):
    return hashlib.sha256(json.dumps(_llm_rows_from_case_file(case, case.name),
                                    ensure_ascii=False, default=str).encode()).hexdigest()


def verify(source: Path, out: Path, keep_html: bool):
    source = source.resolve()
    start = time.monotonic()
    files = [p for p in source.rglob("*") if p.is_file()]
    original = {str(p.relative_to(source)): {"bytes": p.stat().st_size, "sha256": sha(p)} for p in files}
    with (source / "llm_trace.html").open(encoding="utf-8") as f:
        title = html.unescape(re.search(r"<title>(.*?)</title>", f.read(8192), re.S)[1]).removesuffix(" · LLM Trace")
    proofs = []
    with tempfile.TemporaryDirectory(prefix="llmtrace-gzip-verification-") as temporary:
        stage = Path(temporary) / source.name
        stage.mkdir()
        for path in files:
            rel = path.relative_to(source)
            if rel.as_posix() == "llm_trace.html" or path.name == "llm_calls.jsonl":
                continue
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.name == ".trace-writer.lock":
                shutil.copyfile(path, target)  # Do not lock source through a symlink.
            else:
                target.symlink_to(path)
        for path in files:
            if path.name != "llm_calls.jsonl":
                continue
            rel = path.relative_to(source)
            target = stage / rel
            raw = path.read_bytes()
            rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
            baseline_excel = excel_rows_hash(path.parent)
            stored = write_case_llm_jsonl(target, rows)
            if read_calls_bytes(target.parent) != raw:
                raise AssertionError(f"Raw file bytes changed: {rel}")
            if excel_rows_hash(target.parent) != baseline_excel:
                raise AssertionError(f"Excel call rows changed: {rel}")
            proofs.append({"path": str(rel), "calls": len(rows), "original_bytes": len(raw),
                           "stored_bytes": stored.stat().st_size, "original_sha256": hashlib.sha256(raw).hexdigest(),
                           "gzip_sha256": sha(stored), "restored_bytes_identical": True,
                           "excel_rows_sha256": baseline_excel})
        print(json.dumps({"source": str(source), "step": "rebuild HTML", "case_files": len(proofs)}), flush=True)
        output = build_llm_trace_html(stage, title=title)
        output_hash = sha(output)
        if output_hash != original["llm_trace.html"]["sha256"]:
            raise AssertionError("Rebuilt HTML differs from original")
        if keep_html:
            shutil.copyfile(output, out.with_suffix(".html"))
    after = {str(p.relative_to(source)): {"bytes": p.stat().st_size, "sha256": sha(p)} for p in files}
    if original != after:
        raise AssertionError("Source files changed during verification")
    before_bytes = sum(v["bytes"] for v in original.values())
    calls_bytes = sum(v["original_bytes"] for v in proofs)
    stored_bytes = sum(v["stored_bytes"] for v in proofs)
    result = {"source": str(source), "calls": sum(p["calls"] for p in proofs), "case_files": len(proofs),
              "original_run_bytes": before_bytes, "stored_run_bytes": before_bytes - calls_bytes + stored_bytes,
              "original_call_bytes": calls_bytes, "stored_call_bytes": stored_bytes,
              "saved_bytes": calls_bytes - stored_bytes, "source_files_unchanged": True,
              "source_file_count": len(files), "html_identical": True, "html_sha256": output_hash,
              "elapsed_seconds": round(time.monotonic() - start, 2), "proofs": proofs}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "proofs"}), flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="append", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--keep-html", action="store_true", help="Keep rebuilt HTML for visual inspection")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    results = [verify(source, args.out / f"run-{i:02d}.json", args.keep_html) for i, source in enumerate(args.run)]
    keys = ("calls", "case_files", "original_run_bytes", "stored_run_bytes", "original_call_bytes", "stored_call_bytes", "saved_bytes", "source_file_count")
    summary = {"runs": len(results), **{key: sum(r[key] for r in results) for key in keys},
               "all_html_identical": all(r["html_identical"] for r in results),
               "all_source_files_unchanged": all(r["source_files_unchanged"] for r in results)}
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
