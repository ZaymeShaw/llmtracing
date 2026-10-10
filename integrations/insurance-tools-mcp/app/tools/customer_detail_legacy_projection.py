"""客户全量详情投影：业务源数据、可引用事实和缺口各自保留，不猜测关联资格。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import JsonValue

from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Evidence, Fact, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend_contracts import CustomerDetailResponse
from tool_adapter_sdk.customer_detail_catalog import V14_FIELDS
from tool_adapter_sdk.customer_detail_metadata import definition, describe_output_data, group_source_fields, has_alias_conflict

_SOURCE = SourceRef(system="policy_svc", endpoint="/customer/detail")
_BASIC_FIELDS = {
    "searchclientname": ("customer_name", "姓名"), "clientno": ("customer_number", "客户号"),
    "clientage": ("customer_age", "年龄"), "clientbirthday": ("customer_birthday", "出生日期"),
    "clientmobile": ("customer_mobile", "手机号"), "clientsex": ("customer_sex", "性别"),
}
_POLICY_ROOTS = {entry.path.split(".")[0] for entry in V14_FIELDS if entry.category == "保单信息"}
_BENEFIT_ROOTS = {entry.path.split(".")[0] for entry in V14_FIELDS if entry.category == "权益会员"}


def _known(value: JsonValue) -> bool:
    # 空数组/对象是本次明确返回的空结构，不自动等同未知或完整关联为零。
    return value is not None and not (isinstance(value, str) and not value.strip())


def project_legacy_detail(parsed: CustomerDetailResponse, agent_id: str) -> str:
    page = parsed.detail_page
    assert page is not None
    data: dict[str, JsonValue] = dict(page.source_metadata)
    data["records"] = [record.model_dump(mode="json") for record in page.records]
    descriptions = describe_output_data(data)
    if not page.records:
        return make_envelope(
            status=EnvelopeStatus.EMPTY, hint="未查询到该客户",
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
            data=data, field_descriptions=descriptions,
        )

    assert parsed.customer_id is not None
    customer_id = parsed.customer_id
    record = page.records[0].model_dump(mode="json")
    evidence = Evidence(
        evidence_id=f"E-{parsed.request_id}-01", source_type=SourceType.BUSINESS_API,
        source_id="policy_svc", retrieved_at=datetime.now(timezone.utc),
    )
    facts: list[Fact] = []

    def add(predicate: str, value: str | int | float | bool) -> None:
        facts.append(to_fact(
            predicate=predicate, typed_value=value, subject_id=customer_id, subject_type="customer",
            source_call_id=parsed.request_id, identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}", evidence_refs=[evidence.evidence_id],
        ))

    grouped = group_source_fields(record)
    gaps: list[str] = []
    conflicts: set[str] = set()
    for path, values in grouped.items():
        if has_alias_conflict(values):
            conflicts.add(path)
            gaps.append(f"同源字段冲突：{'、'.join(key for key, _ in values)}；原值均保留，不选择单值事实")
    missing_basic = [label for path, (_, label) in _BASIC_FIELDS.items()
                     if path not in conflicts and (path not in grouped or not _known(grouped[path][0][1]))]
    if missing_basic:
        gaps.append("部分基本资料未提供：" + "、".join(missing_basic))

    for path, values in grouped.items():
        entry = definition(path)
        value = values[0][1]
        if path in conflicts or entry is None:
            continue
        if not _known(value):
            if path not in _BASIC_FIELDS:
                gaps.append(f"字段未知：{entry.label}（{path}）；接口返回空值，不能据此推断零或否")
            continue
        if isinstance(value, (str, int, float, bool)):
            predicate = _BASIC_FIELDS[path][0] if path in _BASIC_FIELDS else path
            add("customer_id" if path == "idicm_clientno" else predicate, value)

    has_policy = any(path in _POLICY_ROOTS and any(_known(value) for _, value in values)
                     for path, values in grouped.items())
    has_benefit = any(path in _BENEFIT_ROOTS and any(_known(value) for _, value in values)
                      for path, values in grouped.items())
    if not has_policy:
        gaps.append("保单相关资料未提供，数量未知，不能据此判断没有保单")
    if not has_benefit:
        gaps.append("权益相关资料未提供，数量未知，不能据此判断没有已享权益")
    add("customer_detail_record", "records[0] 原始客户记录：" + json.dumps(
        record, ensure_ascii=False, separators=(",", ":"),
    ))
    policy_hint = "保单相关资料已返回" if has_policy else "保单相关资料未知"
    benefit_hint = "权益相关资料已返回" if has_benefit else "权益相关资料未知"
    return make_envelope(
        status=EnvelopeStatus.PARTIAL if gaps else EnvelopeStatus.SUCCESS,
        facts=facts, evidence=[evidence], gaps=gaps,
        hint=(f"已命中客户，完整保留本次客户记录和页属性；{policy_hint}、{benefit_hint}；"
              "完整源值见 data，字段释义见 field_descriptions（V14）；按问题引用事实组织简短回答，"
              "不直接展示整份原始 JSON；嵌套资料不代表完整关联全集，等级或达标标签不证明已享资格；"
              "币种和业务时点未知时不得自行补元/万元/CNY/截至今天"),
        source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        data=data, field_descriptions=descriptions,
    )
