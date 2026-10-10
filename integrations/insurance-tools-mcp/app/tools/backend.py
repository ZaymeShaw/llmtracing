"""类型化 call_backend 门面、真实后端封装与统一调用计量。"""
from __future__ import annotations

from app.conf.runtime import read_once

from app.observability.langfuse_runtime import span_scope, capture
from opentelemetry.trace import StatusCode

import asyncio
from tool_adapter_sdk.compat import timeout_at, TIMEOUT_ERRORS
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from app.observability.context import log_context
from app.observability.progress import ProgressEvent, emit_progress
from app.observability.tool_calls import mark_backend_call, mark_backend_returned
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.turn_execution import tracked_call
from app.core.envelope import make_envelope
from app.core.admission import TOOL_CAPACITY_MESSAGE, current_capacity_outcome
from app.schemas import EnvelopeStatus, ErrorCode, SourceRef, ToolError
from app.tools.mode import (
    BackendConfigurationError,
    execution_tools_conf,
    require_tool_execution_context,
)
from tool_adapter_sdk.identity import identity_kwargs
from tool_adapter_sdk.deadline import handler_deadline_scope
from tool_adapter_sdk.adapter import HandlerOverloaded, RealToolAdapter, ToolConfigurationError
from tool_adapter_sdk.contracts import (
    ContractModel,
    HandlerCallResult,
    OperationSpec,
)

from tool_adapter_sdk.diagnostics import diagnostic, diagnostic_scope, validation_details

logger = logging.getLogger(__name__)


