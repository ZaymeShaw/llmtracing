"""查询工具的代理人工号元数据；user_id/empno/agentno 表示同一工号。"""
from __future__ import annotations

from collections.abc import Mapping

from tool_adapter_sdk.diagnostics import diagnostic

IDENTITY_TOOLS = frozenset({"policy_search", "policy_detail", "customer_search", "customer_detail"})


def identity_kwargs(
    tool_name: str, *, upstream_user_id: str | None = None,
    empno: str | None = None, agentno: str | None = None,
) -> dict[str, str]:
    """仅向对应业务工具传递非空缺元数据，保留旧客户端缺省调用形状。"""
    if tool_name not in IDENTITY_TOOLS:
        return {}
    field = "empno" if tool_name.startswith("policy_") else "agentno"
    explicit = empno if field == "empno" else agentno
    values = {"upstream_user_id": upstream_user_id, field: explicit}
    return {key: value for key, value in values.items() if value is not None}


def identity_from_dependencies(dependencies: Mapping[str, object] | None, tool_name: str) -> dict[str, str]:
    """读取本轮运行时依赖；无值不注入，不从会话或认证身份补造。"""
    values = dependencies or {}
    selected = {}
    for key in ("upstream_user_id", "empno", "agentno"):
        value = values.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise TypeError(f"{key} 必须是字符串")
            selected[key] = value
    return identity_kwargs(tool_name, **selected)


def resolve_agent_number(explicit: str | None, upstream_user_id: str | None, *, max_length: int | None = None) -> str:
    """兼容单字段传入；同时提供工号和 user_id 时必须一致，非法值不能静默回退。"""
    from tool_adapter_sdk.adapter import HandlerContractError

    if explicit is not None and upstream_user_id is not None and explicit != upstream_user_id:
        diagnostic("agent_identity_conflict", stage="identity_validation")
        raise HandlerContractError("代理人工号与上游 user_id 不一致")
    value = explicit if explicit is not None else upstream_user_id
    if not isinstance(value, str) or not value.strip():
        diagnostic("agent_identity_missing", stage="identity_validation")
        raise HandlerContractError("缺少有效代理人工号或上游 user_id")
    if max_length is not None and len(value) > max_length:
        diagnostic("agent_identity_too_long", stage="identity_validation", current=len(value), limit=max_length)
        raise HandlerContractError("代理人工号或上游 user_id 超过长度限制")
    return value
