#!/usr/bin/env python3
"""Isolated storage experiment. Never changes a source run or production readers.

Run with PYTHONPATH=eval_harness/src and the project's Python environment.
Unchanged files are read-only symlinks in a temporary view. Size accounting counts
their original logical bytes, NOT symlink sizes. Experimental files are actually
written; every omitted source file must reconstruct byte-for-byte before use.
The scoped virtual reader is a prototype, not a production filesystem/codec.
"""
from __future__ import annotations

import argparse
import collections
from contextlib import ExitStack, contextmanager
import hashlib
import html
import io
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import patch


PAIRS = [
    (("request", "messages"), ("request", "client_request", "messages")),
    (("request", "tools"), ("request", "optional_params", "tools")),
    (("response", "messages"), ("request", "messages")),
    (("response", "tools"), ("request", "tools")),
]
DECODER = json.JSONDecoder()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def span(text: str, path: tuple[str, ...], start: int = 0) -> tuple[int, int]:
    """Find the original textual JSON value; preserve whitespace, order, numbers."""
    while text[start].isspace():
        start += 1
    if not path:
        return start, DECODER.raw_decode(text, start)[1]
    if text[start] != "{":
        raise KeyError(path)
    pos = start + 1
    matches = []
    while True:
        while text[pos].isspace():
            pos += 1
        if text[pos] == "}":
            break
        key, pos = DECODER.raw_decode(text, pos)
        while text[pos].isspace():
            pos += 1
        if text[pos] != ":":
            raise ValueError("expected colon")
        pos += 1
        while text[pos].isspace():
            pos += 1
        end = DECODER.raw_decode(text, pos)[1]
        if key == path[0]:
            matches.append((pos, end))
        pos = end
        while text[pos].isspace():
            pos += 1
        if text[pos] == "}":
            break
        if text[pos] != ",":
            raise ValueError("expected comma")
        pos += 1
    if len(matches) != 1:
        raise KeyError(path)  # Missing/ambiguous keys must not be coalesced.
    return span(text, path[1:], matches[0][0])


def encode_line(raw: str) -> tuple[str, list[int]]:
    a, b = span(raw, ())
    body = raw[a:b]
    refs, edits = [], []
    for index, (target, source) in enumerate(PAIRS):
        try:
            ta, tb = span(body, target)
            sa, sb = span(body, source)
        except KeyError:
            continue
        # Strict textual identity: true != 1; 1 != 1.0; key order retained.
        if body[ta:tb] == body[sa:sb] and tb - ta > 4:
            refs.append(index)
            edits.append((ta, tb))
    for ta, tb in sorted(edits, reverse=True):
        body = body[:ta] + "null" + body[tb:]
    meta = {"version": 1, "sha256": digest(raw.encode()), "refs": refs,
            "prefix": raw[:a], "suffix": raw[b:]}
    return dumps(meta)[:-1] + ',"data":' + body + "}\n", refs


def decode_line(encoded: str) -> str:
    meta = json.loads(encoded)
    if meta.get("version") != 1:
        raise ValueError("unsupported storage version")
    refs = meta["refs"]
    if any(type(i) is not int or i not in range(len(PAIRS)) for i in refs) or refs != sorted(set(refs)):
        raise ValueError("invalid reference list")
    a, b = span(encoded, ("data",))
    body = encoded[a:b]
    # Request slots are restored before response slots that refer to them.
    for index in refs:
        target, source = PAIRS[index]
        ta, tb = span(body, target)
        sa, sb = span(body, source)
        if body[ta:tb] != "null":
            raise ValueError("reference target is not empty")
        body = body[:ta] + body[sa:sb] + body[tb:]
    raw = meta["prefix"] + body + meta["suffix"]
    if digest(raw.encode()) != meta["sha256"]:
        raise ValueError("restored record hash mismatch")
    return raw


def encode_file(raw: bytes) -> tuple[bytes, dict]:
    encoded, counts = [], collections.Counter()
    for line_bytes in raw.splitlines(keepends=True):
        line = line_bytes.decode("utf-8")
        if not line.strip():
            # Blank lines are also literal evidence, not discarded.
            encoded.append(dumps({"literal": line}) + "\n")
            continue
        compact, refs = encode_line(line)
        encoded.append(compact)
        counts.update(str(i) for i in refs)
    return "".join(encoded).encode(), dict(counts)


def decode_file(encoded: bytes) -> bytes:
    return "".join(json.loads(line)["literal"] if "literal" in json.loads(line)
                   else decode_line(line) for line in (part.decode() for part in encoded.splitlines())).encode()


