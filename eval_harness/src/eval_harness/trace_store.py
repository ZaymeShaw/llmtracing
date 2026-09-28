"""Durable execution exports: batch/harness/case labels never identify an attempt."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def identity_key(identity):
    fields = tuple(identity.get(k) or "" for k in ("run_id", "harness", "case_id", "execution_id"))
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:32]


def identity_start(identity):
    try:
        date = datetime.fromisoformat(str(identity.get("started_at") or "").replace("Z", "+00:00"))
        return date.replace(tzinfo=timezone.utc).timestamp() if date.tzinfo is None else date.timestamp()
    except ValueError:
        return 0


def read_identity(case_dir, calls):
    identity = {}
    for filename in ("trace.json", "meta.json", "identity.json"):
        try:
            data = json.loads((case_dir / filename).read_text())
            if not isinstance(data, dict):
                continue
            identity.update({k: data[k] for k in ("run_id", "harness", "case_id", "execution_id", "started_at") if data.get(k)})
        except (OSError, ValueError):
            pass
    for key in ("run_id", "harness", "case_id", "execution_id", "started_at"):
        values = {c.get(key) for c in calls if c.get(key)}
        if not identity.get(key) and len(values) == 1:
            identity[key] = next(iter(values))
    identity.setdefault("case_id", case_dir.parent.parent.name if case_dir.parent.name == "attempts" else case_dir.name)
    identity.setdefault("run_id", None)
    identity.setdefault("harness", None)
    starts = [c.get("ts") for c in calls if c.get("ts")]
    identity.setdefault("started_at", min(starts, key=lambda v: identity_start({"started_at": v})) if starts else "")
    return identity


def export_executions(pairs, out_dir, *, case_ids=None, run_id=None, execution_ids=None, include_orphans=False):
    from .llm_gateway_ingest import pairs_to_trace_calls, write_case_llm_jsonl
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = {}
    identities = {}
    for path in (out_dir / "cases").glob("*/identity.json"):
        saved = json.loads(path.read_text())
        if saved.get("execution_id"):
            identities[saved["execution_id"]] = tuple(saved.get(k) for k in ("run_id", "harness", "case_id"))
    for pair in pairs:
        cid = pair.get("case_id")
        if not cid and not include_orphans:
            continue
        if case_ids is not None and cid not in case_ids:
            continue
        if run_id is not None and pair.get("run_id") != run_id:
            continue
        if execution_ids is not None and pair.get("execution_id") not in execution_ids:
            continue
        # No execution id: keep calls separate rather than guessing a run boundary.
        eid = pair.get("execution_id")
        identity = {"case_id": cid or "_orphan", "run_id": pair.get("run_id"),
                    "harness": pair.get("harness"), "execution_id": eid,
                    "legacy_call_id": pair.get("call_id") if not eid else None}
        if eid:
            labels = tuple(identity.get(k) for k in ("run_id", "harness", "case_id"))
            if eid in identities and identities[eid] != labels:
                raise ValueError(f"Conflicting batch/harness/case for execution_id {eid!r}")
            identities[eid] = labels
        key = identity_key(identity) if eid else hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        groups.setdefault(key, (identity, []))[1].append(pair)
    for key, (identity, rows) in groups.items():
        case_dir = out_dir / "cases" / key
        case_dir.mkdir(parents=True, exist_ok=True)
        existing = {}
        path = case_dir / "llm_calls.jsonl"
        if path.is_file():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                existing[row["call_id"]] = row
        for row in pairs_to_trace_calls(rows, case_id=identity["case_id"]):
            old = existing.get(row["call_id"], {})
            # Incremental exports must not discard a terminal event already captured.
            existing[row["call_id"]] = {**old, **{k: v for k, v in row.items() if v is not None}}
        calls = sorted(existing.values(), key=lambda r: (identity_start({"started_at": r.get("ts")}), r["call_id"]))
        for seq, row in enumerate(calls, 1):
            row["seq"] = seq
        identity["started_at"] = min((r.get("started_at") or r.get("ts") for r in calls if r.get("started_at") or r.get("ts")), key=lambda v: identity_start({"started_at": v}), default="")
        (case_dir / "identity.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n")
        write_case_llm_jsonl(path, calls)
    return out_dir
