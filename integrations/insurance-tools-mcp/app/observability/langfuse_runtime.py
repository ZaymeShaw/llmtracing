"""进程级导出资源与请求级上下文；禁用时不建立 Provider 或网络连接。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
from collections.abc import Iterator, AsyncIterator
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from itertools import islice

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from app.observability.langfuse_batch import BoundedBatch
from opentelemetry.trace import StatusCode
from tool_adapter_sdk.compat import TIMEOUT_ERRORS
from app.conf.langfuse import LangfuseConf, langfuse_conf
from app.conf.runtime import current_source
from app.observability.context import current_context
from app.observability.langfuse_projection import clean_content, project

logger = logging.getLogger(__name__)


@dataclass
class RequestTrace:
    runtime: LangfuseRuntime
    metadata: dict[str, Any] = field(default_factory=dict)
    sensitive: tuple[str, ...] = ()
    root: Any = None
    visible: bool = False


_CURRENT: ContextVar[RequestTrace | None] = ContextVar("langfuse_request", default=None)
@dataclass
class ModelContent:
    # 原生插桩可能在 provider 返回前结束，延后本目的地的投影，不改变共享 Span 生命周期。
    pending: list[tuple[ReadableSpan, dict[str, Any]]] = field(default_factory=list)
    input: str | None = None
    parts: list[str] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    remaining_tool_chars: int = 0


_MODEL_CONTENT: ContextVar[ModelContent | None] = ContextVar("langfuse_model_content", default=None)
_RUNTIMES: list[LangfuseRuntime] = []
_LOCK = threading.RLock()
_OWNED_PROVIDER: TracerProvider | None = None


class _ExportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.threadName == "langfuse-export":
            record.msg, record.args = "langfuse_export_failed", ()
            record.exc_info = record.exc_text = None
        return True


class SafeExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner
        self.failures = 0
        self.exported = 0

    def export(self, spans):
        try:
            result = self.inner.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        if result == SpanExportResult.SUCCESS:
            self.exported += len(spans)
        else:
            self.failures += 1
            if self.failures == 1 or self.failures % 100 == 0:
                logger.warning("langfuse_export_failed", extra={"observability": {"items": self.failures}})
        return result

    def shutdown(self):
        try:
            self.inner.shutdown()
        except Exception:
            logger.warning("langfuse_export_failed")


class LangfuseRuntime(SpanProcessor):
    def __init__(self, conf: LangfuseConf, env: str, exporter: SpanExporter) -> None:
        self.conf, self.env = conf, env
        self.exporter = SafeExporter(exporter)
        self.batch = BoundedBatch(self.exporter, conf)
        self.active: dict[int, tuple[RequestTrace, dict[str, Any]]] = {}
        self.models: dict[int, ModelContent] = {}
        self.lock = threading.RLock()
        self.dropped = 0
        self.closed = False

    @contextmanager
    def scope(self) -> Iterator[RequestTrace]:
        state = RequestTrace(self, {"langfuse.environment": self.env})
        token = _CURRENT.set(state)
        try:
            yield state
        finally:
            _CURRENT.reset(token)

    def on_start(self, span: Span, parent_context=None):
        state = _CURRENT.get()
        context = span.get_span_context()
        if self.closed or state is None or state.runtime is not self or context is None:
            return
        if context.trace_id % (1 << 64) >= int(self.conf.sample_rate * (1 << 64)):
            return
        with self.lock:
            if len(self.active) >= self.conf.max_queue_size:
                self.dropped += 1
                return
            self.active[context.span_id] = (state, {k: v for k, v in current_context().items()
                                                  if k in ("trace_id", "run_id", "request_id") and v})
            content = _MODEL_CONTENT.get()
            if (span.attributes or {}).get("openinference.span.kind") == "LLM" and content is not None:
                self.models[context.span_id] = content
                if content.input is not None:
                    self.active[context.span_id][1]["langfuse.observation.input"] = content.input

    def on_end(self, span: ReadableSpan):
        if span.context is None:
            return
        with self.lock:
            value = self.active.pop(span.context.span_id, None)
            content = self.models.pop(span.context.span_id, None)
        if value is not None and not self.closed:
            state, extra = value
            if content is not None:
                content.pending.append((span, extra))
            else:
                self._enqueue(span, state, extra)

    def _enqueue(self, span: ReadableSpan, state: RequestTrace, extra: dict[str, Any]) -> None:
        try:
            self.batch.on_end(project(span, self.conf, dict(state.metadata), extra))
        except Exception:
            self.dropped += 1
            if self.dropped == 1:
                logger.warning("langfuse_export_failed")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.batch.force_flush(min(timeout_millis, self.conf.shutdown_timeout_ms))

    def shutdown(self):
        self.closed = True
        self.batch.shutdown(timeout_millis=self.conf.shutdown_timeout_ms)
        with self.lock:
            self.active.clear()
            self.models.clear()

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": not self.closed, "exported": self.exporter.exported,
                "export_failures": self.exporter.failures, "dropped": self.dropped + self.batch.dropped}


def owned_provider() -> TracerProvider | None:
    return _OWNED_PROVIDER


def initialize_langfuse() -> LangfuseRuntime | None:
    global _OWNED_PROVIDER
    exporter: SpanExporter | None = None
    runtime: LangfuseRuntime | None = None
    try:
        conf = langfuse_conf()
        if not conf.enabled:
            return None
        env = current_source().env_type
        with _LOCK:
            for existing in _RUNTIMES:
                if existing.conf == conf and existing.env == env and not existing.closed:
                    return existing
            from openinference.instrumentation import TraceConfig
            from openinference.instrumentation.agno import AgnoInstrumentor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            auth = base64.b64encode((conf.public_key.get_secret_value() + ":" +
                                    conf.secret_key.get_secret_value()).encode()).decode()
            import requests
            session = requests.Session()
            session.trust_env = False  # 不让机器全局代理把本地追踪转发到其他目的地。
            export_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
            if not any(isinstance(f, _ExportLogFilter) for f in export_logger.filters):
                export_logger.addFilter(_ExportLogFilter())
            exporter = OTLPSpanExporter(session=session, endpoint=conf.host.strip().rstrip("/") + "/api/public/otel/v1/traces",
                headers={"Authorization": "Basic " + auth, "x-langfuse-ingestion-version": "4"},
                timeout=conf.export_timeout_s)
            provider = trace.get_tracer_provider()
            if not isinstance(provider, TracerProvider):
                provider = TracerProvider()
                trace.set_tracer_provider(provider)
                _OWNED_PROVIDER = provider
            if not AgnoInstrumentor().is_instrumented_by_opentelemetry:
                AgnoInstrumentor().instrument(tracer_provider=provider,
                    config=TraceConfig(hide_inputs=True, hide_outputs=True))
            runtime = LangfuseRuntime(conf, env, exporter)
            provider.add_span_processor(runtime)
            _RUNTIMES.append(runtime)
            logger.info("langfuse_ready")
            return runtime
    except Exception:
        # 可选观测目的地失败不能改变主服务；不泄露配置、URL 或认证错误正文。
        if runtime is not None:
            runtime.shutdown()
        elif exporter is not None:
            try:
                exporter.shutdown()
            except Exception:
                pass  # 原始初始化故障已由下方安全事件报告。
        logger.warning("langfuse_config_invalid")
        return None


@contextmanager
def workflow_trace_scope(context: Any) -> Iterator[None]:
    state = _CURRENT.get()
    if state is None:
        yield
        return
    if state.root is not None:
        # Chat 已经绑定可信会话；子 Workflow 不覆盖根轮次的身份。
        yield
        return
    with state.runtime.scope():
        correlate(session_id=getattr(context, "session_id", None), user_id=getattr(context, "user_id", None),
                  run_id=getattr(context, "run_id", None))
        yield


def turn_outcome(outcome: str) -> None:
    state = _CURRENT.get()
    if state is not None and state.root is not None:
        state.root.set_attribute("outcome", outcome)
        if outcome != "completed":
            state.root.set_status(StatusCode.ERROR)


def correlate(*, session_id: str | None = None, user_id: str | None = None,
              trace_id: str | None = None, run_id: str | None = None) -> None:
    state = _CURRENT.get()
    if state is None:
        return
    conf = state.runtime.conf
    if session_id is not None:
        state.metadata["langfuse.session.id"] = hmac.new(conf.secret_key.get_secret_value().encode(),
            (state.runtime.env + ":" + (user_id or "") + ":" + session_id).encode(), hashlib.sha256).hexdigest()
    state.sensitive = tuple(dict.fromkeys((*state.sensitive, *(v for v in (session_id, user_id) if v))))
    if trace_id:
        state.metadata["langfuse.trace.metadata.business_trace_id"] = trace_id
    if run_id:
        state.metadata["langfuse.trace.metadata.run_id"] = run_id


def capture(key: str, value: Any, *, root: bool = False) -> None:
    state = _CURRENT.get()
    if state is None or not state.runtime.conf.capture_content:
        return
    target = state.root if root else trace.get_current_span()
    if target is None:
        return
    context = target.get_span_context()
    try:
        with state.runtime.lock:
            entry = state.runtime.active.get(context.span_id)
            if entry is not None:
                cleaned = clean_content(value, state.runtime.conf, state.sensitive)
                entry[1]["langfuse.observation." + key] = cleaned
                if root:
                    entry[1]["langfuse.trace." + key] = cleaned
    except Exception:
        # 观测序列化失败不能覆盖业务返回。
        state.runtime.dropped += 1


@asynccontextmanager
async def model_content_scope(messages: Any) -> AsyncIterator[list[str]]:
    state = _CURRENT.get()
    content = None
    if state is not None and state.runtime.conf.capture_content:
        content = ModelContent(remaining_tool_chars=state.runtime.conf.max_content_chars * 2)
        try:
            content.input = clean_content([{"role": m.role, "content": m.content} for m in islice(messages, 100)],
                                          state.runtime.conf, state.sensitive)
        except Exception:
            state.runtime.dropped += 1
    token = _MODEL_CONTENT.set(content)
    try:
        yield content.parts if content is not None else []
    finally:
        _MODEL_CONTENT.reset(token)
        if content is not None and state is not None:
            try:
                output: Any = content.parts[0] if content.parts else None
                if content.tools:
                    # 完整参数按结构脱敏；截断或取消留下的半截 JSON 不作为明文输出。
                    for call in content.tools:
                        function = call["function"]
                        try:
                            function["arguments"] = json.loads(function["arguments"])
                        except (ValueError, TypeError):
                            function["arguments"] = "[INCOMPLETE]"
                    output = {"content": output, "tool_calls": content.tools}
                cleaned = clean_content(output, state.runtime.conf, state.sensitive) if output is not None else None
                for span, extra in content.pending:
                    if cleaned is not None:
                        extra["langfuse.observation.output"] = cleaned
                    state.runtime._enqueue(span, state, extra)
            except Exception:
                state.runtime.dropped += 1
            finally:
                content.pending.clear()


def capture_model_chunk(parts: list[str], content: Any) -> None:
    state = _CURRENT.get()
    if state is None or not state.runtime.conf.capture_content or not isinstance(content, str):
        return
    limit = state.runtime.conf.max_content_chars * 2
    accumulated = parts[0] if parts else ""
    if len(accumulated) >= limit:
        return
    parts[:] = [(accumulated + content[:limit])[:limit]]


def capture_model_response(response: Any) -> None:
    """只补当前模型调用的 sidecar，流式工具参数保留实际增量，不伪造完整调用。"""
    content, state = _MODEL_CONTENT.get(), _CURRENT.get()
    if content is None or state is None:
        return
    try:
        capture_model_chunk(content.parts, response.content)
        for index, call in enumerate(islice(getattr(response, "tool_calls", None) or [], 100)):
            if content.remaining_tool_chars <= 0:
                break
            value = call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else call
            if not isinstance(value, dict):
                continue
            index = value.get("index", index)
            if not isinstance(index, int) or not 0 <= index < 100:
                continue
            while len(content.tools) <= index:
                content.tools.append({"function": {"name": "", "arguments": ""}})
            target = content.tools[index]
            function = value.get("function") or {}
            for key, source in (("name", function), ("arguments", function), ("id", value), ("type", value)):
                part = source.get(key)
                if isinstance(part, str):
                    part = part[:content.remaining_tool_chars]
                    destination = target["function"] if key in ("name", "arguments") else target
                    destination[key] = destination.get(key, "") + part
                    content.remaining_tool_chars -= len(part)
    except Exception:
        state.runtime.dropped += 1

@contextmanager
def span_scope(name: str, **attrs: Any) -> Iterator[Any]:
    state = _CURRENT.get()
    if state is None:
        yield trace.INVALID_SPAN
        return
    with trace.get_tracer("insurance.langfuse").start_as_current_span(
        name, attributes=attrs, record_exception=False, set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except (asyncio.CancelledError, GeneratorExit):
            span.set_attribute("outcome", "cancelled")
            span.set_status(StatusCode.ERROR)
            raise
        except BaseException as exc:
            span.set_attribute("outcome", "timeout" if isinstance(exc, TIMEOUT_ERRORS) else "failed")
            span.set_attribute("exception.type", type(exc).__name__)
            span.set_status(StatusCode.ERROR)
            raise


def first_visible() -> None:
    state = _CURRENT.get()
    if state is not None and state.root is not None and not state.visible:
        state.visible = True
        state.root.add_event("first_visible")
