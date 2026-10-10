"""业务工具的模型边界：源数据只出现一次，审计信封只留在应用内部。"""
from __future__ import annotations

import json
import re
from collections import Counter

from pydantic import JsonValue

from app.core.envelope import TOOL_FAILURE_ANSWERS
from app.core.tool_control import control_message
from app.core.tool_ledger import record, visible_business_results
from app.schemas import EnvelopeStatus, ToolEnvelope
from app.tools.compact_projection import project_compact_data
from app.tools.customer_detail_projection import CANDIDATE_FIELDS, CANDIDATES_ONLY_GAP
from app.tools.policy_detail_projection import project_detail_focus
from app.tools.policy_model_filter import (
    POLICY_MODEL_TOOLS, filter_policy_data, filter_policy_fields, filter_policy_gaps,
    filter_policy_legacy_value,
)

BUSINESS_TOOL_NAMES = frozenset({
    "customer_search", "policy_search", "customer_detail", "policy_detail",
    "run_policy_field_lookup",
})

# Agent 漏判恢复只增加“呈现语义”，不增加第二套业务实现：终止型工具与既有
# 搜索工具共享同一 ToolEnvelope 契约、模型投影和本轮来源账本。
TERMINAL_SEARCH_TOOLS = {
    "present_customer_search_card": "customer_search",
    "present_policy_search_card": "policy_search",
}


def canonical_business_tool_name(tool_name: str) -> str:
    """呈现型别名归一到真实业务工具名；未知工具保持原名。"""
    return TERMINAL_SEARCH_TOOLS.get(tool_name, tool_name)


def record_business_tool_result(budget: dict, tool_name: str, raw: str, *, envelope: ToolEnvelope | None = None) -> None:
    """仅记录本轮实际提供给模型的来源，缓存、身份和事实仍使用原信封。"""
    tool_name = canonical_business_tool_name(tool_name)
    if tool_name not in BUSINESS_TOOL_NAMES:
        return
    if envelope is None:
        envelope = ToolEnvelope.model_validate_json(raw)
    record(budget, tool_name, raw, envelope=envelope, visible=True)


def business_results(budget: dict) -> dict[str, list[ToolEnvelope]]:
    """只投影本轮已展示的解析信封；不另存原始字符串列表。"""
    return visible_business_results(budget, BUSINESS_TOOL_NAMES)


def _descriptions(descriptions: dict[str, str]) -> dict[str, str]:
    # 只集中明确全局适用的约束；相同业务含义、容器类型仍必须留在各自路径。
    # Indexed descriptions identical to shared path definitions are redundant.
    # Keep indexed conflicts and qualifiers verbatim.
    descriptions = {path: text for path, text in descriptions.items()
                    if path == (shared_path := re.sub(r"\[\d+\]", "[]", path))
                    or descriptions.get(shared_path) != text}
    parts = {path: list(dict.fromkeys(text.split("；"))) for path, text in descriptions.items()}
    counts = Counter(part for values in parts.values() for part in values if part)
    common = {
        "原始 JSON 类型和值保持不变，null/空值不代表零或否",
        "原始 JSON 类型和值不转换，null/空值不等于零或否",
        "文档含搜索筛选口径，不据此反推客户当前状态",
        "共享路径说明",
        "字典定义不代表每条记录均返回该字段，实际类型和值以 data 为准",
    }
    # 源页允许未知扩展；若已有同名路径，不用元说明覆盖业务字段的说明。
    shared = {part for part, count in counts.items() if count > 1 and part in common} if "$notes" not in descriptions else set()
    result = {path: "；".join(part for part in values if part not in shared)
              for path, values in parts.items()}
    if shared:
        result["$notes"] = "；".join(part for part in counts if part in shared)
    return result


