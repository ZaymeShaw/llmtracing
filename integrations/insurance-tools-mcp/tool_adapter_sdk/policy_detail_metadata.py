"""保单详情的字段说明和关注点解析；不影响其他工具的全局词典。"""
from __future__ import annotations

import re

from pydantic import JsonValue

from tool_adapter_sdk.policy_detail_catalog import SHEET, V5_FIELDS, FieldDefinition
from tool_adapter_sdk.output_descriptions import describe_output_page
from tool_adapter_sdk.search_metadata import ROW_AMOUNT_FIELDS, ROW_AMOUNT_NOTE

SOURCE_ALIASES = {"undwr_date": "undwrt_date", "first_period_prem": "frist_period_prem"}
DEFINITIONS = {field.path: field for field in V5_FIELDS}
_AMOUNT_PATHS = {
    "sum_ins", "year_pay_prem", "total_modal_prem", "frist_period_prem", "agg_paid_prem",
    "agg_pay_amount", "pay_record.pay_amount", "agg_receive_dividend", "nodrw_take_etamt", "aldrw_take_etamt",
}
_FOCUS_ALIASES = {
    "insured_name": "insurant_name", "policy_status": "pol_status_desc",
    "product_name": "pol_product_name", "被保人": "insurant_name", "投保人": "applicant_name",
    "保额": "sum_ins", "险种保额": "sum_ins", "保单状态": "pol_status_desc",
    "被保人姓名": "insurant_name", "投保人姓名": "applicant_name",
    "年缴保费": "year_pay_prem", "年交保费": "year_pay_prem", "累计已交保费": "agg_paid_prem",
    "交费期间": "prem_term", "缴费期间": "prem_term", "理赔记录": "pay_record",
}


def is_amount_field(path: str) -> bool:
    """Read-only field classification; it supplies no currency or business date."""
    return SOURCE_ALIASES.get(path, path) in _AMOUNT_PATHS


def definition(path: str) -> FieldDefinition | None:
    return DEFINITIONS.get(SOURCE_ALIASES.get(path, path))


def describe_output_field(path: str) -> str:
    """直接复用 V5 业务含义和枚举，不拼接开发溯源和重复约束。"""
    entry = definition(path)
    if entry is None:
        if any(field.path.startswith(path + ".") for field in V5_FIELDS):
            return "保单关联业务记录"
        return "业务含义待确认，不据字段名推断"
    text = entry.label
    if entry.meaning and entry.meaning != entry.label:
        text += "；" + entry.meaning
    if entry.domain:
        text += "；取值：" + entry.domain
    if entry.path in {"wide_deadline", "revival_term", "cooling_off"}:
        text += "；上述期限为一般说明，具体以本保单为准"
    if entry.path in ROW_AMOUNT_FIELDS:
        text += "；" + ROW_AMOUNT_NOTE
    return text


def describe_output_data(data: dict[str, JsonValue]) -> dict[str, str]:
    """模型返回专用短释义；保单行与险种行的统计口径保持区分。"""
    return describe_output_page(
        data, describe_field=describe_output_field,
        canonical_path=lambda path: SOURCE_ALIASES.get(path, path), defined_paths=DEFINITIONS,
        page_labels={"pageNo": "当前页码", "pageSize": "服务页容量",
                     "total": "险种行总数，不是保单张数", "records": "当前页全部险种记录"},
        record_description="单条险种记录；不可跨行直接汇总为保单总额；空值不等于零或否；金额未注明单位、币种或时点时不推断，也不改作现金价值",
    )


def describe_field(path: str) -> str:
    entry = definition(path)
    if entry is None:
        children = [f for f in V5_FIELDS if f.path.startswith(path + ".")]
        if children:
            # 在父 description 中展开子路径语义，不为宽松源容器新增结构或类型约束。
            child_descriptions = "；".join(f"{field.path}：{describe_field(field.path)}" for field in children)
            return f"嵌套业务记录容器；V5 定义子字段：{child_descriptions}；保留原始对象、数组或 JSON 字符串格式。"
        return f"原始业务字段 {path}；V5 未定义业务含义，原值保留，不据字段名推断含义。"
    parts = [entry.label, f"V5 {SHEET}第 {entry.row} 行", f"逻辑类型：{entry.logical_type}"]
    if entry.meaning:
        parts.append(entry.meaning)
    if entry.domain:
        parts.append(f"枚举/取值：{entry.domain}")
    if path in SOURCE_ALIASES:
        parts.append(f"字典路径 {entry.path}，接口原键 {path} 保持不变")
    if entry.path in _AMOUNT_PATHS:
        parts.append("仅按原字段原值表达；币种和业务时点未提供时禁止自行补元/万元/CNY/截至今天，不从寿险来源或中文推断币种，不作为保单合计或现金价值")
    parts.append("原始 JSON 类型和值保持不变，null/空值不代表零或否")
    return "；".join(parts)


def resolve_focus(value: str) -> str:
    text = value.strip()
    if not text:
        return text
    if text in _FOCUS_ALIASES:
        return _FOCUS_ALIASES[text]
    if text in SOURCE_ALIASES:
        return text
    for entry in V5_FIELDS:
        aliases = [alias.strip() for alias in re.split(r"[、,，;；/\n]", entry.aliases) if alias.strip()]
        if text in (entry.path, entry.label, *aliases):
            return entry.path
    return text


def describe_data(data: dict[str, JsonValue]) -> dict[str, str]:
    """[] 表示数组所有元素；源 JSON 字符串不解析，但补齐已知内部路径说明。"""
    descriptions: dict[str, str] = {}
    page_labels = {"pageNo": "当前页码", "pageSize": "服务页容量", "total": "服务报告的险种行总数（不是保单张数）", "records": "当前页全部险种记录，按接口顺序保留"}

    def walk(value: JsonValue, path: str, logical_path: str | None) -> None:
        descriptions[path] = page_labels.get(path, "") or describe_field(logical_path or path)
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{logical_path}.{key}" if logical_path else key
                walk(item, f"{path}.{key}", child)
        elif isinstance(value, list):
            descriptions[path + "[]"] = descriptions[path] + "；数组元素"
            for item in value:
                walk(item, path + "[]", logical_path)
        elif isinstance(value, str) and logical_path:
            for entry in V5_FIELDS:
                if entry.path.startswith(logical_path + "."):
                    suffix = entry.path[len(logical_path):]
                    descriptions[path + "[]" + suffix] = describe_field(entry.path) + "；源字段为字符串，未改写或解析其内容"

    for key, value in data.items():
        if key == "records" and isinstance(value, list):
            descriptions[key] = page_labels[key]
            descriptions["records[]"] = "单条险种原始业务记录；不能将多行任意汇总为保单总额"
            for record in value:
                if isinstance(record, dict):
                    for field, item in record.items():
                        walk(item, f"records[].{field}", field)
        else:
            walk(value, key, key)
    return descriptions
