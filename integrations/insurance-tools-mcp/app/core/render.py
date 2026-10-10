"""模板渲染(core 纯函数 · §12.1 形态③ 模板直出 · 0 LLM)。

Direct 执行器 0 渲染(评审五「组织步单点化」)——一切组织发生在 S4 finalize 唯一出口;
本模块是形态③的唯一渲染实现。三重纪律的渲染侧落点(§4.7):金额千分位格式化、
as_of 显式回显、自验证回显(主体 ID + 字段名 + 值——「P002 现金价值…」,错绑一眼可见)。
数字只经格式化、永不重算;字段显示名由调用方从字段字典注入(首别名 = 规范口语名,
core 零配置 IO)。
2026-09-13 检视收口(A1):AgentOS 展示投影族自 turn_workflow 迁入——稳定内部
AnswerEnvelope → 前端自然语言正文(render_agentos_answer),缺口去重/内部状态
脱敏集中在本模块,渲染职责归一。
"""
from __future__ import annotations

import re
from decimal import Decimal
from dataclasses import dataclass
from collections.abc import Sequence

from app.schemas.evidence import Evidence
from app.schemas.enums import SourceType

from app.schemas.answer import AnswerEnvelope
from app.schemas.enums import EnvelopeStatus
from app.schemas.fact import Fact
from app.core.field_dict import FieldDict
from tool_adapter_sdk.policy_detail_catalog import V5_FIELDS
from tool_adapter_sdk.policy_detail_metadata import SOURCE_ALIASES

# 工具层 gaps 的 L1 约定格式(「字段未返回:<字段码>」)——渲染侧转口语名;M1 随 tool_config 错误归一结构化
_GAP_CODE_PREFIX = "字段未返回:"

_NO_FACT_ANSWERS = {
    EnvelopeStatus.DENIED: "无权限查询该保单信息，请确认保单归属或联系人工渠道。",
    EnvelopeStatus.ERROR: "查询服务暂时异常，请稍后重试或联系人工渠道。",
    EnvelopeStatus.TIMEOUT: "查询服务响应超时，请稍后重试或联系人工渠道。",
    EnvelopeStatus.AMBIGUOUS: "尚未形成可执行的查询条件，请补充完整的客户或保单信息。",
}

_SUBJECT_LABELS = {
    "policy": "保单",
    "customer": "客户",
    "product": "产品",
    "benefit": "权益",
}

_RAG_LABELS = {
    "content": "服务内容",
    "threshold": "使用门槛",
    "usage": "使用方式",
    "validity": "有效期",
    "eligibility": "资格条件",
    "waiting_period": "等待期",
    "liability_summary": "保险责任",
    "exclusion_clause": "责任免除",
    "underwriting_rules": "投保规则",
}
_RAG_TEXT_PREDICATES = {"qa_answer", "reference_content"}


def format_amount(value: int | float | str) -> str:
    """金额千分位、固定两位小数(Decimal 规避浮点尾差;str(float) 去尾零故须 quantize);
    不重算金额值。"""
    return f"{Decimal(str(value)).quantize(Decimal('0.01')):,f}"


