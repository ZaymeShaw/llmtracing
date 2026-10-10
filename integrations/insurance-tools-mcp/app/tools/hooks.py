"""BE-04: boundary → turn control → metrics, using Agno's wrapping hook API.

Control owns admission, coalescing, real-start quotas, parsed-envelope pooling and stops.
Metrics measures only actual executions. Backend RPC accounting stays in call_backend.
Boundary retains the original business result and supplies the bounded model view.
"""

from __future__ import annotations
from app.core.identifiers import matching_text

from app.core.audit import count_refusal
from app.core.resolve import POLICY_NO_PATTERN

from tool_adapter_sdk.diagnostics import diagnostic

from tool_adapter_sdk.compat import TIMEOUT_ERRORS
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from functools import wraps

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agno.exceptions import AgentRunException, RunCancelledException, StopAgentRun

from app.conf import gate_conf, harness_conf
from app.conf.runtime import read_once
from app.core.turn_control import StopKind
from app.tools.tool_sets import RAG_TOOLS, STATE_TOOLS
from app.tools.turn_admission import admit
from app.tools.selection_redirect import observe_selection, resolve_redirect
from contextvars import ContextVar
from app.tools.rpc_identity import tool_rpc_key
from app.tools.model_view import ModelView
from app.core.entity_memory import remember_tool_entities, known_subjects
from app.core.tool_ledger import cached, record, records, direct_record
from app.core.model_budget import current_model_budget
from app.core.envelope import TOOL_FAILURE_ANSWERS, make_envelope
from app.core.evidence_pool import merge_into_pool
from app.core.tool_control import argument_hint, SELECTION_MESSAGES, control_message, refusal_envelope
from app.schemas.tool_control import ToolControlReason
from app.observability.tool_calls import observe_backend_calls
from app.schemas import EnvelopeStatus, RouteAction, ToolEnvelope, ToolError
from app.schemas.enums import ErrorCode
from app.tools.model_output import (
    TERMINAL_SEARCH_TOOLS,
    record_business_tool_result,
)

if TYPE_CHECKING:  # hook 按参数名注入,不依赖 agno 类型(直调/替身可测)
    from agno.run import RunContext
    from app.core.harness_config import HarnessConf

logger = logging.getLogger(__name__)

# metrics 保留最近 N 条(会话内;防长会话 state 膨胀,§8.1 状态瘦身)
_METRICS_KEEP = 20
_POLICY_REF_RE = re.compile(rf"(?:{POLICY_NO_PATTERN}|\d{{3,14}})")
_CUSTOMER_ID_NATURAL_LANGUAGE_RE = re.compile(r"[\u4e00-\u9fff]|\s")
_CUSTOMER_PHONE_RE = re.compile(r"(?:\+?86)?1[3-9][0-9]{9}")
_SUBJECTIVE_PRODUCT_COMPARE_RE = re.compile(
    r"性价比|哪(?:个|款|种).{0,6}(?:更|最)?(?:好|合适|适合|划算|值得)|怎么选|选哪(?:个|款|种)",
)
_OBJECTIVE_PRODUCT_DIMENSION_RE = re.compile(
    r"保险责任|保障范围|保费|等待期|责任免除|免责|投保规则|投保年龄|缴费|交费|"
    r"现金价值|分红|领取|保险期间",
)

# RAG 类工具集合（harness-v11）：轮级合计预算只覆盖检索类工具，不拦数据/详情工具。
_RAG_TOOL_NAMES = RAG_TOOLS


# 仅保留正在执行的调用；Future 不进入会持久化的 session_state/dependencies。
# S1 每轮替换 budget，以账本对象身份隔离会话/轮次；执行协程持有 budget 强引用，
# 因而条目存活期间 id 不会复用。事件循环也参与键，避免跨 loop 等待 Future。
# 每个原调用在 finally 中移除条目，已完成结果仍只存入该轮的 JSON 缓存。
_INFLIGHT_TOOL_CALLS: dict[tuple[asyncio.AbstractEventLoop, int, str], asyncio.Future[str]] = {}


@dataclass
class _StateGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_STATE_GATES: dict[tuple[asyncio.AbstractEventLoop, int], _StateGate] = {}
_STATE_TOOLS = STATE_TOOLS


@dataclass
class _ActiveAdmissions:
    reads: int = 0
    stateful: bool = False


# 并行只开放给互不依赖的读取；与逐轮 budget 绑定，不写入持久化 session_state。
_ACTIVE_ADMISSIONS: dict[tuple[asyncio.AbstractEventLoop, int], _ActiveAdmissions] = {}


