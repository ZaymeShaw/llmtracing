"""Shareable LLM trace HTML: case → call picker, one detail pane (no endless scroll)."""
from __future__ import annotations

from eval_harness.protocol_trace import request_fields, response_fields, usage_fields
from eval_harness.trace_store import identity_key, identity_start, read_identity
from eval_harness.wire_turns import observed_turns

import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Optional


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                parts.append(str(b))
                continue
            t = b.get("type")
            if t == "text":
                parts.append(str(b.get("text") or ""))
            elif t == "tool_result":
                body = b.get("content", b)
                parts.append("[tool_result]\n" + (
                    body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
                ))
            else:
                parts.append(json.dumps(b, ensure_ascii=False, default=str))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, default=str)


def _messages(messages: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(messages, list):
        return out
    for m in messages:
        if not isinstance(m, dict):
            continue
        out.append({"role": str(m.get("role") or "?"), "text": _text_from_content(m.get("content"))})
    return out


def _tool_names(tools: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(tools, list):
        return names
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))
        elif t.get("name"):
            names.append(str(t["name"]))
    return names


def _parse_model_response(s: Any) -> dict[str, Any]:
    """Parse assistant content/tool_calls/thinking from dict or legacy ModelResponse repr."""
    empty = {"content": "", "tool_calls": [], "thinking": ""}

    if isinstance(s, dict):
        return response_fields(s)

    if not isinstance(s, str) or not s:
        return dict(empty)

    content = ""
    m = re.search(r"content=(None|'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\")", s)
    if m and m.group(1) != "None":
        content = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        content = (
            content.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\'", "'")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    tools: list[dict[str, str]] = []
    for tm in re.finditer(r"Function\(arguments='((?:\\.|[^'\\])*)',\s*name='([^']+)'\)", s):
        args = tm.group(1).replace("\\n", "\n").replace("\\'", "'").replace("\\\\", "\\")
        tools.append({"name": tm.group(2), "arguments": args})
    if not tools:
        for tm in re.finditer(r"name='([^']+)'[\s\S]*?arguments='((?:\\.|[^'\\])*)'", s):
            tools.append(
                {
                    "name": tm.group(1),
                    "arguments": tm.group(2).replace("\\n", "\n").replace("\\'", "'"),
                }
            )
    thinking = ""
    tm = re.search(r"reasoning_content='((?:\\.|[^'\\])*)'", s)
    if tm:
        thinking = tm.group(1).replace("\\n", "\n").replace("\\'", "'")
    if not thinking:
        # thinking_blocks=[{'type': 'thinking', 'thinking': '...'}]
        parts = re.findall(r"'thinking'\s*:\s*'((?:\\.|[^'\\])*)'", s)
        if parts:
            thinking = "\n".join(p.replace("\\n", "\n").replace("\\'", "'") for p in parts)
    return {"content": content, "tool_calls": tools, "thinking": thinking}



def _preview(text: str, n: int = 80) -> str:
    t = (text or "").replace("\n", " ").strip()
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def _load_calls(case_dir: Path) -> list[dict[str, Any]]:
    try:
        from eval_harness.llm_gateway_ingest import read_case_llm_jsonl_text
        text = read_case_llm_jsonl_text(case_dir)
    except Exception:
        text = None
        plain = case_dir / "llm_calls.jsonl"
        if plain.is_file():
            text = plain.read_text(encoding="utf-8")
    if text is None:
        legacy_trace = case_dir / "trace.json"
        if legacy_trace.is_file():
            try:
                inline = json.loads(legacy_trace.read_text(encoding="utf-8")).get("llm_calls")
                if isinstance(inline, list):
                    return [row for row in inline if isinstance(row, dict)]
            except (OSError, ValueError, AttributeError):
                pass
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows



def _est_tokens(text: str) -> int:
    """Rough token estimate: ~1 per CJK char, ~0.3 per other char."""
    if not text or not str(text).strip():
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = max(0, len(text) - cjk)
    return max(1, int(cjk + other * 0.3))


def _parse_usage(usage) -> dict:
    """Normalize LiteLLM usage dict or Usage(...) repr."""
    extracted = usage_fields(usage)
    if extracted is not None:
        return extracted
    out = {}
    if usage is None or usage == "":
        return out
    s = str(usage)
    for k in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cached_tokens",
        "reasoning_tokens",
    ):
        m = re.search(rf"{re.escape(k)}\s*=\s*(\d+)", s)
        if m:
            out[k] = int(m.group(1))
    return out




def _structure_litellm_value(obj: Any) -> Any:
    """Turn LiteLLM ModelResponse/Usage repr (or dict) into JSON-friendly structures for Raw pane."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _structure_litellm_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_structure_litellm_value(v) for v in obj]
    if not isinstance(obj, str):
        try:
            json.dumps(obj, ensure_ascii=False)
            return obj
        except Exception:
            return str(obj)
    s = obj.strip()
    if not s:
        return s
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return _structure_litellm_value(json.loads(s))
        except Exception:
            pass
    if s.startswith("Usage(") or "completion_tokens=" in s[:80]:
        usage = _parse_usage(s)
        if usage:
            return usage
    if "ModelResponse(" in s[:40] or "ChatCompletion(" in s[:40] or "message=" in s[:200]:
        parsed = _parse_model_response(s)
        usage = _parse_usage(s)
        out: dict[str, Any] = {
            "_from_repr": True,
            "content": parsed.get("content") or "",
            "thinking": parsed.get("thinking") or "",
            "tool_calls": parsed.get("tool_calls") or [],
        }
        if usage:
            out["usage"] = usage
        m = re.search(r"id='([^']+)'", s)
        if m:
            out["id"] = m.group(1)
        m = re.search(r"model='([^']*)'", s)
        if m:
            out["model"] = m.group(1)
        out["repr_chars"] = len(s)
        return out
    return s


def _tools_brief(tools: Any, *, limit: int = 40) -> list[dict[str, Any]]:
    """Name + description only (drop giant JSON schemas) for Raw request completeness."""
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for t in tools[:limit]:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name") or t.get("name")
        if not name:
            continue
        desc = fn.get("description") or t.get("description") or ""
        out.append({"name": str(name), "description": str(desc)[:400]})
    return out



def _slim_payload(payload: Any, *, text_limit: int = 2500) -> dict[str, Any]:
    if not isinstance(payload, dict):
        if payload is None:
            return {}
        s = str(payload)
        return {"text": s if len(s) <= text_limit else s[:text_limit] + "…"}
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in ("input_schema", "parameters", "tools", "system_prompt"):
            # keep tiny proof, not giant schemas inside event stream
            if isinstance(v, (dict, list)):
                out[k + "_chars"] = len(json.dumps(v, ensure_ascii=False, default=str))
            continue
        if isinstance(v, str):
            out[k] = v if len(v) <= text_limit else v[:text_limit] + "…"
        elif isinstance(v, (bool, int, float)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _slim_payload(v, text_limit=min(800, text_limit))
        elif isinstance(v, list):
            out[k] = [
                _slim_payload(x, text_limit=400) if isinstance(x, dict) else (
                    (x if not isinstance(x, str) or len(x) <= 400 else x[:400] + "…")
                )
                for x in v[:20]
            ]
            if len(v) > 20:
                out[k + "_more"] = len(v) - 20
        else:
            s = str(v)
            out[k] = s if len(s) <= text_limit else s[:text_limit] + "…"
    return out


_OVERVIEW_EVENT_KINDS = frozenset({
    "tool_use",
    "tool_result",
    "thinking",
    "text",
    "assistant",
    "result",
    "user",
    "message",
    "llm_request",
    "llm_response",
    "llm_usage",
})


def _slim_events(events: Any, *, limit: int = 80, high_signal_only: bool = True) -> list[dict[str, Any]]:
    """Events for overview: high-signal steps only (no delta/system spam)."""
    out: list[dict[str, Any]] = []
    if not isinstance(events, list):
        return out
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = e.get("kind") or e.get("type") or ""
        if high_signal_only:
            if kind in (
                "thinking_delta", "thinking_start", "text_delta", "text_start",
                "tool_input_delta", "tool_use_start", "signature_delta",
            ):
                continue
            if kind == "system":
                payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
                subtype = payload.get("subtype")
                if subtype is None and not payload.get("result") and not payload.get("session_id"):
                    continue
                if subtype not in ("init", "result"):
                    continue
            elif kind not in _OVERVIEW_EVENT_KINDS:
                continue
        out.append(
            {
                "t_ms": e.get("t_ms"),
                "turn": e.get("turn"),
                "role": e.get("role"),
                "kind": kind,
                "payload": _slim_payload(e.get("payload")),
            }
        )
        if len(out) >= limit:
            break
    return out


def _wire_events_from_calls(normalized: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize a Trace-like event stream from llm_calls (wire-only richness)."""
    events: list[dict[str, Any]] = []
    for call in normalized:
        seq = call.get("seq")
        st = call.get("context_stats") if isinstance(call.get("context_stats"), dict) else {}
        events.append(
            {
                "t_ms": None,
                "turn": seq,
                "role": "system",
                "kind": "llm_request",
                "payload": {
                    "protocol": call.get("protocol"),
                    "model": call.get("model"),
                    "n_messages": len(call.get("messages") or []),
                    "n_tools": len(call.get("tool_names") or []),
                    "system_chars": len(call.get("system") or ""),
                    "prompt_tokens": st.get("prompt_tokens"),
                },
            }
        )
        for m in call.get("messages") or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role") or "?"
            text = m.get("text") or ""
            kind = "message"
            if role == "tool":
                kind = "tool_result"
            elif role == "system":
                kind = "system"
            events.append(
                {
                    "t_ms": None,
                    "turn": seq,
                    "role": role,
                    "kind": kind,
                    "payload": {"text": text[:2500] + ("…" if len(text) > 2500 else ""), "chars": len(text)},
                }
            )
        thinking = call.get("thinking") or ""
        if thinking.strip():
            events.append(
                {
                    "t_ms": None,
                    "turn": seq,
                    "role": "assistant",
                    "kind": "thinking",
                    "payload": {"text": thinking[:2500] + ("…" if len(thinking) > 2500 else ""), "chars": len(thinking)},
                }
            )
        for tc in call.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            events.append(
                {
                    "t_ms": None,
                    "turn": seq,
                    "role": "assistant",
                    "kind": "tool_use",
                    "payload": {"name": tc.get("name"), "arguments": tc.get("arguments")},
                }
            )
        assistant = call.get("assistant") or ""
        if assistant.strip():
            events.append(
                {
                    "t_ms": None,
                    "turn": seq,
                    "role": "assistant",
                    "kind": "assistant",
                    "payload": {"text": assistant[:2500] + ("…" if len(assistant) > 2500 else ""), "chars": len(assistant)},
                }
            )
        usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
        if usage:
            events.append(
                {
                    "t_ms": None,
                    "turn": seq,
                    "role": "system",
                    "kind": "llm_usage",
                    "payload": dict(usage),
                }
            )
        events.append(
            {
                "t_ms": None,
                "turn": seq,
                "role": "system",
                "kind": "llm_response",
                "payload": {
                    "latency_ms": call.get("latency_ms"),
                    "status_code": call.get("status_code"),
                    "error": call.get("error"),
                },
            }
        )
    return events


