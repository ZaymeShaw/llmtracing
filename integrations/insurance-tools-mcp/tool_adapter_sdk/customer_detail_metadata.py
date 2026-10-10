"""客户 V14 字段说明；只识别文档明确路径，不改写源 JSON。"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from functools import cache

from pydantic import JsonValue

from tool_adapter_sdk.customer_detail_catalog import SHEET, V14_FIELDS, FieldDefinition
from tool_adapter_sdk.customer_output_meanings import EXTERNAL_DOMAINS, OUTPUT_MEANINGS
from tool_adapter_sdk.output_descriptions import describe_output_page

DEFINITIONS = {entry.path: entry for entry in V14_FIELDS}


def describe_output_field(path: str) -> str:
    """返回值释义与输入规则隔离；标签和枚举始终来自当前字段目录。"""
    entry = definition(path)
    if entry is None:
        if any(item.path.startswith(canonical_path(path) + ".") for item in V14_FIELDS):
            return "客户关联业务记录；本次集合不代表完整关联或已享权益"
        return "业务含义待确认，不据字段名推断"
    meaning = OUTPUT_MEANINGS.get(entry.path, entry.label)
    if "membergradeinfo." in entry.path:
        leaf = entry.path.rpartition(".")[2]
        # 仅作用于已登记会员字段，各服务线名称/枚举仍取目录，不猜未知后缀。
        qualifiers = {
            "membergradesearch": "等级或版本，不是达标状态",
            "memberperiod": "年度期次，不是达标日期",
            "memberproductname": "会员服务线名称",
            "memberstatus": "会员类型或状态，不是等级版本",
            "nextmembergrade": "预计升级或达标的下一等级",
            "qualifiedtime": "达标日期，不是会员期次",
        }
        for suffix, qualifier in qualifiers.items():
            if leaf.endswith(suffix):
                meaning = entry.label + "；" + qualifier
                break
    if entry.domain and entry.path not in EXTERNAL_DOMAINS:
        meaning += "；取值：" + entry.domain
    return meaning


def describe_output_data(data: dict[str, JsonValue]) -> dict[str, str]:
    """模型返回专用短释义；源数据、原 Schema 与完整查询词典不变。"""
    return describe_output_page(
        data, describe_field=describe_output_field, canonical_path=canonical_path,
        defined_paths=DEFINITIONS,
        page_labels={"pageNo": "当前页码", "pageSize": "服务页容量",
                     # 与 bindings 工具描述同口径：total 是条件命中总数，不是页内或展示口径。
                     "total": "符合查询条件的客户总数，不是当前页记录数或展示条数",
                     "records": "本次客户记录，不代表完整保单或权益集合"},
        record_description="单条客户记录；字段按实际返回为准，空值不等于零或否；金额未注明单位、币种或时点时不推断",
    )

# V14 的 E 列不改写；已由接口文档确认的 Doris 物理列单独映射，不猜测 CSV 错拼。
_PHYSICAL_ALIASES = {
    "pcustsourcetype": (
        "pcustsourctype",
        "docs/工具接口文档/customer-search-request-query接口.md 第 121–124 行；准客来源以 Doris 物理列为准",
    ),
}


def _source_aliases() -> dict[str, str]:
    aliases = {source: target for source, (target, _) in _PHYSICAL_ALIASES.items()}
    for entry in V14_FIELDS:
        match = re.search(r"ES[：:]\s*([A-Za-z_][A-Za-z0-9_.]*)", entry.es_mapping)
        if match:
            source_parts, actual_parts = match[1].split("."), entry.path.split(".")
            if len(source_parts) == len(actual_parts):
                for length in range(1, len(actual_parts) + 1):
                    aliases[".".join(source_parts[:length])] = ".".join(actual_parts[:length])
    # v2 明确列出该 CSV 名；V14 的说明未使用 ES: 前缀，不能靠正则发现。
    aliases["birthdayMd"] = "birthdaymd"
    return aliases


SOURCE_ALIASES = _source_aliases()
_SEGMENT_ALIASES = {
    (actual.rpartition(".")[0], source.rpartition(".")[2]): actual.rpartition(".")[2]
    for source, actual in SOURCE_ALIASES.items()
}


def query_canonical_path(path: str) -> str:
    """逐前缀匹配允许物理根名与逻辑子名混用，但不做猜测性后缀匹配。"""
    canonical: list[str] = []
    for part in path.split("."):
        canonical.append(_SEGMENT_ALIASES.get((".".join(canonical), part), part))
    return ".".join(canonical)


# 2026-09-23 dev 客户查询/详情实测确认；仅证明响应名称，不证明请求字段可用。
_RESPONSE_ONLY_ALIASES = {
    "isBuyInsurance": "isbuyinsurance",
    "insnoSumInsSeqNum": "insnosuminsseqnum",
}
# Schema 也识别实际原键，但查询归一化继续使用上方既有的段映射快照。
SOURCE_ALIASES.update(_RESPONSE_ONLY_ALIASES)


def canonical_path(path: str) -> str:
    """响应识别包含实测别名；查询必须使用 query_canonical_path，避免扩大准入。"""
    root, separator, suffix = path.partition(".")
    return query_canonical_path(_RESPONSE_ONLY_ALIASES.get(root, root) + separator + suffix)


def definition(path: str) -> FieldDefinition | None:
    return DEFINITIONS.get(canonical_path(path))


def group_source_fields(record: Mapping[str, JsonValue]) -> dict[str, list[tuple[str, JsonValue]]]:
    """只读归组实际存在的原键；不生成缺省值、不合并或覆盖源字段。"""
    grouped: dict[str, list[tuple[str, JsonValue]]] = {}
    for key, value in record.items():
        grouped.setdefault(canonical_path(key), []).append((key, value))
    return grouped


def has_alias_conflict(values: Sequence[tuple[str, JsonValue]]) -> bool:
    """JSON 类型敏感相等：null/空白、false/0、1/1.0 都不能互相替代。"""
    return len({json.dumps(value, ensure_ascii=False, sort_keys=True) for _, value in values}) > 1


def source_text(record: Mapping[str, JsonValue], path: str) -> str | None:
    """从同源字段读取无冲突的非空字符串；保留原字符串，不裁剪或强制转换。"""
    values = group_source_fields(record).get(canonical_path(path), [])
    if not values or has_alias_conflict(values):
        return None
    value = values[0][1]
    return value if isinstance(value, str) and value.strip() else None


def describe_field(path: str) -> str:
    canonical = canonical_path(path)
    entry = DEFINITIONS.get(canonical)
    if entry is None:
        children = [item for item in V14_FIELDS if item.path.startswith(canonical + ".")]
        if children:
            return (f"客户嵌套业务记录容器；V14 定义子字段：{'、'.join(item.path for item in children)}；"
                    "对象、数组或 JSON 字符串按原值保留；不据本次集合推断完整关联或已享资格。")
        return f"原始业务字段 {path}；V14 未定义业务含义，原值保留，不据字段名推断。"
    parts = [entry.label, f"V14 {SHEET}第 {entry.row} 行", f"逻辑类型：{entry.logical_type}"]
    if entry.meaning:
        parts.append(entry.meaning)
    if entry.aliases:
        parts.append(f"文档业务别名：{entry.aliases}")
    if entry.domain:
        parts.append(f"枚举/取值及来源：{entry.domain}")
    if entry.external_path != entry.path:
        parts.append(f"D 列外部路径 {entry.external_path}；采用 E 列实际路径 {entry.path}")
    if path != entry.path:
        parts.append(f"字典路径 {entry.path}；接口原键 {path} 保持不变")
    if path in _PHYSICAL_ALIASES:
        parts.append("物理列依据：" + _PHYSICAL_ALIASES[path][1])
    parts.append("文档含搜索筛选口径，不据此反推客户当前状态；原始 JSON 类型和值不转换，null/空值不等于零或否")
    if "membergradeinfo." in entry.path or "权益" in entry.category:
        parts.append("等级、达标或状态原值不自动证明已享有资格")
    if any(word in entry.label for word in ("保费", "保额", "金额", "总额", "余额", "缺口")):
        parts.append("币种和业务时点未提供时不得自行补元/万元/CNY/截至今天，不自行汇总或改作现金价值")
    return "；".join(parts)


def field_schema(path: str) -> dict[str, JsonValue]:
    """只给确定路径添加说明，不限制容器类型、必填项或为缺省字段生成默认值。"""
    canonical = canonical_path(path)
    children = {entry.path[len(canonical) + 1:].split(".")[0]
                for entry in V14_FIELDS if entry.path.startswith(canonical + ".")}
    if not children:
        return {}
    children.update(alias for parent, alias in _SEGMENT_ALIASES if parent == canonical)
    properties: dict[str, JsonValue] = {}
    for child in sorted(children):
        child_path = path + "." + child
        properties[child] = {"description": describe_field(child_path), **field_schema(child_path)}
    return {"properties": properties,
            "items": {"description": "客户原始业务数组元素；顺序保持不变", "properties": properties}}


def add_source_alias_schema(schema: dict[str, JsonValue]) -> None:
    """别名仅增加描述型 properties；不改变字段默认值、校验器及原键序列化。"""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for source in sorted(SOURCE_ALIASES):
            if "." not in source and source not in properties:
                properties[source] = {"description": describe_field(source), **field_schema(source)}


def describe_data(data: dict[str, JsonValue]) -> dict[str, str]:
    """[] 共享路径只说明字段含义；实际容器形态按下标定位，字符串始终不解析。"""
    descriptions: dict[str, str] = {}
    page_labels = {"pageNo": "当前页码", "pageSize": "服务页容量", "total": "服务报告的客户行总数",
                   "records": "本次返回的全部客户记录；不等同完整名下保单或权益集合"}

    @cache
    def shared_description(logical: str, business: bool) -> str:
        # 缓存只取决于路径，不读取某条值；实际与虚拟同路径必须生成同一语义。
        if not business:
            return f"接口 data 内原始页属性 {logical}；V14 未定义，原键和原值保留"
        return (describe_field(logical) + "；共享路径说明；字典定义不代表每条记录均返回该字段，"
                "实际类型和值以 data 为准")

    @cache
    def dictionary_suffixes(logical: str) -> tuple[str, ...]:
        canonical = canonical_path(logical)
        suffixes: set[str] = set()
        for entry in V14_FIELDS:
            if entry.path.startswith(canonical + "."):
                descendants = entry.path[len(canonical) + 1:].split(".")
                for length in range(1, len(descendants) + 1):
                    suffixes.add("." + ".".join(descendants[:length]))
        return tuple(sorted(suffixes))

    def actual_state(value: JsonValue) -> str:
        if isinstance(value, list):
            return "本条实际值为数组；原样保留，元素顺序不变，不据本次集合推断完整关联"
        if isinstance(value, dict):
            return "本条实际值为对象；原键原值保留"
        if isinstance(value, str):
            return "本条实际值为字符串；原样保留，未解析，不据此声称子字段已返回"
        if value is None:
            return "本条实际值为 null；原样保留，不代表零、否或无关联"
        if isinstance(value, bool):
            return "本条实际值为布尔值；原样保留，不推断嵌套业务含义"
        if isinstance(value, int):
            return "本条实际值为整数；原样保留，不推断嵌套业务含义"
        return "本条实际值为浮点数；原样保留，不推断嵌套业务含义"

    def walk(value: JsonValue, path: str, logical: str, actual: str, *, business: bool = True) -> None:
        descriptions[path] = shared_description(logical, business)
        suffixes = dictionary_suffixes(logical) if business else ()
        # 仅实际容器/字典有子项的值需要局部形态；不为每个普通标量复制字典说明。
        if business and (isinstance(value, (dict, list)) or suffixes):
            descriptions[actual] = actual_state(value)
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}", f"{logical}.{key}", f"{actual}.{key}", business=business)
        elif isinstance(value, list):
            # 空数组和非空数组的共享元素路径说明相同，不由后续元素形态覆盖。
            descriptions[path + "[]"] = shared_description(logical, business)
            for index, item in enumerate(value):
                walk(item, path + "[]", logical, f"{actual}[{index}]", business=business)
        elif isinstance(value, str) and business:
            for suffix in suffixes:
                # 保留历史虚拟路径及原键前缀；不生成真实下标子项，也不解析源字符串。
                descriptions[path + "[]" + suffix] = shared_description(logical + suffix, business)

    for key, value in data.items():
        if key == "records" and isinstance(value, list):
            descriptions[key] = page_labels[key]
            descriptions["records[]"] = "单条客户原始业务记录；嵌套资料仍属于该客户源记录"
            for index, record in enumerate(value):
                if isinstance(record, dict):
                    for field, item in record.items():
                        walk(item, f"records[].{field}", field, f"records[{index}].{field}")
        else:
            walk(value, key, key, key, business=False)
            if key in page_labels:
                descriptions[key] = page_labels[key]
    return descriptions
