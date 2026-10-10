"""可选的 Langfuse 启动配置；错误仅隔离此观测目的地。"""
from __future__ import annotations

from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from app.conf.runtime import current_source, load_model


class LangfuseConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    host: str = ""
    public_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    name_prefix: str = Field(default="agentic-insurance-qa-agent", min_length=1, max_length=128)
    tags: list[str] = Field(default_factory=lambda: ["统一智能体"], max_length=32)
    capture_content: bool = False
    sample_rate: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    max_content_chars: int = Field(default=4000, ge=64, le=20000)
    max_queue_size: int = Field(default=2048, ge=1, le=65536)
    max_export_batch_size: int = Field(default=128, ge=1, le=4096)
    schedule_delay_ms: int = Field(default=1000, ge=100, le=60000)
    export_timeout_s: float = Field(default=3.0, gt=0, le=30, allow_inf_nan=False)
    shutdown_timeout_ms: int = Field(default=5000, ge=100, le=30000)

    @field_validator("name_prefix")
    @classmethod
    def prefix_valid(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("名称前缀无效")
        return value.strip().rstrip(".") or "agentic-insurance-qa-agent"

    @field_validator("tags")
    @classmethod
    def tags_valid(cls, value: list[str]) -> list[str]:
        if any(not tag.strip() or len(tag) > 128 or any(ord(c) < 32 for c in tag) for tag in value):
            raise ValueError("标签无效")
        return list(dict.fromkeys(tag.strip() for tag in value))

    @model_validator(mode="after")
    def validate_connection(self) -> LangfuseConf:
        parts = [self.host.strip(), self.public_key.get_secret_value().strip(), self.secret_key.get_secret_value().strip()]
        if any(parts) and not all(parts):
            raise ValueError("连接字段必须完整配置")
        if parts[0]:
            url = urlsplit(parts[0])
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("连接地址无效")
            _ = url.port
        if self.max_export_batch_size > self.max_queue_size:
            raise ValueError("批次不能大于队列")
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.host.strip())


def langfuse_conf() -> LangfuseConf:
    source = current_source()
    if not source.path("langfuse").exists() and not source.common_path("langfuse").exists():
        return LangfuseConf()
    return load_model("langfuse", LangfuseConf, common=True,
                      environment_only=("host", "public_key", "secret_key"))
