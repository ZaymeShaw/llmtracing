"""澄清统一挂起态写入点(§09:一个 clarify 模块 + 三个触发点)。

三产出方(Direct 执行器 / Workflow @pause / Agent ask_clarify)共用同一挂起态
结构与同一写入路径;台账写入全部经 core/ledger 唯一写入点(I1 前台唯一等不变量
由 ledger 断言)。除本模块外禁止直接写 session_state["entities"]["pending_clarify"]
(纪律「禁止第二份挂起态写入路径」,§0.7/§09)。
"""
from __future__ import annotations

from datetime import datetime

from app.core.ledger import complete as ledger_complete
from app.core.ledger import mark_failed
from app.core.ledger import new_task
from app.core.ledger import resume as ledger_resume
from app.core.ledger import suspend as ledger_suspend
from app.core.session_health import session_turn_seq
from app.schemas.execution import ClarifyPayload
from app.schemas.ledger import ExpectedEvent, TaskLedger, TaskRole

_EMPTY_LEDGER: dict[str, object] = {"focus_task_id": None, "tasks": [], "wf_session": {}}

# 连续开放澄清轮数（§10 澄清纪律）：开放澄清无候选、无恢复锚点，用户答非所问时
# 模型可能无限追问（真模型实测轮 3 仍澄清）。计数随开放登记 +1，有效消费/挂起失败
# 清零；ask_clarify 预检超限后停止追问、转人工话术收尾。
_OPEN_STREAK_KEY = "open_clarify_streak"


def open_clarify_streak(session_state: dict) -> int:
    """当前连续开放澄清轮数（只读视图，ask_clarify 准入预检用）。"""
    value = (session_state.get("entities") or {}).get(_OPEN_STREAK_KEY)
    return value if isinstance(value, int) and value > 0 else 0


def clarification_is_current(session_state: dict, task_id: str, *, answering: bool = False) -> bool:
    """Only the pointed-to, dated clarification is eligible; legacy undated cards expire."""
    pending = (session_state.get("entities") or {}).get("pending_clarify") or {}
    registered = pending.get("turn_seq")
    if pending.get("task_id") != task_id or type(registered) is not int or registered < 0:
        return False
    age = session_turn_seq(session_state) - registered
    return age == 1 if answering else age in (0, 1)


def expire_clarifications(session_state: dict, *, unanswered: bool = False, reset_pending: bool = False) -> None:
    """Expire stale/orphan events; after Gate, also expire an unanswered enum card.

    Open questions remain available to the Agent for the adjacent answering turn.
    Ledger failure increments generation and clears the event without deleting history.
    """
    if reset_pending:
        entities = session_state.get("entities") or {}
        entities.pop("pending_clarify", None)
        entities.pop(_OPEN_STREAK_KEY, None)
    ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    for entry in ledger.tasks:
        if entry.role is not TaskRole.SUSPENDED or entry.expected_event is None:
            continue
        stale = not clarification_is_current(session_state, entry.task_id)
        missed = unanswered and bool(entry.expected_event.options) and clarification_is_current(
            session_state, entry.task_id, answering=True,
        )
        if stale or missed:
            ledger = mark_failed(ledger, entry.task_id, summary="expired: 澄清已过期")
    from app.core.ledger import prune_terminal
    ledger = prune_terminal(ledger)
    session_state["ledger"] = ledger.model_dump()
    entities = session_state.get("entities") or {}
    pending = entities.get("pending_clarify") or {}
    if pending and not any(
        entry.task_id == pending.get("task_id") and entry.expected_event is not None
        for entry in ledger.tasks
    ):
        entities.pop("pending_clarify", None)
        entities.pop(_OPEN_STREAK_KEY, None)


def retry_clarification(session_state: dict, task_id: str) -> bool:
    """Give a failed, unconsumed answering turn one adjacent retry; never replay a resumed run."""
    if not clarification_is_current(session_state, task_id, answering=True):
        return False
    ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    entry = next((item for item in ledger.tasks if item.task_id == task_id), None)
    if entry is None or entry.role is not TaskRole.SUSPENDED or entry.expected_event is None:
        return False
    pending = session_state["entities"]["pending_clarify"]
    confirmation = session_state["entities"].get("product_confirmation")
    if (isinstance(confirmation, dict) and confirmation.get("task_id") == task_id
            and confirmation.get("attempted_trace_id")):
        # Product identity confirmations authorize one adjacent lookup. A failed
        # answering turn must not extend that single-use product card.
        return False
    if pending.get("retry_used"):
        fail_clarification(session_state, task_id, summary="澄清续接重试仍失败，请重新发起查询")
        return False
    pending.update(turn_seq=session_turn_seq(session_state), retry_used=True)
    return True


