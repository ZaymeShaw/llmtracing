"""S2 Fast Capability Gate(方案 v13 §4.2 · D2 判定检查插槽,纯函数族——不是引擎)。

判定 = 规则 × 词典 × 状态查表,0 生成式 LLM(P1/T2 铁律:Gate 零后端调用);
检查项在 conf/gate/gate_{ENV_TYPE}_args.yaml 有序登记,可增删可禁用——新增检查 = 一个纯函数 + conf 登记。
放行判据「完全解析或放弃」:残文非空即不放行(§4.6),失败方向安全(漏放 = 多走一次 Agent)。
RouteDecision 双通道留痕(session_state 主 + StepOutput.content 副)由装配层承载(§3.2)。
"""

from __future__ import annotations
from app.core.identifiers import matching_text

import re
import logging
from collections.abc import Callable
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.conf.capabilities import CapabilityAnchor, capabilities_conf
from app.conf.capabilities import CapabilityPolicies
from app.conf.runtime import read_once
from app.core.clarify import clarification_is_current, visible_clarification_options
from app.core.entity_memory import fresh_entities
from app.core.field_dict import FieldDict
from app.core.resolve import (
    event_answer_residual,
    is_customer_cohort,
    is_fully_resolved,
    match_event_answer,
    resolve_continuation,
    resolve_direct_query,
    rewrite_policy_workflow_query,
    scan_fields,
)
from app.core.session_health import session_is_healthy
from app.orchestration.semantic_direct import SemanticDirectConf
from app.schemas.ledger import TaskLedger, TaskRole
from app.schemas.route import RouteAction, RouteCandidate, RouteDecision

CheckImpl: TypeAlias = Callable[[str, dict, "GateConf", FieldDict], RouteDecision | None]

# 配置基础顺序；产品名称问题在裸字段续接前优先尝试 G-4b。
_CANONICAL_CHECK_ORDER = ("G-0", "G-1", "G-2", "G-3", "G-4a", "G-4b")
logger = logging.getLogger(__name__)


class CheckRegistration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    enabled: bool


class RiskLevel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    definition: str
    examples: list[str] = []
    constraints: dict[str, str | bool] = {}


class GateConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    entity_policy_limit: int = Field(default=100, ge=1, le=1000, strict=True)
    entity_max_age_turns: int = Field(default=2, ge=0, strict=True)
    checks: list[CheckRegistration]
    policy_no_pattern: str
    policy_tail_pattern: str = ""  # 广义保单号(裸尾号,Owner 2026-08-30);空串 = 不启用
    risk_levels: dict[str, RiskLevel]
    # S2.5 语义直达判定(独立分发原语,非检查表成员):检查表保持 0 后端调用,
    # 本段承载判定接口连接与启停;机制见 orchestration/semantic_direct。
    semantic_direct: SemanticDirectConf

    @model_validator(mode="after")
    def validate_check_registrations(self) -> "GateConf":
        """占位可登记但不可误启用；编号必须遵守实际执行顺序。"""
        check_ids = [check.id for check in self.checks]
        duplicates = sorted({check_id for check_id in check_ids if check_ids.count(check_id) > 1})
        if duplicates:
            raise ValueError(f"Gate checks 不得重复登记:{duplicates}")
        unknown = sorted(set(check_ids) - set(_CANONICAL_CHECK_ORDER))
        if unknown:
            raise ValueError(f"Gate 检查尚未登记规范顺序:{unknown}")
        expected_order = [check_id for check_id in _CANONICAL_CHECK_ORDER if check_id in check_ids]
        if check_ids != expected_order:
            raise ValueError(f"Gate checks 必须按规范顺序登记:{expected_order}")
        missing_impl = sorted(check.id for check in self.checks if check.enabled and check.id not in _CHECK_IMPLS)
        if missing_impl:
            raise ValueError(f"Gate 检查已启用但实现未注册:{missing_impl}")
        for pattern in (self.policy_no_pattern, self.policy_tail_pattern):
            if pattern:
                try:
                    compiled = re.compile(pattern)
                except (re.error, OverflowError):
                    raise ValueError("Gate 保单号正则非法") from None
                if compiled.match("") is not None:
                    raise ValueError("Gate 保单号正则不得匹配空串")
        if not self.policy_no_pattern:
            raise ValueError("Gate 显式保单号正则不得为空")
        return self

    def enabled_checks(self) -> list[CheckRegistration]:
        return [c for c in self.checks if c.enabled]