class BackendCallMeta(BaseModel):
    """不进入业务 ToolEnvelope 的统一后端调用元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    adapter_id: str
    source_version: str
    latency_ms: int = Field(default=0, ge=0)
    attempts: int = Field(default=1, ge=0)


@dataclass(frozen=True)
class BackendCallResult:
    """BackendClient 可选返回的载荷、重试次数与内部元数据。"""

    payload: object
    retries: int = 0
    meta: BackendCallMeta | None = None


class BackendClient(Protocol):
    """真实后端及调用方显式注入实现共享的最小端口。"""

    async def call(
        self,
        system: str,
        endpoint: str,
        payload: dict[str, Any],
        *,
        agent_id: str,
        trace_id: str,
        request_id: str,
        session_id: str | None = None,
        upstream_user_id: str | None = None,
        empno: str | None = None,
        agentno: str | None = None,
    ) -> object: ...


class RealBackendClient:
    """按冻结的工具上下文调用真实适配器，并保留计量与资源所有权。"""

    def __init__(
        self,
        *,
        real_adapter: RealToolAdapter,
        owned_clients: tuple[object, ...] = (),
    ) -> None:
        if not isinstance(real_adapter, RealToolAdapter):
            raise BackendConfigurationError("真实后端必须注入 RealToolAdapter")
        self._real_adapter = real_adapter
        self._owned_clients = owned_clients
        self._closed = False

    def ensure_tool_supported(self, tool_name: str) -> None:
        self._real_adapter.ensure_tool_supported(tool_name)

    async def call(
        self,
        system: str,
        endpoint: str,
        payload: dict[str, Any],
        *,
        agent_id: str,
        trace_id: str,
        request_id: str,
        session_id: str | None = None,
        upstream_user_id: str | None = None,
        empno: str | None = None,
        agentno: str | None = None,
    ) -> BackendCallResult:
        context = require_tool_execution_context()
        self.ensure_tool_supported(context.tool_name)
        started = time.perf_counter()
        response = await self._real_adapter.call(
            system,
            endpoint,
            payload,
            agent_id=agent_id,
            trace_id=trace_id,
            request_id=request_id,
            **({"session_id": session_id} if session_id is not None else {}),
            **identity_kwargs(context.tool_name, upstream_user_id=upstream_user_id, empno=empno, agentno=agentno),
        )
        if isinstance(response, HandlerCallResult):
            raw = response.result
            retries = response.retries
        elif isinstance(response, BackendCallResult):
            raw = response.payload
            retries = response.retries
        else:
            raw = response
            retries = 0
        return BackendCallResult(
            raw,
            retries=retries,
            meta=BackendCallMeta(
                tool_name=context.tool_name,
                adapter_id=self._real_adapter.adapter_id,
                source_version=self._real_adapter.source_version,
                latency_ms=int((time.perf_counter() - started) * 1000),
                # overload 表示本次尝试在依赖准入处被拒，只有此前的重试尝试已执行。
                attempts=retries if isinstance(raw, dict) and raw.get("error") == "overloaded" else retries + 1,
            ),
        )

    async def aclose(self) -> None:
        """只关闭组合根明确交由当前后端所有的子资源。"""
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for child in self._owned_clients:
            if id(child) in seen:
                continue
            seen.add(id(child))
            close = getattr(child, "aclose", None)
            if close is not None:
                await close()


# 服务端运行时依赖键；三条执行路径共享，避免字符串漂移。
BACKEND_CLIENT_DEPENDENCY = "backend_client"
UPSTREAM_USER_ID_DEPENDENCY = "upstream_user_id"


def backend_client_from_dependencies(
    dependencies: Mapping[str, object] | None,
) -> BackendClient | None:
    """从可信运行时依赖解析 BackendClient，不把对象写入业务状态。"""
    client = (dependencies or {}).get(BACKEND_CLIENT_DEPENDENCY)
    if client is not None and not callable(getattr(client, "call", None)):
        raise TypeError("backend_client 必须实现异步 call()")
    return cast(BackendClient | None, client)


@tracked_call
async def call_backend(
    operation: OperationSpec,
    request: ContractModel | Mapping[str, object],
    *,
    agent_id: str,
    trace_id: str,
    budget: dict[str, Any],
    client: BackendClient | None = None,
    session_id: str | None = None,
    upstream_user_id: str | None = None,
    empno: str | None = None,
    agentno: str | None = None,
) -> dict[str, Any]:
    """验证 operation 所有权并执行一次后端调用。"""
    context = require_tool_execution_context()
    if context.tool_name != operation.owner_tool:
        raise BackendConfigurationError(
            f"工具 {context.tool_name} 不得调用 operation {operation.name}"
        )
    try:
        request_dto = operation.request_model.model_validate(request)
    except ValidationError as exc:
        diagnostic("invalid_operation_request", stage="request_validation", tool=operation.owner_tool,
                   operation=operation.name, trace_id=trace_id, **validation_details(exc, operation.request_model))
        raise BackendConfigurationError(f"operation 请求不符合契约:{operation.name}") from exc
    request_payload = request_dto.model_dump(mode="json")

    # 只复用调用方或已持有租约中的客户端，不在工具层隐式构造资源。
    if client is None:
        from app.tools.config_scope import current_generation

        generation = current_generation()
        client = generation.client if generation is not None else None
    if client is None:
        raise BackendConfigurationError("后端调用必须显式注入 client 或持有配置代租约")

    # 已拒绝的 operation 在本轮不再次探测，即使模型改写参数也不重新压同一资源。
    if operation.name in budget.get("capacity_refused_operations", ()):
        diagnostic("operation_capacity_previously_refused", stage="admission",
                   tool=operation.owner_tool, operation=operation.name, trace_id=trace_id, attempts=0)
        return {"error": "overloaded"}

    # 所有执行链的后端 RPC 均在这里做同一处准入，避免 Direct/Workflow/Agent
    # 各自维护一套计数或在工具内部绕过预算。缺省档位保持 agent，兼容直接调用工具
    # 的单元测试与旧集成入口；正式编排入口会显式登记 direct/workflow/agent。
    from app.conf import harness_conf

    profile_name = str(budget.get("_profile", "agent"))
    harness = read_once("harness", harness_conf)
    try:
        profile = harness.profiles[profile_name]
    except KeyError as exc:
        raise BackendConfigurationError(f"未知预算档位:{profile_name}") from exc
    rpc_limit = profile.max_rpc_calls
    if harness.tool_call_limits_enabled and budget.get("backend_rpc_calls", 0) >= rpc_limit:
        return _rpc_refused(operation, profile_name, trace_id, budget, "rpc_limit",
                            f"{profile_name} 执行链已达后端 RPC 上限 {rpc_limit}", limit=rpc_limit)

    clock = harness.deadline()
    deadline = clock.cutoff('backend') if clock is not None else None
    admission_s = harness.admission_ms["tool_call"] / 1000
    remaining = clock.remaining('backend') if clock is not None else None
    time_reason = "本轮剩余时间不足，已预留答案收尾时间，停止发起后端查询"
    if clock is not None and not clock.can_start('backend', admission_s):
        return _rpc_refused(operation, profile_name, trace_id, budget, "time_budget", time_reason,
                                    remaining_s=remaining, threshold_s=admission_s)

    budget["backend_rpc_calls"] = budget.get("backend_rpc_calls", 0) + 1
    request_id = f"CALL-{trace_id}-{budget['backend_rpc_calls']:02d}"
    # 过程可见性（2026-09-19）：Direct/Workflow 链的 RPC 没有 Agno 原生 ToolCall
    # 投影，经 contextvar 观察者直连 SSE activity；Agent 链已有原生投影，跳过防双发。
    progress = profile_name != "agent"
    with diagnostic_scope(tool=operation.owner_tool, operation=operation.name, trace_id=trace_id, request_id=request_id), log_context(trace_id=trace_id, request_id=request_id), span_scope("backend." + operation.name, operation=operation.name) as observation:
        capture("input", request_payload)
        started = time.perf_counter()
        meta: BackendCallMeta | None = None
        log_status = "failed"
        stage = "progress_started"
        dispatched = False
        latency_ms: int | None = None
        try:
            mark_backend_call()
            if progress:
                await emit_progress(ProgressEvent(
                    name=context.tool_name, phase="started", activity_id=request_id,
                    input=request_payload,
                ))
            # SSE 进度可能等待背压，实际外呼前重新准入，不能刷新本次 deadline。
            remaining = clock.remaining('backend') if clock is not None else None
            if clock is not None and not clock.can_start('backend', admission_s):
                raw: object = _rpc_refused(operation, profile_name, trace_id, budget, "time_budget", time_reason,
                                    remaining_s=remaining, threshold_s=admission_s)
            else:
                stage = "backend"
                try:
                    with handler_deadline_scope(deadline):
                        async with timeout_at(deadline):
                            dispatched = True
                            response = await client.call(
                                operation.system,
                                operation.endpoint,
                                request_payload,
                                agent_id=agent_id,
                                trace_id=trace_id,
                                request_id=request_id,
                                **({"session_id": session_id}
                                   if session_id is not None else {}),
                                **identity_kwargs(context.tool_name, upstream_user_id=upstream_user_id, empno=empno, agentno=agentno),
                            )
                    if isinstance(response, BackendCallResult):
                        raw = response.payload
                        meta = response.meta
                        if response.retries:
                            budget["backend_retries"] = budget.get("backend_retries", 0) + response.retries
                    elif isinstance(response, HandlerCallResult):
                        raw = response.result
                        if response.retries:
                            budget["backend_retries"] = budget.get("backend_retries", 0) + response.retries
                    else:
                        raw = response
                    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                        diagnostic("result_after_deadline", stage="backend_return")
                        raw = {"error": "timeout"}  # 不消费阻塞或吞取消后迟到的结果。
                except HandlerOverloaded as exc:
                    raw = {"error": "overloaded"}
                    meta = BackendCallMeta(tool_name=context.tool_name, adapter_id="injected",
                                           source_version="unknown", attempts=exc.retries)
                    if exc.retries:
                        budget["backend_retries"] = budget.get("backend_retries", 0) + exc.retries
                except TIMEOUT_ERRORS as exc:
                    diagnostic("backend_deadline_exceeded", stage="backend", error=exc)
                    raw = {"error": "timeout"}
                except (ToolConfigurationError, BackendConfigurationError) as exc:
                    diagnostic("backend_configuration_invalid", stage="configuration", error=exc)
                    log_status = "configuration_error"
                    raise
                except Exception as exc:
                    diagnostic("unexpected_backend_exception", stage="backend", error=exc)
                    raw = {"error": "upstream"}

            stage = "validation"
            if not isinstance(raw, dict):
                diagnostic("invalid_backend_result_type", stage="result_validation", actual_type=type(raw).__name__)
                raw = {"error": "contract"}
            elif "error" not in raw:
                try:
                    raw = operation.result_model.model_validate(raw).model_dump(mode="json")
                except (ValidationError, ValueError, TypeError) as exc:
                    details = validation_details(exc, operation.result_model) if isinstance(exc, ValidationError) else {}
                    diagnostic("invalid_backend_result", stage="result_validation", error=exc, **details)
                    raw = {"error": "contract"}
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    diagnostic("result_validation_after_deadline", stage="result_validation")
                    raw = {"error": "timeout"}
            if raw.get("error") == "overloaded":
                refused_operations = budget.setdefault("capacity_refused_operations", [])
                if operation.name not in refused_operations:
                    refused_operations.append(operation.name)
                outcome = current_capacity_outcome()
                if outcome is not None:
                    outcome.tools.add(operation.owner_tool)
            latency_ms = int((time.perf_counter() - started) * 1000)
            raw.setdefault("request_id", request_id)
            raw["latency_ms"] = latency_ms  # type: ignore[assignment]
            if progress:
                stage = "progress_completed"
                await emit_progress(ProgressEvent(
                    name=context.tool_name, phase="completed", activity_id=request_id,
                    status=str(raw.get("error") or "ok"), latency_ms=latency_ms,
                    output=raw,
                ))
            capture("output", raw)
            observation.set_attribute("outcome", str(raw.get("error") or "ok"))
            if raw.get("error"):
                observation.set_status(StatusCode.ERROR)
            if meta is not None:
                observation.set_attribute("attempts", meta.attempts)
            log_status = str(raw.get("error") or "ok")
            stage = "completed"
            mark_backend_returned()
            return raw
        except asyncio.CancelledError:
            log_status = "cancelled"
            # completed 进度入队也可能阻塞；取消耗时须包含此处等待。
            latency_ms = None
            raise
        finally:
            # 在工具/日志上下文退出前同步记录；不再 await，避免取消后重入进度背压。
            # 返回、配置异常、RPC/进度等待取消统一从这里产生一次结束记录。
            _log_backend_call(
                operation, trace_id, request_id,
                latency_ms if latency_ms is not None else int((time.perf_counter() - started) * 1000),
                meta, log_status, stage, dispatched=dispatched,
            )


def _rpc_refused(
    operation: OperationSpec, profile: str, trace_id: str, budget: dict[str, Any], category: str, reason: str,
    **details: Any,
) -> dict[str, Any]:
    from app.core.audit import count_refusal
    from app.core.model_budget import current_model_budget
    model_budget = current_model_budget()
    # 只停止拥有该RPC账本的本轮；共享ContextVar中的无关调用不能污染本轮。
    if model_budget is not None and model_budget.state is not None and model_budget.state.get("budget") is budget:
        model_budget.stop_tools("rpc" if category == "rpc_limit" else "deadline", blocked=True)
    count_refusal(budget, "rpc_" + category)
    diagnostic(category, stage="admission", tool=operation.owner_tool, operation=operation.name,
               trace_id=trace_id, current=budget.get("backend_rpc_calls", 0), attempts=0, **details)
    logger.warning("backend_rpc_refused", extra={"observability": {
        "tool_name": operation.owner_tool, "mode": profile, "category": category,
        "trace_id": trace_id, "total": budget.get("backend_rpc_calls", 0),
    }})
    return {"error": "budget", "status": "BUDGET", "code": "BUDGET", "budget_reason": reason}


def _log_backend_call(
    operation: OperationSpec,
    trace_id: str,
    request_id: str,
    latency_ms: int,
    meta: BackendCallMeta | None,
    status: str,
    stage: str,
    *,
    dispatched: bool,
) -> None:
    execution = require_tool_execution_context()
    attempts: int | None = meta.attempts if meta else 1
    if not dispatched or (status == "overloaded" and meta is None):
        attempts = 0
    elif status in {"cancelled", "timeout"} and meta is None:
        # RPC 中断时无法知道适配器的重试次数。
        attempts = None
    fields = {
        "tool_name": execution.tool_name,
        "mode": "real",
        "config_version": execution.config_version,
        "env_type": execution.env_type,
        "config_revision": execution.config_revision,
        "adapter_id": meta.adapter_id if meta else "injected",
        "source_version": meta.source_version if meta else "unknown",
        "system": operation.system,
        "endpoint": operation.endpoint,
        "trace_id": trace_id,
        "request_id": request_id,
        "attempts": attempts,
        "status": status,
        "stage": stage,
        "latency_ms": latency_ms,
    }
    logger.log(logging.WARNING if status == "overloaded" else logging.INFO,
               "tool_backend_call", extra={"tool_backend": fields})


def error_outcome(raw: dict[str, Any]) -> tuple[EnvelopeStatus, ToolError] | None:
    """后端错误键映射为既有 ToolEnvelope 状态与错误码。"""
    key = raw.get("error")
    if key is None:
        return None
    if key == "overloaded":
        # 宿主准入的机械结果不能被部署错误映射改成可重试的远端故障。
        return EnvelopeStatus.ERROR, ToolError(code=ErrorCode.OVERLOADED, retryable=False)
    table = execution_tools_conf().error_normalization
    entry = table.get(str(key), table["upstream"])
    return entry.status, ToolError(code=entry.code, retryable=entry.retryable)


def error_envelope(
    outcome: tuple[EnvelopeStatus, ToolError],
    *,
    source: SourceRef,
    raw: dict[str, Any],
) -> str:
    """归一化错误映射为既有业务信封。"""
    status, tool_error = outcome
    hint = {
        EnvelopeStatus.DENIED: "无权限访问该数据(仅限本人名下客户与保单),不重试",
        EnvelopeStatus.TIMEOUT: "后端查询超时,可稍后重试一次",
    }.get(status, "后端服务暂时不可用")
    if raw.get("error") == "contract":
        hint = "上游响应格式不符合接口契约，请联系服务维护人员"
    if tool_error.code is ErrorCode.OVERLOADED:
        hint = TOOL_CAPACITY_MESSAGE
    return make_envelope(
        status=status,
        error=tool_error,
        hint=hint,
        gaps=[TOOL_CAPACITY_MESSAGE] if tool_error.code is ErrorCode.OVERLOADED else [],
        source=source,
        request_id=raw.get("request_id"),
        latency_ms=raw.get("latency_ms"),
    )


__all__ = [
    "BACKEND_CLIENT_DEPENDENCY",
    "UPSTREAM_USER_ID_DEPENDENCY",
    "BackendCallMeta",
    "BackendCallResult",
    "BackendClient",
    "RealBackendClient",
    "backend_client_from_dependencies",
    "call_backend",
    "error_envelope",
    "error_outcome",
]
