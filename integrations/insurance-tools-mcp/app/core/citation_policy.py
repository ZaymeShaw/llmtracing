"""CI-02: one scoped citation resolver; local numeric handling with usable recovery."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.core.agent_output import decode_output_content, normalize_fact_refs
from app.core.knowledge_excerpt import ExcerptPolicy, assertion_fragments, CITATION_KINDS
from app.schemas.execution import AgentOutput
from app.schemas import Fact
from app.core.citation_units import citation_units, citation_parts, without_citations

CitationAnomaly = Literal["missing", "unknown_id", "format_error", "mapping_missing"]
NUMERIC_LIMITATION = "部分数值因资料或单位不足暂无法核实，请以原始资料为准。"
EMPTY_EVIDENCE_LIMITATION = "本轮查询未获得可核验资料，相关内容请以原始资料或人工渠道核实为准。"
CITATION_LIMITATION = "部分内容未能在本轮证据中对应，请结合原始资料核实。"


@dataclass(frozen=True)
class CitationDecision:
    output: AgentOutput
    anomalies: tuple[CitationAnomaly, ...] = ()
    fallback: bool = False
    limitation: str | None = None


@dataclass(frozen=True)
class CitationTurnView:
    policy: ExcerptPolicy
    known_ids: set[str]
    shown: dict[str, str] = field(default_factory=dict)
    unconfirmed_ids: set[str] = field(default_factory=set)
    evidence_called: bool = True
    business_data: bool = False
    legacy_guard: bool = False
    business_facts: list[Fact] = field(default_factory=list)
    # Bounded counters only; callers may persist for offline calibration.
    numeric_shadow: dict[str, int] = field(default_factory=dict)
    numeric_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)


_REF = re.compile(r'\[[^\]\n]*\]')
_SUFFIX = re.compile(r'(?:\[[^\]\n]*\]\s*)+[。！？!?.，,；;\s]*$')
_DEPENDENT = re.compile(r'^[\s#*+\->]*(?:但(?:是)?|不过|然而|此外|另外|同时|因此|所以|综上|也就是说|也请|并且|而且)')


def resolve_citations(output: AgentOutput, turn_view: CitationTurnView) -> CitationDecision:
    """One clause-level decision, shared by numbered and transitional legacy references.

    Untraceable numeric units are removed locally; incomplete sources retain
    usable output with a limitation. Rollback retains the legacy contract.
    """
    view = turn_view
    def fail(reason: CitationAnomaly) -> CitationDecision:
        return CitationDecision(output.model_copy(update={'answer_draft': '', 'fact_refs': []}), (reason,), True)

    legacy = list(dict.fromkeys(ref for ref in output.fact_refs if ref in view.known_ids))
    unknown = set(output.fact_refs) - view.known_ids
    if view.legacy_guard:
        # Rollback keeps the legacy answer-level contract within this same entry.
        # It does not activate the new clause scanner or numbered reference rules.
        if unknown & view.unconfirmed_ids:
            if legacy:
                recovered = output.model_copy(update={
                    'answer_draft': '', 'fact_refs': legacy,
                    'gaps': list(dict.fromkeys([*output.gaps, CITATION_LIMITATION])),
                })
                return CitationDecision(recovered, ('unknown_id',), False, CITATION_LIMITATION)
            return fail('unknown_id')
        requires_refs = view.evidence_called and not view.business_data and not output.needs_user_input
        missing = requires_refs and not legacy and bool(view.known_ids)
        limitation = (CITATION_LIMITATION if unknown or missing else
                      EMPTY_EVIDENCE_LIMITATION if requires_refs and not view.known_ids else None)
        return CitationDecision(output.model_copy(update={
            'fact_refs': legacy,
            'gaps': list(dict.fromkeys([*output.gaps, *([limitation] if limitation else [])])),
        }), ('unknown_id',) if unknown else ('missing',) if missing else (), False, limitation)
    if len(output.answer_draft) > view.policy.max_scan_chars:
        return fail('mapping_missing')
    factual = bool(unknown) and bool(assertion_fragments(output.answer_draft, view.policy, kinds=CITATION_KINDS))
    # Legacy answer-level refs cannot localize a forged claim to a sentence.
    if unknown and (not legacy or factual or unknown & view.unconfirmed_ids):
        if view.business_facts:
            # Recover from obsolete/forged legacy IDs with deterministic displayed
            # business values. Never retain the model's unlocalized claim.
            recovered = output.model_copy(update={
                'answer_draft': '', 'fact_refs': list(dict.fromkeys(f.fact_id for f in view.business_facts)),
                'gaps': list(dict.fromkeys([*output.gaps, CITATION_LIMITATION])),
            })
            return CitationDecision(recovered, ('unknown_id',), False, CITATION_LIMITATION)
        return fail('unknown_id')
    anomalies: list[CitationAnomaly] = ['unknown_id'] if unknown else []
    numbered = bool(_REF.search(output.answer_draft))
    used = [] if numbered else legacy.copy()
    kept: list[str] = []
    removed_previous = False
    removed = False
    supported_business = False
    numeric_uncertain = False
    business_removed = False
    from app.core.numeric_support import NumericSupportIndex
    numeric_index = None
    if view.business_data:
        try:
            numeric_index = NumericSupportIndex.build(view.business_facts, view.numeric_aliases,
                                                      max_facts=view.policy.max_entries)
        except Exception:
            pass  # Recovery below discloses uncertainty without failing the request.
    for unit in citation_units(output.answer_draft):
        refs = _REF.findall(unit)
        clean = without_citations(unit)
        reason: CitationAnomaly | None = None
        if '[' in clean or ']' in clean or (refs and ('`' in unit or '](' in unit)):
            reason = 'mapping_missing'
        ids = []
        for ref in refs:
            identity = view.shown.get(ref)
            if not re.fullmatch(r'\[[1-9][0-9]{0,8}\]', ref) or identity not in view.known_ids:
                reason = 'mapping_missing'
            else:
                ids.append(identity)
        local_business = False
        for part in citation_parts(unit):
            part_refs = _REF.findall(part)
            suffix = _SUFFIX.search(part)
            if part_refs and (suffix is None or _REF.findall(suffix.group()) != part_refs):
                reason = 'mapping_missing'
            unnumbered = not part_refs and not (legacy and not numbered)
            claim = without_citations(part)
            inspect_claim = (unnumbered or (view.business_data and not part_refs)) and not output.needs_user_input
            fragments = assertion_fragments(claim, view.policy, kinds=CITATION_KINDS) if inspect_claim else ()
            if fragments:
                if view.business_data:
                    try:
                        key = numeric_index.classify(claim) if numeric_index else 'unverifiable'
                    except Exception:
                        key = 'unverifiable'
                    view.numeric_shadow[key] = view.numeric_shadow.get(key, 0) + 1
                    if key == 'unsupported':
                        reason = reason or 'missing'
                        business_removed = True
                    elif key == 'unverifiable':
                        numeric_uncertain = True
                        local_business = True
                    elif key == 'supported':
                        local_business = True
                    else:
                        # A nonnumeric business assertion needs its own citation.
                        # Pure numeric vocabulary without a parsed value is uncertain.
                        if any(f.kind == 'assertion' for f in fragments) and not any(f.kind == 'quantity' for f in fragments):
                            reason = reason or 'missing'
                            business_removed = True
                        else:
                            numeric_uncertain = True
                            local_business = True
                elif view.evidence_called and reason is None:
                    reason = 'missing'
        if removed_previous and _DEPENDENT.match(clean):
            removed = True
            continue
        if reason:
            if reason not in anomalies:
                anomalies.append(reason)
            removed_previous = removed = True
            continue
        if clean.strip():
            removed_previous = False
        kept.append(clean)
        used.extend(ids)
        supported_business |= local_business
    if not any(text.strip() for text in kept) or (removed and not used and not supported_business):
        if business_removed and view.business_facts:
            recovered = output.model_copy(update={
                'answer_draft': '',
                'fact_refs': list(dict.fromkeys(f.fact_id for f in view.business_facts if f.fact_id in view.known_ids)),
                'gaps': list(dict.fromkeys([*output.gaps, CITATION_LIMITATION])),
            })
            if recovered.fact_refs:
                return CitationDecision(recovered, tuple(anomalies), False, CITATION_LIMITATION)
        return fail(anomalies[0] if anomalies else 'mapping_missing')
    limitation = CITATION_LIMITATION if anomalies else NUMERIC_LIMITATION if numeric_uncertain else None
    draft = ''.join(kept)
    if removed:
        draft = re.sub(r'[，,；;](?=[ \t]*(?:\n|$))', '。', draft)
    candidate = output.model_copy(update={
        'answer_draft': draft, 'fact_refs': list(dict.fromkeys(used)),
        'gaps': [*output.gaps, *([limitation] if limitation and limitation not in output.gaps else [])],
    })
    return CitationDecision(candidate, tuple(anomalies), False, limitation)


def citation_decision(output: AgentOutput, known_ids: set[str], unconfirmed_ids: set[str],
                      policy: ExcerptPolicy, *, business_data: bool = False) -> CitationDecision:
    """Transitional API for callers without a live turn; not a second validation pass."""
    return resolve_citations(output, CitationTurnView(policy, known_ids, unconfirmed_ids=unconfirmed_ids,
                                                     business_data=business_data))


def malformed_fact_refs(content: object) -> bool:
    """Inspect the same decoded object/ID normalization as the tolerant bridge.

    Blank IDs, non-string items and non-list containers remain format errors.
    No evidence is inferred from a structurally valid identifier.
    """
    candidate = decode_output_content(content)
    if isinstance(candidate, AgentOutput):
        refs: object = candidate.fact_refs
    elif isinstance(candidate, dict) and "fact_refs" in candidate:
        refs = normalize_fact_refs(candidate["fact_refs"])
    else:
        return False
    return not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
