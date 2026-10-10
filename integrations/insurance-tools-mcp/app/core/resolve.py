"""实体与实参解析(core 纯函数 · §10.1 resolve · 模型不产 ID,P5)。

L1 范围 = G-4a 硬规则形态:显式保单号 pattern × 字段词典,产出 resolved_args;
序数/指示词/裸字段等 G-3 续接解析随切片 4 扩展(同一单点,策略可换)。
resolve 产出的 residual(未覆盖残文)供 Gate 做完全性/复合性判定(§4.6)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from app.core.identifiers import matching_text, POLICY_PATTERN

from app.core.field_dict import FieldDict
from app.schemas.entity import PolicyEntityRef

# 显式保单号 pattern(G-4a 判据;样例 P123/P002——真实格式确认后迁入 gate/gate_{ENV_TYPE}_args.yaml)
POLICY_NO_PATTERN = POLICY_PATTERN
POLICY_NO_RE = re.compile(POLICY_NO_PATTERN)


def normalize_policy_text(text: str) -> str:
    """Normalize only identifier matching inputs; raw requests/results stay intact."""
    return matching_text(text)

# 残文清理用停用词(完全性判定的助词集;复合性判定规则在 gate,§4.6)
_STOPWORDS = {
    "现在", "谢谢", "是谁", "还有",  "那", "再",
    "保单",
    "保单号",
    "尾号",  # 广义保单号定位词(Owner 2026-08-30):数字提取后无实义,同「保单」
    "的",
    "了",
    "是",
    "多少",
    "查",
    "查询",
    "请问",
    "一下",
    "帮",
    "我",
    "看",
    "告诉",
    "下",
    "啥",
    "什么",
    "有",
    "哪些",
    "和",
    "以及",
    "呢",
    "吗",
    "啊",
    "就",
    "吧",
    "选",
    "选择",
    "要",
}

# G-3 序数引用(「第X张」;按有界展示列表解析，越界不猜)
_ORDINAL_RE = re.compile(r"(?:第([一二三四五六七八九十]|\d+)(?:张|份|个|位)|候选\s*(\d+))")
_CN_NUMERALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# G-3 显式名回指只接受这些无语义壳词；名称本体来自类型化实体记忆，不从回答文本反解。
_NAME_REF_FILLERS = {
    "请问",
    "帮我",
    "查询",
    "一下",
    "这张",
    "那张",
    "这份",
    "那份",
    "保单",
    "的",
    "查",
    "看",
    "呢",
    "吗",
    "啊",
    "吧",
}


@dataclass(frozen=True)
class ResolvedDirectQuery:
    """G-4a 直查实参:保单号 + 字段码集 + 残文(供 Gate 判完全性)。"""

    policy_no: str
    field_codes: tuple[str, ...]
    spoken_fields: tuple[str, ...]
    residual: str  # 去除保单号与字段词后的剩余文本(已剥空白)


@dataclass(frozen=True)
class ResolvedContinuation:
    """G-3 续接实参:实体引用(序数/显式名/裸字段)绑定后与 G-4a 同构。"""

    policy_no: str
    field_codes: tuple[str, ...]
    spoken_fields: tuple[str, ...]
    residual: str
    ref_kind: str  # ordinal / explicit_name / bare_field


@dataclass(frozen=True)
class RewrittenPolicyWorkflowQuery:
    """无号保单 Workflow 的输入拆分结果。

    ``customer_query`` 只承载客户搜索条件；原始问题仍由 Workflow 状态保存，供
    policy-field-lookup 继续解析字段。``scope_explicit`` 表示用户明确说出了
    “某客户名下/的保单”，可供零模型 Gate 安全放行顶层 Workflow。
    """

    customer_query: str
    field_codes: tuple[str, ...]
    spoken_fields: tuple[str, ...]
    scope_explicit: bool


def scan_fields(text: str, field_dict: FieldDict) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """字段词典最长别名扫描，并按用户原文顺序返回命中字段。

    最长匹配只用于解决「年缴保费」与「保费」这类重叠别名；字段的最终顺序必须
    跟随原文，而不能跟随 YAML/别名字典顺序，否则 Direct 模板会打乱用户问题。
    同一字段的多个口语别名仍全部从 residual 中剥离，但只返回首次命中的字段码。
    """
    lowered = text.lower()
    candidates: list[tuple[int, int, str, str]] = []
    # FieldDict.normalize 约定标准字段码恒等映射；扫描路径也保持同一语义，便于
    # 调试/API 调用使用 CASH_VALUE 等标准码且不受大小写影响。
    searchable = dict(field_dict.alias_items())
    searchable.update({code: code for code in field_dict.codes()})
    # Composite request is distinct from the single product-name field.
    searchable.update({alias: 'policy_basic_info' for alias in ('保单基本信息', '基本信息', '保单概况', 'policy_basic_info')})
    for alias, code in searchable.items():
        start = 0
        while (index := lowered.find(alias, start)) >= 0:
            candidates.append((index, index + len(alias), alias, code))
            start = index + len(alias)

    for pattern, code in ((r'(?:年交|年缴|一年交)(?:多少钱|多少|金额)', 'year_pay_prem'),
                          (r'(?:是)?(?:年交|年缴)(?:还是(?:月交|月缴)|吗)', 'prem_method_desc')):
        if code in field_dict.codes():
            candidates.extend((m.start(), m.end(), m.group(), code) for m in re.finditer(pattern, lowered))
    # 同起点优先最长别名；随后按原文位置选择互不重叠的命中。
    selected: list[tuple[int, int, str, str]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        begin, end, _, _ = candidate
        if any(begin < chosen_end and end > chosen_begin for chosen_begin, chosen_end, *_ in selected):
            continue
        selected.append(candidate)

    seen_codes: set[str] = set()
    hits: list[tuple[str, str]] = []
    chars = list(text)
    for begin, end, alias, code in selected:
        chars[begin:end] = " " * (end - begin)
        if code not in seen_codes:
            hits.append((text[begin:end] or alias, code))
            seen_codes.add(code)
    residual = "".join(chars)
    from tool_adapter_sdk.policy_detail_metadata import is_amount_field, definition
    if hits and all(is_amount_field(c) or c in {'premium', 'cash_value'} for _, c in hits):
        residual = residual.replace('是多少钱', '').replace('多少钱', '')
    if hits and all(((entry := definition(c)) is not None and entry.logical_type in {'期限', '日期'}) or c.endswith('_term') or c in {'waiting_period', 'grace_period', 'cooling_off', 'insurance_period', 'hesitation_period'} for _, c in hits):
        residual = residual.replace('多久', '')
    return tuple(c for _, c in hits), tuple(a for a, _ in hits), residual


def resolve_direct_query(
    text: str,
    field_dict: FieldDict,
    policy_pattern: str | None = None,
    tail_pattern: str | None = None,
) -> ResolvedDirectQuery | None:
    """从自然语言抽取「保单号 + 字段集」;无显式保单号 → None(非 G-4a 形态,交后续检查)。

    policy_pattern/tail_pattern 由 Gate 从 gate/gate_{ENV_TYPE}_args.yaml 注入(策略数据;缺省用 core 常量);
    tail_pattern 承接广义保单号(Owner 2026-08-30:「尾号4567」的 "4567" 类裸数字 ≥3 位,
    唯一/多候选由工具后端判定,多候选 → AMBIGUOUS 澄清);P 式优先,尾号仅在 P 式未命中时尝试。
    字段扫描为最长别名优先(避免「现金价值」被「现价」部分截断);同一别名多次出现只记一次。
    """
    text = matching_text(text)
    regex = re.compile(policy_pattern or POLICY_NO_PATTERN, re.IGNORECASE)
    m = regex.search(text)
    if m is None and tail_pattern:
        regex = re.compile(tail_pattern)
        m = regex.search(text)
    if m is None:
        return None
    policy_no = m.group().upper()
    if policy_no.isdigit() and not valid_policy_tail(policy_no, text=text):
        return None
    residual = regex.sub(" ", text, count=1)
    residual = re.sub(r'(?:尾号|保单号)\s*(?:是|为|码)\s*', ' ', residual)
    codes, spoken, residual = scan_fields(residual, field_dict)
    return ResolvedDirectQuery(
        policy_no=policy_no,
        field_codes=codes,
        spoken_fields=spoken,
        residual=strip_stopwords(residual),
    )


def resolve_continuation(
    text: str,
    field_dict: FieldDict,
    entities: dict,
    policy_pattern: str | None = None,
    tail_pattern: str | None = None,
) -> ResolvedContinuation | None:
    """G-3 会话续接解析(§4.6 窄域引用集;0 LLM)。

    白名单:序数(第X张 × last_policy_set)+ 已确认实体显式名(product_name/insured_name
    × policy_entities)+ 裸字段(绑定 recent_policies 最新项，兼容 focused_policy);指示词不做。优先级为显式保单号 >
    序数 > 唯一显式名 > 裸字段。显式名只在 last_policy_set 内唯一命中才绑定，歧义/新名称
    不猜。前提守卫:文本含显式保单号(P 式或广义尾号)→ None(显式优先于记忆,G-4a 领域);
    序数越界/状态未建 → None;完全解析或放弃——残文非空由 Gate 判定不放行。
    entities = session_state["entities"] 子集(§8.1④ 类型化实体上下文,G-3 永远只读此层)。
    """
    text = matching_text(text)
    regex = re.compile(policy_pattern or POLICY_NO_PATTERN, re.IGNORECASE)
    if regex.search(text):
        card = [matching_text(str(p)) for p in entities.get('last_policy_set') or []]
        if text.strip() in card:
            return ResolvedContinuation(text.strip(), ('policy_basic_info',), ('保单基本信息',), '', 'explicit_card')
        return None
    if tail_pattern and re.search(tail_pattern, text):
        return None  # 广义尾号同样显式优先(Owner 2026-08-30 口径)
    last_set = [str(p) for p in (entities.get("last_policy_set") or [])]

    # A bare adjacent card selection asks for basic information.
    if last_set and (choice := match_event_answer(text, last_set)) and not event_answer_residual(text, choice, last_set):
        return ResolvedContinuation(choice, ('policy_basic_info',), ('保单基本信息',), '', 'ordinal')
    m = _ORDINAL_RE.search(text)
    if m is not None:
        token = m.group(1) or m.group(2)
        idx = int(token) if token.isdigit() else _CN_NUMERALS.get(token)
        if idx is None or not (1 <= idx <= len(last_set)):
            return None  # 越界/未建状态 → 不猜(fail-safe:漏放方向)
        residual, policy_no, ref = text.replace(m.group(), " "), last_set[idx - 1], "ordinal"
    elif named := _resolve_named_policy(text, field_dict, entities, last_set or list((entities.get('policy_entities') or {}).keys())):
        return named
    elif entities.get("recent_policies") or entities.get("focused_policy"):
        recent = entities.get("recent_policies") or []
        focused = recent[-1] if recent else entities["focused_policy"]
        residual, policy_no, ref = text, str(focused), "bare_field"
    else:
        return None
    codes, spoken, residual = scan_fields(residual, field_dict)
    if not codes:
        return None
    return ResolvedContinuation(
        policy_no=policy_no, field_codes=codes, spoken_fields=spoken,
        residual=strip_stopwords(residual), ref_kind=ref,
    )


_WORKFLOW_PREFIXES = (
    "请帮我查询", "请帮忙查询", "帮我查询", "请查询", "查询一下", "帮我查一下",
    "帮忙查一下", "请帮我查", "查询", "帮我查", "帮忙查", "查一下", "请问",
    "看看", "看下", "查", "列出",
)
_POLICY_SCOPE_RE = re.compile(r"名下(?:的)?(?:所有|全部)?保单|的(?:所有|全部)?保单")
_WORKFLOW_GENERIC_LOCATORS = {"", "我", "本人", "客户", "该客户", "这个客户"}


def is_customer_cohort(locator: str) -> bool:
    """姓氏或生日条件是群体筛选，不能当作一个待锁定的客户。"""
    normalized = re.sub(r"\s+", "", matching_text(locator)).casefold()
    return bool(
        re.search(r"姓[\u4e00-\u9fff]{1,2}(?:的)?客户", normalized)
        or ("生日" in normalized and "客户" in normalized)
    )


def rewrite_policy_workflow_query(
    text: str,
    field_dict: FieldDict,
    *,
    policy_pattern: str | None = None,
) -> RewrittenPolicyWorkflowQuery | None:
    """把复合保单问句拆成客户搜索入参和字段意图。

    该函数只做确定性句式归约，不调用模型。进入 Workflow 后允许处理
    “王五的现金价值”这类已由 Agent 明确选中 Workflow 的输入；顶层 Gate 只消费
    ``scope_explicit=True`` 的严格“客户—保单”句式，避免把产品名误当客户名。
    """
    normalized = matching_text(text).strip()
    if not normalized:
        return None
    policy_regex = re.compile(policy_pattern or POLICY_NO_PATTERN, re.IGNORECASE)
    if policy_regex.search(normalized):
        return None

    codes, spoken, without_fields = scan_fields(normalized, field_dict)
    candidate = without_fields.strip()
    changed = True
    while changed:
        changed = False
        for prefix in _WORKFLOW_PREFIXES:
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):].strip()
                changed = True
                break

    match = _POLICY_SCOPE_RE.search(candidate)
    scope_explicit = match is not None
    if match is not None:
        locator = candidate[:match.start()]
        tail = candidate[match.end():]
        if not is_fully_resolved(strip_stopwords(tail)):
            return None
    elif codes:
        # Agent/run_* 已选定字段 Workflow 时，字段词前的“某客户的”可安全改写为
        # customer_search 的单一客户条件；这条宽入口不参与顶层 Gate 放行。
        locator = candidate
    else:
        return None

    if re.search(r'[、,，/与和及]|[\u4e00-\u9fff]{2,}\s+[\u4e00-\u9fff]{2,}', locator):
        return None
    locator = locator.strip(" ，。！？!?")
    locator = re.sub(r"\s+", " ", locator).strip()
    # “客户王五”中的“客户”只是实体壳词，可以剥离；“客户号……”里的
    # “客户号”却是 customer_search 解析器的完整字段词，不能被改写成“号……”。
    if locator.startswith("客户") and not locator.startswith(("客户号", "客户ID", "客户编号", "客户姓名")):
        locator = locator[len("客户"):].strip()
    locator = locator.rstrip("的 ").strip()
    # Remove the person wrapper only around a complete labelled identifier.
    # Other qualifiers (another person's customers, exclusions, conjunctions)
    # stay intact and cannot become an exact customer lookup.
    labelled = re.fullmatch(r"(客户(?:号|编号|ID)\s*(?:为|是)?\s*[:：]?\s*[A-Z0-9_-]{3,64})的客户", locator, re.I)
    if labelled:
        locator = labelled.group(1)
    if locator in _WORKFLOW_GENERIC_LOCATORS or any(token in locator for token in ("和", "与", "、")):
        return None
    return RewrittenPolicyWorkflowQuery(
        customer_query=locator,
        field_codes=codes,
        spoken_fields=spoken,
        scope_explicit=scope_explicit,
    )


def _resolve_named_policy(
    text: str,
    field_dict: FieldDict,
    entities: dict,
    last_set: list[str],
) -> ResolvedContinuation | None:
    """将已确认的产品名/被保人名唯一绑定到最近保单集合；不做模糊推断。"""
    codes, spoken, residual = scan_fields(text, field_dict)
    if not codes:
        codes, spoken = ('policy_basic_info',), ('保单基本信息',)
    mention = _normalize_entity_name(_strip_name_ref_fillers(residual))
    if len(mention) < 2:
        return None
    raw_refs = entities.get("policy_entities")
    if not isinstance(raw_refs, dict):
        return None

    matches: list[str] = []
    for policy_no in last_set:
        raw_ref = raw_refs.get(policy_no)
        if not isinstance(raw_ref, dict):
            continue
        products = raw_ref.get('product_names', [raw_ref.get('product_name')])
        if not isinstance(products, list):
            continue
        # Each source name can locate the policy, while clause selection still
        # requires a single product. Do not lose insured-name lookup on multi-risk policies.
        for product in products[:100]:
            try:
                ref = PolicyEntityRef.model_validate({'policy_no': raw_ref.get('policy_no'),
                    'product_name': product, 'insured_name': raw_ref.get('insured_name')})
            except ValueError:
                continue
            if ref.policy_no != policy_no:
                continue
            names = (_normalize_entity_name(ref.product_name), _normalize_entity_name(ref.insured_name))
            if any(_is_explicit_name_match(mention, name) for name in names):
                matches.append(policy_no)
                break
    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) != 1:
        return None
    return ResolvedContinuation(
        policy_no=unique_matches[0],
        field_codes=codes,
        spoken_fields=spoken,
        residual="",
        ref_kind="explicit_name",
    )


def _strip_name_ref_fillers(text: str) -> str:
    """仅剥显式名引用的窄域壳词，避免通用停用词误伤姓名/产品名。"""
    for word in sorted(_NAME_REF_FILLERS, key=len, reverse=True):
        text = text.replace(word, " ")
    text = "".join(" " if unicodedata.category(char).startswith("P") else char for char in text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_entity_name(value: str) -> str:
    return re.sub(r"\s+", "", matching_text(value).casefold())


def _is_explicit_name_match(mention: str, entity_name: str) -> bool:
    """二字姓名只允许全称；三字以上名称允许实体全称中的确定性简称。"""
    if not entity_name:
        return False
    if mention == entity_name:
        return True
    return len(mention) >= 3 and len(entity_name) >= 3 and mention in entity_name


def match_event_answer(text: str, options: list[str]) -> str | None:
    """G-2 挂起应答匹配(§09 共享候选匹配代码;评审五:精确/包含两级,无编辑距离兜底)。

    可匹配:候选本身(大小写归一)/ 序数(第X张)/ 纯数字序号 / 候选在文中唯一出现(包含级);
    多候选同时出现(歧义)或零命中 → None(用户无视换话题 → 常规 Gate 流转,会话式语义)。
    """
    stripped = matching_text(text).strip().rstrip("。.!！")
    compact = re.sub(r"\s+", "", stripped)
    def label(value):
        return re.sub(r"\s+", "", matching_text(value).split(' · ', 1)[0])
    label_hits = [o for o in options if label(o) == label(stripped)]
    if len(label_hits) == 1:
        return label_hits[0]
    lowered = {re.sub(r"\s+", "", matching_text(o)): o for o in options}
    if lowered.get(compact):
        return lowered[compact]  # 精确:应答即候选

    number_hits = [o for o in options if (m := re.search(r'客户号\s*([A-Z0-9_-]+)', matching_text(o)))
                   and compact in {m.group(1), '客户号' + m.group(1)}]
    if len(number_hits) == 1:
        return number_hits[0]
    m = _ORDINAL_RE.search(stripped)
    if m is not None:
        token = m.group(1) or m.group(2)
        idx = int(token) if token.isdigit() else _CN_NUMERALS.get(token)
        if idx is not None and 1 <= idx <= len(options):
            return options[idx - 1]
    if stripped.isdigit() and 1 <= int(stripped) <= len(options):
        return options[int(stripped) - 1]

    hits = {
        o for o in options
        # 包含级:候选在文中唯一出现——词边界防前缀误配(「P1230」不得命中候选「P123」)
        if re.search(rf"(?<![0-9A-Za-z]){re.escape(matching_text(o))}(?![0-9A-Za-z])", stripped, re.IGNORECASE)
    }
    return next(iter(hits)) if len(hits) == 1 else None


def event_answer_residual(text: str, selected: str, options: list[str]) -> str:
    """剥离已命中的候选/序数；剩余实质文本用于区分纯事件与混合输入(§7.2①/③)。"""
    stripped = matching_text(text).strip().rstrip("。.!！")
    if re.sub(r"\s+", "", stripped) == re.sub(r"\s+", "", matching_text(selected)):
        return ""
    if re.sub(r'\s+', '', stripped.split(' · ', 1)[0]) == re.sub(r'\s+', '', matching_text(selected).split(' · ', 1)[0]):
        return ''
    number = re.search(r'客户号\s*([A-Z0-9_-]+)', matching_text(selected))
    if number and re.sub(r'\s+', '', stripped) in {number.group(1), '客户号' + number.group(1)}:
        return ''
    ordinal = _ORDINAL_RE.search(stripped)
    if ordinal is not None:
        token = ordinal.group(1) or ordinal.group(2)
        idx = int(token) if token.isdigit() else _CN_NUMERALS.get(token)
        if idx is not None and 1 <= idx <= len(options) and options[idx - 1] == selected:
            stripped = stripped.replace(ordinal.group(), " ", 1)
    elif stripped.isdigit() and 1 <= int(stripped) <= len(options) and options[int(stripped) - 1] == selected:
        stripped = ""
    else:
        stripped = re.sub(
            rf"(?<![0-9A-Za-z]){re.escape(matching_text(selected))}(?![0-9A-Za-z])",
            " ",
            stripped,
            flags=re.IGNORECASE,
        )
    return strip_stopwords(stripped)


def strip_stopwords(text: str) -> str:
    """剥停用词/Unicode 标点/空白后返回实质残文(空串 = 完全归约)。"""
    for w in sorted(_STOPWORDS, key=len, reverse=True):
        text = text.replace(w, " ")
    text = "".join(" " if unicodedata.category(char).startswith("P") else char for char in text)
    return re.sub(r"\s+", " ", text).strip()


def is_fully_resolved(residual: str) -> bool:
    """完全解析或放弃(§4.6):残文为空才算完全;残留任何片段 → 判不完整,不猜。"""
    return residual == ""


def valid_policy_tail(value: str, *, text: str | None = None) -> bool:
    """Structured tails accept years; natural text requires a locator for year-like numbers.

    Amounts, phone numbers and dates are never inferred as policy identifiers.
    """
    if not re.fullmatch(r"\d{3,14}", value) or re.fullmatch(r"1[3-9]\d{9}", value):
        return False
    if text is None:
        return True
    escaped = re.escape(value)
    if re.search(rf"(?:尾号|保单号)\s*(?:是|为|码)?\s*[:：]?\s*{escaped}(?!\d)", text):
        return True
    if re.search(rf"{escaped}\s*(?:元|万|亿|块|年|月|日|%)", text):
        return False
    if re.search(rf"(?:保费|保额|金额|价值|余额)\s*(?:为|是|超过|大于|小于)?\s*{escaped}", text):
        return False
    return re.fullmatch(r"(?:19|20)\d{2}", value) is None
