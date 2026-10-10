"""客户扁平条件校验；保留客户操作符，不转换为保单 AST。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic

import math
import re
from datetime import date

from tool_adapter_sdk.adapter import HandlerContractError
from tool_adapter_sdk.customer_detail_catalog import V14_FIELDS
from tool_adapter_sdk.customer_detail_metadata import query_canonical_path as canonical_path

MAX_CONDITIONS = 50
MAX_SORT_FIELDS = 50


def _identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def _field_path(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(parts := value.split(".")) <= 6 and all(map(_identifier, parts))


def _sort_field(value: object) -> bool:
    if not isinstance(value, str) or not _identifier(value) or value == "polNo":
        return False
    path = canonical_path(value)
    # polNo 是仅查询别名；目录容器同样不是标量，不能靠省略点号绕过排序限制。
    return "." not in path and not any(entry.path.startswith(path + ".") for entry in V14_FIELDS)


def _number(value: object) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _scalar(value: object) -> bool:
    return isinstance(value, (str, bool)) or _number(value)


def _range(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"min", "max"}:
        diagnostic("range_shape_invalid", stage="filter_validation")
        raise HandlerContractError()
    lower, upper = value["min"], value["max"]
    if _number(lower) and _number(upper):
        if lower > upper:
            diagnostic("range_order_invalid", stage="filter_validation")
            raise HandlerContractError()
    elif isinstance(lower, str) and isinstance(upper, str):
        try:
            if canonical_path(field) == "birthdaymd":
                if (re.fullmatch(r"\d{2}-\d{2}", lower, flags=re.ASCII) is None
                        or re.fullmatch(r"\d{2}-\d{2}", upper, flags=re.ASCII) is None):
                    diagnostic("month_day_format_invalid", stage="filter_validation")
                    raise HandlerContractError()
                date(2000, int(lower[:2]), int(lower[3:]))
                date(2000, int(upper[:2]), int(upper[3:]))
                if lower > upper:  # 跨年区间不能用一条字符串 BETWEEN 表达
                    diagnostic("month_day_range_wraps_year", stage="filter_validation")
                    raise HandlerContractError()
            elif (re.fullmatch(r"\d{4}-\d{2}-\d{2}", lower, flags=re.ASCII) is None
                    or re.fullmatch(r"\d{4}-\d{2}-\d{2}", upper, flags=re.ASCII) is None
                    or date.fromisoformat(lower) > date.fromisoformat(upper)):
                diagnostic("date_range_invalid", stage="filter_validation")
                raise HandlerContractError()
        except ValueError:
            diagnostic("date_value_invalid", stage="filter_validation")
            raise HandlerContractError() from None
    else:
        diagnostic("range_bound_types_invalid", stage="filter_validation")
        raise HandlerContractError()
    return {"min": lower, "max": upper}


def _condition(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or not _field_path(raw.get("field")):
        diagnostic("condition_field_invalid", stage="filter_validation")
        raise HandlerContractError()
    operator = raw.get("operator")
    if not isinstance(operator, str):
        diagnostic("condition_operator_type_invalid", stage="filter_validation")
        raise HandlerContractError()
    keys = {"field", "operator", "node_id"}
    result = dict(raw)
    if "node_id" in raw and not isinstance(raw["node_id"], str):
        diagnostic("condition_node_id_type_invalid", stage="filter_validation")
        raise HandlerContractError()
    if operator in {"MATCH", "GT", "GTE", "LT", "LTE"}:
        keys.add("value")
        value = raw.get("value")
        if not _scalar(value) or (operator != "MATCH" and isinstance(value, bool)):
            diagnostic("condition_value_invalid", stage="filter_validation")
            raise HandlerContractError()
        if operator == "MATCH":
            keys.add("matchMode")
            if "matchMode" in raw and (raw["matchMode"] != "suffix" or not isinstance(value, str)):
                diagnostic("condition_match_mode_invalid", stage="filter_validation")
                raise HandlerContractError()
    elif operator in {"CONTAINS", "NOT_CONTAINS"}:
        keys.add("values")
        values = raw.get("values")
        if not isinstance(values, list) or not 1 <= len(values) <= 100 or not all(map(_scalar, values)):
            diagnostic("condition_values_invalid", stage="filter_validation")
            raise HandlerContractError()
        result["values"] = list(values)
    elif operator == "RANGE":
        keys.add("value")
        result["value"] = _range(raw.get("value"), field=str(raw["field"]))
    elif operator not in {"EXISTS", "NOT_EXISTS"}:
        diagnostic("condition_operator_unsupported", stage="filter_validation")
        raise HandlerContractError()
    if set(raw) - keys:
        diagnostic("condition_extra_fields", stage="filter_validation")
        raise HandlerContractError()
    return result


def _sort(raw: object) -> dict[str, object]:
    if (not isinstance(raw, dict) or set(raw) - {"field", "order", "priority", "mode"}
            or not _sort_field(raw.get("field")) or raw.get("order") not in ("asc", "desc")
            or ("priority" in raw and not _number(raw["priority"]))
            or ("mode" in raw and raw["mode"] != "")):
        diagnostic("sort_invalid", stage="filter_validation")
        raise HandlerContractError()
    return dict(raw)


def validate_customer_query(raw: object) -> dict[str, object]:
    """返回独立请求快照；分页和可信 trace 由查询函数显式提供。"""
    if (not isinstance(raw, dict) or set(raw) - {"queryLogic", "conditions", "sort"}
            or raw.get("queryLogic") not in ("AND", "OR")):
        diagnostic("query_shape_invalid", stage="filter_validation")
        raise HandlerContractError()
    conditions = raw.get("conditions")
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= MAX_CONDITIONS:
        diagnostic("condition_count_invalid", stage="filter_validation")
        raise HandlerContractError()
    result: dict[str, object] = {"queryLogic": raw["queryLogic"], "conditions": [_condition(c) for c in conditions]}
    if "sort" in raw:
        sort = raw["sort"]
        # 排序列表同样有本地上限，避免在缺少完整字段字典时接受无界指令。
        if not isinstance(sort, list) or len(sort) > MAX_SORT_FIELDS:
            diagnostic("sort_count_invalid", stage="filter_validation")
            raise HandlerContractError()
        result["sort"] = [_sort(item) for item in sort]
    return result
