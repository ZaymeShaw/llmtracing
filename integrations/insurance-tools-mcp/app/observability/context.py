"""日志上下文仅作用于当前任务；不写入 Workflow 静态依赖。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from app.observability.fields import CONTEXT_KEYS

_CONTEXT: ContextVar[dict[str, str | None]] = ContextVar("application_log_context", default={})


def current_context() -> dict[str, str | None]:
    return dict(_CONTEXT.get())


@contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    context = current_context()
    context.update({key: value for key, value in values.items() if key in CONTEXT_KEYS})
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def bind_context(**values: str | None) -> None:
    """在已由外层作用域托管的任务中补充稍后才获得的 ID。"""
    context = current_context()
    context.update({key: value for key, value in values.items() if key in CONTEXT_KEYS})
    _CONTEXT.set(context)
