"""Bounded, request-owned execution and retirement; no framework or network IO.

A revoked turn cannot start new calls/writes. Existing calls retain their slots;
its session remains reserved until tracked work and cleanup actually finish.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager, asynccontextmanager, aclosing
from contextvars import ContextVar, copy_context
from functools import wraps
from dataclasses import dataclass
from typing import Any
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from threading import Lock
from tool_adapter_sdk.compat import TIMEOUT_ERRORS
from app.core.budget import current_shutdown_deadline

logger = logging.getLogger(__name__)
_current: ContextVar[TurnExecution | None] = ContextVar('turn_execution', default=None)
_cleanup: ContextVar[bool] = ContextVar('turn_cleanup_allowed', default=False)
_turns: dict[tuple, TurnExecution] = {}
_jobs: set[TurnExecution] = set()


class TurnBusy(RuntimeError):
    def __init__(self, *, session_conflict: bool = True, reason: str | None = None):
        self.session_conflict = session_conflict
        self.reason = reason or ("session_retiring" if session_conflict else "execution_capacity")
        logger.warning("turn_busy", extra={"observability": {"reason": self.reason}})
        super().__init__('previous execution is still retiring' if session_conflict else 'execution capacity is full')


class TurnExpired(TimeoutError):
    pass


class TurnStopping(TurnExpired):
    """An app drain shortened execution; existing delivery/retirement still applies."""


def current_execution() -> TurnExecution | None:
    return _current.get()


@contextmanager
def execution_scope(turn: TurnExecution, *, cleanup: bool = False):
    token, cleanup_token = _current.set(turn), _cleanup.set(cleanup)
    try:
        yield
    finally:
        _cleanup.reset(cleanup_token)
        _current.reset(token)


@contextmanager
def execution_activity():
    turn = _current.get()
    if turn is None:
        yield
        return
    with turn.lock:
        if turn.lease_lost or (turn.revoked and not _cleanup.get()):
            raise TurnExpired('turn execution has ended')
        turn.activities += 1
    try:
        yield
    finally:
        with turn.lock:
            turn.activities -= 1


def tracked_call(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        with execution_activity():
            return await fn(*args, **kwargs)
    return wrapped


async def turn_to_thread(fn, *args, **kwargs):
    turn = _current.get()
    if turn is None:
        return await asyncio.to_thread(fn, *args, **kwargs)
    # Shield only the real thread lifetime, never the caller's execution deadline.
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    turn.track(task)
    return await asyncio.shield(task)


class TurnExecution:
    def __init__(self, namespace: object, session_id: str, maximum: int):
        loop = asyncio.get_running_loop()
        self.key = (loop, namespace, session_id)
        if self.key in _turns:
            previous = _turns[self.key]
            raise TurnBusy(reason='session_retiring' if previous.revoked or previous.retirement else 'session_running')
        active = [turn for turn in _jobs if turn.key[0] is loop]
        if len(active) >= maximum:
            raise TurnBusy(session_conflict=False, reason='retirement_capacity'
                           if any(turn.revoked or turn.retirement for turn in active) else 'execution_capacity')
        self.lock = Lock()
        self.cleanup_closed = False
        self.release_closed = False
        self.retirement_warn_s = 30.0
        self.retirement_slow = False
        self.revoked: bool = False
        # Revoked execution may still produce a local fallback until its owner closes delivery.
        self.delivery_closed = False
        self.delivery_failed = False
        self.lease_lost = False
        self.renew_lease = None
        self.activities = 0
        self.pending: set[asyncio.Task] = set()
        self.worker: asyncio.Task | None = None
        self.retirement: asyncio.Task | None = None
        self.cleanup_callbacks: list = []
        self.child_cleanup_roots: set[tuple[str, str | None]] = set()
        self.terminal_state_updates: dict | None = None
        self.terminal_storage_failed = False
        self.release_callbacks: list = []
        self.audits: dict[str, tuple] = {}
        self.audits_closed = False
        self.finished = False
        self.context = copy_context()
        self.shutdown = current_shutdown_deadline()
        _turns[self.key] = self
        _jobs.add(self)

    def track(self, task: asyncio.Task) -> None:
        self.pending.add(task)
        def done(completed):
            self.pending.discard(completed)
            if not completed.cancelled():
                completed.exception()  # consume detached failures
        task.add_done_callback(done)

    def start(self, operation) -> asyncio.Task:
        with execution_scope(self):
            self.context = copy_context()
            self.worker = asyncio.create_task(operation)
        self.track(self.worker)
        return self.worker

    async def wait(self, deadline: float | None):
        assert self.worker is not None
        done = await self._wait({self.worker}, deadline)
        # Completion wins even if the event loop resumes at/after the deadline.
        # result() also preserves the worker's own exception/cancellation.
        if not done and not self.worker.done():
            raise self._expired(deadline)
        return self.worker.result()

    def _expired(self, original: float | None) -> TurnExpired:
        if (self.shutdown is not None and self.shutdown.answer_at is not None
                and (original is None or self.shutdown.answer_at < original)):
            return TurnStopping('service drain execution deadline reached')
        return TurnExpired('answer execution deadline reached')

    async def _wait(self, tasks: set[asyncio.Task], deadline: float | None) -> set[asyncio.Task]:
        """Wake an already armed wait when this application's drain starts."""
        notice = (asyncio.create_task(self.shutdown.started.wait())
                  if self.shutdown is not None and not self.shutdown.started.is_set() else None)
        try:
            while True:
                effective = deadline if self.shutdown is None else self.shutdown.answer_cutoff(deadline)
                done, _ = await asyncio.wait(
                    tasks | ({notice} if notice is not None else set()), return_when=asyncio.FIRST_COMPLETED,
                    timeout=None if effective is None else max(0.0, effective - asyncio.get_running_loop().time()),
                )
                if notice is not None and notice in done:
                    done.discard(notice)
                    notice = None
                    if not done:
                        continue
                return done
        finally:
            if notice is not None:
                notice.cancel()
                await asyncio.gather(notice, return_exceptions=True)

    async def next_event(self, queue: asyncio.Queue, deadline: float | None):
        """Drain a completed streaming worker's queued events even at its deadline.

        The producer must enqueue its terminal sentinel before returning. Exceptions
        and cancellation still propagate; an unfinished worker never gains more time.
        """
        assert self.worker is not None
        if self.worker.done():
            self.worker.result()
            return queue.get_nowait()
        reader = asyncio.create_task(queue.get())
        try:
            done = await self._wait({reader, self.worker}, deadline)
            if reader in done:
                return reader.result()
            if self.worker.done():
                self.worker.result()
                return queue.get_nowait()
            raise self._expired(deadline)
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    def revoke(self, *, cancel_worker: bool = True) -> None:
        with self.lock:
            if self.revoked:
                return
            self.revoked = True
        if (cancel_worker and self.worker is not None and not self.worker.done()
                and self.worker is not asyncio.current_task()):
            self.worker.cancel()

    def capture_audit(self, sink, record, on_failure) -> bool:
        if not self.audits_closed:
            self.audits[record.turn_id] = (sink, record, on_failure)
        return False  # queued, not yet durably written

    def freeze_audits(self) -> None:
        self.audits_closed = True
        self.audits.clear()

    def defer(self, callback, *, release: bool = False) -> bool:
        if self.finished or (self.release_closed if release else self.cleanup_closed):
            return False
        (self.release_callbacks if release else self.cleanup_callbacks).append(callback)
        return True

    def seal(self) -> None:
        if self.retirement is None:
            # The registry owns this bounded task; one retirement per admitted turn, no retries.
            self.retirement = self.context.copy().run(asyncio.create_task, self._retire())

    async def _idle(self):
        while self.pending or self.activities:
            if self.pending:
                await asyncio.wait(tuple(self.pending), timeout=.02)
            else:
                await asyncio.sleep(.01)

    async def _retire(self):
        from app.core.audit import audit_dispatch_scope, write_turn_audit
        async def maintain_lease():
            while not self.finished:
                await asyncio.sleep(.5)
                try:
                    renewed = self.renew_lease is None or await self.renew_lease()
                except Exception:
                    renewed = False
                if not renewed:
                    self.lease_lost = True
                    self.revoke()
                    return
        loop = asyncio.get_running_loop()
        started = loop.time()
        def slow_retirement():
            self.retirement_slow = True
            logger.warning('turn_retirement_slow', extra={'observability': {
                'elapsed_ms': int((loop.time() - started) * 1000),
                'pending': len(self.pending), 'activities': self.activities}})
        warning = loop.call_later(self.retirement_warn_s, slow_retirement)
        renewal = asyncio.create_task(maintain_lease()) if self.renew_lease is not None else None
        try:
            await self._idle()
            with execution_scope(self, cleanup=True), audit_dispatch_scope(None):
                while self.cleanup_callbacks:
                    callback = self.cleanup_callbacks.pop(0)
                    try:
                        if not self.lease_lost and self.renew_lease is not None:
                            try:
                                valid = await self.renew_lease()
                            except Exception:
                                valid = False
                            if not valid:
                                self.lease_lost = True
                                self.revoke()
                        if not self.lease_lost:
                            await callback()
                    except Exception:
                        logger.error('turn_retirement_failed', exc_info=True)
                    await self._idle()
                self.cleanup_closed = True
                while self.release_callbacks:
                    callback = self.release_callbacks.pop(0)
                    try:
                        await callback()
                    except Exception:
                        logger.warning('turn_release_failed', exc_info=True)
                self.release_closed = True
                self.finished = True
                _turns.pop(self.key, None)
                # Audit has a bounded job slot, but never holds the session lease.
                elapsed = int((loop.time() - started) * 1000)
                logger.info('turn_retired', extra={'observability': {
                    'elapsed_ms': elapsed, 'outcome': 'lease_lost' if self.lease_lost else 'completed',
                    'retirement_slow': self.retirement_slow}})
                self.audits_closed = True
                for sink, record, on_failure in self.audits.values():
                    record = record.model_copy(update={'retirement_ms': elapsed, 'retirement_slow': self.retirement_slow})
                    await turn_to_thread(write_turn_audit, sink, record, on_failure=on_failure)
        finally:
            warning.cancel()
            self.cleanup_closed = self.release_closed = True
            if renewal is not None:
                renewal.cancel()
                # Retain the job slot if an injected renewer ignores cancellation.
                await asyncio.gather(renewal, return_exceptions=True)
            if _turns.get(self.key) is self:
                _turns.pop(self.key, None)
            self.finished = True
            _jobs.discard(self)


