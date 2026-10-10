"""只投影落库副本；交付、审计和失败快照始终保留原内存状态。"""
from __future__ import annotations


def persisted_state(state: dict) -> dict:
    result = dict(state)
    result.pop('_native_prior_ack', None)
    # Workflow namespaces share the live root budget but persist only counters.
    for key in ('wf.policy_snapshot', 'wf.policy_field_lookup'):
        if isinstance(state.get(key), dict):
            result[key] = persisted_state(state[key])
    budget = state.get("budget")
    if isinstance(budget, dict):
        result["budget"] = {k: v for k, v in budget.items()
                            if k not in {"tool_ledger", "tool_result_cache", "business_tool_results", "model_projection"}}
    route = state.get("route")
    if isinstance(route, dict):
        result["route"] = {k: v for k, v in route.items() if k not in {
            "recent_history", "policy_search_contexts", "last_result", "direct_business_result", "direct_agent_context",
            "agent_terminal_clarify", "agent_terminal_search", "direct_answer",
        }}
    evidence = state.get("evidence")
    if isinstance(evidence, dict):
        pool = evidence.get("by_turn") or {}
        trace = (route or {}).get("trace_id")
        current = pool.get(trace) or {}
        result["evidence"] = {**evidence, "by_turn": {
            trace: current} if current and current.get("confirmed", True) is True else {}}
    return result
