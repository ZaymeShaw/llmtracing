"""One exit decision, no extra content checks; valid answers remain deliverable."""

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.harness_config import HarnessConf

from app.schemas import AnswerEnvelope

from app.core.turn_control import StopKind, TurnStop


class Cleanup(str, Enum):
    NONE = "none"
    RETAIN = "retain"


@dataclass(frozen=True)
class ExitSignals:
    reasons: tuple[str, ...] = ()
    stop: TurnStop | None = None
    failed: bool = False
    incomplete: bool = False
    ladder_enabled: bool = False
    capacity_enabled: bool = False
    recovery_enabled: bool = True


@dataclass(frozen=True)
class ExitDecision:
    reason: str | None
    ladder_reason: str | None
    audit_reason: str | None
    health_reason: str | None
    cleanup: Cleanup
    replace_answer: bool
    use_ladder: bool
    degraded: bool


# Explicit hard stops outrank validator symptoms of unfinished work; collisions use
# this order in the answer, audit, health and cleanup. Finish-only is not failure.
PRIORITY = (
    "cancelled",
    "hook_failed",
    "budget_exhausted",
    "context_overflow",
    "model_capacity_exceeded",
    "service_restarting",
    "unparseable",
    "agent_output_contaminated",
    "turn_timeout",
    "model_unavailable",
    "agent_failed",
    "run_exception",
    "run_incomplete",
)


def resolve_exit(signals: ExitSignals) -> ExitDecision:
    reasons = set(filter(None, signals.reasons))
    stop = signals.stop
    if stop is not None:
        if stop.kind is StopKind.EXHAUSTED:
            reasons.add("budget_exhausted")
        elif stop.kind is StopKind.OVERFLOW and stop.reason != "recovering":
            reasons.add("context_overflow")
        elif stop.kind is StopKind.CAPACITY:
            reasons.add(stop.reason)
        elif stop.kind is StopKind.FINISH and signals.incomplete and stop.reason != "finish_only":
            reasons.add("no_progress" if stop.reason == "no_progress" else "budget_finish")
    # Existing citation signals are preserved below explicit hard stops. No new
    # content gate is introduced here.
    citations = sorted(r for r in reasons if r.startswith("citation_"))
    order = (*PRIORITY[:5], *citations, *PRIORITY[5:])
    reason = next((r for r in order if r in reasons), next(iter(sorted(reasons)), None))
    hard_stop = bool(
        stop
        and (
            stop.kind is StopKind.EXHAUSTED
            or stop.kind is StopKind.OVERFLOW
            and stop.reason != "recovering"
            or stop.kind is StopKind.CAPACITY
            and stop.reason == "model_capacity_exceeded"
        )
    )
    replace_answer = signals.failed or hard_stop
    use_ladder = bool(
        reason
        and (
            signals.ladder_enabled
            or reason == "model_capacity_exceeded" and signals.capacity_enabled
            or reason == "budget_exhausted"
            or reason == "service_restarting"
            or reason == "context_overflow"
            and signals.recovery_enabled
        )
        and (reason != "model_capacity_exceeded" or signals.capacity_enabled)
    )
    # A mere tool-stop (including capacity on a single tool) does not invalidate
    # a delivered card, clarification or completed answer.
    degraded = replace_answer or bool(signals.incomplete and reason)
    cleanup = Cleanup.RETAIN if replace_answer else Cleanup.NONE
    return ExitDecision(reason, reason, reason, reason, cleanup, replace_answer, use_ladder, degraded)


def exit_for_state(
    state: dict,
    *,
    reasons: tuple[str, ...] = (),
    failed: bool = False,
    incomplete: bool = False,
    config: "HarnessConf | None" = None,
) -> ExitDecision:
    """Collect only existing signals; route stop is a snapshot, never a second writer."""
    from app.core.model_budget import current_model_budget

    route = state.get("route") or {}
    budget = current_model_budget()
    stop = budget.control.stop if budget is not None and budget.state is state else stop_from_state(state)
    structural = route.get("answer_failure_reason")
    storage_reason = route.get('storage_failure_reason')
    observed = (*reasons, *((structural,) if isinstance(structural, str) and structural else ()),
                *((storage_reason,) if storage_reason == 'storage_unavailable' else ()))
    ladder_on = config.features.answer_ladder.enabled if config else bool(route.get("answer_ladder_enabled"))
    retained = _direct_answer(state, route.get("trace_id"))
    decision = resolve_exit(
        ExitSignals(
            observed,
            stop,
            failed or bool(structural and (ladder_on or retained is not None)),
            incomplete,
            ladder_enabled=ladder_on,
            capacity_enabled=config.features.capacity_answer.enabled
            if config
            else bool(route.get("capacity_answer_enabled")),
            recovery_enabled=config.context_recovery.enabled if config else True,
        )
    )
    # Retained Direct delivery is already independent of the general ladder flag.
    # Publish the same availability decision to native streaming/status consumers.
    return replace(decision, use_ladder=True) if decision.replace_answer and retained is not None else decision


