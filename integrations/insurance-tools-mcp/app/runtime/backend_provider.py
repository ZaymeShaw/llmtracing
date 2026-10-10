"""应用级 BackendRuntime：真实后端组合与生命周期收口。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.conf import tool_config_location, tools_conf
from app.conf.tool_config_loader import ToolConfigError, load_tool_config
from app.runtime.managed_backend import ManagedBackendClient
from app.runtime.tool_config_manager import AdapterFactory, ConfiguredAdapterFactory, ToolConfigManager
from app.tools.backend import (
    BackendClient,
    RealBackendClient,
)
from app.tools.mode import ToolsConf
from app.tools.real.factory import build_configured_real_adapter, validate_builtin_tools
from tool_adapter_sdk.adapter import RealToolAdapter


@dataclass
class BackendRuntime:
    """应用共享的后端 client 及其所有权。"""

    client: BackendClient
    mode: str
    owns_client: bool
    _closed: bool = False

    async def astart(self) -> None:
        """只有组合根持有的运行资源才参与应用启动。"""
        if self.owns_client:
            start = getattr(self.client, "astart", None)
            if start is not None:
                await start()

    async def aclose(self) -> None:
        """仅关闭 provider 自建资源；幂等，便于 lifespan 异常路径复用。"""
        if self._closed:
            return
        self._closed = True
        if not self.owns_client:
            return
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()


def build_backend_runtime(
    *,
    client: BackendClient | None = None,
    real_adapter: RealToolAdapter | None = None,
    real_adapter_factory: AdapterFactory | None = None,
    config_path: Path | None = None,
) -> BackendRuntime:
    """解析应用后端：显式 client 外部所有；默认由配置代持有真实 SDK 适配器。"""
    if client is not None:
        if real_adapter is not None or real_adapter_factory is not None or config_path is not None:
            raise ValueError("client 与 real_adapter/real_adapter_factory/config_path 不得同时传入")
        return BackendRuntime(client=client, mode="injected", owns_client=False)
    if real_adapter is not None and real_adapter_factory is not None:
        raise ValueError("real_adapter 与 real_adapter_factory 不得同时传入")
    env_type, default_path = tool_config_location()
    path = config_path if config_path is not None else default_path
    loaded = load_tool_config(path, env_type=env_type)
    configured_factory: ConfiguredAdapterFactory | None = None
    if real_adapter_factory is None and real_adapter is None:
        validate_builtin_tools(loaded.config)
        configured_factory = build_configured_real_adapter
    elif real_adapter_factory is None:
        # 启动资源装配前就暴露缺失 handler，不把配置错误推迟到业务请求。
        assert real_adapter is not None
        build_real_backend(real_adapter=real_adapter, conf=loaded.config)
    manager = ToolConfigManager(
        path, env_type=env_type, real_adapter=real_adapter, adapter_factory=real_adapter_factory,
        configured_adapter_factory=configured_factory,
    )
    return BackendRuntime(client=ManagedBackendClient(manager), mode="routing", owns_client=True)


def build_real_backend(
    *, real_adapter: RealToolAdapter, conf: ToolsConf | None = None,
) -> RealBackendClient:
    """创建持有适配器的真实后端，并校验全部业务工具的 handler 覆盖。"""
    conf = conf if conf is not None else tools_conf()
    # 固定适配器没有完整配置装配入口，独立使用也不能静默忽略鉴权。
    if any(entry.auth is not None for entry in conf.tools.data_tools().values()):
        raise ToolConfigError("auth_factory", "工具鉴权需要完整配置工厂，不支持固定适配器")
    backend = RealBackendClient(real_adapter=real_adapter, owned_clients=(real_adapter,))
    for tool_name in conf.tools.data_tools():
        backend.ensure_tool_supported(tool_name)
    return backend


__all__ = ["BackendRuntime", "build_backend_runtime", "build_real_backend"]
