"""Pure context-dependency guard shared by deterministic and semantic routing."""
from app.core.identifiers import matching_text
import re

from app.schemas import Fact

_CONTEXT = re.compile(
    r"第[一二三四五六七八九十百0-9]+(?:个|位|张|份)|"
    r"(?:这|那|该|此)(?:[一二三四五六七八九十两几0-9]*[个人位名张份]|俩|些|客户)|"
    r"上一个|前一个|前一位|后一位|另一位|刚才|上面|前面|上述|以上|"
    r"他们|她们|它们|其他|另外|同一个|同一位|(?:他|她|它)(?:的|名下)|(?i:(?<![A-Za-z])TA(?![A-Za-z]))"
)

def explicit_customer_ids(text: str) -> set[str]:
    """Only labelled caller identifiers; shape/phone and ownership checks stay downstream."""
    return set(re.findall(r'客户(?:[Ii][Dd]|号|编号|标识)\s*(?:为|是)?\s*[:：]?\s*([A-Za-z0-9_-]{3,64})(?![A-Za-z0-9_-])', text))


def explicit_customer_locator(text: str) -> bool:
    """A complete person name or explicitly labelled customer ID, never leftover filters."""
    from app.core.resolve import is_customer_cohort
    if context_dependent_query(text) or is_customer_cohort(text):
        return False
    text = normalize_customer_name(text)
    # A suffix “客户” is ambiguous without an explicit name label/honorific:
    # e.g. 杭州客户 may be a cohort even when a customer named 杭州 exists.
    # Keep the condition intact for Agent clarification instead of guessing.
    if '的客户' in text:
        return False
    if text.endswith('客户') and not (
        re.fullmatch(r'[\u4e00-\u9fff]{2,4}(?:先生|女士)客户', text)
        or re.fullmatch(r'客户姓名[:：]?[\u4e00-\u9fff]{2,4}客户', text)
    ):
        return False
    if re.fullmatch(r"客户(?:号|编号|ID)\s*(?:为|是)?\s*[:：]?\s*[A-Za-z0-9_-]{3,64}", text, re.I):
        return True
    locator = re.sub(r"^(?:客户姓名[:：]?|客户)|(?:(?:先生|女士)?客户|先生|女士)$", "", text).strip()
    # This is only eligibility for a confirming lookup, never a guessed identity.
    # Four-character unlabelled strings may be joined names; no surname blacklist.
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,3}", locator)
                or (text.startswith('客户姓名') or locator.startswith(('欧阳', '司马', '上官', '诸葛', '司徒', '夏侯', '东方', '南宫'))) and re.fullmatch(r"[\u4e00-\u9fff]{2,4}", locator))


def context_dependent_query(text: str) -> bool:
    """No inference: a search judge receives no upstream conversational context."""
    return bool(_CONTEXT.search(text))


def qualified_customer_name(text: str) -> bool:
    return bool(re.search(r'[、,，/与和及]|[\u4e00-\u9fff]{2,}\s+[\u4e00-\u9fff]{2,}', text))


def normalize_customer_name(value: str) -> str:
    return re.sub(r'[\s·・]+', '', matching_text(value))


def customer_names(facts: list[Fact], data: dict | None = None) -> dict[str, str]:
    """Raw typed search rows override display briefs; do not split a real middle dot."""
    from tool_adapter_sdk.customer_search_metadata import source_value, require_customer_id
    names = {fact.subject_id: str(fact.typed_value).split('·', 1)[0].strip()
             for fact in facts if fact.subject_type == 'customer' and fact.predicate == 'customer_brief'}
    for row in (data or {}).get('records') or []:
        if not isinstance(row, dict):
            continue
        name = source_value(row, 'clientName')
        if isinstance(name, str):
            names[require_customer_id(row)] = name
    return names


def matched_customer_subjects(facts: list[Fact], locator: str, data: dict | None = None) -> set[str]:
    """Match business customer numbers to clientno, never clientMainId by accident."""
    from tool_adapter_sdk.customer_search_metadata import source_value, require_customer_id
    text = matching_text(locator).strip()
    locator = text
    name = normalize_customer_name(re.sub(r'^客户姓名[:：]?|^客户|(?:(?:先生|女士)?客户|先生|女士)$', '', locator))
    ids = {value.casefold() for value in explicit_customer_ids(text)}
    if ids:
        labelled_internal = bool(re.search(r'客户[Ii][Dd]', text))
        if labelled_internal:
            return {fact.subject_id for fact in facts if fact.subject_type == 'customer'
                    and fact.subject_id.casefold() in ids}
        return {require_customer_id(row) for row in (data or {}).get('records') or []
                if isinstance(row, dict) and str(source_value(row, 'clientno') or '').casefold() in ids}
    if not explicit_customer_locator(text) and not re.fullmatch(r'[\u4e00-\u9fff]', text):
        return set()
    return {subject for subject, value in customer_names(facts, data).items()
            if normalize_customer_name(value) in {name, normalize_customer_name(text)}}


def customer_display_details(data: dict | None) -> dict[str, str]:
    """Optional source customer number/sex distinguishes names; internal IDs stay private."""
    from tool_adapter_sdk.customer_search_metadata import source_value, require_customer_id
    result = {}
    for row in (data or {}).get('records') or []:
        if not isinstance(row, dict):
            continue
        parts = []
        number = source_value(row, 'clientno')
        if isinstance(number, (str, int)) and str(number).strip():
            parts.append('客户号 ' + str(number).strip())
        sex = source_value(row, 'clientSex')
        if isinstance(sex, str) and sex in {'男', '女'}:
            parts.append(sex)
        if parts:
            result[require_customer_id(row)] = '；'.join(parts)
    return result