def stop_from_state(state: dict) -> TurnStop | None:
    route = state.get("route") or {}
    if "turn_stop" in route:
        value = route["turn_stop"]
        if not isinstance(value, dict):
            return None
        try:
            return TurnStop(StopKind(value["kind"]), str(value["reason"]))
        except (KeyError, ValueError):
            return None
    # Compatibility for standalone/injected callers without a bound live control.
    # New turns publish turn_stop (including None), so legacy flags cannot override it.
    for key, kind, reason in (
        ("budget_exhausted", StopKind.EXHAUSTED, "budget_exhausted"),
        ("context_overflow", StopKind.OVERFLOW, "context_overflow"),
        ("capacity_refused", StopKind.CAPACITY, "model_capacity_exceeded"),
        ("budget_finish_reason", StopKind.FINISH, "budget_finish"),
    ):
        if route.get(key):
            return TurnStop(kind, str(route[key]) if kind is StopKind.FINISH else reason)
    return None


def publish_exit(state: dict, decision: ExitDecision) -> None:
    state.setdefault("route", {})["exit_reason"] = decision.reason


DIRECT_RESULT_LIMITATION = "部分内容未能完成整理，以上仅为本轮已查询到的信息。"
_DIRECT_BINDING = ("run_id", "trace_id", "identity_scope", "user_text")


def retain_direct_answer(state: dict, answer: AnswerEnvelope) -> None:
    """Retain a finalized Direct template or original RAG excerpt before escalation, without IO."""
    route = state.get("route") or {}
    if (not all(isinstance(route.get(key), str) and route[key] for key in _DIRECT_BINDING)
            or answer.trace_id != route["trace_id"] or not answer.fact_refs):
        return
    partial = answer.model_copy(update={
        "answer": answer.answer + "\n\n" + DIRECT_RESULT_LIMITATION,
        "completeness": "partial",
        "gaps": list(dict.fromkeys([*answer.gaps, DIRECT_RESULT_LIMITATION])),
        "limitations": list(dict.fromkeys([*(answer.limitations or []), DIRECT_RESULT_LIMITATION])),
    })
    route["direct_answer"] = {
        "binding": {key: route[key] for key in _DIRECT_BINDING},
        "envelope": partial.model_dump(mode="json"),
    }


def _direct_answer(state: dict, trace_id: str | None) -> AnswerEnvelope | None:
    route = state.get("route") or {}
    saved = route.get("direct_answer")
    if not isinstance(saved, dict) or not trace_id or trace_id != route.get("trace_id"):
        return None
    binding = saved.get("binding")
    if not isinstance(binding, dict) or any(
        not binding.get(key) or binding[key] != route.get(key) for key in _DIRECT_BINDING
    ):
        return None
    try:
        answer = AnswerEnvelope.model_validate(saved.get("envelope"))
        return answer if answer.trace_id == trace_id and answer.fact_refs and answer.completeness == "partial" else None
    except (ValueError, TypeError):
        return None


def select_exit_answer(
    decision: ExitDecision, state: dict, trace_id: str | None, candidate: AnswerEnvelope | None = None
) -> AnswerEnvelope:
    from app.core.answer_delivery import failure_answer
    from app.core.answer_ladder import guaranteed_answer

    if decision.replace_answer or candidate is None:
        retained = _direct_answer(state, trace_id)
        if retained is not None:
            return retained
        if decision.reason in {'agent_failed', 'model_unavailable', 'run_exception', 'unparseable'}:
            recovered = _current_record_answer(state, trace_id)
            if recovered is not None:
                return recovered
    if decision.use_ladder and (decision.replace_answer or candidate is None):
        return failure_answer(state=state, trace_id=trace_id, reason=decision.ladder_reason, enabled=True).envelope
    if decision.replace_answer or candidate is None:
        return guaranteed_answer(trace_id=trace_id, reason=decision.ladder_reason).envelope
    return candidate


