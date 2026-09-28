"""eval_case_id extraction contract (local_relay_trace_scheme §3).

Priority:
  1. Header X-Eval-Case-Id
  2. Structured body: eval_case_id (+ Responses metadata.eval_case_id)
  3. Text marker <!--eval_case_id:…-->

No filesystem / process fallback. Conflicts: higher priority wins; return warn note.
"""
from __future__ import annotations

import re
from typing import Any

CASE_HEADER = "x-eval-case-id"
EXECUTION_HEADER = "x-eval-execution-id"
_CASE_MARKER_RE = re.compile(r"<!--\s*eval_case_id:([A-Za-z0-9_.-]+)\s*-->")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or not _CASE_ID_RE.match(value):
        return None
    return value


def _texts_from_system(system: Any) -> list[str]:
    out: list[str] = []
    if isinstance(system, str):
        out.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    out.append(t)
    return out


def _texts_from_messages(messages: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(messages, list):
        return out
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    out.append(block["text"])
        # Responses / input items sometimes carry content similarly
    return out


def _texts_from_input(inp: Any) -> list[str]:
    """OpenAI Responses `input` may be str or list of items."""
    out: list[str] = []
    if isinstance(inp, str):
        out.append(inp)
        return out
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                c = item.get("content")
                if isinstance(c, str):
                    out.append(c)
                elif isinstance(c, list):
                    for block in c:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            out.append(block["text"])
                t = item.get("text")
                if isinstance(t, str):
                    out.append(t)
    return out


def _from_header(headers: Any) -> str | None:
    if not isinstance(headers, dict):
        return None
    for k, v in headers.items():
        if str(k).lower() == CASE_HEADER:
            return _norm(v)
    return None


def _from_header_execution_id(headers: Any) -> str | None:
    """Extract X-Eval-Execution-Id from request headers."""
    if not isinstance(headers, dict):
        return None
    for k, v in headers.items():
        if str(k).lower() == EXECUTION_HEADER:
            val = v.strip() if isinstance(v, str) else str(v).strip()
            if val and _EXECUTION_ID_RE.match(val):
                return val
    return None


def extract_execution_id(
    *,
    headers: Any = None,
    body: Any = None,
    optional_params: Any = None,
) -> str | None:
    """Return execution_id from headers or body metadata."""
    eid = _from_header_execution_id(headers)
    if eid:
        return eid
    # Also check body.metadata.eval_execution_id
    for source in (body, optional_params):
        if isinstance(source, dict):
            meta = source.get("metadata")
            if isinstance(meta, dict):
                val = meta.get("eval_execution_id")
                if isinstance(val, str) and val.strip() and _EXECUTION_ID_RE.match(val.strip()):
                    return val.strip()
    return None


def _from_body_fields(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    direct = _norm(body.get("eval_case_id"))
    if direct:
        return direct
    meta = body.get("metadata")
    if isinstance(meta, dict):
        return _norm(meta.get("eval_case_id"))
    return None


def _from_optional_params(optional_params: Any) -> str | None:
    if not isinstance(optional_params, dict):
        return None
    direct = _norm(optional_params.get("eval_case_id"))
    if direct:
        return direct
    meta = optional_params.get("metadata")
    if isinstance(meta, dict):
        return _norm(meta.get("eval_case_id"))
    return None


def _from_text_marker(
    *,
    messages: Any = None,
    optional_params: Any = None,
    body: Any = None,
) -> str | None:
    chunks: list[str] = []
    if isinstance(optional_params, dict):
        chunks.extend(_texts_from_system(optional_params.get("system")))
        chunks.extend(_texts_from_messages(optional_params.get("messages")))
        chunks.extend(_texts_from_input(optional_params.get("input")))
    if isinstance(body, dict):
        chunks.extend(_texts_from_system(body.get("system")))
        chunks.extend(_texts_from_messages(body.get("messages")))
        chunks.extend(_texts_from_input(body.get("input")))
    chunks.extend(_texts_from_messages(messages))
    for text in chunks:
        m = _CASE_MARKER_RE.search(text)
        if m:
            return m.group(1)
    return None


def extract_case_id(
    *,
    headers: Any = None,
    body: Any = None,
    optional_params: Any = None,
    messages: Any = None,
) -> tuple[str | None, str | None, list[str]]:
    """Return (case_id, source, warnings).

    source ∈ {header, body, optional_params, text, None}
    """
    warnings: list[str] = []
    header_id = _from_header(headers)
    body_id = _from_body_fields(body)
    opt_id = _from_optional_params(optional_params)
    # body and optional_params are the same semantic tier (structured field)
    structured_id = body_id or opt_id
    structured_source = "body" if body_id else ("optional_params" if opt_id else None)
    text_id = _from_text_marker(messages=messages, optional_params=optional_params, body=body)

    present = [(s, v) for s, v in (
        ("header", header_id),
        ("structured", structured_id),
        ("text", text_id),
    ) if v]
    if not present:
        return None, None, warnings

    winner_source, winner = present[0]
    # map structured → body|optional_params for logging
    if winner_source == "structured":
        winner_source = structured_source or "body"
    for src, val in present[1:]:
        if val != winner:
            warnings.append(f"case_id conflict: keeping {winner_source}={winner}, ignoring {src}={val}")
    return winner, winner_source, warnings


def detect_protocol(*, path: Any = None, body: Any = None, messages: Any = None) -> str:
    p = (str(path) if path is not None else "").lower()
    if "/responses" in p:
        return "responses"
    if "/messages" in p or p.endswith("messages"):
        return "anthropic_messages"
    if "/chat/completions" in p or "/completions" in p:
        return "chat_completions"
    if isinstance(body, dict):
        if "input" in body and "messages" not in body:
            return "responses"
        if "system" in body and "max_tokens" in body and "messages" in body and "input" not in body:
            # Anthropic-ish; chat also has messages — prefer anthropic when max_tokens top-level + system
            if "model" in body and "max_tokens" in body and "messages" in body:
                # Ambiguous; OpenAI chat uses max_tokens too. Prefer path.
                pass
    if messages is not None:
        return "chat_completions"
    return "unknown"


def strip_eval_case_id_from_body(body: Any) -> Any:
    """Return a shallow-copied body without top-level eval_case_id; metadata key dropped if present."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    out.pop("eval_case_id", None)
    meta = out.get("metadata")
    if isinstance(meta, dict) and "eval_case_id" in meta:
        meta = dict(meta)
        meta.pop("eval_case_id", None)
        if meta:
            out["metadata"] = meta
        else:
            out.pop("metadata", None)
    return out


def proxy_request_bits(litellm_params: Any) -> tuple[Any, Any, Any]:
    """From LiteLLM kwargs['litellm_params'] → (headers, body, path)."""
    if not isinstance(litellm_params, dict):
        return None, None, None
    psr = litellm_params.get("proxy_server_request")
    if isinstance(psr, str):
        try:
            import json
            psr = json.loads(psr)
        except Exception:
            psr = None
    if not isinstance(psr, dict):
        return None, None, None
    headers = psr.get("headers")
    body = psr.get("body")
    if body is None and "data" in psr:
        body = psr.get("data")
    path = psr.get("url") or psr.get("path") or ""
    return headers, body, path
