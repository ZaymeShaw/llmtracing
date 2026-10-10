"""函数级阶段包装，仅同步/协程使用；异步生成器在原任务内显式管理作用域。"""
from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable, TypeVar, cast
from app.observability.langfuse_runtime import span_scope

F = TypeVar("F", bound=Callable[..., Any])


def observed_call(name: str) -> Callable[[F], F]:
    def decorate(function: F) -> F:
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args: Any, **kwargs: Any) -> Any:
                with span_scope(name):
                    return await function(*args, **kwargs)
            return cast(F, asynchronous)
        @wraps(function)
        def synchronous(*args: Any, **kwargs: Any) -> Any:
            with span_scope(name):
                return function(*args, **kwargs)
        return cast(F, synchronous)
    return decorate
