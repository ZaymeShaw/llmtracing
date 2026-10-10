"""权益检索正文投影，不改变原结构化五元组和客户已享权益职责。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.core.retrieval_quality import document_gap_codes, referenced_retrieval_gaps
from app.schemas import EnvelopeStatus, Evidence, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend_contracts import BenefitDetailResponse


def benefit_documents_envelope(parsed: BenefitDetailResponse, *, agent_id: str, source: SourceRef) -> str:
    documents = parsed.documents
    if not documents:
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint="未召回相关权益/服务内容，请补充问题或经人工渠道确认",
            source=source, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )
    facts, evidence = [], []
    diagnostics = parsed.document_diagnostics
    positions = diagnostics.positions if diagnostics else list(range(1, len(documents) + 1))
    for position, document in zip(positions, documents):
        codes = document_gap_codes(source=document.source, index=document.index,
                                   discarded_count=diagnostics.discarded_count if diagnostics else 0)
        evidence_id = f"E-{parsed.request_id}-{position:02d}"
        evidence.append(Evidence(
            evidence_id=evidence_id, source_type=SourceType.RAG_DOCUMENT,
            source_id=document.source if document.source is not None else source.system,
            retrieval_gaps=codes or None, retrieval_index=document.index, source_url=document.url,
            raw_score=document.score, recall_method=document.recall_method,
            retrieved_at=datetime.now(timezone.utc),
        ))
        facts.append(to_fact(
            predicate="reference_content", typed_value=document.context,
            subject_id=f"benefit-document-{position:02d}", subject_type="benefit_document",
            source_call_id=parsed.request_id, identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{position:03d}", evidence_refs=[evidence_id],
        ))
    return make_envelope(
        status=EnvelopeStatus.PARTIAL, facts=facts, evidence=evidence,
        # 2026-09-18 收敛为单条(检视切片 2,与 product/general_qa 同款):模型纪律
        # 语义(覆盖与要素未确认/不判资格)保留,纯工程披露并入「未经评分标定」。
        gaps=["召回正文未经评分标定，无法确认所涉权益已全覆盖或要素完整，仅供参考；不得据此判断客户是否已享有或具备资格", *referenced_retrieval_gaps(facts, evidence)],
        source=source, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
    )
