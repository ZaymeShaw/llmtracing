"""版本化配置(P8:机制在文档、数字在 conf)。

模型配置读取公共文件与当前环境覆盖；其余配置仍按环境读取完整文件，按轮快照提供。
凭据约定见 models/models_dev_args.yaml 头注(开发期明文,Owner 2026-08-25 决定)。
旧出口检查与词表已退役；策略版本保留 disabled 兼容标记。
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.conf.runtime import current_source, load_model
from app.conf.database import database_conf as database_conf

CONF_DIR = Path(__file__).resolve().parent
TOOL_CONFIG_DIR = CONF_DIR / "tool"

if TYPE_CHECKING:  # 仅类型注解用(运行期惰性导入,保持纯加载器语义)
    from app.core.audit import AuditConf
    from app.core.entity_dict import EntityDict
    from app.core.field_dict import FieldDict
    from app.orchestration.gate import GateConf
    from app.core.harness_config import HarnessConf
    from app.tools.mode import ToolsConf


class TokenClientConf(BaseModel):
    """服务端仅保存随机服务凭据摘要，不保存上游持有的原文。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    client_id: str = Field(min_length=1, max_length=128)
    api_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


class TokenIssuerConf(BaseModel):
    """签发配置随应用启动冻结，凭据撤销和轮换需重启。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    access_token_ttl_seconds: int = Field(default=3600, ge=1, le=86400, strict=True)
    signing_private_key_file: str | None = Field(default=None, repr=False)
    clients: list[TokenClientConf] = Field(default_factory=list, repr=False)

    @model_validator(mode="after")
    def validate_issuer(self) -> "TokenIssuerConf":
        if self.enabled and (not self.clients or not self.signing_private_key_file):
            raise ValueError("签发启用时必须配置客户端和私钥文件")
        if len({client.client_id for client in self.clients}) != len(self.clients):
            raise ValueError("签发客户端标识重复")
        if len({client.api_key_sha256 for client in self.clients}) != len(self.clients):
            raise ValueError("签发客户端凭据重复")
        return self


class JwtConf(BaseModel):
    """/v1chat 与 AgentOS 的 JWT 验签配置(方案 §17;Agno AuthorizationConfig 原生承载)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    enabled: bool = False
    algorithm: str = "RS256"
    verification_keys: list[str] = []
    jwks_file: str | None = None
    verify_audience: bool = False
    audience: str | None = None
    admin_scope: str | None = None
    # true 时 AgentOS 按 JWT sub 做用户级数据隔离;内网评估后再开
    user_isolation: bool = False
    token_issuer: TokenIssuerConf = Field(default_factory=TokenIssuerConf)


class ChatProgressConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=True, strict=True)
    stage_messages_enabled: bool = Field(default=True, strict=True)
    tool_messages_enabled: bool = Field(default=True, strict=True)


class FirstVisibleConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_s: float = Field(default=8.0, gt=0, allow_inf_nan=False)
    send_reserve_s: float = Field(default=0.5, gt=0, allow_inf_nan=False)
    fallback_status_enabled: bool = Field(default=True, strict=True)
    on_timeout: Literal["terminate"] | None = None

    @model_validator(mode="after")
    def validate_deadline(self) -> "FirstVisibleConf":
        if self.send_reserve_s >= self.timeout_s:
            raise ValueError("首帧发送预留必须小于首帧期限")
        if not self.fallback_status_enabled and self.on_timeout != "terminate":
            raise ValueError("关闭首帧保底必须明确选择 terminate")
        return self


class FirstVisibleObservabilityConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics_enabled: bool = Field(default=True, strict=True)
    breach_log_enabled: bool = Field(default=True, strict=True)


