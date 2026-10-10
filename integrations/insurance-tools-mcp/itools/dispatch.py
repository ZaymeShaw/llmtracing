"""通道层:把 MCP 的一次调用接到已有工具核心函数上。

与 `app/tools/bindings.py` 的 Agno 绑定层逐行等价——同样的核心函数、同样的参数（原值直传,
不做 strip/默认值/t 类型转换）、同样的信封返回;**唯一差别是身份来源**:原系统从
`run_context.dependencies` 取,这里从连接级注入的 `Identity` 取。

不要在本模块里加业务逻辑。任何"翻译/编排/裁剪"都归核心函数,否则两个 harness
拿到的就不是同一批工具,对比实验失去意义。入参合法性由协议边界
(`itools/app.py` 按已发布 schema 校验)负责,对应上游由模型侧 JSON Schema 强制的层位。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from tool_adapter_sdk.identity import identity_from_dependencies

from app.core.budget import set_budget_profile
from app.core.envelope import make_envelope
from app.orchestration.assembler import assemble
from app.schemas import EnvelopeStatus, ErrorCode, ToolError
from app.tools.backend import BackendClient
from app.tools.benefit_info import benefit_info as benefit_info_core
from app.tools.customer_detail import customer_detail as customer_detail_core
from app.tools.customer_search import customer_search as customer_search_core
from app.tools.general_qa import general_qa as general_qa_core
from app.tools.policy_detail import fetch_policy_details
from app.tools.policy_search import policy_search as policy_search_core
from app.tools.product_knowledge import product_knowledge as product_knowledge_core

from itools.identity import Identity

# 与 harness 配置里 agent 档同名,单次调用独享一整份 RPC 预算(一次调用最多两跳)。
BUDGET_PROFILE = "agent"


def _session_state() -> dict[str, Any]:
    """按上游 S1 的同一函数造本轮会话态:五命名空间 + 单轮预算账本。

    不用手写 dict —— 上游每轮 run 必经 `assemble()`(assembler.assemble),
    再 `set_budget_profile(budget, "agent")`(turn_workflow:353)。这里同源同顺序。
    """
    state: dict[str, Any] = {}
    assemble(state)
    set_budget_profile(state["budget"], BUDGET_PROFILE)
    return state


async def dispatch(
    name: str,
    arguments: dict[str, Any],
    *,
    identity: Identity,
    backend_client: BackendClient,
) -> str:
    """执行一次工具调用,返回核心函数原样的 str JSON 信封。"""
    trace_id = f"TR-MCP-{uuid.uuid4().hex[:12]}"
    session_id = identity.session_id or str(uuid.uuid4())
    session_state = _session_state()
    budget = session_state["budget"]
    dependencies: dict[str, Any] = {
        "agent_id": identity.agent_id,
        "trace_id": trace_id,
        "backend_client": backend_client,
    }
    dependencies.update(identity.dependency_kwargs())
    common = {"agent_id": identity.agent_id, "trace_id": trace_id, "budget": budget,
              "backend_client": backend_client}

    if name == "product_knowledge":
        return await product_knowledge_core(
            arguments["query"], **common, session_state=session_state,
        )
    if name == "benefit_info":
        return await benefit_info_core(arguments["query"], **common)
    if name == "general_qa":
        return await general_qa_core(arguments["query"], **common)
    if name == "policy_search":
        contexts = (session_state.get("route") or {}).get("policy_search_contexts")
        return await policy_search_core(
            arguments["query"], **common, session_id=session_id,
            contexts=None if contexts == [] else contexts,
            **identity_from_dependencies(dependencies, "policy_search"),
        )
    if name == "customer_search":
        return await customer_search_core(
            arguments["query"], **common, session_id=session_id,
            **identity_from_dependencies(dependencies, "customer_search"),
        )
    if name == "customer_detail":
        return await customer_detail_core(
            arguments.get("customer_id"),
            customer_ids=arguments.get("customer_ids"),
            client_name=arguments.get("client_name"),
            client_mobile=arguments.get("client_mobile"),
            **common,
            **identity_from_dependencies(dependencies, "customer_detail"),
        )
    if name == "policy_detail":
        def _valid(key: str) -> bool:
            value = dependencies.get(key)
            return isinstance(value, str) and bool(value.strip())

        if not (_valid("upstream_user_id") or _valid("empno")):
            return make_envelope(
                status=EnvelopeStatus.DENIED, hint="缺少本轮代理人工号，无法查询保单详情",
                error=ToolError(code=ErrorCode.DENIED, retryable=False),
            )
        polno = arguments["polno"]
        if not polno.strip():
            return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供保单号")
        return await fetch_policy_details(
            polno, arguments.get("fields"), **common,
            **identity_from_dependencies(dependencies, "policy_detail"),
        )
    raise KeyError(f"未登记的工具:{name}")


__all__ = ["dispatch", "BUDGET_PROFILE"]
