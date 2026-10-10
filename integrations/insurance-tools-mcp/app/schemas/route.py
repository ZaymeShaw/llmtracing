"""RouteDecision:S2 Gate 判定输出(方案 v13 §4.2/§4.6 · M0 冻结)。

双通道留痕:session_state["route"](主)+ gate Step 的 StepOutput.content(副,保险位);
S3 三分支 evaluator 读 session_state 消费(POC-11 实测纪律:evaluator 按名注入 run_context)。
D14:action 枚举无 CACHE(Gate 不探测框架私有缓存键)。
"""
from __future__ import annotations

from app.compat import StrEnum

from pydantic import BaseModel, ConfigDict


class RouteAction(StrEnum):
    """S3 分支动作(§4.6 放行动作 + Agent 兜底;D14 后无 CACHE)。"""

    DIRECT_TOOL = "DIRECT_TOOL"  # G-4a/G-3 带号直查；G-4c 语义直达清单搜索(policy/customer_search)
    DIRECT_RAG = "DIRECT_RAG"  # G-4b 锚点(LOW,单发检索 + 轻量组织 ×1)
    WORKFLOW_START = "WORKFLOW_START"  # 锚点/双入口启动业务 Workflow(任意风险,HIGH 词典从严)
    WORKFLOW_CONTINUE = "WORKFLOW_CONTINUE"  # G-2 命中 @pause expected_event 后恢复
    AGENT = "AGENT"  # 词面未命中 → Unified Agent(语义分发默认位,D15)


class RouteCandidate(BaseModel):
    """Gate 移交 Agent 的候选能力摘要(§5.3 注入清单)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_id: str
    summary: str


class RouteDecision(BaseModel):
    """Gate 判定结果:一切判据版本化可回放(§4.5),reason 留痕供审计归因。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: RouteAction
    matched_check: str | None = None  # 放行检查名:G-2/G-3/G-4a/G-4b;None=默认 Agent
    capability_id: str | None = None  # 指向能力(WORKFLOW_START/CONTINUE / 锚点命中)
    resolved_args: dict[str, str | int | float | list[str]] | None = None  # 确定性解析实参(G-3/G-4a)
    candidates_for_agent: list[RouteCandidate] = []  # 未放行时移交 Agent 的候选(§5.3)
    reason: str = ""  # 判定依据描述(影子/回放/审计归因)
