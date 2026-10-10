"""分环境工具配置纯加载器：安全解析、完整 handler 校验与只读快照。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Hashable, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError, field_serializer

from app.tools.mode import BackendConfigurationError, ToolsConf
from app.tools.auth_config import ArkInvokeInterfaces, DUAL_ENDPOINT_TOOLS
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.handlers import REQUIRED_HANDLERS_BY_TOOL

MAX_CONFIG_BYTES = 1024 * 1024
_ENVIRONMENTS = frozenset({"dev", "stg", "prd"})
_HANDLER_NAMES = frozenset(name for names in REQUIRED_HANDLERS_BY_TOOL.values() for name in names)


class ToolConfigError(BackendConfigurationError):
    """仅携带安全类别与固定描述，避免泄露原始校验输入。"""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class LoadedToolConfig:
    env_type: str
    config: ToolsConf
    handlers: Mapping[str, HandlerConfig]
    revision: str
    schema_version: str = "tools-v6"


class _SnapshotToolsConf(ToolsConf):
    @field_serializer("error_normalization")
    def serialize_errors(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """保持只读映射与既有 model_dump JSON 接口兼容。"""
        return dict(value)


def resolve_env_type(value: str | None) -> str:
    """未设置或空白使用 dev，其他非法值不回退。"""
    normalized = value.strip() if isinstance(value, str) else ""
    if value is not None and not isinstance(value, str):
        raise ToolConfigError("environment", "ENV_TYPE 类型非法")
    if not normalized:
        return "dev"
    if normalized not in _ENVIRONMENTS:
        raise ToolConfigError("environment", "ENV_TYPE 必须为 dev、stg 或 prd")
    return normalized


def resolve_tool_config_path(env_type: str, config_dir: Path | None = None) -> Path:
    environment = resolve_env_type(env_type)
    return (config_dir or Path(__file__).resolve().parent / "tool") / f"tool_config_{environment}_args.yaml"


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False,
    ) -> dict[Hashable, Any]:
        result: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ToolConfigError("yaml", "YAML 键必须为字符串且不得重复")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _read_single_payload(path: Path) -> tuple[dict[str, Any], str]:
    try:
        with path.open("rb") as source:
            content = source.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ToolConfigError("read", "无法读取当前环境工具配置文件") from None
    if len(content) > MAX_CONFIG_BYTES:
        raise ToolConfigError("read", "工具配置文件超过大小限制")
    try:
        payload = yaml.load(content.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError, RecursionError):
        raise ToolConfigError("yaml", "工具配置 YAML 语法或编码非法") from None
    if not isinstance(payload, dict):
        raise ToolConfigError("model", "工具配置根节点必须为映射")
    # 摘要与解析必须来自同一次读取，避免文件 A→B→A 时将 B 内容标为 A。
    # _UniqueKeyLoader validates every key as str; cast restores that invariant
    # for the application-facing payload after matching PyYAML's base signature.
    return cast(dict[str, Any], payload), hashlib.sha256(content).hexdigest()


def _read_payload(path: Path) -> tuple[dict[str, Any], str]:
    from app.conf.runtime import layer_paths, _merge_settings

    paths = layer_paths(path)
    payload, digest = _read_single_payload(path)
    if len(paths) > 1:
        common, common_digest = _read_single_payload(paths[0])
        payload = _merge_settings(common, payload)
        digest = hashlib.sha256((common_digest + digest).encode()).hexdigest()
    return payload, digest


def _normalize(payload: dict[str, Any]) -> tuple[ToolsConf, dict[str, HandlerConfig]]:
    if payload.get("version") != "tools-v6":
        raise ToolConfigError("model", "工具配置须迁移至 tools-v6，仅支持真实后端")
    defaults = payload.pop("handler_defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - {
        "retry", "max_concurrency", "admission_wait_s", "circuit_failure_threshold", "circuit_cooldown_s"
    }:
        raise ToolConfigError("handler", "handler_defaults 仅支持重试与依赖可用性策略")
    raw_handlers = payload.pop("handlers", {})
    if not isinstance(raw_handlers, dict) or set(raw_handlers) - _HANDLER_NAMES:
        raise ToolConfigError("handler", "handlers 必须为已登记 handler 的映射")
    try:
        handlers = {name: HandlerConfig.model_validate({**defaults, **raw} if isinstance(raw, dict) else raw) for name, raw in raw_handlers.items()}
        config = _SnapshotToolsConf.model_validate(payload)
    except (ValidationError, RecursionError):
        raise ToolConfigError("model", "工具配置字段、取值或一致性校验失败") from None
    missing = _HANDLER_NAMES - set(handlers)
    if missing:
        raise ToolConfigError("handler", "真实工具缺少 handler 配置：" + ", ".join(sorted(missing)))
    for name in DUAL_ENDPOINT_TOOLS:
        auth = config.tools.get_data_tool(name).auth
        if auth is not None and isinstance(auth.invokeInterface, ArkInvokeInterfaces):
            if handlers[name].query_url is None:
                raise ToolConfigError("handler", "分接口鉴权的工具缺少 query_url 连接配置")
    return config, handlers


def load_tool_config(path: Path, *, env_type: str) -> LoadedToolConfig:
    """加载完整候选配置；无缓存、无环境读取、无客户端构造副作用。"""
    loaded, _ = _load_tool_config_with_digest(path, env_type=env_type)
    return loaded


def _load_tool_config_with_digest(path: Path, *, env_type: str) -> tuple[LoadedToolConfig, str]:
    """同时返回配置及同一份源内容摘要，供后台发布前复核。"""
    environment = resolve_env_type(env_type)
    payload, source_digest = _read_payload(path)
    config, handlers = _normalize(payload)
    effective = {
        "env_type": environment,
        "schema_version": "tools-v6",
        "config": config.model_dump(mode="json"),
        "handlers": {name: entry.model_dump(mode="json") for name, entry in handlers.items()},
    }
    # 公开序列化只含遮蔽值；摘要额外识别凭据变化，不输出真实凭据。
    effective["auth_secrets"] = {
        **{f"tool:{name}": entry.auth.client_secret.get_secret_value()
           for name, entry in config.tools.data_tools().items() if entry.auth is not None},
        **{f"intent:{name}": entry.auth.client_secret.get_secret_value()
           for name, entry in config.semantic_direct.endpoints.items() if entry.auth is not None},
    }
    canonical = json.dumps(effective, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    config = config.model_copy(update={"error_normalization": MappingProxyType(config.error_normalization)})
    return LoadedToolConfig(environment, config, MappingProxyType(handlers), revision), source_digest


__all__ = [
    "LoadedToolConfig", "ToolConfigError", "load_tool_config", "resolve_env_type", "resolve_tool_config_path",
]
