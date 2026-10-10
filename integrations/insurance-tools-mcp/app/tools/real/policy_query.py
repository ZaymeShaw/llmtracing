"""共享保单查询单页执行；调用者持有总预算，不嵌套重试。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details


import asyncio

from app.tools.real.auth import HttpClient
from pydantic import ValidationError

from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call


from app.tools.real.java_response import parse_page, validate_trace_id
from app.tools.real.policy_filter import validate_filter
from tool_adapter_sdk.identity import resolve_agent_number
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout
from tool_adapter_sdk.contracts import (
    PolicyCandidateDto, PolicySearchPageDto, PolicySearchRecordDto, PolicySearchResult, PolicySearchSourcePageDto,
)
from tool_adapter_sdk.policy_search_metadata import source_value

post_json = observed_call("backend.policy_query.request")(_post_json)

# 契约级取数口径披露(面向联调方/审计;模型视角同款纪律常驻工具描述 bindings)。
# app 层信封(policy_search)按精确匹配把它们从用户面 gaps 滤出并入 hint——
# render_agentos_answer 会把信封 gaps 逐条追加到用户正文,契约句对终端用户
# 无行动价值(2026-09-18 检视切片 3)。常量单点,两侧不得各自改写。
CONTRACT_GAP_NOTES: tuple[str, ...] = (
    "产品名称与保单状态以清单摘要及源页记录为准，各行不一致时不展示；险种代码不等于产品名称",
    "险种行金额及累计次数仅保留原始记录，缺少业务时点和聚合口径，不作为当前金额或保单汇总",
)

PAGE_SIZE = 100


async def query_policy_page(
    client: HttpClient, url: str, condition: object, *, trace_id: str, timeout_s: float,
    empno: str | None = None, upstream_user_id: str | None = None,
) -> PolicySearchPageDto:
    """只读取首页；短页或多页均按实际行数显式披露截断。"""
    deadline = asyncio.get_running_loop().time() + timeout_s
    validate_trace_id(trace_id)
    tree = validate_filter(condition)
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        diagnostic("handler_deadline_exhausted", stage="deadline")
        raise HandlerTimeout()
    raw = await post_json(client, url, {
        "traceId": trace_id, "empno": resolve_agent_number(empno, upstream_user_id), "filter": tree, "pageNo": 1, "pageSize": PAGE_SIZE,
    }, timeout_s=remaining)
    page = parse_page(raw, trace_id)
    if (page.page_no != 1 or page.page_size != PAGE_SIZE or len(page.records) > PAGE_SIZE
            or page.total < len(page.records) or (page.total > 0 and not page.records)):
        diagnostic(
            "policy_page_inconsistent",
            stage="response_validation",
            returned_count=len(page.records),
            page_no=page.page_no,
            page_size=page.page_size,
            expected_page_no=1, expected_page_size=PAGE_SIZE,
            total_count=page.total,
        )
        raise HandlerContractError()
    try:
        if not isinstance(raw, dict):
            diagnostic("response_object_type_invalid", stage="response_validation")
            raise HandlerContractError()
        source_page = PolicySearchSourcePageDto.model_validate(raw["data"])
        records = []
        for row in source_page.records:
            # 九列只是兼容摘要；合法 JSON 类型漂移不得阻止原页透传，也不强转成姓名/金额。
            values: dict[str, object] = {"polno": row["polno"]}
            for key in PolicySearchRecordDto.model_fields:
                if key == "polno":
                    continue
                value = source_value(row, key)
                if key in {"agg_paid_prem", "agg_pay_cnt"}:
                    values[key] = value if type(value) in (int, float) else None
                else:
                    values[key] = value if isinstance(value, str) and value.strip() else None
            records.append(PolicySearchRecordDto.model_validate(values))
        result = PolicySearchPageDto(record_total=page.total, records=records,
                                     truncated=page.total > len(records), source_page=source_page)
        if asyncio.get_running_loop().time() >= deadline:
            diagnostic("handler_deadline_exhausted", stage="deadline")
            raise HandlerTimeout()
        return result
    except ValidationError as exc:
        diagnostic(
            "response_schema_invalid",
            stage="response_validation",
            **validation_details(exc, {m.__name__: m for m in (PolicySearchSourcePageDto, PolicySearchRecordDto, PolicySearchPageDto)}.get(exc.title, PolicySearchPageDto)),
        )
        raise HandlerContractError() from None


@observed_call("result.search_result")
def search_result(page: PolicySearchPageDto) -> PolicySearchResult:
    """险种行去重为候选；同保单不同姓名不擅自选择一个。"""
    grouped: dict[str, list[PolicySearchRecordDto]] = {}
    for row in page.records:
        grouped.setdefault(row.polno, []).append(row)
    candidates, gaps = [], []
    for polno, rows in grouped.items():
        names = {row.insurant_name for row in rows if row.insurant_name is not None}
        name = next(iter(names)) if len(names) == 1 and all(row.insurant_name is not None for row in rows) else ""
        candidates.append(PolicyCandidateDto(policy_no=polno, insured_name=name, product_name="", status=""))
        if len(names) > 1:
            gaps.append(f"保单 {polno} 的被保人姓名在险种行间不一致，未能确认唯一被保人")
        elif not name:
            gaps.append(f"保单 {polno} 未完整提供被保人姓名")
    if len(page.records) > len(candidates):
        diagnostic("policy_candidates_grouped", stage="response_normalization", returned_count=len(page.records), retained_count=len(candidates))
    if candidates:
        gaps.extend(CONTRACT_GAP_NOTES)
    # 页级截断不再追加文本 gap:truncated 是结构化权威字段,用户向截断提示由
    # app 层信封按展示口径负责(2026-09-18 前本条与信封层截断句语义重复)。
    return PolicySearchResult(policies=candidates, page=page.model_copy(update={"gaps": gaps}))