def retry_answered_agent_clarification(session_state: dict) -> bool:
    """Only the current Agent G-2 selection may retain its original card for one retry.

    The existing retry contract checks adjacency, pending ownership and suspension;
    repeated settlement in one turn cannot extend the card or consume another retry.
    New questions and Direct/business Workflow continuations own their own settlement.
    """
    decision = (session_state.get("route") or {}).get("decision") or {}
    if not isinstance(decision, dict) or decision.get("action") != "AGENT" or decision.get("matched_check") != "G-2":
        return False
    resolved = decision.get("resolved_args") or {}
    task_id = resolved.get("resume_task_id") if isinstance(resolved, dict) else None
    if not isinstance(task_id, str) or not task_id:
        return False
    try:
        return retry_clarification(session_state, task_id)
    except (ValueError, TypeError, KeyError):
        # A damaged task cannot block the already selected failure answer.
        return False


def register_clarification(
    session_state: dict,
    *,
    question: str,
    options: list[str],
    event_type: str,
    capability: str,
    fields: tuple[str, ...] | list[str] = (),
    display_options: list[str] | None = None,
    workflow_session_id: str | None = None,
    run_id: str | None = None,
    now: datetime,
) -> ClarifyPayload:
    """登记澄清挂起:Ledger SUSPENDED(expected_event)+ pending_clarify 原始意图。

    返回 ClarifyPayload(渲染/匹配投影;结构化候选权威在 Ledger expected_event)。
    ≤5 候选(§4.8③),去重保序;fields 为挂起时已解析的字段意图(G-3 续接记忆,
    仅 Direct 等确定性触发方需要,Agent 开放澄清缺省空)。
    """
    capped, visible = _normalize_options(options, display_options)
    ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    pending = (session_state.get("entities") or {}).get("pending_clarify") or {}
    previous_task_id = str(pending.get("task_id") or "")
    previous = next((item for item in ledger.tasks if item.task_id == previous_task_id), None)
    if (
        previous is not None
        and previous.expected_event is not None
        and not previous.expected_event.options
    ):
        # 开放澄清由 Agent 会话历史承接；若模型进一步提出新澄清，旧开放问题已被
        # 本轮输入/新问题取代，先完成旧任务，避免 pending 指针覆盖后留下孤儿。
        # 不清零连续计数：被取代≠用户给出了可用信息，循环仍在继续。
        consume_clarification(session_state, previous_task_id, reset_streak=False)
    # Replacing an enum card must not leave its choices available as an orphan.
    expire_clarifications(session_state)
    ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    previous = next((item for item in ledger.tasks if item.task_id == previous_task_id), None)
    if previous is not None and previous.expected_event is not None and previous.expected_event.options:
        fail_clarification(session_state, previous.task_id, summary="expired: 澄清已被新问题取代")
        ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    updated, entry = new_task(
        ledger, capability=capability, role=TaskRole.SUSPENDED,
        now=now, expected_event=ExpectedEvent(type=event_type, options=capped),
        workflow_session_id=workflow_session_id, run_id=run_id, summary=question,
    )
    session_state["ledger"] = updated.model_dump()
    pending_state: dict[str, object] = {
        "task_id": entry.task_id,
        "fields": list(fields),
        "turn_seq": session_turn_seq(session_state),
    }
    if visible != capped:
        pending_state["option_labels"] = visible
    session_state.setdefault("entities", {})["pending_clarify"] = pending_state
    if not capped:
        session_state["entities"][_OPEN_STREAK_KEY] = open_clarify_streak(session_state) + 1
    return ClarifyPayload(question=question, options=visible, event_type=event_type)


