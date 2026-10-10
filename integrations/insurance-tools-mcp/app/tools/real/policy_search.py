"""保单条件解析与有界查询，共享一个逻辑调用预算。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.tools.real.auth import HttpClient
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator

from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call

from app.tools.real.java_response import validate_trace_id
from app.tools.real.policy_query import query_policy_page, search_result
from tool_adapter_sdk.identity import resolve_agent_number
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout, ToolConfigurationError
from tool_adapter_sdk.call_policy import call_with_policy
from tool_adapter_sdk.deadline import handler_deadline
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.contracts import BackendContext, HandlerCallResult, PolicySearchRequest, PolicySearchResult

post_json = observed_call("backend.policy_search.request")(_post_json)


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["SUCCESS", "UNSUPPORTED"] = Field(description="解析 SUCCESS/UNSUPPORTED；不等于最终工具信封状态。")
    query: StrictStr | None = Field(default=None, description="解析回显的保单自然语言问题；当前保单分支不校验与请求原文相等。")
    filter: dict[str, object] | None = Field(default=None, description="保单结构化条件树；SUCCESS 时必须提供，UNSUPPORTED 时为 null；不保证物理字段存在。")
    message: StrictStr = Field(min_length=1, description="保单解析说明；不支持条件时作为 unsupported_message 返回，必须非空白。")

    @model_validator(mode="after")
    def validate_branch(self) -> _Result:
        if not self.message.strip() or (self.status == "SUCCESS") != (self.filter is not None):
            raise ValueError("解析状态与条件不一致")
        return self


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    policySearchParseResult: _Result = Field(description="保单解析结果分支，非保单出参记录；SUCCESS 后才执行结构化查询。")


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    end_flag: StrictInt = Field(ge=1, le=1, description="解析结束标记，当前只接受 1。")
    extra_output_params: _Output = Field(description="解析器结构化输出，不将调试文案作为查询条件。")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    code: StrictInt = Field(description="上游业务状态码，0 表示成功；其他值按契约错误处理，不当作空结果。")
    msg: StrictStr = Field(description="上游状态说明；不直接作为业务事实或向模型透传错误原文。")
    data: _Data | None = Field(description="业务数据容器；是否允许为空由具体响应分支验证。")


class PolicySearchHandler:
    def __init__(
        self, client: HttpClient, config: HandlerConfig, *, query_client: HttpClient | None = None,
    ) -> None:
        self.query_client = query_client if query_client is not None else client
        if config.query_url is None:
            raise ToolConfigurationError("policy_search 必须配置 query_url")
        self.client, self.config = client, config
        self.query_url = config.query_url

    async def __call__(
        self, request: PolicySearchRequest, context: BackendContext,
    ) -> HandlerCallResult[PolicySearchResult]:
        empno = resolve_agent_number(context.empno, context.upstream_user_id)
        loop = asyncio.get_running_loop()
        deadline = handler_deadline(self.config.timeout_s)

        def remaining() -> float:
            value = deadline - loop.time()
            if value <= 0:
                diagnostic("handler_deadline_exhausted", stage="deadline")
                raise HandlerTimeout()
            return value

        async def execute() -> PolicySearchResult:
            # 同步校验与解析也计入总预算；检查阶段间 deadline，避免晚到结果发起新 HTTP。
            validate_trace_id(context.trace_id)
            if (not isinstance(context.session_id, str) or not context.session_id.strip()
                    or not isinstance(context.agent_id, str) or not context.agent_id.strip()):
                diagnostic("session_or_agent_context_invalid", stage="request_validation")
                raise HandlerContractError()
            if not isinstance(context.upstream_user_id, str) or not context.upstream_user_id.strip():
                diagnostic("upstream_user_id_missing", stage="request_validation")
                raise HandlerContractError("缺少上游请求体 user_id")
            # 每次尝试只携带剩余预算（内紧外松），重试不得携带全量超时穿透总预算。
            raw = await post_json(self.client, self.config.url, {
                "session_id": context.session_id, "trace_id": context.trace_id,
                "extra_input_params": {
                    "policySearchParseArgs": {
                        "query": request.query, "currentTime": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                        "agentCode": context.upstream_user_id,
                    },
                    # 内置解析服务固定空历史；SDK 兼容字段不进入出站请求。
                    "args": {"contexts": []},
                },
            }, timeout_s=remaining())
            try:
                response = _Response.model_validate(raw)
            except ValidationError as exc:
                diagnostic("response_schema_invalid", stage="response_validation", **validation_details(exc, _Response))
                raise HandlerContractError() from None
            if response.code != 0 or response.data is None:
                diagnostic(
                    "business_code_rejected" if response.code != 0 else "response_data_missing",
                    stage="response_validation",
                    business_code=response.code,
                )
                raise HandlerContractError()
            result = response.data.extra_output_params.policySearchParseResult
            if result.status == "UNSUPPORTED":
                diagnostic("parser_policy_unsupported", stage="query_normalization")
                return PolicySearchResult(policies=[], unsupported_message=result.message)
            page = await query_policy_page(self.query_client, self.query_url, result.filter,
                                           empno=empno, trace_id=context.trace_id, timeout_s=remaining())
            return search_result(page)

        return await call_with_policy(execute, self.config)
