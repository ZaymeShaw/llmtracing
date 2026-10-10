"""无损 JSON 值边界，拒绝非 JSON 对象、隐式类型转换和非有限数。"""
from __future__ import annotations

import json
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, JsonValue, TypeAdapter

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue, config=ConfigDict(strict=True, allow_inf_nan=False))


def validate_json(value: object) -> JsonValue:
    return _JSON.validate_python(value)


StrictJsonValue = Annotated[JsonValue, BeforeValidator(validate_json)]


def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 键不能安全投影")
        result[key] = value
    return result


def reject_constant(value: str) -> JsonValue:
    raise ValueError("非标准 JSON 数值不能安全投影")


def inspect_json_container(value: str) -> JsonValue:
    """仅供已登记容器只读检查；与模型投影共用严格解析，不改写原字符串。"""
    if len(value) > 1024 * 1024:
        return None
    try:
        decoded = json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, (dict, list)):
        return None
    pending = [(decoded, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            return None
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return decoded
