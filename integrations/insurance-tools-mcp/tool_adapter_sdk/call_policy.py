"""内网异步 handler 可选的总预算与有限重试辅助，不创建传输客户端。"""
from __future__ import annotations

import asyncio
from tool_adapter_sdk.compat import timeout_at, TIMEOUT_ERRORS
import math
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from tool_adapter_sdk.adapter import (
    HandlerContractError,
    HandlerError,
    HandlerTimeout,
    HandlerUnavailable,
)
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.contracts import HandlerCallResult
from tool_adapter_sdk.deadline import handler_deadline
from tool_adapter_sdk.diagnostics import diagnostic

ResultT = TypeVar("ResultT")


async def call_with_policy(
    call: Callable[[], Awaitable[ResultT]],
    config: HandlerConfig,
    *,
    remaining_s: float | None = None,
) -> HandlerCallResult[ResultT]:
    """在一次总时限内调用无内部重试的异步函数，并上报实际重试次数。

    call 自行绑定 config.url、现有客户端和可信身份；不得返回 HandlerCallResult
    或自行叠加重试。remaining_s 和 handler_deadline_scope 均由宿主提供，
    与 config.timeout_s 取更早的截止点，不从业务参数读取。
    取消保持原语义；被调用协程须配合取消，不能阻塞事件循环或吞掉取消。
    """
    if remaining_s is not None and (
        isinstance(remaining_s, bool)
        or not isinstance(remaining_s, (int, float))
        or not math.isfinite(remaining_s)
        or remaining_s <= 0
    ):
        diagnostic("invalid_remaining_budget", stage="call_policy", attempts=0)
        raise ValueError("remaining_s 必须为有限正数")
    budget = config.timeout_s if remaining_s is None else min(config.timeout_s, remaining_s)
    loop = asyncio.get_running_loop()
    deadline = handler_deadline(budget)
    retries = 0
    attempts = 0
    started = loop.time()
    backoff = config.retry.initial_backoff_s
    try:
        async with timeout_at(deadline):
            for attempt in range(config.retry.max_retries + 1):
                # 退避结束后才计入下一次重试；预算用尽时不得发起新调用。
                if loop.time() >= deadline:
                    diagnostic("deadline_exhausted", stage="call_policy", attempts=attempts,
                               timeout_s=budget, max_retries=config.retry.max_retries,
                               latency_ms=round((loop.time() - started) * 1000, 3))
                    raise HandlerTimeout(retries=retries)
                retries = attempt
                attempts += 1
                attempt_started = loop.time()
                try:
                    result = await _call_once(call, retries)
                except (HandlerUnavailable, HandlerTimeout) as exc:
                    diagnostic("attempt_failed", stage="call_policy", error=exc, attempts=attempts,
                               timeout_s=budget, max_retries=config.retry.max_retries,
                               latency_ms=round((loop.time() - started) * 1000, 3))
                    if attempt == config.retry.max_retries:
                        diagnostic("retry_exhausted", stage="call_policy", attempts=attempts,
                                   timeout_s=budget, max_retries=config.retry.max_retries)
                        raise
                    # A slow failure predicts another slow attempt. Do not spend
                    # the remainder starting an attempt that cannot fit.
                    if deadline - loop.time() <= loop.time() - attempt_started:
                        diagnostic('retry_budget_insufficient', stage='call_policy', attempts=attempts,
                                   timeout_s=budget, max_retries=config.retry.max_retries)
                        raise
                    diagnostic("retry_scheduled", stage="call_policy", attempts=attempts,
                               timeout_s=budget, max_retries=config.retry.max_retries)
                except HandlerError as exc:
                    diagnostic("attempt_not_retryable", stage="call_policy", error=exc, attempts=attempts,
                               timeout_s=budget, max_retries=config.retry.max_retries,
                               latency_ms=round((loop.time() - started) * 1000, 3))
                    raise
                else:
                    if loop.time() >= deadline:
                        diagnostic("deadline_exhausted", stage="call_policy", attempts=attempts,
                                   timeout_s=budget, max_retries=config.retry.max_retries,
                                   latency_ms=round((loop.time() - started) * 1000, 3))
                        raise HandlerTimeout(retries=retries)
                    return HandlerCallResult(result=result, retries=retries)
                # 全抖动指数退避仍处于同一 timeout 作用域，避免每次尝试重置预算。
                await asyncio.sleep(random.uniform(0.0, backoff))
                backoff = min(config.retry.max_backoff_s, backoff * 2.0)
    except TIMEOUT_ERRORS as exc:
        diagnostic("deadline_exhausted", stage="call_policy", error=exc, attempts=attempts,
                   timeout_s=budget, max_retries=config.retry.max_retries,
                   latency_ms=round((loop.time() - started) * 1000, 3))
        raise HandlerTimeout(retries=retries) from exc
    raise AssertionError("有限重试循环必须返回或抛出异常")


async def _call_once(call: Callable[[], Awaitable[ResultT]], retries: int) -> ResultT:
    """将单次调用错误归一并附带计量，拒绝嵌套重试包装。"""
    try:
        result = await call()
        if isinstance(result, HandlerCallResult):
            diagnostic("nested_result_wrapper", stage="handler_attempt", attempts=retries + 1)
            raise HandlerContractError("call 必须返回未包装的业务结果")
        return result
    except HandlerError as exc:
        if exc.retries:
            diagnostic("nested_retry_rejected", stage="handler_attempt", attempts=retries + 1,
                       reported_retries=exc.retries)
            raise HandlerContractError("call 不得自行叠加重试", retries=retries + exc.retries) from exc
        exc.retries = retries
        raise
    except TIMEOUT_ERRORS as exc:
        diagnostic("attempt_timeout", stage="handler_attempt", error=exc, attempts=retries + 1)
        raise HandlerTimeout(retries=retries) from exc
    except (ValueError, TypeError) as exc:
        diagnostic("attempt_contract_error", stage="handler_attempt", error=exc, attempts=retries + 1)
        raise HandlerContractError(retries=retries) from exc
    except Exception as exc:
        diagnostic("attempt_unclassified_exception", stage="handler_attempt", error=exc, attempts=retries + 1)
        # 未识别错误不猜测可重试性；取消不属于 Exception，不会在此被吞掉。
        raise HandlerError(retries=retries) from exc


__all__ = ["call_with_policy"]
