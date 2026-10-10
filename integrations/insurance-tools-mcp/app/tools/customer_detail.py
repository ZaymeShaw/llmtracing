"""客户详情工具：v5 按客户中心 ID、姓名或手机号查询，保留旧 SDK 结果回放。

真实接口支持零、一、多命中；多命中只表示基本候选，不把空置字段解释为无关联。
可信工号经后端上下文注入，模型不得生成；SDK 旧结构化结果的事实投影保持兼容。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Evidence, Fact, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend import BackendClient, call_backend, error_envelope, error_outcome
from app.tools.backend_contracts import CustomerDetailResponse, parse_backend_response
from app.tools.customer_detail_projection import project_detail
from app.tools.mode import tool_execution_scope
from tool_adapter_sdk.contracts import CUSTOMER_DETAIL, CustomerDetailRequest

IMPLEMENTED = True

_SOURCE = SourceRef(system="policy_svc", endpoint="/customer/detail")


@tool_execution_scope("customer_detail")
async def customer_detail(
    customer_id: str | None = None,
    *,
    customer_ids: list[str] | None = None,
    client_name: str | None = None,
    client_mobile: str | None = None,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
    upstream_user_id: str | None = None,
    agentno: str | None = None,
) -> str:
    """按标准客户标识查询客户详情整卡,返回 str JSON 信封(§10.2 纪律)。"""
    # 旧单 ID 调用允许空白并在本地返回空结果；可用的其它条件不能被空白兼容入口遮蔽。
    customer_id = customer_id.strip() or None if customer_id is not None else None
    client_name = client_name if client_name is not None and client_name.strip() else None
    client_mobile = client_mobile if client_mobile is not None and client_mobile.strip() else None
    if not any((customer_id, customer_ids, client_name, client_mobile)):
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供客户中心ID；只有姓名或手机号时先用 customer_search 定位")

    raw = await call_backend(
        CUSTOMER_DETAIL,
        CustomerDetailRequest(customer_id=customer_id, customer_ids=customer_ids, client_name=client_name, client_mobile=client_mobile),
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        upstream_user_id=upstream_user_id, agentno=agentno,
        client=backend_client,
    )
    if (outcome := error_outcome(raw)) is not None:
        return error_envelope(outcome, source=_SOURCE, raw=raw)
    parsed = parse_backend_response(CustomerDetailResponse, raw, source=_SOURCE)
    if isinstance(parsed, str):
        return parsed
    if parsed.detail_page is not None:
        return project_detail(parsed, agent_id)
    if parsed.customer_id is None:
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint="未查询到该客户" if parsed.policies is None else "未查询到该客户(仅限本人名下客户)",
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )

    facts: list[Fact] = []
    evidence: list[Evidence] = []
    seq = 0

    def _next_evidence() -> Evidence:
        nonlocal seq
        seq += 1
        return Evidence(
            evidence_id=f"E-{parsed.request_id}-{seq:02d}",
            source_type=SourceType.BUSINESS_API,
            source_id="policy_svc",
            retrieved_at=datetime.now(timezone.utc),
        )

    customer_ev = _next_evidence()
    evidence.append(customer_ev)
    if parsed.name or parsed.basic_info is None:
        facts.append(to_fact(
            predicate="customer_name",
            typed_value=parsed.name,
            subject_id=parsed.customer_id,
            subject_type="customer",
            source_call_id=parsed.request_id,
            identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
            evidence_refs=[customer_ev.evidence_id],
        ))
    gaps = []
    if parsed.basic_info is not None:
        fields = parsed.basic_info.model_dump()
        field_mapping = (
            ("customer_number", "customer_number", "客户号"), ("age", "customer_age", "年龄"),
            ("birthday", "customer_birthday", "出生日期"), ("mobile", "customer_mobile", "手机号"),
            ("sex", "customer_sex", "性别"),
        )
        for field, predicate, _ in field_mapping:
            value = fields[field]
            if value is not None:
                facts.append(to_fact(
                    predicate=predicate, typed_value=value,
                    subject_id=parsed.customer_id, subject_type="customer",
                    source_call_id=parsed.request_id, identity_scope=agent_id,
                    fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
                    evidence_refs=[customer_ev.evidence_id],
                ))
        missing = [label for field, _, label in field_mapping if fields[field] is None]
        if not parsed.name:
            missing.insert(0, "姓名")
        if missing:
            gaps.append("部分基本资料未提供：" + "、".join(missing))
        if not facts:
            # 仅主键命中仍有可核验事实，不把缺失的姓名或基本字段补造为空值。
            facts.append(to_fact(
                predicate="customer_id", typed_value=parsed.customer_id,
                subject_id=parsed.customer_id, subject_type="customer",
                source_call_id=parsed.request_id, identity_scope=agent_id,
                fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
                evidence_refs=[customer_ev.evidence_id],
            ))
    if parsed.policies is None:
        gaps.append("名下保单关联未提供，数量未知，不能据此判断没有保单")
    if parsed.benefits is None:
        gaps.append("已享权益关联未提供，数量未知，不能据此判断没有已享权益")
    for item in parsed.policies or []:
        brief = f"{item.insured_name}·{item.product_name}·{item.status}"
        policy_ev = _next_evidence()
        evidence.append(policy_ev)
        facts.append(to_fact(
            predicate="policy_brief",
            typed_value=brief,
            subject_id=item.policy_no,
            subject_type="policy",
            source_call_id=parsed.request_id,
            identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
            evidence_refs=[policy_ev.evidence_id],
        ))
        # 类型化名称 Facts 与 policy_search 同谓词(G-3 显式名回指可消费)
        for predicate, value in (("product_name", item.product_name), ("insured_name", item.insured_name)):
            if value:
                facts.append(to_fact(
                    predicate=predicate,
                    typed_value=value,
                    subject_id=item.policy_no,
                    subject_type="policy",
                    source_call_id=parsed.request_id,
                    identity_scope=agent_id,
                    fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
                    evidence_refs=[policy_ev.evidence_id],
                ))
    for benefit in parsed.benefits or []:
        facts.append(to_fact(
            predicate="enjoyed_benefit",
            typed_value=benefit,
            subject_id=parsed.customer_id,
            subject_type="customer",
            source_call_id=parsed.request_id,
            identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
            evidence_refs=[customer_ev.evidence_id],
        ))
    customer_label = parsed.name if parsed.basic_info is None else (parsed.name or parsed.customer_id)
    policy_hint = "名下保单未知" if parsed.policies is None else f"名下 {len(parsed.policies)} 张保单"
    benefit_hint = "已享权益未知" if parsed.benefits is None else f"{len(parsed.benefits)} 项已享权益"
    return make_envelope(
        status=EnvelopeStatus.PARTIAL if gaps else EnvelopeStatus.SUCCESS,
        gaps=gaps,
        facts=facts,
        evidence=evidence,
        hint=f"客户 {customer_label}:{policy_hint}、{benefit_hint}",
        source=_SOURCE,
        request_id=parsed.request_id,
        latency_ms=parsed.latency_ms,
    )
