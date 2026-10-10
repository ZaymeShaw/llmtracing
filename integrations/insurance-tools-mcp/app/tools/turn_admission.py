"""Pure tool admission; caller owns clocks, accounting, cache and stop effects."""
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from app.schemas.tool_control import ToolControlReason
from app.core.turn_control import StopKind, TurnStop
from app.tools.tool_sets import RAG_TOOLS, STATE_TOOLS

if TYPE_CHECKING:
    from app.core.harness_config import HarnessConf


@dataclass(frozen=True)
class AdmissionRefusal:
    reason: ToolControlReason
    hint: str = ""


def admit(state: dict, tool: str, config: "HarnessConf", *,
          finish_only: bool = False, soft_reason: str | None = None,
          remaining_s: float | None = None, reads: int = 0, stateful: bool = False,
          selection: Callable[[], str | None] | None = None,
          check_rpc: bool = False, model_available: bool = True,
          admission_ms: int | None = None, stop: TurnStop | None = None, reuse: bool = False) -> AdmissionRefusal | None:
    """Read-only admission; reuse skips execution quotas/tool-quota stops, retaining all other guards."""
    budget = state.get("budget") or {}
    reason = ToolControlReason
    legacy = 'turn_stop' not in (state.get('route') or {})
    if stop is not None and stop.kind is StopKind.TERMINAL or legacy and budget.get("terminal_search_started"):
        return AdmissionRefusal(reason.TERMINAL_SEARCH_EXCLUSIVE)
    if stop is not None and stop.kind is StopKind.WAIT_USER or legacy and budget.get("turn_clarify_opened"):
        return AdmissionRefusal(reason.CLARIFY_OPENED)
    # This local registration does no fact lookup or backend IO. It may use the
    # reserved answer window, while pending/state/selection guards still apply.
    clarification = tool == "ask_clarify" and (
        (stop is not None and stop.kind is StopKind.FINISH and stop.reason != "finish_only")
        or (stop is None and (bool(soft_reason) or not finish_only)))
    if stop is not None and stop.kind is StopKind.FINISH and stop.reason == "no_progress" and not clarification:
        return AdmissionRefusal(reason.NO_PROGRESS)
    quota_stop = reuse and stop is not None and stop.kind is StopKind.FINISH and stop.reason == "tools"
    if not clarification and (soft_reason == 'deadline' or stop is not None and stop.reason == 'deadline'):
        return AdmissionRefusal(reason.TIME_BUDGET)
    if not model_available or ((finish_only or soft_reason or (stop is not None and not quota_stop)) and not clarification):
        return AdmissionRefusal(reason.TOTAL_LIMIT)
    profile = config.agent_profile()
    counts = budget.get("tool_call_counts") or {}
    if config.tool_call_limits_enabled and not reuse:
        if check_rpc and budget.get('backend_rpc_calls', 0) >= profile.max_rpc_calls:
            return AdmissionRefusal(reason.BACKEND_BUDGET)
        if budget.get("model_tool_calls", 0) >= profile.max_tool_calls and not clarification:
            return AdmissionRefusal(reason.TOTAL_LIMIT)
        if counts.get(tool, 0) >= profile.max_same_tool_calls:
            return AdmissionRefusal(reason.SAME_TOOL_LIMIT)
        if tool in RAG_TOOLS and sum(counts.get(name, 0) for name in RAG_TOOLS) >= profile.max_rag_tool_calls:
            return AdmissionRefusal(reason.RAG_LIMIT)
    if (not clarification and remaining_s is not None
            and remaining_s * 1000 < (admission_ms or config.admission_ms["tool_call"])):
        return AdmissionRefusal(reason.TIME_BUDGET)
    if stateful or (tool in STATE_TOOLS and reads):
        return AdmissionRefusal(reason.PARALLEL_STATE_CONFLICT)
    if selection is not None and (hint := selection()) is not None:
        return AdmissionRefusal(reason.SELECTION_MISUSE, hint)
    return None
