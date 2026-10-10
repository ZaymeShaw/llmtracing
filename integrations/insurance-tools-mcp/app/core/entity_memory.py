"""Trusted, turn-bounded entity memory shared by Direct, Workflow and Agent tools."""
from __future__ import annotations

from app.core.session_health import session_turn_seq
from app.schemas import EnvelopeStatus, ToolEnvelope

_KEYS = ("active_customer", "last_policy_set", "focused_policy", "policy_entities", "active_products", "recent_policies")


def stamp_entities(state: dict, *keys: str) -> None:
    entities = state.setdefault("entities", {})
    dates = entities.get("entity_turns")
    if not isinstance(dates, dict):
        dates = {}
        entities["entity_turns"] = dates
    seq = session_turn_seq(state)
    for key in keys:
        if entities.get(key):
            dates[key] = seq
        else:
            dates.pop(key, None)


def fresh_entities(state: dict, max_age: int, policy_limit: int = 100) -> dict:
    """Read-only G-3 view. Undated legacy entities cannot authorize continuation."""
    entities = state.get("entities") or {}
    dates = entities.get("entity_turns")
    dates = dates if isinstance(dates, dict) else {}
    seq = session_turn_seq(state)
    view = {key: entities[key] for key in _KEYS if entities.get(key)
            and type(dates.get(key)) is int and dates[key] >= 0 and 0 <= seq - dates[key] <= max_age}
    if "last_policy_set" in view:
        view["last_policy_set"] = list(view["last_policy_set"][:policy_limit])
    if "recent_policies" in view:
        view["recent_policies"] = list(view["recent_policies"][-policy_limit:])
    return view


def remember_subjects(state: dict, ids: list[str], *, subject_type: str | None = None) -> None:
    entities = state.setdefault("entities", {})
    seq = session_turn_seq(state)
    known = entities.get("known_subjects")
    if not isinstance(known, dict):
        known = {}
        entities["known_subjects"] = known
    for subject in ids:
        if subject:
            known.pop(subject, None)  # Refresh recency as well as TTL.
            known[subject] = seq
            if subject_type in {"policy", "customer", "product"}:
                known.pop(f"{subject_type}:{subject}", None)
                known[f"{subject_type}:{subject}"] = {
                    "turn_seq": seq, "subject_type": subject_type, "subject_id": subject,
                }
    # 与原五轮池的主体有效窗口一致；只保留有界 ID，不携带历史事实值。
    retained = {key: value for key, value in known.items()
                if type(turn := value.get("turn_seq") if isinstance(value, dict) else value) is int
                and 0 <= seq - turn < 5}
    # Legacy dict insertion order can disagree with turn timestamps. Stable
    # sorting keeps the newest observations and renewed entries within a turn.
    ordered = sorted(retained.items(), key=lambda item: int(item[1]["turn_seq"]
                     if isinstance(item[1], dict) else item[1]))
    entities["known_subjects"] = dict(ordered[-600:])


def known_subjects(state: dict, subject_type: str) -> set[str]:
    """只有类型明确、未过期的服务端主体记录能授权后续工具选择。"""
    known = (state.get("entities") or {}).get("known_subjects") or {}
    seq = session_turn_seq(state)
    # 类型必须来自服务端记录值，不能仅按 ID 前缀猜测；旧裸 ID 不授予新类型权限。
    return {value["subject_id"] for value in known.values()
            if isinstance(value, dict) and value.get("subject_type") == subject_type
            and isinstance(value.get("subject_id"), str) and type(value.get("turn_seq")) is int
            and 0 <= seq - value["turn_seq"] < 5} if isinstance(known, dict) else set()


def retire_evidence(state: dict) -> None:
    """迁移旧成功桶中的主体 ID，再丢弃上一轮值；失败桶不能授予选择权限。"""
    for bucket in ((state.get("evidence") or {}).get("by_turn") or {}).values():
        if not isinstance(bucket, dict) or bucket.get("confirmed", True) is not True:
            continue
        for fact in bucket.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            kind, subject = fact.get("subject_type"), fact.get("subject_id")
            if kind in {"policy", "customer", "product"} and isinstance(subject, str):
                # 已有带类型记录不刷新 TTL；升级前无记录的旧桶只迁移一次。
                known = (state.get("entities") or {}).get("known_subjects") or {}
                if f"{kind}:{subject}" not in known:
                    remember_subjects(state, [subject], subject_type=kind)
    state["evidence"] = {"by_turn": {}}


def remember_policy(state: dict, policy_no: str, *, policy_limit: int = 100, track_turn: bool = True) -> None:
    """Observe a typed policy identity; commit_turn alone publishes routing focus."""
    # Selection is typed entity memory, not a second source of next-turn focus.
    remember_subjects(state, [policy_no], subject_type="policy")


def remember_policy_list(state: dict, ids: list[str], *, policy_limit: int = 100, track_turn: bool = True) -> list[str]:
    displayed = list(dict.fromkeys(ids))[:policy_limit]
    remember_subjects(state, displayed, subject_type='policy')
    return displayed


