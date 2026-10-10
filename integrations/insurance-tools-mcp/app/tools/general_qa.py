"""general_qa 工具核心(§10.1 存量·接口 5:通用问答兜底,QA 对语义召回)。

Owner 2026-08-30 口径:入参为标准、完整的问题(指代消解后的自足 query),返回最
相近的 n 个 QA 对供参考回答。召回置信度 → 信封 status 映射(§10.7):高分
SUCCESS / 中分 PARTIAL(披露置信缺口)/ 低分或零召回 EMPTY(明确告知 + 替代路径,
不得编造,L3)。QA 对映射为 Fact + Evidence(qa_pair,检索分),供答案溯源引用。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Evidence, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend import BackendClient, call_backend, error_envelope, error_outcome
from app.tools.backend_contracts import QaRecallResponse, parse_backend_response
from app.tools.mode import execution_tools_conf, require_tool_execution_context, tool_execution_scope
from app.tools.real.knowledge import EMPTY_QA_HINT, faq_documents_envelope
from tool_adapter_sdk.contracts import QA_RECALL, QaRecallRequest

IMPLEMENTED = True

_SOURCE = SourceRef(system="qa_svc", endpoint="/qa/recall")


@tool_execution_scope("general_qa")
async def general_qa(
    query: str,
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
) -> str:
    """通用问答召回(入参 = 标准、完整 query,Owner 2026-09-01),返回 str JSON 信封。"""
    text = query.strip()
    if not text:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供问题")

    conf = execution_tools_conf()
    execution = require_tool_execution_context()
    raw = await call_backend(
        QA_RECALL,
        QaRecallRequest(query=text, top_n=execution.tool_config.recall_top_n or 3),
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        client=backend_client,
    )
    if (outcome := error_outcome(raw)) is not None:
        return error_envelope(outcome, source=_SOURCE, raw=raw)

    parsed = parse_backend_response(QaRecallResponse, raw, source=_SOURCE)
    if isinstance(parsed, str):
        return parsed
    if parsed.documents is not None:
        return faq_documents_envelope(parsed, agent_id=agent_id, source=_SOURCE)
    pairs = parsed.pairs
    if not pairs:
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint=EMPTY_QA_HINT,
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )

    answered_pairs = [pair for pair in pairs if pair.answer.strip()]
    if not answered_pairs:
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint="问答库未召回可参考内容；请勿改写同一问题重试，可说明未找到并建议经人工渠道确认",
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )

    top = max(pair.score for pair in answered_pairs)
    if top < conf.qa_score.partial_min:
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint=f"召回相关度过低(最高 {top:.2f})；请勿改写同一问题重试，可说明未找到并建议经人工渠道确认",
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )

    # 只把达到最低可用置信度的 QA 对交给上层 Agent。后端 top_n 常会补齐
    # 低分候选；若原样透传，模型可能把无关退保/保贷内容混入理赔等答案。
    usable_pairs = [
        pair for pair in answered_pairs
        if pair.score >= conf.qa_score.partial_min
    ]
    facts: list = []
    evidence: list = []
    for i, pair in enumerate(usable_pairs):
        answer = pair.answer.strip()
        question_text = pair.question.strip()
        score = pair.score
        if not answer:
            continue
        evidence_id = f"E-{parsed.request_id}-{i + 1:02d}"
        evidence.append(Evidence(
            evidence_id=evidence_id,
            source_type=SourceType.QA_PAIR,
            source_id="qa-rag",
            locator=question_text or None,  # 原问题作为文内定位(答案检索自该 QA 对)
            rerank_score=score,
            retrieval_round=1,
            retrieved_at=datetime.now(timezone.utc),
        ))
        facts.append(to_fact(
            predicate="qa_answer",
            typed_value=answer,
            subject_id=f"qa-pair-{i + 1:02d}",  # 本次响应内的对标识，不代表上游持久 QA ID
            subject_type="qa_pair",
            source_call_id=parsed.request_id,
            identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
            evidence_refs=[evidence_id],
        ))

    if top >= conf.qa_score.success_min:
        status = EnvelopeStatus.SUCCESS
        gaps = None
    elif top >= conf.qa_score.partial_min:
        status = EnvelopeStatus.PARTIAL
        gaps = [f"召回置信度中等(最高相关度 {top:.2f}),答案请以官方口径为准"]
    return make_envelope(
        status=status,
        facts=facts,
        evidence=evidence,
        gaps=gaps,
        source=_SOURCE,
        request_id=parsed.request_id,
        latency_ms=parsed.latency_ms,
    )
