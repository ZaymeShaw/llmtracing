"""应用级工具配置代：校验后发布、租约冻结与延迟资源回收。"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakSet

import httpx

from app.conf.tool_config_loader import (
    MAX_CONFIG_BYTES, LoadedToolConfig, ToolConfigError, _load_tool_config_with_digest, resolve_env_type,
)
from app.tools.backend import RealBackendClient
from app.tools.mode import BackendConfigurationError
from app.tools.real.probe_transport import ProbeTransport
from app.tools.auth_config import ArkAuthConfig
from app.tools.real.auth import HttpClient
from tool_adapter_sdk.adapter import RealToolAdapter
from tool_adapter_sdk.config import HandlerConfig

logger = logging.getLogger(__name__)
ConfiguredAdapterFactory = Callable[[LoadedToolConfig], Awaitable[RealToolAdapter] | RealToolAdapter]
AdapterFactory = Callable[[Mapping[str, HandlerConfig]], Awaitable[RealToolAdapter] | RealToolAdapter]


@dataclass(eq=False)
class Generation:
    """完整配置与客户端绑定；引用计数只在所属事件循环修改。"""

    loaded: LoadedToolConfig
    client: RealBackendClient
    owner: ToolConfigManager = field(repr=False)
    loaded_at: float = field(default_factory=time.time)
    references: int = 0
    retired: bool = False
    close_task: asyncio.Task[None] | None = field(default=None, repr=False)
    probe_transport: ProbeTransport = field(default_factory=ProbeTransport, repr=False)

    @property
    def probe_client(self) -> httpx.AsyncClient:
        if self.references == 0 or self.close_task is not None:
            raise RuntimeError("语义探测需要有效的工具配置租约")
        return self.probe_transport.client

    def authenticated_probe_client(self, tool: str, auth: ArkAuthConfig | None) -> HttpClient:
        _ = self.probe_client  # 复用租约有效性检查。
        return self.probe_transport.bind(tool, auth)


def _single_digest(path: Path) -> str:
    with path.open("rb") as stream:
        content = stream.read(MAX_CONFIG_BYTES + 1)
    if len(content) > MAX_CONFIG_BYTES:
        raise BackendConfigurationError("工具配置文件超过体积上限")
    return hashlib.sha256(content).hexdigest()


def _digest(path: Path) -> str:
    from app.conf.runtime import layer_paths
    digests = [_single_digest(p) for p in layer_paths(path)]
    return digests[0] if len(digests) == 1 else hashlib.sha256("".join(digests).encode()).hexdigest()


def _load_candidate(path: Path, env_type: str) -> tuple[LoadedToolConfig, str]:
    loaded, before = _load_tool_config_with_digest(path, env_type=env_type)
    if before != _digest(path):
        raise ToolConfigError("source_changed", "读取期间配置发生变化")
    return loaded, before


class ToolConfigManager:
    """构造无副作用，start 完成首代准备后才启动唯一轮询任务。"""

    def __init__(
        self,
        path: Path,
        *,
        env_type: str,
        adapter_factory: AdapterFactory | None = None,
        configured_adapter_factory: ConfiguredAdapterFactory | None = None,
        real_adapter: RealToolAdapter | None = None,
        poll_interval_s: float = 1.0,
    ) -> None:
        if sum(value is not None for value in (adapter_factory, configured_adapter_factory, real_adapter)) > 1:
            raise ValueError("adapter_factory、configured_adapter_factory 与 real_adapter 互斥")
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise ValueError("poll_interval_s 必须为有限正数")
        self.path = path
        self.env_type = resolve_env_type(env_type)
        self.adapter_factory = adapter_factory
        self.configured_adapter_factory = configured_adapter_factory
        self.real_adapter = real_adapter
        self.poll_interval_s = poll_interval_s
        self.current: Generation | None = None
        self.failure_count = 0
        self.last_success_at: float | None = None
        self.last_error_category: str | None = None
        self._source_digest: str | None = None
        self._reload_lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._generations: set[Generation] = set()
        self._factory_adapters: WeakSet[RealToolAdapter] = WeakSet()
        self._closed = False
        self._fixed_close_task: asyncio.Task[None] | None = None
        self._last_failure_log: tuple[str, float] | None = None

    @property
    def pending_retirements(self) -> int:
        return sum(generation.retired for generation in self._generations)

    async def start(self) -> None:
        if self._closed:
            raise BackendConfigurationError("工具配置管理器已经关闭")
        if not await self.reload() and self.current is None:
            raise BackendConfigurationError(
                f"工具配置启动失败（类别：{self.last_error_category}）"
            ) from None
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll(), name="tool-config-poll")

    async def _prepare(self, loaded: LoadedToolConfig) -> Generation:
        if self.configured_adapter_factory is None and any(
            entry.auth is not None for entry in loaded.config.tools.data_tools().values()
        ):
            raise ToolConfigError("auth_factory", "工具鉴权需要完整配置工厂，不支持旧工厂或固定适配器")
        if (self.current is not None and self.real_adapter is not None
                and loaded.handlers != self.current.loaded.handlers):
            raise ToolConfigError("fixed_adapter", "固定适配器不支持连接参数热更新，请使用 adapter_factory")
        adapter = self.real_adapter
        owned: tuple[object, ...] = ()
        if self.adapter_factory is not None or self.configured_adapter_factory is not None:
            # factory 抛错之前创建的局部资源由 factory 自己清理。
            if self.configured_adapter_factory is not None:
                result = self.configured_adapter_factory(loaded)
            else:
                assert self.adapter_factory is not None
                result = self.adapter_factory(loaded.handlers)
            adapter = await result if inspect.isawaitable(result) else result
            if not isinstance(adapter, RealToolAdapter):
                raise ToolConfigError("prepare", "adapter_factory 必须返回 RealToolAdapter")
            if adapter in self._factory_adapters:
                # 该对象仍由既有代持有或已经退役，拒绝时不得关闭它。
                raise ToolConfigError("prepare", "adapter_factory 必须为每一代创建新的适配器")
            self._factory_adapters.add(adapter)
            owned = (adapter,)
        if adapter is None:
            raise ToolConfigError("handler", "真实后端未注入 RealToolAdapter 或 adapter_factory")
        client = RealBackendClient(real_adapter=adapter, owned_clients=owned)
        generation = Generation(loaded, client, self)
        try:
            for name in loaded.config.tools.data_tools():
                client.ensure_tool_supported(name)
        except Exception:
            await self._discard(generation)
            raise ToolConfigError("handler", "真实工具 handler 覆盖校验失败") from None
        return generation

    async def reload(self) -> bool:
        """返回是否成功接受当前文件；未改变也返回 True，失败保留上一代。"""
        async with self._reload_lock:
            if self._closed:
                return False
            started = time.monotonic()
            candidate: Generation | None = None
            stage = "read"
            try:
                if await asyncio.to_thread(_digest, self.path) == self._source_digest:
                    if self.last_error_category is not None:
                        self._log("recovered", started, new=self.current)
                        self.last_success_at = time.time()
                        self.last_error_category = None
                        self._last_failure_log = None
                    return True
                stage = "validation"
                loaded, digest = await asyncio.to_thread(_load_candidate, self.path, self.env_type)
                if self.current is not None and loaded.revision == self.current.loaded.revision:
                    self._source_digest = digest
                    self.last_success_at = time.time()
                    self.last_error_category = None
                    self._last_failure_log = None
                    return True
                stage = "prepare"
                candidate = await self._prepare(loaded)
                stage = "source_changed"
                if self._closed or digest != await asyncio.to_thread(_digest, self.path):
                    raise ToolConfigError("source_changed", "准备期间配置发生变化或管理器关闭")
                # 单事件循环内无 await：取得租约和发布之间不存在半代状态。
                previous = self.current
                self.current = candidate
                self._generations.add(candidate)
                self._source_digest = digest
                self.last_success_at = time.time()
                self.last_error_category = None
                self._last_failure_log = None
                if previous is not None:
                    previous.retired = True
                    self._maybe_retire(previous)
                self._log("published", started, old=previous, new=candidate)
                candidate = None
                return True
            except asyncio.CancelledError:
                if candidate is not None:
                    await self._discard(candidate)
                raise
            except Exception as error:
                if candidate is not None:
                    await self._discard(candidate)
                if isinstance(error, ToolConfigError):
                    stage = error.category
                self.failure_count += 1
                self.last_error_category = stage
                self._log("rejected", started, old=self.current, category=stage)
                return False

    def _log(
        self, result: str, started: float, *, category: str | None = None,
        old: Generation | None = None, new: Generation | None = None,
    ) -> None:
        now = time.monotonic()
        if category is not None:
            if self._last_failure_log and self._last_failure_log[0] == category:
                if now - self._last_failure_log[1] < 30:
                    return
            self._last_failure_log = (category, now)
        fields = {
            "env_type": self.env_type, "result": result, "error_category": category,
            "old_revision": old.loaded.revision if old else None,
            "new_revision": new.loaded.revision if new else None,
            "duration_ms": int((now - started) * 1000),
        }
        # 不输出异常正文、路径、URL 或完整配置，防止校验错误泄漏参数。
        logger.log(logging.WARNING if category else logging.INFO, "tool_config_reload", extra={"tool_config": fields})

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_s)
            await self.reload()

    async def _close_client(self, generation: Generation) -> None:
        try:
            try:
                await generation.probe_transport.aclose()
            except Exception:
                logger.warning("resource_cleanup_failed", exc_info=True,
                               extra={"observability": {"resource": "semantic_probe_transport"}})
            try:
                await generation.client.aclose()
            except Exception:
                self._log("close_failed", time.monotonic(), category="close")
        finally:
            self._generations.discard(generation)

    async def _discard(self, generation: Generation) -> None:
        generation.retired = True
        self._generations.add(generation)
        task = self._maybe_retire(generation)
        if task is not None:
            await asyncio.shield(task)

    def _maybe_retire(self, generation: Generation) -> asyncio.Task[None] | None:
        if generation.retired and generation.references == 0 and generation.close_task is None:
            generation.close_task = asyncio.create_task(self._close_client(generation), name="tool-config-retire")
        return generation.close_task

    async def _close_fixed(self) -> None:
        if self.real_adapter is not None:
            try:
                await self.real_adapter.aclose()
            except Exception:
                self._log("close_failed", time.monotonic(), category="close")

    def _maybe_close_fixed(self) -> asyncio.Task[None] | None:
        if (self._closed and self._fixed_close_task is None
                and not any(generation.references for generation in self._generations)):
            self._fixed_close_task = asyncio.create_task(self._close_fixed(), name="tool-config-fixed-close")
        return self._fixed_close_task

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Generation]:
        generation = self.current
        if self._closed or generation is None:
            raise BackendConfigurationError("工具配置管理器尚未启动或已经关闭")
        generation.references += 1
        try:
            yield generation
        finally:
            generation.references -= 1
            tasks = (self._maybe_retire(generation), self._maybe_close_fixed())
            for task in tasks:
                if task is not None:
                    await asyncio.shield(task)

    async def aclose(self) -> None:
        """停止发布并退役；仍有租约的资源由最后一个释放者关闭。"""
        self._closed = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        async with self._reload_lock:
            tasks = []
            for generation in tuple(self._generations):
                generation.retired = True
                task = self._maybe_retire(generation)
                if task is not None:
                    tasks.append(task)
            fixed = self._maybe_close_fixed()
            if fixed is not None:
                tasks.append(fixed)
        for task in tasks:
            await asyncio.shield(task)


__all__ = ["AdapterFactory", "ConfiguredAdapterFactory", "Generation", "ToolConfigManager"]
