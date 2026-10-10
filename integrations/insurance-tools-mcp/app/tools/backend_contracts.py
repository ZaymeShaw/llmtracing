"""SDK 业务结果到框架计量字段与 ToolEnvelope 错误的兼容边界。"""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.envelope import make_envelope
from app.schemas import EnvelopeStatus, ErrorCode, SourceRef, ToolError
from tool_adapter_sdk.contracts import (
    BenefitDetailResult,
    BenefitItemDto,
    CustomerCandidateDto,
    CustomerDetailResult,
    CustomerSearchResult,
    PolicyCandidateDto,
    PolicyDetailResult,
    PolicySearchResult,
    ProductCandidateDto,
    ProductRagResult,
    ProductResolveResult,
    QaPairDto,
    QaRecallResult,
)


class BackendResponse(BaseModel):
    """框架统一追加的调用计量字段。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    request_id: str = Field(min_length=1, description="单次逻辑后端调用标识，用于计量及事实、证据溯源。")
    latency_ms: int = Field(ge=0, description="后端调用耗时，单位为毫秒，非负整数。")


class PolicyDetailResponse(PolicyDetailResult, BackendResponse):
    """统一保单详情接口：支持完整保单号或尾号候选。"""

    model_config = ConfigDict(frozen=True, extra="ignore")


class PolicySearchResponse(PolicySearchResult, BackendResponse):
    model_config = ConfigDict(frozen=True, extra="ignore")


class CustomerSearchResponse(CustomerSearchResult, BackendResponse):
    model_config = ConfigDict(frozen=True, extra="ignore")


class CustomerDetailResponse(CustomerDetailResult, BackendResponse):
    model_config = ConfigDict(frozen=True, extra="ignore")


class ProductResolveResponse(ProductResolveResult, BackendResponse):
    model_config = ConfigDict(frozen=True, extra="ignore")


class ProductRagResponse(ProductRagResult, BackendResponse):
    model_config = ConfigDict(frozen=True, extra="ignore")


class BenefitDetailResponse(BenefitDetailResult, BackendResponse):
    model_config = ConfigDict(frozen=True, extra="ignore")


class QaRecallResponse(QaRecallResult, BackendResponse):
    model_config = ConfigDict(frozen=True, extra="ignore")


ResponseT = TypeVar("ResponseT", bound=BaseModel)


def parse_backend_response(
    model: type[ResponseT], raw: dict, *, source: SourceRef
) -> ResponseT | str:
    """验证端点响应；schema 漂移归一为安全 ERROR 信封。"""
    try:
        return model.model_validate(raw)
    except (ValidationError, ValueError):
        return make_envelope(
            status=EnvelopeStatus.ERROR,
            error=ToolError(code=ErrorCode.UPSTREAM, retryable=False),
            hint="上游响应格式不符合接口契约，请联系服务维护人员",
            source=source,
            request_id=str(raw.get("request_id") or "") or None,
            latency_ms=raw.get("latency_ms") if isinstance(raw.get("latency_ms"), int) else None,
        )


__all__ = [
    "BackendResponse",
    "PolicyCandidateDto",
    "CustomerCandidateDto",
    "ProductCandidateDto",
    "BenefitItemDto",
    "QaPairDto",
    "BenefitDetailResponse",
    "CustomerDetailResponse",
    "CustomerSearchResponse",
    "PolicyDetailResponse",
    "PolicySearchResponse",
    "ProductRagResponse",
    "ProductResolveResponse",
    "QaRecallResponse",
    "parse_backend_response",
]