def _extract_thinking_texts(case_dir: Path) -> list[str]:
    """Pull thinking snippets from Trace events or sibling Claude stream jsonl."""
    found: list[str] = []

    def _from_blocks(content: Any) -> None:
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") in ("thinking", "redacted_thinking", "reasoning"):
                    t = b.get("thinking") or b.get("text") or b.get("content") or ""
                    if str(t).strip():
                        found.append(str(t))
        elif isinstance(content, dict) and content.get("thinking"):
            found.append(str(content.get("thinking")))

    trace_path = case_dir / "trace.json"
    if trace_path.is_file():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except Exception:
            trace = {}
        for e in trace.get("events") or []:
            if not isinstance(e, dict):
                continue
            kind = e.get("kind") or e.get("type")
            payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
            if kind == "thinking" or payload.get("thinking"):
                t = payload.get("thinking") or payload.get("text") or ""
                if str(t).strip():
                    found.append(str(t))
            _from_blocks(payload.get("content"))

    # sibling streams: wire/cases/ID → ../../cases/ID.jsonl or stream.jsonl in case_dir
    candidates = [
        case_dir / "stream.jsonl",
        case_dir / "events.jsonl",
    ]
    if case_dir.parent.name == "cases" and case_dir.parent.parent.name == "wire":
        candidates.append(case_dir.parent.parent.parent / "cases" / f"{case_dir.name}.jsonl")
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                msg = e.get("message") if isinstance(e.get("message"), dict) else e
                _from_blocks(msg.get("content") if isinstance(msg, dict) else None)
                if e.get("type") == "assistant":
                    _from_blocks((e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None)
        except Exception:
            pass
    # dedupe preserving order
    out: list[str] = []
    seen = set()
    for t in found:
        key = t[:200]
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _enrich_calls_thinking(case_dir: Path, calls: list[dict[str, Any]]) -> None:
    texts = _extract_thinking_texts(case_dir)
    if not texts:
        return
    ti = 0
    for call in calls:
        if (call.get("thinking") or "").strip():
            continue
        if ti >= len(texts):
            break
        call["thinking"] = texts[ti]
        ti += 1


def _load_runner_case_meta(case_dir: Path) -> dict[str, Any]:
    """Optional runner sidecar: <run>/cases/<id>.json next to wire/cases/<id>/."""
    run_root = None
    if case_dir.parent.name == "cases":
        parent = case_dir.parent.parent
        run_root = parent.parent if parent.name == "wire" else parent
    if run_root is None:
        return {}
    meta_path = run_root / "cases" / f"{case_dir.name}.json"
    if not meta_path.is_file():
        alt = run_root / "cases" / f"{case_dir.name}.meta.json"
        meta_path = alt if alt.is_file() else meta_path
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        # normalize Claude adapter meta keys
        if "final_text" not in data and data.get("final"):
            data["final_text"] = data.get("final")
        if "exit_code" not in data and data.get("exit") is not None:
            data["exit_code"] = data.get("exit")
        if "success" not in data and data.get("exit") is not None:
            try:
                data["success"] = int(data.get("exit")) == 0
            except Exception:
                pass
        return data
    except Exception:
        return {}


def _input_context_stats(*, system: str, messages: list, tools, usage: dict) -> dict:
    try:
        tools_json = json.dumps(tools, ensure_ascii=False, default=str) if tools is not None else ""
    except Exception:
        tools_json = str(tools or "")
    msg_parts = []
    for m in messages:
        role = m.get("role", "")
        body = m.get("text", "")
        msg_parts.append(f"{role}:\n{body}")
    messages_text = "\n\n".join(msg_parts)
    system = system or ""
    return {
        "system_chars": len(system),
        "system_tokens_est": _est_tokens(system),
        "messages_chars": len(messages_text),
        "messages_tokens_est": _est_tokens(messages_text),
        "messages_count": len(messages),
        "tools_chars": len(tools_json),
        "tools_tokens_est": _est_tokens(tools_json),
        "tools_count": len(tools) if isinstance(tools, list) else 0,
        "input_chars_total": len(system) + len(messages_text) + len(tools_json),
        "input_tokens_est_total": _est_tokens(system + "\n" + messages_text + "\n" + tools_json),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": usage.get("cached_tokens") or usage.get("cache_read_input_tokens"),
    }



def _normalize_call(raw: dict[str, Any]) -> dict[str, Any]:
    req = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    resp = raw.get("response") if isinstance(raw.get("response"), dict) else {}
    fields = request_fields(req)
    system = fields["system"]
    msgs = fields["messages"]
    last = msgs[-1]["text"] if msgs else ""
    response_obj = resp.get("response", raw.get("response"))
    assistant = _parse_model_response(response_obj)
    names = _tool_names(fields["tools"])
    usage = _parse_usage(resp.get("usage") or (response_obj.get("usage") if isinstance(response_obj, dict) else None))
    context_stats = _input_context_stats(
        system=system,
        messages=msgs,
        tools=fields["tools"],
        usage=usage,
    )

    ts_start = raw.get("ts") or req.get("ts")
    ts_end = resp.get("ts")
    tools_brief = _tools_brief(fields["tools"])
    return {
        "seq": raw.get("seq"),
        "call_id": raw.get("call_id"),
        **{key: raw[key] for key in ("execution_id", "run_id", "harness", "lane_id", "attribution_status") if raw.get(key) is not None},
        "protocol": raw.get("protocol") or req.get("protocol") or resp.get("protocol"),
        "model": raw.get("model") or req.get("model"),
        "latency_ms": raw.get("latency_ms") or resp.get("latency_ms"),
        "ts_start": ts_start,
        "ts_end": ts_end,
        **{k: raw.get(k, resp.get(k)) for k in ("first_frame_ms", "first_frame_kind", "first_frame_at")},
        "status_code": raw.get("status_code"),
        "error": raw.get("error"),
        "system": system,
        "messages": msgs,
        "tool_names": names,
        "assistant": assistant.get("content") or "",
        "thinking": assistant.get("thinking") or "",
        "tool_calls": assistant.get("tool_calls") or [],
        **{k: assistant.get(k) for k in ("stop_reason", "response_status", "response_id", "response_error", "incomplete_details")},
        "previous_response_id": fields["previous_response_id"],
        "usage": usage,
        "context_stats": context_stats,
        "label": f"#{raw.get('seq')} · {_preview(last or assistant.get('content') or '(empty)', 56)}",
        "raw_request": {
            **req,
            "tool_names": names,
            "n_tools": len(names),
            "tools_brief": tools_brief,
            # full tool schemas (smoke-era gap: previously dropped → thin Raw)
            "tools": fields["tools"],
        },
        "raw_response": {
            "usage": _structure_litellm_value(resp.get("usage")),
            "latency_ms": resp.get("latency_ms"),
            "response": _structure_litellm_value(response_obj),
        },
    }



def _tool_timeline_from_calls(calls: list[dict[str, Any]]) -> list[str]:
    """Rebuild tool names from wire llm_calls when harness Trace is absent."""
    names: list[str] = []
    for raw in calls:
        norm = _normalize_call(raw) if "assistant" not in raw else raw
        for tc in norm.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("name"):
                names.append(str(tc["name"]))
            if len(names) >= 80:
                return names
        # Also peek request-side OpenAI/Anthropic tool_calls in messages if present
        req = raw.get("request") if isinstance(raw.get("request"), dict) else {}
        msgs = req.get("messages") if isinstance(req.get("messages"), list) else []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict):
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                    if isinstance(fn, dict) and fn.get("name"):
                        names.append(str(fn["name"]))
                if len(names) >= 80:
                    return names
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                        names.append(str(b["name"]))
                        if len(names) >= 80:
                            return names
    return names


