"""保留原生超时和取消语义，3.10 使用 Python 3.12 实现的回移植。"""
import asyncio
import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout as timeout, timeout_at as timeout_at
else:
    from importlib.metadata import version
    from types import TracebackType
    from typing import Any
    from weakref import WeakKeyDictionary

    from taskgroup import Timeout

    # 独立 SDK 不要求安装 AnyIO；存在时只适配已经过取消语义回归的版本。
    try:
        from anyio._backends._asyncio import (
            CancelScope as _AnyioCancelScope,
            _task_states as _anyio_task_states,
        )
    except ModuleNotFoundError as error:
        if error.name != "anyio":
            raise
        _task_states: Any = None
    else:
        if version("anyio") != "4.15.1":
            raise RuntimeError("Python 3.10 timeout compatibility requires anyio==4.15.1")
        if not isinstance(_anyio_task_states, WeakKeyDictionary) or not {
            "_host_task", "_cancel_reason", "_parent_scope", "_active", "_shield",
        }.issubset(_AnyioCancelScope.__slots__):
            raise RuntimeError("Unsupported AnyIO cancellation scope structure")
        _task_states = _anyio_task_states

    class _CancellationTracker:
        def __init__(self, task: Any) -> None:
            self.task = task
            self.users = 0
            self.original_cancel = task.cancel
            inherited_cancel = type(task).cancel.__get__(task, type(task))
            self.instance_cancel = (
                _MISSING if self.original_cancel == inherited_cancel else self.original_cancel
            )
            self.pending: dict[Any, int] = {}
            task.cancel = self.cancel

        def flush(self) -> None:
            # AnyIO 退出时即清理自己的计数，即使业务在 scope 内吞掉了异常。
            # 可见的取消父 scope 仍存活时，计数由它接管，不能提前清理。
            for scope, count in list(self.pending.items()):
                if self._finished(scope):
                    for _ in range(count):
                        self.task.uncancel()
                    del self.pending[scope]

        @staticmethod
        def _finished(scope: Any) -> bool:
            if scope._active or scope._host_task is not None:
                return False
            while not scope.shield and scope._parent_scope is not None:
                scope = scope._parent_scope
                if scope.cancel_called and scope._active:
                    return False
            return True

        def cancel(self, *args: Any, **kwargs: Any) -> bool:
            self.flush()
            message = args[0] if args else kwargs.get("msg")
            state = _task_states.get(self.task) if _task_states is not None else None
            scope = state.cancel_scope if state is not None else None
            owner = None
            while scope is not None:
                if (
                    scope._host_task is self.task
                    and scope.cancel_called
                    and scope._cancel_reason == message
                ):
                    owner = scope
                    break
                scope = scope._parent_scope
            before = self.task.cancelling()
            result = self.original_cancel(*args, **kwargs)
            if owner is not None:
                self.pending[owner] = (
                    self.pending.get(owner, 0) + self.task.cancelling() - before
                )
            return result

        def release(self) -> None:
            self.users -= 1
            if not self.users:
                if self.instance_cancel is _MISSING:
                    del self.task.cancel
                else:
                    self.task.cancel = self.instance_cancel
                del _trackers[self.task]
                self.pending.clear()

    _MISSING = object()
    _trackers: dict[Any, _CancellationTracker] = {}

    class _Timeout:
        def __init__(self, when: float | None) -> None:
            self._timeout = Timeout(when)
            self._tracker: _CancellationTracker | None = None

        def when(self) -> float | None:
            return self._timeout.when()

        def expired(self) -> bool:
            return self._timeout.expired()

        def reschedule(self, when: float | None) -> None:
            self._timeout.reschedule(when)

        async def __aenter__(self) -> "_Timeout":
            tracker = _trackers.get(asyncio.current_task())
            if tracker is not None:
                tracker.flush()
            await self._timeout.__aenter__()
            try:
                if _task_states is not None:
                    task = asyncio.current_task()
                    tracker = _trackers.get(task)
                    if tracker is None:
                        tracker = _CancellationTracker(task)
                        _trackers[task] = tracker
                    tracker.users += 1
                    self._tracker = tracker
            except BaseException:
                # 进入适配失败也必须让 taskgroup 恢复原 Task，不能遗留迁移状态。
                await self._timeout.__aexit__(*sys.exc_info())
                raise
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ) -> bool | None:
            if self._tracker is not None:
                self._tracker.flush()
                self._tracker.release()
                self._tracker = None
            return await self._timeout.__aexit__(exc_type, exc_val, exc_tb)

    def timeout(delay: float | None) -> _Timeout:
        loop = asyncio.get_running_loop()
        return _Timeout(loop.time() + delay if delay is not None else None)

    def timeout_at(when: float | None) -> _Timeout:
        return _Timeout(when)

# 3.10 的 asyncio.TimeoutError 尚非内置异常别名，两者均保留既有超时归类。
TIMEOUT_ERRORS = (TimeoutError, asyncio.TimeoutError)