def remember_tool_entities(
    state: dict, tool: str | None, envelope: ToolEnvelope, *, policy_limit: int = 100,
) -> None:
    """Consume only typed successful results, never model arguments or answer text."""
    if envelope.status not in (EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL):
        return
    from app.core.tool_ledger import record
    source_tool = {'present_policy_search_card': 'policy_search', 'present_customer_search_card': 'customer_search'}.get(tool or '', tool or '')
    if source_tool:
        record(state.setdefault('budget', {}), source_tool, envelope.model_dump_json(), envelope=envelope)
    entities = state.setdefault("entities", {})
    for kind in ("policy", "customer", "product"):
        remember_subjects(state, list(dict.fromkeys(
            f.subject_id for f in envelope.facts if f.subject_type == kind
        )), subject_type=kind)
    if tool in {"policy_search", "present_policy_search_card"}:
        # Source-page records may contain more displayed rows than the brief facts.
        records = (envelope.data or {}).get("records")
        if isinstance(records, list):
            record_ids: list[str] = []
            for row in records:
                value = row.get("polno") if isinstance(row, dict) else None
                if isinstance(value, str) and value.strip():
                    record_ids.append(value)
            ids = list(dict.fromkeys(record_ids))
        else:
            ids = list(dict.fromkeys(f.subject_id for f in envelope.facts if f.subject_type == "policy"))
        if not ids:
            return
        entry = record(state.setdefault('budget', {}), source_tool, envelope.model_dump_json(), envelope=envelope)
        entry.subjects = frozenset(ids)
        ids = remember_policy_list(state, ids, policy_limit=policy_limit)
        refs: dict[str, dict] = {p: {"policy_no": p} for p in ids}
        for fact in envelope.facts:
            if (fact.subject_type == "policy" and fact.subject_id in refs
                    and fact.predicate in {"product_name", "insured_name"}
                    and isinstance(fact.typed_value, str) and fact.typed_value.strip()):
                refs[fact.subject_id][fact.predicate] = fact.typed_value
        entities["policy_entities"] = refs
        remember_policy_products(state, envelope, policy_limit=policy_limit)
        stamp_entities(state, "policy_entities")
        remember_subjects(state, ids)
    elif tool == "policy_detail":
        remember_policy_products(state, envelope, policy_limit=policy_limit)
        ids = list(dict.fromkeys(f.subject_id for f in envelope.facts if f.subject_type == "policy"))
        if len(ids) == 1:
            remember_policy(state, ids[0], policy_limit=policy_limit)

    elif tool == "product_knowledge":
        ids = list(dict.fromkeys(f.subject_id for f in envelope.facts if f.subject_type == "product"))
        if ids:
            entities["active_products"] = ids
            stamp_entities(state, "active_products")


def remember_policy_products(state: dict, envelope: ToolEnvelope, *, policy_limit: int = 100) -> None:
    """Keep only source-confirmed product references; never carry historical amounts."""
    if envelope.status not in {EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL}:
        return
    entities = state.setdefault('entities', {})
    stored = entities.get('policy_entities')
    refs = dict(stored) if isinstance(stored, dict) else {}
    products: dict[str, list[str]] = {}
    for fact in envelope.facts:
        if fact.subject_type == 'policy' and fact.predicate in {'product_name', 'pol_product_name'} and isinstance(fact.typed_value, str) and fact.typed_value.strip():
            products.setdefault(fact.subject_id, []).append(fact.typed_value)
    # Search facts can have a single projected product while the source page has
    # several risk rows. Consume all names, preserving ambiguity without altering it.
    rows = (envelope.data or {}).get('records')
    row_names: dict[str, list[str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        subject = row.get('polno')
        name = row.get('pol_product_name') or row.get('polProductName') or row.get('product_name')
        if isinstance(subject, str) and isinstance(name, str) and name.strip():
            row_names.setdefault(subject, []).append(name)
    products.update(row_names)
    for subject, names in products.items():
        previous = refs.pop(subject, {})
        ref = {**(previous if isinstance(previous, dict) else {}), 'policy_no': subject}
        unique = list(dict.fromkeys(names))
        ref.pop('product_name', None)
        ref.pop('product_names', None)
        if len(unique) == 1:
            ref['product_name'] = unique[0]
        else:
            ref['product_names'] = unique[:policy_limit]
        refs[subject] = ref
    if products:
        entities['policy_entities'] = dict(list(refs.items())[-policy_limit:])
        stamp_entities(state, 'policy_entities')


def invalidate_policy_focus(state: dict, *, clear_card: bool = False) -> None:
    """Invalidate routing authority while retaining typed entity/product references."""
    from app.core.focus import apply_focus, Focus
    card = () if clear_card else tuple((state.get('entities') or {}).get('last_policy_set') or [])
    apply_focus(state, Focus(card=card))
