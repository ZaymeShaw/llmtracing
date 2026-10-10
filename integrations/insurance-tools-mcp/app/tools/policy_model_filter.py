"""保单弃用字段只在模型边界移除；不改变 SDK、原信封或缓存。"""
from __future__ import annotations

import json
import re
from typing import cast

from pydantic import JsonValue

from app.tools.compact_projection import _reject_constant, _unique_object
from tool_adapter_sdk.policy_search_metadata import describe_output_field

RETIRED_POLICY_PATHS = frozenset("""
amount and_insurant_relationship applicant_card_no_cipher applicant_card_type_code
applicant_name_cipher applicant_phoneno_cipher data_flag etl_time insur_term_unit
insurant_card_no_cipher insurant_card_type_code insurant_name_cipher insurant_phoneno_cipher
is_main_emp plan_status plan_status_desc pol_source_flag pol_status prem_method
prem_term_end_date tot_modal_prem_sum
complaint_record.complaint_accept_seq complaint_record.complaint_case_no
complaint_record.complaint_pol_source complaint_record.complaint_polno complaint_record.complaint_reason_code
pay_record.pay_case_no pay_record.pay_case_pay_status pay_record.pay_case_seq
pay_record.pay_case_status pay_record.pay_empno pay_record.pay_pol_source pay_record.pay_polno
pay_record.pay_reason_code pay_record.pay_register_no pay_record.pay_type_code pay_record.pay_type_desc
pol_pay_record.pol_pay_empno pol_pay_record.pol_pay_pol_source pol_pay_record.pol_pay_polno
pos_record.pos_accept_no pos_record.pos_accept_status pos_record.pos_accept_status_desc
pos_record.pos_complete_time pos_record.pos_empno pos_record.pos_entno pos_record.pos_pol_source
pos_record.pos_polno pos_record.pos_type
""".split())
POLICY_MODEL_TOOLS = frozenset({"policy_search", "policy_detail", "run_policy_field_lookup"})
_SEARCH_NAMES = frozenset({"applicant_name_cipher", "insurant_name_cipher"})
_SEARCH_RETIRED = RETIRED_POLICY_PATHS - _SEARCH_NAMES
_CONTAINERS = frozenset(path.split(".")[0] for path in RETIRED_POLICY_PATHS if "." in path)
_OMITTED = object()
_UNSAFE_GAP = "部分保单关联记录无法安全处理，已从模型输出省略；不代表没有相关记录"


def _logical_path(path: str) -> str:
    path = re.sub(r"\[\d*\]", "", path.strip())
    return path.removeprefix("data.").removeprefix("records.")


def _retired_paths(tool_name: str) -> frozenset[str]:
    return _SEARCH_RETIRED if tool_name in {"policy_search", "present_policy_search_card"} else RETIRED_POLICY_PATHS


def is_retired_policy_path(path: str, *, tool_name: str = "policy_detail") -> bool:
    """只认精确逻辑路径，不按子串或叶子同名删除其它业务字段。"""
    return _logical_path(path) in _retired_paths(tool_name)


def filter_policy_fields(fields: list[str] | None, *, tool_name: str = "policy_detail") -> list[str] | None:
    return None if fields is None else [field for field in fields if not is_retired_policy_path(field, tool_name=tool_name)]


def filter_policy_gaps(gaps: list[str], *, tool_name: str = "policy_detail") -> list[str]:
    """仅清理由字段选择产生的固定格式缺口，不扫描任意业务自由文本。"""
    patterns = (
        r"字段(?:未返回|未识别|返回 null/空值|语义未定义):([^；（]+)(?:（险种行 [\d,]+）|；V5 无明确业务定义，不推断当前金额含义)?",
        r"本次源数据未提供明确请求字段 (.+)，不代表该字段为空或不存在",
    )
    result = []
    for gap in gaps:
        matches = [re.fullmatch(pattern, gap) for pattern in patterns]
        if not any(match and is_retired_policy_path(match.group(1), tool_name=tool_name) for match in matches):
            result.append(gap)
    return result


def _filter_value(value: JsonValue, path: str, depth: int = 0) -> JsonValue | object:
    if depth > 64:
        return _OMITTED
    if isinstance(value, str):
        if not value.strip():
            return value
        try:
            decoded = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return _OMITTED
        if not isinstance(decoded, (dict, list)):
            return _OMITTED
        filtered = _filter_value(decoded, path, depth + 1)
        if filtered is _OMITTED:
            return _OMITTED
        # 无弃用字段时连原始空白也保留；发生过滤仍保留字符串承载方式。
        return value if filtered == decoded else json.dumps(filtered, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        items = [_filter_value(item, path, depth + 1) for item in value]
        return _OMITTED if any(item is _OMITTED for item in items) else cast(JsonValue, items)
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if (path + "." + key) not in RETIRED_POLICY_PATHS}
    return value


