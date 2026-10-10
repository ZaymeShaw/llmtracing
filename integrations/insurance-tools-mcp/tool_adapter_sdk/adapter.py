"""固定真实工具适配器与同步遗留函数执行器。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, ParamSpec, TypeVar, cast

from pydantic import ValidationError

from tool_adapter_sdk.identity import identity_kwargs
from tool_adapter_sdk.contracts import (
    HANDLER_API_VERSION,
    OPERATION_BY_ROUTE,
    BackendContext,
    HandlerCallResult,
)
from tool_adapter_sdk.handlers import REQUIRED_HANDLERS_BY_TOOL, RealToolHandlers

from tool_adapter_sdk.diagnostics import diagnostic, diagnostic_scope, validation_details

logger = logging.getLogger(__name__)


class ToolConfigurationError(RuntimeError):
    """注册、版本或 operation 配置错误。"""


class HandlerError(RuntimeError):
    """可安全归一的 handler 异常；retries 仅记录已发生的内部重试。"""

    def __init__(self, message: str = "", *, retries: int = 0) -> None:
        super().__init__(message)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries 必须是非负整数")
        self.retries = retries


class HandlerDenied(HandlerError):
    """handler 明确拒绝当前身份访问。"""


class HandlerTimeout(HandlerError):
    """handler 在自身时限内未完成。"""


class HandlerUnavailable(HandlerError):
    """handler 依赖暂时不可用。"""


class HandlerOverloaded(HandlerError):
    """本地 handler 容量已满；本轮不可自动重试。"""


class HandlerContractError(HandlerError):
    """handler 输入或输出不满足 SDK 契约。"""


CloseCallback = Callable[[], Awaitable[None] | None]


class RealToolAdapter:
    """把既有 BackendClient 路由适配到类型化内网函数表。"""

    def __init__(
        self,
        handlers: RealToolHandlers,
        *,
        adapter_id: str,
        source_version: str,
        close: CloseCallback | None = None,
    ) -> None:
        if not adapter_id.strip() or not source_version.strip():
            raise ValueError("adapter_id 与 source_version 不能为空")
        self.handlers = handlers
        self.adapter_id = adapter_id
        self.source_version = source_version
        self._close_callback = close
        self._closed = False

    def ensure_tool_supported(self, tool_name: str) -> None:
        self.ensure_api_compatible()
        required = REQUIRED_HANDLERS_BY_TOOL.get(tool_name)
        if required is None:
            diagnostic("tool_not_registered", stage="adapter_configuration")
            raise ToolConfigurationError(f"未登记真实工具:{tool_name}")
        missing = [name for name in required if getattr(self.handlers, name) is None]
        if missing:
            diagnostic("handlers_missing", stage="adapter_configuration", missing_count=len(missing))
            raise ToolConfigurationError(f"真实工具 {tool_name} 缺少 handlers:{missing}")

    def ensure_api_compatible(self) -> None:
        """即使暂未选择工具，也必须能独立校验 SDK/handler API 版本。"""
        if self.handlers.api_version != HANDLER_API_VERSION:
            diagnostic("handler_api_version_mismatch", stage="adapter_configuration")
            raise ToolConfigurationError(
                f"handler API 版本不兼容:expected={HANDLER_API_VERSION}, actual={self.handlers.api_version}"
            )

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
    ) -> object:
        if self._closed:
            diagnostic("adapter_closed", stage="adapter_configuration", trace_id=trace_id, request_id=request_id)
            raise ToolConfigurationError("RealToolAdapter 已关闭，不得继续调用")
        operation = OPERATION_BY_ROUTE.get((system, endpoint))
        if operation is None:
            diagnostic("operation_not_registered", stage="adapter_configuration", trace_id=trace_id, request_id=request_id)
            raise ToolConfigurationError(f"未登记 operation:{system}{endpoint}")
        self.ensure_tool_supported(operation.owner_tool)
        handler = getattr(self.handlers, operation.handler_name)
        if handler is None:
            diagnostic("operation_handler_missing", stage="adapter_configuration",
                       operation=operation.name, trace_id=trace_id, request_id=request_id)
            raise ToolConfigurationError(f"operation 缺少 handler:{operation.name}")
        try:
            request = operation.request_model.model_validate(payload)
        except ValidationError as exc:
            diagnostic("invalid_request", stage="adapter_request", error=exc,
                       tool=operation.owner_tool, operation=operation.name, trace_id=trace_id,
                       request_id=request_id, **validation_details(exc, operation.request_model))
            return {"error": "contract"}
        context = BackendContext(
            tool_name=operation.owner_tool,
            agent_id=agent_id,
            trace_id=trace_id,
            request_id=request_id,
            session_id=session_id,
            **identity_kwargs(operation.owner_tool, upstream_user_id=upstream_user_id, empno=empno, agentno=agentno),
        )
        with diagnostic_scope(tool=operation.owner_tool, operation=operation.name,
                              trace_id=trace_id, request_id=request_id):
            retries = 0
            started = time.monotonic()
            stage = "handler"
            try:
                output = await handler(request, context)
                if isinstance(output, HandlerCallResult):
                    result = output.result
                    retries = output.retries
                else:
                    result = output
                    retries = 0
                stage = "adapter_result"
                validated = operation.result_model.model_validate(result)
                return _with_retries(validated.model_dump(mode="json"), retries)
            except HandlerDenied as exc:
                diagnostic("handler_denied", stage=stage, error=exc, retries=exc.retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3))
                return _with_retries({"error": "denied"}, exc.retries)
            except HandlerTimeout as exc:
                diagnostic("handler_timeout", stage=stage, error=exc, retries=exc.retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3))
                return _with_retries({"error": "timeout"}, exc.retries)
            except HandlerOverloaded as exc:
                diagnostic("handler_overloaded", stage=stage, error=exc, retries=exc.retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3))
                return _with_retries({"error": "overloaded"}, exc.retries)
            except HandlerUnavailable as exc:
                diagnostic("handler_unavailable", stage=stage, error=exc, retries=exc.retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3))
                return _with_retries({"error": "upstream"}, exc.retries)
            except HandlerContractError as exc:
                diagnostic("handler_contract", stage=stage, error=exc, retries=exc.retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3))
                return _with_retries({"error": "contract"}, exc.retries)
            except HandlerError as exc:
                diagnostic("handler_error", stage=stage, error=exc, retries=exc.retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3))
                # 直接抛基类(未选具体语义)按家族缺省归一为 upstream;具体子类已在上方命中。
                return _with_retries({"error": "upstream"}, exc.retries)
            except (ValidationError, ValueError, TypeError) as exc:
                details = validation_details(exc, operation.result_model) if isinstance(exc, ValidationError) else {}
                diagnostic("invalid_result", stage=stage, error=exc, retries=retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3), **details)
                return _with_retries({"error": "contract"}, retries)
            except Exception as exc:
                logger.error("handler_unclassified_exception")
                diagnostic("handler_unclassified_exception", stage=stage, error=exc, retries=retries,
                           latency_ms=round((time.monotonic() - started) * 1000, 3))
                return _with_retries({"error": "upstream"}, retries)

    async def aclose(self) -> None:
        """幂等释放内网 Service 资源。"""
        if self._closed:
            return
        self._closed = True
        if self._close_callback is None:
            return
        result = self._close_callback()
        if inspect.isawaitable(result):
            await result


P = ParamSpec("P")
R = TypeVar("R")


def _with_retries(
    payload: dict[str, object], retries: int
) -> dict[str, object] | HandlerCallResult[dict[str, object]]:
    if retries:
        return HandlerCallResult(result=payload, retries=retries)
    return payload


class SyncHandlerRunner:
    """以专属有限线程池包装只读或幂等同步遗留函数。"""

    def __init__(self, max_concurrency: int = 8) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须大于 0")
        self._executor = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="tool-handler")
        self._capacity = asyncio.Semaphore(max_concurrency)
        self._closed = False

    async def run(self, function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
        if self._closed:
            raise RuntimeError("SyncHandlerRunner 已关闭")
        await self._capacity.acquire()
        if self._closed:
            self._capacity.release()
            raise RuntimeError("SyncHandlerRunner 已关闭")
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(self._executor, partial(function, *args, **kwargs))
        except BaseException:
            self._capacity.release()
            raise
        def _release(completed: "asyncio.Future[R]") -> None:
            if not completed.cancelled():
                # 等待方被取消时线程内迟到的异常无人领取,会刷 "exception was never
                # retrieved" 噪音;这里标记消费,容量照常释放。
                completed.exception()
            self._capacity.release()

        future.add_done_callback(_release)
        return cast(R, await asyncio.shield(future))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, partial(self._executor.shutdown, wait=True, cancel_futures=False))


__all__ = [
    "RealToolAdapter",
    "SyncHandlerRunner",
    "ToolConfigurationError",
    "HandlerError",
    "HandlerDenied",
    "HandlerTimeout",
    "HandlerUnavailable",
    "HandlerOverloaded",
    "HandlerContractError",
]