def _contains_null(value: JsonValue, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_null(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_contains_null(item, depth + 1) for item in value)
    return False


def _summary_gap(gap: str) -> bool:
    return gap == "当前展示不包含完整客户集合，请补充条件缩小范围" or bool(re.fullmatch(
        r"当前页去重 \d+ 张保单，仅展示前 \d+ 张，请缩小查询范围", gap,
    ))


def _nest_descriptions(descriptions: dict[str, str]) -> dict[str, JsonValue]:
    """仅折叠说明路径；保留原文，冲突或不省字符时沿用扁平字典。"""
    flat: dict[str, JsonValue] = dict(descriptions)
    nested: dict[str, JsonValue] = {}
    for path, description in descriptions.items():
        segments = path.split(".")
        # 保留键不能与真实路径混淆；限制展开深度，避免扩展字段制造深层 JSON。
        if len(segments) > 64 or any(not part or part == "$description" for part in segments):
            return flat
        node = nested
        for segment in segments[:-1]:
            child = node.get(segment)
            if isinstance(child, str):
                child = {"$description": child}
                node[segment] = child
            elif child is None:
                child = {}
                node[segment] = child
            assert isinstance(child, dict)
            node = child
        leaf = node.get(segments[-1])
        if isinstance(leaf, dict):
            leaf["$description"] = description
        else:
            node[segments[-1]] = description
    compact = json.dumps(nested, ensure_ascii=False, separators=(",", ":"))
    original = json.dumps(flat, ensure_ascii=False, separators=(",", ":"))
    return nested if len(compact) < len(original) else flat


_FIELD_GAP_PATTERNS = (
    (re.compile(r"字段未返回:([^；（]+)(?:（险种行 ([\d,]+)）)?"), "missing"),
    (re.compile(r"字段返回 null/空值:([^；（]+)(?:（险种行 ([\d,]+)）)?"), "empty"),
    (re.compile(r"字段语义未定义:([^；（]+)(?:；.*)?"), "undefined"),
    (re.compile(r"本次源数据未提供明确请求字段 (.+)，不代表该字段为空或不存在"), "absent"),
)
_UNKNOWN_FIELD = re.compile(r"字段未识别:(.+)")
_GAP_TEXT = {"missing": "未返回", "absent": "本次源数据未提供", "empty": "返回空值",
             "undefined": "字段含义未定义，不推断金额含义"}


def _field_label(tool_name: str, code: str) -> str:
    """只读 SDK 已登记的业务名；未登记时保留原字段名，不据字段名推断。"""
    logical = re.sub(r"\[\d*\]", "", code).removeprefix("data.").removeprefix("records.")
    try:
        if tool_name in {"policy_detail", "run_policy_field_lookup"}:
            from tool_adapter_sdk.policy_detail_metadata import definition
            entry = definition(logical)
        else:
            from tool_adapter_sdk import customer_detail_metadata, customer_search_metadata, policy_search_metadata
            module = {"policy_search": policy_search_metadata, "customer_search": customer_search_metadata,
                      "customer_detail": customer_detail_metadata}.get(tool_name)
            entry = None if module is None else module.DEFINITIONS.get(module.SOURCE_ALIASES.get(logical, logical))
    except Exception:  # noqa: BLE001 - 说明元数据缺失只降级为原字段名
        entry = None
    label = getattr(entry, "label", "") or ""
    return f"{label}（{code}）" if label and label != code else code


def readable_field_gaps(gaps: list[str], *, tool_name: str) -> list[str]:
    """模型视图专用：把字段级内部缺口合并为每字段一条业务可读说明。

    内部 gaps 格式是渲染与弃用字段过滤的约定，原信封不改；这里只改模型看到的文本。
    """
    fields: dict[str, list[str]] = {}
    unknown: list[str] = []
    result: list[str] = []
    order: list[str] = []
    for gap in gaps:
        match = _UNKNOWN_FIELD.fullmatch(gap)
        if match:
            unknown.append(match.group(1))
            if "$unknown" not in order:
                order.append("$unknown")
            continue
        for pattern, kind in _FIELD_GAP_PATTERNS:
            match = pattern.fullmatch(gap)
            if match:
                code = match.group(1).strip()
                rows = match.group(2) if match.lastindex and match.lastindex >= 2 else None
                text = _GAP_TEXT[kind] if not rows else f"险种行 {rows} {_GAP_TEXT[kind]}"
                if code not in fields:
                    fields[code] = []
                    order.append(code)
                if text not in fields[code]:
                    fields[code].append(text)
                break
        else:
            order.append(gap)
    for key in order:
        if key == "$unknown":
            names = "、".join(dict.fromkeys(unknown))
            result.append(f"未识别的字段名（展示关注）：{names}；查询已执行，请按默认视图作答或改用工具说明中的字段名")
        elif key in fields:
            detail = "，".join(fields[key])
            notes = [note for marker, note in (("未返回", "未返回不代表为空或不存在"),
                                               ("未提供", "未提供不代表为空或不存在"),
                                               ("返回空值", "空值不代表零或否")) if marker in detail]
            suffix = "；" + "，".join(notes) if notes else ""
            result.append(f"{_field_label(tool_name, key)}：{detail}{suffix}")
        else:
            result.append(key)
    return list(dict.fromkeys(result))


def _drop_candidate_placeholders(
    data: dict[str, JsonValue], descriptions: dict[str, str],
) -> tuple[dict[str, JsonValue], dict[str, str]]:
    """多客户候选只查了四项基本信息；模型视图不展示未查询字段的 null/空数组占位。"""
    records = data.get("records")
    if not isinstance(records, list):
        return data, descriptions
    rows: list[JsonValue] = []
    kept: set[str] = set()
    for row in records:
        if isinstance(row, dict):
            row = {key: value for key, value in row.items()
                   if key in CANDIDATE_FIELDS or value not in (None, [], {}, "")}
            kept.update(row)
        rows.append(row)
    placeholders = {key for row in records if isinstance(row, dict) for key in row} - kept

    def visible(path: str) -> bool:
        match = re.match(r"records(?:\[\d*\])?\.([^.\[\]]+)", path)
        return match is None or match.group(1) not in placeholders

    return ({**data, "records": rows},
            {path: text for path, text in descriptions.items() if visible(path)})


def project_business_tool_result(
    tool_name: str, raw: str, *, compact: bool = True,
    fields: list[str] | None = None, query: str = "", envelope: ToolEnvelope | None = None,
) -> str:
    """投影合法内部信封；其它工具保持原契约，非法业务信封仍显式失败。"""
    tool_name = canonical_business_tool_name(tool_name)
    if tool_name not in BUSINESS_TOOL_NAMES:
        return raw
    envelope = envelope or ToolEnvelope.model_validate_json(raw)
    source_status = envelope.status
    if tool_name == "policy_detail":
        envelope = project_detail_focus(envelope, fields)
    result: dict = {}
    notices: list[str] = []
    gaps = list(envelope.gaps)
    policy = tool_name in POLICY_MODEL_TOOLS
    if envelope.data is not None:
        data = envelope.data
        descriptions = envelope.field_descriptions or {}
        if policy:
            data, descriptions, retired_gaps = filter_policy_data(data, descriptions, tool_name=tool_name)
            gaps.extend(retired_gaps)
            fields = filter_policy_fields(fields, tool_name=tool_name)
        if compact:
            data, descriptions, projection_gaps = project_compact_data(
                tool_name, data, descriptions, fields=fields, query=query,
            )
            if tool_name in {'policy_search', 'customer_search'}:
                for gap in projection_gaps:
                    if gap.startswith('本次源数据未提供明确请求字段 '):
                        from app.core.policy_fields import unavailable_field_message
                        unavailable = unavailable_field_message([gap.removeprefix('本次源数据未提供明确请求字段 ').strip()])
                        notices.append(unavailable or gap + f'；如需核实请使用 {tool_name.replace("search", "detail")}')
                    else:
                        gaps.append(gap)
            else:
                gaps.extend(projection_gaps)
        if tool_name == "customer_detail" and CANDIDATES_ONLY_GAP in gaps:
            data, descriptions = _drop_candidate_placeholders(data, descriptions)
        result["data"] = data
        if compact and _contains_null(envelope.data):
            notices.append('精简视图省略非指定的 null 字段，明确请求的空值保留；省略和空值均不代表零、否或不存在。')
        if compact and tool_name in {'policy_search', 'customer_search'} and 'total' in data:
            notices.append('total 是源页结果条数，不用展示行数代替。' +
                ('保单张数只使用 execution.distinct_policies；险种行数不等于不同保单数。'
                 if tool_name == 'policy_search' else '仅代表本次客户筛选的结果。'))
        result["field_descriptions"] = _descriptions(descriptions)
        gaps = [gap for gap in gaps if not _summary_gap(gap)]
        records = envelope.data.get("records")
        total = envelope.data.get("total")
        page = envelope.data.get("pageNo", 1)
        if isinstance(records, list) and type(total) is int and (total > len(records) or page != 1):
            gaps.append("仅返回当前业务页，尚未取回完整结果集合")
    elif envelope.facts:
        # 旧适配器和尾号候选没有源页；只保留业务值及标准主键，不暴露引用包装。
        legacy_records = []
        for fact in envelope.facts:
            value = fact.typed_value
            if policy:
                keep, filtered_value, retired_gaps = filter_policy_legacy_value(fact.predicate, value, tool_name=tool_name)
                gaps.extend(retired_gaps)
                if not keep:
                    continue
            else:
                filtered_value = value
            if fact.predicate == 'policy_candidate':
                # Raw candidate facts remain available for the Direct clarification path.
                legacy_records.append({'polno': fact.subject_id, 'label': filtered_value})
                continue
            record = {"subject_type": fact.subject_type, "subject_id": fact.subject_id,
                      "field": fact.predicate, "value": filtered_value}
            if fact.unit is not None:
                record["unit"] = fact.unit
            if fact.as_of is not None:
                record["as_of"] = fact.as_of
            legacy_records.append(record)
        result["data"] = {"records": legacy_records}
        result["field_descriptions"] = {
            "records[].subject_id": "工具返回的标准主体标识，客户为内部 customer_id，保单为保单号",
            "records[].field": "兼容结果的业务字段名", "records[].value": "对应业务原值",
        }
        if all(fact.predicate == 'policy_candidate' for fact in envelope.facts):
            result['field_descriptions'] = {'records[].polno': '候选完整保单号',
                                            'records[].label': '候选原始说明，仅用于确认目标'}
        if tool_name == "policy_search":
            result["field_descriptions"]["records[].value"] += (
                "；applicantNameCipher/insurantNameCipher 及对应蛇形姓名字段名含 Cipher 仅为接口命名，"
                "值可读即按原值展示，不自行解密或推断仍加密"
            )
    if policy:
        gaps = filter_policy_gaps(gaps, tool_name=tool_name)
    notices.extend(gap for gap in gaps if _UNKNOWN_FIELD.fullmatch(gap))
    gaps = [gap for gap in gaps if not _UNKNOWN_FIELD.fullmatch(gap)]
    notices = readable_field_gaps(notices, tool_name=tool_name)
    gaps = readable_field_gaps(gaps, tool_name=tool_name)
    execution_status = source_status
    if (tool_name in {'policy_search', 'customer_search'} and execution_status is EnvelopeStatus.PARTIAL
            and envelope.gaps and not gaps and all(_summary_gap(gap) or _UNKNOWN_FIELD.fullmatch(gap) for gap in envelope.gaps)):
        execution_status = EnvelopeStatus.SUCCESS
    if (execution_status is EnvelopeStatus.PARTIAL and envelope.gaps
            and all(_UNKNOWN_FIELD.fullmatch(gap) for gap in envelope.gaps)
            and not gaps and (envelope.facts or envelope.data is not None and envelope.data.get('records'))):
        # A successfully returned page with only an unknown-field notice is usable.
        # Genuine partial pages, missing values and error/control paths stay partial.
        execution_status = EnvelopeStatus.SUCCESS
    execution: dict = {"status": execution_status.value}
    if envelope.error is not None:
        # retryable 是应用内部重试口径；模型本轮不应原样重试，模型视图不再暴露。
        execution = {"status": envelope.status.value, "code": envelope.error.code.value,
                     "message": TOOL_FAILURE_ANSWERS[envelope.error.code]}
    elif envelope.status not in {EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL}:
        execution = {"status": envelope.status.value}
        if envelope.hint:
            execution["message"] = envelope.hint
    elif envelope.data is None and envelope.hint:
        execution = {"status": envelope.status.value, "message": envelope.hint}
    if envelope.control is not None:
        execution["control"] = envelope.control.model_dump(mode="json")
        execution["message"] = control_message(envelope.control, envelope.hint)
    if gaps and envelope.error is None and envelope.control is None:
        if execution_status is EnvelopeStatus.SUCCESS:
            execution["status"] = "PARTIAL"
        execution["gaps"] = list(dict.fromkeys(gaps))
    if tool_name == "policy_search" and envelope.data is not None:
        rows = envelope.data.get("records")
        total = envelope.data.get("total")
        complete = (isinstance(rows, list) and type(total) is int and total == len(rows)
                    and envelope.data.get("pageNo", 1) == 1 and not envelope.data.get("truncated"))
        refs = [row.get("polno") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        if complete and isinstance(rows, list) and len(refs) == len(rows) and all(isinstance(ref, str) and ref.strip() for ref in refs):
            execution["distinct_policies"] = len(set(refs))
        else:
            notices.append("保单张数未确认；total 是查询结果条数，不能代替保单张数")
    if notices:
        execution["notes"] = notices
    if execution:
        result["execution"] = execution
    if "field_descriptions" in result:
        result["field_descriptions"] = _nest_descriptions(result["field_descriptions"])
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