def _tool_failure_hint(code: ErrorCode) -> str:
    """给模型明确可用的失败短句，仍由后续模型调用生成正文。"""
    answer = TOOL_FAILURE_ANSWERS[code]
    guidance = (
        # 按实际错误码使用固定白名单话术，不把超时/拒绝解释为上游异常。
        f'若没有可用事实，answer_draft 写为“{answer}”，fact_refs 保持为空；'
        "不要推测结果、解释为没有数据或追加业务结论；本轮不要以相同参数重试同一工具。"
    )
    if code == ErrorCode.BUDGET:
        guidance += "不要继续调用工具；需要补充信息时请等待用户回复。"
    if code in {ErrorCode.UPSTREAM, ErrorCode.TIMEOUT}:
        # 整轮失败处置已在系统指令写一次；这里只给与本次失败直接相关的边界。
        guidance = "已有其他相关证据时照常回答；" + guidance
    return answer + guidance


@dataclass
class _ToolCall:
    state: dict | None
    config: HarnessConf
    envelope: ToolEnvelope | None = None
    source: str = 'tool'
    body_started: bool = False
    stop: str | None = None
    effective_tool: str | None = None
    effective_args: dict | None = None


_CALL: ContextVar[_ToolCall | None] = ContextVar('tool_control_call', default=None)


