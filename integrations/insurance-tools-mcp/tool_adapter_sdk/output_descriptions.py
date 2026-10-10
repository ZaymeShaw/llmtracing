"""返回字段说明的只读遍历；业务词义由各领域目录提供。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import cache

from pydantic import JsonValue


def describe_output_page(
    data: dict[str, JsonValue],
    *,
    describe_field: Callable[[str], str],
    canonical_path: Callable[[str], str],
    defined_paths: Iterable[str],
    page_labels: dict[str, str],
    record_description: str,
) -> dict[str, str]:
    """共享路径仅承载词义，真实下标承载形态，避免混合页后写覆盖语义。"""
    descriptions: dict[str, str] = {}
    paths = tuple(defined_paths)

    @cache
    def suffixes(logical: str) -> tuple[str, ...]:
        canonical = canonical_path(logical)
        result: set[str] = set()
        for path in paths:
            if path.startswith(canonical + "."):
                parts = path[len(canonical) + 1:].split(".")
                result.update("." + ".".join(parts[:length]) for length in range(1, len(parts) + 1))
        return tuple(sorted(result))

    @cache
    def shared(logical: str, business: bool) -> str:
        return describe_field(logical) if business else "页扩展属性；业务含义未定义"

    def state(value: JsonValue) -> str:
        if isinstance(value, str):
            return "本条为字符串，未解析；共享子路径仅解释字段，不表示子字段已返回"
        if value is None:
            return "本条为 null，不表示零、否或无关联"
        if isinstance(value, list):
            return "本条为数组" + ("（空）" if not value else "")
        if isinstance(value, dict):
            return "本条为对象" + ("（空）" if not value else "")
        return "本条为标量，不据此推断嵌套业务含义"

    def walk(value: JsonValue, path: str, logical: str, actual: str, *, business: bool = True) -> None:
        descriptions[path] = shared(logical, business)
        children = suffixes(logical) if business else ()
        if business and (children or isinstance(value, (dict, list))):
            descriptions[actual] = state(value)
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}", f"{logical}.{key}", f"{actual}.{key}", business=business)
        elif isinstance(value, list):
            descriptions[path + "[]"] = shared(logical, business)
            for index, item in enumerate(value):
                walk(item, path + "[]", logical, f"{actual}[{index}]", business=business)
        elif isinstance(value, str) and business:
            for suffix in children:
                descriptions[path + "[]" + suffix] = shared(logical + suffix, business)

    for key, value in data.items():
        if key == "records" and isinstance(value, list):
            descriptions[key] = page_labels[key]
            descriptions["records[]"] = record_description
            for index, record in enumerate(value):
                if isinstance(record, dict):
                    for field, item in record.items():
                        walk(item, f"records[].{field}", field, f"records[{index}].{field}")
        else:
            walk(value, key, key, key, business=False)
            if key in page_labels:
                descriptions[key] = page_labels[key]
    return descriptions
