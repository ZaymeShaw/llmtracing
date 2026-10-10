"""保单查询 AST 的边界校验与已确认字段兼容，不生成 SQL。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic

import math
import re

from tool_adapter_sdk.adapter import HandlerContractError

_COMPARE = {"EQ", "NE", "GT", "GTE", "LT", "LTE"}
_STRING = {"CONTAINS", "PREFIX", "SUFFIX"}
_PAY_REASON_FIELD = "pay_record.pay_reason_desc"


def validate_filter(value: object) -> dict[str, object]:
    """有界校验并返回兼容后的新树，不修改上游条件或放宽未知字段路径。"""
    seen: set[str] = set()

    def walk(node: object, depth: int) -> dict[str, object]:
        if depth > 12 or not isinstance(node, dict):
            diagnostic("filter_depth_or_node_type_invalid", stage="filter_validation")
            raise HandlerContractError()
        node_id = node.get("node_id")
        if (not isinstance(node_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", node_id) is None
                or node_id in seen or len(seen) >= 200):
            diagnostic("filter_node_identity_or_count_invalid", stage="filter_validation")
            raise HandlerContractError()
        seen.add(node_id)
        kind = node.get("type")
        if kind in ("AND", "OR"):
            _keys(node, {"type", "node_id", "children"})
            children = node["children"]
            if not isinstance(children, list) or not 2 <= len(children) <= 50:
                diagnostic("filter_children_count_invalid", stage="filter_validation")
                raise HandlerContractError()
            return {**node, "children": [walk(child, depth + 1) for child in children]}
        elif kind == "NOT":
            _keys(node, {"type", "node_id", "child"})
            return {**node, "child": walk(node["child"], depth + 1)}
        elif kind == "PREDICATE":
            _predicate(node)
            # 仅兼容已确认的旧查询键，不从响应字段目录推导其他查询路径。
            return {**node, "field": _PAY_REASON_FIELD if node["field"] == "pay_reason_desc" else node["field"]}
        else:
            diagnostic("filter_node_type_unsupported", stage="filter_validation")
            raise HandlerContractError()

    return walk(value, 1)


def _keys(value: dict, keys: set[str]) -> None:
    if set(value) != keys:
        diagnostic("filter_keys_invalid", stage="filter_validation")
        raise HandlerContractError()


def _scalar(value: object) -> None:
    if type(value) not in (str, int, float, bool) or (isinstance(value, float) and not math.isfinite(value)):
        diagnostic("filter_scalar_invalid", stage="filter_validation")
        raise HandlerContractError()


def _predicate(node: dict) -> None:
    field, operator = node.get("field"), node.get("operator")
    if not isinstance(field, str) or (
        field != _PAY_REASON_FIELD and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field) is None
    ):
        diagnostic("filter_field_invalid", stage="filter_validation")
        raise HandlerContractError()
    if not isinstance(operator, str):
        diagnostic("filter_operator_type_invalid", stage="filter_validation")
        raise HandlerContractError()
    keys = {"type", "node_id", "field", "operator"}
    if operator in _COMPARE | _STRING:
        _keys(node, keys | {"value"})
        _scalar(node["value"])
        if operator in _STRING and not isinstance(node["value"], str):
            diagnostic("filter_string_value_invalid", stage="filter_validation")
            raise HandlerContractError()
    elif operator in {"IN", "NOT_IN"}:
        _keys(node, keys | {"values"})
        values = node["values"]
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            diagnostic("filter_values_count_invalid", stage="filter_validation")
            raise HandlerContractError()
        for value in values:
            _scalar(value)
    elif operator == "BETWEEN":
        _keys(node, keys | {"range"})
        bounds = node["range"]
        if not isinstance(bounds, dict):
            diagnostic("filter_range_type_invalid", stage="filter_validation")
            raise HandlerContractError()
        _keys(bounds, {"lower", "upper", "include_lower", "include_upper"})
        _scalar(bounds["lower"])
        _scalar(bounds["upper"])
        if type(bounds["include_lower"]) is not bool or type(bounds["include_upper"]) is not bool:
            diagnostic("filter_range_inclusion_type_invalid", stage="filter_validation")
            raise HandlerContractError()
    elif operator in {"IS_NULL", "IS_NOT_NULL"}:
        _keys(node, keys)
    else:
        diagnostic("filter_operator_unsupported", stage="filter_validation")
        raise HandlerContractError()
