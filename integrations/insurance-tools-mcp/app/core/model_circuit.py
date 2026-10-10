"""Bounded local circuits; recovery uses one ordinary request, never extra IO."""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)
CircuitKey = tuple[str, str, str, str]


class CircuitPolicy(BaseModel):
    """Request-snapshot policy; caller classifies transient provider failures."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    failure_threshold: int = Field(default=3, ge=1, le=100, strict=True)
    cooldown_s: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # Retained for config compatibility; R6 counts transient HTTP errors at every duration.
    slow_error_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)


@dataclass
class _State:
    failures: int = 0
    open_until: float = 0.0
    generation: int = 0
    probe: object | None = None


@dataclass(frozen=True)
class CircuitPermit:
    """Attempt identity: a late closed-state success cannot close a newer circuit."""

    state: _State | None
    generation: int = 0
    probe: object | None = None
    policy: CircuitPolicy = field(default_factory=CircuitPolicy)


class ModelCircuit:
    """Continuous transient failures open a dependency, independently of frequency.

    ``acquire`` atomically reserves the sole half-open probe. Callers must release
    its permit on every exit, including cancellation and local admission failure.
    Only actual provider success closes the circuit; HTTP errors are neutral to
    the closed-state streak and renew an already open circuit's cooldown.
    """

    def __init__(self, *, event_prefix: str = 'model') -> None:
        self._event_prefix = event_prefix
        self._states: OrderedDict[CircuitKey, _State] = OrderedDict()
        self._lock = Lock()
        self._skipped = 0

    def _event(self, event: str, key: CircuitKey, **fields) -> None:
        # No endpoint or credentials in circuit events.
        event = event.replace('model_', self._event_prefix + '_', 1)
        dimensions = {"handler": key[1]} if self._event_prefix == 'handler' else {"tier": key[0], "model": key[2]}
        logger.info(event, extra={"observability": {**dimensions, **fields}})

    def acquire(self, key: CircuitKey, *, policy: CircuitPolicy | None = None) -> CircuitPermit | None:
        policy = policy or CircuitPolicy()
        now = time.monotonic()
        with self._lock:
            state = self._states.get(key)
            if state is None or not state.open_until:
                return CircuitPermit(state, state.generation if state else 0, policy=policy)
            if state.open_until > now or state.probe is not None:
                self._skipped += 1
                return None
            state.probe = object()
            self._states.move_to_end(key)
            self._event("model_circuit_half_open", key)
            return CircuitPermit(state, state.generation, state.probe, policy)

    def release(self, key: CircuitKey, permit: CircuitPermit) -> None:
        with self._lock:
            state = self._states.get(key)
            if (state is permit.state and state is not None and permit.probe is not None
                    and state.probe is permit.probe):
                # No provider outcome (cancel, budget, capacity, overflow). A new
                # ordinary request may probe; this does not mark recovery.
                state.probe = None
                state.generation += 1
                self._event("model_circuit_probe_released", key)

    def observe(self, key: CircuitKey, *, failed: bool | None,
                permit: CircuitPermit | None = None, policy: CircuitPolicy | None = None) -> None:
        """True=transient failure, False=success, None=other provider error.

        ``permit=None`` supports explicit outcome injection; runtime always passes
        the attempt permit. Pending probes are never evicted by history trimming.
        """
        policy = policy or (permit.policy if permit is not None else CircuitPolicy())
        now = time.monotonic()
        with self._lock:
            state = self._states.get(key)
            if failed is False:
                if state is None:
                    return
                probe_success = (permit is not None and permit.probe is not None
                                 and state is permit.state and state.probe is permit.probe)
                if state.open_until and permit is not None and not probe_success and (
                        state is not permit.state or state.generation != permit.generation):
                    return  # Success from an attempt started before this opening.
                self._states.pop(key)
                if state.open_until:
                    self._event("model_circuit_close", key)
                return
            if failed is None and (state is None or not state.open_until):
                return
            if state is None:
                state = self._states[key] = _State()
            if failed is True:
                state.failures = min(100, state.failures + 1)
            if state.open_until and permit is not None and (
                    state is not permit.state or state.generation != permit.generation):
                # Late outcomes count for observation but cannot extend the new
                # generation's cooldown or invalidate its active recovery probe.
                return
            if state.open_until or state.failures >= policy.failure_threshold:
                renewed = bool(state.open_until)
                state.open_until = now + policy.cooldown_s
                if permit is None or state.probe is permit.probe:
                    state.probe = None
                state.generation += 1
                self._event("model_circuit_open", key, renewed=renewed,
                            failures=state.failures, cooldown_s=policy.cooldown_s)
            self._states.move_to_end(key)
            if len(self._states) > 128:
                evict = next(k for k, value in self._states.items() if value.probe is None)
                self._states.pop(evict)

    def is_open(self, key: CircuitKey) -> bool:
        """Read availability without resetting history or claiming a probe."""
        now = time.monotonic()
        with self._lock:
            state = self._states.get(key)
            return bool(state and (state.open_until > now or state.probe is not None))

    def snapshot(self) -> dict[str, int]:
        now = time.monotonic()
        with self._lock:
            return {"open": sum(s.open_until > now for s in self._states.values()),
                    "half_open": sum(s.probe is not None for s in self._states.values()),
                    "skipped": self._skipped}


class ProviderHealth:
    """Bounded provider outcome counters for operations; never a readiness gate."""

    def __init__(self) -> None:
        self._outcomes: OrderedDict[CircuitKey, dict[str, int | str | None]] = OrderedDict()
        self._lock = Lock()

    def observe(self, key: CircuitKey, outcome: str, status: int | None) -> None:
        with self._lock:
            row = self._outcomes.setdefault(key, {"success": 0, "connectivity_failure": 0, "http_error": 0})
            row[outcome] = int(row[outcome] or 0) + 1
            row["last_outcome"], row["last_status"] = outcome, status
            self._outcomes.move_to_end(key)
            if len(self._outcomes) > 128:
                self._outcomes.popitem(last=False)

    def snapshot(self) -> list[dict[str, int | str | None]]:
        with self._lock:
            return [{"tier": key[0], "model": key[2], **row} for key, row in self._outcomes.items()]


circuits = ModelCircuit()
provider_health = ProviderHealth()
