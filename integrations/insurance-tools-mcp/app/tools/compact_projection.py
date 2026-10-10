"""仅在模型出口裁剪业务页；不修改内部源值、事实或字段目录。"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from functools import lru_cache
from typing import cast

from pydantic import JsonValue

from tool_adapter_sdk.customer_detail_metadata import DEFINITIONS as CUSTOMER_FIELDS
from tool_adapter_sdk.customer_detail_metadata import SOURCE_ALIASES as CUSTOMER_ALIASES
from tool_adapter_sdk.customer_detail_metadata import canonical_path
from tool_adapter_sdk.customer_detail_v5 import DEFINITIONS as DETAIL_FIELDS
from tool_adapter_sdk.policy_detail_metadata import DEFINITIONS as POLICY_FIELDS
from tool_adapter_sdk.policy_detail_metadata import SOURCE_ALIASES, resolve_focus
from tool_adapter_sdk import customer_search_metadata, policy_search_metadata
from tool_adapter_sdk.json_values import reject_constant as _reject_constant, unique_object as _unique_object

_CUSTOMER_DEFAULT = frozenset("""
idicm_clientno clientno searchclientname clientsex clientage clientbirthday clientmobile
isbuyinsurance marists education profname assetscondition viptype planabbrnames
""".split())
_POLICY_DEFAULT = frozenset("""
polno pol_source pol_product_name brno plan_code plan_full_name plan_abbr_name
main_subsidiary_risks_flag pol_status_desc is_valid_pol applicant_name insurant_name
applicant_clientno applicant_idicm_clientno insurant_clientno insurant_idicm_clientno
app_ins_relationship_desc insure_date pol_effective_date_term insur_term security_period
matu_date security_period_end_date prem_term prem_method_desc pay_to_day sum_ins
year_pay_prem total_modal_prem agg_paid_prem
""".split())
_META = frozenset({"currency", "unit", "as_of"})
_PAGE = frozenset({"pageNo", "pageSize", "total", "truncated"}) | _META
_RELATED = {
    "familyInfoList": {"familyClientMainId", "familyClientNameCipher", "familyRelation"},
    "clientDynamicInfoList": {"clientMainId", "activityId", "activityDate", "activityCode"},
    "clientNoteInfoList": {"clientMainId", "noteId", "dateRemark", "noteType", "noteTypeValue"},
    "familyinfo": {"familyclientname", "familyrelation"},
    "polnoinfo": {"polno"},
    "polnoinfo.plancodeinfo": {"plancode", "plan_code", "abbrname", "planfullname", "insname"},
    "polnoinfo.claimdatainfo": {"claimno", "claimplancodename"},
    "pay_record": {"claimno", "pay_case_no", "pay_register_date", "plan_code", "brno"},
}
_UNKNOWN = "业务含义待确认，不据字段名推断"
_POLICY_FOCUS = {"premium": "year_pay_prem", "insured_name": "insurant_name",
                 "policy_status": "pol_status_desc", "product_name": "pol_product_name",
                 "现金价值": "cash_value", "现价": "cash_value", "账户价值": "cash_value",
                 "cash_value": "cash_value"}


def _logical(path: str) -> str:
    path = re.sub(r"\[\d*\]", "", path)
    return path.removeprefix("records.")


def project_compact_data(
    tool_name: str, data: dict[str, JsonValue], descriptions: dict[str, str], *,
    fields: list[str] | None = None, query: str = "",
) -> tuple[dict[str, JsonValue], dict[str, str], list[str]]:
    """白名单加明确字段关注点；只返回可见路径说明及必要投影缺口。"""
    customer = tool_name.startswith("customer_")
    source_rows = data.get("records")
    legacy_replay = isinstance(source_rows, list) and bool(source_rows) and all(
        isinstance(row, dict) and "idicm_clientno" in row and "clientMainId" not in row for row in source_rows)
    detail = tool_name == "customer_detail" and not legacy_replay
    catalog = DETAIL_FIELDS if detail else CUSTOMER_FIELDS if customer else POLICY_FIELDS
    aliases = {} if detail else CUSTOMER_ALIASES if customer else SOURCE_ALIASES
    search = tool_name in {"customer_search", "policy_search"}
    search_metadata = customer_search_metadata if customer else policy_search_metadata
    if search:
        catalog = search_metadata.DEFINITIONS
        aliases = search_metadata.SOURCE_ALIASES

    @lru_cache(maxsize=2048)
    def canonical(path: str) -> str:
        path = _logical(path)
        if search:
            return search_metadata.canonical_path(path)
        return path if detail else canonical_path(path) if customer else SOURCE_ALIASES.get(path, path)

    names: dict[str, set[str]] = {}
    for path, entry in catalog.items():
        for name in (path, entry.label, *re.split(r"[、,，;；/\n]", entry.aliases)):
            if name.strip():
                names.setdefault(name.strip(), set()).add(canonical(path))
    for alias, path in aliases.items():
        names.setdefault(alias, set()).add(canonical(path))
    if not customer:
        for alias, path in _POLICY_FOCUS.items():
            names.setdefault(alias, set()).add(canonical(path))
    common_names = ({'手机号': 'clientmobile', '手机': 'clientmobile', '学历': 'education', '职业': 'profname',
                     '姓名': 'searchclientname', '生日': 'clientbirthday', 'VIP': 'vipType', '客户等级': 'newValueLabel'}
                    if customer else {'状态': 'pol_status_desc', '被保人': 'insurant_name', '投保人': 'applicant_name',
                                      '保额': 'sum_ins', '交费期间': 'prem_term', '缴费期间': 'prem_term'})
    if detail:
        common_names = {'手机号': 'phoneNoCipher', '手机': 'phoneNoCipher', '学历': 'education',
                        '姓名': 'clientNameCipher', '生日': 'birthday', 'VIP': 'vipType', '客户等级': 'newValueLabel'}
    for alias, path in common_names.items():
        names.setdefault(alias, set()).add(canonical(path))

    requested: set[str] = set()
    for field in fields or []:
        name = _logical(field.strip())
        groups = {"家庭信息": {"familyinfo"}, "理赔记录": {"polnoinfo.claimdatainfo"},
                  "权益": {path for path in catalog if "membergradeinfo." in path}} if customer else {
                      "理赔记录": {"pay_record"}, "premium": {"year_pay_prem"},
                      "保费": {"year_pay_prem"}, "现金价值": {"cash_value"},
                      "policy_basic_info": set(_POLICY_DEFAULT), "保单基本信息": set(_POLICY_DEFAULT),
                  }
        if detail:
            groups = {"家庭信息": {"familyInfoList"}, "客户动态": {"clientDynamicInfoList"}, "客户笔记": {"clientNoteInfoList"}}
        if name in groups:
            requested.update(groups[name])
            continue
        if name:
            focused = search_metadata.resolve_focus(name) if search else name if customer else resolve_focus(name)
            requested.update(names.get(name, {canonical(focused)}))
    for name, paths in (names.items() if not (search and fields) else []):
        # 自然语言仅命中完整字段标签/别名；“保单”“客户”等泛词不展开目录。
        if (len(name) >= 3 or name in _POLICY_FOCUS) and (re.search(
                r"(?<![A-Za-z0-9_.])" + re.escape(name) + r"(?![A-Za-z0-9_.])", query)
                               if name.isascii() else name in query):
            requested.update(paths)
    requested = {canonical(path) for path in requested}
    selected = set("clientMainId clientNo clientNameCipher clientSex clientAge birthday phoneNoCipher education eduDescription mariSts mariDescription annPremAmt insnoSeqAmt".split()) if detail else {canonical(path) for path in (_CUSTOMER_DEFAULT if customer else _POLICY_DEFAULT)}
    known_requested = {path for path in requested if path in catalog or any(key.startswith(path + '.') for key in catalog)}
    if search and fields and known_requested:
        # Narrow existing source page; never change RPC filtering or request a detail.
        selected = {canonical(path) for path in (
            {'idicm_clientno', 'clientno', 'clientMainId', 'clientNo', 'searchclientname'} if customer
            else {'polno', 'brno', 'plan_code', 'insurant_name', 'applicant_name', 'pol_product_name', 'plan_abbr_name'})}
    for path in requested:
        children = {key for key in catalog if key.startswith(path + ".")}
        selected.update(children or {path})
    related = dict(_RELATED)
    if search and customer:
        related["familyInfo"] = {"familyClientMainId", "familyClientNameCipher", "familyClientSex", "familyRelation"}
    for parent, keys in related.items():
        parent = canonical(parent)
        if any(path.startswith(parent + ".") for path in selected):
            selected.update(canonical(parent + "." + key) for key in keys)

    gaps: list[str] = []
    child_parents = {'.'.join(path.split('.')[:index]) for path in selected for index in range(1, len(path.split('.')))}
    visible: set[str] = set()
    found: set[str] = set()
    parsed_paths: set[str] = set()
    omitted = object()

    def compact_value(value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            return {key: compact_value(item) for key, item in value.items() if item is not None}
        if isinstance(value, list):
            return [compact_value(item) for item in value]
        return deepcopy(value)

    def mark_visible(value: JsonValue, path: str, depth: int = 0) -> None:
        visible.add(path)
        if depth >= 64:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                mark_visible(item, path + "." + key, depth + 1)
        elif isinstance(value, list):
            visible.add(path + "[]")
            for index, item in enumerate(value):
                mark_visible(item, f"{path}[{index}]", depth + 1)

    def walk(value: JsonValue, logical: str, path: str, depth: int = 0) -> JsonValue | object:
        key = canonical(logical)
        exact = (key in selected or logical.rsplit(".", 1)[-1] in _META
                 or (key.endswith("_unit") and key[:-5] in selected))
        children = key in child_parents
        if not exact and not children:
            return omitted
        if depth > 64:
            gaps.append(f"精简模式未展示 {path}：嵌套层级过深")
            return omitted
        if isinstance(value, str) and children:
            if not value.strip():
                visible.add(path)
                found.add(key)
                return value
            try:
                decoded = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                decoded = None
            if not isinstance(decoded, (dict, list)):
                gaps.append(f"精简模式未展示 {path}：关联记录字符串无法安全解析")
                return omitted
            projected = walk(decoded, logical, path, depth + 1)
            if projected is omitted:
                return omitted
            parsed_paths.add(path)
            return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        found.add(key)  # null is present, not a missing requested field.
        if value is None:
            if key in requested or any(key.startswith(parent + '.') or parent.startswith(key + '.') for parent in requested):
                visible.add(path)
                return None
            return omitted
        visible.add(path)
        if isinstance(value, dict) and children:
            result: dict[str, JsonValue] = {}
            for child, item in value.items():
                projected = walk(item, logical + "." + child, path + "." + child, depth + 1)
                if projected is not omitted:
                    result[child] = cast(JsonValue, projected)
            return result
        if isinstance(value, list) and children:
            visible.add(path + "[]")
            result_list: list[JsonValue] = []
            for index, item in enumerate(value):
                projected = walk(item, logical, f"{path}[{index}]", depth + 1)
                # 保持行序和位置；失败行不透传原大块字符串。
                result_list.append(None if projected is omitted else cast(JsonValue, projected))
            return result_list
        value = compact_value(value)
        mark_visible(value, path)
        return value

    result: dict[str, JsonValue] = {key: deepcopy(value) for key, value in data.items() if key in _PAGE}
    visible.update(result)
    records = data.get("records")
    if isinstance(records, list):
        rows: list[JsonValue] = []
        visible.update({"records", "records[]"})
        for index, record in enumerate(records):
            visible.add(f"records[{index}]")
            row: dict[str, JsonValue] = {}
            if isinstance(record, dict):
                for key, value in record.items():
                    projected = walk(value, key, f"records[{index}].{key}")
                    if projected is not omitted:
                        row[key] = cast(JsonValue, projected)
            else:
                gaps.append(f"精简模式未展示 records[{index}]：业务行不是对象")
            rows.append(row)
        result["records"] = rows
    elif "records" in data:
        if records is None:
            result["records"] = None
            visible.add("records")
        else:
            gaps.append("精简模式未展示 records：业务记录不是数组")
    visible.update(re.sub(r"\[\d+\]", "[]", path) for path in tuple(visible))
    kept = {path: text for path, text in descriptions.items() if path in visible}
    parsed_paths.update(re.sub(r"\[\d+\]", "[]", path) for path in tuple(parsed_paths))
    # 源目录为字符串统一生成 [] 虚拟子路径；解析得到对象时只将已有词义映射到
    # 实际可见路径，绝不借目录补出未返回字段，也不覆盖实际下标的冲突/形态说明。
    def virtual_key(path: str) -> str:
        # 搜索字符串子键可采用登记别名，说明按逻辑路径关联，输出仍保留实际原键。
        return canonical(path) if search else re.sub(r"\[\]", "", path)

    virtual_descriptions: dict[str, str] = {}
    for path, text in descriptions.items():
        if not re.search(r"\[\d+\]", path):
            virtual_descriptions.setdefault(virtual_key(path), text)
    for path in sorted(visible):
        if path in kept or re.search(r"\[\d+\]", path):
            continue
        if any(path.startswith(parent + ".") or path.startswith(parent + "[]") for parent in parsed_paths):
            mapped_description = virtual_descriptions.get(virtual_key(path))
            if mapped_description is not None:
                kept[path] = mapped_description
    for path in parsed_paths:
        if path in kept and any(word in kept[path] for word in ("未解析", "未改写或解析", "原样保留")):
            conflict = kept[path].partition("；字符串内同源别名冲突")[2]
            kept[path] = "本条为 JSON 字符串；精简模式已解析并裁剪内部字段，仍以字符串返回"
            if conflict:
                kept[path] += "；字符串内同源别名冲突" + conflict
    for path in visible:
        key = canonical(path)
        explicit_unknown = any(key == requested_path or key.startswith(requested_path + ".")
                               for requested_path in requested if requested_path not in catalog)
        if explicit_unknown and key not in catalog and path not in kept and path.startswith("records"):
            kept[path] = _UNKNOWN
    for path in sorted(requested) if isinstance(records, list) and records else []:
        if path not in found and not any(key.startswith(path + ".") for key in found):
            known = catalog if not search else {**catalog, **(CUSTOMER_FIELDS if customer else POLICY_FIELDS)}
            if path in known or any(key.startswith(path + '.') for key in known) or path in _POLICY_FOCUS.values():
                gaps.append(f"本次源数据未提供明确请求字段 {path}，不代表该字段为空或不存在")
            else:
                gaps.append(f"字段未识别:{path}")
    return result, kept, list(dict.fromkeys(gaps))
