"""Ingest llm_gateway jsonl into Trace.llm_calls + Excel rows.

Keep each LLM call as the captured request/response payload.
Do not force Anthropic-shaped system/messages/tools columns.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ_SH = timezone(timedelta(hours=8))


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _dumps(obj: Any, *, indent: int | None = None) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)


def _raw_side(rec: dict[str, Any], *, drop: set[str]) -> dict[str, Any]:
    """Copy logged fields as-is, dropping only join/index noise."""
    return {k: v for k, v in rec.items() if k not in drop}


def load_gateway_pairs(log_path: Path, *, start_offset: int = 0, strict: bool = False) -> list[dict[str, Any]]:
    """Join per-call records from thin-proxy OR LiteLLM callback jsonl.

    Thin proxy events: request / response / response_stream / error
    LiteLLM callback events: pre_api_call / success / failure
    """
    if not log_path.is_file():
        if start_offset:
            raise FileNotFoundError(log_path)
        return []
    if log_path.stat().st_size < start_offset:
        raise RuntimeError("gateway log was truncated during case execution")
    by: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def slot_for(cid: str) -> dict[str, Any]:
        if cid not in by:
            by[cid] = {"call_id": cid}
            order.append(cid)
        return by[cid]

    with log_path.open("r", encoding="utf-8") as source:
        source.seek(start_offset)
        lines = source.readlines()
    for line_no, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(f"malformed gateway log line after offset {start_offset}, line {line_no}") from exc
            continue
        cid = rec.get("call_id")
        if not cid:
            continue
        slot = slot_for(cid)
        if rec.get("case_id") and not slot.get("case_id"):
            slot["case_id"] = rec.get("case_id")
        for key in ("execution_id", "run_id", "lane_id", "attribution_status", "case_id_source", "thinking_effective", "thinking_source"):
            if rec.get(key) is not None and slot.get(key) is None:
                slot[key] = rec[key]
        ev = rec.get("event")

        # ---- thin reverse proxy: wire JSON already in rec["request"] ----
        if ev == "request":
            req = rec.get("request")
            slot["ts"] = rec.get("ts")
            slot["path"] = rec.get("path")
            slot["method"] = rec.get("method")
            slot["protocol"] = rec.get("protocol")
            slot["model"] = rec.get("model") or (req.get("model") if isinstance(req, dict) else None)
            slot["stream"] = rec.get("stream")
            slot["request"] = req  # captured wire body as-is
            slot["source"] = "thin_proxy"
        elif ev in ("response", "response_stream"):
            slot["status_code"] = rec.get("status_code")
            slot["latency_ms"] = rec.get("latency_ms")
            if ev == "response":
                slot["response"] = rec.get("response")
            else:
                # prefer explicit stream text if present; else keep raw side fields
                if rec.get("response_text") is not None:
                    slot.setdefault("response", rec.get("response_text"))
                elif rec.get("response") is not None:
                    slot.setdefault("response", rec.get("response"))
                else:
                    slot.setdefault("response", _raw_side(rec, drop={"event", "call_id"}))
            slot.setdefault("source", "thin_proxy")
        elif ev == "error" and (slot.get("source") == "thin_proxy" or "request" in slot):
            slot["error"] = rec.get("error")
            slot.setdefault("source", "thin_proxy")

        # ---- LiteLLM callback: keep capture shape, do not reshape ----
        elif ev == "pre_api_call":
            op = rec.get("optional_params") if isinstance(rec.get("optional_params"), dict) else {}
            slot["ts"] = rec.get("ts")
            slot["model"] = rec.get("model")
            slot["stream"] = op.get("stream") if isinstance(op, dict) else None
            slot["protocol"] = rec.get("protocol") or "unknown"
            # Best-effort path hint from protocol when proxy path not present
            proto = slot["protocol"]
            if proto == "responses":
                slot["path"] = "/v1/responses"
            elif proto == "chat_completions":
                slot["path"] = "/v1/chat/completions"
            elif proto == "anthropic_messages":
                slot["path"] = "/v1/messages"
            else:
                slot["path"] = rec.get("path") or "/v1/unknown"
            slot["source"] = "litellm"
            slot["case_id_source"] = rec.get("case_id_source")
            slot["request"] = _raw_side(rec, drop={"event", "call_id"})
        elif ev == "success":
            slot["latency_ms"] = rec.get("latency_ms")
            slot["status_code"] = 200
            slot["response"] = _raw_side(rec, drop={"event", "call_id"})
            if rec.get("protocol"):
                slot["protocol"] = rec.get("protocol")
            if rec.get("case_id") and not slot.get("case_id"):
                slot["case_id"] = rec.get("case_id")
            slot.setdefault("source", "litellm")
            slot.setdefault("path", "/v1/unknown")
            slot.setdefault("model", rec.get("model"))
            slot.setdefault("ts", rec.get("ts"))
        elif ev == "failure":
            slot["error"] = rec.get("error")
            slot["status_code"] = slot.get("status_code") or 500
            if rec.get("protocol"):
                slot["protocol"] = rec.get("protocol")
            if slot.get("request") is None:
                raw = _raw_side(rec, drop={"event", "call_id", "error"})
                if raw:
                    slot["request"] = raw
            slot.setdefault("source", "litellm")
            slot.setdefault("ts", rec.get("ts"))
            slot.setdefault("model", rec.get("model"))
        elif ev == "error":
            slot["error"] = rec.get("error")

    out: list[dict[str, Any]] = []
    for cid in order:
        slot = by[cid]
        if "ts" not in slot and "request" not in slot:
            continue
        out.append(slot)
    return out


def filter_pairs_by_window(
    pairs: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    pad_sec: float = 2.0,
) -> list[dict[str, Any]]:
    lo = start - timedelta(seconds=pad_sec)
    hi = end + timedelta(seconds=pad_sec)
    selected: list[dict[str, Any]] = []
    for p in pairs:
        ts = p.get("ts")
        if not ts:
            continue
        t = _parse_ts(ts)
        if lo <= t <= hi:
            selected.append(p)
    return selected


_CASE_MARKER_RE = re.compile(r"<!--\s*eval_case_id:([A-Za-z0-9_.-]+)\s*-->")


def _case_id_from_pair(p: dict[str, Any]) -> str | None:
    if p.get("case_id"):
        return str(p["case_id"])
    req = p.get("request")
    chunks: list[str] = []
    if isinstance(req, dict):
        # LiteLLM pre_api_call shape
        op = req.get("optional_params") if isinstance(req.get("optional_params"), dict) else {}
        sys = op.get("system") if op else req.get("system")
        if isinstance(sys, str):
            chunks.append(sys)
        elif isinstance(sys, list):
            for b in sys:
                if isinstance(b, str):
                    chunks.append(b)
                elif isinstance(b, dict) and isinstance(b.get("text"), str):
                    chunks.append(b["text"])
        msgs = req.get("messages")
        if isinstance(msgs, list):
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                c = msg.get("content")
                if isinstance(c, str):
                    chunks.append(c)
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and isinstance(b.get("text"), str):
                            chunks.append(b["text"])
    for text in chunks:
        m = _CASE_MARKER_RE.search(text)
        if m:
            return m.group(1)
    return None


def filter_pairs_for_case(
    pairs: list[dict[str, Any]],
    *,
    case_id: str,
    start: datetime,
    end: datetime,
    pad_sec: float = 2.0,
) -> list[dict[str, Any]]:
    """Prefer case_id tags; fall back to time window only when no tagged calls exist.

    When any call in the window carries a case_id tag, keep only exact matches.
    Untagged foreign traffic in the same window is dropped.
    """
    windowed = filter_pairs_by_window(pairs, start=start, end=end, pad_sec=pad_sec)
    tagged: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    for p in windowed:
        cid = _case_id_from_pair(p)
        if cid:
            tagged.append(p)
            if cid == case_id:
                # stamp for downstream consumers
                p = dict(p)
                p["case_id"] = cid
                matched.append(p)
    if tagged:
        return matched
    # Legacy logs without tags: keep previous time-window behavior.
    return windowed


def filter_pairs_by_case_id(pairs: list[dict[str, Any]], *, case_id: str) -> list[dict[str, Any]]:
    """Wire-only: keep exact case_id matches; no time-window fallback."""
    out: list[dict[str, Any]] = []
    for p in pairs:
        cid = _case_id_from_pair(p)
        if cid == case_id:
            q = dict(p)
            q["case_id"] = cid
            out.append(q)
    return out


def filter_pairs_by_execution_id(
    pairs: list[dict[str, Any]], *, execution_id: str, case_id: str
) -> list[dict[str, Any]]:
    """Match gateway pairs by execution_id + case_id (lane or header-sourced)."""
    out = []
    for pair in pairs:
        if pair.get("execution_id") != execution_id:
            continue
        if pair.get("case_id") != case_id:
            raise ValueError("execution_id belongs to a different case")
        out.append(pair)
    return out


def orphan_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calls with no resolvable case_id (scheme orphan bucket)."""
    return [p for p in pairs if not _case_id_from_pair(p)]