async def drain_turn_executions(timeout_s: float) -> None:
    loop = asyncio.get_running_loop()
    until = loop.time() + timeout_s
    # Re-snapshot after each completion: cancellation can seal another retirement
    # while this drain is waiting. A one-shot task list can miss that cleanup.
    while loop.time() < until:
        jobs = [turn for turn in list(_jobs) if turn.key[0] is loop and (turn.retirement or turn.revoked)]
        if not jobs:
            return
        tasks = [turn.retirement for turn in jobs if turn.retirement and not turn.retirement.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=until - loop.time())
        else:
            await asyncio.sleep(min(.01, until - loop.time()))


@asynccontextmanager
async def fenced_closing(stream):
    """An interrupted consumer fences background framework writes before aclose."""
    async with aclosing(stream):
        try:
            yield stream
        except BaseException:
            turn = current_execution()
            if turn is not None:
                turn.revoke(cancel_worker=False)
            raise


@dataclass
class BoundedDelivery:
    """Channel fallback after supervision expires; no public response fields."""

    result: Any = None


@asynccontextmanager
async def run_bounded(
    turn: TurnExecution,
    execute: Callable[[], Coroutine[Any, Any, Any]],
    on_expired: Callable[[BaseException], Awaitable[Any]],
    *, seal: bool = True, handoff: bool = False,
) -> AsyncIterator[BoundedDelivery]:
    """Own one worker stage and fence before channel-specific expiry delivery.

    Callers consume wait/next_event with the admission deadline. Cancellation and
    generator close always propagate after cleanup registration. Preparation may
    hand off a successful worker stage; every failed stage revokes and retires.
    A channel holding an external lease owns seal() when seal=False.
    """
    delivery = BoundedDelivery()
    completed = False
    try:
        turn.start(execute())
        try:
            yield delivery
            completed = True
        except (TIMEOUT_ERRORS + (asyncio.CancelledError, GeneratorExit)) as exc:
            turn.delivery_closed = True
            turn.revoke()
            try:
                delivery.result = await on_expired(exc)
            finally:
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                    raise exc
    finally:
        if not (handoff and completed):
            turn.delivery_closed = True
            turn.revoke()
            if seal:
                turn.seal()
