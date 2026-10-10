"""按工具调用作用域观察 RPC；并行兄弟调用不共享观测标记。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class BackendCallObservation:
    seen: bool = False
    returned: bool = False


_OBSERVERS: ContextVar[tuple[BackendCallObservation, ...]] = ContextVar(
    "backend_call_observers", default=(),
)


@contextmanager
def observe_backend_calls() -> Iterator[BackendCallObservation]:
    # 可变标记让被等待的子任务能够通知父作用域；嵌套 hook 同时通知祖先，
    # 防止一条 RPC 在外层再次记成本地调用。不进入可持久化的 budget/state。
    observation = BackendCallObservation()
    token = _OBSERVERS.set((observation, *_OBSERVERS.get()))
    try:
        yield observation
    finally:
        _OBSERVERS.reset(token)


def mark_backend_call() -> None:
    """只在已准入、由 finally 保证 RPC 结束日志的边界调用。"""
    for observation in _OBSERVERS.get():
        observation.seen = True


def mark_backend_returned() -> None:
    for observation in _OBSERVERS.get():
        observation.returned = True