def tool_body(function):
    """Mark entry after Agno argument validation; internal ValidationErrors stay failures."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        call = _CALL.get()
        if call is not None:
            call.body_started = True
        return await function(*args, **kwargs)
    return wrapped


def _argument_error(exc: ValidationError, name: str) -> bool:
    call = _CALL.get()
    return call is not None and not call.body_started and exc.title.rsplit('.', 1)[-1] == name


def _budget_of(state: dict) -> dict:
    budget = state.setdefault('budget', {})
    budget.setdefault('model_tool_calls', 0)
    budget.setdefault('t0', time.time())
    budget.setdefault('tool_metrics', [])
    return budget


def _state(run_context: 'RunContext') -> dict:
    if run_context.session_state is None:
        raise RuntimeError('Tool hook session_state 未初始化')
    return run_context.session_state


def _pending_task_id(state: dict) -> str | None:
    return str(((state.get('entities') or {}).get('pending_clarify') or {}).get('task_id') or '') or None


def _safe_failure_result(result: str, envelope: ToolEnvelope | None = None) -> str:
    if envelope is None:
        if not isinstance(result, str) or not result.lstrip().startswith('{'):
            return result
        try:
            envelope = ToolEnvelope.model_validate_json(result)
        except ValueError:
            return result
    safe = _safe_envelope(envelope)
    return result if safe is envelope else safe.model_dump_json()


def _safe_envelope(envelope: ToolEnvelope) -> ToolEnvelope:
    if envelope.control is None and envelope.error is None:
        return envelope
    control = envelope.control
    if control is None and envelope.error is not None and envelope.error.code is ErrorCode.BUDGET:
        from app.core.tool_control import control_for
        control = control_for(ToolControlReason.BACKEND_BUDGET)
    if control is not None:
        hint = control_message(control, envelope.hint)
    elif envelope.error is not None:
        hint = _tool_failure_hint(envelope.error.code)
    else:
        return envelope
    return ToolEnvelope(ok=False, status=envelope.status, error=envelope.error, control=control, hint=hint)


async def boundary_hook(function_name: str, function_call, arguments: dict[str, Any],
                        run_context: 'RunContext | None' = None) -> str:
    state = _state(run_context) if run_context is not None else None
    call = _ToolCall(state, read_once('harness', harness_conf))
    token = _CALL.set(call)
    try:
        raw = await function_call(**arguments)
        if call.envelope is None:
            call.envelope = _parse_envelope(raw)
        effective = call.effective_tool or function_name
        canonical = TERMINAL_SEARCH_TOOLS.get(effective, effective)
        safe_envelope = _safe_envelope(call.envelope) if call.envelope is not None else None
        safe_raw = raw
        if safe_envelope is not None and safe_envelope is not call.envelope:
            safe_raw = safe_envelope.model_dump_json()
        visible = await ModelView.async_tool_result(canonical, safe_raw, call.effective_args if call.effective_args is not None else arguments,
                                       state, source=call.source,
                                       envelope=safe_envelope, config=call.config)
        if state is not None:
            record_business_tool_result(_budget_of(state), canonical, raw, envelope=call.envelope)
            if call.stop == 'terminal':
                results = records(state['budget'])
                index = next(i for i, item in enumerate(results) if item.tool == canonical and item.raw == raw)
                route = state.setdefault('route', {})
                route['agent_terminal_search'] = {'tool': canonical, 'result_index': index,
                    'trace_id': str(route.get('trace_id') or ''), 'turn_run_id': str(route.get('run_id') or '')}
        if call.stop:
            raise StopAgentRun('agent_terminal_search' if call.stop == 'terminal' else visible, agent_message=visible)
        return visible
    except (AgentRunException, RunCancelledException):
        raise
    except Exception as exc:
        if isinstance(exc, ValidationError) and _argument_error(exc, function_name):
            raw = (_budget_refused(_budget_of(state), argument_hint(exc), category=ToolControlReason.SELECTION_MISUSE.value, tool=function_name)
                   if state is not None else refusal_envelope(ToolControlReason.SELECTION_MISUSE, argument_hint(exc)))
            return ModelView.tool_result(function_name, raw, arguments, state, config=call.config)
        diagnostic('unexpected_tool_exception', stage='tool_boundary', tool=function_name, error=exc)
        logger.error('agent_tool_error_sanitized', extra={'observability': {
            'tool': function_name, 'outcome': 'failed', 'stage': 'boundary', 'category': type(exc).__name__}})
        code = ErrorCode.TIMEOUT if isinstance(exc, TIMEOUT_ERRORS) else ErrorCode.UPSTREAM
        raise RuntimeError(_tool_failure_hint(code)) from None
    finally:
        _CALL.reset(token)


def _budget_refused(budget: dict, hint: str, *, category: str, tool: str = '', **details: Any) -> str:
    budget['tool_calls_refused'] = budget.get('tool_calls_refused', 0) + 1
    count_refusal(budget, category)
    diagnostic(category, stage='tool_admission', tool=tool, **details)
    logger.warning('agent_tool_refused', extra={'observability': {'tool': tool, 'category': category}})
    mb = current_model_budget()
    if mb is not None and category in {'total_limit', 'time_budget', 'no_progress'}:
        stop_reason = 'no_progress' if category == 'no_progress' else 'tools' if category == 'total_limit' else 'deadline'
        mb.stop_tools(stop_reason, blocked=True)
    raw = refusal_envelope(ToolControlReason(category), hint)
    call = _CALL.get()
    if call is not None:
        call.envelope = ToolEnvelope.model_validate_json(raw)
    return raw


def _selection_hint(name: str, args: dict, state: dict) -> str | None:
    if name in TERMINAL_SEARCH_TOOLS:
        route = state.get('route') or {}
        decision = route.get('decision') or {}
        if (decision.get('action') != RouteAction.AGENT.value or decision.get('resolved_args') is not None
                or not str(route.get('user_text') or '').strip()):
            return SELECTION_MESSAGES['terminal_route']
        if (state.get('budget') or {}).get('model_tool_calls', 0):
            return SELECTION_MESSAGES['terminal_prior']
    return _selection_error(name, args, state)


async def turn_control_hook(run_context: 'RunContext', function_call, arguments: dict[str, Any],
                            function_name: str = '') -> str:
    name = function_name or str(getattr(function_call, '__name__', 'unknown_tool'))
    state = _state(run_context)
    budget = _budget_of(state)
    # Standalone callers use the same controller, without model projection.
    token = None
    if _CALL.get() is None:
        token = _CALL.set(_ToolCall(state, read_once('harness', harness_conf)))
    try:
        if name not in STATE_TOOLS:
            return await _controlled_call(run_context, name, function_call, arguments)
        key = (asyncio.get_running_loop(), id(budget))
        gate = _STATE_GATES.setdefault(key, _StateGate())
        gate.users += 1
        try:
            async with gate.lock:
                return await _controlled_call(run_context, name, function_call, arguments)
        finally:
            gate.users -= 1
            if not gate.users:
                _STATE_GATES.pop(key, None)
    finally:
        if token is not None:
            _CALL.reset(token)


async def _controlled_call(ctx: 'RunContext', name: str, function_call, args: dict, *, allow_redirect: bool = True) -> str:
    state = _state(ctx)
    budget = _budget_of(state)
    call = _CALL.get()
    assert call is not None
    config = call.config
    if name in {"policy_detail", "policy_search", "run_policy_field_lookup"}:
        from app.core.policy_fields import unavailable_field_message
        from app.conf.rules import rules_conf
        from app.core.field_dict import FieldDict
        from app.core.resolve import scan_fields
        field_config = rules_conf().fields
        requested = args.get("fields")
        if not requested and name != "policy_detail":
            requested, _, _ = scan_fields(str(args.get("query") or (state.get("route") or {}).get("user_text") or ""),
                FieldDict({**field_config.fields, **field_config.non_detail_fields}))
        message = unavailable_field_message(list(requested)) if requested else None
        if message:
            raw = make_envelope(EnvelopeStatus.EMPTY, hint=message)
            call.envelope = ToolEnvelope.model_validate_json(raw)
            from app.core.resolve import POLICY_NO_RE, normalize_policy_text
            subjects = {normalize_policy_text(str(args['polno']))} if args.get('polno') else {
                m.group() for m in POLICY_NO_RE.finditer(normalize_policy_text(str(args.get('query') or (state.get('route') or {}).get('user_text') or '')))}
            item = record(_budget_of(state), name, raw, envelope=call.envelope)
            item.subjects = frozenset(subjects)
            item.unavailable_fields = frozenset(requested or [])
            return raw
    key = (asyncio.get_running_loop(), id(budget))
    active = _ACTIVE_ADMISSIONS.get(key)
    # Resolve reusable results before charging execution quotas. State/selection
    # and explicit stop guards still apply before reading or awaiting the result.
    canonical = TERMINAL_SEARCH_TOOLS.get(name, name)
    if name in TERMINAL_SEARCH_TOOLS and not args:
        query = str((state.get("route") or {}).get("user_text") or "").strip()
        rpc_key = tool_rpc_key(canonical, {"query": query})
    else:
        rpc_key = tool_rpc_key(name, args)
    hit = cached(budget, rpc_key)
    flight_key = (*key, rpc_key)
    inflight = _INFLIGHT_TOOL_CALLS.get(flight_key)
    reused = _reuse_direct_evidence(name, args, state)
    mb = current_model_budget()
    # Clocks are read once outside the pure predicate.
    reuse = hit is not None or inflight is not None or reused is not None
    soft_reason = mb.soft_reason(include_calls=False, include_tools=not reuse) if mb else None
    if mb is not None and soft_reason:
        mb.stop_tools(soft_reason)
    deadline = config.deadline()
    refusal = admit(state, name, config,
        remaining_s=deadline.remaining('tool') if deadline is not None else None,
        reads=active.reads if active else 0, stateful=active.stateful if active else False,
        selection=lambda: _selection_hint(name, args, state),
        stop=mb.control.stop if mb else None, soft_reason=soft_reason,
        reuse=reuse)
    # Selection is the predicate's final stage; earlier refusals were not scanned.
    if allow_redirect and (refusal is None or refusal.reason is ToolControlReason.SELECTION_MISUSE):
        observe_selection(budget, 'guard', 'checked')
    if refusal is not None:
        if allow_redirect and refusal.reason is ToolControlReason.SELECTION_MISUSE:
            subtype = next((key for key, hint in SELECTION_MESSAGES.items() if hint == refusal.hint), 'other')
            redirect = resolve_redirect(subtype, args, str((state.get('route') or {}).get('user_text') or ''),
                known_customers=_known_customer_ids(state) if subtype == 'customer_search' else set())
            observe_selection(budget, subtype, 'eligible' if redirect else 'ineligible')
            if redirect is not None and subtype in config.features.selection_redirects:
                # Re-enter only admission/dedupe/accounting, never boundary or original
                # metrics. The target owns identity checks and raw business execution.
                from app.tools.bindings import execute_selection_target
                async def target_body(**kwargs):
                    return await execute_selection_target(ctx, redirect.tool, kwargs)
                async def measured_target(**kwargs):
                    return await metrics_hook(ctx, redirect.tool, target_body, kwargs,
                                              envelope_tools=frozenset({redirect.tool}))
                call.effective_tool, call.effective_args = redirect.tool, redirect.arguments
                observe_selection(budget, subtype, 'redirected')
                try:
                    raw = await _controlled_call(ctx, redirect.tool, measured_target, redirect.arguments,
                                                 allow_redirect=False)
                except asyncio.CancelledError:
                    observe_selection(budget, subtype, 'cancelled')
                    raise
                except Exception:
                    observe_selection(budget, subtype, 'exception')
                    raise
                outcome = ('target_refused' if call.envelope is not None and call.envelope.control is not None
                           else call.envelope.status.value.lower() if call.envelope is not None else 'unknown')
                observe_selection(budget, subtype, outcome)
                return raw
            observe_selection(budget, subtype, 'observe' if redirect else 'hint')
        if mb is not None and soft_reason and refusal.reason is ToolControlReason.TOTAL_LIMIT:
            mb.stop_tools(soft_reason, blocked=True)
        raw = _budget_refused(budget, refusal.hint, category=refusal.reason.value, tool=name)
        _stop_after_result(call, name, raw, _pending_task_id(state))
        return raw
    if reused is not None:
        _record_direct_reuse(state, name)
        direct = direct_record(budget)
        call.envelope = direct.envelope if direct is not None and direct.raw == reused else ToolEnvelope.model_validate_json(reused)
        return reused
    if hit is not None or inflight is not None:
        budget['deduplicated_tool_calls'] = budget.get('deduplicated_tool_calls', 0) + 1
        logger.info('agent_tool_deduplicated', extra={'observability': {
            'tool': name, 'category': 'cache' if hit is not None else 'inflight'}})
        if hit is not None:
            raw = hit.raw
            call.envelope = hit.envelope
        else:
            assert inflight is not None
            raw = await asyncio.shield(inflight)
            completed = cached(budget, rpc_key)
            call.envelope = completed.envelope if completed is not None else _parse_envelope(raw)
        return raw
    # Predicate, dedupe, quota and in-flight registration contain no await.
    budget['model_tool_calls'] += 1
    counts = budget.setdefault('tool_call_counts', {})
    counts[name] = counts.get(name, 0) + 1
    active = active or _ActiveAdmissions()
    _ACTIVE_ADMISSIONS[key] = active
    stateful = name in STATE_TOOLS
    if stateful:
        active.stateful = True
    else:
        active.reads += 1
    if name in TERMINAL_SEARCH_TOOLS:
        budget['terminal_search_started'] = name
        if mb is not None:
            mb.control.set(StopKind.TERMINAL, name)
    pending = _pending_task_id(state)
    inflight = asyncio.get_running_loop().create_future()
    _INFLIGHT_TOOL_CALLS[flight_key] = inflight
    try:
        raw = await function_call(**args)
        if call.envelope is None:
            call.envelope = _parse_envelope(raw)
        _accept_envelope(ctx, raw, call.envelope, name)
        _mark_pending(state, pending)
        _stop_after_result(call, name, raw, pending)
        if isinstance(raw, str):
            record(budget, TERMINAL_SEARCH_TOOLS.get(name, name), raw,
                   envelope=call.envelope, rpc_args_key=rpc_key)
        inflight.set_result(raw)
        return raw
    except ValidationError as exc:
        if not _argument_error(exc, name):
            inflight.set_exception(exc)
            inflight.exception()
            raise
        # Two free corrections per tool per turn; later misuse consumes the existing
        # attempt quota but still is not counted as a real body/RPC execution.
        corrections = budget.setdefault('argument_corrections', {})
        refunded = corrections.get(name, 0)
        if refunded < 2:
            corrections[name] = refunded + 1
            budget['model_tool_calls'] -= 1
            counts[name] -= 1
            if not counts[name]:
                counts.pop(name)
        raw = _budget_refused(budget, argument_hint(exc), category=ToolControlReason.SELECTION_MISUSE.value, tool=name)
        inflight.set_result(raw)
        return raw
    except asyncio.CancelledError:
        inflight.cancel()
        raise
    except BaseException as exc:
        inflight.set_exception(exc)
        inflight.exception()
        raise
    finally:
        _mark_pending(state, pending)
        if name in TERMINAL_SEARCH_TOOLS and call.stop != 'terminal':
            budget.pop('terminal_search_started', None)
            if mb is not None:
                mb.control.release_terminal()
        _INFLIGHT_TOOL_CALLS.pop(flight_key, None)
        if stateful:
            active.stateful = False
        else:
            active.reads -= 1
        if not active.reads and not active.stateful:
            _ACTIVE_ADMISSIONS.pop(key, None)


def _parse_envelope(raw: str, *, required: bool = False) -> ToolEnvelope | None:
    if not required and (not isinstance(raw, str) or '"facts"' not in raw):
        return None
    return ToolEnvelope.model_validate_json(raw)


def _mark_pending(state: dict, before: str | None) -> None:
    after = _pending_task_id(state)
    if after != before:
        budget = _budget_of(state)
        budget['turn_clarify_opened'] = True
        budget['turn_clarify_task_id'] = after


def _accept_envelope(ctx: 'RunContext', raw: str, envelope: ToolEnvelope | None, name: str) -> None:
    if envelope is None:
        return
    _pool_envelope(ctx, raw, tool=name, envelope=envelope)


def _stop_after_result(call: _ToolCall, name: str, raw: str, pending_before: str | None) -> None:
    state, envelope = call.state, call.envelope
    if state is None or envelope is None:
        return
    budget, route = _budget_of(state), state.setdefault('route', {})
    mb = current_model_budget()
    pending = _pending_task_id(state)
    matching = [t for t in (state.get('ledger') or {}).get('tasks', [])
                if pending is not None and t.get('task_id') == pending
                and t.get('role') == 'SUSPENDED' and t.get('expected_event')]
    current_suspension = bool(route.get('run_id') and matching and budget.get('turn_clarify_opened')
                              and budget.get('turn_clarify_task_id') == pending)
    owned = bool(route.get('run_id') and matching and (
        any(t.get('run_id') == route['run_id'] for t in matching) if name == 'ask_clarify' else current_suspension))
    exhausted = envelope.control is not None and envelope.control.reason is ToolControlReason.OPEN_CLARIFY_LIMIT
    # A refused ask_clarify must also stop on an owned child-workflow suspension.
    waiting = (envelope.control is not None and envelope.control.reason is ToolControlReason.CLARIFY_OPENED
               and current_suspension)
    if exhausted or waiting or (name in STATE_TOOLS and envelope.status is EnvelopeStatus.NEEDS_CLARIFICATION
                     and pending != pending_before and owned):
        route['agent_terminal_clarify'] = {'trace_id': str(route.get('trace_id') or ''),
            'turn_run_id': str(route.get('run_id') or ''),
            'result': make_envelope(EnvelopeStatus.NEEDS_CLARIFICATION) if waiting else _safe_failure_result(raw, envelope)}
        call.stop = 'clarify'
        if mb is not None:
            mb.control.set(StopKind.WAIT_USER, 'clarify')
    elif envelope.error is not None and envelope.error.code is ErrorCode.OVERLOADED:
        call.stop = 'capacity'
        if mb is not None:
            mb.control.set(StopKind.CAPACITY, 'tool_capacity')
    elif (name in TERMINAL_SEARCH_TOOLS and envelope.status in {EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL}
          and bool((envelope.data or {}).get('records') or envelope.facts)):
        call.source, call.stop = 'terminal', 'terminal'
        logger.info('agent_terminal_search', extra={'observability': {'tool': name, 'outcome': 'completed', 'stage': 'terminal'}})


# Compatibility names for standalone callers; production uses exactly three hooks.
safe_error_hook = boundary_hook
budget_hook = turn_control_hook
async def selection_guard_hook(run_context: 'RunContext', function_name: str, function_call, arguments: dict) -> str:
    return await turn_control_hook(run_context, function_call, arguments, function_name)


async def capacity_hook(run_context: 'RunContext', function_name: str, function_call, arguments: dict) -> str:
    async def controlled(**args):
        return await turn_control_hook(run_context, function_call, args, function_name)
    return await boundary_hook(function_name, controlled, arguments, run_context)


terminal_search_hook = capacity_hook
clarification_stop_hook = capacity_hook


async def dedupe_hook(run_context: 'RunContext', function_name: str, function_call, arguments: dict) -> str:
    # Old standalone dedupe tests keep the same coalescing semantics without projection.
    return await turn_control_hook(run_context, function_call, arguments, function_name)


def _reuse_direct_evidence(
    function_name: str,
    arguments: dict[str, Any],
    session_state: dict,
) -> str | None:
    """Read-only Direct reuse probe; caller records reuse only after admission."""
    route = session_state.get("route") or {}
    direct_context = route.get("direct_agent_context") or {}
    decision = route.get("decision") or {}
    if (
        route.get("escalate")
        and decision.get("action") in {RouteAction.DIRECT_RAG, RouteAction.DIRECT_TOOL}
        and direct_context.get("tool") == function_name
        and (
            direct_context.get("error_code") in {"UPSTREAM", "TIMEOUT"}
            or (isinstance(direct_context.get("query"), str)
                and tool_rpc_key(function_name, {"query": direct_context["query"].strip()})
                == tool_rpc_key(function_name, {
                    **arguments,
                    "query": arguments.get("query", "").strip()
                    if isinstance(arguments.get("query"), str) else arguments.get("query"),
                }))
        )
    ):
        gaps = [str(gap) for gap in direct_context.get("gaps") or [] if str(gap).strip()]
        status = EnvelopeStatus(str(direct_context.get("status") or "EMPTY"))
        if status is EnvelopeStatus.PARTIAL and not gaps:
            gaps = ["本轮直达查询仍有信息未确认"]
        return make_envelope(
            status=status,
            error=(ToolError(code=ErrorCode(direct_context["error_code"]), retryable=False)
                   if status in {EnvelopeStatus.ERROR, EnvelopeStatus.TIMEOUT} else None),
            gaps=gaps or None,
            hint=(
                "已复用本轮直达查询失败状态，未再次请求同一后端；本轮不要再调用该来源，"
                "有其他证据则回答，否则说明无法确认"
                if direct_context.get("error_code") in {"UPSTREAM", "TIMEOUT"} else
                "已复用本轮直达查询结果，未再次请求同一后端；"
                "请使用本轮输入中[本轮直达已取证]的内容并披露缺口；引用遵循当前引用模式"
            ),
        )
    if function_name != "policy_detail":
        return None
    target = route.get("direct_evidence_target") or {}
    if (
        not route.get("escalate")
        or decision.get("action") != RouteAction.DIRECT_TOOL
        or target.get("tool") != function_name
    ):
        return None
    polno = matching_text(str(arguments.get("polno") or "")).strip().upper()
    refs = {str(value).strip().upper() for value in target.get("policy_refs") or []}
    if not polno or polno not in refs:
        return None
    business_result = direct_record(session_state.get("budget") or {})
    if business_result is not None and business_result.tool == function_name:
        return business_result.raw
    trace_id = str(route.get("trace_id") or "")
    bucket = (((session_state.get("evidence") or {}).get("by_turn") or {}).get(trace_id) or {})
    fact_ids = set(target.get("fact_ids") or [])
    available_fact_ids = {
        str(item.get("fact_id") or "") for item in bucket.get("facts") or []
    }
    if not fact_ids.intersection(available_fact_ids):
        return None
    gaps = [str(gap) for gap in target.get("gaps") or [] if str(gap).strip()]
    if not gaps:
        gaps = ["当前目标仍有字段未确认"]
    return make_envelope(
        status=EnvelopeStatus.PARTIAL,
        gaps=gaps,
        hint=(
            "已复用本轮 Direct 权威证据，未再次请求保单详情后端；"
            "请使用本轮输入中已提供的业务数据，并披露未确认字段"
        ),
    )


def _record_direct_reuse(session_state: dict, function_name: str) -> None:
    """记录 Direct 复用命中；不增加后端 RPC。"""
    budget = _budget_of(session_state)
    budget["reused_direct_evidence_tool_calls"] = (
        budget.get("reused_direct_evidence_tool_calls", 0) + 1
    )
    metrics: list[dict] = budget["tool_metrics"]
    metrics.append({
        "tool": function_name,
        "latency_ms": 0.0,
        "ok": True,
        "reused_direct_evidence": True,
    })
    del metrics[:-_METRICS_KEEP]


def _selection_error(function_name: str, arguments: dict[str, Any], session_state: dict) -> str | None:
    if function_name == "product_knowledge":
        user_text = str((session_state.get("route") or {}).get("user_text") or "")
        if (
            _SUBJECTIVE_PRODUCT_COMPARE_RE.search(user_text)
            and not _OBJECTIVE_PRODUCT_DIMENSION_RE.search(user_text)
        ):
            return SELECTION_MESSAGES["product_compare"]
    elif function_name == "policy_detail":
        polno = matching_text(str(arguments.get("polno") or "")).strip().upper()
        from app.core.resolve import valid_policy_tail
        if _POLICY_REF_RE.fullmatch(polno) is None or polno.isdigit() and not valid_policy_tail(polno):
            return SELECTION_MESSAGES["policy_shape"]
        known = _known_policy_refs(session_state) | set((session_state.get('route') or {}).get('visible_policy_refs') or []) | set((session_state.get('route') or {}).get('confirmed_user_policy_refs') or [])
        user_text = matching_text(str((session_state.get("route") or {}).get("user_text") or "")).upper()
        if polno.upper() not in known and re.search(rf"(?<![0-9A-Za-z]){re.escape(polno)}(?![0-9A-Za-z])", user_text, re.IGNORECASE) is None:
            return SELECTION_MESSAGES["policy_unconfirmed"]
    elif function_name == "policy_search":
        query = str(arguments.get("query") or "").strip()
        from app.core.resolve import POLICY_NO_RE, normalize_policy_text
        if POLICY_NO_RE.search(normalize_policy_text(query)):
            return SELECTION_MESSAGES["policy_search"]
    elif function_name == "customer_detail":
        single, multiple = arguments.get("customer_id"), arguments.get("customer_ids")
        invalid = any(key in arguments for key in ("client_name", "client_mobile"))
        invalid |= multiple is not None and (not isinstance(multiple, list) or len(multiple) > 100)
        ids = ([single] if single is not None else []) + (multiple if isinstance(multiple, list) else [])
        invalid |= not ids or any(
            not isinstance(value, str) or not value or len(value) > 64
            or _CUSTOMER_ID_NATURAL_LANGUAGE_RE.search(value) is not None
            or _CUSTOMER_PHONE_RE.fullmatch(matching_text(value).replace("-", "")) is not None
            for value in ids
        )
        if invalid or any(value not in _known_customer_ids(session_state) for value in ids):
            return SELECTION_MESSAGES["customer_shape"]
    elif function_name == "customer_search":
        query = str(arguments.get("query") or "").strip()
        if query and query in _known_customer_ids(session_state):
            return SELECTION_MESSAGES["customer_search"]
    return None


def _known_customer_ids(session_state: dict) -> set[str]:
    known = known_subjects(session_state, "customer")
    from app.core.query_scope import explicit_customer_ids
    known.update(explicit_customer_ids(str((session_state.get('route') or {}).get('user_text') or '')))
    active = str((session_state.get("entities") or {}).get("active_customer") or "").strip()
    if active:
        known.add(active)
    for bucket in ((session_state.get("evidence") or {}).get("by_turn") or {}).values():
        if bucket.get("confirmed", True) is not True:
            continue
        for fact in bucket.get("facts") or []:
            if fact.get("subject_type") == "customer" and fact.get("predicate") in {
                "customer_brief", "customer_id", "customer_search_record",
            }:
                subject_id = str(fact.get("subject_id") or "").strip()
                if subject_id:
                    known.add(subject_id)
    return known


def _known_policy_refs(session_state: dict) -> set[str]:
    entities = session_state.get("entities") or {}
    known = {
        str(value).strip().upper()
        for value in [entities.get("focused_policy"), *(entities.get("last_policy_set") or [])]
        if str(value or "").strip()
    }
    resolved = ((session_state.get("route") or {}).get("decision") or {}).get("resolved_args") or {}
    selected = str(resolved.get("selected_option") or "")
    if resolved.get("resume_task_id") and _POLICY_REF_RE.fullmatch(selected):
        known.add(selected.upper())
    known.update(value.upper() for value in known_subjects(session_state, "policy"))
    known.update((session_state.get("route") or {}).get("agent_policy_refs") or [])
    route = session_state.get('route') or {}
    for bucket in ((session_state.get("evidence") or {}).get("by_turn") or {}).values():
        if bucket.get("confirmed", True) is not True:
            continue
        for fact in bucket.get("facts") or []:
            if fact.get("subject_type") == "policy":
                subject_id = str(fact.get("subject_id") or "").strip().upper()
                if subject_id:
                    known.add(subject_id)
    for task in (session_state.get("ledger") or {}).get("tasks") or []:
        expected = task.get("expected_event") or {}
        known.update(
            str(option).strip().upper()
            for option in expected.get("options") or []
            if _POLICY_REF_RE.fullmatch(str(option).strip())
        )
    if 'visible_policy_refs' in route:
        current = ((session_state.get('evidence') or {}).get('by_turn') or {}).get(route.get('trace_id')) or {}
        current_ids = {str(f.get('subject_id') or '').upper() for f in current.get('facts') or []
                       if f.get('subject_type') == 'policy'}
        known &= set(route.get('visible_policy_refs') or []) | current_ids | set(route.get('agent_policy_refs') or []) | {selected.upper()}
    return known


async def metrics_hook(
    run_context: "RunContext",
    function_name: str,
    function_call,
    arguments: dict[str, Any],
    *,
    envelope_tools: frozenset[str] = frozenset(),
) -> str:
    """真实执行计量，并将一次解析的信封交给外层控制器入池。

    独立复用时仍执行入池；Direct/RPC 继续使用各自原有单点。
    """
    budget = _budget_of(_state(run_context))
    started = time.perf_counter()
    budget["tool_calls_real"] = budget.get("tool_calls_real", 0) + 1
    outcome, stage = "failed", "call"
    with observe_backend_calls() as rpc:
        try:
            result = await function_call(**arguments)
            stage = "evidence"
            call = _CALL.get()
            if call is not None:
                call.envelope = _parse_envelope(result, required=function_name in envelope_tools or function_name in STATE_TOOLS)
            else:
                _pool_envelope(run_context, result, tool=function_name,
                               require_envelope=function_name in envelope_tools or function_name in STATE_TOOLS)
            # completed 表示正常返回且通过适用的信封校验，不代表业务有数据。
            # 合法 EMPTY/ERROR 等控制信封仍沿用成功完成调用的计量口径。
            outcome, stage = "completed", "completed"
            return result
        except ValidationError as exc:
            if stage == "call" and not rpc.seen and _argument_error(exc, function_name):
                budget["tool_calls_real"] -= 1
                outcome, stage = "rejected", "validation"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TIMEOUT_ERRORS:
            outcome = "timeout"
            raise
        finally:
            if outcome != "rejected":
                _append_metric(budget, function_name, started, ok=outcome == "completed")
            # RPC 调用由 tool_backend_call 单点留痕；使用当前作用域标记，
            # 避免同轮并行工具的共享 RPC 计数误压本地日志。取消后不再 await。
            if not rpc.seen or (rpc.returned and outcome != "completed"):
                event = "agent_tool_processing_failed" if rpc.seen else "agent_tool_call"
                logger.info(event, extra={"observability": {
                    "tool": function_name,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "outcome": outcome,
                    "stage": stage,
                }})


def _append_metric(
    budget: dict,
    function_name: str,
    started: float,
    *,
    ok: bool,
) -> None:
    metrics: list[dict] = budget["tool_metrics"]
    metrics.append({
        "tool": function_name,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "ok": ok,
    })
    del metrics[:-_METRICS_KEEP]


def _pool_envelope(
    run_context: "RunContext",
    result: str,
    *,
    require_envelope: bool = False,
    tool: str | None = None, envelope: ToolEnvelope | None = None,
) -> None:
    """成功/部分成功的工具信封按轮入池(失败信封无 facts 可入;坏信封 fail 显式)。"""
    # 生产注册工具必须履行信封契约；未注册的局部函数只参与 hook 单元测试/复用。
    if envelope is None and not require_envelope and (not isinstance(result, str) or '"facts"' not in result):
        return
    try:
        envelope = envelope or ToolEnvelope.model_validate_json(result)
    except ValueError as exc:
        raise ValueError(f"工具返回值不是合法 ToolEnvelope:{exc}(fail 显式)") from exc
    if envelope.status in (EnvelopeStatus.SUCCESS, EnvelopeStatus.PARTIAL):
        deps = getattr(run_context, 'dependencies', None) or {}
        state = _state(run_context)
        route = state.get("route") or {}
        trace_id = str(deps.get("trace_id") or route.get("trace_id") or "")
        if trace_id:
            merge_into_pool(state, trace_id, envelope.facts, envelope.evidence)
            from app.core.excerpt_delivery import record_excerpt_disclosures
            record_excerpt_disclosures(state, envelope, trace_id)
        remember_tool_entities(state, tool, envelope, policy_limit=read_once("gate", gate_conf).entity_policy_limit)