def run_gate(text: str, session_state: dict, cfg: GateConf, field_dict: FieldDict) -> RouteDecision:
    """按 conf 基础顺序执行；产品实体锚点先于裸字段续接。首个决策返回，放弃继续。

    session_state 只读注入(G-2 查台账 / G-3 读实体记忆)——Gate 零后端调用(T2 无例外)。
    """
    from app.conf.rules import rules_conf
    special = rules_conf().fields.non_detail_fields
    if special:
        field_dict = FieldDict({**{k: list(v) for k, v in field_dict._fields.items()}, **special})
    raw_text = text
    text = matching_text(text)
    checks = cfg.enabled_checks()
    attempted: set[str] = set()
    for check in checks:
        if check.id == "G-3" and any(item.id == "G-4b" for item in checks):
            cont = resolve_continuation(
                text, field_dict, fresh_entities(session_state, cfg.entity_max_age_turns, cfg.entity_policy_limit),
                cfg.policy_no_pattern, cfg.policy_tail_pattern or None,
            )
            if cont is not None and cont.ref_kind == "bare_field" and cont.residual:
                from app.conf import entity_dict

                if entity_dict().mentions_product(cont.residual):
                    attempted.add("G-4b")
                    decision = _g4b_policy_workflow(text, session_state, cfg, field_dict)
                    if decision is not None:
                        return _restore_query(_route_field_contract(decision, text, special), raw_text)
        if check.id in attempted:
            continue
        impl = _CHECK_IMPLS.get(check.id)
        if impl is not None and (decision := impl(text, session_state, cfg, field_dict)) is not None:
            return _restore_query(_route_field_contract(decision, text, special), raw_text)
    return RouteDecision(
        action=RouteAction.AGENT,
        reason=f"已启用检查 {[c.id for c in cfg.enabled_checks()]} 均未命中(已禁用检查不参与)",
    )


def _restore_query(decision: RouteDecision, raw_text: str) -> RouteDecision:
    args = decision.resolved_args
    if args and 'query' in args and decision.matched_check != 'G-2':
        return decision.model_copy(update={'resolved_args': {**args, 'query': raw_text}})
    return decision


def _route_field_contract(decision: RouteDecision, text: str, special: dict[str, list[str]]) -> RouteDecision:
    args = decision.resolved_args or {}
    fields = args.get("fields")
    if not isinstance(fields, list) or not set(fields) & special.keys():
        return decision
    clauses = {"waiting_period", "exclusion_clause"}
    supported = [code for code in fields if code not in special]
    unavailable = [special[code][0] for code in fields if code in special and code not in clauses]
    message = f"保单详情不包含{'、'.join(unavailable)}，请通过保全或柜面渠道查询核实。"
    if not supported and not set(fields) & clauses:
        return decision.model_copy(update={"action": RouteAction.DIRECT_TOOL, "capability_id": None,
            "resolved_args": {**args, "tool": "unavailable_policy_fields", "message": message}})
    if decision.action is not RouteAction.DIRECT_TOOL:
        focus = list(dict.fromkeys([*supported, *(['pol_product_name'] if set(fields) & clauses else [])]))
        return decision.model_copy(update={"resolved_args": {**args, "fields": focus,
            "clause_fields": sorted(set(fields) & clauses), "unavailable_fields": [message] if unavailable else [],
            "requested_unavailable_codes": [code for code in fields if code in special and code not in clauses]}})
    if set(fields) & clauses:
        # A policy reference is not a product name. Resolve the product through
        # its authoritative detail first; mixed queries also prefetch requested facts.
        return decision.model_copy(update={"resolved_args": {**args,
            "fields": list(dict.fromkeys([*supported, "pol_product_name"])),
            "clause_fields": sorted(set(fields) & clauses), "query": text,
            "unavailable_fields": [message] if unavailable else [],
            "requested_unavailable_codes": [code for code in fields if code in special and code not in clauses]},
            "reason": "先核实保单产品及可查字段，再按产品查条款"})
    return decision.model_copy(update={"resolved_args":
        {**args, "fields": supported, "unavailable_fields": [message],
         "requested_unavailable_codes": [code for code in fields if code in special and code not in clauses]} if supported else
        {"tool": "unavailable_policy_fields", "message": message}})