def render_template_facts(
    facts: list[Fact],
    labels: dict[str, str],
    gaps: list[str] | None = None,
    status: EnvelopeStatus | None = None,
    empty_answer: str | None = None,
) -> str:
    """确定性 facts → 答案文本:按主体分组回显,金额带元与数据时点,缺口如实披露(零编造)。

    empty_answer 由查询主体方注入(customer 清单等非保单主体在 EMPTY/AMBIGUOUS 等
    无 facts 状态的兜底),防止跨主体复用保单话术;默认话术保持保单口径
    (既有对外行为不变)。"""
    had_material = bool(facts)
    facts = [fact for fact in facts if is_template_fact(fact)]
    gap_lines = _render_gaps(gaps or [], labels)
    if not facts:
        answer = empty_answer or ("已获得部分查询资料，但尚未完成整理；请稍后重试或联系人工渠道。"
                                 if had_material else None) or _NO_FACT_ANSWERS.get(
            status or EnvelopeStatus.EMPTY, "未查询到该保单的相关信息。"
        )
        return answer + (f"\n{gap_lines}" if gap_lines else "")

    lines: list[str] = []
    subject: tuple[str, str] | None = None
    policy_list = bool(facts) and all(fact.predicate == 'policy_brief' for fact in facts)
    policy_index = 0
    for fact in facts:
        if fact.predicate == 'qa_answer':
            lines.append(render_excerpts([fact], {fact.fact_id: ExcerptAttribution()}))
            subject = None
            continue
        current = (fact.subject_type, fact.subject_id)
        if current != subject:  # 主体切换才起新头(自验证回显:类型 + 标准 ID 显式可见)
            subject = current
            subject_label = _SUBJECT_LABELS.get(fact.subject_type, fact.subject_type)
            if fact.subject_type == "customer":
                # customer_id 是工作流内部定位键，不属于代理人需要核对的业务标识。
                # customer_brief 的首段来自权威搜索事实，可安全作为可见标题。
                visible_name = (
                    str(fact.typed_value).split("·", 1)[0].strip()
                    if fact.predicate == "customer_brief"
                    else ""
                )
                lines.append(f"{subject_label}{f' {visible_name}' if visible_name else ''}:")
            elif fact.subject_type == 'policy':
                if policy_list:
                    policy_index += 1
                    lines.append(f'{policy_index}.')
                lines.append(f"{subject_label} {fact.subject_id}:")
            else:
                name = fact.subject_id.strip()
                visible = name if re.search(r"[\u4e00-\u9fff]", name) else ""
                lines.append(f"{_SUBJECT_LABELS.get(fact.subject_type, '知识资料')}{' ' + visible if visible else ''}:")
        label = {**_RAG_LABELS, **labels}.get(fact.predicate, fact.predicate if re.search(r"[一-鿿]", fact.predicate) else "查询信息")
        lines.append(f"· {label}:{_render_value(fact)}")
    if gap_lines:
        lines.append(gap_lines)
    return "\n".join(lines)


@dataclass(frozen=True)
class ExcerptAttribution:
    question: str | None = None
    source: str = "知识资料"


def excerpt_attribution(refs: Sequence[Evidence]) -> ExcerptAttribution:
    questions = list(dict.fromkeys(item.locator for item in refs
        if item.source_type is SourceType.QA_PAIR and item.rerank_score is not None
        and item.locator and not item.locator.startswith("召回序号:")))
    return ExcerptAttribution(question="；".join(questions) or None,
        source="知识问答库" if refs and all(item.source_type is SourceType.QA_PAIR for item in refs) else "知识资料")


def render_excerpts(facts: Sequence[Fact], eligibility: dict[str, ExcerptAttribution],
                    *, gaps: Sequence[str] = (), confirmed: bool = True) -> str:
    """Render only approved facts; storage/reference IDs stay in the envelope sidecar."""
    passages = []
    for fact in facts:
        attribution = eligibility.get(fact.fact_id)
        if attribution is None or not str(fact.typed_value).strip():
            continue
        question = f"匹配原问题：{attribution.question}\n" if attribution.question else ""
        passage = f"{question}{str(fact.typed_value).strip()}\n[来源：{attribution.source}]"
        if passage not in passages:
            passages.append(passage)
    if not passages:
        return ""
    title = "参考原文（仅供参考）：" if confirmed else "参考原文（未确认，仅供参考）："
    answer = title + "\n" + "\n\n".join(passages)
    if gaps:
        answer += "\n参考限制：" + "；".join(dict.fromkeys(gaps))
    return answer


def render_rag_facts(
    facts: list[Fact],
    labels: dict[str, str],
    gaps: list[str] | None = None,
    status: EnvelopeStatus | None = None,
    *, evidence: Sequence[Evidence] = (), confirmed: bool = True,
) -> str:
    """Render RAG facts without exposing storage IDs or predicate codes."""
    gap_lines = _render_gaps(gaps or [], {**labels, **_RAG_LABELS})
    if not facts:
        answer = {
            EnvelopeStatus.DENIED: "无权限查询相关知识，请确认权限或联系人工渠道。",
            EnvelopeStatus.ERROR: "知识查询服务暂时异常，请稍后重试或联系人工渠道。",
            EnvelopeStatus.TIMEOUT: "知识查询服务响应超时，请稍后重试或联系人工渠道。",
            EnvelopeStatus.AMBIGUOUS: "尚未形成可执行的知识查询条件，请补充更具体的问题。",
        }.get(status or EnvelopeStatus.EMPTY, "未检索到相关知识，请补充更具体的问题。")
        return answer + (f"\n{gap_lines}" if gap_lines else "")

    if all(fact.predicate in _RAG_TEXT_PREDICATES for fact in facts):
        evidence_by_id = {item.evidence_id: item for item in evidence}
        return render_excerpts(facts, {
            fact.fact_id: excerpt_attribution([evidence_by_id[ref] for ref in fact.evidence_refs if ref in evidence_by_id])
            for fact in facts
        }, gaps=[_visible_gap(gap, {**labels, **_RAG_LABELS}) for gap in gaps or []], confirmed=confirmed)

    return render_template_facts(
        facts,
        {**labels, **_RAG_LABELS},
        gaps,
        status,
    )


