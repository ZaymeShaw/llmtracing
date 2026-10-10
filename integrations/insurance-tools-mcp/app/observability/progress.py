"""非 Agent 执行链的过程事件通道（2026-09-19 全面 review 第二批：过程可见性收口）。

背景：SSE 的 activity 事件此前只覆盖 Agent 路径（Agno 原生 ToolCall 投影）——
Direct 直查与能力分支子 Workflow 的后端调用对渠道整段不可见（capability 分支
子流程非流式 arun，内部 Step/工具事件不上主事件总线），期间只有固定 status 与
心跳。本通道与模型增量预览（observe_model_generations）同款
contextvar 观察者模式直连 API 层：同请求同进程，天然可达。

分工纪律：
- **只补非 Agent 路径**：发射方以 budget `_profile != "agent"` 门控——Agent 路径
  已有原生 ToolCall 投影，重复发射只会造成同调用双 activity；
- **过程提示是尽力而为**：观察者异常在 emit 内吞掉（不得反噬 RPC 主链路），
  终态后的迟到事件由消费方按丢弃处理；
- **I/O 单次复用**：Direct/Workflow 只在已有 ProgressEvent 上携带请求与响应，
  不为调试展示增加业务调用或第二条事件链。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ProgressPhase = Literal["started", "completed"]


@dataclass(frozen=True)
class ProgressEvent:
    """一次非 Agent 后端调用/判定的过程事件。"""

    name: str
    phase: ProgressPhase
    # 关联标识：RPC 用 request_id（CALL-trace-nn），判定探测用 probe id。
    activity_id: str | None = None
    probe: str | None = None
    status: str | None = None  # completed 时：ok / 错误键 / hit / miss
    latency_ms: int | None = None
    input: Any = None
    output: Any = None


ProgressObserver = Callable[[ProgressEvent], Awaitable[None]]

_progress_observers: ContextVar[tuple[ProgressObserver, ...]] = ContextVar(
    "execution_progress_observers", default=()
)


@contextmanager
def observe_progress(observer: ProgressObserver) -> Iterator[None]:
    """请求内订阅过程事件；退出恢复上层上下文（可叠加，最新先收）。"""
    token = _progress_observers.set((observer, *_progress_observers.get()))
    try:
        yield
    finally:
        _progress_observers.reset(token)


async def emit_progress(event: ProgressEvent) -> None:
    """广播一个过程事件；无订阅者零开销，观察者异常不反噬调用方。"""
    for observer in _progress_observers.get():
        try:
            await observer(event)
        except Exception:
            logger.exception("progress_observer_failed")
