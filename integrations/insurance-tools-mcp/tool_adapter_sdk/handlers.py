"""真实工具 handler 函数表。"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import TypeAlias, TypeVar

from tool_adapter_sdk.contracts import (
    BackendContext,
    BenefitDetailRequest,
    BenefitDetailResult,
    CustomerDetailRequest,
    CustomerDetailResult,
    CustomerSearchRequest,
    CustomerSearchResult,
    HandlerCallResult,
    OPERATION_SPECS,
    PolicyDetailRequest,
    PolicyDetailResult,
    PolicySearchRequest,
    PolicySearchResult,
    ProductRagRequest,
    ProductRagResult,
    ProductResolveRequest,
    ProductResolveResult,
    QaRecallRequest,
    QaRecallResult,
)

ResultT = TypeVar("ResultT")
HandlerOutput: TypeAlias = ResultT | HandlerCallResult[ResultT]

PolicyDetailHandler = Callable[
    [PolicyDetailRequest, BackendContext], Awaitable[HandlerOutput[PolicyDetailResult]]
]
ProductResolveHandler = Callable[
    [ProductResolveRequest, BackendContext], Awaitable[HandlerOutput[ProductResolveResult]]
]
ProductRagHandler = Callable[
    [ProductRagRequest, BackendContext], Awaitable[HandlerOutput[ProductRagResult]]
]
BenefitDetailHandler = Callable[
    [BenefitDetailRequest, BackendContext], Awaitable[HandlerOutput[BenefitDetailResult]]
]
QaRecallHandler = Callable[
    [QaRecallRequest, BackendContext], Awaitable[HandlerOutput[QaRecallResult]]
]
PolicySearchHandler = Callable[
    [PolicySearchRequest, BackendContext], Awaitable[HandlerOutput[PolicySearchResult]]
]
CustomerSearchHandler = Callable[
    [CustomerSearchRequest, BackendContext], Awaitable[HandlerOutput[CustomerSearchResult]]
]
CustomerDetailHandler = Callable[
    [CustomerDetailRequest, BackendContext], Awaitable[HandlerOutput[CustomerDetailResult]]
]


@dataclass(frozen=True)
class RealToolHandlers:
    """内网只需登记已抽取的函数或 Service 绑定方法。"""

    api_version: str
    policy_detail: PolicyDetailHandler | None = None
    product_resolve: ProductResolveHandler | None = None
    product_rag: ProductRagHandler | None = None
    benefit_detail: BenefitDetailHandler | None = None
    qa_recall: QaRecallHandler | None = None
    policy_search: PolicySearchHandler | None = None
    customer_search: CustomerSearchHandler | None = None
    customer_detail: CustomerDetailHandler | None = None


REQUIRED_HANDLERS_BY_TOOL: dict[str, tuple[str, ...]] = {
    "policy_detail": ("policy_detail",),
    "product_knowledge": ("product_resolve", "product_rag"),
    "benefit_info": ("benefit_detail",),
    "general_qa": ("qa_recall",),
    "policy_search": ("policy_search",),
    "customer_search": ("customer_search",),
    "customer_detail": ("customer_detail",),
}


def validate_handler_registry() -> None:
    """锁定 OperationSpec、顶层工具映射与函数表字段三者的一致性。"""
    expected: defaultdict[str, list[str]] = defaultdict(list)
    for operation in OPERATION_SPECS.values():
        expected[operation.owner_tool].append(operation.handler_name)
    # 按集合比较、顺序不参与:contracts.py 里 operation 声明顺序对调属语义等价变更,
    # 不应让"新 contracts 配旧 handlers"以外的正常升级在 import 期误报。
    expected_mapping = {name: tuple(sorted(names)) for name, names in expected.items()}
    declared = {
        name: tuple(sorted(names)) for name, names in REQUIRED_HANDLERS_BY_TOOL.items()
    }
    if declared != expected_mapping:
        raise RuntimeError("REQUIRED_HANDLERS_BY_TOOL 与 OperationSpec 不一致")

    handler_fields = {field.name for field in fields(RealToolHandlers)} - {"api_version"}
    mapped_fields = {
        handler_name
        for handler_names in REQUIRED_HANDLERS_BY_TOOL.values()
        for handler_name in handler_names
    }
    if handler_fields != mapped_fields:
        raise RuntimeError("RealToolHandlers 字段与 operation handler 不一致")


# SDK 被复制或升级后，任何文件版本混搭都尽量在 import 时暴露，而不是等联调请求。
validate_handler_registry()


__all__ = [
    "RealToolHandlers",
    "REQUIRED_HANDLERS_BY_TOOL",
    "validate_handler_registry",
    "HandlerOutput",
    "PolicyDetailHandler",
    "ProductResolveHandler",
    "ProductRagHandler",
    "BenefitDetailHandler",
    "QaRecallHandler",
    "PolicySearchHandler",
    "CustomerSearchHandler",
    "CustomerDetailHandler",
]