def _abstain(check: str, reason: str) -> None:
    # Side-channel only: Gate does not mutate session state or log user text/IDs.
    logger.info("gate_check_abstained", extra={"observability": {"check": check, "reason": reason}})


# 检查实现注册表(D2:新增检查 = 一个纯函数 + 本表注册 + conf 登记；未注册的
# disabled 项允许作为未来占位，若误设 enabled 则由 GateConf 装载期 fail-fast)。
_CHECK_IMPLS: dict[str, "CheckImpl"] = {}


def _register(check_id: str):
    def deco(fn):
        _CHECK_IMPLS[check_id] = fn
        return fn

    return deco


@_register("G-2")
def _g2_suspended_event(text: str, session_state: dict, cfg: GateConf, field_dict: FieldDict) -> RouteDecision | None:
    """G-2 挂起流程应答(§7.2/§09):expected_event 查表 × 精确/包含两级匹配。

    纯读 Gate(T2 无例外):台账/实体记忆只读——resume(台账写入)与 pending 清理由
    Direct 执行器或 Agent bridge 消费 resolved_args["resume_task_id"] 完成(台账唯一写入点纪律);
    应答无匹配 → None(用户无视换话题,会话式——常规流转 G-3/G-4a)。
    Direct 字段澄清允许应答自带字段词(「第二张的现金价值」),否则取
    pending_clarify 原始字段(纯选择应答「2」;task_id 绑定防串挂)；Workflow @pause
    只接受纯事件，事件+字段目标按 §7.2③ 不提交。仅接受 pending 指向且处于紧邻应答轮的任务，孤儿和过期任务不参与。
    """
    entities = session_state.get("entities") or {}
    pending = entities.get("pending_clarify") or {}
    raw_ledger = session_state.get("ledger") or {}
    if not raw_ledger.get("tasks"):
        return None
    ledger = TaskLedger.model_validate(raw_ledger)
    direct_matches: list[tuple[str, str, list[str], str]] = []
    workflow_matches: list[tuple[str, str, str, str]] = []
    agent_matches: list[tuple[str, str, str]] = []
    for entry in ledger.tasks:
        if (entry.role is not TaskRole.SUSPENDED or entry.expected_event is None
                or not clarification_is_current(session_state, entry.task_id, answering=True)):
            continue
        request = pending.get('customer_number_request')
        if isinstance(request, dict) and re.fullmatch(r'(?:客户(?:号|编号)[:：]?)?[A-Za-z][A-Za-z0-9_-]{2,63}', text.strip()):
            from app.core.resolve import POLICY_NO_RE
            from app.core.query_scope import explicit_customer_ids
            number = next(iter(explicit_customer_ids(text)), text.strip())
            if not POLICY_NO_RE.fullmatch(number):
                locator = '客户号:' + number
                query = str(request.get('query') or '').replace(str(request.get('customer_query') or ''), locator, 1)
                return RouteDecision(action=RouteAction.WORKFLOW_START, matched_check='G-2',
                    capability_id=str(request['capability']), resolved_args={**request.get('args', {}),
                        'query': query, 'customer_query': locator, 'replaced_locator_task': entry.task_id},
                    reason='以本轮客户号重新定位，保留上一轮字段意图')
        internal_options = list(entry.expected_event.options)
        display_options = visible_clarification_options(internal_options, pending, entry.task_id)
        matched = match_event_answer(text, display_options)
        selected: str | None
        match_options = display_options
        if matched is not None:
            selected = internal_options[display_options.index(matched)]
        else:
            selected = match_event_answer(text, internal_options)
            matched = selected
            match_options = internal_options
        if selected is None or matched is None:
            continue
        codes, _, residual = scan_fields(text, field_dict)
        residual = event_answer_residual(residual, matched, match_options)
        if not is_fully_resolved(residual):
            _abstain("G-2", "event_residual")
            return None
        fields = list(codes) or (list(pending["fields"]) if pending.get("task_id") == entry.task_id else [])
        if entry.capability == "policy_detail" and fields:
            direct_matches.append((entry.task_id, selected, fields, entry.expected_event.type))
        elif entry.capability == 'policy_search' and not codes and isinstance(pending.get('policy_search_query'), str):
            return RouteDecision(action=RouteAction.DIRECT_TOOL, matched_check='G-2',
                resolved_args={'tool': 'policy_search', 'query': pending['policy_search_query'],
                    'customer_id': selected, 'customer_name': str((pending.get('customer_names') or {}).get(selected) or ''),
                    'resume_task_id': entry.task_id}, reason='复用客户选择卡的已确认标识查询保单清单')
        elif entry.workflow_session_id and entry.run_id:
            if codes:
                return RouteDecision(
                    action=RouteAction.AGENT,
                    matched_check="G-2",
                    reason=(f"Workflow 挂起事件 {selected!r} 同时携带字段目标 {list(codes)}——按 §7.2③ 不提交事件"),
                )
            workflow_matches.append((entry.task_id, selected, entry.expected_event.type, entry.capability))
        elif entry.capability == "unified_agent":
            if codes and entry.expected_event.type == 'PRODUCT_SELECTED' and matching_text(selected) in matching_text(text):
                # Explicit product plus a new field is a fresh anchored query.
                return None
            agent_matches.append((entry.task_id, selected, entry.expected_event.type))

    if direct_matches:
        task_id, selected, fields, event_type = direct_matches[0]
        return RouteDecision(
            action=RouteAction.DIRECT_TOOL,
            matched_check="G-2",
            resolved_args={"policy_no": selected, "fields": fields, "resume_task_id": task_id},
            reason=f"挂起应答命中 {selected!r} → 续 {task_id}({event_type})× 字段 {fields}",
        )
    if workflow_matches:
        task_id, selected, event_type, capability = workflow_matches[0]
        return RouteDecision(
            action=RouteAction.WORKFLOW_CONTINUE,
            matched_check="G-2",
            capability_id=capability,
            resolved_args={
                "resume_task_id": task_id,
                "selected_option": selected,
                "event_type": event_type,
            },
            reason=f"Workflow 挂起应答命中 {selected!r} → 续 {task_id}({event_type})",
        )
    if agent_matches:
        task_id, selected, event_type = agent_matches[0]
        return RouteDecision(
            action=RouteAction.AGENT,
            matched_check="G-2",
            resolved_args={
                "resume_task_id": task_id,
                "selected_option": selected,
                "event_type": event_type,
            },
            reason=f"Agent 挂起应答命中 {selected!r} → 交 Unified Agent 续 {task_id}({event_type})",
        )
    return None