def _wire_only_overview(case_dir: Path, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Synthesize case overview from llm_calls when trace.json is absent."""
    normalized = [_normalize_call(c) for c in calls]
    latencies = [c.get("latency_ms") for c in normalized if isinstance(c.get("latency_ms"), (int, float))]
    # Prefer assistant tool_calls on normalized rows; fall back to raw request scan.
    tool_timeline = []
    for c in normalized:
        for tc in c.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("name"):
                tool_timeline.append(str(tc["name"]))
                if len(tool_timeline) >= 80:
                    break
        if len(tool_timeline) >= 80:
            break
    if not tool_timeline:
        tool_timeline = _tool_timeline_from_calls(calls)

    def _parse_ts(v: Any) -> float | None:
        if not v or not isinstance(v, str):
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            return None

    starts = [_parse_ts(c.get("ts_start")) for c in normalized]
    ends = []
    for c in normalized:
        e = _parse_ts(c.get("ts_end"))
        if e is None:
            s = _parse_ts(c.get("ts_start"))
            lat = c.get("latency_ms")
            if s is not None and isinstance(lat, (int, float)):
                e = s + float(lat)
        ends.append(e)
    start_vals = [x for x in starts if x is not None]
    end_vals = [x for x in ends if x is not None]
    wall_ms = None
    if start_vals and end_vals:
        wall_ms = int(max(end_vals) - min(start_vals))

    thinking_present = any(bool((c.get("thinking") or "").strip()) for c in normalized)
    if not thinking_present:
        # also peek raw response strings/dicts
        for raw in calls:
            resp = raw.get("response") if isinstance(raw.get("response"), dict) else {}
            blob = resp.get("response")
            text = blob if isinstance(blob, str) else json.dumps(blob or "", ensure_ascii=False, default=str)
            if any(k in text for k in ("reasoning_content", "thinking_blocks", "'thinking'", '"thinking"')):
                thinking_present = True
                break

    turns, main_calls, other_conversations = observed_turns(normalized)
    prompt = turns[0]["prompt"] if turns else ""
    final_text = turns[-1]["final_text"] if turns else ""

    meta = _load_runner_case_meta(case_dir)
    success = meta.get("success")
    exit_code = meta.get("exit_code")
    error = meta.get("error")
    cost = meta.get("cost_usd")
    meta_wall = meta.get("wall_ms") or meta.get("ms")
    resp_obj = meta.get("response") if isinstance(meta.get("response"), dict) else {}
    answer = meta.get("final_text") or meta.get("answer") or resp_obj.get("answer")
    if answer:
        ans = str(answer)
        if not (final_text or "").strip() or str(final_text).strip().startswith("{"):
            final_text = ans
            if turns:
                turns[-1]["final_text"] = ans
            else:
                turns = [{"index": 1, "prompt": prompt, "final_text": ans}]
    http_status = meta.get("http_status")
    if success is None and http_status is not None:
        try:
            success = int(http_status) == 200
        except Exception:
            pass
    if exit_code is None and success is True:
        exit_code = 0
    if exit_code is None and success is False:
        exit_code = 1

    note_bits = ["无 harness Trace；整案总览由中转 llm_calls 合成（wire-only）"]
    note_bits.append("turn 按请求中的用户消息历史还原；工具结果不另计一轮。首字按正文或 thinking 的首个非空白文字片段计，纯工具调用不计；中转从主会话首次上游调用前计时，跨工具调用等待至文字出现，不含 harness 启动和浏览器渲染；旧日志未采集时无法补算")
    if other_conversations:
        note_bits.append(f"总览展示调用最多的会话；另有 {other_conversations} 条独立会话，全部保留在调用明细中")
    if meta:
        note_bits.append("已合并 runner cases 元数据（answer/wall/success）")
    artifacts = {"llm_calls": "llm_calls.jsonl"}
    if meta:
        artifacts["runner_case"] = f"../../cases/{case_dir.name}.json"

    wire_events = _wire_events_from_calls(normalized)
    first_frame_ms = None
    first_frame_kind = None

    return {
        "available": True,
        "wire_only": True,
        "execution_metadata": bool(meta),
        "main_call_ids": [c.get("call_id") for c in main_calls],
        "case_id": case_dir.name,
        "success": success,
        "exit_code": exit_code,
        "error": error,
        "metrics": {
            "wall_ms": meta_wall,
            "model_span_ms": wall_ms,
            "api_ms": int(sum(latencies)) if latencies else None,
            "first_frame_ms": first_frame_ms,
            "first_frame_kind": first_frame_kind,
            "num_turns": len(turns),
            "num_tool_calls": len(tool_timeline),
            "num_llm_calls": len(normalized),
            "thinking_present": thinking_present,
            "cost_usd": cost,
        },
        "turns": turns,
        "tool_timeline": tool_timeline,
        "artifacts": artifacts,
        "n_events": len(wire_events),
        "events": _slim_events(wire_events, limit=200),
        "note": "；".join(note_bits),
    }


def _load_case_overview(case_dir: Path, *, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Slim case-level summary from Trace for the HTML "整案总览" pane.

    If trace.json is missing but llm_calls exist, fall back to wire-only overview.
    """
    path = case_dir / "trace.json"
    if not path.is_file():
        wire_calls = calls if calls is not None else _load_calls(case_dir)
        meta = None
        meta_path = case_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        insurance_meta = (
            isinstance(meta, dict)
            and meta.get("case_id") == read_identity(case_dir, wire_calls).get("case_id")
            and "attribution_status" in meta
        )
        if wire_calls:
            overview = _wire_only_overview(case_dir, wire_calls)
            if insurance_meta:
                overview["execution_metadata"] = True
                meta_turns = meta.get("turns") if isinstance(meta.get("turns"), list) else []
                overview["turns"] = [
                    {
                        "index": turn.get("index") or index,
                        "prompt": turn.get("prompt") or "",
                        "final_text": turn.get("final_text", turn.get("answer")) or "",
                    }
                    for index, turn in enumerate(meta_turns, start=1)
                    if isinstance(turn, dict)
                ] or [{
                    "index": 1, "prompt": meta.get("prompt") or "",
                    "final_text": meta.get("answer") or "",
                }]
                overview["success"] = meta.get("success")
                overview["exit_code"] = (
                    0 if meta.get("success") is True else
                    1 if meta.get("success") is False else None
                )
                overview["error"] = meta.get("error")
                overview["metrics"]["wall_ms"] = meta.get("wall_ms")
                overview["metrics"]["num_turns"] = len(overview["turns"])
                overview["artifacts"].update({
                    "business_response": "response.json", "runner_case": "meta.json",
                })
                overview["note"] += "；业务输入输出来自 Insurance 评测器产物"
                overview["attribution_status"] = meta.get("attribution_status")
            return overview
        # Insurance already persists the business request/response in its
        # runner artifacts. Show that existing evidence for zero-LLM cases;
        # never present it as a model trace or infer missing model calls.
        if insurance_meta:
            meta_turns = meta.get("turns") if isinstance(meta.get("turns"), list) else []
            turns = [
                {
                    "index": turn.get("index") or index,
                    "prompt": turn.get("prompt") or "",
                    "final_text": turn.get("final_text", turn.get("answer")) or "",
                }
                for index, turn in enumerate(meta_turns, start=1)
                if isinstance(turn, dict)
            ] or [{"index": 1, "prompt": meta.get("prompt") or "",
                   "final_text": meta.get("answer") or ""}]
            return {
                    "available": True,
                    "runner_only": True,
                    "case_id": case_dir.name,
                    "success": meta.get("success"),
                    "exit_code": None,
                    "error": meta.get("error"),
                    "metrics": {"wall_ms": meta.get("wall_ms"), "api_ms": None,
                                "first_frame_ms": None, "num_turns": len(turns),
                                "num_tool_calls": 0, "num_llm_calls": 0,
                                "thinking_present": False, "cost_usd": None},
                    "turns": turns,
                    "tool_timeline": [],
                    "artifacts": {"business_response": "response.json", "runner_case": "meta.json"},
                    "n_events": 0,
                    "events": [],
                    "note": "业务输入输出来自 Insurance 评测器产物；未观测到 LLM 调用，不能据此断言没有调用",
                    "attribution_status": meta.get("attribution_status"),
            }
        return {"available": False, "error": "无 trace.json 且无 llm_calls.jsonl"}
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"available": False, "error": f"trace.json 解析失败: {e}"}

    # Minimal adapters report execution status only. Recover their conversation
    # exactly as for direct gateway runs, while retaining harness status/timing.
    if not trace.get("turns") and not trace.get("events"):
        wire_calls = calls if calls is not None else _load_calls(case_dir)
        if wire_calls:
            overview = _wire_only_overview(case_dir, wire_calls)
            overview["execution_metadata"] = True
            overview["case_id"] = trace.get("case_id") or case_dir.name
            for key in ("success", "exit_code", "error"):
                overview[key] = trace.get(key)
            overview["metrics"]["wall_ms"] = (trace.get("metrics") or {}).get("wall_ms")
            overview["artifacts"] = {**(trace.get("artifacts") or {}), **overview["artifacts"]}
            overview["note"] = overview["note"].replace(
                "无 harness Trace；整案总览由中转 llm_calls 合成（wire-only）",
                "Adapter 提供执行状态；各轮对话和模型指标由中转 llm_calls 还原")
            return overview

    metrics_src = trace.get("metrics") if isinstance(trace.get("metrics"), dict) else {}
    metric_keys = (
        "wall_ms",
        "api_ms",
        "first_frame_ms",
        "first_frame_kind",
        "num_turns",
        "num_tool_calls",
        "num_llm_calls",
        "thinking_present",
        "cost_usd",
    )
    metrics = {k: metrics_src.get(k) for k in metric_keys}

    turns_out: list[dict[str, Any]] = []
    for t in trace.get("turns") or []:
        if not isinstance(t, dict):
            continue
        turns_out.append(
            {
                "index": t.get("index"),
                "prompt": t.get("prompt") or "",
                "final_text": t.get("final_text") or "",
            }
        )

    tool_timeline: list[str] = []
    events = trace.get("events") or []
    if isinstance(events, list):
        for e in events:
            if not isinstance(e, dict):
                continue
            if e.get("kind") != "tool_use":
                continue
            payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
            name = payload.get("name") or payload.get("tool_name")
            if name:
                tool_timeline.append(str(name))
            if len(tool_timeline) >= 80:
                break

    return {
        "available": True,
        "case_id": trace.get("case_id") or case_dir.name,
        "success": trace.get("success"),
        "exit_code": trace.get("exit_code"),
        "error": trace.get("error"),
        "metrics": metrics,
        "turns": turns_out,
        "tool_timeline": tool_timeline,
        "artifacts": trace.get("artifacts"),
        "n_events": len(events) if isinstance(events, list) else 0,
        "events": _slim_events(events, limit=200),
    }



def _clean_user_prompt(text: str) -> str:
    """Mechanically extract the user question from a wire user message.

    Does NOT rewrite wording. Only removes wrappers/harness tails.
    Returned text is taken from the wire log, not authored by the HTML builder.
    """
    if not text:
        return ""
    if text.startswith("[tool_result]"):
        return ""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
    if not text:
        return ""
    cut = re.search(r"\n(?:import |from |def |run\s*=)", text)
    if cut:
        text = text[: cut.start()].strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    picked: list[str] = []
    for p in paragraphs:
        if p.startswith("[tool_result]"):
            continue
        if p.startswith("```"):
            break
        if p.startswith("import ") or p.startswith("from ") or p.startswith("def "):
            break
        if "write_text(" in p or "subprocess" in p:
            break
        picked.append(p)
    if picked:
        return "\n\n".join(picked).strip()
    return text[:400].strip()



def _prompt_from_calls(calls: list[dict[str, Any]]) -> str:
    candidates: list[str] = []
    for c in calls:
        for m in c.get("messages") or []:
            if not isinstance(m, dict):
                continue
            if m.get("role") != "user":
                continue
            cleaned = _clean_user_prompt((m.get("text") or "").strip())
            if cleaned:
                candidates.append(cleaned)
    if not candidates:
        return ""

    def score(s: str) -> tuple:
        hit = 1 if any(k in s for k in ("查询", "保单", "查下", "多少", "？", "?")) else 0
        return (-hit, abs(len(s) - 80), len(s))

    candidates.sort(key=score)
    return candidates[0]



