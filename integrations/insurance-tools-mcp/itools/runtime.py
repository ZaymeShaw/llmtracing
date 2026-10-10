"""后端适配器装配:与上游 /v1/chat 同一条组合根路径。

上游用 `build_backend_runtime()` —— `ManagedBackendClient` 转发到本轮持有的配置代
(支持工具配置热更)。这里照用同一条,不用裸 adapter,免得配置代语义与 agno 侧分叉。
进程启动时建一次,全程复用同一批 HTTP 连接。
"""
from __future__ import annotations

import os

from app.conf.tool_config_loader import resolve_env_type
from app.runtime.backend_provider import BackendRuntime, build_backend_runtime

ENV_TYPE_ENV = "ENV_TYPE"


def resolved_env_type() -> str:
    """`ENV_TYPE` 决定读哪份下游地址配置;未设置时按既有约定回落 dev。"""
    return resolve_env_type(os.environ.get(ENV_TYPE_ENV))


async def build_backend_runtime_for_process() -> tuple[BackendRuntime, str]:
    from app.conf import tool_config_location

    env_type, _ = tool_config_location()
    runtime = build_backend_runtime()
    await runtime.astart()
    return runtime, env_type


__all__ = ["build_backend_runtime_for_process", "resolved_env_type", "ENV_TYPE_ENV"]
