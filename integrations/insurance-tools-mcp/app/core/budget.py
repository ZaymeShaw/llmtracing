"""Shared execution-chain budget metadata and request-local deadline."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

SERVICE_RESTARTING_REASON = 'service_restarting'
SERVICE_RESTARTING_MESSAGE = '服务正在重启，请稍后重试。'


class ShutdownDeadline:
    """One application's monotonic drain cutoff, shared by its in-flight contexts.

    Trigger once on the owning event loop. Threads only read the published cutoff;
    this object is never persisted or copied into session state.
    """
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.answer_at: float | None = None
        self.delivery_at: float | None = None
        self.finalize_reserve_s: float = 8.0

    def begin(self, answer_s: float, delivery_s: float) -> None:
        if self.started.is_set():
            return
        now = asyncio.get_running_loop().time()
        self.answer_at, self.delivery_at = now + answer_s, now + delivery_s
        self.started.set()

    def answer_cutoff(self, original: float | None) -> float | None:
        if self.answer_at is None:
            return original
        return self.answer_at if original is None else min(original, self.answer_at)


_shutdown_deadline: ContextVar[ShutdownDeadline | None] = ContextVar("shutdown_deadline", default=None)


def current_shutdown_deadline() -> ShutdownDeadline | None:
    return _shutdown_deadline.get()


@contextmanager
def shutdown_deadline_scope(deadline: ShutdownDeadline) -> Iterator[None]:
    token = _shutdown_deadline.set(deadline)
    try:
        yield
    finally:
        _shutdown_deadline.reset(token)


def _answer_cutoff(original: float | None) -> float | None:
    shutdown = current_shutdown_deadline()
    return original if shutdown is None else shutdown.answer_cutoff(original)


# 使用当前事件循环的单调时钟；只活在请求上下文，不写入可持久化的预算账本。
_answer_deadline: ContextVar[float | None] = ContextVar("answer_deadline", default=None)


@contextmanager
def answer_deadline_scope(deadline: float | None):
    parent = _answer_deadline.get()
    token = _answer_deadline.set(parent if deadline is None else deadline if parent is None else min(deadline, parent))
    try:
        yield
    finally:
        _answer_deadline.reset(token)


# 同步 hooks 经 to_thread 继承原事件循环时钟，不能在线程内重新找 loop。
_turn_clock: ContextVar[Callable[[], float]] = ContextVar("turn_clock", default=time.monotonic)


_turn_deadline: ContextVar[float | None] = ContextVar("turn_deadline", default=None)


@contextmanager
def turn_deadline_scope(deadline: float | None) -> Iterator[None]:
    """发布实际执行 deadline；嵌套流程只可收紧，None 继承外层时限。"""
    parent = _turn_deadline.get()
    effective = parent if deadline is None else deadline if parent is None else min(parent, deadline)
    token = _turn_deadline.set(effective)
    try:
        clock = asyncio.get_running_loop().time
    except RuntimeError:
        clock = _turn_clock.get()
    clock_token = _turn_clock.set(clock)
    try:
        yield
    finally:
        _turn_clock.reset(clock_token)
        _turn_deadline.reset(token)


def remaining_turn_seconds() -> float | None:
    """返回当前请求真实剩余时间，不使用 S1 起点或可能受校时影响的 wall clock。"""
    deadline = _answer_cutoff(_turn_deadline.get())
    return None if deadline is None else max(0.0, deadline - _turn_clock.get()())


def remaining_answer_seconds(answer_reserve_s: float) -> float | None:
    """Remaining execution time; H-08 publishes answer_at so the reserve is deducted once."""
    deadline = _answer_cutoff(_turn_deadline.get())
    if deadline is None:
        return None
    now = _turn_clock.get()()
    answer_at = _answer_deadline.get()
    # A drain-only deadline already reserves transport time; do not subtract the
    # normal turn reserve again when ordinary timeouts were disabled.
    answer_at = deadline if _turn_deadline.get() is None else (
        min(deadline, answer_at) if answer_at is not None else deadline - answer_reserve_s)
    return TurnDeadline(deadline, answer_at).optional_seconds(max(0.0, deadline - now), now=now)


def current_deadline(finalize_reserve_s: float, answer_reserve_s: float = 0) -> TurnDeadline | None:
    """读取本轮截止；快照已经发布 answer_at 时不再次扣交付预留。"""
    hard_at = _turn_deadline.get()
    if hard_at is None:
        shutdown = current_shutdown_deadline()
        if shutdown is not None and shutdown.answer_at is not None:
            return TurnDeadline(shutdown.answer_at, shutdown.answer_at, shutdown.finalize_reserve_s)
    if hard_at is None:
        return None
    answer_at = _answer_deadline.get()
    answer_at = min(hard_at, answer_at) if answer_at is not None else hard_at - answer_reserve_s
    return TurnDeadline(hard_at, answer_at, finalize_reserve_s)


def set_budget_profile(budget: dict, profile: str) -> None:
    """Record the RPC budget profile used by the current execution chain."""
    if profile not in {"direct", "workflow", "agent"}:
        raise ValueError(f"未知预算档位:{profile}")
    budget["_profile"] = profile


def ensure_budget_profile(budget: dict, profile: str) -> None:
    """Set a standalone branch profile without overriding its parent chain."""
    if "_profile" not in budget:
        set_budget_profile(budget, profile)


@dataclass(frozen=True)
class TurnDeadline:
    """Unified monotonic clock model for admission and execution supervision.

    Create once at admission. Optional actions share answer_at and never subtract a
    reserve again. No ContextVar persistence or wall-clock timestamps are involved.
    """

    hard_at: float
    answer_at: float
    finalize_reserve_s: float = 0

    def __post_init__(self) -> None:
        if (not math.isfinite(self.hard_at) or not math.isfinite(self.answer_at) or self.answer_at > self.hard_at
                or not math.isfinite(self.finalize_reserve_s) or self.finalize_reserve_s < 0):
            raise ValueError("invalid monotonic deadlines")

    def cutoff(self, kind: str) -> float:
        """工具和后端共用同一截止；模型收尾可使用完整执行窗口。"""
        answer_at = _answer_cutoff(self.answer_at)
        assert answer_at is not None
        if kind in {"tool", "backend"}:
            cutoff = self.answer_at - self.finalize_reserve_s
            shutdown = current_shutdown_deadline()
            if shutdown is not None and shutdown.answer_at is not None:
                cutoff = min(cutoff, shutdown.answer_at - shutdown.finalize_reserve_s)
            return cutoff
        if kind in {"model", "finish"}:
            return answer_at
        raise ValueError(f"unknown deadline kind: {kind}")

    def remaining(self, kind: str, *, now: float | None = None) -> float:
        current = _turn_clock.get()() if now is None else now
        if not math.isfinite(current):
            raise ValueError('clock must be finite')
        return max(0.0, self.cutoff(kind) - current)

    def can_start(self, kind: str, admission_s: float = 0, *, observed_p95_s: float = 0,
                  now: float | None = None) -> bool:
        if any(not math.isfinite(v) or v < 0 for v in (admission_s, observed_p95_s)):
            raise ValueError("admission thresholds must be finite and nonnegative")
        remaining = self.remaining(kind, now=now)
        return remaining > 0 and remaining >= max(admission_s, observed_p95_s)

    @classmethod
    def from_admission(cls, admitted_at: float, hard_ceiling_s: float, answer_reserve_s: float) -> TurnDeadline:
        if not all(math.isfinite(value) for value in (admitted_at, hard_ceiling_s, answer_reserve_s)):
            raise ValueError("deadline values must be finite")
        if not 0 <= answer_reserve_s < hard_ceiling_s:
            raise ValueError("answer_reserve_s must be nonnegative and below hard ceiling")
        hard_at = admitted_at + hard_ceiling_s
        return cls(hard_at=hard_at, answer_at=hard_at - answer_reserve_s)

    def optional_seconds(self, config_limit_s: float, *, now: float | None = None) -> float:
        if not math.isfinite(config_limit_s) or config_limit_s < 0:
            raise ValueError("optional limit must be finite and nonnegative")
        current = time.monotonic() if now is None else now
        if not math.isfinite(current):
            raise ValueError("clock must be finite")
        return max(0.0, min(config_limit_s, self.cutoff("finish") - current))
