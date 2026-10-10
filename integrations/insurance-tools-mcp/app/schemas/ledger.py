"""Task Ledger 契约(方案 v13 §7.1 权威 schema · M0 冻结)。

一个前台 + 多个挂起(@pause)/后台(background)流程;台账变更唯一写入点 = core/ledger.py
(不变量断言);generation 版本栅栏(§7.3):任务的每次取代/重启/取消使 generation + 1,
旧 generation 的晚到结果不覆盖、不进上下文、只进审计;expected_event 消费即失效。
"""
from __future__ import annotations

from datetime import datetime
from app.compat import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskRole(StrEnum):
    """流程角色(§7.1)。"""

    FOREGROUND = "FOREGROUND"
    SUSPENDED = "SUSPENDED"  # @pause 挂起,待 G-2 匹配 expected_event 恢复(POC-19)
    BACKGROUND = "BACKGROUND"  # background run(异步深查道等,POC-8)


class Lifecycle(StrEnum):
    """Agno run 状态的镜像摘要(§7.1:非权威,权威状态在 Workflow Session)。"""

    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"  # H-03：可恢复失败，证据仅作未确认候选
    FAILED = "FAILED"  # 取消一致性:被取消任务标记 FAILED(POC-17)


def _compatible_entry(value):
    """Unknown persisted states are failed, never resumed; preserve valid known states."""
    if not isinstance(value, dict):
        return value
    lifecycle = value.get("lifecycle")
    if not isinstance(lifecycle, str) or not lifecycle.strip() or lifecycle in Lifecycle._value2member_map_:
        return value
    result = dict(value)
    result.update(lifecycle="FAILED", expected_event=None)
    if result.get("role") == "FOREGROUND":
        result["role"] = "SUSPENDED"
    generation = result.get("generation", 1)
    if type(generation) is not int or generation < 1:
        raise ValueError("Unknown lifecycle requires a positive integer generation")
    result["generation"] = generation + 1
    return result


def _compatible_ledger(value):
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return value
    tasks = [_compatible_entry(task) for task in value["tasks"]]
    changed = [task for old, task in zip(value["tasks"], tasks) if old is not task]
    if not changed:
        return value
    result = {**value, "tasks": tasks}
    if isinstance(result.get("focus_task_id"), str) and result["focus_task_id"] in {
        task["task_id"] for task in changed if isinstance(task.get("task_id"), str)
    }:
        result["focus_task_id"] = None
    sessions = {task["workflow_session_id"] for task in changed if isinstance(task.get("workflow_session_id"), str)}
    if isinstance(result.get("wf_session"), dict):
        result["wf_session"] = {key: sid for key, sid in result["wf_session"].items() if not isinstance(sid, str) or sid not in sessions}
    return result


class ExpectedEvent(BaseModel):
    """挂起任务的预期用户事件(G-2 纯查表匹配键;多选扩展位预留)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str  # 如 POLICY_SELECTED
    options: list[str] = []  # 候选(澄清卡 ≤5 项,§4.8③)
    max_select: int = 1


class LedgerEntry(BaseModel):
    """台账条目(§7.1 schema 原文)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _read_compatible_state(cls, value):
        return _compatible_entry(value)

    task_id: str  # 如 T-002
    capability: str  # 流程/能力名
    role: TaskRole
    lifecycle: Lifecycle
    generation: int = 1  # 版本栅栏:取代/重启/取消时 +1(core/ledger.py 维护)
    workflow_session_id: str | None = None  # 权威状态在 Agno Workflow Session
    run_id: str | None = None  # 后台任务可查(POC-8 get_run/cancel_run)
    expected_event: ExpectedEvent | None = None  # SUSPENDED 时 G-2 匹配键
    summary: str | None = None  # 答尾待办提示 / 任务摘要
    created_at: datetime


class TaskLedger(BaseModel):
    """会话任务台账(session_state["ledger"];唯一写入点 core/ledger.py,函数式变更)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _read_compatible_states(cls, value):
        return _compatible_ledger(value)

    focus_task_id: str | None = None  # 唯一前台(I1:至多一个)
    tasks: list[LedgerEntry] = []
    wf_session: dict[str, str] = {}  # 流程名 → workflow session_id(§5.4 薄包装快捷索引,条目字段的镜像)
    # 任务完成后条目会移除，不能再从存活 tasks 推导下一个 ID；持久化单调游标
    # 避免旧后台结果与后来复用的 task_id/generation 碰撞。
    next_task_seq: int = Field(default=1, ge=1)