def _infer_first_frame(case_dir: Path, calls: list[dict[str, Any]], overview: dict[str, Any]) -> tuple[Optional[int], Optional[str]]:
    """Fill first_frame from real run artifacts (same meaning as Trace: first client-visible action).

    Priority:
    1) Trace metrics already set
    2) Claude stream-jsonl result.ttft_ms (+ kind from first assistant text/tool_use)
    3) Captured gateway first-content timestamp, relative to the main conversation start
    """
    metrics = overview.get("metrics") if isinstance(overview.get("metrics"), dict) else {}
    if metrics.get("first_frame_ms") is not None and metrics.get("first_frame_kind") in ("text", "thinking"):
        return metrics.get("first_frame_ms"), metrics.get("first_frame_kind")

    # Claude Code stream sibling: cases/<id>.jsonl
    stream = None
    if case_dir.parent.name == "cases" and case_dir.parent.parent.name == "wire":
        stream = case_dir.parent.parent.parent / "cases" / f"{case_dir.name}.jsonl"
    if stream is None:
        cand = case_dir / "stream.jsonl"
        stream = cand if cand.is_file() else None
    if stream and stream.is_file():
        ttft = None
        kind = None
        try:
            for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                if e.get("type") == "result" and e.get("ttft_ms") is not None and ttft is None:
                    try:
                        ttft = int(e["ttft_ms"])
                    except Exception:
                        pass
                if kind is None and e.get("type") == "assistant":
                    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            bt = b.get("type")
                            if bt == "tool_use":
                                kind = "tool_use"
                                break
                            if bt == "text" and (b.get("text") or "").strip():
                                kind = "text"
                                break
                            if bt == "thinking" and (b.get("thinking") or "").strip():
                                # Trace reference maps early thinking to first_frame_kind=text
                                kind = "text"
                                break
        except Exception:
            pass
        if ttft is not None and kind in ("text", "thinking"):
            return ttft, kind

    # Only measured stream timing counts; response completion is not a first frame.
    main_ids = overview.get("main_call_ids")
    main = [c for c in calls if main_ids is None or c.get("call_id") in main_ids]
    if main:
        from datetime import datetime

        def seconds(value):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except (AttributeError, TypeError, ValueError):
                return None

        start = seconds(main[0].get("ts_start"))
        for call in main:
            measured = call.get("first_frame_ms")
            kind = call.get("first_frame_kind")
            if kind in ("text", "thinking") and isinstance(measured, (int, float)) and measured >= 0:
                if call is main[0]:
                    return round(measured), kind
                at = seconds(call.get("first_frame_at"))
                call_start = seconds(call.get("ts_start"))
                if at is None and call_start is not None:
                    at = call_start + measured / 1000
                if start is not None and at is not None and at >= start:
                    return round((at - start) * 1000), kind
                return None, None
            # Older logs may have text but only a tool timestamp. A later call's
            # text must not masquerade as this execution's first character.
            if (call.get("assistant") or "").strip() or (call.get("thinking") or "").strip():
                return None, None

    return None, None



def _enrich_overview(overview: dict[str, Any], calls: list[dict[str, Any]], case_dir: Path) -> dict[str, Any]:
    """Fill 整案总览 gaps; keep events high-signal."""
    if not overview.get("available"):
        return overview
    metrics = overview.get("metrics") if isinstance(overview.get("metrics"), dict) else {}
    if calls:
        metrics["num_llm_calls"] = len(calls)
        lats = [c.get("latency_ms") for c in calls if isinstance(c.get("latency_ms"), (int, float))]
        if metrics.get("api_ms") is None and lats:
            metrics["api_ms"] = int(sum(lats))
        if metrics.get("thinking_present") is None:
            metrics["thinking_present"] = any(bool((c.get("thinking") or "").strip()) for c in calls)
        if metrics.get("num_tool_calls") is None:
            n = 0
            for c in calls:
                n += len(c.get("tool_calls") or [])
            metrics["num_tool_calls"] = n

    ff_ms, ff_kind = _infer_first_frame(case_dir, calls, overview)
    metrics["first_frame_ms"] = ff_ms
    metrics["first_frame_kind"] = ff_kind

    # Claude stream-jsonl result: authoritative wall/api/ttft/turns when present
    stream = None
    if case_dir.parent.name == "cases" and case_dir.parent.parent.name == "wire":
        stream = case_dir.parent.parent.parent / "cases" / f"{case_dir.name}.jsonl"
    if stream and stream.is_file():
        try:
            for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"type":"result"' not in line and '"type": "result"' not in line:
                    continue
                e = json.loads(line)
                if e.get("duration_api_ms") is not None:
                    metrics["api_ms"] = int(e["duration_api_ms"])
                if e.get("duration_ms") is not None:
                    metrics["wall_ms"] = int(e["duration_ms"])
                if e.get("num_turns") is not None:
                    metrics["num_turns"] = int(e["num_turns"])
                if e.get("total_cost_usd") is not None and metrics.get("cost_usd") is None:
                    metrics["cost_usd"] = float(e["total_cost_usd"])
                break
        except Exception:
            pass

    turns = overview.get("turns") if isinstance(overview.get("turns"), list) else []
    if not turns and calls:
        turns = [{"index": 1, "prompt": "", "final_text": ""}]
    if turns:
        t0 = turns[0] if isinstance(turns[0], dict) else {"index": 1, "prompt": "", "final_text": ""}
        if (t0.get("prompt") or "").strip():
            cleaned = _clean_user_prompt(str(t0.get("prompt") or ""))
            if cleaned:
                t0["prompt"] = cleaned
        if not (t0.get("prompt") or "").strip():
            prompt = _prompt_from_calls(calls)
            if not prompt:
                meta = _load_runner_case_meta(case_dir)
                prompt = str(meta.get("prompt") or meta.get("user") or "")
            if not prompt:
                stream = None
                if case_dir.parent.name == "cases" and case_dir.parent.parent.name == "wire":
                    stream = case_dir.parent.parent.parent / "cases" / f"{case_dir.name}.jsonl"
                if stream and stream.is_file():
                    for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        if e.get("type") != "user":
                            continue
                        msg = e.get("message") if isinstance(e.get("message"), dict) else e
                        content = msg.get("content") if isinstance(msg, dict) else None
                        prompt = _text_from_content(content)
                        if prompt and not (prompt.startswith("<system-reminder>") and len(prompt) > 500):
                            break
                        if prompt.startswith("<system-reminder>"):
                            # keep looking for a shorter user question inside
                            continue
            t0["prompt"] = prompt or t0.get("prompt") or ""
        if not overview.get("wire_only") and not (t0.get("final_text") or "").strip():
            for c in reversed(calls):
                if (c.get("assistant") or "").strip():
                    t0["final_text"] = c["assistant"]
                    break
            if not (t0.get("final_text") or "").strip():
                meta = _load_runner_case_meta(case_dir)
                ans = meta.get("final_text") or meta.get("final") or meta.get("answer")
                resp = meta.get("response") if isinstance(meta.get("response"), dict) else {}
                ans = ans or resp.get("answer")
                if ans:
                    t0["final_text"] = str(ans)
        turns[0] = t0
    overview["turns"] = turns
    overview["metrics"] = metrics
    # provenance: prompt is extracted from wire, not authored here

    if not overview.get("tool_timeline"):
        names = []
        for c in calls:
            for tc in c.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("name"):
                    names.append(str(tc["name"]))
        overview["tool_timeline"] = names

    raw_events = overview.get("events")
    if isinstance(raw_events, list) and raw_events:
        overview["events"] = _slim_events(raw_events, limit=60, high_signal_only=True)
    return overview


def build_payload(run_dir: Path) -> dict[str, Any]:
    cases_root = Path(run_dir) / "cases"
    cases: list[dict[str, Any]] = []
    if cases_root.is_dir():
        directories = list(p for p in cases_root.iterdir() if p.is_dir())
        directories += [p for parent in list(directories) for p in (parent / "attempts").glob("*") if p.is_dir()]
        for case_dir in sorted(directories):
            raw_calls = _load_calls(case_dir)
            if not raw_calls and not any((case_dir / name).is_file() for name in ("meta.json", "trace.json", "identity.json")):
                continue
            identity = read_identity(case_dir, raw_calls)
            calls = [_normalize_call(c) for c in raw_calls]
            # Context growth: delta vs previous LLM call (scheme wire-only must-have)
            for i, call in enumerate(calls):
                st = call.get("context_stats") if isinstance(call.get("context_stats"), dict) else {}
                if i == 0:
                    call["context_growth"] = {
                        "is_first": True,
                        "messages_count_delta": 0,
                        "messages_chars_delta": 0,
                        "prompt_tokens_delta": None,
                        "input_chars_total_delta": 0,
                    }
                    continue
                prev = calls[i - 1].get("context_stats") if isinstance(calls[i - 1].get("context_stats"), dict) else {}

                def _di(a, b):
                    if a is None or b is None:
                        return None
                    try:
                        return int(a) - int(b)
                    except Exception:
                        return None

                call["context_growth"] = {
                    "is_first": False,
                    "messages_count_delta": _di(st.get("messages_count"), prev.get("messages_count")),
                    "messages_chars_delta": _di(st.get("messages_chars"), prev.get("messages_chars")),
                    "prompt_tokens_delta": _di(st.get("prompt_tokens"), prev.get("prompt_tokens")),
                    "input_chars_total_delta": _di(st.get("input_chars_total"), prev.get("input_chars_total")),
                }
            _enrich_calls_thinking(case_dir, calls)
            overview = _load_case_overview(case_dir, calls=raw_calls)
            if overview.get("wire_only"):
                wire_events = _wire_events_from_calls(calls)
                overview["n_events"] = len(wire_events)
                overview["events"] = wire_events
            overview = _enrich_overview(overview, calls, case_dir)
            cases.append(
                {
                    "id": identity_key(identity) if identity.get("execution_id") else case_dir.name,
                    "identity": identity,
                    "artifact_path": str(case_dir.relative_to(run_dir)),
                    "label": " / ".join(str(identity.get(k) or "未标记") for k in ("run_id", "harness", "case_id")),
                    "n_calls": len(calls),
                    "preview": calls[0]["label"] if calls else "评测器输入输出；模型调用未观测到",
                    "calls": calls,
                    "overview": overview,
                }
            )
    groups = {}
    for case in cases:
        ident = case["identity"]
        key = tuple(ident.get(k) for k in ("run_id", "harness", "case_id"))
        if not ident.get("execution_id") or not ident.get("run_id"):
            key += (case["id"],)  # Unattributed legacy calls are not inferred retries.
        groups.setdefault(key, []).append(case)
    for rows in groups.values():
        rows.sort(key=lambda c: (identity_start(c["identity"]), c["id"]))
        for index, case in enumerate(rows):
            case["attempt"] = index + 1
            case["latest"] = index == len(rows) - 1
    return {"run_id": Path(run_dir).name, "cases": cases}


def build_llm_trace_html(run_dir: Path, *, title: Optional[str] = None) -> Path:
    run_dir = Path(run_dir)
    payload = build_payload(run_dir)
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    # Prevent </script> breakout
    data_json = data_json.replace("<", "\\u003c").replace(">", "\\u003e")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title or payload['run_id'])} · LLM Trace</title>
