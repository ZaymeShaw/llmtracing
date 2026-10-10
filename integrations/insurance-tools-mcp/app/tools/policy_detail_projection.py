"""完整源数据与同源事实分离；fields 仅指定关注点，不裁剪业务数据。"""
from __future__ import annotations

import json
from typing import cast

from pydantic import JsonValue

from app.core.policy_projection import expand_basic_info, BASIC_INFO_ALIASES
from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Fact, SourceRef, ToolEnvelope
from app.tools.backend_contracts import PolicyDetailResponse
from tool_adapter_sdk.policy_detail_metadata import SOURCE_ALIASES, DEFINITIONS, definition, describe_output_data, resolve_focus

_SOURCE = SourceRef(system="policy_svc", endpoint="/policy/full")
_NAME_CODES = {"insurant_name": "insured_name", "applicant_name": "applicant_name"}
_POLICY_COLUMNS = {"polno", "insurant_name", "applicant_name", "pol_product_name", "pol_status_desc"}
_FOCUS = {
    "现金价值": "cash_value",
    "现价": "cash_value",
    "账户价值": "cash_value",
    # fields_{ENV_TYPE}_args.yaml 的逻辑字段码 premium 表示代理人口径的年缴保费；真实 V5
    # 明细对应 year_pay_prem。不能把逻辑码直接当成源字段名查询。
    "premium": "year_pay_prem",
    "保费": "year_pay_prem",
}
_MISSING = object()


def _present(value: object) -> bool:
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not _MISSING and value is not None and value != "" and not (isinstance(value, str) and not value.strip())


def _path_value(row: dict[str, JsonValue], path: str) -> object:
    values: list[object] = [row]
    for part in path.split("."):
        next_values: list[object] = []
        for value in values:
            items = value if isinstance(value, list) else [value]
            keys = [part, SOURCE_ALIASES.get(part), *(key for key, original in SOURCE_ALIASES.items() if original == part)]
            next_values.extend(next((item[key] for key in keys if key in item), _MISSING)
                               if isinstance(item, dict) else _MISSING for item in items)
        values = next_values
    if not values or any(value is _MISSING for value in values):
        return _MISSING
    if any(not _present(value) for value in values):
        return None
    return values[0] if len(values) == 1 else values


def _focus_gaps(rows: list[dict[str, JsonValue]], requested: list[str] | None, incomplete: bool) -> list[str]:
    gaps: list[str] = []
    for spoken in requested or []:
        selected = tuple(_FOCUS.get(item, resolve_focus(item)) for item in expand_basic_info(spoken))
        for code in selected:
            values = [_path_value(row, code) for row in rows]
            missing = [str(i + 1) for i, value in enumerate(values) if value is _MISSING]
            empty = [str(i + 1) for i, value in enumerate(values) if value is not _MISSING and not _present(value)]
            if missing:
                gaps.append(f"字段未返回:{code}（险种行 {','.join(missing)}）")
            if empty:
                gaps.append(f"字段返回 null/空值:{code}（险种行 {','.join(empty)}）")
            if definition(code) is None:
                gaps.append(f"字段语义未定义:{code}；V5 无明确业务定义，不推断当前金额含义")
            if code in _POLICY_COLUMNS and not missing and not empty:
                consistent = all(type(value) is type(values[0]) and value == values[0] for value in values)
                if incomplete or not consistent:
                    gaps.append(f"{_NAME_CODES.get(code, code)} 存在冲突或明细完整性不足，未生成保单级断言")
    return list(dict.fromkeys(gaps))


