"""未评分 FAQ 正文投影；不改变原 QA 对评分路径。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.core.retrieval_quality import document_gap_codes, referenced_retrieval_gaps
from app.schemas import EnvelopeStatus, Evidence, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend_contracts import QaRecallResponse


EMPTY_QA_HINT = "问答库未召回相关内容；请勿改写同一问题重试，可说明未找到并建议经人工渠道确认"


def faq_documents_envelope(parsed: QaRecallResponse, *, agent_id: str, source: SourceRef) -> str:
    documents = parsed.documents
    if not documents:
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint=EMPTY_QA_HINT,
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
            evidence_id=evidence_id, source_type=SourceType.QA_PAIR,
            source_id=document.source if document.source is not None else source.system,
            retrieval_gaps=codes or None, locator=f"召回序号:{document.index}" if document.index is not None else None,
            retrieved_at=datetime.now(timezone.utc),
        ))
        facts.append(to_fact(
            predicate="qa_answer", typed_value=document.context,
            subject_id=f"qa-document-{position:02d}", subject_type="qa_pair",
            source_call_id=parsed.request_id, identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{position:03d}", evidence_refs=[evidence_id],
        ))
    return make_envelope(
        status=EnvelopeStatus.PARTIAL, facts=facts, evidence=evidence,
        # 2026-09-18 措辞收敛(检视切片 2):「上游未提供」是工程视角,模型/用户
        # 视角只需知道匹配程度未经评分确认。
        gaps=["召回内容未经评分确认匹配程度，仅供参考", *referenced_retrieval_gaps(facts, evidence)],
        source=source, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
    )
