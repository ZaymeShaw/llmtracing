"""S2.5 语义直达判定(2026-09-16 Owner 与同事方案 · 独立分发原语,非 G 检查表成员)。

run_gate 确定性检查(状态/显式实参/严格句式/能力锚点)全部未命中(AGENT 默认)后,
关键词预筛后并行调用外部意图判定接口,按 conf 优先级选中 → DIRECT_TOOL(tool + query 原文
透传)。判定的是「意图类型」而非「能否回答」;条件理解仍由下游解析器负责。

与 Gate 检查表的边界:检查表保持 0 后端调用纯函数契约(T2 不破);本原语显式
调用后端判定接口,fail-safe 方向 = 漏放——一切非 status_code=200(含 HTTP 层
失败/超时/坏契约/trace 串线)一律视为未命中,继续后续流转(多走一次 Agent,
安全方向)。判定调用不是工具执行链 RPC,不占 backend_rpc_calls 预算,计数记
budget.semantic_probe_calls;时延属用户首响链路,probes 并行最坏 max(timeout_s),
启用前须重标定 D3 时限(数字在 conf)。

接口协议:docs/接口文档/直达意图判定接口.md(intent-v1 · Python 扁平风格)。
"""
from __future__ import annotations

from app.observability.langfuse_calls import observed_call

import asyncio
from tool_adapter_sdk.compat import timeout, TIMEOUT_ERRORS
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from tool_adapter_sdk.adapter import HandlerContractError, HandlerDenied, HandlerTimeout, HandlerUnavailable

from app.core.session_health import session_is_healthy
from app.observability.progress import ProgressEvent, emit_progress
from app.schemas.route import RouteAction, RouteDecision
from app.tools.mode import SUPPORTED_DIRECT_TOOLS, SemanticDirectEnvConf
from app.tools.auth_config import ArkAuthConfig
from functools import partial
from app.tools.real.http import post_json
from app.tools.real.probe_transport import probe_http_client

logger = logging.getLogger(__name__)

# 直达工具白名单单点在 app/tools/mode.py(Direct 执行器 dispatch 同步扩展)。

ProbeCaller = Callable[[str, dict[str, object], float], Awaitable[object]]


