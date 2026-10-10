"""Framework-free harness configuration shared by admission, views and exits."""
from __future__ import annotations
from typing import Literal
from copy import deepcopy
import logging
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from app.core.budget import TurnDeadline, current_deadline
from app.core.model_circuit import CircuitPolicy
from app.core.context_overflow import ContextRecoveryConf
from app.core.knowledge_excerpt import ExcerptPolicy
from app.core.tool_policy import OutputShapingPolicy, SelectionRule


class ProfileLimits(BaseModel):
    """分档 RPC 硬顶与观测用时延目标（不是执行超时）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_rpc_calls: int = Field(gt=0)
    latency_target_s: int = Field(gt=0)


class AgentProfileLimits(ProfileLimits):
    """Agent 档附加维度(llm/工具/token;§0.3 NFR-2)。"""

    max_llm_calls: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_same_tool_calls: int = Field(gt=0)
    # harness-v11：RAG 类工具（product_knowledge/benefit_info/general_qa）轮级合计上限。
    max_rag_tool_calls: int = Field(gt=0)
    max_llm_tokens: int = Field(gt=0)


class HistoryWindow(BaseModel):
    """近期对话窗口：模型历史与业务查询上下文各自采用有界投影。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_runs: int = Field(gt=0)
    max_chars: int = Field(gt=0)
    max_answer_chars: int = Field(default=600, gt=0)
    # 查询条件是业务输入，不受模型历史摘要的压缩上限影响。
    policy_context_max_chars: int = Field(default=30000, gt=0)
    customer_id_pattern: str = r'(?<![A-Za-z0-9_-])(?:C[_-]?\d{6,62}|(?:KH|LIFE)[_-]?\d[A-Z0-9_-]{2,61})(?![A-Za-z0-9_-])'

    @field_validator('customer_id_pattern')
    @classmethod
    def validate_customer_id_pattern(cls, value: str) -> str:
        import re
        try:
            pattern = re.compile(value)
        except re.error as exc:
            raise ValueError('客户号匹配正则非法') from exc
        if pattern.search(''):
            raise ValueError('客户号匹配不得匹配空串')
        return value


class ModelTimeoutLimits(BaseModel):
    """当前两档模型的单调用超时目标；真实工具超时属于 SDK handler。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: int = Field(gt=0)
    lite: int = Field(gt=0)


class AnswerLadderConf(BaseModel):
    """答案保底阶梯开关；通用摘录资格由内容扫描及来源校验决定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=False, strict=True)
    @model_validator(mode="before")
    @classmethod
    def _retired_categories(cls, value):
        if isinstance(value, dict) and "l2_categories" in value:
            from pydantic import TypeAdapter
            TypeAdapter(list[str]).validate_python(value["l2_categories"], strict=True)
            return {k: v for k, v in value.items() if k != "l2_categories"}
        return value


class CapacityAnswerConf(BaseModel):
    """容量出口独立接管开关（H-07b；off = 完全现状：SSE 仅 error+done / JSON 503）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=False, strict=True)
    # enabled=true 时必须显式选择：false=200；true=503 过渡状态码 + 降级信封 body。
    http_503: bool = Field(default=False, strict=True)
    @model_validator(mode="before")
    @classmethod
    def _retired_lite_organize(cls, value):
        if isinstance(value, dict) and "lite_organize" in value:
            if type(value["lite_organize"]) is not bool:
                raise ValueError("历史lite_organize必须为boolean")
            return {k: v for k, v in value.items() if k != "lite_organize"}
        return value

    @model_validator(mode="before")
    @classmethod
    def _explicit_capacity_status(cls, value):
        if isinstance(value, dict) and value.get("enabled") is True and "http_503" not in value:
            raise PydanticCustomError("capacity_http_status_required",
                                      "capacity_answer.enabled=true 必须显式声明 http_503=true/false")
        return value


class ToolOutputShapingConf(BaseModel):
    """CI-07 fixed-on compatibility input; historical off/shadow no longer select a path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["on"] = "on"

    @model_validator(mode="before")
    @classmethod
    def _retired_modes(cls, value):
        if isinstance(value, dict) and value.get("mode") in ("off", "shadow"):
            return {**value, "mode": "on"}
        return value


