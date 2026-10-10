"""保单明细薄适配：尾号先有界定位，完整唯一保单号再精确查询。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details

import asyncio
import re
from typing import cast

from app.tools.real.auth import HttpClient
from pydantic import JsonValue, ValidationError

from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call

from app.tools.real.java_response import parse_page, validate_trace_id
from app.tools.real.policy_query import PAGE_SIZE, query_policy_page, search_result
from tool_adapter_sdk.identity import resolve_agent_number
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout, ToolConfigurationError
from tool_adapter_sdk.call_policy import call_with_policy
from tool_adapter_sdk.deadline import handler_deadline
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.contracts import (
    BackendContext, HandlerCallResult, PolicyDetailPageDto, PolicyDetailRecordDto,
    PolicyDetailRequest, PolicyDetailResult,
)

post_json = observed_call("backend.policy_detail.request")(_post_json)

_FULL_NO = re.compile(r"P[0-9]{3,63}")
_TAIL = re.compile(r"[0-9]{3,64}")


class PolicyDetailHandler:
    def __init__(
        self, client: HttpClient, config: HandlerConfig, *, query_client: HttpClient | None = None,
    ) -> None:
        self.query_client = query_client if query_client is not None else client
        if config.query_url is None:
            raise ToolConfigurationError("policy_detail 缺少尾号查询地址 query_url")
        self.client = client
        self.config = config
        self.query_url = config.query_url

    async def __call__(
        self, request: PolicyDetailRequest, context: BackendContext,
    ) -> HandlerCallResult[PolicyDetailResult]:
        empno = resolve_agent_number(context.empno, context.upstream_user_id)
        validate_trace_id(context.trace_id)
        ref = request.policy_input
        if _FULL_NO.fullmatch(ref) is None and _TAIL.fullmatch(ref) is None:
            diagnostic("policy_reference_invalid", stage="request_validation")
            raise HandlerContractError()
        loop = asyncio.get_running_loop()
        deadline = handler_deadline(self.config.timeout_s)

        def remaining() -> float:
            budget = deadline - loop.time()
            if budget <= 0:
                diagnostic("handler_deadline_exhausted", stage="deadline")
                raise HandlerTimeout()
            return budget

        async def query() -> PolicyDetailResult:
            polno = ref
            from app.tools.mode import visible_policy_revalidation
            if visible_policy_revalidation.get() and _FULL_NO.fullmatch(ref):
                located = await query_policy_page(self.query_client, self.query_url, {
                    "type": "PREDICATE", "node_id": "n0", "field": "polno", "operator": "EQ", "value": ref,
                }, empno=empno, trace_id=context.trace_id, timeout_s=remaining())
                if located.truncated or not located.records or any(row.polno != ref for row in located.records):
                    diagnostic("visible_policy_unconfirmed", stage="ownership_lookup")
                    return PolicyDetailResult(policy_no=None, fields={}, as_of=None)
            if _TAIL.fullmatch(ref):
                page = await query_policy_page(self.query_client, self.query_url, {
                    "type": "PREDICATE", "node_id": "n0", "field": "polno", "operator": "SUFFIX", "value": ref,
                }, empno=empno, trace_id=context.trace_id, timeout_s=remaining())
                if any(_FULL_NO.fullmatch(row.polno) is None or not row.polno.endswith(ref) for row in page.records):
                    diagnostic("policy_suffix_identity_mismatch", stage="response_validation")
                    raise HandlerContractError()
                found = search_result(page)
                if page.truncated or len(found.policies) != 1:
                    return PolicyDetailResult(policy_no=None, fields={}, as_of=None,
                                              candidates=found.policies, candidate_page=found.page)
                polno = found.policies[0].policy_no
            raw = await post_json(self.client, self.config.url, {
                "traceId": context.trace_id, "empno": empno, "polno": polno,
                "pageNo": 1, "pageSize": 20,
            }, timeout_s=remaining())
            page_data = parse_page(raw, context.trace_id)
            # parse_page 已验证成功包装；单独取原始 data，避免共享分页模型忽略的扩展字段丢失。
            raw_data = cast(dict[str, JsonValue], cast(dict[str, object], raw)["data"])
            # 显式请求首页和20行容量；仍保留服务实际容量，100仅作为本地行数上限。
            if (page_data.page_no != 1 or len(page_data.records) > page_data.page_size
                    or len(page_data.records) > PAGE_SIZE or page_data.total < len(page_data.records)
                    or (page_data.total > 0 and not page_data.records)):
                diagnostic(
                    "policy_detail_page_inconsistent",
                    stage="response_validation",
                    returned_count=len(page_data.records),
                    page_no=page_data.page_no,
                    page_size=page_data.page_size,
                    total_count=page_data.total,
                )
                raise HandlerContractError()
            records = []
            try:
                for row in page_data.records:
                    record = PolicyDetailRecordDto.model_validate(row)
                    if record.polno != polno:
                        diagnostic("policy_identity_mismatch", stage="response_validation")
                        raise HandlerContractError()
                    records.append(record)
                page_result = PolicyDetailPageDto(record_total=page_data.total, records=records,
                    truncated=page_data.total > len(records),
                    source_metadata={key: value for key, value in raw_data.items() if key != "records"})
                return PolicyDetailResult(policy_no=polno if records else None, fields={}, as_of=None,
                                          detail_page=page_result)
            except ValidationError as exc:
                diagnostic(
                    "response_schema_invalid",
                    stage="response_validation",
                    **validation_details(exc, {m.__name__: m for m in (PolicyDetailRecordDto, PolicyDetailPageDto, PolicyDetailResult)}.get(exc.title, PolicyDetailPageDto)),
                )
                raise HandlerContractError() from None

        return await call_with_policy(query, self.config)
