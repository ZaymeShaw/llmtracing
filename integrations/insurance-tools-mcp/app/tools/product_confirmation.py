"""产品专属候选确认状态；不写台账，也不解析用户的自由确认文本。"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.ledger import LedgerEntry, Lifecycle, TaskLedger, TaskRole
from app.schemas.route import RouteAction, RouteDecision
from tool_adapter_sdk.contracts import ProductCandidateDto

CANDIDATE_LIMIT = 5
CONFIRMATION_TTL_S = 15 * 60
_STATE_KEY = "product_confirmation"


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    label: str = Field(min_length=1)
    name: str = Field(min_length=1)
    code: str = Field(min_length=1)


class _Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    version: Literal[1] = 1
    agent_id: str = Field(min_length=1)
    origin_trace_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    config_revision: str | None
    created_at: float
    candidates: list[_Candidate] = Field(min_length=1, max_length=CANDIDATE_LIMIT)
    prior_task_id: str | None = None
    task_id: str | None = None
    task_generation: int | None = None
    task_created_at: float | None = None
    attempted_trace_id: str | None = None


class ConfirmedProduct(BaseModel):
    """仅由有效台账选择产出；原问题和产品代码不得由模型补填。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    query: str
    name: str
    code: str
    task_id: str
    trace_id: str
    may_call: bool = True


def clear_product_confirmation(session_state: dict[str, Any] | None) -> None:
    if session_state is not None:
        entities = session_state.get("entities")
        if isinstance(entities, dict):
            entities.pop(_STATE_KEY, None)


def _load(session_state: dict[str, Any]) -> _Confirmation | None:
    entities = session_state.get("entities")
    raw = entities.get(_STATE_KEY) if isinstance(entities, dict) else None
    if raw is None:
        return None
    try:
        state = _Confirmation.model_validate(raw)
    except ValidationError:
        clear_product_confirmation(session_state)
        return None
    age = time.time() - state.created_at
    if not 0 <= age <= CONFIRMATION_TTL_S:
        clear_product_confirmation(session_state)
        return None
    return state


def _save(session_state: dict[str, Any], state: _Confirmation) -> None:
    session_state.setdefault("entities", {})[_STATE_KEY] = state.model_dump(mode="json")


def _pending_entry(session_state: dict[str, Any]) -> LedgerEntry | None:
    entities = session_state.get("entities")
    pending = entities.get("pending_clarify") if isinstance(entities, dict) else None
    task_id = pending.get("task_id") if isinstance(pending, dict) else None
    try:
        ledger = TaskLedger.model_validate(session_state.get("ledger") or {})
    except ValidationError:
        return None
    return next((entry for entry in ledger.tasks if entry.task_id == task_id), None)


def candidate_labels(candidates: list[ProductCandidateDto]) -> list[str]:
    """序号防止标签碰撞；名称和代码共同展示，不把同名产品合并。"""
    return [f"候选{i}：{candidate.name}（险种代码：{candidate.product_code}）"
            for i, candidate in enumerate(candidates[:CANDIDATE_LIMIT], start=1)]


def remember_candidates(
    session_state: dict[str, Any] | None, *, candidates: list[ProductCandidateDto],
    query: str, agent_id: str, trace_id: str, config_revision: str | None,
) -> None:
    if session_state is None:
        return
    clear_product_confirmation(session_state)
    shown = candidates[:CANDIDATE_LIMIT]
    if not shown or not agent_id.strip() or any(not candidate.product_code for candidate in shown):
        return
    previous = _pending_entry(session_state)
    state = _Confirmation(
        agent_id=agent_id, origin_trace_id=trace_id, query=query, config_revision=config_revision,
        created_at=datetime.now(timezone.utc).timestamp(), prior_task_id=previous.task_id if previous else None,
        candidates=[_Candidate(label=label, name=candidate.name, code=candidate.product_code or "")
                    for label, candidate in zip(candidate_labels(shown), shown, strict=True)],
    )
    _save(session_state, state)


def _valid_entry(entry: LedgerEntry | None, state: _Confirmation) -> bool:
    return bool(
        entry is not None and entry.role is TaskRole.SUSPENDED
        and entry.lifecycle is Lifecycle.WAITING_USER and entry.capability == "unified_agent"
        and entry.workflow_session_id is None and entry.run_id is None
        and entry.expected_event is not None and entry.expected_event.type == "CLARIFY_ANSWERED"
        and entry.expected_event.max_select == 1
        and entry.expected_event.options == [candidate.label for candidate in state.candidates]
    )


def bind_pending_product(session_state: dict[str, Any], *, agent_id: str, trace_id: str) -> None:
    """仅在 ask_clarify 原入口成功登记后绑定本轮产品候选，不新增挂起写入方。"""
    state = _load(session_state)
    if state is None:
        return
    entry = _pending_entry(session_state)
    # 台账 datetime 仅有微秒精度；候选墙钟向下对齐，避免亚微秒尾差误删同刻新任务。
    candidate_created_at = math.floor(state.created_at * 1_000_000) / 1_000_000
    if (state.agent_id != agent_id or state.origin_trace_id != trace_id or state.task_id is not None
            or not _valid_entry(entry, state) or entry is None
            or entry.task_id == state.prior_task_id or entry.created_at.timestamp() < candidate_created_at):
        clear_product_confirmation(session_state)
        return
    _save(session_state, state.model_copy(update={
        "task_id": entry.task_id, "task_generation": entry.generation,
        "task_created_at": entry.created_at.timestamp(),
    }))


def take_confirmed_product(
    session_state: dict[str, Any] | None, *, query: str, agent_id: str, trace_id: str,
    config_revision: str | None,
) -> ConfirmedProduct | None:
    """读取已有 G-2 权威选择；每轮最多尝试一次，失败/取消下轮可重新确认重试。"""
    if session_state is None:
        return None
    state = _load(session_state)
    if state is None:
        return None
    route = session_state.get("route") or {}
    entry = _pending_entry(session_state)
    try:
        decision = RouteDecision.model_validate(route.get("decision") or {})
    except (ValidationError, AttributeError):
        clear_product_confirmation(session_state)
        return None
    args = decision.resolved_args or {}
    if (state.agent_id != agent_id or state.config_revision != config_revision
            or state.origin_trace_id == trace_id or route.get("trace_id") != trace_id
            or not state.task_id or not _valid_entry(entry, state) or entry is None
            or (entry.task_id, entry.generation, entry.created_at.timestamp())
            != (state.task_id, state.task_generation, state.task_created_at)
            or decision.action is not RouteAction.AGENT or decision.matched_check != "G-2"
            or args.get("event_type") != "CLARIFY_ANSWERED" or args.get("resume_task_id") != state.task_id):
        clear_product_confirmation(session_state)
        return None
    # 名称只用于校验模型复调入参；授权身份始终由 G-2 唯一候选标签确定。
    selected = next((candidate for candidate in state.candidates
                     if candidate.label == args.get("selected_option")), None)
    if selected is None or query not in {state.query, selected.label, selected.name}:
        clear_product_confirmation(session_state)
        return None
    if state.attempted_trace_id == trace_id:
        return ConfirmedProduct(query=state.query, name=selected.name, code=selected.code,
                                task_id=state.task_id, trace_id=trace_id, may_call=False)
    _save(session_state, state.model_copy(update={"attempted_trace_id": trace_id}))
    return ConfirmedProduct(query=state.query, name=selected.name, code=selected.code,
                            task_id=state.task_id, trace_id=trace_id)
