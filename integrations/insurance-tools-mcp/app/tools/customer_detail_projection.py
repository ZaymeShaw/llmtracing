"""v5 详情逐客户投影，候选占位不形成负面业务事实。"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Evidence, Fact, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend_contracts import CustomerDetailResponse
from tool_adapter_sdk.contracts import CustomerDetailV5RecordDto
from app.tools.customer_detail_legacy_projection import project_legacy_detail
from tool_adapter_sdk.customer_detail_v5 import DEFINITIONS, describe_output_data

_SOURCE = SourceRef(system="policy_svc", endpoint="/customer/detail")
_BASIC = {"clientMainId": "customer_id", "clientNameCipher": "customer_name", "clientAge": "customer_age", "clientSex": "customer_sex",
          "clientNo": "customer_number", "birthday": "customer_birthday", "phoneNoCipher": "customer_mobile"}
_CANDIDATE = {"clientMainId", "clientNameCipher", "clientAge", "clientSex"}
CANDIDATES_ONLY_GAP = ("多命中仅返回基本候选（客户中心ID、姓名、性别、年龄），其余字段未查询，源数据中的空值只是占位，不能据此推断无业务；"
                       "需要完整资料且目标明确时再按客户中心ID单查")
CANDIDATE_FIELDS = frozenset(_CANDIDATE)

def project_detail(parsed: CustomerDetailResponse, agent_id: str) -> str:
    page = parsed.detail_page
    assert page is not None
    if page.records and not isinstance(page.records[0], CustomerDetailV5RecordDto):
        return project_legacy_detail(parsed, agent_id)
    data = dict(page.source_metadata)
    data["records"] = [record.model_dump(mode="json") for record in page.records]
    descriptions = describe_output_data(data)
    if not page.records:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未查询到该客户", source=_SOURCE,
            request_id=parsed.request_id, latency_ms=parsed.latency_ms, data=data, field_descriptions=descriptions)
    evidence = Evidence(evidence_id=f"E-{parsed.request_id}-01", source_type=SourceType.BUSINESS_API,
                        source_id="policy_svc", retrieved_at=datetime.now(timezone.utc))
    facts: list[Fact] = []
    gaps = list(page.gaps)
    if page.candidates_only:
        gaps.append(CANDIDATES_ONLY_GAP)
    for index, row in enumerate(page.records):
        assert isinstance(row, CustomerDetailV5RecordDto)
        record = row.model_dump(mode="json")
        def add(predicate: str, value: str | int | float | bool) -> None:
            facts.append(to_fact(predicate=predicate, typed_value=value, subject_id=row.clientMainId, subject_type="customer",
                source_call_id=parsed.request_id, identity_scope=agent_id, fact_id=f"F-{parsed.request_id}-{len(facts)+1:03d}",
                evidence_refs=[evidence.evidence_id]))
        for path, value in record.items():
            if path not in DEFINITIONS or page.candidates_only and path not in _CANDIDATE:
                continue
            if path in _BASIC and value is not None:
                valid = type(value) in (str, int) if path == "clientAge" else isinstance(value, str)
                if not valid:
                    gaps.append(f"字段 {path} 返回类型非预期；原值保留，未形成规范化事实")
                    continue
            if isinstance(value, (str, int, float, bool)) and not (isinstance(value, str) and not value.strip()):
                add(_BASIC.get(path, path), value)
        if not page.candidates_only:
            missing = [DEFINITIONS[path].label for path in _CANDIDATE if record.get(path) is None]
            if missing:
                gaps.append("部分基本资料未提供：" + "、".join(missing))
        add("customer_detail_record", f"records[{index}] 原始客户记录：" + json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return make_envelope(status=EnvelopeStatus.PARTIAL if gaps else EnvelopeStatus.SUCCESS,
        facts=facts, evidence=[evidence], gaps=list(dict.fromkeys(gaps)),
        hint="客户详情 v5 原值保留；候选不是完整详情，空值不代表零或否；不推断金额单位、出生年份或已享资格",
        source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms, data=data, field_descriptions=descriptions)
