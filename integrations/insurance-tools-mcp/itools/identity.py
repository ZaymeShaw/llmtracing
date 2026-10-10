"""身份通道:等价于原系统请求体注入的字段,模型不可见。

原系统里工号由上游 `/v1/chat` 的可选字段 `user_id` / `empno` / `agentno` 在运行时注入,
模型与前端传入的身份都不作权限主体。MCP 没有请求体,所以改成连接级注入(stdio 走客户端
配置的 env,HTTP 走连接头),但**契约逐条保持**:

- `agent_id` 是认证主体,必填;缺失即拒绝启动,不用任何其它值兜底。
- `user_id` 是业务 user_id;保单解析与查询要求它非空,缺失时工具按原系统同样方式报错,
  **不用 agent_id 顶替**(原系统明确禁止"缺业务值以认证值兜底")。
- `empno` / `agentno` 是显式工号;缺省由核心函数回退到 `user_id`,与本项目内既有行为一致。
- 显式给了空串/空白即视为配置错误,启动期拒绝——与原系统聊天入口拒绝空白显式值同口径。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from tool_adapter_sdk.adapter import HandlerContractError

AGENT_ID_ENV = "ITOOLS_AGENT_ID"
USER_ID_ENV = "ITOOLS_USER_ID"
EMPNO_ENV = "ITOOLS_EMPNO"
AGENTNO_ENV = "ITOOLS_AGENTNO"
SESSION_ID_ENV = "ITOOLS_SESSION_ID"


@dataclass(frozen=True)
class Identity:
    """一次 MCP 连接绑定的可信身份。"""

    agent_id: str
    upstream_user_id: str | None = None
    empno: str | None = None
    agentno: str | None = None
    session_id: str | None = None

    def dependency_kwargs(self) -> dict[str, str]:
        """仅携带非空元数据,保留原系统"无值不注入"的形状。"""
        values = {
            "upstream_user_id": self.upstream_user_id,
            "empno": self.empno,
            "agentno": self.agentno,
        }
        return {key: value for key, value in values.items() if value is not None}


def _read(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise HandlerContractError(f"{name} 已显式设置但为空白;请删除该变量或填入有效值")
    return value


def identity_from_env() -> Identity:
    """从连接级环境读取身份;缺 agent_id 直接失败,不做隐式兜底。"""
    agent_id = _read(AGENT_ID_ENV)
    if agent_id is None:
        raise HandlerContractError(
            f"缺少 {AGENT_ID_ENV}:MCP 客户端未注入认证主体,拒绝启动"
        )
    return Identity(
        agent_id=agent_id,
        upstream_user_id=_read(USER_ID_ENV),
        empno=_read(EMPNO_ENV),
        agentno=_read(AGENTNO_ENV),
        session_id=_read(SESSION_ID_ENV),
    )


__all__ = ["Identity", "identity_from_env", "AGENT_ID_ENV", "USER_ID_ENV", "EMPNO_ENV", "AGENTNO_ENV", "SESSION_ID_ENV"]
