"""Protocol-level extraction; no harness results required.

Retain original payloads. Normalized fields are projections, not replacements.
"""
from __future__ import annotations
import json


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") in ("text", "input_text", "output_text", "summary_text"):
            return str(value.get("text") or "")
        if value.get("type") == "tool_result":
            return text_content(value.get("content"))
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def request_fields(req):
    # Prefer the exact incoming body over LiteLLM's converted representation.
    client = req.get("client_request")
    body = client if isinstance(client, dict) and client else req
    op = req.get("optional_params") or {}
    source = body.get("messages", body.get("input", op.get("input", req.get("messages", []))))
    if isinstance(source, str):
        source = [{"role": "user", "content": source}]
    messages = []
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "function_call_output":
            messages.append({"role": "tool", "text": text_content(item.get("output")),
                             "tool_call_id": item.get("call_id")})
        elif typ == "function_call":
            messages.append({"role": "assistant", "text": "", "tool_calls": [item]})
        else:
            messages.append({"role": item.get("role") or "user", "text": text_content(item.get("content")),
                             **{k: item[k] for k in ("tool_call_id", "tool_calls") if k in item},
                             "content": item.get("content")})
    system = body.get("system", body.get("instructions", op.get("system", op.get("instructions"))))
    if system is None:
        system = "\n".join(m["text"] for m in messages if m["role"] in ("system", "developer"))
    return dict(system=text_content(system), messages=messages,
                tools=body.get("tools", req.get("tools", op.get("tools", []))),
                previous_response_id=body.get("previous_response_id", op.get("previous_response_id")))


def response_fields(response):
    if not isinstance(response, dict):
        return None
    texts, thoughts, tools = [], [], []
    status = response.get("status")
    stop = response.get("stop_reason")
    blocks = response.get("content") or []
    if response.get("choices"):
        choice = response["choices"][0]
        msg = choice.get("message") or choice.get("delta") or {}
        texts.append(text_content(msg.get("content")))
        thoughts.append(str(msg.get("reasoning_content") or ""))
        for block in msg.get("thinking_blocks") or []:
            thoughts.append(str(block.get("thinking") or block.get("text") or ""))
        for tool in msg.get("tool_calls") or []:
            fn = tool.get("function") or tool
            tools.append(dict(id=tool.get("id"), name=fn.get("name"), arguments=fn.get("arguments") or ""))
        stop = choice.get("finish_reason")
    elif "output" in response:
        for item in response.get("output") or []:
            typ = item.get("type")
            if typ == "message":
                texts.append(text_content(item.get("content")))
            elif typ in ("function_call", "tool_call"):
                tools.append(dict(id=item.get("call_id") or item.get("id"), name=item.get("name"),
                                  arguments=item.get("arguments") or item.get("input") or ""))
            elif typ in ("reasoning", "thinking"):
                thoughts.append(text_content(item.get("summary") or item.get("content")))
    else:
        if isinstance(blocks, str):
            texts.append(blocks)
        for b in blocks if isinstance(blocks, list) else []:
            if not isinstance(b, dict):
                texts.append(str(b))
                continue
            typ = b.get("type")
            if typ == "text":
                texts.append(b.get("text") or "")
            elif typ == "thinking":
                thoughts.append(b.get("thinking") or "")
            elif typ == "tool_use":
                tools.append(dict(id=b.get("id"), name=b.get("name"), arguments=json.dumps(b.get("input") or {}, ensure_ascii=False)))
            else:
                texts.append(json.dumps(b, ensure_ascii=False))
    return dict(content="\n".join(t for t in texts if t), thinking="\n".join(t for t in thoughts if t),
                tool_calls=tools, stop_reason=stop, response_status=status,
                response_id=response.get("id"), response_error=response.get("error"),
                incomplete_details=response.get("incomplete_details"))


def usage_fields(usage):
    if not isinstance(usage, dict):
        return None
    out = dict(usage)
    for source, dest in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        if source in usage:
            out[dest] = usage[source]
    for k in ("prompt_tokens_details", "input_tokens_details"):
        if isinstance(usage.get(k), dict) and usage[k].get("cached_tokens") is not None:
            out["cached_tokens"] = usage[k]["cached_tokens"]
    # Anthropic input_tokens excludes cache reads/writes. Preserve all components.
    if "input_tokens" in usage and ("cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage):
        out["prompt_tokens"] = sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if "total_tokens" not in out and all(k in out for k in ("prompt_tokens", "completion_tokens")):
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out
