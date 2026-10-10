"""Single reducer for routing focus; authoritative history plus typed tool subjects.

Turns keep bounded hashes and references, never historical values. Native callers
use stored run history only after a durable start/terminal acknowledgement; missing
or uncommitted history cannot authorize implicit focus.
"""
from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass

from app.core.identifiers import matching_text, policy_ids
from app.core.policy_projection import POLICY_FIELDS_MISSING_HINT
from app.core.resolve import scan_fields, strip_stopwords, match_event_answer

MAX_TURNS = 12


def question_hash(text: str) -> str:
    normalized = re.sub(r'[\s。！？?!]+', '', matching_text(text))
    return hashlib.sha256(normalized.encode()).hexdigest()


def plain_answer(text: str) -> str:
    try:
        value = json.loads(text)
        if isinstance(value, dict) and isinstance(value.get('answer'), str):
            return value['answer']
    except (ValueError, TypeError):
        pass
    return text


def numbered_policies(text: str) -> list[str]:
    result = []
    for block in re.finditer(r'^\s*\d+[.)、]\s*(.*?)(?=^\s*\d+[.)、]|\Z)', plain_answer(text), re.M | re.S):
        ids = policy_ids(block.group(1)[:300])
        if len(ids) == 1:
            result.append(ids[0])
    return list(dict.fromkeys(result))[:10]


def pure_followup(text: str, dictionary) -> bool:
    codes, _, residual = scan_fields(matching_text(text), dictionary)
    return bool(codes) and not strip_stopwords(residual)


@dataclass(frozen=True)
class Focus:
    policy: str | None = None
    card: tuple[str, ...] = ()
    aligned: bool = False


def history_turns(history: list[dict]) -> list[dict]:
    """Visible question/answer pairs; references identify branches, never authorize values."""
    result: list[dict] = []
    for message in history:
        text = str(message.get('content') or '')
        if message.get('role') == 'user':
            result.append({'q_norm_hash': question_hash(text), 'user_refs': policy_ids(text)[:10],
                           'answer_refs': [], 'focus_after': None, 'card': [], 'refs_touched': []})
        elif message.get('role') == 'assistant' and result:
            result[-1]['answer_refs'] = sorted(policy_ids(plain_answer(text)))[:10]
            result[-1]['card'] = numbered_policies(text)
    return result[-MAX_TURNS:]


def derive_focus(history: list[dict] | None, turns: list[dict], dictionary) -> Focus:
    turns = [t for t in turns if isinstance(t, dict)] if isinstance(turns, list) else []
    if history is None:
        return Focus()  # No durable acknowledgement; identical policy across workers.
    if not history:
        return Focus()
    visible = history_turns(history)
    count = min(len(visible), len(turns), MAX_TURNS)
    needed = min(3, len(turns), len(visible))
    pairs = list(zip(visible[-count:], turns[-count:])) if count else []
    aligned = bool(count and count >= needed and all(
        left['q_norm_hash'] == right.get('q_norm_hash')
        and left['answer_refs'] == right.get('answer_refs', []) for left, right in pairs))
    if aligned and len(visible) < len(turns):
        aligned = all(left['answer_refs'] for left, _ in pairs) or any(set(left['user_refs']) & set(right.get('user_refs') or [])
                      or set(left['card']) & set(right.get('card') or []) for left, right in pairs)
    if aligned:
        tail = turns[-1]
        return Focus(tail.get('focus_after'), tuple(tail.get('card') or []), True)
    # Unmatched history: assistant prose cannot introduce a new focus. The only
    # exception is an immediately visible numbered list, used as an index only.
    focus = None
    card: list[str] = []
    retry_card: list[str] = []
    for message in history[-2 * MAX_TURNS:]:
        text = str(message.get('content') or '')
        if message.get('role') == 'assistant':
            displayed = numbered_policies(text)
            retry_notice = any(note in plain_answer(text) for note in ('本轮未保存', '本轮查询暂未能保存', '请重发', '请重新发送'))
            card = displayed or (retry_card if retry_notice else [])
            retry_card = []
            continue
        if message.get('role') != 'user':
            continue
        ids = policy_ids(text)
        selected = match_event_answer(text, card) if card else None
        if len(ids) == 1:
            focus = ids[0]
        elif len(ids) > 1:
            focus = None
        elif selected:
            focus = selected
        elif not pure_followup(text, dictionary):
            focus = None
        retry_card = list(card) if selected else []
        card = []
    if history[-1].get('role') != 'assistant':
        card = []
    return Focus(focus, tuple(card))