def pairs_to_trace_calls(pairs: list[dict[str, Any]], *, case_id: str) -> list[dict[str, Any]]:
    """Trace.llm_calls: index metadata + raw request/response only."""
    calls: list[dict[str, Any]] = []
    for i, p in enumerate(pairs, start=1):
        req = p.get("request")
        calls.append(
            {
                "seq": i,
                "call_id": p.get("call_id"),
                "case_id": case_id,
                "execution_id": p.get("execution_id"),
                "run_id": p.get("run_id"),
                "lane_id": p.get("lane_id"),
                "attribution_status": p.get("attribution_status"),
                "case_id_source": p.get("case_id_source"),
                "thinking_effective": p.get("thinking_effective"),
                "thinking_source": p.get("thinking_source"),
                "ts": p.get("ts"),
                "path": p.get("path"),
                "protocol": p.get("protocol"),
                "model": p.get("model") or (req.get("model") if isinstance(req, dict) else None),
                "stream": p.get("stream"),
                "status_code": p.get("status_code"),
                "latency_ms": p.get("latency_ms"),
                "source": p.get("source"),
                "request": req,
                "response": p.get("response"),
                "error": p.get("error"),
            }
        )
    return calls


def pairs_to_excel_rows(pairs: list[dict[str, Any]], *, case_id: str) -> list[dict[str, Any]]:
    """Excel llm_calls: index cols + request/response JSON bodies."""
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(pairs, start=1):
        req = p.get("request")
        rows.append(
            {
                "case_id": case_id,
                "seq": i,
                "call_id": p.get("call_id"),
                "ts": p.get("ts"),
                "model": p.get("model") or (req.get("model") if isinstance(req, dict) else None),
                "stream": p.get("stream"),
                "path": p.get("path"),
                "protocol": p.get("protocol"),
                "status_code": p.get("status_code"),
                "latency_ms": p.get("latency_ms"),
                "request": _dumps(req, indent=2) if req is not None else "",
                "response": _dumps(p.get("response"), indent=2) if p.get("response") is not None else "",
                "error": p.get("error") or "",
            }
        )
    return rows


