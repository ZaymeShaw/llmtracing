"""AnswerEnvelope：S4 输出与接入层结构校验共用的应答信封。

正常信封的 checks 为 null；仅运行失败或信封异常时 status=DEGRADED。
旧检查戳记输入兼容忽略；checks 保留 null，不影响有效正文与结构化数据。
"""
from __future__ import annotations

from typing import Literal
from typing_extensions import Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.schemas.json_values import StrictJsonValue

# followup_actions 动作位(协议字面量,单点定义;消费方=接入层/前端。2026-08-26 检视统一:
# 此前 finalize 写「转人工」、降级信封写「handoff」,同语义两写法)
HANDOFF_ACTION = "handoff"  # 转人工话术引导(修订五裁撤 handoff 工具后,协议动作位保留)

# AgentOS run 输出上的承载键:Agno 把 run 起点构造的 run_context.metadata 同一 dict
# 挂到 WorkflowRunOutput.metadata,step 内原地写才能被 run 路由回读(见 turn_workflow
# 的 _present_agentos)。该通道只有自然语言正文一个出口位置,结构化结果挂在其侧。
STRUCTURED_METADATA_KEY = "structured_result"


class StructuredToolResult(BaseModel):
    """纯清单搜索工具的结构化结果(前端适配用;Owner 2026-09-20 决策)。

    与同轮 answer 同源、只读投影:Direct 命中或 Agent 终止型搜索均可产生；
    搜索参与字段/详情/复合问答时为 null。
    仅当工具信封携带业务页 data 时产生——终止失败态(DENIED/ERROR/TIMEOUT)契约上
    禁止携带 data/field_descriptions,因此天然为 null,不需要额外判据。
    data 是接口原样业务页(by_alias,含 pageNo/pageSize/total/records),不是候选摘要;
    field_descriptions 是同一份数据的字段中文说明,与 data 同源同取,不单独放行。
    本载荷不经过 format_answer 口吻净化——它是给前端渲染的原始数据,不是面向用户的正文。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(description="产生本轮纯清单结果的工具名称，当前为 customer_search 或 policy_search。")
    status: str = Field(description="原工具结果状态字面量；不是聊天信封 status，也不表示全量分页已取完。")
    data: dict[str, StrictJsonValue] = Field(description="同轮工具的原始业务页对象，动态字段及原值保真；不是候选摘要或 activity.output。")
    field_descriptions: dict[str, str] = Field(default={}, description="同一份原始业务页的中文路径释义；不是 activity.output 中可能折叠的模型字段说明。")


class AnswerEnvelope(BaseModel):
    """出口统一信封(§10.4 字段表 + POC-21 降级形态并集)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(description="最终权威答复正文，始终整段覆盖已有预览，不追加；保留模型原有 Markdown。")
    status: str | None = Field(default=None, description="降级时为 DEGRADED，其余通常为 null；null 不等于内容完整，须结合 completeness/limitations。")  # 降级信封 "DEGRADED"(POC-21);正常信封不填
    fact_refs: list[str] = Field(default=[], description="本轮引用的事实 ID 数组，用于证据溯源和排障，可为空，不宜作为来源文案展示。")
    sources: list[str] = Field(default=[], description="面向读者的来源标签数组，可为空，不是内部事实 ID。")  # 来源可读标签(finalize 从 Evidence 渲染的对外投影)
    gaps: list[str] = Field(default=[], description="本轮未能回答部分的缺口说明数组，无缺口时为空数组。")
    assumptions: list[str] = Field(default=[], description="本轮采纳的假设数组，无假设时为空数组。")
    trace_id: str | None = Field(default=None, description="原值回显请求 trace_id；省略或 null 时为 null，不是服务端内部追踪标识。")
    followup_actions: list[str] = Field(default=[], description="后续动作位数组，当前可用 handoff 表示转人工话术引导，无动作为空数组。")  # 如转人工(降级信封动作位;修订五后转人工为话术引导,无工具)
    handoff_status: Literal["none", "recommended", "requested", "confirmed"] = Field(
        default="none", description="转人工引导状态；当前产生 none/recommended，requested/confirmed 为前端回执链路预留。",
    )
    checks: None = Field(default=None, description="退役检查字段，当前输出恒为 null；不表示成功，旧 object 输入兼容归一为 null。")  # CL-03：保留响应字段，检查戳记不再是有效协议类型。
    # 清单卡片轮的工具结构化结果(前端适配出口)；非该轮次与降级信封均为 null。
    structured: StructuredToolResult | None = Field(default=None, description="同轮客户/保单纯清单搜索的原始结构化结果；其余轮和降级时为 null，前端可回落 answer 正文。")

    # None 表示旧生产者未提供；只省略新增空字段，不改变旧字段的序列化规则。
    completeness: Literal["complete", "partial", "fallback"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="可选完整度：complete 完整、partial 部分、fallback 兜底；未提供时省略，不能由 status=null 猜完整。",
    )
    limitations: list[str] | None = Field(default=None, exclude_if=lambda value: value is None,
                                         description="可选业务局限数组；未提供或 null 时省略，显式空数组保留；部分回答须同时展示局限。")

    supersedes_preview: StrictBool | None = Field(default=None, exclude_if=lambda value: value is None,
                                                 description="可选预览替换标记；true 强调撤销预览，未提供时省略，但最终 answer 仍始终覆盖预览。")

    @field_validator("checks", mode="before")
    @classmethod
    def ignore_legacy_checks(cls, value):
        # Historical serialized envelopes remain readable without reviving checks.
        # The retired mapping has no authority over answer/refs/structured data.
        return None if isinstance(value, dict) else value

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.completeness == "fallback" and self.status != "DEGRADED":
            raise ValueError("fallback 回答须为 DEGRADED")
        if self.completeness in ("complete", "partial") and self.status is not None:
            raise ValueError("complete/partial 回答的 status 须为空")
        return self

    @property
    def is_incomplete(self) -> bool:
        """消费端完整度判据；旧信封继续按 status 解释。"""
        return self.status is not None or self.completeness in ("partial", "fallback")