def reconstruct(root: Path, rel: str, entry: dict) -> bytes:
    if entry["kind"] == "call_fields":
        result = decode_file((root / entry["sources"][0]).read_bytes())
    elif entry["kind"] == "combined_stream":
        parts = []
        for source in entry["sources"]:
            turn = int(re.fullmatch(r"stream_turn(\d+)\.jsonl", Path(source).name)[1])
            raw = (root / source).read_bytes()
            parts.append(f"### TURN {turn}\n".encode() + raw + (b"\n" if raw and not raw.endswith(b"\n") else b""))
        result = b"".join(parts)
    elif entry["kind"] == "events":
        rows = json.loads((root / entry["sources"][0]).read_text())["events"]
        result = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows).encode()
    elif entry["kind"] == "mapped":
        from eval_harness.adapters.pi import pi_events_to_claude_like
        rows = [json.loads(line) for line in (root / entry["sources"][0]).read_text().splitlines() if line.strip()]
        mapped = pi_events_to_claude_like(rows)
        result = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in mapped).encode()
    else:
        raise ValueError("unknown reconstruction recipe")
    if digest(result) != entry["sha256"] or len(result) != entry["bytes"]:
        raise ValueError(f"restored file hash mismatch: {rel}")
    return result


@contextmanager
def virtual_reader(root: Path, entries: dict):
    """Only intercept known missing files in this isolated run, in this process.

    This intentionally does not claim support for third-party readers, Path.open,
    append, concurrent collection, rotation, recovery or production migration.
    """
    raw_read_bytes, raw_read_text = Path.read_bytes, Path.read_text
    raw_is_file, raw_exists = Path.is_file, Path.exists
    lookup = {str(root / rel): (rel, entry) for rel, entry in entries.items()}

    def get(path):
        return lookup.get(str(path)) if not raw_is_file(path) else None

    def read_bytes(path):
        item = get(path)
        return reconstruct(root, *item) if item else raw_read_bytes(path)

    def read_text(path, encoding=None, errors=None, **kwargs):
        if get(path):
            with io.TextIOWrapper(io.BytesIO(read_bytes(path)), encoding=encoding or "utf-8", errors=errors, **kwargs) as stream:
                return stream.read()
        return raw_read_text(path, encoding=encoding, errors=errors, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(Path, "read_bytes", read_bytes))
        stack.enter_context(patch.object(Path, "read_text", read_text))
        stack.enter_context(patch.object(Path, "is_file", lambda p: bool(get(p)) or raw_is_file(p)))
        stack.enter_context(patch.object(Path, "exists", lambda p: bool(get(p)) or raw_exists(p)))
        yield


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def recipe(path: Path, root: Path) -> dict | None:
    sources, kind = [], None
    if path.name == "stream.jsonl":
        turns = []
        for p in path.parent.glob("stream_turn*.jsonl"):
            match = re.fullmatch(r"stream_turn(\d+)\.jsonl", p.name)
            if match:
                turns.append((int(match[1]), p))
        if turns:
            sources, kind = [p for _, p in sorted(turns)], "combined_stream"
    elif path.name == "events.jsonl" and (path.parent / "trace.json").is_file():
        sources, kind = [path.parent / "trace.json"], "events"
    elif re.fullmatch(r"stream_turn\d+_mapped.jsonl", path.name):
        original = path.with_name(path.name.replace("_mapped", ""))
        if original.is_file():
            sources, kind = [original], "mapped"
    if not kind:
        return None
    return {"kind": kind, "sources": [str(p.relative_to(root)) for p in sources],
            "sha256": hash_file(path), "bytes": path.stat().st_size}


def render(root: Path, title: str, entries: dict) -> dict:
    from eval_harness import llm_trace_html
    from eval_harness.run import _llm_rows_from_case_file
    from eval_harness.llm_gateway_ingest import read_case_llm_jsonl_text
    counters = collections.Counter()
    excel = hashlib.sha256()
    with virtual_reader(root, entries):
        for directory in sorted(p for p in (root / "cases").rglob("*") if p.is_dir()):
            text = read_case_llm_jsonl_text(directory)
            if text is None:
                continue
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            counters["case_files"] += 1
            counters["calls"] += len(rows)
            counters["calls_with_failure"] += sum(bool(row.get("error")) or bool((row.get("response") or {}).get("error")) for row in rows)
            counters["attempt_case_files"] += int("attempts" in directory.parts)
            excel.update(json.dumps(_llm_rows_from_case_file(directory, directory.name), ensure_ascii=False, default=str).encode())
        output = llm_trace_html.build_llm_trace_html(root, title=title)
    result = {**counters, "html_sha256": hash_file(output), "html_bytes": output.stat().st_size,
              "excel_llm_rows_sha256": excel.hexdigest()}
    output.unlink()  # Own generated file only. Never linked to source HTML.
    return result


