"""类型化实体记忆条目（v13 §8.1④ · 7-b3）。

实体状态只接受工具事实、UI 结构化选择或确定性解析产物；模型与答案文本都不是
写入来源。``PolicyEntityRef`` 为 G-3 名称回指提供标准 ID 对应的类型化名称字段。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PolicyEntityRef(BaseModel):
    """会话内已确认保单实体：标准保单号及可确定性匹配的名称字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_no: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    insured_name: str = Field(min_length=1)
