"""TS-03 pure, conservative redirects. No IO, semantic guessing or argument loss.

Only closed whole-query grammars qualify. Callers still run the target's admission,
identity checks, metrics and ordinary business implementation. Names/queries never
enter observation keys. A decision authorizes one redirect, never a redirect chain.
"""
from dataclasses import dataclass
import re
from app.core.tool_policy import SelectionRule as SelectionRule


@dataclass(frozen=True)
class SelectionRedirect:
    tool: str
    arguments: dict


def _detail_request(text: str, identifier: str, subject: str) -> bool:
    return re.fullmatch(
        rf'(?:请)?(?:查询|查看)?(?:{subject}(?:号|中心ID)?)?\s*{re.escape(identifier)}'
        r'(?:的)?(?:详情|详细信息|信息)?[。？?]?', text.strip(), flags=re.IGNORECASE,
    ) is not None


def resolve_redirect(subtype: str, arguments: dict, user_text: str, *,
                     known_customers: set[str]) -> SelectionRedirect | None:
    """Keep projection parameters verbatim; unknown/ill-typed parameters disqualify.

    Policy numbers must be complete P-prefixed references (numeric suffixes stay
    on the original clarification path). Customer IDs must already be confirmed
    center IDs. Natural-language search is limited to explicit named-owner lists;
    the complete query must match this turn's user request, without lost filters.
    """
    key = 'polno' if subtype == 'policy_shape' else 'query'
    if set(arguments) - {key, 'compact', 'fields'}:
        return None
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or len(user_text) > 1024:
        return None
    if 'compact' in arguments and type(arguments['compact']) is not bool:
        return None
    fields = arguments.get('fields')
    if fields is not None and (not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)):
        return None
    projection = {k: arguments[k] for k in ('compact', 'fields') if k in arguments}
    query = value.strip()
    if subtype == 'policy_search':
        from app.core.resolve import POLICY_NO_RE, normalize_policy_text
        refs = POLICY_NO_RE.findall(normalize_policy_text(query))
        if len(refs) != 1:
            return None
        ref = refs[0]
        if not _detail_request(query, ref, '保单') or not _detail_request(user_text, ref, '保单'):
            return None
        # The whole current request explicitly supplies this complete reference;
        # target admission still applies its existing confirmation rules.
        return SelectionRedirect('policy_detail', {'polno': ref, **projection})
    if subtype == 'customer_search':
        if (query in known_customers and re.search(r'(?:详情|详细信息|信息)[。？?]?$', user_text.strip())
                and _detail_request(user_text, query, '客户')):
            return SelectionRedirect('customer_detail', {'customer_id': query, **projection})
    if subtype == 'policy_shape' and query == user_text.strip():
        match = re.fullmatch(r'(?:请)?(?:查询|查找|列出|搜索)([\u4e00-\u9fff]{2,4})名下的(?:有效|失效|全部)?保单[。？?]?', query)
        if match and not re.search(r'我|他|她|其|该|这|那|谁|上述|客户|投保人|被保人', match[1]):
            return SelectionRedirect('policy_search', {'query': value, **projection})
    return None


def observe_selection(budget: dict, subtype: str, outcome: str) -> None:
    """Bounded keys supplied by the guard/controller, never arguments or identifiers."""
    counts = budget.setdefault('selection_corrections', {})
    key = f'{subtype}.{outcome}'
    counts[key] = counts.get(key, 0) + 1
