"""语义探测专用 HTTP 池；由冻结的工具配置代持有，不保存用户状态。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from app.tools.config_scope import current_generation
from app.tools.auth_config import ArkAuthConfig
from app.tools.real.auth import ArkTokenProvider, AuthenticatedHttpClient, HttpClient


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        # 不接收 Set-Cookie，避免连接复用额外引入跨用户会话状态。
        return False


class ProbeTransport:
    """客户端按需创建；只在创建配置代的 loop 上使用/关闭。"""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        self._bound: dict[tuple[str, ArkAuthConfig], AuthenticatedHttpClient] = {}
        self._auth_client: httpx.AsyncClient | None = None

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("语义探测连接不得跨事件循环使用")

    @property
    def client(self) -> httpx.AsyncClient:
        self._check_loop()
        if self._closed:
            raise RuntimeError("语义探测连接已经关闭")
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                cookies=CookieJar(policy=_NoCookies()),
            )
        return self._client

    def bind(self, tool: str, auth: ArkAuthConfig | None) -> HttpClient:
        client = self.client
        if auth is None:
            return client
        key = (tool, auth)
        if key not in self._bound:
            if self._auth_client is None:
                self._auth_client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                                    cookies=CookieJar(policy=_NoCookies()))
            self._bound[key] = AuthenticatedHttpClient(client, ArkTokenProvider(auth, self._auth_client))
        return self._bound[key]

    async def aclose(self) -> None:
        self._check_loop()
        if self._closed:
            return
        self._closed = True
        for wrapper in self._bound.values():
            wrapper.provider.clear()
        self._bound.clear()
        try:
            if self._auth_client is not None:
                await self._auth_client.aclose()
        finally:
            if self._client is not None:
                await self._client.aclose()


@asynccontextmanager
async def probe_http_client(
    tool: str = "", auth: ArkAuthConfig | None = None,
) -> AsyncIterator[HttpClient]:
    generation = current_generation()
    if generation is not None:
        yield generation.authenticated_probe_client(tool, auth) if auth is not None else generation.probe_client
        return
    # 独立调用/显式注入后端未提供配置管理器时，仍由本次调用拥有并关闭资源。
    transport = ProbeTransport()
    try:
        yield transport.bind(tool, auth)
    finally:
        await transport.aclose()
