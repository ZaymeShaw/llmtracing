"""接入层运行与信封结构校验。

出口合规检测已关闭；保留完成态与可解析 AnswerEnvelope 的机械检查，
避免 Agno 静默跳过步骤后把错误占位串当作答案。

H-01 起静态降级话术与 L3 兜底由答案阶梯（app/core/answer_ladder）承载——
本模块继续导出同名常量（历史安全消费方与既有回归不变）。
"""
from __future__ import annotations

from pydantic import ValidationError

from app.core.answer_ladder import (
    CONTEXT_OVERFLOW_ANSWER,
    DEGRADED_ANSWER,
    MODEL_UNAVAILABLE_ANSWER,
    TIMEOUT_ANSWER,
    guaranteed_answer,
)
from app.schemas import AnswerEnvelope

# 拦截原因(审计事件字段)
UNPARSEABLE = "unparseable"  # content 非法 JSON 或不符合 AnswerEnvelope 结构
RUN_INCOMPLETE = "run_incomplete"  # run 未 COMPLETED(content 不可信)

DEGRADED_ANSWERS = frozenset({
    DEGRADED_ANSWER, MODEL_UNAVAILABLE_ANSWER, TIMEOUT_ANSWER, CONTEXT_OVERFLOW_ANSWER,
    # 仅历史兼容：以下文案无当前生产者，历史投影仍须识别为失败答案。
    "模型服务繁忙，请稍后再试。",
    "本轮查询未能完整完成，请稍后重试或转人工。",
    "服务暂时繁忙,已为您保留已确认的信息;如需完整答案请稍后重试或转人工。",
})


def degraded_envelope(
    fact_refs: list[str], trace_id: str | None = None, *, reason: str | None = None,
) -> AnswerEnvelope:
    """最低依赖静态答复；保留旧参数签名，但失败后不回放未交付的业务事实。

    H-01 起内部改调答案阶梯 L3（answer_ladder.guaranteed_answer）——出口行为
    零变化；fact_refs 旧参数仅为签名兼容，降级恒不回放引用。
    """
    return guaranteed_answer(reason=reason, trace_id=trace_id).envelope


def verify_or_downgrade(
    run_status: object,
    content: str | object,
    *,
    trace_id: str | None = None,
) -> tuple[AnswerEnvelope, bool, str | None]:
    """校验 run 完成态与信封结构；引用与数值验证已由专用流程负责。

    返回 (envelope, intercepted, reason):intercepted=False 时 envelope 为原信封;
    intercepted=True 时 envelope 为降级信封,reason 供接入层写审计事件(D13)。
    """
    # agno RunStatus 为普通 Enum(非 StrEnum):str(成员) = "RunStatus.completed" 而值 = "COMPLETED"
    # ——取 .value 归一(无 .value 的裸 str 原样);严格等于,不做 endswith/大小写折叠
    status = getattr(run_status, "value", run_status)
    if str(status) != "COMPLETED":
        return degraded_envelope([], trace_id), True, RUN_INCOMPLETE
    try:
        env = AnswerEnvelope.model_validate_json(str(content))
    except (ValidationError, ValueError):
        return degraded_envelope([], trace_id), True, UNPARSEABLE
    if (env.status is not None and env.completeness != "fallback") or not env.answer.strip():
        return degraded_envelope([], trace_id), True, UNPARSEABLE
    return env, False, None