def run_experiment(source: Path, output: Path) -> dict:
    source = source.resolve()
    files = sorted(p for p in source.rglob("*") if p.is_file())
    before = {str(p.relative_to(source)): {"sha256": hash_file(p), "bytes": p.stat().st_size} for p in files}
    original_html = source / "llm_trace.html"
    with original_html.open(encoding="utf-8") as f:
        header = f.read(8192)
    title = html.unescape(re.search(r"<title>(.*?)</title>", header, re.S)[1]).removesuffix(" · LLM Trace")
    total = sum(v["bytes"] for v in before.values())
    all_entries, rejected, field_counts = {}, [], collections.Counter()
    with tempfile.TemporaryDirectory(prefix="llmtrace-storage-experiment-") as tmp:
        stage = Path(tmp) / source.name
        stage.mkdir()
        for p in files:
            rel = str(p.relative_to(source))
            if rel == "llm_trace.html":
                continue
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(p)
        print(dumps({"run": str(source), "step": "baseline"}), flush=True)
        baseline = render(stage, title, {})
        for p in files:
            rel = str(p.relative_to(source))
            entry = recipe(p, source)
            if entry:
                try:
                    reconstruct(stage, rel, entry)
                except (ValueError, KeyError, json.JSONDecodeError):
                    rejected.append({"path": rel, "reason": "not byte-identical; retained"})
                else:
                    all_entries[rel] = entry
            if p.name == "llm_calls.jsonl":
                original = p.read_bytes()
                encoded, counts = encode_file(original)
                if decode_file(encoded) != original:
                    raise AssertionError("call codec lost original bytes")
                field_counts.update(counts)
                if len(encoded) < len(original):
                    target_rel = rel + ".experiment-refs"
                    (stage / target_rel).write_bytes(encoded)
                    all_entries[rel] = {"kind": "call_fields", "sources": [target_rel],
                                        **before[rel], "stored_bytes": len(encoded), "stored_sha256": digest(encoded)}
        variants = {}
        for name, kinds in [("auxiliary_only", {"combined_stream", "events", "mapped"}),
                            ("call_fields_only", {"call_fields"}),
                            ("combined", {"combined_stream", "events", "mapped", "call_fields"})]:
            entries = {p: e for p, e in all_entries.items() if e["kind"] in kinds}
            for rel in entries:
                (stage / rel).unlink()  # Removes our symlink, never the source file.
            manifest = dumps({"experiment_version": 1, "files": entries}).encode()
            (stage / "storage-manifest.json").write_bytes(manifest)
            print(dumps({"run": str(source), "step": name, "files": len(entries)}), flush=True)
            # Test a clean, serialized manifest, not the in-memory construction map.
            reloaded = json.loads((stage / "storage-manifest.json").read_bytes())["files"]
            for rel, entry in reloaded.items():
                if reconstruct(stage, rel, entry) != (source / rel).read_bytes():
                    raise AssertionError("file restoration mismatch")
            result = render(stage, title, reloaded)
            result["identical_to_baseline"] = result == baseline
            if not result["identical_to_baseline"]:
                raise AssertionError(dumps({"baseline": baseline, "variant": result}))
            removed = sum(e["bytes"] for e in entries.values())
            stored = sum(e.get("stored_bytes", 0) for e in entries.values()) + len(manifest)
            result.update(original_run_bytes=total, optimized_run_bytes=total - removed + stored,
                          saved_bytes=removed - stored, original_selected_bytes=removed,
                          stored_selected_bytes=stored, manifest_bytes=len(manifest),
                          restored_files=len(entries), restored_original_bytes=removed)
            variants[name] = result
            for rel in entries:
                (stage / rel).symlink_to(source / rel)
        after = {str(p.relative_to(source)): {"sha256": hash_file(p), "bytes": p.stat().st_size} for p in files}
        if before != after:
            raise AssertionError("source changed during experiment")
    result = {"source": str(source), "baseline": baseline, "variants": variants,
              "existing_html_matches_current_baseline": before["llm_trace.html"]["sha256"] == baseline["html_sha256"],
              "source_files_unchanged": True, "source_file_count": len(files), "field_reference_counts": dict(field_counts),
              "rejected_recipes": rejected, "recipes": all_entries, "source_hashes": before}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(dumps({"run": str(source), "step": "passed", "saved_bytes": variants["combined"]["saved_bytes"],
                 "existing_html_matches": result["existing_html_matches_current_baseline"]}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for index, source in enumerate(args.run):
        results.append(run_experiment(source, args.out / f"run-{index:02d}.json"))
    summary = {"runs": len(results), "calls": sum(r["baseline"]["calls"] for r in results),
               "case_files": sum(r["baseline"]["case_files"] for r in results),
               "variants": {name: {key: sum(r["variants"][name][key] for r in results)
                                   for key in ("original_run_bytes", "optimized_run_bytes", "saved_bytes", "restored_files", "restored_original_bytes")}
                            for name in ("auxiliary_only", "call_fields_only", "combined")},
               "all_sources_unchanged": all(r["source_files_unchanged"] for r in results),
               "all_variants_identical": all(v["identical_to_baseline"] for r in results for v in r["variants"].values()),
               "existing_html_matches_current_baseline": sum(r["existing_html_matches_current_baseline"] for r in results)}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(dumps(summary), flush=True)


if __name__ == "__main__":
    main()
