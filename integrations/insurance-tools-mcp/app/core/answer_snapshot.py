"""Immutable, trace-bound L2 snapshots. Pure memory projection, never model/IO calls.

Consumers read current request configuration and revalidate the current bucket;
no policy or excerpt cache is persisted in session state.
"""
from __future__ import annotations

from typing import Literal
import math

from app.core.render import ExcerptAttribution, excerpt_attribution, render_excerpts
from app.core.retrieval_quality import referenced_retrieval_gaps
from app.core.excerpt_delivery import excerpt_disclosures

from pydantic import BaseModel, ConfigDict

from app.core.answer_ladder import LadderMaterials
from app.core.knowledge_excerpt import (
    ExcerptPolicy, KnowledgeCategory, is_general_query, knowledge_category, matching_topics,
)
from app.schemas import Evidence, Fact
from app.schemas.enums import SourceType


class QueryBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trace_id: str
    query: str
    identity_scope: str


class Excerpt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fact_id: str
    source_call_id: str
    text: str
    evidence_refs: tuple[str, ...]
    subject_type: str
    subject_id: str
    product_versions: tuple[str, ...]
    as_of: str | None
    knowledge_category: KnowledgeCategory
    question: str | None = None
    source: str = "知识资料"
    gaps: tuple[str, ...] = ()


class AnswerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    binding: QueryBinding
    confirmed: bool
    completeness: Literal["partial"] = "partial"
    excerpts: tuple[Excerpt, ...] = ()
    # Reason codes only: rejected source text never enters public limitations/gaps.
    rejection_reasons: tuple[str, ...] = ()

    def materials(self) -> LadderMaterials | None:
        if not self.excerpts:
            return None
        limitations = ["仅提供相关原文摘录，尚未完成全部问题的核实；请结合来源确认。"]
        if not self.confirmed:
            limitations.append("本轮处理未完成，以上材料尚未确认。")
        if self.rejection_reasons:
            limitations.append("部分内容未包含。")
        limitations.extend(gap for entry in self.excerpts for gap in entry.gaps)
        limitations = list(dict.fromkeys(limitations))
        facts = [Fact(fact_id=e.fact_id, subject_type=e.subject_type, subject_id=e.subject_id,
                      predicate="qa_answer", typed_value=e.text, source_call_id=e.source_call_id,
                      identity_scope=self.binding.identity_scope) for e in self.excerpts]
        return LadderMaterials(
            body=render_excerpts(facts, {e.fact_id: ExcerptAttribution(e.question, e.source) for e in self.excerpts},
                                 gaps=limitations, confirmed=self.confirmed),
            fact_refs=tuple(entry.fact_id for entry in self.excerpts), limitations=tuple(limitations),
        )


