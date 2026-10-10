"""ExecutionResult(统一返回契约)+ AgentOutput 子契约(方案 v13 §10.4 · D16 · M0 冻结)。

任何执行体(Direct / 业务 Workflow / Agent / 后台深查)→ 主链路的唯一返回契约;
Direct / Workflow 的 facts 由工具信封确定性组装,无 answer_draft(渲染一律在 finalize,评审五);
Agent 的 output_schema = AgentOutput——模型只产 answer_draft + fact_refs 引用,
facts/evidence 唯一写入方 = 工具信封,薄包装层按 fact_refs 从证据池收集组装(D16)。
"""
from __future__ import annotations

from app.compat import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from app.schemas.answer import StructuredToolResult
from app.schemas.enums import EnvelopeStatus
from app.schemas.evidence import Evidence
from app.schemas.fact import Fact


class RenderHint(StrEnum):
    """finalize 组织形态提示(§12.1 输入形态;冻结评审项 2026-08-26 随切片 3-c 定稿)。

    形态①(Agent draft)不需要 hint——answer_draft 字段存在性即判据。
    执行体一律 0 LLM/0 渲染,只产 facts/hint;正文组织单点发生在 finalize。
    (2026-09-20 检视:形态⑤ DIRECT_LLM_LIGHT 的 finalize 侧加工已整体下线——
    直达清单改由 ExecutionResult.structured 直出结构化结果,不做 LLM 加工。)
    """

    TEMPLATE = "template"  # ③ 模板直出(0 组织 LLM;Direct 或 Agent 终止型清单卡)
    RAG_LIGHT = "rag_light"  # ② 单发 RAG + 轻量级确定性组织(0 LLM,G-4b 锚点)
    CLARIFY_CARD = "clarify_card"  # ④ 澄清卡(§09 澄清体系,切片 4)


class BusinessClaim(BaseModel):
    """Exact current-turn fact binding; values and labels are server rendered."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    subject_id: str
    field: str
    fact_id: str


class DerivedClaim(BaseModel):
    """Closed arithmetic operations over compatible current-turn numeric facts."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal['monthly_average', 'sum', 'difference']
    fact_refs: list[str] = Field(min_length=1, max_length=20)


class AgentOutput(BaseModel):
    """Unified Agent 的 output_schema(D16:模型只产引用,永不转写 facts/evidence)。

    schema 层结构保证:本契约不存在 facts / evidence 字段——转写旁路被关闭。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_draft: str  # 答案草稿(经 finalize 出口检查后成为 AnswerEnvelope.answer)
    fact_refs: list[str] = []  # Current-turn knowledge references.
    claims: list[BusinessClaim] = Field(default_factory=list, max_length=100)
    derived: list[DerivedClaim] = Field(default_factory=list, max_length=20)
    gaps: list[str] = []
    needs_user_input: bool = False  # 本轮以澄清收尾(与 ask_clarify 工具配合)
    task_updates: list[str] = []  # 给 Task Ledger 的更新指令(结构随 M2 拆轮细化)


class ClarifyPayload(BaseModel):
    """形态④澄清卡载荷(§09:≤5 候选 + expected_event;结构化候选在 Ledger,
    本载荷是 finalize 渲染与 G-2 匹配共享的数据投影)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str
    options: list[str]  # 用户可见候选(≤5);内部 std_id 仅保存在 Ledger expected_event
    event_type: str  # 如 POLICY_SELECTED(与 expected_event.type 一致)


class ExecutionResult(BaseModel):
    """三机制最终统一输出(§10.4):status 由产出方归一(Agent 路径 = 包装层归一)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnvelopeStatus
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    gaps: list[str] = []
    needs_user_input: bool = False
    task_updates: list[str] = []
    # 只有 Agent 路径产 draft(D16);Direct / Workflow 执行体不产,其正文组织在 finalize
    # 唯一出口(模板直出 / RAG 轻组织,均 0 LLM)。
    answer_draft: str | None = None
    render_hint: RenderHint | None = None  # finalize 组织形态提示(§12.1;枚举见 RenderHint)
    clarify: ClarifyPayload | None = None  # 形态④专用(render_hint=CLARIFY_CARD 时必填)
    # 无 facts 时的兜底正文,按查询主体由 Direct 分支注入;缺省用 render 通用话术
    # (默认话术为保单口径——customer 等其他主体分支必须显式提供,防主体错位)。
    # 2026-09-17 起同时承载 EMPTY(零命中)与 AMBIGUOUS(条件不可执行):两者都没有
    # facts,原先只覆盖 EMPTY,导致客户 AMBIGUOUS 落回保单话术且工具层原因被丢弃。
    empty_answer: str | None = None
    # 清单卡片轮的工具结构化结果(2026-09-20 Owner 决策:不做 LLM 加工,
    # 由前端适配结构化输出);写入方 = Direct 清单分支或 Agent 终止型搜索旁路。
    structured: StructuredToolResult | None = None
