"""应用出站鉴权配置；不进入独立 SDK 或模型参数。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from tool_adapter_sdk.config import HandlerConfig


DUAL_ENDPOINT_TOOLS = frozenset({"customer_search", "policy_search", "policy_detail"})


class ArkInvokeInterfaces(BaseModel):
    """冻结接口角色映射；禁止拼写错误及显式空值静默降级。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    query_url: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("url", "query_url")
    @classmethod
    def nonempty(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("接口鉴权参数不得为空")
        return value

    @model_validator(mode="after")
    def require_role(self) -> ArkInvokeInterfaces:
        if self.url is None and self.query_url is None:
            raise ValueError("接口鉴权映射至少需要一个角色")
        return self


class ArkAuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    type: Literal["ark"]
    url: str
    client_id: str
    client_secret: SecretStr
    grant_type: str
    invokeInterface: str | ArkInvokeInterfaces

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return HandlerConfig.validate_url(value)

    @field_validator("client_id", "grant_type")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("鉴权参数不得为空")
        return value

    @field_validator("invokeInterface")
    @classmethod
    def nonempty_interface(cls, value: str | ArkInvokeInterfaces) -> str | ArkInvokeInterfaces:
        if isinstance(value, str) and not value.strip():
            raise ValueError("鉴权参数不得为空")
        return value

    def for_endpoint(self, role: Literal["url", "query_url"]) -> ArkAuthConfig | None:
        """缺省角色不鉴权；provider 只接收已解析的启用角色。"""
        if isinstance(self.invokeInterface, str):
            return self
        value = getattr(self.invokeInterface, role)
        if value is None:
            return None
        return self.model_copy(update={"invokeInterface": value})

    def validate_roles(self, *, query_required: bool) -> None:
        if isinstance(self.invokeInterface, ArkInvokeInterfaces):
            if self.invokeInterface.query_url is not None and not query_required:
                raise ValueError("接口鉴权映射包含工具不使用的接口")

    @field_validator("client_secret")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("鉴权参数不得为空")
        return value
