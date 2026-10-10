"""Turn-bounded health signal for automatic routing; external hard flags stay intact."""
from __future__ import annotations

from app.core.admission import MODEL_CAPACITY_CODE

_HEALTH_KEYS = ("degraded", "session_degraded", "fatal_error")
_TRANSIENT = frozenset({
    "model_unavailable", MODEL_CAPACITY_CODE, "turn_timeout", "context_overflow",
    "budget_exhausted", "budget_finish", "no_progress", "cancelled",
})


def _sequence(route: dict) -> int:
    value = (route.get("health") or {}).get("turn_seq") if isinstance(route.get("health"), dict) else None
    return value if type(value) is int and value >= 0 else 0


def session_turn_seq(session_state: dict) -> int:
    """Shared session turn clock, advanced only by assemble (also used by H-15)."""
    route = session_state.get("route")
    return _sequence(route if isinstance(route, dict) else {})


def _failure(route: dict) -> dict | None:
    health = route.get("health")
    if not isinstance(health, dict):
        return None
    failed, expires = health.get("failed_turn_seq"), health.get("expires_after_turn")
    if (type(failed) is int and type(expires) is int and 0 <= failed <= _sequence(route)
            and failed <= expires <= failed + 1 and isinstance(health.get("reason"), str)):
        return health
    return None


def session_is_healthy(session_state: dict) -> bool:
    """Managed failures expire; externally supplied session/fatal flags remain guards."""
    route = session_state.get("route")
    route = route if isinstance(route, dict) else {}
    if any(session_state.get(key) for key in _HEALTH_KEYS) or route.get("degraded") or route.get("fatal_error"):
        return False
    if not route.get("session_degraded"):
        return True
    failure = _failure(route)
    return failure is not None and failure["expires_after_turn"] < _sequence(route)


def reset_route_preserving_health(session_state: dict) -> None:
    """Advance once at assemble; carrying a failure never refreshes its expiry."""
    previous = session_state.get("route")
    previous = previous if isinstance(previous, dict) else {}
    seq = _sequence(previous)
    failure = _failure(previous)
    legacy_session_degraded = session_state.pop("session_degraded", False)
    if failure is None and (legacy_session_degraded or any(previous.get(key) for key in _HEALTH_KEYS)):
        # Legacy/corrupt booleans get one conservative subsequent turn, not immortality.
        failure = {"reason": "unknown", "failed_turn_seq": seq, "expires_after_turn": seq + 1}
    health = {"turn_seq": seq + 1}
    route: dict = {"health": health}
    if failure is not None and failure["expires_after_turn"] >= seq + 1:
        health.update({key: failure[key] for key in ("reason", "failed_turn_seq", "expires_after_turn")})
        route["session_degraded"] = True
    session_state["route"] = route


def _reason(route: dict, reason: str | None) -> str:
    if "exit_reason" in route:
        return route["exit_reason"] or reason or "unknown"
    # Standalone/legacy callers use the same priority table without publishing a
    # decision; production channel settlement always supplies exit_reason.
    from app.core.exit_decision import exit_for_state
    reasons = tuple(value for value in (
        reason, "citation_error" if route.get("citation_anomaly") and not route.get("citation_observation_only") else None,
        "turn_timeout" if route.get("timeout_snapshot_expired") else None,
    ) if value)
    return exit_for_state({"route": route}, reasons=reasons, incomplete=True).health_reason or "unknown"


def record_session_health(session_state: dict, *, degraded: bool, reason: str | None = None) -> dict:
    """Record cause and origin, keeping the legacy boolean for existing consumers."""
    route = session_state.get("route")
    if not isinstance(route, dict):
        route = {}
        session_state["route"] = route
    seq = _sequence(route)
    if reason == 'storage_unavailable' or route.get('exit_reason') == 'storage_unavailable':
        degraded = False  # A storage fault does not make tool selection or context unsafe.
    if degraded:
        cause = _reason(route, reason)
        previous = _failure(route)
        # Duplicate observers cannot erase a known cause, or weaken a structural
        # failure when later cleanup also times out. Unknown causes can be refined.
        if "exit_reason" not in route and previous is not None and previous["failed_turn_seq"] == seq:
            prior = previous["reason"]
            if cause == "unknown" or (prior != "unknown" and prior not in _TRANSIENT and cause in _TRANSIENT):
                cause = prior
        route["health"] = {"turn_seq": seq, "reason": cause, "failed_turn_seq": seq,
                           "expires_after_turn": seq + int(cause not in _TRANSIENT)}
        route["session_degraded"] = True
    else:
        for key in _HEALTH_KEYS:
            route.pop(key, None)
        route["health"] = {"turn_seq": seq}
    return route