class SemanticProbeConf(BaseModel):
    """单条 probe 登记(顺序即结果优先级);URL/timeout_s 在三环境工具配置
    tool_config_{ENV_TYPE}_args.yaml → semantic_direct 段(环境数据面)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    tool: str


class SemanticDirectConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    probes: list[SemanticProbeConf] = []

    @model_validator(mode="after")
    def validate_probes(self) -> "SemanticDirectConf":
        """登记约束:白名单工具、id/tool 不得重复。"""
        if not self.probes:
            return self
        ids = [probe.id for probe in self.probes]
        tools = [probe.tool for probe in self.probes]
        if len(set(ids)) != len(ids):
            raise ValueError("semantic_direct probes 的 id 不得重复")
        if len(set(tools)) != len(tools):
            raise ValueError("semantic_direct probes 的 tool 不得重复")
        unknown = sorted(set(tools) - SUPPORTED_DIRECT_TOOLS)
        if unknown:
            raise ValueError(f"semantic_direct probes 含未支持直达工具:{unknown}")
        return self


class _IntentJudgeResponse(BaseModel):
    """判定接口响应契约(intent-v1):只认 200+hit;其余一律 fail-safe miss。"""

    model_config = ConfigDict(extra="ignore", strict=True)

    status_code: int
    hit: bool
    trace_id: str | None = None  # 回显防串线(可选);不一致按坏契约处理


@observed_call("backend.semantic_probe.request")
async def _http_probe(
    url: str, payload: dict[str, object], timeout_s: float, *,
    tool: str = "", auth: ArkAuthConfig | None = None,
) -> object:
    """默认判定调用:复用本轮冻结配置代的专用 HTTP 池。post_json 保留单次
    HTTP 防护(字节上限/拒压缩/错误分类),异常由 _judge 归一为 miss。
    httpx Timeout 为逐操作(connect/read/write)计时而非总期限,滴流响应可
    无限拖延——总占用以 asyncio.timeout 兜底(每 probe ≤ timeout_s,
    「并行最坏 max(timeout_s)」的 D3 预算前提,与 tool 层 deadline 惯例同款)。"""
    async with timeout(timeout_s):
        async with probe_http_client(tool, auth) as client:
            return await post_json(client, url, payload, timeout_s=timeout_s)


def _agent_code(deps: Mapping[str, object]) -> str:
    """user_id 尽力取(保单/客户两域显式工号优先,回退上游 user_id;字段名对齐
    接口 7 命名);判定不依赖权限,身份缺失传空串继续——后端仅用于归属日志,
    越权校验在执行工具时发生。"""
    for key in ("empno", "agentno", "upstream_user_id"):
        value = deps.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


async def _judge_hit(
    *,
    url: str,
    timeout_s: float,
    text: str,
    trace_id: str,
    session_id: str | None,
    agent_code: str,
    caller: ProbeCaller,
    probe: str | None = None,
) -> bool:
    """故障向路由返回未命中，但日志区分超时、不可用及坏契约；外部取消传播。"""
    payload: dict[str, object] = {
        "query": text,
        "current_time": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": agent_code,
        "session_id": session_id or "",
        "trace_id": trace_id,
    }
    started, outcome = time.perf_counter(), "unavailable"
    error: BaseException | None = None
    reason: str | None = None
    status_code: int | None = None
    try:
        async with timeout(timeout_s):
            raw = await caller(url, payload, timeout_s)
        response = _IntentJudgeResponse.model_validate(raw)
        status_code = response.status_code
        if response.trace_id is not None and response.trace_id != trace_id:
            outcome = "invalid_contract"
            reason = "trace_mismatch"
            return False
        if response.status_code != 200:
            outcome = "timeout" if response.status_code in (408, 504) else "unavailable"
            reason = "response_status"
            return False
        outcome = "hit" if response.hit else "miss"
        return response.hit
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except (*TIMEOUT_ERRORS, httpx.TimeoutException, HandlerTimeout) as exc:
        outcome = "timeout"
        error, reason = exc, "timeout"
        return False
    except (ValidationError, HandlerContractError) as exc:
        outcome = "invalid_contract"
        error, reason = exc, "invalid_contract"
        return False
    except (httpx.HTTPError, HandlerUnavailable, HandlerDenied) as exc:
        error, reason = exc, "backend_unavailable"
        return False
    except Exception as exc:
        outcome = "error"
        error, reason = exc, "unexpected_exception"
        return False
    finally:
        level = logging.ERROR if outcome == "error" else (
            logging.INFO if outcome in {"hit", "miss", "cancelled"} else logging.WARNING
        )
        logger.log(level, "semantic_probe_completed", extra={"observability": {
            "trace_id": trace_id, "session_id": session_id, "probe": probe,
            "outcome": outcome, "reason": reason, "status_code": status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }}, exc_info=(type(error), error, error.__traceback__) if error is not None else None)


def _probe_tools(query: str) -> frozenset[str]:
    """Cheap intent prefilter, never infer query conditions or trust facts.

    A policy list scoped by customers needs only the policy judge. Ambiguous
    searches retain both judges; ordinary knowledge/chat does not call either.
    """
    from app.core.query_scope import context_dependent_query, explicit_customer_ids
    if context_dependent_query(query):
        return frozenset()
    if re.search(r'多少|总共|数量|几(?:张|份|位|名|个)', query):
        return frozenset()  # Cards cannot answer counts; the Agent must inspect source totals.
    if explicit_customer_ids(query) and re.search(r'详情|要素', query) and '保单' not in query:
        return frozenset()
    if not re.search(r"查|找|筛选|列|有哪些|有哪|名单|清单|多少|名下|姓|生日|谁|哪些|名单|几位|几名|多少位|多少名", query):
        return frozenset()
    policy = "保单" in query
    customer = "客户" in query
    if policy and customer and re.search(r"客户(?:的|名下(?:的)?(?:所有|全部)?)保单", query):
        return frozenset({"policy_search"})
    if policy or customer:
        return frozenset(tool for present, tool in (
            (policy, "policy_search"), (customer, "customer_search"),
        ) if present)
    if re.search(r"查找|查询|筛选|列出|帮我查|帮我找", query):
        return frozenset({"policy_search", "customer_search"})
    return frozenset()


async def semantic_direct_route(
    text: str,
    session_state: dict,
    *,
    deps: Mapping[str, object],
    conf: SemanticDirectConf,
    session_id: str | None = None,
    caller: ProbeCaller | None = None,
    env: SemanticDirectEnvConf | None = None,
) -> RouteDecision | None:
    """S2.5 语义直达判定入口;未命中/关闭/降级/失败一律返回 None(维持 AGENT)。

    调用方(turn_workflow._gate)保证仅在 run_gate 未命中(AGENT)后进入;
    session_state 只写 budget 计数,不写路由/实体状态——判定产物经
    RouteDecision 主通道与既有直达同构。caller/env 惰性解析默认实现(env 读
    当前工具配置快照,enabled/endpoints 随 dev/stg/prd 独立灰度)——便于
    e2e 测试以 monkeypatch 接管判定接口(零网络)走完整 G-4c 链路。
    环境配置读取失败按「全部未接入」跳过——判定配置坏了不惩罚整轮。
    """
    if env is None:
        from app.conf import tools_conf

        try:
            env = tools_conf().semantic_direct
        except Exception:
            logger.exception("semantic_direct_env_conf_error")
            env = SemanticDirectEnvConf()
    if not env.enabled or not conf.probes:
        return None
    if not session_is_healthy(session_state):
        return None  # 降级/错误态不放行(与 G-5 能力锚点仲裁同款守卫,共用 session_health 单点)
    query = text.strip()
    if not query:
        return None
    trace_id = str(deps.get("trace_id") or "").strip()
    if not trace_id:
        return None  # 无链路标识不打外部接口(fail-safe)
    agent_code = _agent_code(deps)
    eligible = _probe_tools(query)
    probes = [probe for probe in conf.probes if probe.tool in eligible
              and (endpoint := env.endpoints.get(probe.tool)) is not None and endpoint.url.strip()]
    if not probes:
        return None
    budget = session_state.setdefault("budget", {})
    started = time.perf_counter()

    async def run_probe(probe: SemanticProbeConf) -> bool:
        endpoint = env.endpoints[probe.tool]
        budget["semantic_probe_calls"] = budget.get("semantic_probe_calls", 0) + 1
        await emit_progress(ProgressEvent(name="semantic_direct", phase="started", probe=probe.id))
        probe_caller = caller
        if probe_caller is None:
            probe_caller = partial(_http_probe, tool=probe.tool, auth=endpoint.auth) if endpoint.auth else _http_probe
        try:
            async with timeout(endpoint.timeout_s):
                hit = await _judge_hit(
                    url=endpoint.url.strip(), timeout_s=endpoint.timeout_s, text=query, trace_id=trace_id,
                    session_id=session_id, agent_code=agent_code, caller=probe_caller, probe=probe.id,
                )
        except asyncio.CancelledError:
            await emit_progress(ProgressEvent(name="semantic_direct", phase="completed", probe=probe.id, status="cancelled"))
            raise
        except TIMEOUT_ERRORS:
            logger.warning("semantic_direct_probe_timeout", extra={
                "semantic_direct": {"probe": probe.id, "trace_id": trace_id},
            })
            hit = False
        except Exception:
            logger.exception("semantic_direct_probe_error", extra={
                "semantic_direct": {"probe": probe.id, "trace_id": trace_id},
            })
            hit = False
        await emit_progress(ProgressEvent(
            name="semantic_direct", phase="completed", probe=probe.id, status="hit" if hit else "miss",
        ))
        return hit

    tasks = [asyncio.create_task(run_probe(probe)) for probe in probes]
    hits: list[bool | None] = [None] * len(tasks)
    chosen: SemanticProbeConf | None = None
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                hits[tasks.index(task)] = task.result()
            # A lower-priority hit waits only for unresolved higher priorities.
            # Once the prefix is settled, slow irrelevant probes can be cancelled.
            for index, hit in enumerate(hits):
                if hit is None:
                    break
                if hit:
                    chosen = probes[index]
                    break
            if chosen is not None:
                break
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        budget["semantic_probe_latency_ms"] = int((time.perf_counter() - started) * 1000)
    if chosen is None:
        return None
    logger.info("semantic_direct_hit", extra={
        "semantic_direct": {"probe": chosen.id, "tool": chosen.tool, "trace_id": trace_id},
    })
    return RouteDecision(
        action=RouteAction.DIRECT_TOOL, matched_check="G-4c",
        resolved_args={"tool": chosen.tool, "query": query},
        reason=f"语义直达判定命中 {chosen.tool}(协议 {conf.version},probe {chosen.id})",
    )