@_register("G-3")
def _g3_continuation(text: str, session_state: dict, cfg: GateConf, field_dict: FieldDict) -> RouteDecision | None:
    """G-3 会话续接(§4.6):实体记忆已有 ∧ 窄域引用 × 字段词典完全解析 → DIRECT_TOOL。

    白名单:序数 + 已确认实体显式名 + 裸字段;显式名仅在 last_policy_set 内唯一命中，
    歧义/新名称安全回退 Agent；指示词不做。0 LLM 的三个前提:第一问建状态 /
    窄域引用集 / 完全解析或放弃。
    """
    cont = resolve_continuation(
        text,
        field_dict,
        fresh_entities(session_state, cfg.entity_max_age_turns, cfg.entity_policy_limit),
        cfg.policy_no_pattern,
        cfg.policy_tail_pattern or None,
    )
    if cont is None or not cont.field_codes:
        return None
    if not is_fully_resolved(cont.residual):
        _abstain("G-3", "continuation_residual")
        return None
    return RouteDecision(
        action=RouteAction.DIRECT_TOOL,
        matched_check="G-3",
        resolved_args={"policy_no": cont.policy_no, "fields": list(cont.field_codes)},
        reason=f"续接 {cont.ref_kind} × 实体记忆 {cont.policy_no} × 字段词典 {list(cont.field_codes)}",
    )


