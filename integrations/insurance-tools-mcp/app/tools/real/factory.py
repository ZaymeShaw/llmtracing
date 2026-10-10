"""现有 RealToolAdapter 的显式默认装配，不进行插件发现。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any, Literal
from http.cookiejar import CookieJar
from app.tools.real.probe_transport import _NoCookies

from app.core.admission import Admission, CapacityExceeded

import httpx
import asyncio
from app.core.model_circuit import ModelCircuit, CircuitPolicy
from tool_adapter_sdk.deadline import handler_deadline, handler_deadline_scope
from tool_adapter_sdk.adapter import HandlerTimeout, HandlerUnavailable
from tool_adapter_sdk.compat import timeout_at, TIMEOUT_ERRORS

from app.tools.mode import ToolsConf
from app.tools.auth_config import ArkInvokeInterfaces, DUAL_ENDPOINT_TOOLS
from app.conf.tool_config_loader import LoadedToolConfig
from app.tools.real.auth import ArkTokenProvider, AuthenticatedHttpClient, HttpClient
from app.tools.real.benefit import BenefitHandler
from app.tools.real.customer_detail import CustomerDetailHandler
from app.tools.real.customer_search import CustomerSearchHandler
from app.tools.real.policy_detail import PolicyDetailHandler
from app.tools.real.policy_search import PolicySearchHandler
from app.tools.real.general_qa import GeneralQaHandler
from app.tools.real.product_resolve import ProductResolveHandler
from app.tools.real.product_rag import ProductRagHandler
from tool_adapter_sdk.adapter import RealToolAdapter, ToolConfigurationError, HandlerOverloaded
from tool_adapter_sdk.config import HandlerConfig
from tool_adapter_sdk.diagnostics import diagnostic
from tool_adapter_sdk.handlers import REQUIRED_HANDLERS_BY_TOOL, RealToolHandlers


class LimitedHandler:
    """Per-dependency bounded short queue and transient failure circuit."""
    def __init__(self, delegate: Any, *, name: str = "handler") -> None:
        self.delegate = delegate
        config = getattr(delegate, "config", None)
        self.wait_s = getattr(config, "admission_wait_s", 1.0)
        self.timeout_s = getattr(config, "timeout_s", 10.0)
        self.admission = Admission(getattr(config, "max_concurrency", 8), resource=f"handler:{name}")
        self.circuit = ModelCircuit(event_prefix='handler')
        self.circuit_key = ("tool", name, "handler", "local")
        self.circuit_policy = CircuitPolicy(failure_threshold=getattr(config, "circuit_failure_threshold", 3),
                                           cooldown_s=getattr(config, "circuit_cooldown_s", 15.0))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        configured_deadline = asyncio.get_running_loop().time() + self.timeout_s
        deadline = handler_deadline(self.timeout_s)
        caller_limited = deadline < configured_deadline
        if deadline <= asyncio.get_running_loop().time():
            raise HandlerTimeout()
        permit = self.circuit.acquire(self.circuit_key, policy=self.circuit_policy)
        if permit is None:
            diagnostic("handler_circuit_open", stage="admission", attempts=0)
            raise HandlerUnavailable()
        try:
            ticket = await self.admission.acquire_wait(min(self.wait_s, max(0.0, deadline - asyncio.get_running_loop().time())))
        except CapacityExceeded:
            self.circuit.release(self.circuit_key, permit)
            if asyncio.get_running_loop().time() >= deadline:
                raise HandlerTimeout() from None
            counts = self.admission.snapshot()
            diagnostic("handler_capacity_exceeded", stage="admission", current=counts["active"],
                       limit=counts["limit"], attempts=0)
            raise HandlerOverloaded() from None
        except BaseException:
            self.circuit.release(self.circuit_key, permit)
            raise
        try:
            with handler_deadline_scope(deadline):
                async with timeout_at(deadline):
                    result = await self.delegate(*args, **kwargs)
            self.circuit.observe(self.circuit_key, failed=False, permit=permit)
            return result
        except (HandlerTimeout, HandlerUnavailable, *TIMEOUT_ERRORS) as exc:
            if not (caller_limited and asyncio.get_running_loop().time() >= deadline):
                self.circuit.observe(self.circuit_key, failed=True, permit=permit)
            if isinstance(exc, TIMEOUT_ERRORS):
                raise HandlerTimeout() from exc
            raise
        finally:
            self.circuit.release(self.circuit_key, permit)
            ticket.release()


def isolate_handlers(handlers: RealToolHandlers) -> RealToolHandlers:
    updates: dict[str, Any] = {
        field.name: LimitedHandler(handler, name=field.name)
        for field in fields(handlers)
        if field.name != "api_version" and (handler := getattr(handlers, field.name)) is not None
    }
    return replace(handlers, **updates)


def validate_builtin_tools(conf: ToolsConf) -> None:
    """在创建资源前拒绝尚未接入的真实工具，保留启动即失败语义。"""
    for name in conf.tools.data_tools():
        if name not in REQUIRED_HANDLERS_BY_TOOL:
            raise ToolConfigurationError(f"默认 RealToolAdapter 尚未实现工具:{name}")


async def build_configured_real_adapter(loaded: LoadedToolConfig) -> RealToolAdapter:
    validate_builtin_tools(loaded.config)
    return await build_real_adapter(loaded.handlers, conf=loaded.config)


async def build_real_adapter(
    configs: Mapping[str, HandlerConfig], *, conf: ToolsConf | None = None,
) -> RealToolAdapter:
    client = httpx.AsyncClient(
        trust_env=False, follow_redirects=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    auth_client: httpx.AsyncClient | None = None
    bound: dict[tuple[str, str], HttpClient] = {}

    async def close() -> None:
        for wrapper in bound.values():
            if isinstance(wrapper, AuthenticatedHttpClient):
                wrapper.provider.clear()
        try:
            if auth_client is not None:
                await auth_client.aclose()
        finally:
            await client.aclose()

    try:
        if conf is not None:
            for tool, entry in conf.tools.data_tools().items():
                if entry.auth is not None:
                    if auth_client is None:
                        auth_client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                                        cookies=CookieJar(policy=_NoCookies()))
                    entry.auth.validate_roles(query_required=tool in DUAL_ENDPOINT_TOOLS)
                    roles: tuple[Literal["url", "query_url"], ...] = ("url",)
                    if isinstance(entry.auth.invokeInterface, ArkInvokeInterfaces) and tool in DUAL_ENDPOINT_TOOLS:
                        roles = ("url", "query_url")
                    for role in roles:
                        auth = entry.auth.for_endpoint(role)
                        if auth is None:
                            continue
                        wrapped = AuthenticatedHttpClient(
                            client, ArkTokenProvider(auth, auth_client),
                        )
                        for operation in REQUIRED_HANDLERS_BY_TOOL[tool]:
                            bound[operation, role] = wrapped
                            if isinstance(entry.auth.invokeInterface, str) and tool in DUAL_ENDPOINT_TOOLS:
                                bound[operation, "query_url"] = wrapped
        config = configs.get("qa_recall")
        benefit_config = configs.get("benefit_detail")
        resolve_config = configs.get("product_resolve")
        rag_config = configs.get("product_rag")
        customer_config = configs.get("customer_detail")
        search_config = configs.get("policy_search")
        detail_config = configs.get("policy_detail")
        customer_search_config = configs.get("customer_search")
        return RealToolAdapter(
            isolate_handlers(RealToolHandlers(
                api_version="3",
                policy_detail=(PolicyDetailHandler(bound.get(("policy_detail", "url"), client), detail_config,
                                      query_client=bound.get(("policy_detail", "query_url"), client))
                               if detail_config is not None and detail_config.query_url is not None else None),
                policy_search=(PolicySearchHandler(bound.get(("policy_search", "url"), client), search_config,
                                      query_client=bound.get(("policy_search", "query_url"), client))
                               if search_config is not None and search_config.query_url is not None else None),
                customer_search=(CustomerSearchHandler(bound.get(("customer_search", "url"), client), customer_search_config,
                                      query_client=bound.get(("customer_search", "query_url"), client))
                                 if customer_search_config is not None and customer_search_config.query_url is not None else None),
                customer_detail=CustomerDetailHandler(bound.get(("customer_detail", "url"), client), customer_config) if customer_config is not None else None,
                product_resolve=ProductResolveHandler(bound.get(("product_resolve", "url"), client), resolve_config) if resolve_config is not None else None,
                product_rag=ProductRagHandler(bound.get(("product_rag", "url"), client), rag_config) if rag_config is not None else None,
                qa_recall=GeneralQaHandler(bound.get(("qa_recall", "url"), client), config) if config is not None else None,
                benefit_detail=BenefitHandler(bound.get(("benefit_detail", "url"), client), benefit_config) if benefit_config is not None else None,
            )),
            adapter_id="builtin-http", source_version="knowledge-context-v1", close=close,
        )
    except BaseException:
        await close()
        raise
