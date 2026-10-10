"""信封组装帮助函数(core 纯函数 · §10.2/§19.1「信封组装」)。

facts 权威产生方 = 工具信封(D16);status_of 实现 §4.8④ 的消费判据
(PARTIAL/EMPTY 触发升级标记,EMPTY 不编造)。
"""
from __future__ import annotations

from app.core.admission import TOOL_CAPACITY_MESSAGE
from app.schemas import EnvelopeStatus, ErrorCode, Evidence, Fact, SourceRef, ToolEnvelope, ToolError
from pydantic import JsonValue
from app.schemas.tool_control import ToolControl


# 失败说明不含业务断言；工具反馈与无事实出口共用，不能用关键词匹配替代完整白名单。
TOOL_FAILURE_ANSWERS = {
    ErrorCode.OVERLOADED: TOOL_CAPACITY_MESSAGE,
    ErrorCode.UPSTREAM: "本次工具调用失败，未能获取相关信息，请稍后再试。",
    ErrorCode.TIMEOUT: "本次工具调用超时，未能确认相关信息，请稍后再试。",
    ErrorCode.DENIED: "本次查询未获授权，无法获取相关信息，请确认查询权限。",
    ErrorCode.BUDGET: "本次查询未能完成，请稍后再试。",
}
SAFE_TOOL_FAILURE_ANSWERS = frozenset({
    *TOOL_FAILURE_ANSWERS.values(),
    "本次查询工具调用失败，暂时无法获取相关信息，请稍后再试。",
    "本次工具调用失败，相关信息未能确认，请稍后再试。",
})


def to_fact(
    predicate: str,
    typed_value,
    subject_id: str,
    source_call_id: str,
    identity_scope: str,
    fact_id: str,
    unit: str | None = None,
    as_of: str | None = None,
    evidence_refs: list[str] | None = None,
    subject_type: str = "policy",
) -> Fact:
    """字段值 → Fact(金额类必带 as_of 与 unit,§10.3 纪律)。

    fact_id 必填无缺省(§10.4 生成规则:F-{request_id}-{序号:03d})——强制调用方
    遵循规则,杜绝「每次调用从 F-001 重计」的同轮撞号(2026-08-26 检视修复)。
    subject_type 缺省 policy;产品/权益/客户断言由 6-a 各工具显式传入
    (Fact.subject_type 为开放字符串,随能力扩展,§10.4)。
    """
    return Fact(
        fact_id=fact_id,
        subject_type=subject_type,
        subject_id=subject_id,
        predicate=predicate,
        typed_value=typed_value,
        unit=unit,
        as_of=as_of,
        source_call_id=source_call_id,
        evidence_refs=evidence_refs or [],
        identity_scope=identity_scope,
    )


def status_of(returned: list[str], requested: list[str]) -> EnvelopeStatus:
    """按「请求字段 vs 实得字段」归一 status:全中 SUCCESS / 部分 PARTIAL / 零 EMPTY。"""
    if not returned:
        return EnvelopeStatus.EMPTY
    if set(returned) >= set(requested):
        return EnvelopeStatus.SUCCESS
    return EnvelopeStatus.PARTIAL


def make_envelope(
    status: EnvelopeStatus,
    facts: list[Fact] | None = None,
    gaps: list[str] | None = None,
    hint: str | None = None,
    source: SourceRef | None = None,
    request_id: str | None = None,
    latency_ms: int | None = None,
    error: ToolError | None = None,
    evidence: list[Evidence] | None = None,
    data: dict[str, JsonValue] | None = None,
    field_descriptions: dict[str, str] | None = None,
    control: ToolControl | None = None,
) -> str:
    """组装 ToolEnvelope 并序列化为 str JSON(§10.2 纪律:工具返回一律 str,规避 agno#6676)。"""
    env = ToolEnvelope(
        ok=status in (EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL),
        status=status,
        facts=facts or [],
        evidence=evidence or [],
        gaps=gaps or [],
        hint=hint,
        source=source,
        request_id=request_id,
        latency_ms=latency_ms,
        error=error,
        data=data,
        field_descriptions=field_descriptions,
        control=control,
    )
    return env.model_dump_json(ensure_ascii=False)