@_register("G-4a")
def _g4a_hard_rule(text: str, session_state: dict, cfg: GateConf, field_dict: FieldDict) -> RouteDecision | None:
    """G-4a 硬规则(§4.6):显式保单号(P 式或广义尾号)∧ 字段词典命中 ∧ 完全解析 → DIRECT_TOOL。

    HIGH 风险能力的唯一直达窄门(§4.7):实参完全确定 + 三重纪律由下游保证
    (金额必带 as_of / 永不缓存 / 出口数值一致核对)。尾号多候选由工具后端判定 →
    AMBIGUOUS 结构化候选 → Direct 执行器澄清卡(§4.8③,6-b 接线)——放行语义仍是
    「实参完全确定」:尾号即用户显式给出的定位输入,歧义交用户消解,不交模型猜。
    """
    resolved = resolve_direct_query(
        text,
        field_dict,
        policy_pattern=cfg.policy_no_pattern,
        tail_pattern=cfg.policy_tail_pattern or None,
    )
    if resolved is None:
        return None
    if not resolved.field_codes:
        return None
    if not is_fully_resolved(resolved.residual):
        _abstain("G-4a", "explicit_policy_residual")
        return None
    return RouteDecision(
        action=RouteAction.DIRECT_TOOL,
        matched_check="G-4a",
        resolved_args={"policy_no": resolved.policy_no, "fields": list(resolved.field_codes)},
        reason=f"显式保单号 {resolved.policy_no} × 字段词典 {list(resolved.field_codes)} × 完全解析",
    )


@_register("G-4b")
def _g4b_policy_workflow(
    text: str,
    session_state: dict,
    cfg: GateConf,
    field_dict: FieldDict,
) -> RouteDecision | None:
    """完整保单清单请求直接搜索；不明确/复合意图仍交 Agent。"""
    rewritten = rewrite_policy_workflow_query(
        text,
        field_dict,
        policy_pattern=cfg.policy_no_pattern,
    )
    if rewritten is None or not rewritten.scope_explicit:
        # 严格无号保单句式未命中时，继续尝试能力包的词面锚点。
        return _g4b_capability_routes(text, session_state, cfg)
    if not rewritten.field_codes:
        if session_is_healthy(session_state) and _explicit_policy_list(text, rewritten.customer_query):
            return RouteDecision(
                action=RouteAction.DIRECT_TOOL,
                matched_check="G-4b",
                resolved_args={"tool": "policy_search", "query": text,
                    **({'customer_query': rewritten.customer_query} if rewritten.customer_query not in
                       {'我的客户', '我的所有客户', '我的全部客户', '所有客户', '全部客户', '所有', '全部'} else {})},
                reason="完整客户范围 × 纯保单清单请求；原文交查询解析器并沿用卡片直出",
            )
        return RouteDecision(
            action=RouteAction.AGENT,
            matched_check="G-4b",
            reason=(
                f"保单清单句式 × 客户条件 {rewritten.customer_query!r}"
                "——范围或作答意图不明确，保留 Agent 与 semantic_direct"
            ),
        )
    if re.search(r"怎么|如何|怎样|以及|同时|另外|并且", rewritten.customer_query):
        return RouteDecision(action=RouteAction.AGENT, matched_check="G-4b",
                             reason="客户定位残文包含流程提问或复合意图，交 Agent 处理")
    if is_customer_cohort(rewritten.customer_query):
        # 群体条件(姓氏/生日等一组人)× 字段意图:跨多保单字段核实,交 Agent。
        return RouteDecision(
            action=RouteAction.AGENT,
            matched_check="G-4b",
            reason="群体客户条件同时要求保单字段，交 Agent 处理多保单字段意图",
        )
    from app.core.query_scope import explicit_customer_locator
    if not explicit_customer_locator(rewritten.customer_query):
        return RouteDecision(action=RouteAction.AGENT, matched_check="G-4b",
                             reason="客户条件不是完整姓名或明确客户号，保留原条件交 Agent")
    return RouteDecision(
        action=RouteAction.WORKFLOW_START,
        matched_check="G-4b",
        capability_id="policy-field-lookup",
        resolved_args={"customer_query": rewritten.customer_query, "fields": list(rewritten.field_codes)},
        reason=(
            f"严格客户保单句式 × 客户条件 {rewritten.customer_query!r} × "
            f"字段 {list(rewritten.field_codes)} → policy-field-lookup"
        ),
    )


