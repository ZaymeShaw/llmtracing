"""Langfuse 专用安全投影，不修改共享 Span 或数据库 exporter 的输入。"""
from __future__ import annotations

import json
import hashlib
import hmac
from datetime import datetime, timezone
from itertools import islice
import re
from collections.abc import Mapping
from typing import Any
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Event
from opentelemetry.trace import Status
from app.conf.langfuse import LangfuseConf

_SAFE = frozenset({
    "openinference.span.kind", "llm.model_name", "llm.provider", "llm.system",
    "llm.token_count.prompt", "llm.token_count.completion", "llm.token_count.total",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.request.model",
    "http.response.status_code", "http.request.method", "request.bytes", "response.bytes",
    "operation", "outcome", "attempts", "items", "first_output_ms", "first_visible_ms",
    "exception.type", "run_id", "trace_id", "request_id", "fallback", "stage",
})
_SENSITIVE = re.compile(r"password|secret|token|authorization|cookie|api.?key|empno|agentno|user.?id|session.?id|customer.?id|身份证|手机|姓名|客户名|agentCode|insured|policy.*(?:id|no)|^name$", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_NUMBER = re.compile(r"(?<!\w)(?:\+?\d[\d -]{8,}\d|\d{17}[\dXx])(?!\w)")
_AUTH = re.compile(r"(?i)(?:bearer\s+|(?:sk|pk)-)[A-Za-z0-9_.-]+")
_ASSIGN = re.compile(r"(?i)(password|secret|token|authorization|api[_-]?key|empno|agentno|user_id|session_id)\s*[:=]\s*[^\s,;]+")


def clean_content(value: Any, conf: LangfuseConf, sensitive: tuple[str, ...] = ()) -> str:
    remaining = 512
    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal remaining
        remaining -= 1
        if depth > 8 or remaining < 0:
            return "[TRUNCATED]"
        if isinstance(item, Mapping):
            return {str(k): "[REDACTED]" if _SENSITIVE.search(str(k)) else visit(v, depth + 1)
                    for k, v in islice(item.items(), 100) if remaining > 0}
        if isinstance(item, (list, tuple)):
            return [visit(v, depth + 1) for v in item[:100] if remaining > 0]
        if isinstance(item, str):
            text = item[:conf.max_content_chars * 2]
            for secret in (*sensitive, conf.public_key.get_secret_value(), conf.secret_key.get_secret_value()):
                if secret:
                    text = text.replace(secret, "[REDACTED]")
            text = _ASSIGN.sub("[REDACTED]", _AUTH.sub("[REDACTED]", text))
            return _NUMBER.sub("[REDACTED]", _EMAIL.sub("[REDACTED]", text))
        return item if item is None or isinstance(item, (bool, int, float)) else "[OMITTED]"
    cleaned = visit(value)
    text = cleaned if isinstance(cleaned, str) else json.dumps(cleaned, ensure_ascii=False, allow_nan=False)
    return text[:conf.max_content_chars]


def project(span: ReadableSpan, conf: LangfuseConf, metadata: dict[str, Any],
            extra: dict[str, Any]) -> ReadableSpan:
    source = span.attributes or {}
    attrs = {k: v for k, v in source.items() if k in _SAFE}
    attrs.update(metadata)
    attrs.update(extra)
    # 原生 AgentOS 的根 Span 在 workflow_scope 之前创建，从其框架属性单独派生
    # 关联值，不把同一 WebSocket 连接的可变状态当作每轮的身份来源。
    if "langfuse.session.id" not in attrs and isinstance(source.get("session.id"), str):
        identity = str(source.get("user.id", ""))
        attrs["langfuse.session.id"] = hmac.new(conf.secret_key.get_secret_value().encode(),
            (str(attrs.get("langfuse.environment", "")) + ":" + identity + ":" + str(source["session.id"])).encode(),
            hashlib.sha256).hexdigest()
    if source.get("agno.run.id"):
        attrs["langfuse.trace.metadata.run_id"] = source["agno.run.id"]
    attrs["langfuse.trace.tags"] = tuple(conf.tags)
    kind = str(source.get("openinference.span.kind", "SPAN"))
    if span.instrumentation_scope and span.instrumentation_scope.name == "insurance.langfuse":
        operation = span.name
    else:
        # 框架 name 可能包含用户指定的 Agent/Workflow 名称，不原样上传。
        operation = {"LLM": "llm.call", "TOOL": "tool.call", "AGENT": "agent.run",
                     "CHAIN": "workflow.run"}.get(kind, "workflow.step")
    known_steps = {"assemble", "gate", "direct", "run_capability", "agent_input", "agent", "agent_bridge", "finalize", "present_agentos"}
    candidate = span.name.split(".")[0]
    if kind == "CHAIN" and candidate in known_steps:
        operation = "workflow.step." + candidate
    tool = source.get("tool.name")
    if kind == "TOOL" and tool in {"customer_search", "policy_search", "customer_detail", "policy_detail",
        "product_knowledge", "benefit_info", "general_qa", "entity_lookup", "ask_clarify",
        "present_policy_search_card", "present_customer_search_card", "run_policy_snapshot", "run_policy_field_lookup"}:
        operation = "tool." + str(tool)
    attrs["langfuse.observation.type"] = "generation" if kind == "LLM" else "span"
    attrs.setdefault("langfuse.trace.name", conf.name_prefix + ".chat.turn")
    events = [Event(event.name, {k: v for k, v in (event.attributes or {}).items() if k in _SAFE}, event.timestamp)
              for event in span.events if event.name in ("first_output", "first_visible", "fallback", "cancelled")]
    for event in events:
        if event.name == "first_output" and event.timestamp is not None:
            attrs["langfuse.observation.completion_start_time"] = datetime.fromtimestamp(
                event.timestamp / 1_000_000_000, timezone.utc).isoformat()
        if event.name == "first_visible" and event.timestamp is not None and span.start_time is not None:
            attrs["first_visible_ms"] = (event.timestamp - span.start_time) / 1_000_000
    return ReadableSpan(name=conf.name_prefix + "." + operation, context=span.context, parent=span.parent,
                        resource=Resource({"service.name": conf.name_prefix}), attributes=attrs,
                        events=events, links=(), kind=span.kind, status=Status(span.status.status_code),
                        start_time=span.start_time, end_time=span.end_time,
                        instrumentation_scope=span.instrumentation_scope)
