"""以稳定的应用级客户端转发到本轮持有的配置代。"""
from __future__ import annotations

from typing import Any

from tool_adapter_sdk.identity import identity_kwargs
from app.runtime.tool_config_manager import ToolConfigManager
from app.tools.config_scope import current_generation, tool_runtime_scope
from app.tools.mode import require_tool_execution_context


class ManagedBackendClient:
    """共享配置管理器，不复制连接池，也不在 RPC 间重新选择配置。"""

    def __init__(self, manager: ToolConfigManager) -> None:
        self.config_manager = manager

    def __deepcopy__(self, memo: dict[int, Any]) -> ManagedBackendClient:
        memo[id(self)] = self
        return self

    async def astart(self) -> None:
        await self.config_manager.start()

    async def aclose(self) -> None:
        await self.config_manager.aclose()

    async def call(
        self, system: str, endpoint: str, payload: dict[str, Any], *,
        agent_id: str, trace_id: str, request_id: str, session_id: str | None = None,
        upstream_user_id: str | None = None,
        empno: str | None = None,
        agentno: str | None = None,
    ) -> object:
        async with tool_runtime_scope(self.config_manager):
            generation = current_generation(self.config_manager)
            if generation is None:
                raise RuntimeError("工具配置代未初始化")
            has_identity = any(value is not None for value in (upstream_user_id, empno, agentno))
            context = require_tool_execution_context() if has_identity else None
            return await generation.client.call(
                system, endpoint, payload,
                agent_id=agent_id, trace_id=trace_id, request_id=request_id,
                **({"session_id": session_id} if session_id is not None else {}),
                **(identity_kwargs(context.tool_name, upstream_user_id=upstream_user_id, empno=empno, agentno=agentno)
                   if context is not None else {}),
            )
