"""H-04 failure delivery: request-local memory only, no model/tool/config/storage IO."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from copy import deepcopy
from functools import wraps
from typing import Any

from app.core.admission import MODEL_CAPACITY_CODE
from app.core.answer_ladder import LadderOutcome, guaranteed_answer
from app.core.answer_snapshot import AnswerSnapshot, current_snapshot


@dataclass
class AnswerDelivery:
    state: dict | None = None
    snapshot: AnswerSnapshot | None = None
    frozen: bool = False
    snapshot_attempted: bool = False

    def freeze(self, trace_id: str | None) -> None:
        if self.frozen:
            return
        self.answer(trace_id=trace_id, reason="turn_timeout")
        # Retain only immutable answer material and its binding; late worker state is isolated.
        route = (self.state or {}).get("route") or {}
        self.state = {"route": deepcopy({key: route.get(key) for key in (
            "trace_id", "identity_scope", "user_text", "answer_ladder_enabled", "capacity_answer_enabled",
            "l2_eligible", "snapshot_rejections", "run_id", "direct_answer",
        )})}
        self.frozen = True

    def answer(self, *, trace_id: str | None, reason: str | None = None, enabled: bool | None = None) -> LadderOutcome:
        route = self.state.get("route") if isinstance(self.state, dict) else None
        route = route if isinstance(route, dict) else {}
        active = route.get("answer_ladder_enabled", False) if enabled is None else enabled
        active = active or reason == 'budget_exhausted'
        # Capacity is independently gated; a ladder-only rollout preserves the old protocol.
        if (not active or (reason == MODEL_CAPACITY_CODE and not route.get("capacity_answer_enabled"))
                or not trace_id or route.get("trace_id") != trace_id):
            return guaranteed_answer(trace_id=trace_id, reason=reason)
        try:
            if self.snapshot is None and not self.snapshot_attempted:
                self.snapshot_attempted = True
                snapshot = current_snapshot(self.state or {}, trace_id)
                # Failure delivery can never advertise previously captured material as confirmed.
                if snapshot is not None:
                    self.snapshot = snapshot.model_copy(update={"confirmed": False})
            snapshot = self.snapshot
            if snapshot is not None and (
                snapshot.binding.trace_id == trace_id
                and snapshot.binding.identity_scope == route.get("identity_scope")
                and snapshot.binding.query == route.get("user_text")
            ):
                return guaranteed_answer(trace_id=trace_id, reason=reason, materials=snapshot.materials())
        except Exception:
            # Broken state/rendering loses only L2; cancellation (BaseException) propagates.
            pass
        return guaranteed_answer(trace_id=trace_id, reason=reason)


_DELIVERY: ContextVar[AnswerDelivery | None] = ContextVar("answer_delivery", default=None)


@contextmanager
def answer_delivery_scope(delivery: AnswerDelivery | None = None):
    current = _DELIVERY.get()
    if current is not None:
        yield current
        return
    value = delivery if delivery is not None else AnswerDelivery()
    token = _DELIVERY.set(value)
    try:
        yield value
    finally:
        _DELIVERY.reset(token)


def delivery_scoped(fn):
    @wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any):
        with answer_delivery_scope():
            return await fn(*args, **kwargs)
    return wrapped


def capture_delivery_state(state: dict) -> None:
    delivery = _DELIVERY.get()
    if delivery is not None and not delivery.frozen:
        delivery.state = state
        delivery.snapshot = None
        delivery.snapshot_attempted = False


def delivery_state(trace_id: str) -> dict | None:
    delivery = _DELIVERY.get()
    state = delivery.state if delivery is not None else None
    return state if state is not None and (state.get("route") or {}).get("trace_id") == trace_id else None


def failure_answer(*, state: dict | None = None, trace_id: str | None, reason: str | None = None,
                   enabled: bool | None = None) -> LadderOutcome:
    delivery = _DELIVERY.get()
    if delivery is None:
        delivery = AnswerDelivery(state=state)
    elif state is not None and delivery.state is None:
        delivery.state = state
    outcome = delivery.answer(trace_id=trace_id, reason=reason, enabled=enabled)
    # Deadline owners settle/audit a detached copy. Snapshot construction updates
    # the delivery slot; carry only its observations into the same bound turn.
    # Never substitute evidence, answer material or another request's observation.
    if state is not None and state is not delivery.state:
        source = (delivery.state or {}).get("route") or {}
        target = state.get("route") or {}
        binding = ("trace_id", "identity_scope", "user_text")
        if (isinstance(source, dict) and isinstance(target, dict) and trace_id
                and target.get("trace_id") == trace_id and all(target.get(k) == source.get(k) for k in binding)):
            if type(source.get("l2_eligible")) is bool:
                target["l2_eligible"] = source["l2_eligible"]
            if isinstance(source.get("snapshot_rejections"), list):
                target["snapshot_rejections"] = list(source["snapshot_rejections"])
    return outcome