def _filter_record(record: dict[str, JsonValue], *, tool_name: str = "policy_detail") -> tuple[dict[str, JsonValue], set[str]]:
    result: dict[str, JsonValue] = {}
    omitted: set[str] = set()
    for key, value in record.items():
        if key in _retired_paths(tool_name):
            continue
        filtered = _filter_value(value, key) if key in _CONTAINERS else value
        if filtered is _OMITTED:
            omitted.add(key)
        else:
            result[key] = cast(JsonValue, filtered)
    return result, omitted


def filter_policy_data(
    data: dict[str, JsonValue], descriptions: dict[str, str], *, tool_name: str = "policy_detail",
) -> tuple[dict[str, JsonValue], dict[str, str], list[str]]:
    """对模型页做无副作用投影；只修改 records 中的已声明路径。"""
    result = dict(data)
    omitted_paths: set[str] = set()
    visible_containers: set[str] = set()
    records = data.get("records")
    if isinstance(records, list):
        rows: list[JsonValue] = []
        for index, record in enumerate(records):
            if isinstance(record, dict):
                row, omitted = _filter_record(record, tool_name=tool_name)
                rows.append(row)
                omitted_paths.update(f"records[{index}].{path}" for path in omitted)
                visible_containers.update(key for key in row if key in _CONTAINERS)
            else:
                rows.append(record)
        result["records"] = rows
    kept = {}
    for path, description in descriptions.items():
        record_path = path.removeprefix("data.")
        if not re.match(r"records(?:\[\d*\])?\.", record_path):
            kept[path] = description
            continue
        logical = _logical_path(path)
        # 已删除字段若为扩展对象，其子路径说明也不能留在模型视图。
        retired = any(logical == item or logical.startswith(item + ".") for item in _retired_paths(tool_name))
        hidden = any(record_path == item or record_path.startswith(item + ".") or record_path.startswith(item + "[")
                     for item in omitted_paths)
        if not re.search(r"\[\d+\]", record_path):
            hidden = hidden or any(
                (logical == container or logical.startswith(container + "."))
                and container not in visible_containers
                and any(item.endswith("." + container) for item in omitted_paths)
                for container in _CONTAINERS
            )
        if not retired and not hidden:
            kept[path] = description
    filtered_rows = result.get("records")
    if tool_name == "policy_search" and isinstance(filtered_rows, list):
        # 兼容缓存/旧信封缺说明的情况，不能把重新允许的姓名密文当作无标记姓名。
        for filtered_row in filtered_rows:
            if isinstance(filtered_row, dict):
                for key in _SEARCH_NAMES | {"applicantNameCipher", "insurantNameCipher"}:
                    if key in filtered_row:
                        kept[f"records[].{key}"] = describe_output_field(key)
    return result, kept, [_UNSAFE_GAP] if omitted_paths else []


def filter_policy_legacy_value(
    predicate: str, value: JsonValue, *, tool_name: str = "policy_detail",
) -> tuple[bool, JsonValue, list[str]]:
    """兼容 facts-only 结果；仅解析已知记录载体，不检查普通业务文本。"""
    if is_retired_policy_path(predicate, tool_name=tool_name):
        return False, None, []
    logical = _logical_path(predicate)
    if logical in _CONTAINERS:
        filtered = _filter_value(value, logical)
        return (False, None, [_UNSAFE_GAP]) if filtered is _OMITTED else (True, cast(JsonValue, filtered), [])
    if predicate in {"policy_risk_record", "policy_search_record"}:
        prefix = ""
        source = value
        if isinstance(value, str):
            label = "保单(?:险种|搜索)" if predicate == "policy_search_record" else "险种"
            match = re.match(r"records\[\d+\] 原始" + label + "记录：", value)
            if match:
                prefix, source = match.group(), value[match.end():]
            try:
                source = json.loads(str(source), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                return False, None, [_UNSAFE_GAP]
        if not isinstance(source, dict):
            return False, None, [_UNSAFE_GAP]
        filtered_record, omitted = _filter_record(source, tool_name=tool_name)
        output: JsonValue = filtered_record
        if isinstance(value, str):
            output = value if filtered_record == source else prefix + json.dumps(
                filtered_record, ensure_ascii=False, separators=(",", ":"),
            )
        return True, output, [_UNSAFE_GAP] if omitted else []
    return True, value, []
