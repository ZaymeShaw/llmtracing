"""工具配置校验与调用级冻结上下文。"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, Literal, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas import EnvelopeStatus, ErrorCode
from app.tools.auth_config import ArkAuthConfig, DUAL_ENDPOINT_TOOLS


class BackendConfigurationError(RuntimeError):
    """工具归属或执行上下文配置错误。"""


# S2.5 语义直达白名单 = Direct 执行器已支持的清单搜索分支(app/runtime/direct.py
# dispatch 同步扩展);置于本层供 endpoints 键装载校验与 probes 策略校验共用。
SUPPORTED_DIRECT_TOOLS = frozenset({"policy_search", "customer_search"})


class ToolDataConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    auth: ArkAuthConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    recall_top_n: int | None = Field(default=None, ge=1, le=20)
    result_limit: int | None = Field(default=None, ge=1, le=20)


class LocalToolConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["local"]


class ToolEntriesConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_detail: ToolDataConf
    product_knowledge: ToolDataConf
    benefit_info: ToolDataConf
    general_qa: ToolDataConf
    policy_search: ToolDataConf
    customer_search: ToolDataConf
    customer_detail: ToolDataConf
    ask_clarify: LocalToolConf
    entity_lookup: LocalToolConf

    @model_validator(mode="after")
    def validate_tool_specific_options(self) -> "ToolEntriesConf":
        for name, entry in self.data_tools().items():
            if entry.auth is not None:
                entry.auth.validate_roles(query_required=name in DUAL_ENDPOINT_TOOLS)
            if name != "general_qa" and entry.recall_top_n is not None:
                raise ValueError(f"recall_top_n 只允许配置在 general_qa:{name}")
            if name not in {"policy_search", "customer_search"} and entry.result_limit is not None:
                raise ValueError(f"result_limit 只允许配置在搜索工具:{name}")
        return self

    def get_data_tool(self, tool_name: str) -> ToolDataConf:
        value = getattr(self, tool_name, None)
        if not isinstance(value, ToolDataConf):
            raise BackendConfigurationError(f"未登记后端业务工具:{tool_name}")
        return value

    def data_tools(self) -> dict[str, ToolDataConf]:
        return {
            name: self.get_data_tool(name)
            for name in (
                "policy_detail",
                "product_knowledge",
                "benefit_info",
                "general_qa",
                "policy_search",
                "customer_search",
                "customer_detail",
            )
        }


class QaScoreConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success_min: float
    partial_min: float

    @model_validator(mode="after")
    def validate_thresholds(self) -> "QaScoreConf":
        if not 0 < self.partial_min < self.success_min < 1:
            raise ValueError("qa_score 须满足 0 < partial_min < success_min < 1")
        return self


class NormalizedErrorConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnvelopeStatus
    code: ErrorCode
    retryable: bool

    @model_validator(mode="after")
    def validate_semantics(self) -> "NormalizedErrorConf":
        if self.status is EnvelopeStatus.TIMEOUT and self.code is not ErrorCode.TIMEOUT:
            raise ValueError("TIMEOUT 状态必须配 TIMEOUT 错误码")
        if self.status is EnvelopeStatus.DENIED and self.retryable:
            raise ValueError("DENIED 不可重试")
        return self


class SemanticProbeEndpointConf(BaseModel):
    """S2.5 语义直达判定接口的环境连接(键 = probe.tool;url 空 = 该环境未接入)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auth: ArkAuthConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    url: str = ""
    timeout_s: float = Field(default=3.0, gt=0)

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, value: ArkAuthConfig | None) -> ArkAuthConfig | None:
        if value is not None and not isinstance(value.invokeInterface, str):
            raise ValueError("意图接口的 invokeInterface 只支持字符串")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        # 空串 = 未接入哨兵放行;非空复用 HandlerConfig 同款装载期校验(绝对
        # HTTP(S)、无内嵌凭据/空白/控制字符)——坏 URL 在配置装载即失败,
        # 不进入灰度期每请求静默 miss 的假象。
        from tool_adapter_sdk.config import HandlerConfig

        return value if not value.strip() else HandlerConfig.validate_url(value)


