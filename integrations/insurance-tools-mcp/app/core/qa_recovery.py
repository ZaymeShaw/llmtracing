"""Whole current-turn QA excerpts within a total delivery budget; no source mutation."""
from app.schemas import Fact, Evidence
from app.core.knowledge_excerpt import ExcerptPolicy

QA_RECOVERY_CHARS = 1500


def qa_recovery(facts: list[Fact], evidence: list[Evidence] | None = None, *, policy: ExcerptPolicy | None = None, query: str = "") -> tuple[list[Fact], list[str]]:
    from app.core.render import render_rag_facts
    selected: list[Fact] = []
    used = 0
    omitted = False
    seen: set[str] = set()
    tokens = {query[i:i + 2] for i in range(len(query) - 1)}
    ranked = sorted(facts, key=lambda f: sum(t in str(f.typed_value) for t in tokens), reverse=True)
    for fact in ranked:
        if fact.predicate not in {'qa_answer', 'reference_content'} or not isinstance(fact.typed_value, str):
            continue
        text = fact.typed_value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        size = len(render_rag_facts([fact], {}, evidence=evidence or [], confirmed=False)) + 2
        if len(selected) >= 2:
            omitted = True
            continue
        if used + size > QA_RECOVERY_CHARS:
            omitted = True
            allowance = QA_RECOVERY_CHARS - used - (size - len(text)) - 10
            if allowance < 80:
                continue
            excerpt = text[:allowance]
            boundary = max(excerpt.rfind('。'), excerpt.rfind('；'), excerpt.rfind('\n'))
            if boundary >= allowance // 2:
                excerpt = excerpt[:boundary + 1]
            fact = fact.model_copy(update={'typed_value': excerpt + '…（节选）'})
            size = len(render_rag_facts([fact], {}, evidence=evidence or [], confirmed=False)) + 2
        used += size
        selected.append(fact)
    gaps = []
    if selected:
        gaps.append('同轮知识仅提供来源摘录，尚未完成整理核实。')
    if omitted:
        gaps.append('部分同轮知识原文超出本次摘录总量，未完整展示，可继续提问。')
    return selected, gaps
