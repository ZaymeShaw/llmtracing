"""Framework-free contracts shared by configuration and tool consumers."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

SelectionRule = Literal['policy_search', 'customer_search', 'policy_shape']


class OutputShapingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    max_chars: int = Field(default=8000, ge=512, le=64000, strict=True)
    item_chars: int = Field(default=1200, ge=128, le=8000, strict=True)
    max_input_chars: int = Field(default=262144, ge=1024, le=1048576, strict=True,
                                 description="Projection input limit, applied after business record projection")
    max_references: int = Field(default=512, ge=1, le=4096, strict=True)
    max_records: int = Field(default=64, ge=1, le=512, strict=True)
    max_nodes: int = Field(default=8192, ge=64, le=32768, strict=True)
    max_containers: int = Field(default=64, ge=1, le=256, strict=True)