def _render_value(fact: Fact) -> str:
    if fact.unit == "CNY":
        value = f"{format_amount(fact.typed_value)} 元"
        return f"{value}(数据时点 {fact.as_of})" if fact.as_of else value  # 金额必带 as_of 回显
    if fact.unit:
        return f"{fact.typed_value} {fact.unit}"
    from tool_adapter_sdk.policy_detail_metadata import is_amount_field
    from app.core.quantities import decimal_value
    numeric_value = decimal_value(fact.typed_value)
    if numeric_value is not None and is_amount_field(fact.predicate):
        exponent = numeric_value.as_tuple().exponent
        places = max(2, -exponent) if isinstance(exponent, int) else 2
        return f'{numeric_value:,.{places}f}'  # Unknown currency cannot imply rounding away source precision.
    if numeric_value is not None and fact.predicate == 'prem_term':
        if numeric_value == numeric_value.to_integral_value() and 0 < numeric_value <= 100:
            return f'{numeric_value.normalize():f} 年'
        return f'{fact.typed_value}（交费期间取值待核实）'
    return str(fact.typed_value)


def render_clarify_card(question: str, options: list[str]) -> str:
    """形态④候选卡渲染(§4.8③/§09:≤5 候选 + 应答方式提示;下一轮 G-2 匹配恢复)。"""
    lines = [question, *[f"{i}. {opt}" for i, opt in enumerate(options[:5], 1)]]
    lines.append("请回复序号或对应编号。")
    return "\n".join(lines)


def _render_gaps(gaps: list[str], labels: dict[str, str]) -> str:
    if not gaps:
        return ""
    missing = []
    notes = []
    for gap in dict.fromkeys(gaps):
        if gap.strip().casefold() in {'', '无', '无缺口', '没有', 'none', 'null', 'n/a', '不适用'}:
            continue
        spoken = _visible_gap(gap, labels)
        if gap.startswith(_GAP_CODE_PREFIX) or gap in labels:
            missing.append(spoken)
        else:
            notes.append(spoken)
    return "\n".join([*([f"尚未确认的信息：{'、'.join(missing)}"] if missing else []), *notes])


def is_template_fact(fact: Fact) -> bool:
    """Record JSON/reference passages remain in storage and their own renderers."""
    return fact.predicate != "reference_content" and not fact.predicate.endswith("_record")


def _visible_gap(gap: str, labels: dict[str, str]) -> str:
    """将缺口中的逻辑码/真实源字段码转为代理人可读名称，保留行号等限定。"""
    text = gap.removeprefix(_GAP_CODE_PREFIX)
    for code in sorted(labels, key=len, reverse=True):
        text = text.replace(code, labels[code])
    if gap.startswith(_GAP_CODE_PREFIX):
        text = re.sub(r"[A-Za-z_][A-Za-z0-9_.]*", "相关字段", text)
    return text


# ---- AgentOS 展示投影(自 turn_workflow 迁入,2026-09-13 A1)----

_GAP_ENTITY = re.compile(r"[A-Za-z]+[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*|「([^」]+)」")
_GAP_LEADING_LABEL = re.compile(r"^\s*([一-鿿A-Za-z0-9_-]{2,20})\s*[：:]")
_UNCERTAINTY_MARKERS = (
    "未能确认",
    "未能匹配",
    "未匹配",
    "未查询到",
    "未获取",
    "未返回",
    "未识别",
    "暂未",
    "无法",
)
_DISCLOSURE_ACTION_MARKERS = ("补充", "提供", "确认", "选择", "回复", "重查", "继续")
_GENERIC_GAP_TERMS = frozenset({"产品", "信息", "字段", "记录", "完整", "提供"})
_PUBLIC_GAP_INTERNAL_STATUS = re.compile(
    r"[（(]?\s*(?:工具|接口)返回\s+(?:EMPTY|AMBIGUOUS|ERROR|TIMEOUT|DENIED|BUDGET)\s*[）)]?",
    re.IGNORECASE,
)