def resuspend_clarification(
    session_state: dict,
    task_id: str,
    *,
    question: str,
    options: list[str],
    event_type: str,
    display_options: list[str] | None = None,
) -> ClarifyPayload:
    """同一业务 Workflow 再次暂停：更新原任务，不创建第二份挂起。"""
    capped, visible = _normalize_options(options, display_options)
    if not capped:
        raise ValueError("业务 Workflow 重暂停必须提供候选")
    ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    session_state["ledger"] = ledger_suspend(
        ledger,
        task_id,
        ExpectedEvent(type=event_type, options=capped),
        summary=question,
    ).model_dump()
    pending_state: dict[str, object] = {
        "task_id": task_id,
        "fields": [],
        "turn_seq": session_turn_seq(session_state),
    }
    if visible != capped:
        pending_state["option_labels"] = visible
    session_state.setdefault("entities", {})["pending_clarify"] = pending_state
    return ClarifyPayload(question=question, options=visible, event_type=event_type)


def visible_clarification_options(
    options: list[str],
    pending: dict,
    task_id: str,
) -> list[str]:
    """返回当前任务的用户可见候选；内部标准 ID 仍只保存在 Ledger。"""
    labels = pending.get("option_labels") if pending.get("task_id") == task_id else None
    if (
        isinstance(labels, list)
        and len(labels) == len(options)
        and all(isinstance(label, str) and label.strip() for label in labels)
        and len(set(labels)) == len(labels)
    ):
        return list(labels)
    return list(options)


def _normalize_options(
    options: list[str],
    display_options: list[str] | None,
) -> tuple[list[str], list[str]]:
    """候选 ID 去重限长，同时保持显示标签与 ID 的位置映射。"""
    if display_options is not None and len(display_options) != len(options):
        raise ValueError("显示候选与内部候选数量必须一致")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, option in enumerate(options):
        value = str(option).strip()
        if not value or value in seen:
            continue
        label = str(display_options[index]).strip() if display_options is not None else value
        if not label:
            raise ValueError("显示候选不得为空")
        seen.add(value)
        pairs.append((value, label))
        if len(pairs) == 5:
            break
    capped = [value for value, _ in pairs]
    visible = [label for _, label in pairs]
    if len(set(visible)) != len(visible):
        raise ValueError("显示候选必须唯一，避免用户选择歧义")
    return capped, visible


def consume_clarification(session_state: dict, task_id: str, *, reset_streak: bool = True) -> None:
    """消费已回答的澄清任务；兼容执行器已先把 Workflow 恢复为前台的形态。

    Direct 路径从 SUSPENDED 一次性 resume+complete；Workflow 路径在 continue_run
    前先 resume，成功后只 complete。仅清理与 task_id 绑定的 pending，避免误删新追问。
    有效消费（默认）同时清零连续开放澄清计数；register 内联清理旧开放任务时传
    reset_streak=False（被取代≠用户给出可用信息）。
    """
    ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    entry = next((item for item in ledger.tasks if item.task_id == task_id), None)
    if entry is None:
        raise ValueError(f"台账中不存在澄清任务 {task_id}")
    resumed = ledger
    if entry.role is TaskRole.SUSPENDED:
        resumed, _ = ledger_resume(ledger, task_id)
    session_state["ledger"] = ledger_complete(resumed, task_id).model_dump()
    entities = session_state.get("entities") or {}
    pending = entities.get("pending_clarify") or {}
    if pending.get("task_id") == task_id:
        entities.pop("pending_clarify", None)
    if reset_streak:
        entities.pop(_OPEN_STREAK_KEY, None)


def fail_clarification(session_state: dict, task_id: str, *, summary: str) -> None:
    """恢复执行失败：Ledger 保留 FAILED 审计摘要，待答事件与 pending 同步失效。

    挂起失败即澄清链条断裂，连续开放澄清计数一并清零（下轮重新起算）。
    """
    ledger = TaskLedger.model_validate(session_state.get("ledger") or _EMPTY_LEDGER)
    session_state["ledger"] = mark_failed(ledger, task_id, summary=summary).model_dump()
    entities = session_state.get("entities") or {}
    pending = entities.get("pending_clarify") or {}
    if pending.get("task_id") == task_id:
        entities.pop("pending_clarify", None)
    entities.pop(_OPEN_STREAK_KEY, None)
