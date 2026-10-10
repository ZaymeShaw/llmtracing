"""产品实体解析线协议与 SDK 三态结果的薄适配。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details

import asyncio

from app.tools.real.auth import HttpClient
from pydantic import Field, StrictInt, StrictStr, ValidationError, model_validator
from typing import Any, Literal

from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call

from app.tools.real.response_normalization import ExternalObject, ExternalResponse
from app.tools.real.java_response import validate_trace_id
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout
from tool_adapter_sdk.call_policy import call_with_policy
from tool_adapter_sdk.deadline import handler_deadline
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.contracts import (
    BackendContext, HandlerCallResult, ProductCandidateDto, ProductResolveRequest, ProductResolveResult,
)

post_json = observed_call("backend.product_resolve.request")(_post_json)


class _Product(ExternalObject):
    """已知产品身份与描述字段；未知扩展键仅在外部边界忽略。"""

    code: StrictStr = Field(alias="险种代码", min_length=1, description="险种代码；与产品全称共同标识候选，不作为产品名称。")
    name: StrictStr = Field(alias="产品全称", min_length=1, description="产品全称；不得为空白。")
    short_name: StrictStr | None = Field(default=None, alias="产品标准简称", description="产品标准简称，按上游原值保留。")
    aliases: StrictStr | None = Field(default=None, alias="产品别称", description="产品别称原始字符串，不自行拆解匹配。")
    start_date: StrictStr | None = Field(default=None, alias="开始时间", description="产品开始时间原值；不推定条款生效时间。")
    end_date: StrictStr | None = Field(default=None, alias="结束时间", description="产品结束时间原值；不直接认定为停售日期。")
    category: StrictStr | None = Field(default=None, alias="产品类别", description="产品类别原值，不重分类。")
    product_type: StrictStr | None = Field(default=None, alias="产品类型", description="产品类型原值。")
    main_rider: StrictStr | None = Field(default=None, alias="主附加险", description="主附加险属性原值。")
    channel: StrictStr | None = Field(default=None, alias="销售渠道", description="销售渠道原值。")
    public_web: StrictStr | None = Field(default=None, alias="发布外网", description="发布外网标记原始字符串，不转换布尔值。")
    sale_status: StrictStr | None = Field(default=None, alias="销售状态", description="产品销售状态原值，不等于保单状态。")

    @model_validator(mode="before")
    @classmethod
    def observe_missing_descriptions(cls, value: Any) -> Any:
        if isinstance(value, dict):
            # 仅统计声明为可选的描述，不记录字段名或上游值。
            optional = [field.alias for field in cls.model_fields.values() if not field.is_required()]
            missing = sum(key not in value for key in optional)
            nulls = sum(key in value and value[key] is None for key in optional)
            if missing or nulls:
                diagnostic("product_descriptions_absent", stage="response_normalization",
                           missing_count=missing, null_count=nulls)
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> "_Product":
        if not self.code.strip() or not self.name.strip():
            raise ValueError("产品代码和全称不能为空白")
        return self


class _Entity(ExternalObject):
    input_context: StrictStr = Field(description="实体匹配使用的输入上下文，服务可进行标准化。")
    match_type: Literal["exact", "fuzzy"] = Field(description="上游实体匹配类型 exact/fuzzy，单一 fuzzy 也不自动升级 exact。")
    extracted_product: StrictStr = Field(description="上游抽取的产品实体文本。")
    products: list[_Product] = Field(description="该实体的候选产品元数据；不直接等同已确认条款。")


class _Data(ExternalObject):
    results: list[_Entity] = Field(description="每个输入实体的解析结果列表。")


class _ParseResponse(ExternalResponse):
    code: StrictInt = Field(description="上游业务状态码，0 表示成功；其他值按契约错误处理，不当作空结果。")
    data: _Data = Field(description="产品实体解析数据容器，必须提供；results 为空或各实体均无候选时表示未匹配。")


def _normalize(response: _ParseResponse, top_n: int) -> ProductResolveResult:
    products: dict[tuple[str, str], _Product] = {}
    for entity in response.data.results:
        for product in entity.products:
            key = (product.code, product.name)
            if key in products and products[key] != product:
                # 同一身份返回矛盾元数据，不能取第一条悄悄覆盖。
                diagnostic("product_metadata_conflict", stage="response_validation")
                raise HandlerContractError()
            products[key] = product
    returned_count = sum(len(entity.products) for entity in response.data.results)
    if returned_count > len(products):
        diagnostic("product_candidates_deduplicated", stage="response_normalization", returned_count=returned_count, retained_count=len(products))
    if not products:
        return ProductResolveResult(match="empty", standard_name="", candidates=[])
    if (len(response.data.results) == 1 and response.data.results[0].match_type == "exact"
            and len(products) == 1):
        product = next(iter(products.values()))
        return ProductResolveResult(
            match="exact", standard_name=product.name, product_code=product.code, candidates=[],
        )
    candidates = [
        ProductCandidateDto(
            name=product.name, product_code=product.code,
            intro="；".join(f"{key}:{value}" for key, value in product.model_dump(by_alias=True, exclude_none=True).items()),
        )
        for product in products.values()
    ]
    if len(candidates) > top_n:
        diagnostic("product_candidates_truncated", stage="response_normalization", returned_count=len(candidates), retained_count=top_n)
    # 先基于完整结果判断歧义，再限量，避免截断后误认唯一精确产品。
    return ProductResolveResult(match="fuzzy", standard_name="", candidates=candidates[:top_n])


class ProductResolveHandler:
    """字符串查询避开上游实体数组缺字段分支；不把单 fuzzy 变为 exact。"""

    def __init__(self, client: HttpClient, config: HandlerConfig) -> None:
        self.client = client
        self.config = config

    async def __call__(
        self, request: ProductResolveRequest, context: BackendContext,
    ) -> HandlerCallResult[ProductResolveResult]:
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

        async def resolve() -> ProductResolveResult:
            # 每次尝试只携带剩余预算（内紧外松），重试不得携带全量超时穿透总预算。
            raw = await post_json(
                self.client, self.config.url,
                {"query": request.query, "top_n": top_n, "trace_id": context.trace_id},
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
                return _normalize(_ParseResponse.model_validate(raw), top_n)
            except ValidationError as exc:
                diagnostic("response_schema_invalid", stage="response_validation", **validation_details(exc, _ParseResponse))
                raise HandlerContractError() from None

        return await call_with_policy(resolve, self.config)
