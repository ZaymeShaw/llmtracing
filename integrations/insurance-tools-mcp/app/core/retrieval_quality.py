"""检索质量的固定披露，不从模型文本或业务正文推测缺口。"""
from __future__ import annotations

from collections.abc import Iterable

from app.schemas import Evidence, Fact
from app.schemas.enums import SourceType
from app.schemas.evidence import RetrievalGap

RETRIEVAL_GAP_TEXT: dict[RetrievalGap, str] = {
    "missing_source": "部分参考内容未提供文档来源，证据仅能指向检索服务，无法核验具体出处。",
    "missing_index": "部分参考内容未提供上游召回序号，无法按该序号精确定位。",
    "discarded_documents": "部分召回条目格式异常，已略过；当前仅保留可用内容，结果可能不完整。",
}


def document_gap_codes(*, source: str | None, index: int | str | None, discarded_count: int) -> list[RetrievalGap]:
    codes: list[RetrievalGap] = []
    if source is None:
        codes.append("missing_source")
    if index is None:
        codes.append("missing_index")
    if discarded_count:
        codes.append("discarded_documents")
    return codes


def referenced_retrieval_gaps(facts: Iterable[Fact], evidence: Iterable[Evidence]) -> list[str]:
    """只跟随实际交付知识事实的引用，忽略未引用或业务接口证据。"""
    refs = {ref for fact in facts if fact.predicate in {"qa_answer", "reference_content"}
            for ref in fact.evidence_refs}
    messages = [RETRIEVAL_GAP_TEXT[code] for item in evidence
                if item.evidence_id in refs and item.source_type in {SourceType.QA_PAIR, SourceType.RAG_DOCUMENT}
                for code in item.retrieval_gaps or []]
    return list(dict.fromkeys(messages))
