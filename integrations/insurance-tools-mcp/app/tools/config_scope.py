"""工具配置整轮作用域；同应用嵌套继承，不跨应用复用客户端。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import httpx

    from app.conf.tool_config_loader import LoadedToolConfig
    from app.tools.backend import BackendClient
    from app.tools.mode import ToolsConf
    from app.tools.auth_config import ArkAuthConfig
    from app.tools.real.auth import HttpClient


class ConfigGeneration(Protocol):
    """工具层只依赖代的读取能力，不依赖 runtime 的管理器实现。"""

    @property
    def loaded(self) -> LoadedToolConfig: ...

    @property
    def client(self) -> BackendClient: ...

    @property
    def owner(self) -> object: ...

    @property
    def probe_client(self) -> httpx.AsyncClient: ...

    def authenticated_probe_client(self, tool: str, auth: ArkAuthConfig | None) -> HttpClient: ...


class ConfigManager(Protocol):
    """组合根提供租约端口；工具层不构造客户端或管理发布。"""

    def lease(self) -> AbstractAsyncContextManager[ConfigGeneration]: ...


_CURRENT: ContextVar[ConfigGeneration | None] = ContextVar("tool_config_generation", default=None)
_FALLBACK: ContextVar[ToolsConf | None] = ContextVar("tool_config_fallback", default=None)


def current_fallback_config() -> ToolsConf | None:
    return _FALLBACK.get()


def current_generation(manager: ConfigManager | None = None) -> ConfigGeneration | None:
    generation = _CURRENT.get()
    if generation is not None and (manager is None or generation.owner is manager):
        return generation
    return None


@asynccontextmanager
async def tool_runtime_scope(
    manager: ConfigManager | None, *, freeze_fallback: bool = False,
) -> AsyncIterator[ConfigGeneration | None]:
    """仅外层持有租约，异常与取消均恢复 ContextVar 并释放引用。"""
    generation = current_generation(manager)
    if generation is not None:
        yield generation
        return
    if manager is None:
        token: Token[ToolsConf | None] | None = None
        if freeze_fallback and _FALLBACK.get() is None:
            from app.conf import tools_conf

            # 读取完成后才设置上下文，避免兼容加载入口递归。
            config = tools_conf()
            token = _FALLBACK.set(config)
        try:
            yield None
        finally:
            if token is not None:
                _FALLBACK.reset(token)
        return
    async with manager.lease() as generation:
        generation_token: Token[ConfigGeneration | None] = _CURRENT.set(generation)
        try:
            yield generation
        finally:
            _CURRENT.reset(generation_token)


__all__ = ["current_fallback_config", "current_generation", "tool_runtime_scope"]