def build_snapshot(
    bucket: dict, binding: QueryBinding, policy: ExcerptPolicy,
    *, success_min: float = 0.75, gaps_by_fact: dict[str, tuple[str, ...]] | None = None,
) -> AnswerSnapshot:
    """Keep whole entries only (<=300 chars, <=2 paragraphs total), never slice qualifiers.

    Only general queries and general-knowledge facts qualify. Product-specific
    binding is not supported by this internal projection.
    """
    confirmed = bucket.get("confirmed", True) is True
    rejected: list[str] = []
    selected: list[Excerpt] = []
    paragraphs = 0
    if len(binding.query) > policy.max_scan_chars:
        return AnswerSnapshot(binding=binding, confirmed=confirmed, rejection_reasons=("query_limit",))
    topics = matching_topics(binding.query, policy)
    if not binding.trace_id or not binding.query or not binding.identity_scope:
        return AnswerSnapshot(binding=binding, confirmed=confirmed, rejection_reasons=("missing_binding",))
    if not topics:
        return AnswerSnapshot(binding=binding, confirmed=confirmed, rejection_reasons=("unknown_topic",))
    if not is_general_query(binding.query, policy):
        return AnswerSnapshot(binding=binding, confirmed=confirmed, rejection_reasons=("unverified_subject",))
    raw_facts, raw_evidence = bucket.get("facts", []), bucket.get("evidence", [])
    if len(raw_facts) > policy.max_entries or len(raw_evidence) > policy.max_entries:
        return AnswerSnapshot(binding=binding, confirmed=confirmed, rejection_reasons=("pool_limit",))
    if sum(len(str(item.get("typed_value", ""))) for item in raw_facts) > policy.max_scan_chars:
        return AnswerSnapshot(binding=binding, confirmed=confirmed, rejection_reasons=("pool_text_limit",))
    evidence = {item.evidence_id: item for raw in raw_evidence if (item := Evidence.model_validate(raw))}
    for raw in raw_facts:
        fact = Fact.model_validate(raw)
        text = str(fact.typed_value).strip()
        category = knowledge_category(fact.predicate, text, policy)
        reason = ""
        refs = [evidence[ref] for ref in fact.evidence_refs if ref in evidence]
        entry_paragraphs = len(text.split("\n\n"))
        if fact.identity_scope != binding.identity_scope:
            reason = "identity_mismatch"
        elif category is not KnowledgeCategory.GENERAL:
            reason = "category_not_allowed"
        elif not refs or len(refs) != len(fact.evidence_refs) or any(
            not item.source_id.strip() or (item.retrieval_gaps and "missing_source" in item.retrieval_gaps) for item in refs
        ):
            reason = "missing_source"
        elif not matching_topics(text, policy) or not set(matching_topics(text, policy)).issubset(topics):
            reason = "topic_mismatch"
        elif any(item.source_type is not SourceType.QA_PAIR or item.rerank_score is None or not math.isfinite(item.rerank_score)
                 or not success_min <= item.rerank_score <= 1 for item in refs):
            reason = "insufficient_score"
        elif any(not item.locator or item.locator.startswith("召回序号:") for item in refs):
            reason = "missing_original_question"
        elif any(not matching_topics(item.locator or "", policy)
                 or not set(matching_topics(item.locator or "", policy)).issubset(topics) for item in refs):
            reason = "question_mismatch"
        elif any(not is_general_query(item.locator or "", policy) for item in refs):
            reason = "question_subject_mismatch"
        elif fact.subject_type not in {"qa_pair", "general_knowledge"}:
            reason = "subject_dependent"
        elif not text or len(text) > 300 or text[-1] not in "。！？.!?" or entry_paragraphs > 2:
            reason = "incomplete_unit"
        elif paragraphs + entry_paragraphs > 2:
            reason = "excerpt_limit"
        if reason:
            rejected.append(reason)
            continue
        attribution = excerpt_attribution(refs)
        selected.append(Excerpt(
            fact_id=fact.fact_id, source_call_id=fact.source_call_id, text=text, evidence_refs=tuple(fact.evidence_refs),
            subject_type=fact.subject_type, subject_id=fact.subject_id,
            product_versions=tuple(item.product_version for item in refs if item.product_version),
            as_of=fact.as_of, knowledge_category=category, question=attribution.question, source=attribution.source,
            gaps=tuple(dict.fromkeys([*(gaps_by_fact or {}).get(fact.fact_id, ()),
                                     *referenced_retrieval_gaps([fact], refs)])),
        ))
        paragraphs += entry_paragraphs
    return AnswerSnapshot(binding=binding, confirmed=confirmed, excerpts=tuple(selected), rejection_reasons=tuple(rejected))


def current_snapshot(state: dict, trace_id: str) -> AnswerSnapshot | None:
    """Build failure material in memory on demand; never persist excerpts or a snapshot cache."""
    route = state.get("route") or {}
    if not isinstance(route, dict) or route.get("trace_id") != trace_id:
        return None
    # Legacy persisted caches are untrusted and removed when encountered.
    route.pop("best_effort", None)
    route.pop("best_effort_error", None)
    route.pop("excerpt_config", None)
    try:
        bucket = state.get("evidence", {}).get("by_turn", {}).get(trace_id, {})
        binding = QueryBinding(trace_id=trace_id, query=route.get("user_text", ""), identity_scope=route.get("identity_scope", ""))
        if isinstance(bucket, dict) and isinstance(bucket.get("facts", []), list) and not bucket.get("facts"):
            # No evidence can produce L2. Do not load tool/config dictionaries
            # while a failure answer or its hooks are consuming the last budget.
            route["l2_eligible"] = False
            route["snapshot_rejections"] = ["no_material"]
            return AnswerSnapshot(binding=binding, confirmed=bucket.get("confirmed", True) is True,
                                  rejection_reasons=("no_material",))
        from app.conf import harness_conf, tools_conf
        config = harness_conf()
        snapshot = build_snapshot(
            bucket, binding,
            config.excerpt_policy,
            success_min=tools_conf().qa_score.success_min,
            gaps_by_fact=excerpt_disclosures(state, trace_id, bucket.get("facts", [])),
        )
        route["l2_eligible"] = bool(snapshot.excerpts)
        route["snapshot_rejections"] = list(snapshot.rejection_reasons)
        return snapshot
    except Exception:
        # Only reason codes survive; malformed material cannot interrupt failure delivery.
        route["l2_eligible"] = False
        route["snapshot_rejections"] = ["invalid_snapshot_input"]
        return None
