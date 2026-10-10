"""Pure source-fact scope selection shared by tools, recovery and model assembly."""
from app.schemas import Fact
from app.core.render import is_template_fact
from tool_adapter_sdk.policy_detail_metadata import SOURCE_ALIASES, resolve_focus

POLICY_FIELDS_MISSING_HINT = "已查询到该保单，但本次未返回所需字段信息；可调整查询字段或联系人工核实。"

BASIC_INFO_FIELDS = ("insured_name", "product_name", "policy_status", "premium")
BASIC_INFO_ALIASES = frozenset({"policy_basic_info", "保单基本信息", "基本信息", "保单概况"})


def expand_basic_info(field: str) -> tuple[str, ...]:
    """One shared logical field contract for tool and display projections."""
    return BASIC_INFO_FIELDS if field in BASIC_INFO_ALIASES else (field,)


_LOGICAL_FOCUS = {"premium": "year_pay_prem"}

def display_policy_facts(facts: list[Fact], fields: list[str] | None) -> list[Fact]:
    # 完整逐行引用留在工具结果/证据池，不把原始 JSON 直接铺成用户答案。
    candidates = [fact for fact in facts if not fact.predicate.endswith('_record')]
    if not fields:
        return candidates
    predicates: set[str] = set()
    for field in fields:
        selected = expand_basic_info(field)
        for item in selected:
            code = _LOGICAL_FOCUS.get(item, resolve_focus(item))
            code = "insured_name" if code == "insurant_name" else code
            predicates.add(SOURCE_ALIASES.get(code, code))
    def canonical(value: str) -> str:
        code = _LOGICAL_FOCUS.get(value, resolve_focus(value))
        return 'insurant_name' if code == 'insured_name' else SOURCE_ALIASES.get(code, code)
    wanted = {canonical(value) for value in predicates}
    return [fact for fact in candidates if canonical(fact.predicate) in wanted]


def requested_template_facts(facts: list[Fact], requested: list[str] | None,
                              subjects: set[str] | None = None) -> list[Fact]:
    facts = [fact for fact in facts if not subjects or fact.subject_type != 'policy' or fact.subject_id in subjects]
    focus = requested if requested is None or requested else ['applicant_name', 'insurant_name', 'pol_status_desc', 'pol_product_name']
    business = [fact for fact in facts if fact.subject_type == 'policy']
    other = [fact for fact in facts if fact.subject_type != 'policy' and is_template_fact(fact)
             and (not requested or fact.subject_type == 'customer')]
    return [*display_policy_facts(business, focus), *other]


def requested_policy_subjects(state: dict) -> set[str]:
    """Scope recovery to explicit current references; never display another policy's pool."""
    from app.core.resolve import POLICY_NO_RE, normalize_policy_text
    route = state.get('route') or {}
    explicit = {match.group() for match in POLICY_NO_RE.finditer(normalize_policy_text(str(route.get('user_text') or '')))}
    args = (route.get('decision') or {}).get('resolved_args') or {}
    ref = args.get('policy_no')
    if not explicit and isinstance(ref, str) and POLICY_NO_RE.fullmatch(ref):
        explicit.add(ref)
    return explicit

