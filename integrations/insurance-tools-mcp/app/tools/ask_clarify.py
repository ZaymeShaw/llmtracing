"""ask_clarify 工具核心(§10.1 新增·本地:Unified Agent 主动澄清,写挂起并终止本轮)。

Owner 2026-08-30 口径:通用澄清工具即可。挂起态写入经 core/clarify 单一写入点
(§09 三产出方之一);带 options 的枚举型澄清登记 expected_event,下一轮 G-2 查表
匹配(序号/候选名);无 options 的开放语义澄清不可查表匹配,自然漏放回 Agent
(归属规则④,失败方向安全)。本地工具:0 后端调用、无 request_id(§10.3)。
"""
from __future__ import annotations

from app.conf.runtime import read_once

from datetime import datetime, timezone

from app.conf import harness_conf
from app.core.clarify import open_clarify_streak, register_clarification
from app.core.envelope import make_envelope
from app.core.tool_control import control_for, control_message
from app.schemas.tool_control import ToolControlReason
from app.schemas import ClarifyEvent, ErrorCode, EnvelopeStatus, ToolError

IMPLEMENTED = True

CLARIFY_EVENT = ClarifyEvent.CLARIFY_ANSWERED
_CAPABILITY = "unified_agent"


async def ask_clarify(
    question: str = "",
    options: list[str] | None = None,
    *,
    session_state: dict,
    agent_id: str,
    trace_id: str,
    budget: dict,
) -> str:
    """向用户发起澄清(登记挂起、指示本轮终止),返回 NEEDS_CLARIFICATION 信封。

    调用方(Agent)收到本信封后应以 needs_user_input 收尾,不得继续取数作答。
    开放澄清(无 options)连续超限(harness max_consecutive_open_clarify_rounds)时
    不再登记新挂起,返回 ERROR 信封——bridge 转形态③后由 finalize 以转人工话术
    收尾(§10 澄清纪律;枚举澄清有候选锚点不受此限)。
    """
    normalized_options = [str(o) for o in options or []]
    normalized_question = question.strip()
    if normalized_question.lower() in {"", "placeholder", "n/a", "tbd", "todo"}:
        normalized_question = (
            "为避免查询错误，请选择要继续处理的对象："
            if normalized_options
            else "为避免查询错误，请补充需要确认的信息："
        )
    if not normalized_options and (
        open_clarify_streak(session_state) >= read_once("harness", harness_conf).max_consecutive_open_clarify_rounds
    ):
        # 会话级追问预算耗尽：code=BUDGET（不可重试），status=ERROR 走 bridge
        # 停止型链路（_TERMINAL_TOOL_STATUSES 不含 BUDGET 信封形态）。
        control = control_for(ToolControlReason.OPEN_CLARIFY_LIMIT)
        return make_envelope(
            status=EnvelopeStatus.ERROR,
            control=control,
            gaps=["连续多轮未能确认所需信息，为避免误查已停止追问，请转人工渠道办理"],
            hint=control_message(control),
            error=ToolError(code=ErrorCode.BUDGET, retryable=False),
        )
    payload = register_clarification(
        session_state,
        question=normalized_question,
        options=normalized_options,
        event_type=CLARIFY_EVENT,
        capability=_CAPABILITY,
        run_id=(session_state.get("route") or {}).get("run_id"),
        now=datetime.now(timezone.utc),
    )
    hint = payload.question
    if payload.options:
        hint = f"{payload.question}(候选:{'、'.join(payload.options)})"
    return make_envelope(status=EnvelopeStatus.NEEDS_CLARIFICATION, hint=hint)