def _current_record_answer(state: dict, trace_id: str | None) -> AnswerEnvelope | None:
    """Recover only confirmed current requested records; no IO or new evidence."""
    route = state.get('route') or {}
    if not trace_id or trace_id != route.get('trace_id'):
        return None
    bucket = ((state.get('evidence') or {}).get('by_turn') or {}).get(trace_id) or {}
    if not isinstance(bucket, dict) or bucket.get('confirmed', True) is not True:
        return None
    from app.conf import fields_dict
    from app.core.resolve import scan_fields
    from app.core.policy_projection import requested_template_facts, requested_policy_subjects
    from app.schemas import Fact, EnvelopeStatus
    from app.core.render import render_template_facts, field_labels
    from app.core.answer_format import format_answer
    try:
        dictionary = fields_dict()
        fields, _, _ = scan_fields(str(route.get('user_text') or ''), dictionary)
        material = [Fact.model_validate(item) for item in bucket.get('facts', []) if isinstance(item, dict)]
        facts = requested_template_facts([item for item in material if item.subject_type in {'policy', 'customer'}],
                                        list(fields), requested_policy_subjects(state))
        gaps=['部分内容未能完成整理，以下为本轮已取得的资料。'] if facts else []
        body=render_template_facts(facts,field_labels(dictionary),gaps,EnvelopeStatus.PARTIAL) if facts else ''
        # A confirmed QA answer can be delivered as source material when the model
        # fails. Whole entries only; never dump product RAG or serialized records.
        from app.schemas import Evidence
        from app.core.render import render_rag_facts
        from app.core.qa_recovery import qa_recovery
        refs = [Evidence.model_validate(item) for item in bucket.get('evidence', []) if isinstance(item, dict)]
        knowledge, knowledge_gaps = qa_recovery(material, refs)
        gaps.extend(knowledge_gaps)
        if knowledge:
            body += '\n\n' + render_rag_facts(knowledge, field_labels(dictionary), evidence=refs, confirmed=False)
            facts += knowledge
        if not facts and not knowledge_gaps:
            return None
        if not body.strip():
            body = '本轮已取得部分知识资料，但尚未完成整理，可继续提问。'
        return AnswerEnvelope(answer=format_answer(body),fact_refs=[f.fact_id for f in facts],
                              gaps=gaps,completeness='partial',limitations=gaps,trace_id=trace_id)
    except (ValueError, TypeError):
        return None


def settle_exit(state: dict, decision: ExitDecision, trace_id: str | None, *, degraded: bool) -> dict:
    """Settle once in memory; each channel owns its single persistence operation.

    The marker is per-turn route state, reset by assemble. It prevents native
    presenter and stream observers from revoking evidence/recording health twice.
    """
    from app.core.turn_cleanup import discard_incomplete_turn
    from app.core.session_health import record_session_health

    publish_exit(state, decision)
    route = state["route"]
    # Disclosure-only partial answers are usable and do not disable next-turn fast paths.
    degraded = degraded and (decision.degraded or bool(decision.health_reason))
    marker = [decision.reason, decision.cleanup.value, degraded]
    if route.get("_exit_settled") == marker:
        return {}
    updates = {}
    if decision.cleanup is not Cleanup.NONE:
        if decision.reason != "cancelled" and route.get("trace_id") in (None, trace_id):
            from app.core.clarify import retry_answered_agent_clarification
            retry_answered_agent_clarification(state)
            if isinstance(state.get("entities"), dict):
                updates["entities"] = state["entities"]
        updates.update(discard_incomplete_turn(state, trace_id, "本轮交付未完成"))
    record_session_health(state, degraded=degraded, reason=decision.health_reason)
    route["_exit_settled"] = marker
    updates = {**updates, "route": route}
    if isinstance(state.get('turns'), list):
        updates['turns'] = state['turns']
    # Chat's terminal write is deferred until after delivery. Freeze the final
    # authority changes here, so its earlier workflow snapshot cannot resurrect
    # evidence confirmation or an interrupted ledger. No live answer is read later.
    from app.core.turn_execution import current_execution
    turn = current_execution()
    if turn is not None and route.get('session_id') == turn.key[2]:
        from copy import deepcopy
        from app.core.persisted_state import persisted_state
        turn.terminal_state_updates = deepcopy(persisted_state(updates))
    return updates
