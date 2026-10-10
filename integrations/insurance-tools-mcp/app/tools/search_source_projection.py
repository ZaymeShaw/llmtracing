"""搜索原始业务页的模型投影；候选摘要与整页数据、证据保持独立。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import JsonValue

from app.core.envelope import to_fact
from app.schemas import Evidence, Fact
from app.schemas.enums import SourceType
from tool_adapter_sdk.customer_search_metadata import (
    canonical_path, require_customer_id, describe_output_data as describe_customer_data,
)
from tool_adapter_sdk.policy_search_metadata import (
    canonical_path as policy_canonical_path, describe_output_data as describe_policy_data,
    response_profile,
)
from tool_adapter_sdk.search_source_pages import CustomerSearchSourcePageDto, PolicySearchSourcePageDto

SearchKind = Literal["policy", "customer"]

SOURCE_HINT = (
    "；data 保留已取回全页，不受摘要展示上限限制，不代表尚未取回的分页；"
    "field_descriptions 的文档语义仅供参考，实际字段、类型和值以 data 为准；"
    "请按问题引用对应原记录 Fact，不必输出整页 JSON；未定义或冲突的业务含义待确认，不能猜测"
)


def source_page_data(
    source_page: PolicySearchSourcePageDto | CustomerSearchSourcePageDto | None,
    kind: SearchKind,
) -> tuple[dict[str, JsonValue] | None, dict[str, str] | None]:
    """只序列化已经通过领域校验的成功业务 data，不从候选或文档重建源页。"""
    if source_page is None:
        return None, None
    data = source_page.model_dump(mode="json", by_alias=True)
    describe = describe_policy_data if kind == "policy" else describe_customer_data
    # 字段目录的根路径相对于 records 内的一行，不能借同名给页级扩展赋予行字段语义。
    page_fields = {"pageNo", "pageSize", "total", "records"}
    descriptions = describe({key: value for key, value in data.items() if key in page_fields})
    def describe_page_extension(value: JsonValue, path: str) -> None:
        descriptions[path] = "页级扩展字段，业务含义待确认"
        if isinstance(value, dict):
            for key, item in value.items():
                describe_page_extension(item, f"{path}.{key}")
        elif isinstance(value, list):
            descriptions[path + "[]"] = descriptions[path] + "；数组元素"
            for item in value:
                describe_page_extension(item, path + "[]")

    for key, value in data.items():
        if key not in page_fields:
            describe_page_extension(value, key)

    def mark_conflicts(value: JsonValue, path: str, actual_path: str, logical_path: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                mark_conflicts(item, path + "[]", f"{actual_path}[{index}]", logical_path)
            return
        if not isinstance(value, dict):
            return
        aliases: dict[str, list[str]] = {}
        for key in value:
            logical = f"{logical_path}.{key}" if logical_path else key
            canonical = policy_canonical_path(logical) if kind == "policy" else canonical_path(logical)
            aliases.setdefault(canonical, []).append(key)
        for keys in aliases.values():
            # 按 JSON 类型和值比较；false/0、1/1.0 不能被 Python 的相等规则合并。
            if len(keys) < 2 or len({json.dumps(value[key], ensure_ascii=False, sort_keys=True)
                                     for key in keys}) == 1:
                continue
            conflict = (f"；{actual_path} 同源字段冲突：{'、'.join(keys)}；"
                        "原键原值均保留，业务口径待确认，不选择单值")
            for key in keys:
                descriptions[f"{path}.{key}"] += conflict
        # 别名也可能位于嵌套记录中；只比较同一对象，不跨数组行，也不解析 JSON 字符串。
        for key, item in value.items():
            logical = f"{logical_path}.{key}" if logical_path else key
            mark_conflicts(item, f"{path}.{key}", f"{actual_path}.{key}", logical)

    for index, record in enumerate(source_page.records):
        mark_conflicts(record, "records[]", f"records[{index}]", "")
    return data, descriptions


def append_source_records(
    data: dict[str, JsonValue] | None,
    kind: SearchKind,
    *,
    request_id: str,
    agent_id: str,
    facts: list[Fact],
    evidence: list[Evidence],
) -> None:
    """整页逐行可引用，同保单多险种不合并，也不受候选摘要 result_limit 限制。"""
    if data is None:
        return
    records = data["records"]
    assert isinstance(records, list)
    if not records:
        return
    # 全页来源于同一次业务查询；共享证据但为每一行保留独立 Fact ID。
    evidence_id = f"E-{request_id}-{len(evidence) + 1:02d}"
    evidence.append(Evidence(
        evidence_id=evidence_id, source_type=SourceType.BUSINESS_API,
        source_id="policy_svc", retrieved_at=datetime.now(timezone.utc),
    ))
    for index, record in enumerate(records):
        # 形状和身份由 source_page 契约校验；这里不再转换主键或非身份字段。
        assert isinstance(record, dict)
        label = ("保单险种" if response_profile(record) == "legacy" else "保单搜索") if kind == "policy" else "客户"
        subject_id = record["polno"] if kind == "policy" else require_customer_id(record)
        assert isinstance(subject_id, str)
        facts.append(to_fact(
            predicate=f"{kind}_search_record",
            typed_value=f"records[{index}] 原始{label}记录：" + json.dumps(
                record, ensure_ascii=False, separators=(",", ":"),
            ),
            subject_id=subject_id, subject_type=kind,
            source_call_id=request_id, identity_scope=agent_id,
            fact_id=f"F-{request_id}-{len(facts) + 1:03d}", evidence_refs=[evidence_id],
        ))
