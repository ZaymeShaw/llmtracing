"""有限响应的 HTTP 单次执行，不包含业务解析或内部重试。"""
from __future__ import annotations

from opentelemetry import trace

import asyncio
import json
import time

from tool_adapter_sdk.diagnostics import diagnostic

import httpx
from app.tools.real.auth import HttpClient

from tool_adapter_sdk.adapter import (
    HandlerContractError, HandlerDenied, HandlerTimeout, HandlerUnavailable,
)

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024


async def post_json(
    client: HttpClient, url: str, payload: dict[str, object], *, timeout_s: float,
) -> object:
    """错误只暴露固定分类；总预算由调用方的 call_with_policy 管理。"""
    started = time.perf_counter()
    stage, reason = "request_encoding", "request_encoding_failed"
    status_code: int | None = None
    response_bytes = 0
    failure: BaseException | None = None
    try:
        request_body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        trace.get_current_span().set_attribute("request.bytes", len(request_body))
        trace.get_current_span().set_attribute("http.request.method", "POST")
        if len(request_body) > MAX_REQUEST_BYTES:
            reason = "request_too_large"
            raise HandlerContractError()
        stage, reason = "http_request", "transport_failed"
        async with client.stream(
            "POST", url, content=request_body, timeout=timeout_s, follow_redirects=False,
            headers={"Accept-Encoding": "identity", "Content-Type": "application/json"},
        ) as response:
            status_code = response.status_code
            stage, reason = "http_status", "unexpected_http_status"
            trace.get_current_span().set_attribute("http.response.status_code", response.status_code)
            if response.status_code in {401, 403}:
                raise HandlerDenied()
            if response.status_code == 504:
                raise HandlerTimeout()
            if response.status_code == 429 or response.status_code >= 500:
                raise HandlerUnavailable()
            if response.status_code != 200:
                raise HandlerContractError()
            stage, reason = "response_headers", "unsupported_content_encoding"
            # 拒绝压缩，避免解压产生不受响应字节上限约束的内存峰值。
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise HandlerContractError()
            reason = "invalid_or_oversized_content_length"
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                raise HandlerContractError()
            stage, reason = "response_read", "response_too_large"
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                response_bytes = len(body) + len(chunk)
                if response_bytes > MAX_RESPONSE_BYTES:
                    raise HandlerContractError()
                body.extend(chunk)
            trace.get_current_span().set_attribute("response.bytes", len(body))
            stage, reason = "response_decode", "invalid_utf8"
            decoded = body.decode("utf-8")
            stage, reason = "response_json", "invalid_json"
            result = json.loads(decoded)
            reason = "ok"
            return result
    except httpx.TimeoutException as exc:
        failure, reason = exc, "http_timeout"
        raise HandlerTimeout() from None
    except httpx.TransportError as exc:
        failure, reason = exc, "transport_failed"
        raise HandlerUnavailable() from None
    except (ValueError, UnicodeError, RecursionError) as exc:
        failure = exc
        raise HandlerContractError() from None
    except asyncio.CancelledError as exc:
        failure, reason = exc, "request_cancelled"
        raise
    except Exception as exc:
        failure = exc
        if not isinstance(exc, (HandlerContractError, HandlerDenied, HandlerTimeout, HandlerUnavailable)):
            reason = "unexpected_exception"
        raise
    finally:
        if reason != "ok":
            # 不输出 URL、正文、响应头或异常原文；异常类型可区分连接/读取/连接池超时。
            diagnostic(reason, stage=stage, error=failure, http_status=status_code,
                       latency_ms=round((time.perf_counter() - started) * 1000, 3),
                       timeout_s=timeout_s, response_bytes=response_bytes,
                       request_limit_bytes=MAX_REQUEST_BYTES, response_limit_bytes=MAX_RESPONSE_BYTES)
