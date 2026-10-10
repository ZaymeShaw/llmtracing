"""产品知识分块检索薄适配；名称提示不等于产品 ID 级过滤。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details

import asyncio

from app.tools.real.auth import HttpClient
from pydantic import Field, StrictInt, ValidationError

from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call

from app.tools.real.response_normalization import ExternalResponse, parse_documents, retain_document_prefix
from app.tools.real.java_response import validate_trace_id
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout
from tool_adapter_sdk.call_policy import call_with_policy
from tool_adapter_sdk.deadline import handler_deadline
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.contracts import (
    BackendContext, HandlerCallResult, KnowledgeDocumentDto, ProductRagRequest, ProductRagResult,
)

post_json = observed_call("backend.product_rag.request")(_post_json)


class _ChunkResponse(ExternalResponse):
    code: StrictInt = Field(description="上游业务状态码，0 表示成功；其他值按契约错误处理，不当作空结果。")
    docs: list[object] = Field(description="召回正文列表；空列表表示无召回，缺字段则是契约错误。")



class ProductRagHandler:
    """仅在调用方锁定单产品后检索；不伪造评分、版本或结构化条款。"""

    def __init__(self, client: HttpClient, config: HandlerConfig) -> None:
        self.client = client
        self.config = config

    async def __call__(
        self, request: ProductRagRequest, context: BackendContext,
    ) -> HandlerCallResult[ProductRagResult]:
        validate_trace_id(context.trace_id)
        top_n = self.config.top_n if self.config.top_n is not None else 5
        loop = asyncio.get_running_loop()
        deadline = handler_deadline(self.config.timeout_s)

        def remaining() -> float:
            value = deadline - loop.time()
            if value <= 0:
                diagnostic("handler_deadline_exhausted", stage="deadline")
                raise HandlerTimeout()
            return value

        async def recall() -> ProductRagResult:
            identity = request.standard_name
            if request.product_code is not None:
                identity += f"（险种代码：{request.product_code}）"
            # 每次尝试只携带剩余预算（内紧外松），重试不得携带全量超时穿透总预算。
            raw = await post_json(
                self.client, self.config.url,
                {"source": self.config.source or "product_wiki",
                 "user_text": f"已确认产品：{identity}\n查询问题：{request.query}",
                 "top_n": top_n, "trace_id": context.trace_id},
                timeout_s=remaining(),
            )
            if not isinstance(raw, dict) or type(raw.get("code")) is not int or raw["code"] != 0:
                diagnostic(
                    "response_object_type_invalid" if not isinstance(raw, dict) else "business_code_type_invalid" if type(raw.get("code")) is not int else "business_code_rejected",
                    stage="response_validation",
                    business_code=raw.get("code") if isinstance(raw, dict) and type(raw.get("code")) is int else None,
                    actual_type=type(raw.get("code") if isinstance(raw, dict) else raw).__name__,
                )
                raise HandlerContractError()
            try:
                response = _ChunkResponse.model_validate(retain_document_prefix(raw, top_n))
            except ValidationError as exc:
                diagnostic("response_schema_invalid", stage="response_validation", **validation_details(exc, _ChunkResponse))
                raise HandlerContractError() from None
            documents, diagnostics = parse_documents(response.docs, KnowledgeDocumentDto)
            return ProductRagResult(
                standard_name=request.standard_name, found=bool(documents), documents=documents, document_diagnostics=diagnostics,
            )

        return await call_with_policy(recall, self.config)
