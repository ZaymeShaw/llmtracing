"""benefit_info 工具核心(§10.1 存量·接口 4:权益/服务详情查询)。

Owner 2026-09-01 契约:入参 = 服务/权益查询的自然语言 query(接口内做名称匹配
归一召回);未匹配不编造,如实按 EMPTY/PARTIAL 披露。要素卡五元组(内容/门槛/
方式/有效期/资格条件)映射为 Fact,谓词化引用。
客户已享权益盘点统一由 customer_search → customer_detail 承载；本工具不接收
customer_id，也不做客户资格推断。若需正式资格核验，必须等待确定性后端接口。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Evidence, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend import BackendClient, call_backend, error_envelope, error_outcome
from app.tools.backend_contracts import BenefitDetailResponse, parse_backend_response
from app.tools.mode import tool_execution_scope
from app.tools.real.benefit_knowledge import benefit_documents_envelope
from tool_adapter_sdk.contracts import BENEFIT_DETAIL, BenefitDetailRequest

IMPLEMENTED = True

_SOURCE = SourceRef(system="benefit_svc", endpoint="/benefit/detail")

# 要素卡五元组(§11.5 benefit_service 领域纪律;资格结论只来自接口,模型不得推断)
_FIELDS: tuple[str, ...] = ("content", "threshold", "usage", "validity", "eligibility")


@tool_execution_scope("benefit_info")
async def benefit_info(
    query: str,
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
) -> str:
    """按自然语言 query 查权益详情要素，返回 str JSON 信封。"""
    text = query.strip()
    if not text:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供权益/服务查询")

    raw = await call_backend(
        BENEFIT_DETAIL,
        BenefitDetailRequest(query=text),
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        client=backend_client,
    )
    if (outcome := error_outcome(raw)) is not None:
        return error_envelope(outcome, source=_SOURCE, raw=raw)
    parsed = parse_backend_response(BenefitDetailResponse, raw, source=_SOURCE)
    if isinstance(parsed, str):
        return parsed
    if parsed.documents is not None:
        return benefit_documents_envelope(parsed, agent_id=agent_id, source=_SOURCE)
    results = parsed.results
    unknown = parsed.unknown
    facts: list = []
    evidence: list = []
    for i, item in enumerate(results):
        std = item.standard_name
        evidence_id = f"E-{parsed.request_id}-{i + 1:02d}"
        evidence.append(Evidence(
            evidence_id=evidence_id,
            source_type=SourceType.BUSINESS_API,
            source_id="benefit_svc",
            retrieved_at=datetime.now(timezone.utc),
        ))
        for key in _FIELDS:
            value = getattr(item, key)
            if value is None or str(value) == "":
                continue
            facts.append(to_fact(
                predicate=key,
                typed_value=value,
                subject_id=std,
                subject_type="benefit",
                source_call_id=parsed.request_id,
                identity_scope=agent_id,
                fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
                evidence_refs=[evidence_id],
            ))

    gaps = [f"权益名未匹配:{u}" for u in unknown]
    if not facts:
        return make_envelope(
            status=EnvelopeStatus.EMPTY, hint="未识别或未匹配到权益/服务名称，请提供更完整的权益问题",
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )
    status = EnvelopeStatus.PARTIAL if gaps else EnvelopeStatus.SUCCESS
    return make_envelope(
        status=status,
        facts=facts,
        evidence=evidence,
        gaps=gaps or None,
        source=_SOURCE,
        request_id=parsed.request_id,
        latency_ms=parsed.latency_ms,
    )
