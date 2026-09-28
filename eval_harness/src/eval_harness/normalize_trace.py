"""Normalize Claude stream events into harness-agnostic trace.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from eval_harness.adapters.base import CaseRunResult, redact_obj


def _preview(obj: Any, n: int = 800) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        s = obj
    else:
        try:
            s = json.dumps(obj, ensure_ascii=False)
        except TypeError:
            s = str(obj)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _events_from_turn(turn_index: int, raw_events: list[dict]) -> list[dict]:
    out: list[dict] = []

    def emit(kind: str, role: str, payload: dict, t_ms: Optional[int] = None) -> None:
        out.append({"t_ms": t_ms, "turn": turn_index, "role": role, "kind": kind, "payload": redact_obj(payload)})

    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        et = ev.get("type")
        if et == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "thinking":
                        emit("thinking", "assistant", {"text": block.get("thinking") or block.get("text") or ""})
                    elif bt == "text":
                        emit("text", "assistant", {"text": block.get("text") or ""})
                    elif bt == "tool_use":
                        emit("tool_use", "assistant", {"id": block.get("id"), "name": block.get("name"), "input": block.get("input")})
                    else:
                        emit(bt or "assistant_block", "assistant", block)
            continue
        if et == "user":
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        emit("tool_result", "tool", {
                            "tool_use_id": block.get("tool_use_id"),
                            "content": block.get("content"),
                            "is_error": block.get("is_error"),
                        })
                    else:
                        emit(block.get("type") or "user_block", "user", block)
            continue
        if et in ("stream_event", "partial"):
            inner = ev.get("event") if isinstance(ev.get("event"), dict) else ev
            itype = inner.get("type")
            if itype == "content_block_delta":
                delta = inner.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    emit("text_delta", "assistant", {"text": delta.get("text") or ""})
                elif dtype == "thinking_delta":
                    emit("thinking_delta", "assistant", {"text": delta.get("thinking") or delta.get("text") or ""})
                elif dtype == "input_json_delta":
                    emit("tool_input_delta", "assistant", {"partial_json": delta.get("partial_json")})
                else:
                    emit(dtype or "delta", "assistant", delta)
            elif itype == "content_block_start":
                cb = inner.get("content_block") or {}
                cbt = cb.get("type")
                if cbt == "tool_use":
                    emit("tool_use_start", "assistant", {"id": cb.get("id"), "name": cb.get("name")})
                elif cbt == "thinking":
                    emit("thinking_start", "assistant", {})
                elif cbt == "text":
                    emit("text_start", "assistant", {})
            continue
        if et == "result":
            emit("result", "system", {
                "session_id": ev.get("session_id"),
                "is_error": ev.get("is_error"),
                "duration_ms": ev.get("duration_ms"),
                "total_cost_usd": ev.get("total_cost_usd"),
                "result_preview": _preview(ev.get("result"), 500),
            })
            continue
        if et == "system":
            emit("system", "system", {"subtype": ev.get("subtype")})
            continue
        if et == "parse_error":
            emit("stderr_or_raw", "system", {"raw": ev.get("raw")})
            continue
        emit(et or "unknown", "system", ev)
    return out


def _events_from_transcript(path: Path, turn_index_hint: int = 1) -> list[dict]:
    extra: list[dict] = []
    if not path or not Path(path).is_file():
        return extra
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or obj
        content = None
        if isinstance(msg, dict):
            content = msg.get("content")
            if content is None and isinstance(msg.get("message"), dict):
                content = msg["message"].get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "thinking":
                extra.append({
                    "t_ms": None, "turn": turn_index_hint, "role": "assistant", "kind": "thinking",
                    "payload": redact_obj({"text": block.get("thinking") or block.get("text") or "", "source": "session_transcript"}),
                })
            elif bt == "tool_use":
                extra.append({
                    "t_ms": None, "turn": turn_index_hint, "role": "assistant", "kind": "tool_use",
                    "payload": redact_obj({
                        "id": block.get("id"), "name": block.get("name"), "input": block.get("input"),
                        "source": "session_transcript",
                    }),
                })
    return extra


def extract_tool_rows(events: list[dict], case_id: str) -> list[dict]:
    rows: list[dict] = []
    pending: dict[str, dict] = {}
    seq = 0
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        turn = ev.get("turn") or 1
        if kind in ("tool_use", "tool_use_start") and payload.get("name"):
            if payload.get("id") and payload["id"] in pending:
                if payload.get("input") is not None:
                    pending[payload["id"]]["input_preview"] = _preview(payload["input"], 500)
                continue
            seq += 1
            tid = payload.get("id") or f"seq-{seq}"
            row = {
                "case_id": case_id,
                "turn_index": turn,
                "seq": seq,
                "tool_name": payload.get("name"),
                "input_preview": _preview(payload.get("input"), 500),
                "output_preview": "",
            }
            pending[tid] = row
            rows.append(row)
        elif kind == "tool_result":
            tid = payload.get("tool_use_id")
            preview = _preview(payload.get("content"), 500)
            if tid and tid in pending:
                pending[tid]["output_preview"] = preview
            elif rows:
                rows[-1]["output_preview"] = preview or rows[-1]["output_preview"]
    return rows


def build_trace(case_result: CaseRunResult, harness: str = "claude_code") -> dict:
    events: list[dict] = []
    turns_out = []
    num_tool_calls = 0
    seen_tool_ids: set[str] = set()
    thinking_present = False
    first_frame_ms = None
    first_frame_kind = None
    cost_sum = 0.0
    cost_any = False
    api_ms_sum = 0
    api_any = False

    for tr in case_result.turns:
        turns_out.append({"index": tr.index, "prompt": tr.prompt, "final_text": tr.final_text})
        events.extend(_events_from_turn(tr.index, tr.raw_events))
        if tr.first_frame_ms is not None and first_frame_ms is None:
            first_frame_ms = tr.first_frame_ms
            first_frame_kind = tr.first_frame_kind
        if tr.cost_usd is not None:
            cost_sum += tr.cost_usd
            cost_any = True
        if tr.api_ms is not None:
            api_ms_sum += tr.api_ms
            api_any = True

    if case_result.transcript_path:
        has_thinking = any(e.get("kind") in ("thinking", "thinking_delta", "thinking_start") for e in events)
        if not has_thinking:
            events.extend(_events_from_transcript(Path(case_result.transcript_path)))

    for e in events:
        if e.get("kind") in ("thinking", "thinking_delta", "thinking_start"):
            thinking_present = True
        if e.get("kind") in ("tool_use", "tool_use_start"):
            tool_id = (e.get("payload") or {}).get("id")
            if not tool_id or tool_id not in seen_tool_ids:
                num_tool_calls += 1
            if tool_id:
                seen_tool_ids.add(tool_id)

    trace = {
        "schema_version": "1.0",
        "case_id": case_result.case_id,
        "harness": harness,
        "session_id": case_result.session_id,
        "turns": turns_out,
        "success": case_result.success,
        "exit_code": case_result.exit_code,
        "metrics": {
            "wall_ms": case_result.wall_ms,
            "api_ms": api_ms_sum if api_any else None,
            "first_frame_ms": first_frame_ms,
            "first_frame_kind": first_frame_kind,
            "num_turns": len(case_result.turns),
            "num_tool_calls": num_tool_calls,
            "cost_usd": cost_sum if cost_any else None,
            "thinking_present": thinking_present,
        },
        "events": events,
        "error": case_result.error,
    }
    return redact_obj(trace)


def write_trace(trace: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
