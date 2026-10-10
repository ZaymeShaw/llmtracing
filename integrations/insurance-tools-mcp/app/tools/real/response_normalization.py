"""外部响应的浅层投影；内部 SDK DTO 继续执行原有严格校验。"""
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator

from tool_adapter_sdk.diagnostics import diagnostic, validation_details
from tool_adapter_sdk.adapter import HandlerContractError
from tool_adapter_sdk.contracts import BenefitDocumentDto, DocumentDiagnosticsDto, KnowledgeDocumentDto


def project_external_object(value: Any, model: type[BaseModel]) -> Any:
    """只丢弃当前对象的未知键；不修复类型、别名冲突或嵌套业务数据。"""
    if not isinstance(value, dict):
        return value
    known = {field.alias or name for name, field in model.model_fields.items()}
    if model.model_config.get("validate_by_name"):
        known.update(model.model_fields)
    ignored_count = sum(key not in known for key in value)
    if ignored_count:
        diagnostic("response_extra_fields_ignored", stage="response_normalization", ignored_count=ignored_count)
    return {key: item for key, item in value.items() if key in known}


def retain_document_prefix(value: dict[str, Any], limit: int) -> dict[str, Any]:
    """成功信封的 docs 先限量再校验条目，不修改原响应或补齐坏条目。"""
    docs = value.get("docs")
    if not isinstance(docs, list) or len(docs) <= limit:
        return value
    diagnostic(
        "recall_results_truncated", stage="response_normalization",
        returned_count=len(docs), retained_count=limit,
    )
    return {**value, "docs": docs[:limit]}


class ExternalObject(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    @model_validator(mode="before")
    @classmethod
    def project_known_fields(cls, value: Any) -> Any:
        return project_external_object(value, cls)


class ExternalResponse(ExternalObject):
    message: StrictStr | None = Field(default=None, description="可选上游说明文本，不参与业务成功判定。")

    @model_validator(mode="before")
    @classmethod
    def observe_missing_message(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("message") is None:
            diagnostic(
                "response_message_absent", stage="response_normalization",
                missing_count=int("message" not in value), null_count=int("message" in value),
            )
        return value


DocumentT = TypeVar("DocumentT", KnowledgeDocumentDto, BenefitDocumentDto)


def parse_documents(
    values: list[object], model: type[DocumentT],
) -> tuple[list[DocumentT], DocumentDiagnosticsDto | None]:
    """只处理已截取的前缀；坏条目不补尾部，缺来源不伪造来源。"""
    documents: list[DocumentT] = []
    positions: list[int] = []
    missing_metadata = False
    for position, value in enumerate(values, 1):
        projected = project_external_object(value, model)
        if isinstance(projected, dict) and isinstance(projected.get("source"), str) and not projected["source"].strip():
            projected = {**projected, "source": None}
        try:
            document = model.model_validate(projected)
        except ValidationError as exc:
            diagnostic(
                "document_item_rejected", stage="document_validation", position=position,
                **validation_details(exc, model),
            )
            continue
        documents.append(document)
        positions.append(position)
        if document.source is None or document.index is None:
            missing_metadata = True
            diagnostic(
                "document_metadata_missing", stage="document_validation", position=position,
                missing_source_count=int(document.source is None), missing_index_count=int(document.index is None),
            )
    discarded_count = len(values) - len(documents)
    if values and not documents:
        diagnostic("all_documents_invalid", stage="document_validation", considered_count=len(values), discarded_count=discarded_count)
        raise HandlerContractError()
    diagnostics = None
    if discarded_count or missing_metadata:
        diagnostics = DocumentDiagnosticsDto(
            considered_count=len(values), discarded_count=discarded_count, positions=positions,
        )
    return documents, diagnostics
