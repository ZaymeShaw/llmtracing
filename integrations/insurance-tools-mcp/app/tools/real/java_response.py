"""Java 查询响应的边界校验；具体分页约束由业务 handler 决定。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details

from app.observability.langfuse_calls import observed_call

import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from tool_adapter_sdk.adapter import HandlerContractError, HandlerUnavailable

class PageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    page_no: StrictInt = Field(alias="pageNo", ge=1, description="当前页码，从 1 起；具体请求页码约束由调用方校验。")
    page_size: StrictInt = Field(alias="pageSize", ge=1, description="服务返回页容量；不是实际返回记录数。")
    total: StrictInt = Field(ge=0, description="匹配记录总数；客户按客户计数，保单按险种行计数。")
    records: list[dict[str, object]] = Field(description="当前页记录，额外列只由具体 handler 的明确映射消费。")


class JavaResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    code: StrictInt = Field(description="查询业务状态码；0 表示成功，1001/1005 表示服务不可用，其他值为契约错误。")
    msg: StrictStr = Field(description="上游状态说明；不直接作为业务事实或向模型透传错误原文。")
    trace_id: StrictStr = Field(alias="traceId", description="上游返回链路标识，必须与本轮请求一致。")
    data: PageResponse | None = Field(description="业务数据容器；是否允许为空由具体响应分支验证。")


def validate_trace_id(trace_id: str) -> None:
    if not isinstance(trace_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", trace_id) is None:
        diagnostic("trace_id_format_invalid", stage="request_validation")
        raise HandlerContractError()


@observed_call("result.parse_page")
def parse_page(raw: object, trace_id: str) -> PageResponse:
    """不暴露业务错误原文；未知错误码不能视为零结果。"""
    try:
        response = JavaResponse.model_validate(raw)
    except ValidationError as exc:
        diagnostic("response_schema_invalid", stage="response_validation", **validation_details(exc, JavaResponse))
        raise HandlerContractError() from None
    # 错误响应的 traceId 是服务端新生成的（文档错误示例即 UUID），
    # 先按业务码分类可用性/契约错误，回显一致性只在成功响应上校验。
    if response.code in {1001, 1005}:
        diagnostic("business_unavailable", stage="response_validation", business_code=response.code)
        raise HandlerUnavailable()
    if response.code != 0 or response.data is None:
        diagnostic(
            "business_code_rejected" if response.code != 0 else "response_data_missing",
            stage="response_validation",
            business_code=response.code,
        )
        raise HandlerContractError()
    if response.trace_id != trace_id:
        diagnostic("response_trace_mismatch", stage="response_validation")
        raise HandlerContractError()
    return response.data
