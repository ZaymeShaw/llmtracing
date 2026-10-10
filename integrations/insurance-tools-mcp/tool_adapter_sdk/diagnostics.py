"""SDK 独立的安全诊断：调用方只传静态原因、计数和可信关联标识。"""
from __future__ import annotations

import logging
import types
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorType

logger = logging.getLogger(__name__)
_scope: ContextVar[dict[str, Any]] = ContextVar("tool_diagnostic_scope", default={})
_ERROR_TYPES = frozenset(get_args(ErrorType))


@contextmanager
def diagnostic_scope(**fields: Any) -> Iterator[None]:
    """恢复嵌套及并发调用的关联字段，不依赖宿主应用。"""
    token = _scope.set({**_scope.get(), **fields})
    try:
        yield
    finally:
        _scope.reset(token)


def diagnostic(reason: str, *, stage: str, error: BaseException | None = None, **fields: Any) -> None:
    """不读取异常文本、局部变量或源码；不接受业务正文作为 fields。"""
    details = {**_scope.get(), **fields, "reason": reason, "stage": stage}
    if error is not None:
        details["error_type"] = type(error).__name__
        frames: list[dict[str, str | int]] = []
        traceback = error.__traceback__
        while traceback is not None and len(frames) < 20:
            code = traceback.tb_frame.f_code
            frames.append({"file": code.co_filename, "function": code.co_name, "line": traceback.tb_lineno})
            traceback = traceback.tb_next
        details["error_frames"] = frames
    unexpected = reason.endswith("unclassified_exception") or reason in {
        "unexpected_backend_exception", "unexpected_tool_exception", "unexpected_exception",
    }
    level = logging.ERROR if unexpected else logging.WARNING
    if reason in {
        "results_truncated", "response_truncated", "candidates_truncated", "retry_scheduled",
        "recall_results_truncated", "product_candidates_truncated", "product_candidates_deduplicated",
        "policy_candidates_grouped", "response_extra_fields_ignored",
        "response_message_absent", "product_descriptions_absent",
    }:
        level = logging.INFO
    logger.log(level, "tool_call_diagnostic", extra={"observability": details})


def _variants(annotation: Any) -> list[Any]:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _variants(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return [item for child in get_args(annotation) for item in _variants(child)]
    return [annotation]


def _safe_location(location: tuple[Any, ...], model: type[BaseModel]) -> list[str | int]:
    candidates: list[Any] = [model]
    result: list[str | int] = []
    for part in location[:20]:
        next_candidates: list[Any] = []
        known = False
        for candidate in candidates:
            for variant in _variants(candidate):
                origin = get_origin(variant)
                args = get_args(variant)
                if isinstance(variant, type) and issubclass(variant, BaseModel):
                    for name, field in variant.model_fields.items():
                        if isinstance(part, str) and part in (name, field.alias, field.validation_alias):
                            known = True
                            next_candidates.append(field.annotation)
                elif origin in (list, tuple, set, frozenset) and isinstance(part, int):
                    known = True
                    next_candidates.extend(args[:1])
                elif origin is dict:
                    # 字典键来自业务数据，即使与别处 schema 字段同名也不能放行。
                    next_candidates.extend(args[1:2])
        result.append(part if known and isinstance(part, (str, int)) else "<unknown>")
        candidates = next_candidates
    return result


def validation_details(exc: ValidationError, model: type[BaseModel]) -> dict[str, Any]:
    """只投影 schema 路径与错误类别，未知动态键及自定义错误类别遮蔽。"""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return {
        "validation_error_count": exc.error_count(),
        "validation_errors": [
            {"loc": _safe_location(item["loc"], model),
             "type": item["type"] if item["type"] in _ERROR_TYPES else "custom_error"}
            for item in errors[:20]
        ],
    }