def case_llm_calls_path(case_dir: Path) -> Path | None:
    """Prefer plain llm_calls.jsonl; fall back to llm_calls.jsonl.gz after post-run slim."""
    plain = Path(case_dir) / "llm_calls.jsonl"
    if plain.is_file():
        return plain
    gz = Path(case_dir) / "llm_calls.jsonl.gz"
    if gz.is_file():
        return gz
    return None


def read_case_llm_jsonl_text(case_dir: Path) -> str | None:
    """Read per-case llm_calls.jsonl or llm_calls.jsonl.gz as UTF-8 text."""
    path = case_llm_calls_path(case_dir)
    if path is None:
        return None
    if path.name.endswith(".gz"):
        import gzip

        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    return path.read_text(encoding="utf-8")


def write_case_llm_jsonl(path: Path, calls: list[dict[str, Any]]) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".llm-calls-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for c in calls:
                f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def attach_llm_calls_to_trace(
    trace: dict[str, Any], calls: list[dict[str, Any]], *, embed: bool = True
) -> dict[str, Any]:
    trace = dict(trace)
    if embed:
        trace["llm_calls"] = calls
    else:
        trace.pop("llm_calls", None)
        trace["llm_calls_ref"] = "llm_calls.jsonl"
    arts = dict(trace.get("artifacts") or {})
    arts["llm_calls"] = "llm_calls.jsonl"
    trace["artifacts"] = arts
    metrics = dict(trace.get("metrics") or {})
    metrics["num_llm_calls"] = len(calls)
    trace["metrics"] = metrics
    return trace


def read_trace_llm_calls(trace: dict[str, Any], case_dir: Path) -> list[dict[str, Any]]:
    """Read either historical inline calls or the new complete per-case JSONL."""
    inline = trace.get("llm_calls")
    if isinstance(inline, list):
        return inline
    ref = trace.get("llm_calls_ref") or (trace.get("artifacts") or {}).get("llm_calls")
    if ref != "llm_calls.jsonl":
        return []
    path = case_dir / ref
    if not path.is_file():
        raise FileNotFoundError(f"missing llm call artifact: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def estimate_case_windows(
    *,
    run_started_at: datetime,
    case_ids: list[str],
    wall_ms_by_case: dict[str, int],
) -> dict[str, tuple[datetime, datetime]]:
    """Sequential cases: each window follows the previous by wall_ms."""
    cursor = run_started_at
    out: dict[str, tuple[datetime, datetime]] = {}
    for cid in case_ids:
        wall = int(wall_ms_by_case.get(cid) or 0)
        end = cursor + timedelta(milliseconds=max(wall, 0))
        out[cid] = (cursor, end)
        cursor = end
    return out
