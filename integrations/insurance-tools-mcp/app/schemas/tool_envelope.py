"""ToolEnvelope:统一工具信封(方案 v13 §10.3 · M0 冻结)。

一切模型可见工具与 Workflow @tool 薄包装的唯一返回契约;
返回一律 str(JSON 信封)——规避 agno#6676 缓存序列化缺陷(§10.2)。
字段表以 §10.3 为准;hint 为 §10.2/§5.4 示例使用、字段表未列的开发期补齐项(冻结评审确认)。
"""
from __future__ import annotations

from typing_extensions import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import EnvelopeStatus, ErrorCode
from app.schemas.evidence import Evidence
from app.schemas.fact import Fact
from app.schemas.json_values import StrictJsonValue
from app.schemas.tool_control import ToolControl, ToolControlReason


class SourceRef(BaseModel):
    """后端来源引用(本地工具为空)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str = Field(description="后端逻辑服务名称，如 policy_svc，不是实际网络地址。")
    endpoint: str = Field(description="后端逻辑路由，如 /policy/full，不保证等于真实 HTTP 路径。")


class ToolError(BaseModel):
    """归一化错误(错误码映射表在 conf/tool/tool_config_{ENV_TYPE}_args.yaml 维护,§10.4)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode = Field(description="归一化错误码：TIMEOUT 超时、UPSTREAM 上游错误、DENIED 无权限、BUDGET 预算耗尽、OVERLOADED 本地容量不足。")
    retryable: bool = Field(description="是否允许本轮重试；DENIED、OVERLOADED 必须为 false，不代表已经执行重试。")

    @model_validator(mode="after")
    def validate_capacity_retry(self) -> Self:
        if self.code is ErrorCode.OVERLOADED and self.retryable:
            raise ValueError("OVERLOADED 不允许本轮自动重试")
        return self


class ToolEnvelope(BaseModel):
    """统一工具信封(§10.3):status 语义见 app/schemas/enums.py。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool = Field(description="是否可使用返回结果；仅 SUCCESS 和 PARTIAL 为 true，其余状态为 false。")
    status: EnvelopeStatus = Field(
        description="工具结果状态：SUCCESS 可答、PARTIAL 部分缺失、EMPTY 无结果、AMBIGUOUS 实体歧义或带 control 的并行/选择拒绝、NEEDS_CLARIFICATION 待澄清、DENIED 无权限、ERROR 系统错误或预算拒绝、TIMEOUT 超时。",
    )
    facts: list[Fact] = Field(default=[], description="工具产生的类型化事实列表，供答案引用；终止失败态不得携带业务事实。")
    evidence: list[Evidence] = Field(default=[], description="支持事实的来源证据列表，供引用溯源；终止失败态不得携带业务证据。")
    gaps: list[str] = Field(default=[], description="数据缺失、冲突或截断说明，如产品缺少免赔维度；PARTIAL 必须披露非空缺口，SUCCESS 不得携带。")
    hint: str | None = Field(default=None, description="面向用户的可读提示，如保单未锁定；None 表示无附加提示。")
    source: SourceRef | None = Field(default=None, description="后端来源引用；本地工具或未指定来源时为 None。")
    request_id: str | None = Field(
        default=None,
        description="工具调用标识，与事实 source_call_id 关联；后端使用链路标识及本轮逻辑调用序号生成，本地工具可为空。",
    )
    latency_ms: int | None = Field(default=None, description="后端调用耗时，单位为毫秒；未提供计量时为 None。")
    error: ToolError | None = Field(default=None, description="DENIED、ERROR 或 TIMEOUT 的归一化错误；其余状态必须为 None。")
    data: dict[str, StrictJsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="完整上游业务数据，保留原键、类型、空值及嵌套结构；不含外层协议码。未使用时省略，失败态禁止携带。",
    )
    field_descriptions: dict[str, str] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="模型可见的中文字段路径说明；[] 代表数组元素，未知含义明确标注。未使用时省略。",
    )

    control: ToolControl | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="本地工具准入或停止控制；未使用时省略，不是业务事实或上游错误说明。",
    )

    @model_validator(mode="after")
    def validate_status_semantics(self) -> Self:
        """让 §10.3 八态在契约层成为不可矛盾的消费判据。"""
        if self.control is not None:
            recoverable = self.control.reason in {
                ToolControlReason.PARALLEL_STATE_CONFLICT, ToolControlReason.SELECTION_MISUSE,
            }
            if recoverable:
                if self.status is not EnvelopeStatus.AMBIGUOUS or self.error is not None:
                    raise ValueError("并行状态冲突或选择误用必须为 AMBIGUOUS 且无 error")
            elif (self.status is not EnvelopeStatus.ERROR or self.error is None
                  or self.error.code is not ErrorCode.BUDGET or self.error.retryable):
                raise ValueError("本地控制拒绝必须为不可重试的 ERROR/BUDGET")
            if self.facts or self.evidence or self.data is not None or self.field_descriptions is not None:
                raise ValueError("本地控制信封不得携带业务数据")
        successful = self.status in (EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL)
        if self.ok is not successful:
            raise ValueError(f"status={self.status.value} 时 ok 必须为 {successful}")

        if self.status is EnvelopeStatus.PARTIAL and not any(gap.strip() for gap in self.gaps):
            raise ValueError("status=PARTIAL 必须披露非空 gaps")
        if self.status is EnvelopeStatus.SUCCESS and self.gaps:
            raise ValueError("status=SUCCESS 不得携带 gaps；有缺口应使用 PARTIAL")

        expected_error_codes = {
            EnvelopeStatus.DENIED: {ErrorCode.DENIED},
            EnvelopeStatus.ERROR: {ErrorCode.UPSTREAM, ErrorCode.BUDGET, ErrorCode.OVERLOADED},
            EnvelopeStatus.TIMEOUT: {ErrorCode.TIMEOUT},
        }
        allowed_codes = expected_error_codes.get(self.status)
        if allowed_codes is None:
            if self.error is not None:
                raise ValueError(f"status={self.status.value} 不得携带 error")
            return self

        # 终止失败态不得夹带任何业务数据。除了避免脏数据被误渲染，这也是
        # DENIED 的防探测边界：失败响应只能说明状态，不能顺带泄漏事实/证据。
        if self.facts or self.evidence or self.data is not None or self.field_descriptions is not None:
            raise ValueError(
                f"status={self.status.value} 不得携带 facts/evidence/data/field_descriptions"
            )

        if self.error is None:
            raise ValueError(f"status={self.status.value} 必须携带归一化 error")
        if self.error.code not in allowed_codes:
            allowed = ",".join(sorted(code.value for code in allowed_codes))
            raise ValueError(f"status={self.status.value} 的 error.code 必须为 {allowed}")
        if self.status is EnvelopeStatus.DENIED and self.error.retryable:
            raise ValueError("status=DENIED 的 error.retryable 必须为 false")
        return self