def _explicit_policy_list(text: str, customer_query: str) -> bool:
    """Do not turn counts, product references, or incomplete follow-ups into cards."""
    normalized = _normalize_route_text(text)
    # The search parser has no conversational context. Never send a deictic or
    # ordinal locator down a deterministic, non-escalating card path.
    from app.core.query_scope import context_dependent_query, qualified_customer_name, explicit_customer_locator
    if context_dependent_query(customer_query) or qualified_customer_name(customer_query):
        return False
    if not re.search(r"保单(?:有哪些|有哪几张|有哪几份|清单|列表|列出来)?$", normalized):
        return False
    if re.search(r"多少|总共|数量|几张|几份|以及|并且|同时|除了|除外|不要|不是|不包括", customer_query):
        return False
    if re.search(r"产品|保险|投保|退保|理赔|赔付|缴费|交费|保障|权益|等待期", customer_query):
        return False
    from app.conf import entity_dict
    try:
        if entity_dict().product_name_spans(customer_query, max_matches=1):
            return False
    except Exception:
        # Dictionary failure loses only the fast path, never service availability.
        return False
    if customer_query in {"我的客户", "我的所有客户", "我的全部客户", "所有客户", "全部客户"}:
        return True
    if customer_query in {"所有", "全部"}:
        return True
    return explicit_customer_locator(customer_query)


def _normalize_route_text(text: str) -> str:
    """锚点匹配的确定性归一：大小写、空白与句末标点不影响 exact。"""
    value = re.sub(r"\s+", "", text).strip("，。！？?!；;：:,. ").casefold()
    value = re.sub(r"[呢吧呀啊嘛吗]+$", "", value)
    # Equivalent question forms only; qualifiers/negation remain significant.
    for source, target in (("如何", "怎么"), ("怎样", "怎么"), ("需要什么材料", "需要哪些材料"),
                           ("缴费", "缴纳")):
        value = value.replace(source, target)
    value = re.sub(r"(?<!需)要哪些材料", "需要哪些材料", value)
    return value


def _anchor_matches(anchor: CapabilityAnchor, text: str, normalized: str) -> bool:
    value = _normalize_route_text(anchor.value)
    if anchor.type == "exact":
        return normalized == value
    if anchor.type == "keyword":
        return bool(value) and value in normalized
    try:
        return re.search(anchor.value, text, flags=re.IGNORECASE) is not None
    except (re.error, OverflowError):
        # 配置加载阶段应尽早失败；这里仍 fail-closed，防止运行态热加载坏正则放行。
        return False


def _g5_arbitrate(
    candidates: list[tuple[str, str, CapabilityAnchor]],
    session_state: dict,
    cfg: GateConf,
    query_text: str,
    policies: CapabilityPolicies | None = None,
) -> RouteDecision:
    """G-5 四条件仲裁：唯一命中、前置、风险、会话健康。"""
    if len(candidates) != 1:
        summaries = [
            (capability, summary)
            for capability, summary, _anchor in candidates
        ]
        return RouteDecision(
            action=RouteAction.AGENT,
            matched_check="G-5",
            candidates_for_agent=[
                RouteCandidate(capability_id=capability, summary=summary)
                for capability, summary in summaries
            ],
            reason="能力锚点未形成唯一命中，交 Agent 仲裁",
        )
    capability, summary, anchor = candidates[0]
    policies = policies or read_once(('capabilities', frozenset(cfg.risk_levels)),
                                    lambda: capabilities_conf(allowed_risk_levels=set(cfg.risk_levels)))
    manifest = policies.manifests[capability]
    if not session_is_healthy(session_state):
        return RouteDecision(action=RouteAction.AGENT, matched_check="G-5", capability_id=capability,
                             reason="会话处于降级/错误态，G-5 不自动放行")
    if anchor.requires_entity and not fresh_entities(session_state, cfg.entity_max_age_turns, cfg.entity_policy_limit):
        return RouteDecision(action=RouteAction.AGENT, matched_check="G-5", capability_id=capability,
                             reason=f"能力 {capability} 前置实体未满足，交 Agent 补充")
    if anchor.action is RouteAction.DIRECT_RAG:
        if "direct" not in manifest.execution_modes:
            return RouteDecision(action=RouteAction.AGENT, matched_check="G-5", capability_id=capability,
                                 reason=f"能力 {capability} 未登记 direct 执行方式")
        if manifest.risk != "LOW":
            return RouteDecision(action=RouteAction.AGENT, matched_check="G-5", capability_id=capability,
                                 reason=f"能力 {capability} 风险={manifest.risk}，禁止锚点 DIRECT_RAG")
        if not anchor.tool or anchor.tool not in manifest.tools:
            return RouteDecision(action=RouteAction.AGENT, matched_check="G-5", capability_id=capability,
                                 reason=f"能力 {capability} 锚点工具不在 manifest 白名单")
        return RouteDecision(
            action=RouteAction.DIRECT_RAG,
            matched_check="G-4b",
            capability_id=capability,
            resolved_args={"query": query_text, "tool": anchor.tool},
            reason=f"唯一能力锚点命中 {capability} × LOW 风险 × 工具 {anchor.tool}",
        )
    if anchor.action is RouteAction.WORKFLOW_START:
        if "workflow" not in manifest.execution_modes:
            return RouteDecision(action=RouteAction.AGENT, matched_check="G-5", capability_id=capability,
                                 reason=f"能力 {capability} 未登记 workflow 执行方式")
        return RouteDecision(
            action=RouteAction.WORKFLOW_START,
            matched_check="G-4b",
            capability_id=capability,
            resolved_args={"query": query_text},
            reason=f"唯一能力锚点命中 {capability} × Workflow 前置满足",
        )
    return RouteDecision(action=RouteAction.AGENT, matched_check="G-5", capability_id=capability,
                         reason=f"能力 {capability} 锚点动作 {anchor.action} 不可放行")


