"""客户详情 v5 薄适配：验证身份及响应边界，保留可用记录和原始业务值。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic

import asyncio

from app.tools.real.auth import HttpClient
from pydantic import ValidationError

from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call

from app.tools.real.java_response import validate_trace_id
from tool_adapter_sdk.identity import resolve_agent_number
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout, HandlerUnavailable
from tool_adapter_sdk.call_policy import call_with_policy
from tool_adapter_sdk.deadline import handler_deadline
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.contracts import (
    BackendContext, CustomerDetailPageDto, CustomerDetailV5RecordDto,
    CustomerDetailRequest, CustomerDetailResult, HandlerCallResult,
)

post_json = observed_call("backend.customer_detail.request")(_post_json)


class CustomerDetailHandler:
    def __init__(self, client: HttpClient, config: HandlerConfig) -> None:
        self.client = client
        self.config = config

    async def __call__(
        self, request: CustomerDetailRequest, context: BackendContext,
    ) -> HandlerCallResult[CustomerDetailResult]:
        agentno = resolve_agent_number(context.agentno, context.upstream_user_id, max_length=64)
        validate_trace_id(context.trace_id)
        loop = asyncio.get_running_loop()
        deadline = handler_deadline(self.config.timeout_s)

        def remaining() -> float:
            value = deadline - loop.time()
            if value <= 0:
                diagnostic("handler_deadline_exhausted", stage="deadline")
                raise HandlerTimeout()
            return value

        async def query() -> CustomerDetailResult:
            # 每次尝试只携带剩余预算（内紧外松），重试不得携带全量超时穿透总预算。
            raw = await post_json(
                self.client, self.config.url,
                {"traceId": context.trace_id, "agentNo": agentno, **request.to_wire()},
                timeout_s=remaining(),
            )
            return parse_detail_response(raw, request, context)

        return await call_with_policy(query, self.config)


def parse_detail_response(raw: object, request: CustomerDetailRequest, context: BackendContext) -> CustomerDetailResult:
    """共用 v5 解析；逐条保留可定位记录，身份错配仍使整个调用失败。"""
    if not isinstance(raw, dict) or type(raw.get("code")) is not int:
        diagnostic("response_schema_invalid", stage="response_validation")
        raise HandlerContractError()
    if raw["code"] in {1001, 1005}:
        diagnostic("business_unavailable", stage="response_validation", business_code=raw["code"])
        raise HandlerUnavailable()
    if raw["code"] != 0 or raw.get("traceId") != context.trace_id:
        diagnostic("business_code_rejected" if raw["code"] != 0 else "response_trace_mismatch", stage="response_validation")
        raise HandlerContractError()
    data = raw.get("data")
    if not isinstance(data, dict) or type(data.get("total")) is not int or data["total"] < 0 or not isinstance(data.get("records"), list):
        diagnostic("response_data_type_invalid", stage="response_validation")
        raise HandlerContractError()
    rows = data["records"]
    agentno = resolve_agent_number(context.agentno, context.upstream_user_id, max_length=64)
    records: list[CustomerDetailV5RecordDto] = []
    gaps: list[str] = []
    requested_ids = request.to_wire().get("clientMainIdSet", [])
    for index, row in enumerate(rows):
        try:
            record = CustomerDetailV5RecordDto.model_validate(row)
        except (ValidationError, ValueError):
            diagnostic("customer_detail_record_unusable", stage="response_validation", record_index=index, returned_count=len(rows))
            gaps.append("部分客户记录缺少有效客户中心 ID 或包含非法 JSON，已跳过；结果不完整")
            continue
        if requested_ids and record.clientMainId not in requested_ids:
            diagnostic("customer_identity_mismatch", stage="response_validation")
            raise HandlerContractError()
        if isinstance(record.agentNo, str) and record.agentNo.strip() and record.agentNo != agentno:
            diagnostic("customer_agent_mismatch", stage="response_validation")
            raise HandlerContractError()
        records.append(record)
    if rows and not records or not rows and data["total"] > 0:
        diagnostic("customer_detail_records_unusable", stage="response_validation")
        raise HandlerContractError()
    if data["total"] != len(records):
        diagnostic("customer_detail_count_mismatch", stage="response_validation", returned_count=len(records), total_count=data["total"])
        gaps.append("服务报告命中总数与可用记录数不同，结果不完整；不据此推断未返回客户资料")
    try:
        page = CustomerDetailPageDto(records=[*records], source_metadata={key: value for key, value in data.items() if key != "records"},
                                     gaps=list(dict.fromkeys(gaps)), candidates_only=data["total"] > 1 or len(rows) > 1)
        first = records[0] if records else None
        return CustomerDetailResult(customer_id=first.clientMainId if first else None,
            name=first.clientNameCipher if first and isinstance(first.clientNameCipher, str) else "",
            policies=None, benefits=None, detail_page=page)
    except ValidationError:
        diagnostic("response_schema_invalid", stage="response_validation")
        raise HandlerContractError() from None