<style>
  :root {{
    --bg: #f4f5f7; --panel: #fff; --line: #e3e5e8; --text: #1a1a1a;
    --muted: #6b7280; --accent: #2563eb; --accent-soft: #eff6ff;
    --ok: #059669; --warn: #b45309;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
    color: var(--text); background: var(--bg); height: 100vh; overflow: hidden;
  }}
  .app {{
    display: grid; grid-template-columns: 220px 280px 1fr; height: 100vh;
  }}
  aside, .calls, .detail {{ background: var(--panel); overflow: auto; }}
  aside {{ border-right: 1px solid var(--line); }}
  .calls {{ border-right: 1px solid var(--line); }}
  .head {{
    position: sticky; top: 0; background: var(--panel); z-index: 1;
    padding: 12px 14px; border-bottom: 1px solid var(--line);
  }}
  .head h1 {{ margin: 0; font-size: 14px; }}
  .head .sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
  .search {{
    width: 100%; margin-top: 8px; padding: 6px 8px; border: 1px solid var(--line);
    border-radius: 6px; font-size: 13px;
  }}
  .list {{ list-style: none; margin: 0; padding: 8px; }}
  .list li {{
    padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 4px;
    font-size: 13px; line-height: 1.35;
  }}
  .list li:hover {{ background: #f3f4f6; }}
  .list li.active {{ background: var(--accent-soft); color: var(--accent); font-weight: 600; }}
  .list .meta {{ display: block; color: var(--muted); font-size: 11px; font-weight: 400; margin-top: 2px; }}
  .detail {{ display: flex; flex-direction: column; min-width: 0; }}
  .tabs {{
    display: flex; gap: 4px; padding: 10px 14px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: var(--panel); z-index: 1;
  }}
  .tab {{
    border: 0; background: transparent; padding: 8px 12px; border-radius: 8px;
    cursor: pointer; font-size: 13px; color: var(--muted);
  }}
  .tab.active {{ background: var(--accent-soft); color: var(--accent); font-weight: 600; }}
  .pane {{ display: none; padding: 14px 18px 40px; overflow: auto; flex: 1; }}
  .pane.active {{ display: block; }}
  .msg {{
    border: 1px solid var(--line); border-radius: 10px; margin: 0 0 10px; overflow: hidden;
  }}
  .msg .role {{
    background: #f9fafb; padding: 6px 10px; font-size: 11px; text-transform: uppercase;
    color: var(--muted); border-bottom: 1px solid var(--line);
  }}
  .msg pre, .block pre {{
    margin: 0; padding: 10px 12px; white-space: pre-wrap; word-break: break-word;
    font-size: 12.5px; line-height: 1.45; max-height: 420px; overflow: auto;
  }}
  .assistant {{
    background: var(--accent-soft); border: 1px solid #bfdbfe; border-radius: 10px;
  }}
  .tool {{
    background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px; margin: 8px 0;
    padding: 8px 10px;
  }}
  .tool code {{ font-size: 12px; }}
  .stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 8px; margin: 0 0 12px; padding: 10px 12px;
    background: #f8fafc; border: 1px solid var(--line); border-radius: 10px;
    font-size: 12px; line-height: 1.45;
  }}
  .stats > div {{ background: #fff; border: 1px solid #eef2f7; border-radius: 8px; padding: 8px 10px; }}
  .stats .k {{ color: var(--muted); font-size: 11px; }}
  .stats .v {{ font-weight: 600; margin-top: 2px; }}
  .stats .note {{ grid-column: 1 / -1; color: var(--muted); font-size: 11px; background: transparent; border: 0; padding: 0; }}
  .empty {{ color: var(--muted); padding: 24px; }}
  .chip {{
    display: inline-block; background: #f3f4f6; border-radius: 999px; padding: 2px 8px;
    font-size: 11px; margin: 2px 4px 2px 0; color: #374151;
  }}
  .bar {{
    padding: 10px 18px; border-bottom: 1px solid var(--line); font-size: 13px;
    background: #fafafa;
  }}
  .timeline {{
    border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
    margin: 0 0 12px; background: #fff;
  }}
  .timeline h3 {{ margin: 0 0 8px; font-size: 12px; color: var(--muted); font-weight: 600; }}
  .tl-row {{
    display: grid; grid-template-columns: 72px 1fr 170px; gap: 8px;
    align-items: center; margin: 5px 0; font-size: 12px;
  }}
  .tl-track {{ position: relative; height: 16px; background: #eef2f7; border-radius: 4px; overflow: hidden; }}
  .tl-bar {{ position: absolute; top: 2px; bottom: 2px; background: #2563eb; border-radius: 3px; opacity: 0.9; min-width: 2px; }}
  .tl-meta {{ color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; font-size: 11px; }}
  .tl-axis {{ font-size: 11px; color: var(--muted); display: flex; justify-content: space-between; margin-top: 6px; font-variant-numeric: tabular-nums; }}
  @media (max-width: 900px) {{
    .app {{ grid-template-columns: 1fr; grid-template-rows: 160px 140px 1fr; }}
  }}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="head">
      <h1>Cases</h1>
      <div class="sub" id="runMeta"></div>
      <select class="search" id="batchFilter" aria-label="批次"><option value="">全部批次</option></select>
      <input class="search" id="caseFilter" placeholder="筛选题目 / harness…"/>
      <label><input type="checkbox" id="showHistory"/> 显示历史尝试</label>
    </div>
    <ul class="list" id="caseList"></ul>
  </aside>
  <div class="calls">
    <div class="head">
      <h1>调用</h1>
      <div class="sub" id="callMeta">先选左侧 case</div>
    </div>
    <ul class="list" id="callList"></ul>
  </div>
  <section class="detail">
    <div class="bar" id="detailBar">选择 case 查看整案总览，或点选 LLM 调用</div>
    <div class="tabs" id="tabs">
      <button class="tab active" data-tab="context">上下文 messages</button>
      <button class="tab" data-tab="reply">模型回复</button>
      <button class="tab" data-tab="tools">工具列表</button>
      <button class="tab" data-tab="raw">Raw JSON</button>
    </div>
    <div class="pane active" id="pane-context"></div>
    <div class="pane" id="pane-reply"></div>
    <div class="pane" id="pane-tools"></div>
    <div class="pane" id="pane-raw"></div>
  </section>
</div>
<script id="DATA" type="application/json">{data_json}</script>
<script>
const DATA = JSON.parse(document.getElementById('DATA').textContent);
const batchFilter = document.getElementById('batchFilter');
for (const run of [...new Set(DATA.cases.map(c => c.identity?.run_id || '未标记批次'))]) {{
  const opt = document.createElement('option'); opt.value = run; opt.textContent = run; batchFilter.appendChild(opt);
}}
function visibleCases() {{
  const batch = batchFilter.value;
  const history = document.getElementById('showHistory').checked;
  return DATA.cases.filter(c => (!batch || (c.identity?.run_id || '未标记批次') === batch) && (history || c.latest !== false));
}}
function caseLabel(c) {{ return (c.label || c.id) + (c.attempt > 1 || c.latest === false ? ' · 尝试 ' + c.attempt : ''); }}
let caseIdx = -1, callIdx = -1; // -1 = 整案总览

const caseList = document.getElementById('caseList');
const callList = document.getElementById('callList');
const runMeta = document.getElementById('runMeta');
runMeta.textContent = DATA.run_id + ' · ' + visibleCases().length + ' / ' + DATA.cases.length + ' 次执行';

function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}


function parseTs(s) {{
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
}}
function fmtClock(ms) {{
  if (ms == null) return '-';
  const d = new Date(ms);
  const p = n => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}}
function fmtDur(ms) {{
  if (ms == null || ms === '') return '-';
  const n = Number(ms);
  if (!Number.isFinite(n)) return '-';
  if (Math.abs(n) < 1000) return Math.round(n) + 'ms';
  return (n / 1000).toFixed(n < 10000 ? 2 : 1) + 's';
}}
function callSpan(call) {{
  const start = parseTs(call.ts_start);
  let end = parseTs(call.ts_end);
  if (start != null && end == null && call.latency_ms != null) end = start + Number(call.latency_ms);
  const latency = call.latency_ms != null ? Number(call.latency_ms) : (start != null && end != null ? end - start : null);
  return {{ start, end, latency }};
}}
function renderCallTimeline(calls, title) {{
  const spans = (calls || []).map(call => ({{ call, ...callSpan(call) }})).filter(x => x.start != null);
  if (!spans.length) {{
    return '<div class="timeline"><h3>' + esc(title || 'LLM timeline') + '</h3><div class="empty">no timestamps</div></div>';
  }}
  const t0 = Math.min(...spans.map(s => s.start));
  const t1 = Math.max(...spans.map(s => (s.end != null ? s.end : s.start)));
  const span = Math.max(1, t1 - t0);
  let html = '<div class="timeline"><h3>' + esc(title || 'LLM timeline') + '</h3>';
  spans.forEach((s, idx) => {{
    const left = ((s.start - t0) / span) * 100;
    const width = Math.max(0.8, (((s.end != null ? s.end : s.start) - s.start) / span) * 100);
    let gapTxt = '';
    if (idx > 0 && spans[idx - 1].end != null) gapTxt = ' | gap ' + fmtDur(s.start - spans[idx - 1].end);
    html += '<div class="tl-row"><code>#' + esc(s.call.seq) + '</code>';
    html += '<div class="tl-track"><div class="tl-bar" style="left:' + left + '%;width:' + width + '%"></div></div>';
    html += '<span class="tl-meta">' + fmtClock(s.start) + ' -> ' + fmtClock(s.end) + ' | ' + fmtDur(s.latency) + gapTxt + '</span></div>';
  }});
  html += '<div class="tl-axis"><span>' + fmtClock(t0) + '</span><span>' + fmtDur(span) + '</span><span>' + fmtClock(t1) + '</span></div></div>';
  return html;
}}
function renderRunTimeline() {{
  const rows = [];
  for (const c of visibleCases()) {{
    for (const call of (c.calls || [])) {{
      const sp = callSpan(call);
      if (sp.start == null) continue;
      rows.push({{ case_id: caseLabel(c), seq: call.seq, ...sp }});
    }}
  }}
  rows.sort((a, b) => a.start - b.start);
  if (!rows.length) {{
    return '<div class="timeline"><h3>Run LLM timeline</h3><div class="empty">no timestamps</div></div>';
  }}
  const t0 = rows[0].start;
  const t1 = Math.max(...rows.map(r => (r.end != null ? r.end : r.start)));
  const span = Math.max(1, t1 - t0);
  let html = '<div class="timeline"><h3>Run LLM timeline (wall clock)</h3>';
  let prevEnd = null;
  for (const r of rows) {{
    const left = ((r.start - t0) / span) * 100;
    const width = Math.max(0.8, (((r.end != null ? r.end : r.start) - r.start) / span) * 100);
    const gapTxt = prevEnd != null
      ? (r.start < prevEnd ? ' | 并行重叠 ' + fmtDur(prevEnd - r.start) : ' | 间隔 ' + fmtDur(r.start - prevEnd))
      : '';
    html += '<div class="tl-row"><code>' + esc(r.case_id) + '#' + esc(r.seq) + '</code>';
    html += '<div class="tl-track"><div class="tl-bar" style="left:' + left + '%;width:' + width + '%"></div></div>';
    html += '<span class="tl-meta">' + fmtClock(r.start) + ' -> ' + fmtClock(r.end) + ' | ' + fmtDur(r.latency) + gapTxt + '</span></div>';
    if (r.end != null) prevEnd = r.end;
  }}
  html += '<div class="tl-axis"><span>' + fmtClock(t0) + '</span><span>' + fmtDur(span) + '</span><span>' + fmtClock(t1) + '</span></div></div>';
  html += '<div class="msg"><div class="role">case windows</div><pre>';
  for (const c of visibleCases()) {{
    const spans = (c.calls || []).map(callSpan).filter(s => s.start != null);
    if (!spans.length) {{ html += esc(caseLabel(c)) + ': -\\\n'; continue; }}
    const a = Math.min(...spans.map(s => s.start));
    const b = Math.max(...spans.map(s => (s.end != null ? s.end : s.start)));
    html += esc(caseLabel(c)) + ': ' + fmtClock(a) + ' -> ' + fmtClock(b) + ' (span ' + fmtDur(b - a) + ', ' + spans.length + ' calls)\\\n';
  }}
  html += '</pre></div>';
  return html;
}}

function setOverviewTabs(isOverview) {{
  const map = isOverview
    ? {{ context: '各轮输入/输出', reply: '最终输出', tools: '工具摘要', raw: 'Trace JSON' }}
    : {{ context: '上下文 messages', reply: '模型回复', tools: '工具列表', raw: 'Raw JSON' }};
  document.querySelectorAll('.tab').forEach(t => {{
    const k = t.dataset.tab;
    if (map[k]) t.textContent = map[k];
  }});
}}

function renderCases(filter = '') {{
  runMeta.textContent = DATA.run_id + ' · ' + visibleCases().length + ' / ' + DATA.cases.length + ' 次执行';
  const q = filter.trim().toLowerCase();
  caseList.innerHTML = '';
  if (!q) {{
    const li = document.createElement('li');
    if (caseIdx === -1) li.classList.add('active');
    li.innerHTML = '<strong>模型调用时间线</strong><span class="meta">' + visibleCases().length + ' 次执行</span>';
    li.onclick = () => {{
      caseIdx = -1; callIdx = -1;
      location.hash = 'timeline';
      renderCases(document.getElementById('caseFilter').value);
      renderCalls(); renderDetail();
    }};
    caseList.appendChild(li);
  }}
  // Execution directories are hashes; display logical case IDs in natural order.
  const ordered = DATA.cases.map((c, i) => ({{ c, i }})).sort((a, b) => {{
    for (const key of ['run_id', 'harness', 'case_id']) {{
      const cmp = String((a.c.identity || {{}})[key] || '').localeCompare(
        String((b.c.identity || {{}})[key] || ''), undefined, {{ numeric: true }});
      if (cmp) return cmp;
    }}
    return (b.c.attempt || 0) - (a.c.attempt || 0);
  }});
  ordered.forEach(({{ c, i }}) => {{
    if (!visibleCases().includes(c)) return;
    if (q && !caseLabel(c).toLowerCase().includes(q)) return;
    const li = document.createElement('li');
    li.dataset.i = i;
    if (i === caseIdx) li.classList.add('active');
    const turns = ((c.overview || {{}}).turns || []).length;
    li.innerHTML = `<strong>${{esc(caseLabel(c))}}</strong><span class="meta">${{turns ? turns + ' 轮 · ' : ''}}${{c.n_calls}} calls${{c.latest && c.attempt > 1 ? " · 最新" : ""}} · ${{esc(c.preview)}}</span>`;
    li.onclick = () => {{
      caseIdx = i; callIdx = -1;
      location.hash = DATA.cases[i].id + '/overview';
      renderCases(document.getElementById('caseFilter').value);
      renderCalls(); renderDetail();
    }};
    caseList.appendChild(li);
  }});
}}

function renderCalls() {{
  const c = caseIdx >= 0 ? DATA.cases[caseIdx] : null;
  callList.innerHTML = '';
  if (caseIdx < 0) {{
    document.getElementById('callMeta').textContent = '全量时间线';
    const li = document.createElement('li');
    li.classList.add('active');
    li.innerHTML = '<strong>Run timeline</strong><span class="meta">all LLM calls</span>';
    callList.appendChild(li);
    return;
  }}
  document.getElementById('callMeta').textContent = c ? (caseLabel(c) + ' · overview') : 'pick case';
  if (!c) return;

  const ovLi = document.createElement('li');
  if (callIdx < 0) ovLi.classList.add('active');
  const m = (c.overview && c.overview.metrics) || {{}};
  const ok = c.overview && c.overview.success;
  const okLabel = ok === true ? 'OK' : (ok === false ? 'FAIL' : '—');
  ovLi.innerHTML = `<strong>整案总览</strong><span class="meta">${{okLabel}} · wall=${{m.wall_ms != null ? m.wall_ms + 'ms' : '—'}} · first=${{m.first_frame_ms != null ? m.first_frame_ms + 'ms' : '—'}} · llm=${{m.num_llm_calls ?? c.n_calls}}</span>`;
  ovLi.onclick = () => {{ callIdx = -1; location.hash = c.id + '/overview'; renderCalls(); renderDetail(); }};
  callList.appendChild(ovLi);

  c.calls.forEach((call, i) => {{
    const li = document.createElement('li');
    if (i === callIdx) li.classList.add('active');
    const sp = callSpan(call);
    const label = String(call.label || '').replace(/^#\\d+ · /, '');
    li.innerHTML = '<strong>LLM #' + esc(call.seq) + '</strong> ' + esc(label)
      + '<span class="meta">' + fmtClock(sp.start) + '→' + fmtClock(sp.end) + ' · ' + fmtDur(sp.latency)
      + ' · ' + esc(call.model || '') + ' · msgs=' + String((call.messages || []).length) + '</span>';
    li.onclick = () => {{ callIdx = i; location.hash = c.id + '/' + call.seq; renderCalls(); renderDetail(); }};
    callList.appendChild(li);
  }});
}}

function setTab(name) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.id === 'pane-' + name));
}}

document.getElementById('tabs').onclick = (e) => {{
  const btn = e.target.closest('.tab');
  if (btn) setTab(btn.dataset.tab);
}};

document.getElementById('caseFilter').oninput = (e) => renderCases(e.target.value);
batchFilter.onchange = document.getElementById('showHistory').onchange = () => {{ caseIdx = -1; callIdx = -1; renderCases(document.getElementById('caseFilter').value); renderCalls(); renderDetail(); }};

function renderOverview(c) {{
  const bar = document.getElementById('detailBar');
  const ctx = document.getElementById('pane-context');
  const reply = document.getElementById('pane-reply');
  const tools = document.getElementById('pane-tools');
  const raw = document.getElementById('pane-raw');
  const ov = c.overview || {{}};
  const m = ov.metrics || {{}};
  const tok = (n) => (n == null || n === '' ? '—' : Number(n).toLocaleString());
  setOverviewTabs(true);
  if (!ov.available) {{
    bar.textContent = caseLabel(c) + ' · 整案总览不可用';
    ctx.innerHTML = `<div class="empty">${{esc(ov.error || '无 case 总览')}}</div>`;
    reply.innerHTML = tools.innerHTML = raw.innerHTML = '<div class="empty">—</div>';
    setTab('context');
    return;
  }}
  const statusHtml = ov.success === true
    ? '<span style="color:#059669">OK</span>'
    : (ov.success === false
      ? '<span style="color:#b91c1c">FAIL</span>'
      : '<span style="color:#64748b">—</span>');
  bar.innerHTML = `<strong>${{esc(caseLabel(c))}}</strong> · 整案总览 · `
    + statusHtml
    + (ov.wire_only && !ov.execution_metadata ? " · 模型调用观测（执行状态未上报）" : "")
    + ` · exit=${{ov.exit_code == null ? '—' : esc(ov.exit_code)}}`
    + ` · wall=${{tok(m.wall_ms)}}ms · api=${{tok(m.api_ms)}}ms`
    + ` · first_frame=${{tok(m.first_frame_ms)}}ms`
    + (m.first_frame_kind ? ` (${{esc(m.first_frame_kind)}})` : '')
    + ` · llm=${{tok(m.num_llm_calls ?? c.n_calls)}} · tools=${{tok(m.num_tool_calls)}}`;

  let ctxHtml = `<div class="stats">
    <div><div class="k">${{ov.wire_only && !ov.execution_metadata ? "模型调用跨度 ms" : "wall_ms"}}</div><div class="v">${{tok(ov.wire_only && !ov.execution_metadata ? m.model_span_ms : m.wall_ms)}}</div></div>
    <div><div class="k">api_ms</div><div class="v">${{tok(m.api_ms)}}</div></div>
    <div title="正文或 thinking 的首个非空白文字片段；纯工具调用不算。${{ov.wire_only ? '从主会话首次上游调用前计时，包含文字出现前的工具等待；测量点在中转，不是浏览器渲染。' : '从 harness 调用前计时，到 CLI 输出文字事件；测量点在 CLI，不是浏览器渲染。'}}"><div class="k">首字耗时 ms${{ov.wire_only ? " · 中转" : " · harness"}}</div><div class="v">${{m.first_frame_ms == null ? "未采集" : tok(m.first_frame_ms)}}${{m.first_frame_kind ? ' (' + esc(m.first_frame_kind) + ')' : ''}}</div></div>
    <div><div class="k">num_turns</div><div class="v">${{tok(m.num_turns)}}</div></div>
    <div><div class="k">num_tool_calls</div><div class="v">${{tok(m.num_tool_calls)}}</div></div>
    <div><div class="k">num_llm_calls</div><div class="v">${{tok(m.num_llm_calls ?? c.n_calls)}}</div></div>
    <div><div class="k">thinking_present</div><div class="v">${{esc(String(m.thinking_present))}}</div></div>
    <div><div class="k">cost_usd</div><div class="v">${{m.cost_usd != null ? Number(m.cost_usd).toFixed(6) : '—'}}</div></div>
    <div><div class="k">n_events</div><div class="v">${{tok(ov.n_events)}}</div></div>
  </div>`;
  ctxHtml += renderCallTimeline(c.calls, 'case LLM timeline');
  if (ov.attribution_status && ov.note) ctxHtml += `<p class="sub">${{esc(ov.note)}}</p>`;
  if (ov.error) ctxHtml += `<div class="tool"><strong>error</strong><pre>${{esc(ov.error)}}</pre></div>`;
  const turns = ov.turns || [];
  if (!turns.length) {{
    ctxHtml += '<div class="empty">无 turns（缺 prompt/final）</div>';
  }} else {{
    turns.forEach(t => {{
      ctxHtml += `<div class="msg"><div class="role">${{"turn " + esc(t.index) + " · 输入 prompt"}}</div><pre>${{esc(t.prompt || '')}}</pre></div>`;
      ctxHtml += `<div class="msg assistant"><div class="role">${{"turn " + esc(t.index) + " · 输出 final_text"}}</div><pre>${{esc(t.final_text || '')}}</pre></div>`;
    }});
  }}
  const evs = ov.events || [];
  if (evs.length) {{
    let steps = '';
    evs.forEach((e, idx) => {{
      const head = [e.kind || '', e.role || '', e.turn != null ? ('turn ' + e.turn) : ''].filter(Boolean).join(' · ');
      const pay = e.payload || {{}};
      let summary = '';
      if (pay.name) {{
        summary = String(pay.name);
        if (pay.arguments != null) summary += ' ' + String(pay.arguments).slice(0, 160);
        else if (pay.input != null) summary += ' ' + JSON.stringify(pay.input).slice(0, 160);
      }} else if (pay.text) summary = String(pay.text).slice(0, 240);
      else if (pay.result_preview) summary = String(pay.result_preview).slice(0, 240);
      else summary = JSON.stringify(pay).slice(0, 240);
      steps += `<div class="msg"><div class="role">#${{idx+1}} ${{esc(head)}}</div><pre>${{esc(summary)}}</pre></div>`;
    }});
    ctxHtml += `<details><summary>关键步骤（${{evs.length}}）</summary>${{steps}}</details>`;
  }}
  ctx.innerHTML = ctxHtml;

  reply.innerHTML = turns.length
    ? turns.map(t => `<div class="msg assistant"><div class="role">${{"turn " + esc(t.index) + " final"}}</div><pre>${{esc(t.final_text || '')}}</pre></div>`).join('')
    : '<div class="empty">无最终输出</div>';

  const tl = ov.tool_timeline || [];
  tools.innerHTML = tl.length
    ? (`<p class="sub">工具调用顺序</p>` + tl.map((n,i) => `<span class="chip">${{i+1}}. ${{esc(n)}}</span>`).join(''))
    : '<div class="empty">无工具摘要</div>';

  const slim = {{
    identity: c.identity, case_id: ov.case_id, success: ov.success, exit_code: ov.exit_code, error: ov.error,
    metrics: ov.metrics, turns: ov.turns, tool_timeline: ov.tool_timeline,
    artifacts: ov.artifacts, n_events: ov.n_events,
    events: ov.events, note: ov.note, wire_only: ov.wire_only, runner_only: ov.runner_only,
    attribution_status: ov.attribution_status,
  }};
  raw.innerHTML = `<div class="msg"><pre>${{esc(JSON.stringify(slim, null, 2))}}</pre></div>`
    + (ov.runner_only
      ? `<p class="sub">评测器产物：${{esc(c.artifact_path || ("cases/" + c.id))}}/meta.json、response.json</p>`
      : (ov.wire_only
        ? `<p class="sub">wire-only 总览 · llm_calls.jsonl</p>`
        : `<p class="sub">Trace：${{esc(c.artifact_path || ("cases/" + c.id))}}/trace.json</p>`));
  setTab('context');
}}

function renderDetail() {{
  const bar = document.getElementById('detailBar');
  const ctx = document.getElementById('pane-context');
  const reply = document.getElementById('pane-reply');
  const tools = document.getElementById('pane-tools');
  const raw = document.getElementById('pane-raw');
  if (caseIdx < 0) {{
    setOverviewTabs(true);
    bar.innerHTML = '<strong>模型调用时间线</strong> · ' + visibleCases().length + ' 次执行';
    ctx.innerHTML = renderRunTimeline();
    let sum = '';
    for (const c of visibleCases()) {{
      const spans = (c.calls || []).map(callSpan).filter(s => s.start != null);
      if (!spans.length) continue;
      const a = Math.min(...spans.map(s => s.start));
      const b = Math.max(...spans.map(s => (s.end != null ? s.end : s.start)));
      sum += '<div class="msg"><div class="role">' + esc(caseLabel(c)) + '</div><pre>' + fmtClock(a) + ' → ' + fmtClock(b) + ' · ' + spans.length + ' calls</pre></div>';
    }}
    reply.innerHTML = sum || '<div class="empty">-</div>';
    tools.innerHTML = '<div class="empty">gaps on timeline</div>';
    raw.innerHTML = '<div class="msg"><pre>' + esc(JSON.stringify(visibleCases().map(c => ({{ id: c.id, identity: c.identity, calls: (c.calls || []).map(x => ({{ seq: x.seq, ts_start: x.ts_start, ts_end: x.ts_end, latency_ms: x.latency_ms }})) }})), null, 2)) + '</pre></div>';
    setTab('context');
    return;
  }}
  const c = DATA.cases[caseIdx];
  if (!c) {{
    bar.textContent = 'pick case';
    ctx.innerHTML = reply.innerHTML = tools.innerHTML = raw.innerHTML = '<div class="empty">-</div>';
    return;
  }}
  if (callIdx < 0) {{
    renderOverview(c);
    return;
  }}
  const call = c.calls[callIdx];
  if (!call) {{
    bar.textContent = '选择一次 LLM 调用查看完整上下文';
    ctx.innerHTML = reply.innerHTML = tools.innerHTML = raw.innerHTML = '<div class="empty">无数据</div>';
    return;
  }}
  setOverviewTabs(false);
  const st = call.context_stats || {{}};
  const tok = (n) => (n == null || n === '' ? '—' : Number(n).toLocaleString());
  const pair = (chars, est) => `${{tok(chars)}} 字 / 约 ${{tok(est)}} tok`;
  const sp = callSpan(call);
  const growth = call.context_growth || {{}};
  const gbit = growth.is_first
    ? ' · ctx=first'
    : (growth.prompt_tokens_delta != null
      ? ` · Δprompt_tok=${{growth.prompt_tokens_delta >= 0 ? '+' : ''}}${{growth.prompt_tokens_delta}}`
      : (growth.messages_chars_delta != null
        ? ` · Δmsg_chars=${{growth.messages_chars_delta >= 0 ? '+' : ''}}${{growth.messages_chars_delta}}`
        : ''));
  bar.innerHTML = '<strong>' + esc(caseLabel(c)) + '</strong> · seq=' + esc(call.seq)
    + (call.execution_id ? ' · execution=' + esc(call.execution_id) : '')
    + (call.stop_reason ? ' · stop=' + esc(call.stop_reason) : '')
    + (call.response_status ? ' · status=' + esc(call.response_status) : '')
    + (call.previous_response_id ? ' · previous_response=' + esc(call.previous_response_id) : '')
    + (call.protocol ? ' · ' + esc(call.protocol) : '')
    + ' · ' + esc(call.model || '')
    + ' · ' + fmtClock(sp.start) + ' → ' + fmtClock(sp.end) + ' · lat=' + fmtDur(sp.latency)
    + ' · tools=' + String((call.tool_names || []).length)
    + (st.prompt_tokens != null ? ` · prompt_tokens=${{tok(st.prompt_tokens)}}` : '')
    + gbit;

  let ctxHtml = `<div class="stats">
    <div><div class="k">system</div><div class="v">${{pair(st.system_chars, st.system_tokens_est)}}</div></div>
    <div><div class="k">messages（${{tok(st.messages_count)}} 条）</div><div class="v">${{pair(st.messages_chars, st.messages_tokens_est)}}</div></div>
    <div><div class="k">tools schema（${{tok(st.tools_count)}} 个）</div><div class="v">${{pair(st.tools_chars, st.tools_tokens_est)}}</div></div>
    <div><div class="k">输入合计（估算）</div><div class="v">${{pair(st.input_chars_total, st.input_tokens_est_total)}}</div></div>
    <div><div class="k">上游 usage.prompt_tokens</div><div class="v">${{tok(st.prompt_tokens)}}</div></div>
    <div><div class="k">usage.completion_tokens</div><div class="v">${{tok(st.completion_tokens)}}</div></div>
    <div><div class="k">usage.total_tokens</div><div class="v">${{tok(st.total_tokens)}}</div></div>
    <div><div class="k">cached_tokens</div><div class="v">${{tok(st.cached_tokens)}}</div></div>
    <div class="note">分项 tok 为本地粗估（CJK≈1，其它≈0.3）；prompt_tokens 来自上游 usage，两者口径不同，以 usage 为准看计费。</div>
  </div>`;
  if (growth && !growth.is_first) {{
    ctxHtml += `<div class="msg"><div class="role">上下文增长（相对上一 LLM call）</div><pre>`
      + esc(JSON.stringify({{
        messages_count_delta: growth.messages_count_delta,
        messages_chars_delta: growth.messages_chars_delta,
        prompt_tokens_delta: growth.prompt_tokens_delta,
        input_chars_total_delta: growth.input_chars_total_delta,
      }}, null, 2))
      + `</pre></div>`;
  }}
  if (call.system) {{
    ctxHtml += `<details open><summary>system（${{pair(st.system_chars, st.system_tokens_est)}}）</summary><div class="msg"><pre>${{esc(call.system)}}</pre></div></details>`;
  }}
  call.messages.forEach((m, i) => {{
    // system 已在上方独立块展示时，跳过 messages 里重复的 system，避免整案/明细不对齐观感
    if (call.system && m.role === 'system' && (m.text || '') === call.system) return;
    const n = (m.text || '').length;
    let body = m.text || '';
    if (m.tool_calls?.length) body += (body ? '\\n' : '') + JSON.stringify(m.tool_calls, null, 2);
    if (m.tool_call_id) body = 'tool_call_id: ' + m.tool_call_id + '\\n' + body;
    if (!body && m.role === 'assistant') body = '(本条无文本)';
    ctxHtml += `<div class="msg"><div class="role">${{esc(m.role)}} · ${{n}} 字 / 约 ${{tok(Math.max(1, Math.round((body||'').split('').reduce((a,ch)=>a+(/[\u4e00-\u9fff]/.test(ch)?1:0.3),0))))}} tok · #${{i+1}}</div><pre>${{esc(body)}}</pre></div>`;
  }});
  ctx.innerHTML = ctxHtml || '<div class="empty">无 messages</div>';

  let replyHtml = '';
  if (call.thinking) {{
    replyHtml += `<div class="msg"><div class="role">thinking</div><pre>${{esc(call.thinking)}}</pre></div>`;
  }}
  replyHtml += `<div class="msg assistant"><div class="role">assistant</div><pre>${{esc(call.assistant || '(空)')}}</pre></div>`;
  (call.tool_calls || []).forEach(t => {{
    replyHtml += `<div class="tool"><code>${{esc(t.name)}} ${{esc(t.id || "")}}</code><pre>${{esc(t.arguments)}}</pre></div>`;
  }});
  if (call.usage && Object.keys(call.usage).length) {{
    replyHtml += `<div class="msg"><div class="role">usage</div><pre>${{esc(JSON.stringify(call.usage, null, 2))}}</pre></div>`;
  }}
  if (call.response_error || call.incomplete_details) replyHtml += `<div class="tool"><pre>${{esc(JSON.stringify(call.response_error || call.incomplete_details, null, 2))}}</pre></div>`;
  if (call.error) replyHtml += `<div class="tool"><strong>error</strong><pre>${{esc(call.error)}}</pre></div>`;
  reply.innerHTML = replyHtml;

  const brief = (call.raw_request && call.raw_request.tools_brief) || [];
  if (brief.length) {{
    tools.innerHTML = '<p class="sub">工具名 + 描述（完整 schema 见 Raw.request.tools）</p>' + brief.map(t =>
      `<div class="tool"><code>${{esc(t.name || '')}}</code><pre>${{esc(t.description || '')}}</pre></div>`
    ).join('');
  }} else if ((call.tool_names || []).length) {{
    tools.innerHTML = call.tool_names.map(n => `<span class="chip">${{esc(n)}}</span>`).join('');
  }} else {{
    tools.innerHTML = '<div class="empty">无 tools</div>';
  }}

  raw.innerHTML = (call.execution_id || call.lane_id
      ? `<h4>归属</h4><div class="msg"><pre>${{esc(JSON.stringify({{call_id: call.call_id, execution_id: call.execution_id, lane_id: call.lane_id, attribution_status: call.attribution_status}}, null, 2))}}</pre></div>`
      : '')
    + `<h4>request（含 tools 全文 + messages）</h4><div class="msg"><pre>${{esc(JSON.stringify(call.raw_request, null, 2))}}</pre></div>`
    + `<h4>response（结构化）</h4><div class="msg"><pre>${{esc(JSON.stringify(call.raw_response, null, 2))}}</pre></div>`
    + `<h4>usage</h4><div class="msg"><pre>${{esc(JSON.stringify(call.usage || {{}}, null, 2))}}</pre></div>`
    + `<p class="sub">完整原始文件：${{esc(c.artifact_path || ("cases/" + c.id))}}/llm_calls.jsonl</p>`;
  setTab('context');
}}

function applyHash() {{
  const h = (location.hash || '').replace(/^#/, '');
  if (!h) return;
  if (h === 'timeline') {{ caseIdx = -1; callIdx = -1; return; }}
  const parts = h.split('/');
  const cid = parts[0];
  const seqStr = parts[1];
  const ci = DATA.cases.findIndex(c => c.id === cid);
  if (ci < 0) return;
  if (DATA.cases[ci].latest === false) document.getElementById('showHistory').checked = true;
  if (batchFilter.value && batchFilter.value !== (DATA.cases[ci].identity?.run_id || '未标记批次')) batchFilter.value = '';
  caseIdx = ci;
  if (seqStr == null || seqStr === '' || seqStr === 'overview') {{
    callIdx = -1;
  }} else {{
    const callI = DATA.cases[ci].calls.findIndex(c => String(c.seq) === String(seqStr));
    callIdx = callI >= 0 ? callI : -1;
  }}
}}
window.addEventListener('hashchange', () => {{
  applyHash();
  renderCases(document.getElementById('caseFilter').value);
  renderCalls();
  renderDetail();
}});

renderCases();
applyHash();
renderCases(document.getElementById('caseFilter').value);
renderCalls();
renderDetail();
</script>
</body>
</html>
"""
    out = run_dir / "llm_trace.html"
    out.write_text(doc, encoding="utf-8")
    return out


def _share_manifest(run_dir: Path) -> dict[str, Any]:
    """Snapshot of what a share zip should contain (for humans + CI checks)."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    except Exception:
        now = datetime.now().isoformat(timespec="seconds")

    cases_root = run_dir / "cases"
    case_ids: list[str] = []
    n_success = 0
    n_fail = 0
    n_missing_trace = 0
    fail_ids: list[str] = []
    if cases_root.is_dir():
        for d in sorted(p for p in cases_root.iterdir() if p.is_dir()):
            case_ids.append(d.name)
            tp = d / "trace.json"
            if not tp.is_file():
                n_missing_trace += 1
                fail_ids.append(d.name)
                continue
            try:
                tr = json.loads(tp.read_text(encoding="utf-8"))
            except Exception:
                n_missing_trace += 1
                fail_ids.append(d.name)
                continue
            if tr.get("success"):
                n_success += 1
            else:
                n_fail += 1
                fail_ids.append(d.name)

    def _mtime(name: str) -> Optional[str]:
        fp = run_dir / name
        if not fp.is_file():
            return None
        return datetime.fromtimestamp(fp.stat().st_mtime).isoformat(timespec="seconds")

    return {
        "schema_version": "1.0",
        "run_id": run_dir.name,
        "packed_at": now,
        "n_cases": len(case_ids),
        "n_success": n_success,
        "n_fail": n_fail,
        "n_missing_trace": n_missing_trace,
        "fail_or_missing_ids": fail_ids,
        "artifacts": {
            "results.xlsx": _mtime("results.xlsx"),
            "llm_trace.html": _mtime("llm_trace.html"),
            "run_meta.json": _mtime("run_meta.json"),
            "progress.jsonl": _mtime("progress.jsonl"),
            "console.log": _mtime("console.log"),
        },
        "policy": {
            "rebuild_html_before_pack": True,
            "atomic_replace": True,
            "includes": ["results.xlsx", "llm_trace.html", "run_meta.json", "progress.jsonl", "console.log", "share_manifest.json", "cases/**"],
            "note": "Canonical share zip is always <run_id>_share.zip overwritten only after HTML refresh + atomic rename.",
        },
    }


def pack_run_zip(run_dir: Path, *, out_zip: Optional[Path] = None) -> Path:
    """Pack a portable share zip for a finished (or snapshot) run.

    Guarantees:
    - rebuilds llm_trace.html from all cases/ before packing
    - refuses to pack if results.xlsx is missing
    - writes share_manifest.json (counts + artifact mtimes) into the zip and run dir
    - atomic replace: write *.zip.tmp then rename over <run_id>_share.zip
    - if an older share zip exists, keep one backup as <run_id>_share.prev.zip
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir not found: {run_dir}")
    xlsx = run_dir / "results.xlsx"
    if not xlsx.is_file():
        raise FileNotFoundError(f"results.xlsx missing under {run_dir}; rebuild excel before pack-zip")

    out_zip = Path(out_zip) if out_zip else run_dir / f"{run_dir.name}_share.zip"
    html_path = build_llm_trace_html(run_dir)

    manifest = _share_manifest(run_dir)
    manifest["artifacts"]["llm_trace.html"] = None
    try:
        from datetime import datetime
        manifest["artifacts"]["llm_trace.html"] = datetime.fromtimestamp(html_path.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        pass
    manifest_path = run_dir / "share_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tmp_zip = out_zip.with_name(out_zip.name + ".tmp")
    if tmp_zip.exists():
        tmp_zip.unlink()
    top_files = (
        "results.xlsx",
        "llm_trace.html",
        "run_meta.json",
        "progress.jsonl",
        "console.log",
        "share_manifest.json",
    )
    members: list = []
    for name in top_files:
        p = run_dir / name
        if p.is_file():
            members.append((p, name))
    cases = run_dir / "cases"
    if cases.is_dir():
        for p in cases.rglob("*"):
            if p.is_file():
                members.append((p, str(p.relative_to(run_dir))))
    # Secret-scan gate: every file going into the zip; raises SecretScanError (no zip written).
    from eval_harness.secret_scan import gate as _secret_gate
    _secret_gate([m[0] for m in members], what=f"share zip {out_zip.name}")
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p, arc in members:
            zf.write(p, arcname=arc)

    if out_zip.exists():
        prev = run_dir / f"{run_dir.name}_share.prev.zip"
        if prev.exists():
            prev.unlink()
        out_zip.replace(prev)
    tmp_zip.replace(out_zip)
    return out_zip


def materialize_wire_run(
    *,
    gateway_log: Path,
    out_run_dir: Path,
    case_ids: list[str] | None = None,
    include_orphans: bool = False,
    run_id: str | None = None,
    execution_ids: list[str] | None = None,
) -> Path:
    """Export gateway calls by execution and build HTML without harness results."""
    from eval_harness.llm_gateway_ingest import load_gateway_pairs
    from eval_harness.trace_store import export_executions
    from eval_harness.trace_identity import writer_lock
    out_run_dir = Path(out_run_dir)
    with writer_lock(out_run_dir):
        export_executions(load_gateway_pairs(Path(gateway_log)), out_run_dir,
                          case_ids=case_ids, run_id=run_id, execution_ids=execution_ids,
                          include_orphans=include_orphans)
        build_llm_trace_html(out_run_dir, title=f"{out_run_dir.name} · wire-only")
    return out_run_dir


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path, nargs="?")
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--from-gateway-log", type=Path, help="Build a wire-only run from gateway jsonl")
    ap.add_argument("--out-run-dir", type=Path, help="Output run dir for --from-gateway-log")
    ap.add_argument("--cases", type=str, default="", help="Comma-separated case ids (default: all tagged)")
    ap.add_argument("--include-orphans", action="store_true")
    ap.add_argument("--run-id", help="Select an explicitly tagged batch")
    ap.add_argument("--executions", help="Comma-separated execution IDs")
    args = ap.parse_args()
    if args.from_gateway_log:
        if not args.out_run_dir:
            raise SystemExit("--out-run-dir required with --from-gateway-log")
        case_ids = [c.strip() for c in args.cases.split(",") if c.strip()] or None
        out = materialize_wire_run(
            gateway_log=args.from_gateway_log,
            out_run_dir=args.out_run_dir,
            case_ids=case_ids,
            include_orphans=args.include_orphans,
            run_id=args.run_id,
            execution_ids=args.executions.split(",") if args.executions else None,
        )
        print("wire_run", out)
        print("html", out / "llm_trace.html")
    else:
        if not args.run_dir:
            raise SystemExit("run_dir required unless --from-gateway-log")
        print("html", build_llm_trace_html(args.run_dir))
        if args.zip:
            print("zip", pack_run_zip(args.run_dir))
