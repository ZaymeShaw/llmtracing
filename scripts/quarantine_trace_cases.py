#!/usr/bin/env python3
"""Quarantine cases whose llm_calls trace cannot be trusted (cross-talk / empty).

Root cause recap (dual_datasetA_* runs):
  insurance live QA calls reach LiteLLM(:4001) WITHOUT a case tag, while the
  concurrent Claude harness calls ARE tagged (X-Eval-Case-Id). Ingest falls back
  to a time window, so an insurance window that also contains a tagged Claude
  call yields "no exact match -> 0 calls", and a window with only untagged
  traffic can pick up a neighbouring insurance case's calls.

This tool classifies every `cases/<id>` under a run dir by checking whether each
recorded LLM call actually carries that case's own prompt in its `request.messages`
(user role). Calls that don't are foreign = cross-talk ("串").

  clean    : >=1 call, every call contains this case's prompt
  tainted  : >=1 call, at least one call belongs to another case
  empty    : 0 recorded calls

Default action quarantines `tainted` only (reversible move, never delete).
`--include-empty` also quarantines cases with no trace.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval_harness/src"))
from eval_harness.llm_gateway_ingest import read_case_llm_jsonl_text

TZ = ZoneInfo("Asia/Shanghai")


def _now_tag() -> str:
    return datetime.now(TZ).strftime("%Y%m%d_%H%M%S")


def _user_texts(record: dict) -> list[str]:
    req = record.get("request")
    if not isinstance(req, dict):
        return []
    out: list[str] = []
    for msg in req.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.append(json.dumps(content, ensure_ascii=False))
    return out


def _load_calls(path: Path) -> list[dict]:
    text = read_case_llm_jsonl_text(path.parent)
    if text is None:
        return []
    calls: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return calls


def classify_case(case_dir: Path) -> dict:
    case_id = case_dir.name
    prompt_file = case_dir / "prompt.txt"
    prompt = prompt_file.read_text(encoding="utf-8").strip() if prompt_file.is_file() else None
    calls = _load_calls(case_dir / "llm_calls.jsonl")

    own = 0
    foreign: list[dict] = []
    for rec in calls:
        users = _user_texts(rec)
        if prompt and any(prompt in text for text in users):
            own += 1
        else:
            foreign.append(
                {
                    "call_id": rec.get("call_id"),
                    "user_preview": (users[-1][:80] if users else None),
                }
            )

    if not calls:
        status = "empty"
    elif foreign:
        status = "tainted"
    else:
        status = "clean"

    return {
        "case_id": case_id,
        "status": status,
        "n_calls": len(calls),
        "n_own": own,
        "n_foreign": len(foreign),
        "foreign": foreign,
        "prompt": prompt,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="run dir containing cases/ (e.g. eval_runs/<run>/insurance)")
    ap.add_argument("--include-empty", action="store_true",
                    help="also quarantine cases with 0 recorded calls")
    ap.add_argument("--dry-run", action="store_true", help="report only, move nothing")
    ap.add_argument("--no-html", action="store_true", help="skip rebuilding llm_trace.html")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    cases_root = run_dir / "cases"
    if not cases_root.is_dir():
        print(f"ERROR: {cases_root} is not a directory", file=sys.stderr)
        return 2

    reports = [classify_case(d) for d in sorted(p for p in cases_root.iterdir() if p.is_dir())]
    by_status = {"clean": [], "tainted": [], "empty": []}
    for r in reports:
        by_status[r["status"]].append(r["case_id"])

    print(f"run_dir : {run_dir}")
    print(f"cases   : {len(reports)}  clean={len(by_status['clean'])} "
          f"tainted={len(by_status['tainted'])} empty={len(by_status['empty'])}")
    if by_status["tainted"]:
        print("tainted :")
        for r in reports:
            if r["status"] == "tainted":
                ids = ", ".join(str(f["call_id"])[:8] for f in r["foreign"])
                print(f"  - {r['case_id']:10} own={r['n_own']} foreign={r['n_foreign']} [{ids}]")

    quarantine = list(by_status["tainted"])
    if args.include_empty:
        quarantine += list(by_status["empty"])
    quarantine.sort()
    print(f"\nto quarantine ({len(quarantine)}): {', '.join(quarantine) or '(none)'}")

    report_path = run_dir / "_quarantine_report.json"
    rerun_path = run_dir / "rerun_cases.txt"
    payload = {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "include_empty": args.include_empty,
        "dry_run": args.dry_run,
        "summary": {k: len(v) for k, v in by_status.items()},
        "quarantined": quarantine,
        "cases": reports,
    }
    if not args.dry_run:
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        rerun_path.write_text("\n".join(quarantine) + ("\n" if quarantine else ""), encoding="utf-8")
        print(f"wrote   : {report_path.name}, {rerun_path.name}")

    if args.dry_run or not quarantine:
        if args.dry_run:
            print("\n[dry-run] nothing moved")
        return 0

    # ---- move case dir + sidecar json into _quarantine/cases/ (reversible) ----
    qroot = run_dir / "_quarantine" / "cases"
    qroot.mkdir(parents=True, exist_ok=True)
    for case_id in quarantine:
        for src in (cases_root / case_id, cases_root / f"{case_id}.json"):
            if src.exists():
                dst = qroot / src.name
                if dst.exists():
                    shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
                shutil.move(str(src), str(dst))
        print(f"  moved {case_id} -> _quarantine/cases/")

    # ---- filter summary.json (keep a backup) ----
    summary_path = run_dir / "summary.json"
    if summary_path.is_file():
        try:
            rows = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rows = None
        if isinstance(rows, list):
            qset = set(quarantine)
            kept = [r for r in rows if r.get("case_id") not in qset]
            summary_path.with_suffix(f".json.bak_quarantine_{_now_tag()}").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            summary_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"summary : {len(rows)} -> {len(kept)} rows")

    # ---- rebuild frontend ----
    if not args.no_html:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval_harness" / "src"))
            from eval_harness.llm_trace_html import build_llm_trace_html  # noqa: WPS433
            out = build_llm_trace_html(run_dir)
            print(f"html    : rebuilt {out}")
        except Exception as exc:  # pragma: no cover
            print(f"html    : rebuild failed: {exc!r}", file=sys.stderr)

    print("\ndone. restore with: mv _quarantine/cases/<id>* cases/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
