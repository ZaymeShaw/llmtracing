"""进程内准入：请求即时准入，模型与依赖支持有界异步等待。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic

from tool_adapter_sdk.compat import timeout, TIMEOUT_ERRORS

logger = logging.getLogger(__name__)


class CapacityExceeded(RuntimeError):
    """本地资源准入拒绝，不表示远端调用失败。"""


@dataclass(eq=False)
class _Waiter:
    future: asyncio.Future[None]
    loop: asyncio.AbstractEventLoop
    user: str
    continuation: bool
    entered: float
    ticket: Ticket | None = None


class Admission:
    def __init__(self, maximum: int | None, per_user: int | None = None, *, resource: str = "request",
                 wait_timeout_s: float | None = None) -> None:
        # None disables only the duplicate global gate; per-user counts and telemetry remain.
        self.maximum, self.per_user = maximum, per_user
        self.resource = resource
        self.wait_timeout_s = wait_timeout_s
        self._active = 0
        self._users: dict[str, int] = {}
        self._peak = 0
        self._rejected = 0
        self._lock = Lock()
        # Insertion order is FIFO within each lane. Assigned tickets leave the
        # queue but remain owned by the waiter until its coroutine claims them.
        self._waiters: dict[_Waiter, None] = {}
        self._queued = self._cancelled_waiters = self._continuation_rejected = 0
        self._wait_ms_total = self._wait_ms_max = 0.0

    def _full(self, user: str) -> bool:
        return ((self.maximum is not None and self._active >= self.maximum)
                or (self.per_user is not None and self._users.get(user, 0) >= self.per_user))

    def _refuse(self, continuation: bool = False) -> None:
        self._rejected += 1
        self._continuation_rejected += int(continuation)
        logger.warning("capacity_refused", extra={"observability": {
            "total": self._active, "category": "global" if self.per_user is None or (self.maximum is not None and self._active >= self.maximum) else "user",
            "resource": self.resource,
            "continuation": continuation,
        }})
        raise CapacityExceeded("capacity exhausted")

    def _grant(self, user: str) -> Ticket:
        self._active += 1
        self._peak = max(self._active, self._peak)
        self._users[user] = self._users.get(user, 0) + 1
        return Ticket(self, user)

    def acquire(self, user: str = "") -> Ticket:
        with self._lock:
            self._dispatch_locked()
            if self._full(user):
                self._refuse()
            return self._grant(user)

    def _release_locked(self, ticket: Ticket) -> None:
        if ticket.released:
            return
        ticket.released = True
        self._active -= 1
        count = self._users[ticket.user] - 1
        if count:
            self._users[ticket.user] = count
        else:
            del self._users[ticket.user]

    def _dispatch_locked(self) -> None:
        # Reserve under the same lock as release, so newcomers cannot steal a
        # queued slot. Wake only its owner, on that owner's event loop.
        for continuation in (True, False):
            for waiter in tuple(self._waiters):
                if waiter.future.done() or waiter.loop.is_closed():
                    self._waiters.pop(waiter, None)
                    continue
                if waiter.continuation != continuation or self._full(waiter.user):
                    continue
                self._waiters.pop(waiter)
                waiter.ticket = self._grant(waiter.user)
                try:
                    waiter.loop.call_soon_threadsafe(_wake_waiter, waiter)
                except RuntimeError:
                    self._release_locked(waiter.ticket)
                    waiter.ticket = None

    async def acquire_wait(self, wait_s: float, user: str = "", *, continuation: bool = False) -> Ticket:
        """Continuation first, FIFO per lane; cancellation relinquishes handoffs.

        This coroutine claims a reserved ticket only after checking cancellation.
        Both the wake callback and exception cleanup may release it; release is
        idempotent. No provider call or budget charge takes place while queued.
        """
        loop = asyncio.get_running_loop()
        _check_cancelled()
        with self._lock:
            self._dispatch_locked()
            if not self._full(user):
                return self._grant(user)
            if wait_s <= 0:
                self._refuse(continuation)
            waiter = _Waiter(loop.create_future(), loop, user, continuation, monotonic())
            self._waiters[waiter] = None
            self._queued += 1
        try:
            try:
                # The request task owns the timeout; no child wait_for can
                # swallow its cancellation when the wakeup arrives concurrently.
                async with timeout(wait_s):
                    await asyncio.shield(waiter.future)
            except TIMEOUT_ERRORS:
                _check_cancelled()
                with self._lock:
                    # An available handoff at the boundary is usable; the model
                    # budget still checks its actual remaining execution time.
                    if waiter.ticket is None:
                        self._refuse(continuation)
            _check_cancelled()
            with self._lock:
                ticket = waiter.ticket
                assert ticket is not None
                waiter.ticket = None  # Ownership has moved to this coroutine.
            return ticket
        except BaseException as exc:
            with self._lock:
                self._waiters.pop(waiter, None)
                if isinstance(exc, asyncio.CancelledError):
                    self._cancelled_waiters += 1
                if waiter.ticket is not None:
                    self._release_locked(waiter.ticket)
                    waiter.ticket = None
            raise
        finally:
            with self._lock:
                self._waiters.pop(waiter, None)
                elapsed_ms = (monotonic() - waiter.entered) * 1000
                self._wait_ms_total += elapsed_ms
                self._wait_ms_max = max(self._wait_ms_max, elapsed_ms)
                waiter.future.cancel()
                self._dispatch_locked()

    def snapshot(self) -> dict[str, int | float | None]:
        with self._lock:
            return {"active": self._active, "peak": self._peak, "rejected": self._rejected,
                    "users": len(self._users), "limit": self.maximum, "waiting": len(self._waiters),
                    "waiting_continuations": sum(w.continuation for w in self._waiters),
                    "continuation_rejected": self._continuation_rejected, "queued": self._queued,
                    "cancelled_waiters": self._cancelled_waiters,
                    "wait_ms_total": round(self._wait_ms_total, 3), "wait_ms_max": round(self._wait_ms_max, 3)}


class Ticket:
    def __init__(self, owner: Admission, user: str) -> None:
        self.owner, self.user, self.released = owner, user, False

    def release(self) -> None:
        with self.owner._lock:
            self.owner._release_locked(self)
            self.owner._dispatch_locked()


def _wake_waiter(waiter: _Waiter) -> None:
    if waiter.future.done():
        if waiter.ticket is not None:
            waiter.ticket.release()
    else:
        waiter.future.set_result(None)


def _check_cancelled() -> None:
    task = asyncio.current_task()
    # The shared timeout adapter supplies cancellation counting on 3.10.
    if task is not None and getattr(task, 'cancelling', lambda: 0)():
        raise asyncio.CancelledError


_models: ContextVar[Admission | None] = ContextVar("model_capacity", default=None)

MODEL_CAPACITY_CODE = "model_capacity_exceeded"
MODEL_CAPACITY_MESSAGE = "模型服务暂时繁忙，请稍后重试。"
TOOL_CAPACITY_MESSAGE = "查询服务暂时繁忙，请稍后重试。"


@dataclass
class CapacityOutcome:
    model_refused: bool = False
    tools: set[str] = field(default_factory=set)


_outcome: ContextVar[CapacityOutcome | None] = ContextVar("capacity_outcome", default=None)
_optional_model: ContextVar[bool] = ContextVar("optional_model_capacity", default=False)


def current_capacity_outcome() -> CapacityOutcome | None:
    return _outcome.get()


@contextmanager
def capacity_outcome_scope() -> Iterator[CapacityOutcome]:
    current = _outcome.get()
    if current is not None:
        yield current
        return
    current = CapacityOutcome()
    token = _outcome.set(current)
    try:
        yield current
    finally:
        _outcome.reset(token)


@contextmanager
def optional_model_scope() -> Iterator[None]:
    token = _optional_model.set(True)
    try:
        yield
    finally:
        _optional_model.reset(token)


@contextmanager
def model_capacity_scope(capacity: Admission | None) -> Iterator[None]:
    if capacity is None:
        yield
        return
    token = _models.set(capacity)
    try:
        yield
    finally:
        _models.reset(token)


@contextmanager
def model_slot() -> Iterator[None]:
    capacity = _models.get()
    try:
        ticket = capacity.acquire() if capacity is not None else None
    except CapacityExceeded:
        _model_refused()
        raise
    try:
        yield
    finally:
        if ticket is not None:
            ticket.release()


MODEL_SLOT_WAIT_S = 2.0


def _model_refused() -> None:
    outcome = current_capacity_outcome()
    if outcome is not None and not _optional_model.get():
        outcome.model_refused = True
        from app.core.model_budget import current_model_budget
        from app.core.turn_control import StopKind
        budget = current_model_budget()
        if budget is not None:
            budget.control.set(StopKind.CAPACITY, MODEL_CAPACITY_CODE)


@asynccontextmanager
async def async_model_slot(*, deadline: float | None = None) -> AsyncIterator[None]:
    capacity = _models.get()
    configured_wait = capacity.wait_timeout_s if capacity is not None else None
    remaining = None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
    wait_s = (remaining if remaining is not None else MODEL_SLOT_WAIT_S) if configured_wait is None else configured_wait
    if remaining is not None:
        wait_s = min(wait_s, remaining)
    if _optional_model.get():
        wait_s = 0.0
    from app.core.model_budget import current_model_budget
    budget = current_model_budget()
    continuation = not _optional_model.get() and budget is not None and budget.has_progress
    try:
        ticket = await capacity.acquire_wait(wait_s, continuation=continuation) if capacity is not None else None
    except CapacityExceeded:
        _model_refused()
        raise
    try:
        yield
    finally:
        if ticket is not None:
            ticket.release()