def project_real_detail(parsed: PolicyDetailResponse, requested: list[str] | None, unknown: list[str], agent_id: str) -> str:
    """已命中但没有目标字段与保单未命中明确区分。"""
    candidate_page = parsed.candidate_page
    if candidate_page is not None:
        if candidate_page.truncated:
            return make_envelope(status=EnvelopeStatus.AMBIGUOUS,
                gaps=["尾号查询结果不完整，不能确定唯一保单或提供完整候选集合"],
                hint="请提供完整保单号；当前页不能用于自动锁定保单", source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms)
        if not parsed.candidates:
            return make_envelope(status=EnvelopeStatus.EMPTY, hint="未查询到匹配该尾号的保单，请核对保单号", source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms)
        candidates = parsed.candidates[:5]
        candidate_facts = [to_fact(predicate="policy_candidate",
            typed_value=f"{c.policy_no}·被保人：{c.insured_name or '未提供'}·产品名称未提供",
            subject_id=c.policy_no, source_call_id=parsed.request_id, identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{i + 1:03d}") for i, c in enumerate(candidates)]
        return make_envelope(status=EnvelopeStatus.AMBIGUOUS, facts=candidate_facts,
            hint=f"尾号匹配到 {len(parsed.candidates)} 张保单，展示前 {len(candidates)} 张，请确认完整保单号；产品名称和状态未提供",
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms)
    page = parsed.detail_page
    assert page is not None
    rows = [row.model_dump(mode="json") for row in page.records]
    data: dict[str, JsonValue] = dict(page.source_metadata) if page.source_metadata is not None else {
        "pageNo": 1, "pageSize": 100, "total": page.record_total,
    }
    data["records"] = cast(JsonValue, rows)
    descriptions = describe_output_data(data)
    if not parsed.policy_no:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未查询到该保单，请核对保单号",
            data=data, field_descriptions=descriptions, source=_SOURCE,
            request_id=parsed.request_id, latency_ms=parsed.latency_ms)
    incomplete = page.truncated or len(rows) == 100
    policy_no = parsed.policy_no
    gaps = _focus_gaps(rows, requested, incomplete)
    gaps.extend(f"字段未识别:{value}" for value in unknown)
    if incomplete:
        gaps.append("明细达到服务上限或仍有未返回行，不能确认全部险种记录")
    facts: list[Fact] = []

    def add_fact(predicate: str, value: float | int | str | bool) -> None:
        facts.append(to_fact(predicate=predicate, typed_value=value, subject_id=policy_no,
            source_call_id=parsed.request_id, identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}"))

    # 只把完整一致的已定义标量提升为保单级事实，不从首行猜测姓名或汇总险种金额。
    if not incomplete:
        for column, value in rows[0].items():
            if definition(column) is None or not _present(value) or not isinstance(value, (str, int, float, bool)):
                continue
            if len(rows) > 1 and column not in _POLICY_COLUMNS:
                # 两个险种金额相同也不能证明保单总额；风险行属性仅从完整逐行事实引用。
                continue
            if all(type(row.get(column)) is type(value) and row.get(column) == value for row in rows):
                add_fact(_NAME_CODES.get(column, column), value)
            elif column in _POLICY_COLUMNS and requested is None:
                gaps.append(f"{_NAME_CODES.get(column, column)} 未完整提供或存在冲突，未生成保单级断言")
    # 所有关注点都提供完整逐行引用，复杂/未知字段也不会因无法生成标量事实而失去来源。
    for index, row in enumerate(rows):
        add_fact("policy_risk_record", f"records[{index}] 原始险种记录：" + json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    hint = f"已命中保单 {parsed.policy_no}，返回 {len(rows)} 条险种记录（服务报告 {page.record_total} 条）"
    if gaps:
        hint += "；" + "；".join(gaps)
    hint += ("；data 保留完整原始业务数据，字段说明见 field_descriptions；原始金额不等于当前现金价值，不能跨险种汇总。"
             "币种和业务时点未提供时仅按原字段原值表达，例如‘原始险种保额120,000.00，币种和业务时点未提供’；"
             "禁止自行补‘元/万元/CNY/截至今天’，不得由寿险来源或中文推断币种")
    return make_envelope(status=EnvelopeStatus.PARTIAL if gaps else EnvelopeStatus.SUCCESS,
        facts=facts, gaps=gaps, hint=hint, source=_SOURCE, request_id=parsed.request_id,
        latency_ms=parsed.latency_ms, data=data, field_descriptions=descriptions)


def expand_detail_fields(codes: list[str]) -> list[str]:
    expanded: list[str] = []
    for code in codes:
        values = expand_basic_info(code)
        expanded.extend(value for value in values if value not in expanded)
    return expanded


def project_detail_focus(envelope: ToolEnvelope, requested: list[str] | None) -> ToolEnvelope:
    """Per-call model view of an unfiltered cached result; never mutate source evidence."""
    if not requested or envelope.status not in {EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL}:
        return envelope
    gaps = list(envelope.gaps)
    facts = envelope.facts
    if envelope.data is not None:
        rows = envelope.data.get("records")
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
            return envelope
        total = envelope.data.get("total")
        incomplete = len(rows) >= 100 or (type(total) is int and total > len(rows))
        # Model-only focus: unknown names are correction notices, not missing
        # business values. Returned extensions and known-but-absent fields keep
        # their existing semantic/null/absence disclosures.
        focused = []
        for spoken in requested:
            code = _FOCUS.get(spoken, resolve_focus(spoken))
            if (spoken in BASIC_INFO_ALIASES or definition(code) is not None
                    or code in _FOCUS.values() or any(path.startswith(code + '.') for path in DEFINITIONS)
                    or any(_path_value(row, code) is not _MISSING for row in cast(list[dict[str, JsonValue]], rows))):
                focused.append(spoken)
            else:
                gaps.append(f'字段未识别:{spoken}')
        gaps.extend(_focus_gaps(cast(list[dict[str, JsonValue]], rows), focused, incomplete))
    else:
        # Legacy adapters return typed scalar facts instead of a source page.
        from app.conf import fields_dict

        dictionary = fields_dict()
        normalized = [(value, dictionary.normalize(value) or (focus if (focus := resolve_focus(value)) in DEFINITIONS else None))
                      for field in requested for value in expand_basic_info(field)]
        codes = expand_detail_fields(list(dict.fromkeys(code for _, code in normalized if code is not None)))
        available = {fact.predicate for fact in facts}
        legacy = {'year_pay_prem': 'premium', 'insurant_name': 'insured_name',
                  'pol_status_desc': 'policy_status', 'pol_product_name': 'product_name'}
        codes = [legacy.get(code, code) if code not in available else code for code in codes]
        gaps.extend(f"字段未返回:{code}" for code in codes if code not in available)
        gaps.extend(f"字段未识别:{value}" for value, code in normalized if code is None)
        facts = [fact for fact in facts if fact.predicate in codes] if codes else facts
    return envelope.model_copy(update={
        "facts": facts, "gaps": list(dict.fromkeys(gaps)),
        "status": EnvelopeStatus.PARTIAL if gaps else envelope.status,
    })