def render_agentos_answer(envelope: AnswerEnvelope) -> str:
    """把稳定内部信封投影为前端正文，只补用户可行动的业务缺口。"""
    answer = envelope.answer.strip()
    if envelope.completeness == "partial":
        answer = "（部分回答）\n" + answer
    internal_markers = ("步骤痕迹", "trace_id")
    public_gaps = [
        _sanitize_public_gap(gap)
        for gap in dict.fromkeys([*envelope.gaps, *(envelope.limitations or [])])
        if gap.strip() and not any(marker in gap for marker in internal_markers)
    ]
    public_gaps = list(dict.fromkeys(gap.strip().rstrip('。；;') for gap in public_gaps if gap))
    missing = [gap for gap in public_gaps if not _gap_already_disclosed(answer, gap)]
    if missing:
        answer += "\n补充说明：" + "；".join(missing) + "。"
    if "handoff" in envelope.followup_actions and "人工" not in answer:
        answer += "\n如需继续处理，请联系人工渠道核实。"
    return answer


def _sanitize_public_gap(gap: str) -> str:
    """移除面向实现的工具状态枚举，保留实体、业务缺口与用户可行动提示。"""
    # Translate the historical machine-only citation diagnostic, without
    # suppressing an entire gap or any adjacent business qualification.
    from app.core.citation_policy import CITATION_LIMITATION
    gap = re.sub(r"模型引用了证据池外的事实\s+F-[A-Za-z0-9_,， -]+[,，]已按 D16 剔除", CITATION_LIMITATION, gap)
    if gap.strip().casefold() in {'', '无', '无缺口', '没有', 'none', 'null', 'n/a', '不适用'}:
        return ''
    cleaned = _PUBLIC_GAP_INTERNAL_STATUS.sub("", gap)
    cleaned = cleaned.replace("（）", "").replace("()", "")
    return cleaned.strip(" ,，;；")


def _gap_already_disclosed(answer: str, gap: str) -> bool:
    """保守判断结构化缺口是否已在自然语言正文披露，避免 AgentOS 重复追加。

    除逐字包含外，只接受“明确实体全部出现 + 正文和 gap 均含不确定性标记”。
    实体来自保单号、书名号或 gap 句首的受限冒号标签；否则不做语义猜测，
    继续追加，失败方向保持完整披露。
    """
    if gap in answer:
        return True
    entities = [match.group(1) or match.group(0) for match in _GAP_ENTITY.finditer(gap)]
    if not entities and (leading := _GAP_LEADING_LABEL.match(gap)):
        entities = [leading.group(1)]
    if not entities or not all(entity in answer for entity in entities):
        return False
    disclosure_aligned = (
        any(marker in answer for marker in _UNCERTAINTY_MARKERS)
        and any(marker in gap for marker in _UNCERTAINTY_MARKERS)
    ) or (
        any(marker in answer for marker in _DISCLOSURE_ACTION_MARKERS)
        and any(marker in gap for marker in _DISCLOSURE_ACTION_MARKERS)
    )
    if not disclosure_aligned:
        return False
    topics = []
    for raw in re.findall(r"[一-鿿]{2,}", gap):
        topic = raw
        for marker in _UNCERTAINTY_MARKERS:
            topic = topic.replace(marker, "")
        if len(topic) >= 2 and topic not in _GENERIC_GAP_TERMS and topic not in entities:
            topics.append(topic)
    return bool(topics) and any(
        topic in answer
        or (len(topic) >= 3 and any(topic[index : index + 3] in answer for index in range(len(topic) - 2)))
        for topic in topics
    )


def field_labels(fd: FieldDict) -> dict[str, str]:
    """字段码 → 规范口语显示名(字段字典首别名;调用方传入已加载实例,避免二次 IO)。"""
    labels = {
        "customer_brief": "客户摘要",
        "policy_brief": "保单摘要",
        "product_name": "产品名称",
        # 真实保单明细事实保留 V5 源字段码以维持可追溯性；只在用户答案的
        # 展示层使用权威目录中文标签，不能把 pol_status_desc 等内部码透出。
        **{field.path: field.label for field in V5_FIELDS},
        # 逻辑字段码面向用户，若与 V5 源路径同名（如 applicant_name），
        # 业务词典的规范口语应覆盖“姓名明文”等内部数据字典标签。
        **{code: fd.aliases(code)[0] for code in fd.codes() if fd.aliases(code)},
    }
    for source, canonical in SOURCE_ALIASES.items():
        if canonical in labels:
            labels[source] = labels[canonical]
    labels.update(insured_name="被保人", premium="保费", policy_status="保单状态",
                  product_name="产品名称", pol_product_name="产品名称")
    return labels
