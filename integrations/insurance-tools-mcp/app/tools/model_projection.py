"""Request-scoped model view and citation mapping. No IO or evidence mutation."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from app.core.citation_policy import CitationDecision
from app.core.knowledge_excerpt import ExcerptPolicy
from app.core.evidence_pool import collect_pool
from app.schemas import Fact
from app.schemas import ToolEnvelope
from app.core.harness_config import HarnessConf
from app.tools.tool_sets import RAG_TOOLS
from app.schemas.execution import AgentOutput
from app.tools.output_shaper import preview_output


def enabled() -> bool:
    return True


def _scope(state: dict) -> list[str]:
    route = state.get('route') or {}
    values = [route.get(key) for key in ('session_id', 'trace_id', 'identity_scope')]
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError('Missing projection scope')
    return [str(value) for value in values]


def _fingerprint(fact: Fact) -> str:
    return hashlib.sha256(fact.model_dump_json(exclude={'evidence_refs'}).encode()).hexdigest()


def _visible_fact(raw: dict, records: list) -> bool:
    """Keep matching scalar facts; whole-row facts duplicate the authoritative data records."""
    value = raw.get('typed_value')
    predicate = str(raw.get('predicate') or '')
    if predicate.endswith('_search_record') or predicate in {'customer_detail_record', 'policy_risk_record'}:
        return False  # 整行业务数据已有 data，不再次附加编号 JSON 副本。
    from tool_adapter_sdk.policy_detail_metadata import SOURCE_ALIASES
    from app.tools.policy_detail_presentation import _path_scalars
    code = {'premium': 'year_pay_prem', 'product_name': 'pol_product_name', 'policy_status': 'pol_status_desc',
            'insured_name': 'insurant_name'}.get(predicate, SOURCE_ALIASES.get(predicate, predicate))
    for record in records:
        if not isinstance(record, dict):
            continue
        subject = record.get('polno') or record.get('subject_id') or record.get('clientMainId')
        if subject == raw.get('subject_id') and value in _path_scalars(record, code.split('.')):
            return True
        if (record.get('subject_id') == raw.get('subject_id')
                and record.get('field') == raw.get('predicate') and record.get('value') == value):
            return True
        if isinstance(value, dict) and value and all(key in record and record[key] == item for key, item in value.items()):
            return True
    return False


def project_model_output(tool: str, visible: str, state: dict | None, *,
                         source: str = 'tool', raw_chars: int | None = None, original: str | None = None,
                         query: str = '', max_chars: int | None = None,
                         config: HarnessConf | None = None, envelope: ToolEnvelope | None = None) -> str:
    """Internal model projection and reference publication; originals remain authoritative.

    State contains bounded, JSON-safe identifiers/digests only. No await occurs
    between allocation and publication, including for parallel async tools. Failures
    publish a bounded disclosure, never an unbounded original or invisible reference.
    """
    from app.conf import harness_conf
    from app.conf.runtime import read_once
    config = config or read_once('harness', harness_conf)
    try:
        policy = config.output_shaping
        if max_chars is not None:
            policy = policy.model_copy(update={'max_chars': max_chars, 'item_chars': min(policy.item_chars, max_chars // 2)})
        if state is None:
            return preview_output(tool, visible, policy, query=query, numbered_references=config.features.citation_l2).text
        try:
            scope = _scope(state)
        except ValueError:
            # Stateless consumers still receive a usable bounded view, without authorization state.
            return preview_output(tool, visible, policy, query=query, numbered_references=config.features.citation_l2).text
        budget = state.setdefault('budget', {})
        mapping = budget.get('model_projection')
        if not isinstance(mapping, dict) or mapping.get('scope') != scope:
            mapping = {'scope': scope, 'allocations': {}, 'shown': {}, 'legacy': {}, 'business': [], 'calls': 0}
        mapping = deepcopy(mapping)
        if max_chars is None and mapping['calls'] >= policy.max_records:
            return '{"result":{},"projection":{"incomplete":true,"reason":"视图容量已耗尽，原始工具结果保留，不能推断无数据"}}'
        if max_chars is None:
            mapping['calls'] += 1
        previously_visible_business = bool(mapping.get('business_data'))
        parsed = json.loads(visible)
        from app.tools.model_output import BUSINESS_TOOL_NAMES, canonical_business_tool_name
        business = canonical_business_tool_name(tool) in BUSINESS_TOOL_NAMES
        if business or tool in RAG_TOOLS or tool == 'run_policy_snapshot':
            mapping['evidence_called'] = True
        # Only values surviving the existing business projection can authorize legacy refs.
        facts = parsed.get('facts', []) if isinstance(parsed, dict) else []
        if business and original and isinstance(parsed, dict):
            raw = envelope.model_dump(mode='json') if envelope is not None else json.loads(original)
            records = (parsed.get('data') or {}).get('records', [])
            facts = [fact for fact in raw.get('facts', []) if _visible_fact(fact, records)]
            mapping['business_data'] = bool(records) or mapping.get('business_data', False)
        current = {}
        facts = facts[:policy.max_references] if isinstance(facts, list) else []
        for raw in facts if isinstance(facts, list) else []:
            try:
                fact = Fact.model_validate(raw)
            except ValueError:
                continue
            if fact.identity_scope == scope[2]:
                digest = _fingerprint(fact)
                current[(fact.source_call_id, fact.fact_id)] = digest
                legacy_binding = {'fact_id': fact.fact_id, 'source_call_id': fact.source_call_id, 'fingerprint': digest}
                previous = mapping['legacy'].get(fact.fact_id)
                if previous and previous != legacy_binding:
                    previous['conflict'] = True
                elif len(mapping['legacy']) < policy.max_references:
                    mapping['legacy'][fact.fact_id] = legacy_binding
                if business and fact.fact_id not in mapping['business']:
                    mapping['business'].append(fact.fact_id)
        allocations = dict(mapping['allocations'])
        candidate = visible
        if business and facts:
            candidate = json.dumps({**parsed, 'facts': facts}, ensure_ascii=False)
        preview = preview_output(tool, candidate, policy, allocations, live=True, query=query, numbered_references=config.features.citation_l2)
        # Only publish numeric references for complete facts retained by the projection.
        shown = {key: dict(value) for key, value in mapping['shown'].items()}
        for entry in shown.values():
            shown_digest = current.get((entry['source_call_id'], entry['fact_id']))
            if shown_digest and shown_digest != entry['fingerprint']:
                entry['conflict'] = True
        for binding in preview.references:
            shown_digest = current.get((binding.source_call_id, binding.fact_id))
            if shown_digest and binding.ref not in shown:
                shown[binding.ref] = {'fact_id': binding.fact_id, 'source_call_id': binding.source_call_id,
                                      'fingerprint': shown_digest}
        # fact_id authorizes legacy citations even when numeric references are disabled.
        projected = json.loads(preview.text)
        retained = ({binding.fact_id for binding in preview.references} if config.features.citation_l2
                    else {fact.get('fact_id') for fact in projected.get('result', {}).get('facts', [])
                          if isinstance(fact, dict)})
        for _, fact_id in current:
            if fact_id not in retained:
                mapping['legacy'].pop(fact_id, None)
        if business:
            projected = json.loads(preview.text)
            result = projected.get('result', projected) if isinstance(projected, dict) else {}
            data = result.get('data') or {}
            mapping['business_data'] = (previously_visible_business or bool(preview.references)
                                        or bool(data.get('records')))
        mapping.update(allocations=allocations, shown=shown)
        budget['model_projection'] = mapping
        return preview.text
    except Exception:
        # Original evidence and cards are already retained before this optional step.
        # A projection failure must not restore an oversized model input.
        return '{"result":{},"projection":{"incomplete":true,"reason":"模型视图生成失败，原始结果保留，不能确认省略内容"}}'


def turn_view(state: dict, policy: ExcerptPolicy):
    from app.core.citation_policy import CitationTurnView
    try:
        scope = _scope(state)
    except ValueError:
        return CitationTurnView(policy=policy, known_ids=set())
    budget = state.get('budget') or {}
    mapping = budget.get('model_projection') or {}
    current = mapping if mapping.get('scope') == scope else {}
    by_turn = (state.get('evidence') or {}).get('by_turn') or {}
    facts, _ = collect_pool({scope[1]: by_turn.get(scope[1], {})})
    authorized = {}
    for binding in current.get('legacy', {}).values():
        fact = facts.get(binding['fact_id'])
        if (fact and fact.identity_scope == scope[2] and fact.source_call_id == binding['source_call_id']
                and not binding.get('conflict') and binding['fingerprint'] == _fingerprint(fact)):
            authorized[fact.fact_id] = fact
    shown = {}
    for ref, binding in current.get('shown', {}).items():
        fact = authorized.get(binding['fact_id'])
        if fact and not binding.get('conflict') and binding['fingerprint'] == _fingerprint(fact):
            shown[ref] = fact.fact_id
    aliases = {}
    if current.get('business_data'):
        try:
            from app.conf import fields_dict
            dictionary = fields_dict()
            aliases = {code: tuple(dictionary.aliases(code)) for code in dictionary.codes()}
        except Exception:
            pass  # Numeric observation must remain optional.
    return CitationTurnView(
        numeric_aliases=aliases,
        policy=policy, known_ids=set(authorized), shown=shown,
        evidence_called=bool(current.get('evidence_called') or set(budget.get('tool_call_counts') or {})
                             & (RAG_TOOLS | {'policy_detail', 'policy_search', 'customer_detail', 'customer_search',
                                             'run_policy_field_lookup', 'run_policy_snapshot'})),
        business_facts=[fact for key, fact in authorized.items() if key in current.get('business', [])],
        business_data=bool(current.get('business_data')),
    )


def map_citations(output: AgentOutput, state: dict, policy: ExcerptPolicy) -> CitationDecision:
    """Compatibility facade; production resolves once in the result bridge."""
    from app.core.citation_policy import resolve_citations
    try:
        return resolve_citations(output, turn_view(state, policy))
    except Exception:
        return CitationDecision(output.model_copy(update={'answer_draft': '', 'fact_refs': []}),
                                ('mapping_missing',), True)
