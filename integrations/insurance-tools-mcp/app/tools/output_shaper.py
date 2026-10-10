"""One bounded JSON model shape; no source mutation or head/tail slicing."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.tool_policy import OutputShapingPolicy as OutputShapingPolicy

from app.tools.tool_sets import RAG_TOOLS


@dataclass(frozen=True)
class ReferenceBinding:
    ref: str
    source_call_id: str
    fact_id: str


@dataclass(frozen=True)
class Preview:
    text: str
    json_output: bool
    omitted_items: int
    excerpted_values: int
    facts_total: int
    facts_retained: int
    reference_conflicts: int
    references: tuple[ReferenceBinding, ...] = ()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _overflow_preview(data: dict, policy: OutputShapingPolicy, stub: dict) -> Preview:
    """Keep bounded execution/page identity when the projected payload is too large.

    Counts and IDs describe the source page, never proof of omitted record values.
    IDs are whole values; if they cannot all fit, disclose that separately.
    """
    source_execution = data.get('execution')
    execution = {key: source_execution[key] for key in ('status', 'code') if key in source_execution} if isinstance(source_execution, dict) else {}
    if 'status' not in execution and isinstance(data.get('status'), str):
        execution['status'] = data['status']
    page = data.get('data')
    if isinstance(page, dict):
        for key in ('total', 'pageNo', 'pageSize', 'truncated'):
            value = page.get(key)
            if type(value) in (int, bool) or value is None and key in page:
                execution[key] = value
        rows = page.get('records')
        if isinstance(rows, list):
            ids = []
            for row in rows:
                if isinstance(row, dict):
                    identity = {key: row[key] for key in ('polno', 'policy_no', 'idicm_clientno', 'customer_id', 'clientMainId', 'clientNo', 'subject_id',
                                                        'clientName', 'searchclientname', 'insurantNameCipher', 'insurant_name', 'polProductName', 'plan_abbr_name')
                                if isinstance(row.get(key), (str, int)) and not isinstance(row[key], bool)}
                    if identity:
                        ids.append(identity)
            execution['primary_keys'] = ids
    rows = page.get('records') if isinstance(page, dict) else None
    stub = {**stub, 'projection': {**stub['projection'], 'omitted_items': max(1, len(rows) if isinstance(rows, list) else 1)}}
    # The source truncation flag keeps its meaning. Projection omission is separate.
    wrapped = {**stub, 'result': {'execution': execution} if execution else {}}
    ids = execution.get('primary_keys', [])
    if ids and len(_dump(wrapped)) > policy.max_chars:
        kept: list[dict] = []
        execution['primary_keys'] = kept
        wrapped['projection'] = {**stub['projection'], 'primary_keys_incomplete': True}
        remaining = policy.max_chars - len(_dump(wrapped))
        # Serialize each whole ID once, rather than repeatedly dumping the page.
        for identity in ids:
            cost = len(_dump(identity)) + bool(kept)
            if cost > remaining:
                break
            kept.append(identity)
            remaining -= cost
    if len(_dump(wrapped)) > policy.max_chars:
        wrapped = stub
    return Preview(_dump(wrapped), True, stub['projection']['omitted_items'], 0, 0, 0, 0)


def _sentences(value: str, limit: int, query: str) -> str | None:
    """Select complete units, retaining all conditional/negative units or omit the entry."""
    units = [u for u in re.split(r'(?<=[。！？])|(?<=[.!?])\s+|\n\s*\n', value) if u]
    if len(units) > 8192:
        return None
    required = {i for i, u in enumerate(units) if re.search(r'不|未|无|否|除|仅|只|若|如果|须|必须|条件|限于|但是|但', u)}
    # Keep adjoining explanation with a qualifier; never present a standalone exception.
    required |= {j for i in tuple(required) for j in (i - 1, i + 1) if 0 <= j < len(units)}
    if sum(len(units[i]) for i in required) > limit:
        return None
    terms = re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]{2}', query[:256])[:64]
    ordered = sorted(range(len(units)), key=lambda i: (-sum(t in units[i] for t in terms), i))
    chosen = set(required)
    size = sum(len(units[i]) for i in chosen)
    for i in ordered:
        if i not in chosen and size + len(units[i]) <= limit:
            chosen.add(i)
            size += len(units[i])
    return ''.join(units[i] for i in sorted(chosen)) or None


def _descriptions(data: dict, descriptions: dict, allowance: int) -> tuple[dict, bool]:
    """Keep complete definitions of visible fields, prioritizing shared paths.

    Flattening is internal only. Indexed conflicts are kept separately; descriptions
    for omitted rows cannot claim that a field was returned in the retained rows.
    """
    flat: dict[str, str] = {}
    def flatten(node: Any, prefix: str = '', depth: int = 0) -> None:
        if depth > 64:
            return
        if isinstance(node, str):
            flat[prefix] = node
        elif isinstance(node, dict):
            for key, child in node.items():
                path = prefix if key == '$description' else (prefix + '.' if prefix else '') + key
                flatten(child, path, depth + 1)
    flatten(descriptions)
    visible: set[str] = set()
    def visit(value: Any, path: str, depth: int = 0) -> None:
        if depth > 64:
            return
        if path:
            visible.update({path, re.sub(r'\[\d+\]', '[]', path)})
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, (path + '.' if path else '') + key, depth + 1)
        elif isinstance(value, list):
            visible.add(path + '[]')
            for index, child in enumerate(value):
                visit(child, f'{path}[{index}]', depth + 1)
        elif isinstance(value, str) and value[:1] in {'[', '{'}:
            try:
                child = json.loads(value)
            except (ValueError, RecursionError):
                return
            if isinstance(child, (dict, list)):
                visit(child, path, depth + 1)
    visit(data.get('data', {}), '')
    kept: dict[str, str] = {}
    size = 2
    omitted = False
    for path in sorted(flat, key=lambda path: (bool(re.search(r'\[\d+\]', path)), path)):
        if path not in visible and path != '$notes':
            continue
        text = flat[path]
        cost = len(_dump(path)) + len(_dump(text)) + 1 + bool(kept)
        if size + cost > allowance:
            omitted = True
            continue
        kept[path] = text
        size += cost
    return kept, omitted


def preview_output(tool: str, visible: str, policy: OutputShapingPolicy,
                   references: dict[str, dict] | None = None, *, live: bool = True, query: str = '',
                   numbered_references: bool = True) -> Preview:
    """Stable {result, projection}; bounded serialization work, indivisible business records.

    execution is protected as a whole. If even execution cannot fit, return an explicit
    unavailable-view stub, never a partially clipped execution or altered source value.
    """
    marker: dict = {'incomplete': False, 'omitted_items': 0, 'excerpted_values': 0}
    stub = {'result': {}, 'projection': {**marker, 'incomplete': True,
        'reason': '模型视图无法完整呈现；原始结果已保留，不能据省略判断无数据或确认业务结论'}}
    try:
        data = json.loads(visible, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        return Preview(_dump(stub), True, 1, 0, 0, 0, 0)
    if not isinstance(data, dict):
        data = {'value': data}
    if len(visible) > policy.max_input_chars:
        return _overflow_preview(data, policy, stub)
    from app.tools.model_output import BUSINESS_TOOL_NAMES, canonical_business_tool_name
    knowledge_view = canonical_business_tool_name(tool) not in BUSINESS_TOOL_NAMES
    if knowledge_view:
        for key in ('ok', 'source', 'request_id', 'latency_ms'):
            data.pop(key, None)
        if isinstance(data.get('error'), dict):
            data['error'].pop('retryable', None)
    if tool in RAG_TOOLS and data.get('status') == 'EMPTY':
        hint = data.get('hint')
        reason = hint.partition('；')[0] if isinstance(hint, str) else ''
        # Preserve a bounded source reason (no match vs no documents), while
        # replacing old retry directions with the same usable next step.
        if not reason or len(reason) > 256 or any(word in reason for word in ('请', '改写', '重试')):
            reason = '本次未找到可回答资料'
        data['hint'] = reason + '；请勿改写同一问题重复查询。可用已有其他证据回答并说明未找到的部分，必要时建议通过人工渠道确认。'
    if tool == 'entity_lookup':
        # Dictionary metadata stays in the original envelope and evidence pool.
        def strip_metadata(value: Any) -> None:
            if isinstance(value, dict):
                for key in ('sales_status', 'period'):
                    value.pop(key, None)
                for child in value.values():
                    strip_metadata(child)
            elif isinstance(value, list):
                for child in value:
                    strip_metadata(child)
        strip_metadata(data)
    refs = dict(references or {})
    source_evidence = data.pop('evidence', [])
    evidence = {item.get('evidence_id'): item for item in source_evidence if isinstance(item, dict)} if isinstance(source_evidence, list) else {}
    facts = data.get('facts')
    total = len(facts) if isinstance(facts, list) else 0
    projected = []
    conflicts = 0
    bindings: list[ReferenceBinding] = []
    for fact in (facts[:policy.max_references] if isinstance(facts, list) else []):
        if not isinstance(fact, dict):
            marker['omitted_items'] += 1
            continue
        item = {k: v for k, v in fact.items() if k not in {'evidence_refs', 'identity_scope', 'source_call_id', 'ref'}}
        if knowledge_view:
            for key in ('unit', 'as_of'):
                if item.get(key) is None:
                    item.pop(key, None)
        value = item.get('typed_value')
        if tool in RAG_TOOLS and item.get('predicate') in {'qa_answer', 'reference_content'} and isinstance(value, str) and len(value) > policy.item_chars:
            excerpt = _sentences(value, policy.item_chars, query)
            if excerpt is None:
                marker['omitted_items'] += 1
                continue
            item.update(typed_value=excerpt, excerpted=True)
            marker['excerpted_values'] += 1
        item['sources'] = [{k: source[k] for k in ('source_id', 'locator', 'product_version', 'document_id') if source.get(k) is not None}
                           for key in fact.get('evidence_refs', []) if (source := evidence.get(key))]
        if numbered_references and all(isinstance(fact.get(k), str) and len(fact[k]) <= 256 for k in ('source_call_id', 'fact_id')):
            key = hashlib.sha256(_dump([fact['source_call_id'], fact['fact_id']]).encode()).hexdigest()
            fingerprint = hashlib.sha256(_dump({k: v for k, v in fact.items() if k != 'evidence_refs'}).encode()).hexdigest()
            previous = refs.get(key)
            if previous and previous['fingerprint'] != fingerprint:
                conflicts += 1
            elif previous:
                item['ref'] = previous['ref']
            elif len(refs) < policy.max_references:
                item['ref'] = f'[{len(refs) + 1}]'
                refs[key] = {'ref': item['ref'], 'fact_id': fact['fact_id'], 'source_call_id': fact['source_call_id'], 'fingerprint': fingerprint}
            if 'ref' in item:
                bindings.append(ReferenceBinding(item['ref'], fact['source_call_id'], fact['fact_id']))
        projected.append(item)
    if isinstance(facts, list):
        marker['omitted_items'] += max(0, total - policy.max_references)
        data['facts'] = projected
    wrapped = {'result': data, 'projection': marker}
    # Linear pruning: serialize each complete list item once, never the whole output in a search loop.
    if len(_dump(wrapped)) > policy.max_chars:
        descriptions = data.pop('field_descriptions', {})
        reserved, omitted_descriptions = _descriptions(data, descriptions, policy.max_chars // 3)
        description_size = len(_dump(reserved)) + 24 if descriptions else 0
        execution = data.pop('execution', None)
        protected_size = len(_dump(execution)) + 20 if execution is not None else 0
        allowance = policy.max_chars - protected_size - description_size - 384
        visited = 0
        def fit(value: Any, remaining: int, depth: int = 0) -> tuple[Any, int]:
            nonlocal visited
            visited += 1
            if visited > policy.max_nodes or depth > 64 or remaining < 2:
                marker['omitted_items'] += 1
                return {}, 2
            if isinstance(value, list):
                kept: list[Any] = []
                size = 2
                for i, item in enumerate(value):
                    rendered = _dump(item)
                    cost = len(rendered) + bool(kept)
                    if size + cost > remaining:
                        marker['omitted_items'] += len(value) - i
                        break
                    kept.append(item)
                    size += cost
                return kept, size
            if isinstance(value, dict):
                kept_dict: dict[str, Any] = {}
                size = 2
                for key, child in value.items():
                    overhead = len(_dump(key)) + 1 + bool(kept_dict)
                    if isinstance(child, (list, dict)):
                        fitted, cost = fit(child, remaining - size - overhead, depth + 1)
                    else:
                        fitted, cost = child, len(_dump(child))
                    if size + overhead + cost <= remaining:
                        kept_dict[key] = fitted
                        size += overhead + cost
                    else:
                        marker['omitted_items'] += 1
                return kept_dict, size
            return value, len(_dump(value))
        data, _ = fit(data, allowance)
        if execution is not None:
            data['execution'] = execution
        if descriptions:
            data['field_descriptions'], _ = _descriptions(data, reserved, description_size)
            if omitted_descriptions:
                marker['descriptions_omitted'] = True
        wrapped['result'] = data
    marker['incomplete'] = bool(marker['omitted_items'] or marker['excerpted_values'] or marker.get('descriptions_omitted'))
    text = _dump(wrapped)
    if len(text) > policy.max_chars:
        text = _overflow_preview(data, policy, stub).text
        data = {}
        marker['omitted_items'] += 1
    retained_facts = data.get('facts', [])
    shown = {(f.get('ref'), f.get('fact_id')) for f in retained_facts if isinstance(f, dict)}
    retained = tuple(b for b in bindings if (b.ref, b.fact_id) in shown)
    if references is not None:
        references.update(refs)
    return Preview(text, True, marker['omitted_items'], marker['excerpted_values'], total,
                   len(retained_facts), conflicts, retained)
