"""Fact:类型化断言(方案 v13 §10.4 · M0 冻结)。

答案引用与溯源的数据基础——AnswerEnvelope.fact_refs 由其 fact_id 生成;
facts 的权威产生方唯一 = 工具信封(D16:模型只产 fact_refs 引用,永不转写)。
语义纪律(§10.3):金额类必带 as_of。
"""
from __future__ import annotations

from datetime import date
from typing_extensions import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Fact(BaseModel):
    """类型化断言:主体 × 谓词 × 类型化值 × 溯源引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(description="事实引用标识，由工具生成，供答案 fact_refs 引用。")
    subject_type: str = Field(description="事实主体类型，如 policy、product、customer、benefit；开放字符串，可随能力扩展。")
    subject_id: str = Field(description="事实主体的标准标识，由确定性解析或工具结果提供，不由模型编造。")
    predicate: str = Field(description="归一化字段码，如 cash_value，表示事实断言的属性。")
    typed_value: float | int | str | bool = Field(description="断言的类型化值：数值、文本或布尔值；具体口径由 predicate 和 unit 指定。")
    unit: str | None = Field(default=None, description="数值单位，如 CNY、天或 %；不适用或未提供时为 None。")
    as_of: str | None = Field(default=None, description="数据业务时点，ISO 日期 YYYY-MM-DD；CNY 金额必须提供，不得用请求时间补造。")
    source_call_id: str = Field(description="产生该事实的工具调用标识，与 ToolEnvelope.request_id 及按轮分桶的证据池关联。")
    evidence_refs: list[str] = Field(default=[], description="该事实引用的 Evidence.evidence_id 列表；空列表表示未附加证据引用。")
    identity_scope: str = Field(description="事实归属的认证身份域，如 AGENT-001，用于越权校验及去重，不是客户主键。")

    @model_validator(mode="after")
    def validate_as_of(self) -> Self:
        """金额事实必须带可机器比较的 ISO 日期时点（§10.3）。"""
        if self.unit == "CNY" and self.as_of is None:
            raise ValueError("CNY 金额 Fact 必须提供 as_of")
        if self.as_of is not None:
            try:
                date.fromisoformat(self.as_of)
            except ValueError as exc:
                raise ValueError("Fact.as_of 必须是 ISO 日期（YYYY-MM-DD）") from exc
        return self
