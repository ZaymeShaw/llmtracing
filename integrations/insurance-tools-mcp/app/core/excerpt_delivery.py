"""Bounded delivery disclosures beside the immutable evidence pool."""
from __future__ import annotations

import hashlib
import json
from typing import TypeGuard

from app.schemas import Fact, ToolEnvelope
from app.schemas.enums import EnvelopeStatus

_OMITTED = '部分来源说明未完整保留，请结合原始资料核实。'


def fact_binding(fact: Fact) -> str:
    return hashlib.sha256(fact.model_dump_json().encode()).hexdigest()


def _bounded_gaps(gaps: list[str]) -> list[str]:
    unique = list(dict.fromkeys(gaps))
    messages = [g for g in unique if len(g) <= 500][:16]
    return messages if len(messages) == len(unique) else [*messages[:15], _OMITTED]


def _valid_entry(entry: object) -> TypeGuard[dict]:
    return (isinstance(entry, dict) and isinstance(entry.get('binding'), str)
            and isinstance(entry.get('gaps'), list) and len(entry['gaps']) <= 16
            and all(isinstance(g, str) and len(g) <= 500 for g in entry['gaps']))


def record_excerpt_disclosures(state: dict, envelope: ToolEnvelope, trace_id: str) -> None:
    route = state.get('route') or {}
    if not route.get('answer_ladder_enabled') or route.get('trace_id') != trace_id:
        return
    if envelope.status not in {EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL}:
        return
    facts = [f for f in envelope.facts if f.predicate in {'qa_answer', 'reference_content'}]
    if not facts:
        return
    budget = state.setdefault('budget', {})
    scope = [trace_id, route.get('identity_scope'), route.get('session_id')]
    record = budget.get('excerpt_disclosures')
    if (not isinstance(record, dict) or record.get('scope') != scope
            or not isinstance(record.get('facts'), dict) or len(record['facts']) > 64
            or not all(_valid_entry(v) for v in record['facts'].values())):
        record = {'scope': scope, 'facts': {}}
        budget['excerpt_disclosures'] = record
    messages = _bounded_gaps(envelope.gaps)
    entries = record['facts']
    if len(facts) > 64:
        record['truncated'] = True
    for fact in facts[:64]:
        binding = fact_binding(fact)
        old = entries.get(fact.fact_id)
        if old is not None and old['binding'] != binding:
            continue
        candidate = {'binding': binding, 'gaps': _bounded_gaps([*(old['gaps'] if old else []), *messages])}
        updated = {**entries, fact.fact_id: candidate}
        if len(updated) > 64 or len(json.dumps(updated, ensure_ascii=False)) > 16000:
            record['truncated'] = True
            continue
        entries[fact.fact_id] = candidate


def excerpt_disclosures(state: dict, trace_id: str, facts: list[dict]) -> dict[str, tuple[str, ...]]:
    route = state.get('route') or {}
    record = (state.get('budget') or {}).get('excerpt_disclosures') or {}
    if not isinstance(record, dict) or record.get('scope') != [trace_id, route.get('identity_scope'), route.get('session_id')]:
        return {}
    entries = record.get('facts')
    if not isinstance(entries, dict):
        return {}
    result = {}
    for raw in facts[:64]:
        fact = Fact.model_validate(raw)
        entry = entries.get(fact.fact_id)
        if _valid_entry(entry) and entry['binding'] == fact_binding(fact):
            result[fact.fact_id] = tuple(entry['gaps']) + ((_OMITTED,) if record.get('truncated') else ())
        elif entry is not None or record.get('truncated'):
            # A fixed disclosure is safe even when exact metadata cannot be retained.
            result[fact.fact_id] = (_OMITTED,)
    return result
