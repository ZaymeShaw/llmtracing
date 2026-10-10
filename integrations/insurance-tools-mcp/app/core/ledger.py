"""Task Ledger 唯一写入点(方案 v13 §7.1/§7.3 · 不变量断言 · 纯函数式变更)。

不变量只保两条(v13 极简):
  I1 前台唯一——至多一个 FOREGROUND,且 focus_task_id 与之一致;
  I2 generation 单调——条目存续期间 generation 只增不减。
一切台账变更必须经本模块函数(返回新 TaskLedger,调用方赋回 session_state);
「Agent/工具直接改 session_state["ledger"]」为架构违规(D16 同款纪律)。
G-2 查表(find_by_event)为纯读:0 LLM、0 后端(Gate 铁律 T2 无例外)。
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections import Counter
import logging

from pydantic import ValidationError

from app.schemas.ledger import ExpectedEvent, LedgerEntry, Lifecycle, TaskLedger, TaskRole


class LedgerInvariantError(ValueError):
    """台账不变量被破坏(I1/I2)——fail 显式,不静默修复。"""


def assert_invariants(ledger: TaskLedger) -> None:
    foreground = [t for t in ledger.tasks if t.role is TaskRole.FOREGROUND]
    if len(foreground) > 1:
        raise LedgerInvariantError(f"I1 违例:存在多个前台任务 {[t.task_id for t in foreground]}")
    # I1 是双向一致，不只是“有 focus 时能找到前台”：存在 FOREGROUND 就必须是
    # 唯一 focus；没有 FOREGROUND 时 focus 也必须为空。
    expected_focus = foreground[0].task_id if foreground else None
    if ledger.focus_task_id != expected_focus:
        raise LedgerInvariantError(
            f"I1 违例:focus_task_id={ledger.focus_task_id},唯一前台应为 {expected_focus}"
        )
    # wf_session 为条目字段的镜像:索引里的 session 必须能在条目中找到出处
    entry_sessions = {t.workflow_session_id for t in ledger.tasks if t.workflow_session_id}
    stale = {k: v for k, v in ledger.wf_session.items() if v not in entry_sessions}
    if stale:
        raise LedgerInvariantError(f"wf_session 索引悬空(无对应条目):{stale}")


def _find(ledger: TaskLedger, task_id: str) -> LedgerEntry | None:
    return next((t for t in ledger.tasks if t.task_id == task_id), None)


def _replace(ledger: TaskLedger, entry: LedgerEntry) -> TaskLedger:
    """以新条目替换同 task_id 条目(中间步骤:只守 I2;I1 由公开操作的出口断言统一执行)。"""
    if (_old := _find(ledger, entry.task_id)) is not None and entry.generation < _old.generation:
        raise LedgerInvariantError(f"I2 违例:{entry.task_id} generation 回退 {_old.generation} → {entry.generation}")
    tasks = [entry if t.task_id == entry.task_id else t for t in ledger.tasks]
    return ledger.model_copy(update={"tasks": tasks})


def _next_task_number(ledger: TaskLedger) -> int:
    """返回不会与存量条目碰撞的单调序号；兼容无游标的旧会话载荷。"""
    nums = [int(t.task_id.removeprefix("T-")) for t in ledger.tasks if t.task_id.startswith("T-")]
    return max(ledger.next_task_seq, max(nums, default=0) + 1)


# Terminal summaries are diagnostic history, never execution authority.
TERMINAL_TASK_LIMIT = 32


def release_child_sessions(ledger: TaskLedger, session_ids: set[str]) -> TaskLedger:
    """Drop retired child references only after owner-scoped storage cleanup.

    Active tasks and pending events keep their authority, even if an ambiguous
    historical entry points to the same child.
    """
    active = {t.workflow_session_id for t in ledger.tasks
              if t.lifecycle not in {Lifecycle.COMPLETED, Lifecycle.FAILED} or t.expected_event is not None}
    retired = session_ids - active
    tasks = [t.model_copy(update={"workflow_session_id": None, "run_id": None})
             if t.workflow_session_id in retired else t for t in ledger.tasks]
    result = ledger.model_copy(update={"tasks": tasks,
        "wf_session": {key: sid for key, sid in ledger.wf_session.items() if sid not in retired}})
    assert_invariants(result)
    return result


def prune_terminal(ledger: TaskLedger, *, keep: int = TERMINAL_TASK_LIMIT) -> TaskLedger:
    """Retain recent terminal entries, all active tasks, and monotonic IDs."""
    if keep < 0:
        raise ValueError("keep must be nonnegative")
    terminal = {Lifecycle.COMPLETED, Lifecycle.FAILED, Lifecycle.INTERRUPTED}
    removable = [entry for entry in ledger.tasks if entry.lifecycle in terminal
                 and entry.task_id != ledger.focus_task_id and entry.expected_event is None]
    if len(removable) <= keep:
        return ledger
    # Creation timestamp + insertion order provides deterministic ties.
    def order(item):
        position, entry = item
        # Legacy naive dates are UTC, never interpreted in the worker's local TZ.
        created = entry.created_at
        return ((created if created.tzinfo else created.replace(tzinfo=timezone.utc)).timestamp(), position)
    ordered = sorted(enumerate(removable), key=order)
    removed = {entry.task_id for _, entry in ordered[:len(ordered) - keep]}
    tasks = [entry for entry in ledger.tasks if entry.task_id not in removed]
    sessions = {entry.workflow_session_id for entry in tasks if entry.workflow_session_id}
    result = ledger.model_copy(update={"tasks": tasks,
        "wf_session": {key: sid for key, sid in ledger.wf_session.items() if sid in sessions},
        "next_task_seq": _next_task_number(ledger)})
    assert_invariants(result)
    return result


def new_task(
    ledger: TaskLedger,
    capability: str,
    role: TaskRole,
    now: datetime,
    workflow_session_id: str | None = None,
    run_id: str | None = None,
    expected_event: ExpectedEvent | None = None,
    summary: str | None = None,
) -> tuple[TaskLedger, LedgerEntry]:
    """登记新任务;FOREGROUND 登记即取代旧前台(旧前台退位 SUSPENDED,generation +1,§7.3)。"""
    task_number = _next_task_number(ledger)
    entry = LedgerEntry(
        task_id=f"T-{task_number:03d}",
        capability=capability,
        role=role,
        lifecycle=Lifecycle.WAITING_USER if role is TaskRole.SUSPENDED else Lifecycle.RUNNING,
        generation=1,
        workflow_session_id=workflow_session_id,
        run_id=run_id,
        expected_event=expected_event,
        summary=summary,
        created_at=now,
    )
    updated = ledger.model_copy(update={
        "tasks": [*ledger.tasks, entry],
        "focus_task_id": ledger.focus_task_id,
        "next_task_seq": task_number + 1,
    })
    if role is TaskRole.FOREGROUND:
        updated = _demote_foreground(updated, reason=f"被 {entry.task_id} 取代")
        updated = updated.model_copy(update={"focus_task_id": entry.task_id})
    if workflow_session_id:
        updated = updated.model_copy(update={"wf_session": {**updated.wf_session, capability: workflow_session_id}})
    assert_invariants(updated)
    return updated, entry


def _demote_foreground(ledger: TaskLedger, reason: str) -> TaskLedger:
    """当前前台退位为 SUSPENDED(无 expected_event——退位非挂起待答)并 generation +1。"""
    if ledger.focus_task_id is None:
        return ledger
    fg = _find(ledger, ledger.focus_task_id)
    if fg is None:
        return ledger
    demoted = fg.model_copy(
        update={"role": TaskRole.SUSPENDED, "lifecycle": Lifecycle.WAITING_USER,
                "generation": fg.generation + 1, "summary": f"{fg.summary or fg.capability}({reason})"}
    )
    return _replace(ledger, demoted).model_copy(update={"focus_task_id": None})


def suspend(ledger: TaskLedger, task_id: str, expected_event: ExpectedEvent, summary: str | None = None) -> TaskLedger:
    """任务挂起等待用户事件(§5.4 register_suspended_flow 语义;挂起不递增 generation——非取代)。"""
    entry = _require(ledger, task_id)
    # 前台挂起 → 焦点暂空(G-2 应答后 resume);先清指针再改条目,出口断言
    base = ledger.model_copy(update={"focus_task_id": None}) if ledger.focus_task_id == task_id else ledger
    updated = _replace(base, entry.model_copy(
        update={"role": TaskRole.SUSPENDED, "lifecycle": Lifecycle.WAITING_USER,
                "expected_event": expected_event, "summary": summary or entry.summary}))
    assert_invariants(updated)
    return updated


def resume(ledger: TaskLedger, task_id: str) -> tuple[TaskLedger, LedgerEntry]:
    """G-2 命中后恢复前台:expected_event 消费即失效(§7.3 同一追问不消费两次)。"""
    entry = _require(ledger, task_id)
    if entry.role is not TaskRole.SUSPENDED:
        raise LedgerInvariantError(f"{task_id} 非 SUSPENDED,不能 resume(当前 {entry.role})")
    resumed = entry.model_copy(
        update={"role": TaskRole.FOREGROUND, "lifecycle": Lifecycle.RUNNING, "expected_event": None})
    updated = _demote_foreground(_replace(ledger, resumed), reason=f"被 {task_id} 恢复")
    updated = updated.model_copy(update={"focus_task_id": task_id})
    assert_invariants(updated)
    return updated, resumed


def complete(ledger: TaskLedger, task_id: str) -> TaskLedger:
    """任务完成移除;focus 与 wf_session 索引同步清理。"""
    entry = _require(ledger, task_id)
    tasks = [t for t in ledger.tasks if t.task_id != task_id]
    wf = {k: v for k, v in ledger.wf_session.items() if v != entry.workflow_session_id}
    updated = ledger.model_copy(update={
        "tasks": tasks, "wf_session": wf,
        "focus_task_id": None if ledger.focus_task_id == task_id else ledger.focus_task_id})
    assert_invariants(updated)
    return updated


def mark_failed(ledger: TaskLedger, task_id: str, summary: str | None = None) -> TaskLedger:
    """失败/取消标记(POC-17):FAILED + generation+1，活动前台退出焦点。

    v13 冻结角色只有 FOREGROUND/SUSPENDED/BACKGROUND、没有 TERMINAL；因此失败的
    FOREGROUND 转为无 expected_event 的 SUSPENDED 记录以保留审计摘要，后台失败仍
    保持 BACKGROUND。Lifecycle=FAILED 明确区分它与等待用户的真正挂起任务。
    """
    entry = _require(ledger, task_id)
    failed_role = TaskRole.SUSPENDED if entry.role is TaskRole.FOREGROUND else entry.role
    updated = _replace(ledger, entry.model_copy(
        update={"role": failed_role, "lifecycle": Lifecycle.FAILED, "generation": entry.generation + 1,
                "expected_event": None, "summary": summary or entry.summary}))
    if updated.focus_task_id == task_id:
        updated = updated.model_copy(update={"focus_task_id": None})
    assert_invariants(updated)
    return prune_terminal(updated)


def mark_interrupted(ledger: TaskLedger, task_id: str, summary: str | None = None) -> TaskLedger:
    """失败轮退出前台且抬高 generation 栅栏；保留 run/session 以供复核。"""
    entry = _require(ledger, task_id)
    if entry.lifecycle is Lifecycle.INTERRUPTED:
        return ledger
    role = TaskRole.SUSPENDED if entry.role is TaskRole.FOREGROUND else entry.role
    updated = _replace(ledger, entry.model_copy(update={
        "role": role, "lifecycle": Lifecycle.INTERRUPTED, "generation": entry.generation + 1,
        "expected_event": None, "summary": summary or entry.summary,
    }))
    if updated.focus_task_id == task_id:
        updated = updated.model_copy(update={"focus_task_id": None})
    assert_invariants(updated)
    return prune_terminal(updated)


def find_by_event(ledger: TaskLedger, event_type: str) -> LedgerEntry | None:
    """G-2 纯查表:按 expected_event.type 匹配挂起任务(0 LLM、0 后端)。"""
    return next(
        (t for t in ledger.tasks
         if t.role is TaskRole.SUSPENDED and t.expected_event and t.expected_event.type == event_type),
        None,
    )


def is_stale(ledger: TaskLedger, entry: LedgerEntry) -> bool:
    """generation 栅栏:条目已移除或其 generation 已被超越 → 晚到结果只进审计(§7.3)。"""
    current = _find(ledger, entry.task_id)
    return current is None or current.generation > entry.generation


def _require(ledger: TaskLedger, task_id: str) -> LedgerEntry:
    if (entry := _find(ledger, task_id)) is None:
        raise LedgerInvariantError(f"台账中不存在任务 {task_id}")
    return entry


def read_persisted_ledger(value: object) -> tuple[TaskLedger, bool]:
    """宽读历史台账，返回严格模型及是否发现损坏；不修改输入。

    仅持久化入口使用 extra=ignore，运行期写契约不放宽。损坏条目逐条隔离，
    序号仍计入被丢弃条目的合法 ID，避免复用旧任务编号。
    """
    raw = value if isinstance(value, dict) else {}
    damaged = value is not None and not isinstance(value, dict)
    changed = damaged or bool(raw.keys() - TaskLedger.model_fields.keys())
    rows = raw.get("tasks", [])
    if not isinstance(rows, list):
        rows = []
        damaged = True
    cursor = raw.get("next_task_seq", 1)
    if type(cursor) is not int or cursor < 1:
        cursor = 1
        damaged = True
    counts = Counter(row["task_id"] for row in rows if isinstance(row, dict) and isinstance(row.get("task_id"), str))
    entries: list[LedgerEntry] = []
    for row in rows:
        if isinstance(row, dict):
            task_id = row.get("task_id")
            if isinstance(task_id, str) and task_id.startswith("T-") and task_id[2:].isascii() and task_id[2:].isdigit():
                # Limit conversion to Python's supported integer string length.
                if len(task_id[2:]) < 100:
                    cursor = max(cursor, int(task_id[2:]) + 1)
            changed |= bool(row.keys() - LedgerEntry.model_fields.keys())
            event = row.get("expected_event")
            if isinstance(event, dict):
                changed |= bool(event.keys() - ExpectedEvent.model_fields.keys())
        try:
            if isinstance(row, dict) and (type(row.get("generation", 1)) is not int or row.get("generation", 1) < 1):
                raise ValueError("invalid persisted generation")
            entry = LedgerEntry.model_validate(row, extra="ignore")
            if (not entry.task_id.strip() or not entry.capability.strip()
                    or type(entry.generation) is not int or entry.generation < 1
                    or (entry.task_id.startswith("T-") and (
                        not entry.task_id[2:].isascii() or not entry.task_id[2:].isdigit() or len(entry.task_id[2:]) >= 100))):
                raise ValueError("invalid persisted task identity")
        except (ValidationError, ValueError, TypeError):
            damaged = True
            continue
        if isinstance(row, dict) and row.get("lifecycle") not in Lifecycle._value2member_map_:
            damaged = True
        entries.append(entry)
    if any(count > 1 for count in counts.values()):
        entries = [entry for entry in entries if counts[entry.task_id] == 1]
        damaged = True
    foreground = [entry.task_id for entry in entries if entry.role is TaskRole.FOREGROUND]
    focus = raw.get("focus_task_id")
    authority_damaged = len(foreground) > 1 or focus != (foreground[0] if foreground else None)
    if authority_damaged:
        damaged = True
    sessions = {entry.workflow_session_id for entry in entries if entry.workflow_session_id}
    wf = raw.get("wf_session", {})
    if not isinstance(wf, dict):
        wf = {}
        damaged = True
    index = {key: sid for key, sid in wf.items() if isinstance(key, str) and isinstance(sid, str) and sid in sessions}
    damaged |= index != wf
    if authority_damaged:
        # 不猜测冲突前台；保留合法记录但撤销继续执行的权威，抬高版本栅栏。
        entries = [entry.model_copy(update={"role": TaskRole.SUSPENDED, "lifecycle": Lifecycle.FAILED,
                    "expected_event": None, "generation": entry.generation + 1})
                   if entry.role is TaskRole.FOREGROUND else entry for entry in entries]
        focus = None
    ledger = TaskLedger(focus_task_id=focus, tasks=entries, wf_session=index, next_task_seq=cursor)
    assert_invariants(ledger)
    if changed or damaged:
        logging.getLogger(__name__).warning("session_state_quarantined", extra={"observability": {
            "namespace": "ledger", "reason": "invalid_entries" if damaged else "unknown_fields",
            "discarded_count": len(rows) - len(entries),
        }})
    return ledger, damaged