def apply_focus(state: dict, focus: Focus) -> None:
    """The only writer of deterministic focused_policy/recent_policies/card."""
    from app.core.entity_memory import stamp_entities
    entities = state.setdefault('entities', {})
    for key in ('focused_policy', 'recent_policies', 'last_policy_set'):
        entities.pop(key, None)
    if focus.policy:
        entities['focused_policy'] = focus.policy
        entities['recent_policies'] = [focus.policy]
    if focus.card:
        entities['last_policy_set'] = list(focus.card)
    stamp_entities(state, 'focused_policy', 'recent_policies', 'last_policy_set')
    if not entities.get('entity_turns'):
        entities.pop('entity_turns', None)


def commit_turn(state: dict, answer: str) -> None:
    from app.core.tool_ledger import records
    from app.schemas import EnvelopeStatus
    route = state.get('route') or {}
    question = route.get('user_text')
    if not isinstance(question, str) or not question.strip():
        return
    from app.conf import fields_dict
    entries = records(state.get('budget') or {})
    touched, successful, detailed, detail_touched = [], [], [], []
    for entry in entries:
        if entry.envelope is None:
            continue
        ids = [f.subject_id for f in entry.envelope.facts if f.subject_type == 'policy']
        touched.extend([*entry.subjects, *ids])
        if entry.tool == 'policy_detail':
            detail_touched.extend([*entry.subjects, *ids])
        if entry.envelope.status in {EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL} or (entry.tool == 'policy_detail' and entry.envelope.status is EnvelopeStatus.EMPTY
                and entry.envelope.hint == POLICY_FIELDS_MISSING_HINT):
            subjects = [*ids, *sorted(entry.subjects)] if entry.tool in {'policy_detail', 'policy_search', 'present_policy_search_card'} else ids
            successful.extend(subjects)
            if entry.tool == 'policy_detail':
                detailed.extend(subjects)
    touched = list(dict.fromkeys(touched))[:10]
    successful = list(dict.fromkeys(successful))[:10]
    explicit = policy_ids(question)
    detailed = list(dict.fromkeys(detailed))
    focus_candidates = detailed or successful
    old = (state.get('entities') or {}).get('focused_policy')
    focus = focus_candidates[0] if len(focus_candidates) == 1 and len(set(detail_touched)) <= 1 else None
    args = (route.get('decision') or {}).get('resolved_args') or {}
    choice = str(args.get('selected_option') or args.get('policy_no') or '')
    confirmed_choice = explicit[0] if len(explicit) == 1 else choice
    if confirmed_choice in successful and len(set(detail_touched)) <= 1:
        focus = confirmed_choice
    if not touched:
        args = (route.get('decision') or {}).get('resolved_args') or {}
        choice = str(args.get('selected_option') or args.get('policy_no') or '')
        choice_ids = policy_ids(choice)
        if len(explicit) == 1:
            focus = explicit[0]
        elif len(choice_ids) == 1 and matching_text(choice) == choice_ids[0]:
            # A server-resolved adjacent card may answer an unsupported field
            # without making a new RPC. The choice remains the user's subject.
            focus = choice_ids[0]
        elif pure_followup(question, fields_dict()) or route.get('preserved_policy_focus'):
            focus = old or route.get('preserved_policy_focus')
    if len(explicit) > 1:
        focus = None
    displayed = numbered_policies(answer)
    # A model-written list cannot mint new routing identities.
    card = [ref for ref in displayed if ref in successful]
    if card:
        focus = card[0] if len(card) == len(successful) == 1 else focus
    else:
        previous_card = (state.get('entities') or {}).get('last_policy_set') or []
        # Aligned, successive selections may refer to the same displayed list.
        if (focus in previous_card and (not explicit or set(explicit) <= set(previous_card))
                and (pure_followup(question, fields_dict()) or match_event_answer(question, previous_card)
                     or ((route.get('decision') or {}).get('resolved_args') or {}).get('policy_no') == focus)):
            card = list(previous_card)
    turn = {'q_norm_hash': question_hash(question), 'refs_touched': touched,
            'answer_refs': sorted(policy_ids(plain_answer(answer)))[:10], 'user_refs': explicit[:10],
            'focus_after': focus, 'card': card, 'run_id': str(route.get('run_id') or route.get('trace_id') or '')}
    raw_turns = route.get('resync_turns', state.get('turns'))
    turns = [t for t in raw_turns if isinstance(t, dict)] if isinstance(raw_turns, list) else []
    if turns and turn['run_id'] and turns[-1].get('run_id') == turn['run_id']:
        turns[-1] = turn
    else:
        turns.append(turn)
    state['turns'] = turns[-MAX_TURNS:]
    state.pop('continuity', None)
    state.pop('native_focus_generation', None)
    apply_focus(state, Focus(focus, tuple(card), True))