class SemanticDirectEnvConf(BaseModel):
    """S2.5 语义直达判定的环境运行面(单源分工,2026-09-16 Owner 指令):

    gate_{ENV_TYPE}_args.yaml semantic_direct 只承载策略与顺序(version/probes);启停与连接
    归三环境工具配置各一份——enabled 按 dev/stg/prd 独立灰度(dev 先开验证,
    stg/prd 跟进),URL 差异随环境文件天然隔离。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    endpoints: dict[str, SemanticProbeEndpointConf] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_endpoint_keys(self) -> "SemanticDirectEnvConf":
        # 键 = probe.tool 白名单:typo 键(如 policy_serch)装载期即失败,防
        # 「配置看似接入但 probe 永远跳过」的静默全 miss 灰度假象。
        unknown = sorted(set(self.endpoints) - SUPPORTED_DIRECT_TOOLS)
        if unknown:
            raise ValueError(f"semantic_direct.endpoints 含未支持直达工具:{unknown}")
        return self


class ToolsConf(BaseModel):
    """工具运行层规范化契约；版本统一取快照 schema 版（tools-v6），避免同一份
    配置在观测字段（config_version）里出现两个名字。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["tools-v6"]
    tools: ToolEntriesConf
    qa_score: QaScoreConf
    error_normalization: dict[str, NormalizedErrorConf]
    # 判定接口运行面(非工具 handler,不参与 RealToolAdapter 装配;缺省 = 该环境未接入)
    semantic_direct: SemanticDirectEnvConf = Field(default_factory=SemanticDirectEnvConf)

    @model_validator(mode="after")
    def validate_error_keys(self) -> "ToolsConf":
        required_errors = {"denied", "timeout", "upstream", "contract", "budget"}
        missing = sorted(required_errors - set(self.error_normalization))
        if missing:
            # 真实接口错误码到位后在当前环境 YAML 增补细分键即可,结构不变;
            # 校验只锁"五个已登记语义必须存在",不禁止扩展。
            raise ValueError(f"error_normalization 缺少必需键:{sorted(missing)}")
        return self


@dataclass(frozen=True)
class ResolvedToolConfig:
    recall_top_n: int | None
    result_limit: int | None


@dataclass(frozen=True)
class ToolExecutionContext:
    tool_name: str
    tool_config: ResolvedToolConfig
    config_version: str
    snapshot: ToolsConf | None = None
    env_type: str | None = None
    config_revision: str | None = None


_CURRENT_CONTEXT: ContextVar[ToolExecutionContext | None] = ContextVar(
    "tool_execution_context", default=None
)


def require_tool_execution_context() -> ToolExecutionContext:
    context = _CURRENT_CONTEXT.get()
    if context is None:
        raise BackendConfigurationError("call_backend 必须在顶层工具执行上下文中调用")
    return context


def _resolve_context(tool_name: str) -> ToolExecutionContext:
    from app.conf import tools_conf
    from app.tools.config_scope import current_generation

    conf = tools_conf()
    generation = current_generation()
    configured = conf.tools.get_data_tool(tool_name)
    resolved = ResolvedToolConfig(
        recall_top_n=configured.recall_top_n,
        result_limit=configured.result_limit,
    )
    return ToolExecutionContext(
        tool_name=tool_name,
        tool_config=resolved,
        config_version=generation.loaded.schema_version if generation is not None else conf.version,
        snapshot=conf,
        env_type=generation.loaded.env_type if generation is not None else None,
        config_revision=generation.loaded.revision if generation is not None else None,
    )


@contextmanager
def tool_execution_context(tool_name: str) -> Iterator[ToolExecutionContext]:
    """读取并冻结一次顶层工具调用所需的完整配置。"""
    current = _CURRENT_CONTEXT.get()
    if current is not None:
        if current.tool_name != tool_name:
            raise BackendConfigurationError(
                f"工具执行上下文不可从 {current.tool_name} 隐式切换到 {tool_name}"
            )
        yield current
        return
    context = _resolve_context(tool_name)
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_CONTEXT.reset(token)


def execution_tools_conf() -> ToolsConf:
    """评分和错误归一读取本次调用已冻结的完整配置。"""
    current = _CURRENT_CONTEXT.get()
    if current is not None and current.snapshot is not None:
        return current.snapshot
    from app.conf import tools_conf

    return tools_conf()


P = ParamSpec("P")
R = TypeVar("R")


def tool_execution_scope(tool_name: str) -> Callable[[Callable[P, Any]], Callable[P, Any]]:
    """保持原函数签名与元数据的顶层异步工具装饰器。"""
    def decorate(function: Callable[P, Any]) -> Callable[P, Any]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            from app.tools.config_scope import tool_runtime_scope

            client = kwargs.get("backend_client")
            manager = getattr(client, "config_manager", None)
            async with tool_runtime_scope(manager):
                with tool_execution_context(tool_name):
                    return await function(*args, **kwargs)

        return wrapped

    return decorate


__all__ = [
    "BackendConfigurationError",
    "NormalizedErrorConf",
    "QaScoreConf",
    "ResolvedToolConfig",
    "ToolDataConf",
    "ToolEntriesConf",
    "ToolExecutionContext",
    "ToolsConf",
    "require_tool_execution_context",
    "tool_execution_context",
    "tool_execution_scope",
]


# Internal request-local hint; never a model argument or a persisted authority.
visible_policy_revalidation: ContextVar[bool] = ContextVar('visible_policy_revalidation', default=False)