class HarnessFeaturesConf(BaseModel):
    """交付开关登记；模型视图、预算及无进展守卫固定开启，其余按配置回滚。

    显式关闭父开关时级联关闭依赖项；遗漏依赖、类型错误等仍拒绝。
    加载与热更新使用同一归一化/校验路径。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_ladder: AnswerLadderConf = Field(default_factory=AnswerLadderConf)
    static_system_prompt: bool = Field(default=False, strict=True)
    capacity_answer: CapacityAnswerConf = Field(default_factory=CapacityAnswerConf)
    tool_output_shaping: ToolOutputShapingConf = Field(default_factory=ToolOutputShapingConf)
    citation_l2: bool = Field(default=False, strict=True)
    supersedes_preview: bool = Field(default=False, strict=True)
    # Empty = observe only; enable individual rules after real misuse review.
    selection_redirects: tuple[SelectionRule, ...] = ()

    @field_validator('selection_redirects', mode='before')
    @classmethod
    def _selection_rules(cls, value):
        # YAML sequences become immutable tuples even under strict config loading.
        if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
            raise ValueError('selection_redirects必须为规则名列表')
        return tuple(sorted(set(value)))

    @model_validator(mode="before")
    @classmethod
    def _retired_admission_input(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            for key in ('tool_admission_split', 'budget_controller', 'no_progress_guard', 'keep_failed_turn_state', 'timeout_snapshot', 'tool_result_offload', 'citation_coverage', 'knowledge_tool_cache'):
                if key in value and type(value[key]) is not bool:
                    raise ValueError(f"历史{key}必须为boolean")
                value.pop(key, None)
        return value
    @model_validator(mode="wrap")
    @classmethod
    def _cascade_disabled_parents(cls, value, handler):
        # Explicit false is a rollback instruction; omitted parents still fail dependency checks.
        if not isinstance(value, dict):
            return handler(value)
        data = deepcopy(value)
        for key in ("answer_ladder", "capacity_answer", "tool_output_shaping"):
            if isinstance(data.get(key), BaseModel):
                data[key] = data[key].model_dump(exclude_unset=True)
        changes = []

        def disable(key):
            if data.get(key) is True:
                data[key] = False
                changes.append(key)

        ladder = data.get("answer_ladder")
        if isinstance(ladder, dict) and ladder.get("enabled") is False:
            for key in ("citation_l2",):
                disable(key)
        result = handler(data)
        if changes:
            logging.getLogger(__name__).warning("harness_features_cascaded", extra={"observability": {
                "disabled_features": changes, "outcome": "normalized",
            }})
        return result

    @model_validator(mode="after")
    def _feature_dependencies(self) -> HarnessFeaturesConf:
        if self.citation_l2 and not self.answer_ladder.enabled:
            raise PydanticCustomError("answer_ladder_required",
                "citation_l2 置 on "
                "依赖 answer_ladder.enabled=true（启用了出口改造就必须已有可用的降级实现）"
            )
        return self


class BudgetControllerConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reserve_calls: int = Field(default=2, ge=1, strict=True)
    soft_token_ratio: float = Field(default=0.85, gt=0, lt=1, allow_inf_nan=False)
    finalize_reserve_s: float = Field(default=25.0, gt=0, allow_inf_nan=False)


class HarnessConf(BaseModel):
    """§12.3 三层时限 + 分档硬顶的 conf 契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _retired_observer(cls, value):
        if isinstance(value, dict):
            # CL-02: retired policy mappings are accepted but never enter a snapshot.
            # Their nested values have no consumers; non-mappings still indicate bad config.
            for key in ("failure_recovery", "knowledge_cache", "failed_evidence"):
                if key in value and not isinstance(value[key], dict):
                    raise ValueError(f"历史{key}必须为mapping，已退役且不再生效")
            if "answer_formatting_enabled" in value and type(value["answer_formatting_enabled"]) is not bool:
                raise ValueError("历史answer_formatting_enabled必须为boolean")
            return {k: v for k, v in value.items() if k not in {
                "tool_output_observe_chars", "tool_finalize_reserve_s", "answer_formatting_enabled",
                "failure_recovery", "knowledge_cache", "failed_evidence"}}
        return value


    version: str
    excerpt_policy: ExcerptPolicy = Field(default_factory=ExcerptPolicy)
    answer_reserve_s: float = Field(default=0, ge=0, allow_inf_nan=False)
    # 剩余交付开关默认关闭；模型视图与预算控制为唯一固定路径。
    features: HarnessFeaturesConf = Field(default_factory=HarnessFeaturesConf)
    budget_controller: BudgetControllerConf = Field(default_factory=BudgetControllerConf)
    context_recovery: ContextRecoveryConf = Field(default_factory=ContextRecoveryConf)
    output_shaping: OutputShapingPolicy = Field(default_factory=OutputShapingPolicy)
    after_hooks_budget_s: float = Field(default=2.0, ge=0, allow_inf_nan=False, strict=True)
    no_progress_limit: int = Field(default=3, ge=1, le=20, strict=True)
    tracing_enabled: bool = False
    # 仅本地评测开启模型输入输出采集，默认仍隐藏。
    tracing_capture_io: bool = False
    timeouts_enabled: bool
    tool_call_limits_enabled: bool
    sse_event_queue_maxsize: int = Field(ge=2)
    sse_heartbeat_s: float = Field(gt=0)
    # harness-v7：开放澄清（无候选、无恢复锚点）连续轮上限，超限 ask_clarify
    # 拒绝登记并转人工收尾（§10 澄清纪律）；枚举澄清不受此限。
    max_consecutive_open_clarify_rounds: int = Field(ge=1)
    # harness-v15：删去 direct_llm_light（形态⑤ 直达清单 LLM 加工 2026-09-20 整体
    # 下线；直达清单改由 ExecutionResult.structured 直出结构化结果，见 runtime/direct.py）。
    turn_hard_ceiling_s: int = Field(gt=0)
    agentos_turn_hard_ceiling_s: int = Field(gt=0)
    history_window: HistoryWindow
    # union 判别:agent 档的附加维度(llm/tool/token)由 smart union 按字段集匹配到
    # AgentProfileLimits——extra=forbid 保证 direct/workflow 档混入 Agent 字段即校验失败
    profiles: dict[str, ProfileLimits | AgentProfileLimits]
    admission_ms: dict[str, int]
    customer_confirmation_timeout_s: float = Field(default=3.0, gt=0, le=75, allow_inf_nan=False)
    model_timeout_s: ModelTimeoutLimits
    model_circuit: CircuitPolicy = Field(default_factory=CircuitPolicy)

    @model_validator(mode="after")
    def _required_runtime_limits(self) -> HarnessConf:
        if self.answer_reserve_s >= min(self.turn_hard_ceiling_s, self.agentos_turn_hard_ceiling_s):
            raise ValueError("answer_reserve_s 必须小于两个入口的整轮硬截止")
        if not {"direct", "workflow", "agent"}.issubset(self.profiles):
            raise ValueError("profiles 缺少必需运行档位")
        self.agent_profile()
        if self.budget_controller.reserve_calls >= self.agent_profile().max_llm_calls:
            raise ValueError("reserve_calls 必须小于 max_llm_calls")
        if not {"tool_call", "primary_llm", "lite_llm"}.issubset(self.admission_ms) or any(value <= 0 for value in self.admission_ms.values()):
            raise ValueError("admission_ms 缺少工具准入值或包含非正值")
        if (self.budget_controller.finalize_reserve_s + self.answer_reserve_s + max(self.admission_ms.values()) / 1000
                >= min(self.turn_hard_ceiling_s, self.agentos_turn_hard_ceiling_s)):
            raise ValueError("收尾预留 + 交付预留 + 准入阈值必须小于两个入口的整轮硬截止")
        return self

    def deadline(self) -> TurnDeadline | None:
        from app.conf import models_conf
        from app.conf.runtime import AppConfigError, read_once
        from app.core.model_budget import current_model_budget
        from app.core.model_latency import p95
        reserve = self.budget_controller.finalize_reserve_s
        if current_deadline(reserve, self.answer_reserve_s) is None:
            return None
        try:
            models = read_once('models', models_conf)
            for tier in ('primary', 'lite'):
                measured = p95((tier, models.base_url, models.models[tier].id))
                if measured:
                    reserve = max(reserve, measured + self.admission_ms[f'{tier}_llm'] / 1000)
        except AppConfigError:
            # 纯工具直达不依赖模型配置可用；仍保留配置收尾窗口。
            pass
        budget = current_model_budget()
        if budget is not None:
            budget.finalize_reserve_s = max(budget.finalize_reserve_s, reserve)
            reserve = budget.finalize_reserve_s
        return current_deadline(reserve, self.answer_reserve_s)

    def agent_profile(self) -> AgentProfileLimits:
        """Agent 档 limits(字段超集;conf 缺维度即校验失败——fail 显式)。"""
        profile = self.profiles["agent"]
        if isinstance(profile, AgentProfileLimits):
            return profile
        return AgentProfileLimits.model_validate(profile.model_dump())