class ChatConf(BaseModel):
    """Conversation API 启动配置：身份来源与同会话执行权等待期限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    pre_stream_policy: Literal["early_sse", "http_error"] = "early_sse"
    progress: ChatProgressConf = Field(default_factory=ChatProgressConf)
    first_visible: FirstVisibleConf = Field(default_factory=FirstVisibleConf)
    first_visible_observability: FirstVisibleObservabilityConf = Field(default_factory=FirstVisibleObservabilityConf)
    # 未启用 JWT 时是否接受请求体 user_id 作为权限主体(内网可信网关模式)。
    trusted_body_user_id: bool = False
    # 独立于 Workflow 执行硬顶；排队超时在建 Workflow / SSE 开流之前返回 409。
    session_turn_wait_timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    max_active_requests: int = Field(default=24, ge=1)
    max_active_per_user: int = Field(default=4, ge=1)
    max_model_requests: int = Field(default=16, ge=1)
    # null 按本轮剩余可启动预算等待；数值可进一步收紧，0 立即准入。
    model_slot_wait_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    graceful_shutdown_s: float = Field(default=32.0, gt=0, allow_inf_nan=False)
    shutdown_readiness_delay_s: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    shutdown_answer_timeout_s: float = Field(default=20.0, gt=0, allow_inf_nan=False)
    shutdown_finalize_reserve_s: float = Field(default=8.0, gt=0, allow_inf_nan=False)
    shutdown_delivery_timeout_s: float = Field(default=25.0, gt=0, allow_inf_nan=False)
    request_cleanup_reserve_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    setup_timeout_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    state_operation_timeout_s: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    cleanup_timeout_s: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    send_timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def _shutdown_windows(self) -> ChatConf:
        if self.shutdown_answer_timeout_s <= self.shutdown_finalize_reserve_s:
            raise ValueError('停机执行窗口必须大于停机收尾预留')
        if self.shutdown_answer_timeout_s >= self.shutdown_delivery_timeout_s:
            raise ValueError('停机执行窗口必须小于交付窗口')
        if self.shutdown_delivery_timeout_s + self.send_timeout_s + 2.0 > self.graceful_shutdown_s:
            raise ValueError('停机交付与发送预留须在服务器宽限内保留至少 2 秒余量')
        return self

    @model_validator(mode='before')
    @classmethod
    def _retired_request_timeout(cls, value):
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k != 'request_timeout_s'}
        return value

    @property
    def request_timeout_s(self) -> float:
        """请求硬顶随本轮 harness 配置推导，预留启动、发送及清理窗口。"""
        from app.conf.runtime import read_once
        return read_once('harness', harness_conf).turn_hard_ceiling_s + self.request_cleanup_reserve_s



class ModelEntry(BaseModel):
    """凭据明文在 conf 属 Owner 决定(§九);装载纪律与其余 conf 同 strict。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    max_tokens: int = Field(gt=0)  # 单次响应上限(含思维链;§13.1/切片 5-a 实测口径)
    # 不支持格式约束的网关显式关闭；省略字段仍保持原请求行为。
    send_response_format: bool = Field(default=True, strict=True)
    # None 表示沿用网关默认值；百炼混合思考模型可按档显式开启/关闭。
    enable_thinking: bool | None = Field(default=None, strict=True)

    # H-05：仅 Agno 模型调用层重试；旧环境缺字段仍为 0，共用配置显式开启主力档。
    retries: int = Field(default=0, ge=0, le=1, strict=True)
    delay_between_retries: int = Field(default=1, ge=0, strict=True)
    exponential_backoff: bool = Field(default=True, strict=True)

    @field_validator("id")
    @classmethod
    def _nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("模型 id 不得为空白")
        return value


class ModelsConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    base_url: str
    api_key: str = Field(repr=False)
    models: dict[str, ModelEntry]
    fallback_chain: list[str]

    @field_validator("provider", "base_url", "api_key")
    @classmethod
    def _required_connection(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("模型连接参数必须由当前环境配置提供")
        return value

    @field_validator("base_url")
    @classmethod
    def _http_endpoint(cls, value: str) -> str:
        AnyHttpUrl(value)
        return value

    @property
    def primary(self) -> ModelEntry:
        return self.models["primary"]

    @property
    def lite(self) -> ModelEntry:
        return self.models["lite"]

    @model_validator(mode="after")
    def _chain_entries_exist(self) -> "ModelsConf":
        if not {"primary", "lite"}.issubset(self.models):
            raise ValueError("models 必须包含 primary 与 lite")
        if not self.fallback_chain:
            raise ValueError("fallback_chain 不得为空")
        missing = [name for name in self.fallback_chain if name not in self.models]
        if missing:
            raise ValueError(f"fallback_chain 引用了未定义的模型:{missing}")
        if any(name not in {"primary", "lite"} for name in self.fallback_chain):
            raise ValueError("fallback_chain 仅支持有超时配置的 primary/lite 档位")
        if len(set(self.fallback_chain)) != len(self.fallback_chain):
            raise ValueError("fallback_chain 不得重复登记模型")
        return self


def models_conf() -> ModelsConf:
    """公共模型参数 + 当前环境地址/凭据；坏更新保留上一整份有效配置，按轮固定。"""
    return load_model(
        "models", ModelsConf, reload=True, common=True,
        environment_only=("base_url", "api_key"),
    )


def fields_dict() -> FieldDict:
    """加载字段字典(core/field_dict 的输入;别名挖掘 = 存量 3168 条历史日志,§11)。"""
    from app.core.field_dict import FieldDict

    from app.conf.rules import rules_conf

    from app.conf.runtime import snapshot_derived

    def build():
        fields = rules_conf().fields
        return FieldDict(fields.fields, version=fields.version)
    return snapshot_derived("fields_dict", build)


@lru_cache(maxsize=1)
def entity_dict() -> "EntityDict":
    """加载实体归属词典(core/entity_dict 的输入;Owner 2026-09-13 设计对齐)。

    产物 conf/entity_dict/entity_dict.json 由 scripts/build_entity_dict.py 从 datas/ 两份
    全量数据生成(判据=版本化数据);双通道纪律见 core/entity_dict 模块注释。
    刻意偏离「不缓存」惯例:词典产物 ~1MB,json 解析毫秒级,entity_lookup 工具
    每次调用都读——不缓存会拖慢 Agent 工具路径;词典随数据版本重生成,不属于
    运行期热调对象,缓存不损失语义。
    """
    from app.core.entity_dict import EntityDict

    with (CONF_DIR / "entity_dict" / "entity_dict.json").open(encoding="utf-8") as f:
        payload = json.load(f)
    return EntityDict(payload, version=payload["version"])


def gate_conf() -> GateConf:
    """加载 Gate 判定配置(orchestration/gate 的输入;检查项登记与风险两级表,§4.2/§4.7)。"""
    from app.conf.rules import rules_conf

    return rules_conf().gate


def harness_conf() -> HarnessConf:
    """加载 Harness 时限/预算配置(runtime/model_router 的输入;§12.3 三层时限与分档硬顶)。"""
    from app.core.harness_config import HarnessConf

    return load_model("harness", HarnessConf, reload=True)


def tools_conf() -> ToolsConf:
    """返回本轮工具快照；独立调用读取当前环境并规范化为运行模型。"""
    from app.conf.tool_config_loader import load_tool_config
    from app.tools.config_scope import current_fallback_config, current_generation

    generation = current_generation()
    if generation is not None:
        return generation.loaded.config
    fallback = current_fallback_config()
    if fallback is not None:
        return fallback
    env_type, path = tool_config_location()
    return load_tool_config(path, env_type=env_type).config


def tool_config_location() -> tuple[str, Path]:
    """仅在应用装配或独立调用入口选择环境，不在运行中切换环境。"""
    from app.conf.tool_config_loader import resolve_tool_config_path

    env_type = current_source().env_type
    return env_type, resolve_tool_config_path(env_type, config_dir=TOOL_CONFIG_DIR)


def audit_conf() -> AuditConf:
    """加载审计落库配置(main 装配默认 sink 的输入;§12.1⓪ · D13/NFR-7)。"""
    from app.core.audit import AuditConf

    return load_model("audit", AuditConf)


def jwt_conf() -> JwtConf:
    """加载 JWT 验签配置(main 装配 AuthorizationConfig 的输入;§17)。"""
    return load_model("jwt", JwtConf)


def chat_conf() -> ChatConf:
    """加载 Conversation API 启动配置（身份来源与会话等待期限，重启生效）。"""
    return load_model("chat", ChatConf)


def current_policy_versions() -> dict[str, str]:
    """返回当轮唯一的审计策略版本向量，并在读取时完成跨配置校验。"""
    from app.conf.capabilities import capabilities_conf

    gate = gate_conf()
    capabilities = capabilities_conf(allowed_risk_levels=set(gate.risk_levels))
    return {
        "gate": gate.version,
        "harness": harness_conf().version,
        "lexicons": "disabled",  # CL-03 历史版本向量兼容，不加载已退役词表
        "fields": fields_dict().version,
        "entity_dict": entity_dict().version,
        "skills": (resources.files("skills") / "VERSION").read_text(encoding="utf-8").strip(),
        "caps": capabilities.version,
        "routes": capabilities.routes_version,
    }
