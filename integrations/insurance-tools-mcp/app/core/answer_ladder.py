"""答案保底阶梯（G1-1 / H-01）——失败出口的最终收口（纯函数、零 IO、零 Agno）。

词汇说明：本模块的 L2/L3 指「答案保底阶梯」级别（L2 证据池部分回答+局限说明 →
L3 兜底话术；L1 完整回答不经阶梯），与 model_router 的 Agno 模型降级链（§12.4
L3）和 §12.3 三层时限（层 1/层 2）是三套独立词汇，检索时注意区分。

H-01 形态：生产路径仅经 verify_envelope.degraded_envelope 触达 L3，出口行为零
变化——L3 话术首版与 verify_envelope 原静态话术逐字一致；L2 材料的运行期采集
（运行中快照、RAG 原文摘录）已随 H-02 落地，出口统一接线随 H-04 落地
（features.answer_ladder 开关届时生效，schema 已随 H-01 进 HarnessConf）。

取消语义纪律（排期 §一.4）：guaranteed_answer 外层只捕 Exception；
asyncio.CancelledError / KeyboardInterrupt / SystemExit（BaseException 族）必须
穿透——超时/取消场景的最终交付动作由 API 层在取消域外零 IO 读快照完成（H-08），
不靠吞取消信号保证 answer。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.admission import MODEL_CAPACITY_CODE, MODEL_CAPACITY_MESSAGE, TOOL_CAPACITY_MESSAGE
from app.schemas import HANDOFF_ACTION, AnswerEnvelope

logger = logging.getLogger(__name__)

# L3 兜底话术清单（首版与 verify_envelope 原静态话术逐字一致——零行为变化）。
# 修订话术须同步：verify_envelope.DEGRADED_ANSWERS（历史安全投影按成员资格把
# 降级答案挡出历史）与锁定精确文案的既有回归；话术送合规预审的节奏随 Owner
# 2026-09-27 方向后置，不阻塞开发。
DEGRADED_ANSWER = "本次处理未能完成，请稍后再试。"
MODEL_UNAVAILABLE_ANSWER = "本次回答未能完成，请稍后重试或联系人工渠道。"
TIMEOUT_ANSWER = "本次处理超时，未能完成请求，请稍后再试。"
CONTEXT_OVERFLOW_ANSWER = "本轮信息较多，未能完成全部整理。请聚焦一个问题或分批提供资料，也可转人工继续处理。"
DEGRADED_GAP = "本次未能完成请求，暂时无法提供完整答复。"
L2_NEXT_STEP = "如需核实未完成部分，请补充具体问题或联系人工渠道。"

L3_MESSAGES: dict[str, str] = {
    MODEL_CAPACITY_CODE: MODEL_CAPACITY_MESSAGE,
    "context_overflow": CONTEXT_OVERFLOW_ANSWER,
    "model_unavailable": MODEL_UNAVAILABLE_ANSWER,
    "turn_timeout": TIMEOUT_ANSWER,
    "service_restarting": "服务正在重启，请稍后重试。",
    "tool_failure": "本次工具调用失败，未能获取相关信息，请稍后再试。",
    "tool_timeout": "本次工具调用超时，未能确认相关信息，请稍后再试。",
    "tool_denied": "本次查询未获授权，无法获取相关信息，请确认查询权限。",
    "tool_capacity": TOOL_CAPACITY_MESSAGE,
}
_L3_DEFAULT = DEGRADED_ANSWER


@dataclass(frozen=True)
class LadderMaterials:
    """L2 部分回答的确定性渲染材料（H-02 采集快照；失败出口消费随 H-04）。

    body 必须是确定性渲染产物（模板 / RAG 原文摘录 / 业务投影），不是模型原文
    直通；limitations 为业务可读局限说明（未完成部分 + 原因 + 建议）。
    """

    body: str
    fact_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class LadderOutcome:
    """阶梯判定结果：最终信封 + 所选级别（审计 ladder_level 的单一来源）。"""

    envelope: AnswerEnvelope
    level: str  # "L2" | "L3"


def l3_answer_text(reason: str | None) -> str:
    """L3 话术按原因取值；未知/缺省原因回落默认兜底。"""
    return L3_MESSAGES.get(reason or "", _L3_DEFAULT)


def guaranteed_answer(
    *,
    reason: str | None = None,
    trace_id: str | None = None,
    materials: LadderMaterials | None = None,
) -> LadderOutcome:
    """三级判定收口：材料可渲染 → L2 部分回答；否则 L3 兜底（永远可达）。

    - L1（完整回答）不是阶梯产物——正常完成路径不经本函数；
    - L2：materials 非空且确定性渲染出非空正文；
    - L3：纯常量话术、零 IO，任何输入下可达；
    - 外层 except Exception 兜到 L3——阶梯自身出错也不能空返；不捕
      BaseException（CancelledError 穿透，见模块 docstring 取消语义纪律）。
    """
    try:
        partial = _l2_partial(materials, trace_id=trace_id)
    except Exception:
        # 阶梯自身失败只丢 L2 机会，不丢答案；现场经观测事件留痕（排障用）。
        logger.warning(
            "answer_ladder_partial_failed",
            extra={"observability": {"reason": reason, "trace_id": trace_id}},
        )
        partial = None
    if partial is not None:
        return LadderOutcome(envelope=partial, level="L2")
    return LadderOutcome(envelope=_l3_envelope(reason, trace_id=trace_id), level="L3")


def _l2_partial(materials: LadderMaterials | None, *, trace_id: str | None) -> AnswerEnvelope | None:
    if materials is None or not materials.body.strip():
        return None
    return AnswerEnvelope(
        answer=materials.body if L2_NEXT_STEP in materials.body else f"{materials.body.rstrip()}\n{L2_NEXT_STEP}",
        status=None,
        completeness="partial",
        limitations=list(materials.limitations),
        fact_refs=list(materials.fact_refs),
        gaps=list(materials.limitations),
        trace_id=trace_id,
    )


def _l3_envelope(reason: str | None, *, trace_id: str | None) -> AnswerEnvelope:
    """L3 静态信封（H-01a 起显式标注 fallback，正文保持原语义）。"""
    return AnswerEnvelope(
        answer=l3_answer_text(reason),
        status="DEGRADED",
        completeness="fallback",
        fact_refs=[],
        gaps=[DEGRADED_GAP],
        followup_actions=[HANDOFF_ACTION],  # 动作位单点定义(schemas/answer)——非工具,修订五后为话术引导
        handoff_status="recommended",
        trace_id=trace_id,
    )
