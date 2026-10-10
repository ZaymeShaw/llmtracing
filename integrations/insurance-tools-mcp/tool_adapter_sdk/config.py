"""可独立复制的真实 handler 连接参数，不读取环境或配置文件。"""
from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RetryConfig(BaseModel):
    """首次调用之外的有限重试次数及有上限的退避参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    max_retries: int = Field(default=0, ge=0, le=10)
    initial_backoff_s: float = Field(default=0.1, gt=0)
    max_backoff_s: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def validate_backoff(self) -> RetryConfig:
        if self.initial_backoff_s > self.max_backoff_s:
            raise ValueError("初始退避不得超过最大退避")
        return self


class HandlerConfig(BaseModel):
    """单次 handler 总时限包含重试与退避，认证由原装配路径提供。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    url: str
    query_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    timeout_s: float = Field(gt=0)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    max_concurrency: int = Field(default=8, ge=1, le=128)
    admission_wait_s: float = Field(default=1.0, ge=0, le=2)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    circuit_cooldown_s: float = Field(default=15.0, gt=0, le=300)
    source: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    top_n: int | None = Field(default=None, ge=1, le=100, exclude_if=lambda value: value is None)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("source 不得为空白")
        return value

    @field_validator("query_url")
    @classmethod
    def validate_query_url(cls, value: str | None) -> str | None:
        return cls.validate_url(value) if value is not None else None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and not any(char.isspace() or ord(char) < 32 for char in value)
                and "\\" not in value
            )
            # 访问 port 属性才能校验非法端口和超出范围的端口。
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("接口地址必须为不含用户口令的绝对 HTTP(S) URL")
        return value


__all__ = ["HandlerConfig", "RetryConfig"]
