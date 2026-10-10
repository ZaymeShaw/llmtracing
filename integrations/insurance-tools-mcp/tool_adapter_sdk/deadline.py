"""Handler deadlines supplied by the host in the current event loop's clock."""
from __future__ import annotations

import asyncio
import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("handler_deadline", default=None)


@contextmanager
def handler_deadline_scope(deadline: float | None) -> Iterator[None]:
    """Inherit a host deadline; nested scopes may shorten but never extend it."""
    if deadline is not None and (isinstance(deadline, bool) or not math.isfinite(deadline)):
        raise ValueError("handler deadline must be finite")
    parent = _deadline.get()
    effective = parent if deadline is None else deadline if parent is None else min(parent, deadline)
    token = _deadline.set(effective)
    try:
        yield
    finally:
        _deadline.reset(token)


def handler_deadline(timeout_s: float) -> float:
    """Clamp a handler's configured timeout to the host's absolute deadline."""
    local = asyncio.get_running_loop().time() + timeout_s
    parent = _deadline.get()
    return local if parent is None else min(local, parent)
