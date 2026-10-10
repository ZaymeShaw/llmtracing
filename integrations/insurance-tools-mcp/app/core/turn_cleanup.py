"""未完成轮次的内存状态清理；持久化由对应入口负责。"""
from __future__ import annotations

from app.core.ledger import mark_interrupted
from app.schemas import TaskLedger


def discard_incomplete_turn(
    state: dict, trace_id: str | None, summary: str, *, keep_failed_state: bool | None = None,
) -> dict[str, object]:
    """中断不删除资料或用户选择；旧 keep_failed_state 参数仅供调用兼容。"""
    updates: dict[str, object] = {}
    route = state.get("route")
    current_turn = not route or route.get("trace_id") in (None, trace_id)
    if isinstance(route, dict) and route.get("trace_id") == trace_id:
        route.pop("best_effort", None)
        updates["route"] = route
    evidence = state.get("evidence")
    if isinstance(evidence, dict):
        by_turn = evidence.get("by_turn")
        if isinstance(by_turn, dict) and trace_id in by_turn:
            by_turn[trace_id]["confirmed"] = False
            updates["evidence"] = evidence
    try:
        ledger = TaskLedger.model_validate(state.get("ledger"))
        if current_turn and ledger.focus_task_id is not None:
            failed = mark_interrupted(ledger, ledger.focus_task_id, summary=summary)
            state["ledger"] = failed.model_dump()
            updates["ledger"] = state["ledger"]
    except (TypeError, ValueError):
        pass  # 损坏台账不能阻断既定收尾，沿用接入层容错行为。
    return updates
