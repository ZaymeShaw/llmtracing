"""产品召回正文投影；不把非结构化知识误报为完整条款。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.core.retrieval_quality import document_gap_codes, referenced_retrieval_gaps
from app.schemas import EnvelopeStatus, Evidence, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend_contracts import ProductRagResponse


def product_documents_envelope(parsed: ProductRagResponse, *, agent_id: str, source: SourceRef) -> str:
    if not parsed.documents:
        return make_envelope(
            status=EnvelopeStatus.EMPTY, hint=f"产品「{parsed.standard_name}」未召回相关内容",
            source=source, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )
    facts, evidence = [], []
    diagnostics = parsed.document_diagnostics
    positions = diagnostics.positions if diagnostics else list(range(1, len(parsed.documents) + 1))
    for position, document in zip(positions, parsed.documents):
        codes = document_gap_codes(source=document.source, index=document.index,
                                   discarded_count=diagnostics.discarded_count if diagnostics else 0)
        evidence_id = f"E-{parsed.request_id}-{position:02d}"
        evidence.append(Evidence(
            evidence_id=evidence_id, source_type=SourceType.RAG_DOCUMENT,
            source_id=document.source if document.source is not None else source.system,
            retrieval_gaps=codes or None, retrieval_index=document.index,
            retrieved_at=datetime.now(timezone.utc),
        ))
        facts.append(to_fact(
            predicate="reference_content", typed_value=document.context,
            subject_id=f"product-document-{position:02d}", subject_type="product_document",
            source_call_id=parsed.request_id, identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{position:03d}", evidence_refs=[evidence_id],
        ))
    return make_envelope(
        status=EnvelopeStatus.PARTIAL, facts=facts, evidence=evidence,
        hint=f"产品「{parsed.standard_name}」相关知识仅供参考",
        # 2026-09-18 收敛为单条(检视切片 2):多条工程披露并列会被模型按
        # 「gaps=未能确认的缺口」解读为取证未完成,诱发换问法/换工具重复检索。
        # 模型纪律语义(维度覆盖未确认/正文归属不保证)保留;评分/版本类纯工程
        # 披露并入「未经确认」表述。PARTIAL 必披露实质 gaps 的契约不变。
        gaps=["召回正文未经评分与版本定位，所问条款维度的覆盖未经确认；检索按产品名称提示、无产品 ID 级过滤，不能保证正文全部属于该产品，相关内容仅供参考", *referenced_retrieval_gaps(facts, evidence)],
        source=source, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
    )
