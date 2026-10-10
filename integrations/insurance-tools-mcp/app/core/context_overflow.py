"""Bounded request-only recovery; tool messages and evidence remain authoritative."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ContextRecoveryConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=True, strict=True)
    excerpt_chars: int = Field(default=1200, ge=128, le=8000, strict=True)
    max_input_chars: int = Field(default=1048576, ge=1024, le=4194304, strict=True)
    max_messages: int = Field(default=512, ge=1, le=2048, strict=True)
    max_facts: int = Field(default=1024, ge=1, le=4096, strict=True)
