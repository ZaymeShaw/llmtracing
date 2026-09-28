#!/usr/bin/env python3
"""Post-run disk slim for eval artifacts (safe defaults).

What llm_trace.html depends on
------------------------------
* **View time**: nothing on disk besides the HTML file. ``build_llm_trace_html``
  embeds the full case payload in ``<script id="DATA">`` (self-contained).
* **Rebuild time** (``python -m eval_harness.llm_trace_html <run_dir>``):
  - required: ``cases/<id>/llm_calls.jsonl`` (or ``.jsonl.gz``) and/or
    ``trace.json`` / Insurance ``meta.json``
  - optional enrichment: ``stream.jsonl``, ``events.jsonl``, runner meta
  - NOT read at rebuild: ``stream_turn*.jsonl``, ``stream_pi.jsonl``,
    ``stream_turn*_mapped.jsonl``, ``stream_turn*_attempt*.jsonl``
* **Excel rebuild**: ``trace.json`` + ``llm_calls.jsonl(.gz)`` via harness.

Safe actions (default ``--apply`` set)
--------------------------------------
1. Delete ``stream_turnN.jsonl`` when ``stream.jsonl`` exactly equals the
   harness ``concat_streams`` reconstruction (header + turn bodies).
2. Delete ``stream_pi.jsonl`` when byte-identical to ``stream.jsonl``.
3. Delete ``stream_turn*_mapped.jsonl`` (Pi debug view; normalize already ran).
4. Delete ``stream_turn*_attempt*.jsonl`` (failed-attempt archives).
5. Optionally ``--gzip-llm-calls``: gzip ``llm_calls.jsonl`` → ``.gz`` and
   remove the plain file (rebuild/HTML loaders understand ``.gz``).

Unsafe / deliberately not done here
-----------------------------------
* Delete ``stream.jsonl``, ``events.jsonl``, ``trace.json``, ``meta.json``,
  ``llm_trace.html``, ``results.xlsx``
* Delete plain ``llm_calls.jsonl`` without a verified ``.gz`` sibling
* Touch an in-flight run (default skip stamp substring
  ``115051_aliyun_maas4001_claude120``; override with ``--allow-live``)
* Delete active ``llm_gateway/logs/llm_calls.jsonl``
* Delete rotated gateway ``llm_calls*.gz`` while a run may still need
  manual recovery (use ``--gateway-rotated`` only after runs finish)

Usage
-----
  # dry-run (default)
  python3 scripts/slim_eval_artifacts.py eval_runs/triple_datasetA_20260926_final_merged

  # apply stream-dedup only
  python3 scripts/slim_eval_artifacts.py eval_runs/... --apply

  # also gzip per-case llm_calls after HTML exists
  python3 scripts/slim_eval_artifacts.py eval_runs/... --apply --gzip-llm-calls
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_LIVE_SKIP = ("115051_aliyun_maas4001_claude120",)


@dataclass
class Stats:
    examined_cases: int = 0
    deleted: list[tuple[str, int]] = field(default_factory=list)
    gzipped: list[tuple[str, int, int]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def bytes_deleted(self) -> int:
        return sum(n for _, n in self.deleted)

    @property
    def bytes_gzip_saved(self) -> int:
        return sum(max(0, before - after) for _, before, after in self.gzipped)


def _is_agent_run_dir(d: Path) -> bool:
    return (d / "cases").is_dir() and (
        (d / "llm_trace.html").is_file()
        or any((d / "cases").iterdir())
    )


def _iter_agent_run_dirs(root: Path) -> list[Path]:
    root = root.resolve()
    if _is_agent_run_dir(root):
        return [root]
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and _is_agent_run_dir(child):
            out.append(child)
    return out


def _html_ok(run_dir: Path) -> bool:
    html = run_dir / "llm_trace.html"
    return html.is_file() and html.stat().st_size > 1024


def _turn_files(case_dir: Path) -> list[Path]:
    turns = []
    for p in case_dir.glob("stream_turn*.jsonl"):
        name = p.name
        if "_attempt" in name or "_mapped" in name:
            continue
        m = re.search(r"stream_turn(\d+)\.jsonl$", name)
        if m:
            turns.append((int(m.group(1)), p))
    turns.sort(key=lambda x: x[0])
    return [p for _, p in turns]


def _stream_matches_turns(case_dir: Path) -> bool:
    stream = case_dir / "stream.jsonl"
    turns = _turn_files(case_dir)
    if not stream.is_file() or not turns:
        return False
    parts: list[bytes] = []
    for t in turns:
        n = re.search(r"stream_turn(\d+)\.jsonl$", t.name).group(1)
        body = t.read_bytes()
        chunk = f"### TURN {n}\n".encode() + body
        if body and not body.endswith(b"\n"):
            chunk += b"\n"
        parts.append(chunk)
    return stream.read_bytes() == b"".join(parts)


def _unlink(path: Path, stats: Stats, *, apply: bool) -> None:
    size = path.stat().st_size if path.is_file() else 0
    rel = str(path)
    if apply:
        path.unlink()
    stats.deleted.append((rel, size))


def _gzip_llm_calls(case_dir: Path, stats: Stats, *, apply: bool) -> None:
    plain = case_dir / "llm_calls.jsonl"
    gz_path = case_dir / "llm_calls.jsonl.gz"
    if not plain.is_file():
        return
    if gz_path.is_file():
        # already gzipped companion — only remove plain if gz verifies
        try:
            with gzip.open(gz_path, "rb") as f:
                gz_bytes = f.read()
            if gz_bytes == plain.read_bytes():
                _unlink(plain, stats, apply=apply)
            else:
                stats.skipped.append(f"llm_calls mismatch plain vs gz: {case_dir}")
        except Exception as e:
            stats.errors.append(f"gz verify failed {case_dir}: {e!r}")
        return
    raw = plain.read_bytes()
    before = len(raw)
    compressed = gzip.compress(raw, compresslevel=9)
    after = len(compressed)
    if apply:
        tmp = gz_path.with_suffix(gz_path.suffix + ".tmp")
        tmp.write_bytes(compressed)
        # verify round-trip
        with gzip.open(tmp, "rb") as f:
            if f.read() != raw:
                tmp.unlink(missing_ok=True)
                stats.errors.append(f"gzip round-trip failed: {plain}")
                return
        tmp.replace(gz_path)
        plain.unlink()
    stats.gzipped.append((str(plain), before, after))


def slim_agent_run(
    run_dir: Path,
    stats: Stats,
    *,
    apply: bool,
    gzip_llm_calls: bool,
    delete_attempts: bool,
    delete_mapped: bool,
    require_html: bool,
) -> None:
    run_dir = run_dir.resolve()
    if require_html and not _html_ok(run_dir):
        stats.skipped.append(f"no usable llm_trace.html: {run_dir}")
        return
    cases_root = run_dir / "cases"
    if not cases_root.is_dir():
        stats.skipped.append(f"no cases/: {run_dir}")
        return
    for case_dir in sorted(p for p in cases_root.iterdir() if p.is_dir()):
        stats.examined_cases += 1
        # 1) redundant stream_turnN
        if _stream_matches_turns(case_dir):
            for t in _turn_files(case_dir):
                _unlink(t, stats, apply=apply)
        # 2) stream_pi identical
        stream = case_dir / "stream.jsonl"
        stream_pi = case_dir / "stream_pi.jsonl"
        if stream.is_file() and stream_pi.is_file():
            try:
                if stream.read_bytes() == stream_pi.read_bytes():
                    _unlink(stream_pi, stats, apply=apply)
            except OSError as e:
                stats.errors.append(f"stream_pi compare {case_dir}: {e!r}")
        # 3) mapped
        if delete_mapped:
            for p in case_dir.glob("stream_turn*_mapped.jsonl"):
                _unlink(p, stats, apply=apply)
        # 4) attempts
        if delete_attempts:
            for p in case_dir.glob("stream_turn*_attempt*.jsonl"):
                _unlink(p, stats, apply=apply)
        # 5) gzip llm_calls
        if gzip_llm_calls:
            if not require_html or _html_ok(run_dir):
                _gzip_llm_calls(case_dir, stats, apply=apply)


def slim_gateway_rotated(logs_dir: Path, stats: Stats, *, apply: bool) -> None:
    """Optional: remove old rotated gateway jsonl.gz (NOT the live llm_calls.jsonl)."""
    logs_dir = logs_dir.resolve()
    live = logs_dir / "llm_calls.jsonl"
    for p in sorted(logs_dir.glob("llm_calls.jsonl.rotated_*.gz")):
        # Never touch live file
        if p.resolve() == live.resolve():
            continue
        _unlink(p, stats, apply=apply)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="Agent run dir or parent (e.g. final_merged)")
    ap.add_argument("--apply", action="store_true", help="Actually delete/gzip (default: dry-run)")
    ap.add_argument("--gzip-llm-calls", action="store_true", help="Gzip per-case llm_calls.jsonl after HTML exists")
    ap.add_argument("--keep-attempts", action="store_true", help="Keep stream_turn*_attempt*.jsonl")
    ap.add_argument("--keep-mapped", action="store_true", help="Keep stream_turn*_mapped.jsonl")
    ap.add_argument("--allow-missing-html", action="store_true", help="Slim even without llm_trace.html")
    ap.add_argument(
        "--skip-live-substr",
        action="append",
        default=None,
        help="Skip paths containing this substring (repeatable). Default: in-flight stamp.",
    )
    ap.add_argument("--allow-live", action="store_true", help="Do not skip in-flight stamp paths")
    ap.add_argument(
        "--gateway-rotated",
        action="store_true",
        help="Also delete llm_gateway/logs/llm_calls.jsonl.rotated_*.gz (keep live jsonl)",
    )
    ap.add_argument(
        "--gateway-logs-dir",
        type=Path,
        default=None,
        help="Gateway logs dir (default: <repo>/llm_gateway/logs)",
    )
    args = ap.parse_args(argv)

    skip = tuple(args.skip_live_substr) if args.skip_live_substr else DEFAULT_LIVE_SKIP
    if args.allow_live:
        skip = ()

    root = args.root.expanduser().resolve()
    root_s = str(root)
    for s in skip:
        if s and s in root_s:
            print(f"REFUSING: path looks in-flight ({s}): {root}", file=sys.stderr)
            print("Pass --allow-live if you really mean it.", file=sys.stderr)
            return 2

    stats = Stats()
    agent_dirs = _iter_agent_run_dirs(root)
    if not agent_dirs:
        print(f"No agent run dirs under {root}", file=sys.stderr)
        return 1

    for run_dir in agent_dirs:
        rs = str(run_dir)
        if any(s and s in rs for s in skip):
            stats.skipped.append(f"live-skip: {run_dir}")
            continue
        slim_agent_run(
            run_dir,
            stats,
            apply=args.apply,
            gzip_llm_calls=args.gzip_llm_calls,
            delete_attempts=not args.keep_attempts,
            delete_mapped=not args.keep_mapped,
            require_html=not args.allow_missing_html,
        )

    if args.gateway_rotated:
        logs = args.gateway_logs_dir
        if logs is None:
            # root may be eval_runs/...; repo root = parents
            # Prefer sibling of eval_runs
            cand = root
            for _ in range(6):
                if (cand / "llm_gateway" / "logs").is_dir():
                    logs = cand / "llm_gateway" / "logs"
                    break
                cand = cand.parent
        if logs is None or not logs.is_dir():
            stats.errors.append(f"gateway logs dir not found (pass --gateway-logs-dir)")
        else:
            slim_gateway_rotated(logs, stats, apply=args.apply)

    mode = "APPLY" if args.apply else "DRY-RUN"
    freed = stats.bytes_deleted + stats.bytes_gzip_saved
    print(f"[{mode}] agent_runs={len(agent_dirs)} cases={stats.examined_cases}")
    print(f"  delete_files={len(stats.deleted)} delete_bytes={stats.bytes_deleted} ({stats.bytes_deleted/1024/1024:.1f} MiB)")
    print(f"  gzip_files={len(stats.gzipped)} gzip_saved_bytes={stats.bytes_gzip_saved} ({stats.bytes_gzip_saved/1024/1024:.1f} MiB)")
    print(f"  total_freed_est={freed} ({freed/1024/1024:.1f} MiB)")
    if stats.skipped:
        print(f"  skipped={len(stats.skipped)}")
        for s in stats.skipped[:12]:
            print(f"    - {s}")
    if stats.errors:
        print(f"  errors={len(stats.errors)}")
        for s in stats.errors[:12]:
            print(f"    - {s}")
    # sample deleted names
    if stats.deleted:
        from collections import Counter
        kinds = Counter()
        for rel, _ in stats.deleted:
            name = Path(rel).name
            if name.startswith("stream_turn") and "_mapped" in name:
                kinds["stream_turn*_mapped.jsonl"] += 1
            elif name.startswith("stream_turn") and "_attempt" in name:
                kinds["stream_turn*_attempt*.jsonl"] += 1
            elif name.startswith("stream_turn"):
                kinds["stream_turnN.jsonl"] += 1
            elif name == "stream_pi.jsonl":
                kinds["stream_pi.jsonl"] += 1
            elif name == "llm_calls.jsonl":
                kinds["llm_calls.jsonl (plain after gz)"] += 1
            elif "rotated_" in name:
                kinds["gateway rotated gz"] += 1
            else:
                kinds[name] += 1
        print("  deleted_by_kind:")
        for k, n in kinds.most_common():
            print(f"    {n:4d}  {k}")
    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
