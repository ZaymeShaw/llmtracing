"""Ark 懒加载令牌及请求级注入；缓存与连接由配置代持有。"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

import httpx
from opentelemetry.instrumentation.utils import suppress_http_instrumentation

from app.tools.auth_config import ArkAuthConfig
from tool_adapter_sdk.adapter import HandlerContractError, HandlerDenied, HandlerTimeout, HandlerUnavailable, ToolConfigurationError
from tool_adapter_sdk.compat import TIMEOUT_ERRORS, timeout
from tool_adapter_sdk.diagnostics import diagnostic

MAX_AUTH_RESPONSE_BYTES = 64 * 1024
_SENSITIVE_HTTP: ContextVar[bool] = ContextVar("sensitive_tool_http", default=False)


class _SensitiveHttpFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _SENSITIVE_HTTP.get()


# HTTPX INFO 含完整 URL；HTTPcore DEBUG 含响应头。过滤发生在源 logger，
# 不依赖应用出口，也不临时修改全进程级别影响其他并发请求。
_FILTER = _SensitiveHttpFilter()
for _logger_name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                     "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_logger_name).addFilter(_FILTER)


@contextmanager
def _sensitive_http() -> Any:
    token = _SENSITIVE_HTTP.set(True)
    try:
        with suppress_http_instrumentation():
            yield
    finally:
        _SENSITIVE_HTTP.reset(token)


def _positive_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise HandlerContractError()
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise HandlerContractError() from None
    if not math.isfinite(number) or number <= 0:
        raise HandlerContractError()
    return number


class ArkTokenProvider:
    """单身份单代缓存；锁等待与刷新均包含在调用方剩余时限内。"""

    def __init__(self, config: ArkAuthConfig, client: httpx.AsyncClient) -> None:
        if not isinstance(config.invokeInterface, str):
            raise ToolConfigurationError("Ark provider 需要已解析的字符串 invokeInterface")
        self._invoke_interface = config.invokeInterface
        self._config = config
        self._client = client
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._deadline = 0.0

    def clear(self) -> None:
        self._token, self._deadline = None, 0.0

    async def token(self, timeout_s: float) -> str:
        try:
            async with timeout(timeout_s):
                async with self._lock:
                    if self._token is not None and time.monotonic() < self._deadline:
                        return self._token
                    return await self._fetch(timeout_s)
        except (*TIMEOUT_ERRORS, httpx.TimeoutException):
            diagnostic("auth_timeout", stage="authentication")
            raise HandlerTimeout() from None
        except httpx.TransportError:
            diagnostic("auth_unavailable", stage="authentication")
            raise HandlerUnavailable() from None
        except (HandlerContractError, HandlerDenied, HandlerUnavailable, HandlerTimeout):
            diagnostic("auth_failed", stage="authentication")
            raise
        except (ValueError, UnicodeError, RecursionError):
            diagnostic("auth_invalid_response", stage="authentication")
            raise HandlerContractError() from None

    async def _fetch(self, timeout_s: float) -> str:
        started = time.monotonic()
        wall_started = time.time()
        conf = self._config
        with _sensitive_http():
            async with self._client.stream(
                "POST", conf.url, params={
                    "client_id": conf.client_id, "client_secret": conf.client_secret.get_secret_value(),
                    "grant_type": conf.grant_type, "invokeInterface": self._invoke_interface,
                }, timeout=timeout_s, follow_redirects=False, headers={"Accept-Encoding": "identity"},
            ) as response:
                if response.status_code in {401, 403}:
                    raise HandlerDenied()
                if response.status_code == 504:
                    raise HandlerTimeout()
                if response.status_code == 429 or response.status_code >= 500:
                    raise HandlerUnavailable()
                if response.status_code != 200:
                    raise HandlerContractError()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise HandlerContractError()
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_AUTH_RESPONSE_BYTES):
                    raise HandlerContractError()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    if len(body) + len(chunk) > MAX_AUTH_RESPONSE_BYTES:
                        raise HandlerContractError()
                    body.extend(chunk)
        raw = json.loads(body.decode("utf-8"))
        if not isinstance(raw, dict):
            raise HandlerContractError()
        if raw.get("code") != "0":
            raise HandlerDenied()
        token = raw.get("access_token")
        # HTTP 请求头只接受可见 ASCII；拒绝控制字符、空白以及过大的值。
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise HandlerContractError()
        if "expirationTime" in raw:
            lifetime = _positive_number(raw["expirationTime"]) / 1000 - wall_started
        else:
            lifetime = _positive_number(raw.get("expires_in"))
        deadline = started + lifetime
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise HandlerContractError()
        self._token, self._deadline = token, deadline
        return token


class AuthenticatedHttpClient:
    """只包装 stream 端口，借用业务连接池，不修改其共享 headers。"""

    def __init__(self, client: httpx.AsyncClient, provider: ArkTokenProvider) -> None:
        self.client = client
        self.provider = provider

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: Any) -> AsyncIterator[httpx.Response]:
        timeout_s = float(kwargs["timeout"])
        started = time.monotonic()
        try:
            async with timeout(timeout_s):
                token = await self.provider.token(timeout_s)
                headers = httpx.Headers(kwargs.pop("headers", None))
                headers["token"] = token
                kwargs["timeout"] = max(0.0, timeout_s - (time.monotonic() - started))
                with _sensitive_http():
                    async with self.client.stream(method, url, headers=headers, **kwargs) as response:
                        yield response
        except TIMEOUT_ERRORS:
            raise HandlerTimeout() from None


HttpClient = httpx.AsyncClient | AuthenticatedHttpClient
