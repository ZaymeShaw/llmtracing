"""客户明文解析与有界查询，共享逻辑总预算与客户端生命周期。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic

import asyncio

from app.tools.real.auth import HttpClient

from app.tools.real.customer_parser import BROWSE_ALL_QUERY, UNSUPPORTED_MESSAGE, parse_customer_query
from app.tools.real.customer_query import query_customer_page
from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call

from app.tools.real.java_response import validate_trace_id
from tool_adapter_sdk.identity import resolve_agent_number
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout, ToolConfigurationError
from tool_adapter_sdk.call_policy import call_with_policy
from tool_adapter_sdk.deadline import handler_deadline
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.contracts import (
    BackendContext, CustomerCandidateDto, CustomerSearchPageDto, CustomerSearchRequest,
    CustomerSearchResult, HandlerCallResult,
)

post_json = observed_call("backend.customer_search.request")(_post_json)


class CustomerSearchHandler:
    def __init__(
        self, client: HttpClient, config: HandlerConfig, *, query_client: HttpClient | None = None,
    ) -> None:
        self.query_client = query_client if query_client is not None else client
        if config.query_url is None:
            raise ToolConfigurationError("customer_search 必须配置 query_url")
        self.client, self.config, self.query_url = client, config, config.query_url

    async def __call__(self, request: CustomerSearchRequest, context: BackendContext) -> HandlerCallResult[CustomerSearchResult]:
        loop = asyncio.get_running_loop()
        agentno = resolve_agent_number(context.agentno, context.upstream_user_id, max_length=64)
        deadline = handler_deadline(self.config.timeout_s)

        def remaining() -> float:
            value = deadline - loop.time()
            if value <= 0:
                diagnostic("handler_deadline_exhausted", stage="deadline")
                raise HandlerTimeout()
            return value

        async def execute() -> CustomerSearchResult:
            validate_trace_id(context.trace_id)
            if (not isinstance(context.session_id, str) or not context.session_id.strip()
                    or not isinstance(context.agent_id, str) or not context.agent_id.strip()):
                diagnostic("session_or_agent_context_invalid", stage="request_validation")
                raise HandlerContractError()
            raw = await post_json(self.client, self.config.url, {
                "source": "askbob", "user_text": request.query,
                "session_id": context.session_id, "trace_id": context.trace_id, "user_id": context.agent_id,
                "user_action": "write", "action_scenario": "customerSearch", "extra_input_params": {},
            }, timeout_s=remaining())
            query = parse_customer_query(raw, query=request.query, trace_id=context.trace_id)
            remaining()
            if query is None:
                return CustomerSearchResult(customers=[], unsupported_message=UNSUPPORTED_MESSAGE)
            # 空条件浏览与「恰好只解析出主键存在性」语义同构（≡ 名下全部），同一披露口径。
            browsed_all = query.get("conditions") == BROWSE_ALL_QUERY["conditions"]
            page = await query_customer_page(self.query_client, self.query_url, query,
                                             agentno=agentno, trace_id=context.trace_id, timeout_s=remaining(), page_size=100)
            return CustomerSearchResult(
                customers=[CustomerCandidateDto(customer_id=row.customer_id, name=row.name, policy_count=None) for row in page.records],
                page=CustomerSearchPageDto(total=page.total, truncated=page.truncated, source_page=page.source_page),
                browsed_all=True if browsed_all else None,
            )

        return await call_with_policy(execute, self.config)
