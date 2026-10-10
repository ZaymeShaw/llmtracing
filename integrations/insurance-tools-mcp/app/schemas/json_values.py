"""Strict JSON value validation kept in the contract layer."""
from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, JsonValue, TypeAdapter

_JSON: TypeAdapter[JsonValue] = TypeAdapter(
    JsonValue, config=ConfigDict(strict=True, allow_inf_nan=False)
)


def validate_json(value: object) -> JsonValue:
    return _JSON.validate_python(value)


StrictJsonValue = Annotated[JsonValue, BeforeValidator(validate_json)]
