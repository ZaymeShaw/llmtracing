"""product_knowledge 工具核心(§10.1 存量·接口 1+2:产品名标准化 → 产品信息 RAG)。

Owner 2026-09-01 契约:入参 = 产品信息查询的自然语言 query(单一 str)——产品名
解析在链路内完成(接口 1 三态:exact/fuzzy≤5/empty),模型不产标准名(P5)。
fuzzy 多候选以 AMBIGUOUS 信封透传,由上层(Direct 执行器澄清卡 / Agent
ask_clarify)接续,本工具不猜测;一句 query 命中多个不同产品同样按 AMBIGUOUS
候选透传(对比类问法应逐产品分别调用,修订五)。query 命中的条款维度
(core/clause_dims)决定请求集与 gaps 口径——未指明维度按全维度请求;
条款信息切成 Fact(谓词化)+ Evidence(rag_document,带版本与文内定位,§10.4)。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.clause_dims import scan_clause_dimensions
from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Evidence, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend import BackendClient, call_backend, error_envelope, error_outcome
from app.tools.backend_contracts import (
    ProductRagResponse,
    ProductResolveResponse,
    parse_backend_response,
)
from app.tools.real.product_knowledge import product_documents_envelope
from app.tools.mode import require_tool_execution_context, tool_execution_scope
from app.tools.product_confirmation import (
    CANDIDATE_LIMIT, candidate_labels, clear_product_confirmation,
    remember_candidates, take_confirmed_product,
)
from tool_adapter_sdk.contracts import (
    PRODUCT_RAG,
    PRODUCT_RESOLVE,
    ProductRagRequest,
    ProductResolveRequest,
)

IMPLEMENTED = True

_RESOLVE_SOURCE = SourceRef(system="product_svc", endpoint="/product/resolve")
_RAG_SOURCE = SourceRef(system="product_svc", endpoint="/product/rag")

# 条款字段码 → 文内定位(Evidence.locator;条款引用必须带版本与 locator,§11.5 产品纪律)
_FIELDS: tuple[tuple[str, str], ...] = (
    ("waiting_period", "等待期条款"),
    ("liability_summary", "保险责任条款"),
    ("exclusion_clause", "责任免除条款"),
    ("underwriting_rules", "投保规则"),
)


@tool_execution_scope("product_knowledge")
async def product_knowledge(
    query: str,
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
    session_state: dict | None = None,
) -> str:
    """按自然语言 query 查产品条款/责任/等待期/免责/投保规则,返回 str JSON 信封。"""
    text = query.strip()
    context = require_tool_execution_context()
    confirmation = take_confirmed_product(
        session_state, query=text, agent_id=agent_id, trace_id=trace_id,
        config_revision=context.config_revision,
    )
    if confirmation is not None:
        if not confirmation.may_call:
            return make_envelope(status=EnvelopeStatus.AMBIGUOUS, hint="本轮已尝试该确认，请勿重复调用；失败可下一轮重新选择")
        return await _fetch_product_rag(
            confirmation.query, confirmation.name, product_code=confirmation.code,
            agent_id=agent_id, trace_id=trace_id, budget=budget, backend_client=backend_client,
        )
    # 开始新问题前清理旧候选；异常或取消不能让旧身份继续适用于新问题。
    clear_product_confirmation(session_state)
    if not text:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供产品查询问题")

    resolved = await call_backend(
        PRODUCT_RESOLVE, ProductResolveRequest(query=text),
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        client=backend_client,
    )
    if (outcome := error_outcome(resolved)) is not None:
        return error_envelope(outcome, source=_RESOLVE_SOURCE, raw=resolved)
    resolved_dto = parse_backend_response(ProductResolveResponse, resolved, source=_RESOLVE_SOURCE)
    if isinstance(resolved_dto, str):
        return resolved_dto
    if resolved_dto.match == "empty":
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint=f"未能匹配到产品,请提供更完整的产品名(query:{text[:40]})",
        )
    if resolved_dto.match == "fuzzy":
        candidates = resolved_dto.candidates[:CANDIDATE_LIMIT]
        coded = all(c.product_code for c in candidates)
        labels = candidate_labels(candidates) if coded else [c.name for c in candidates]
        if coded:
            remember_candidates(
                session_state, candidates=candidates, query=text, agent_id=agent_id,
                trace_id=trace_id, config_revision=context.config_revision,
            )
        # 结构化候选 facts(6-b):候选即类型化断言,Direct/Workflow 澄清生产方可直接消费(§4.8③)
        candidate_facts = [
            to_fact(
                predicate="product_candidate",
                typed_value=c.intro,
                subject_id=labels[i],
                subject_type="product",
                source_call_id=resolved_dto.request_id,
                identity_scope=agent_id,
                fact_id=f"F-{resolved_dto.request_id}-{i + 1:03d}",
            )
            for i, c in enumerate(candidates)
        ]
        names = labels
        hint = f"query 命中 {len(names)} 个产品候选:{'、'.join(names)},请确认后逐个查询"
        if coded:
            hint += (
                "。不要改写 query 重试或换检索工具；本轮没有其他可答事实时，用 ask_clarify 请用户选择，"
                f"options 原样使用候选标签；用户确认后以原 query「{text}」原样再查，不要改写"
            )
            if len(resolved_dto.candidates) > CANDIDATE_LIMIT:
                hint += f"；共 {len(resolved_dto.candidates)} 个候选，仅展示前 {CANDIDATE_LIMIT} 个，可补充名称缩小范围"
        return make_envelope(
            status=EnvelopeStatus.AMBIGUOUS,
            facts=candidate_facts,
            hint=hint,
            source=_RESOLVE_SOURCE,
            request_id=resolved_dto.request_id,
            latency_ms=resolved_dto.latency_ms,
        )

    return await _fetch_product_rag(
        text, resolved_dto.standard_name, product_code=resolved_dto.product_code,
        agent_id=agent_id, trace_id=trace_id, budget=budget, backend_client=backend_client,
    )


async def _fetch_product_rag(
    text: str, standard_name: str, *, product_code: str | None, agent_id: str, trace_id: str,
    budget: dict, backend_client: BackendClient | None,
) -> str:
    """解析锁定和有效确认共享同一检索投影，沿用调用方的配置代作用域。"""
    raw = await call_backend(
        PRODUCT_RAG,
        ProductRagRequest(query=text, standard_name=standard_name, product_code=product_code),
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        client=backend_client,
    )
    if (outcome := error_outcome(raw)) is not None:
        return error_envelope(outcome, source=_RAG_SOURCE, raw=raw)
    rag = parse_backend_response(ProductRagResponse, raw, source=_RAG_SOURCE)
    if isinstance(rag, str):
        return rag
    if rag.documents is not None:
        return product_documents_envelope(rag, agent_id=agent_id, source=_RAG_SOURCE)
    std = rag.standard_name
    if not rag.found:
        return make_envelope(
            status=EnvelopeStatus.EMPTY, hint=f"产品「{std}」暂无条款信息",
            source=_RAG_SOURCE, request_id=rag.request_id, latency_ms=rag.latency_ms,
        )

    # 请求维度 = query 命中维度;未指明 = 全维度(缺省整卡口径)
    requested = set(scan_clause_dimensions(text)) or {code for code, _ in _FIELDS}
    version = rag.product_version
    facts: list = []
    evidence: list = []
    for i, (key, locator) in enumerate(_FIELDS):
        value = getattr(rag, key)
        if value is None or str(value) == "":
            continue
        evidence_id = f"E-{rag.request_id}-{i + 1:02d}"
        evidence.append(Evidence(
            evidence_id=evidence_id,
            source_type=SourceType.RAG_DOCUMENT,
            source_id="product-rag",
            document_id=std,
            chunk_id=f"chunk-{i + 1:02d}",
            product_version=version,
            locator=locator,
            retrieval_round=1,
            retrieved_at=datetime.now(timezone.utc),
        ))
        facts.append(to_fact(
            predicate=key,
            typed_value=value,
            subject_id=std,
            subject_type="product",
            source_call_id=rag.request_id,
            identity_scope=agent_id,
            fact_id=f"F-{rag.request_id}-{len(facts) + 1:03d}",
            evidence_refs=[evidence_id],
        ))

    if not facts:
        return make_envelope(
            status=EnvelopeStatus.EMPTY, hint=f"产品「{std}」条款信息为空",
            source=_RAG_SOURCE, request_id=rag.request_id, latency_ms=rag.latency_ms,
        )
    # gaps 只按请求维度披露:query 只问等待期时,未返回免责不算缺口
    missing = [
        locator for key, locator in _FIELDS
        if key in requested and key not in {f.predicate for f in facts}
    ]
    status = EnvelopeStatus.SUCCESS if not missing else EnvelopeStatus.PARTIAL
    return make_envelope(
        status=status,
        facts=facts,
        evidence=evidence,
        gaps=[f"条款维度未返回:{m}" for m in missing] or None,
        source=_RAG_SOURCE,
        request_id=rag.request_id,
        latency_ms=rag.latency_ms,
    )
