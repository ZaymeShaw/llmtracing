"""能力包策略配置的单一加载与交叉校验入口（R6-a）。

领域能力以 ``capabilities/VERSION``、manifest、routes 与 eval 为权威；manifest
独占工具白名单，避免与提示词资产维护第二份注册事实。
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.route import RouteAction
from app.conf.runtime import AppConfigError, layer_payload

CAPABILITIES_DIR = Path(str(resources.files("capabilities")))
# capability 仅从公共 manifest 发现；环境层和 routes 必须在联合快照中存在并通过交叉校验。
CAPABILITY_NAMES = tuple(sorted(
    path.parent.name for path in CAPABILITIES_DIR.glob("*/manifest_common_args.yaml")
))


class CapabilityManifest(BaseModel):
    """``manifest_{ENV_TYPE}_args.yaml`` 的注册元数据契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str
    name: str
    description_for_model: str
    execution_modes: list[Literal["direct", "workflow", "agent"]] = Field(min_length=1)
    risk: str
    tools: list[str] = Field(min_length=1)
    routes: str
    eval: str
    gray_switch: str = Field(pattern=r"^(off|on|shadow)$")

    @model_validator(mode="after")
    def validate_execution_modes(self) -> "CapabilityManifest":
        if len(self.execution_modes) != len(set(self.execution_modes)):
            raise ValueError("execution_modes 不得重复登记")
        return self


class CapabilityRoutes(BaseModel):
    """G-4b 词面路由词典。

    ``positive``/``negative`` are deliberately data-only.  A route can be an
    exact phrase, a substring keyword, or a regular expression; the gate owns
    the matcher and this model only validates the registered shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    anchors: dict[str, list["CapabilityAnchor"]]

    @model_validator(mode="after")
    def validate_anchor_groups(self) -> "CapabilityRoutes":
        expected = {"positive", "negative"}
        if set(self.anchors) != expected:
            raise ValueError(f"anchors 必须且只能包含 {sorted(expected)}")
        return self


class CapabilityAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(pattern=r"^(exact|keyword|regex)$")
    value: str = Field(min_length=1)
    action: RouteAction
    tool: str | None = None
    # Route-specific risk/prerequisite overrides are intentionally explicit;
    # the G-5 gate still applies the manifest risk and session-health checks.
    requires_entity: bool = False

    @field_validator("action", mode="before")
    @classmethod
    def parse_action(cls, value: object) -> RouteAction:
        if isinstance(value, RouteAction):
            return value
        if not isinstance(value, str):
            raise ValueError("能力动作必须是已登记字符串")
        return RouteAction(value)


class CapabilityPolicies(BaseModel):
    """同一时刻生效的领域能力策略快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    manifests: dict[str, CapabilityManifest]
    routes: dict[str, CapabilityRoutes]

    @property
    def routes_version(self) -> str:
        """稳定、可读的逐能力 routes 版本向量压平值。"""
        return ";".join(f"{name}={self.routes[name].version}" for name in sorted(self.routes))


def capabilities_conf(*, allowed_risk_levels: set[str]) -> CapabilityPolicies:
    """返回与 Gate/字段一起验证并冻结的能力规则；保留显式调用方风险约束。"""
    from app.conf.rules import rules_conf

    policies = rules_conf().capabilities
    if any(manifest.risk not in allowed_risk_levels for manifest in policies.manifests.values()):
        raise AppConfigError("risk")
    return policies


def capability_paths(root: Path, env_type: str) -> tuple[Path, ...]:
    return (root / "VERSION", *(
        root / name / filename
        for name in CAPABILITY_NAMES
        for filename in (f"manifest_{env_type}_args.yaml", f"routes_{env_type}_args.yaml")
    ))


def parse_capabilities(
    content: dict[Path, bytes], *, root: Path, env_type: str, allowed_risk_levels: set[str],
) -> CapabilityPolicies:
    """只解析候选缓冲区；任何交叉校验失败都不得单独发布 manifest 或 routes。"""
    # 运行时延迟读取实现清单，避免配置模块导入阶段反向依赖工具绑定。
    from app.tools.bindings import AGENT_TOOLS

    registered_tools = {tool.name for tool in AGENT_TOOLS} | {
        "run_policy_snapshot",
        "run_policy_field_lookup",
    }
    manifests: dict[str, CapabilityManifest] = {}
    routes_by_capability: dict[str, CapabilityRoutes] = {}
    try:
        version = content[root / "VERSION"].decode("utf-8").strip()
    except (KeyError, UnicodeError):
        raise AppConfigError("caps_version") from None
    if re.fullmatch(r"caps-v[1-9][0-9]*", version) is None:
        raise AppConfigError("caps_version")
    for name in CAPABILITY_NAMES:
        directory = root / name
        manifest = CapabilityManifest.model_validate(
            layer_payload(content, directory / f"manifest_{env_type}_args.yaml"), strict=True,
        )
        if manifest.capability != name or manifest.risk not in allowed_risk_levels:
            raise AppConfigError("risk")
        if len(set(manifest.tools)) != len(manifest.tools) or not set(manifest.tools) <= registered_tools:
            raise AppConfigError("tools")
        if manifest.routes != f"routes_{env_type}_args.yaml":
            raise AppConfigError("routes_source")
        route_conf = CapabilityRoutes.model_validate(
            layer_payload(content, directory / manifest.routes), strict=True,
        )
        for anchor_group in ("positive", "negative"):
            for anchor in route_conf.anchors[anchor_group]:
                if anchor.type == "regex":
                    try:
                        re.compile(anchor.value)
                    except (re.error, OverflowError):
                        raise AppConfigError("regex") from None
                if anchor_group == "negative" and anchor.action is not RouteAction.AGENT:
                    raise AppConfigError("routes")
                if anchor.action is RouteAction.DIRECT_RAG and (
                    not anchor.tool or "direct" not in manifest.execution_modes
                ):
                    raise AppConfigError("execution_mode")
                if anchor.action is RouteAction.WORKFLOW_START and "workflow" not in manifest.execution_modes:
                    raise AppConfigError("execution_mode")
                if anchor.tool and anchor.tool not in manifest.tools:
                    raise AppConfigError("tools")
        manifests[name] = manifest
        routes_by_capability[name] = route_conf
    if not manifests:
        raise AppConfigError("capabilities")
    return CapabilityPolicies(version=version, manifests=manifests, routes=routes_by_capability)