def _route_user_text(session_state: dict) -> str:
    return str((session_state.get("route") or {}).get("user_text") or "").strip()


def _g4b_capability_routes(text: str, session_state: dict, cfg: GateConf) -> RouteDecision | None:
    """从 Capability routes 生成 G-5 候选；routes 是数据，matcher 保持纯确定性。"""
    policies = read_once(('capabilities', frozenset(cfg.risk_levels)),
                         lambda: capabilities_conf(allowed_risk_levels=set(cfg.risk_levels)))
    normalized = _normalize_route_text(text)
    candidates: list[tuple[str, str, CapabilityAnchor]] = []
    for capability, routes in policies.routes.items():
        manifest = policies.manifests[capability]
        # 灰度开关是 capability 独立的运行时准入位：off/shadow 只保留
        # 配置与观测入口，不得产生正式 Direct/Workflow 路由。
        if manifest.gray_switch != "on":
            continue
        positive = [a for a in routes.anchors.get("positive", []) if _anchor_matches(a, text, normalized)]
        negative = [a for a in routes.anchors.get("negative", []) if _anchor_matches(a, text, normalized)]
        if not positive or negative:
            continue
        # 同一能力多个正锚点只产生一个候选；动作不一致时安全交 Agent。
        actions = {anchor.action for anchor in positive}
        if len(actions) != 1:
            candidates.extend((capability, manifest.description_for_model, anchor) for anchor in positive)
            continue
        # 一个正则锚点可以命中多个服务；这些问题必须交 Agent 分别检索。
        # exact 是整句白名单；关键词/正则按实际命中片段计数，重叠片段归并。
        if actions == {RouteAction.DIRECT_RAG} and not any(a.type == "exact" for a in positive):
            spans = sorted({match.span() for anchor in positive
                            for match in re.finditer(
                                anchor.value if anchor.type == "regex" else re.escape(anchor.value),
                                text, flags=re.IGNORECASE) if match.start() != match.end()})
            groups: list[tuple[int, int]] = []
            for start, end in spans:
                if groups and start < groups[-1][1]:
                    groups[-1] = (groups[-1][0], max(end, groups[-1][1]))
                else:
                    groups.append((start, end))
            if len(groups) != 1:
                return RouteDecision(action=RouteAction.AGENT, matched_check="G-5",
                                     capability_id=capability,
                                     reason="知识锚点未形成单一实体命中，交 Agent 分别检索")
        candidates.append((capability, manifest.description_for_model, positive[0]))
    if not candidates:
        return None
    return _g5_arbitrate(candidates, session_state, cfg, text, policies)
