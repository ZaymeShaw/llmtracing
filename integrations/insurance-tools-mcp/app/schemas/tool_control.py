"""本地工具准入控制契约，不从上游正文推断控制动作。"""
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self


class ToolControlReason(str, Enum):
    TOTAL_LIMIT = "total_limit"
    NATIVE_LIMIT = "native_limit"
    BACKEND_BUDGET = "backend_budget"
    SELECTION_MISUSE = "selection_misuse"
    NO_PROGRESS = "no_progress"
    SAME_TOOL_LIMIT = "same_tool_limit"
    RAG_LIMIT = "rag_limit"
    TIME_BUDGET = "time_budget"
    PARALLEL_STATE_CONFLICT = "parallel_state_conflict"
    CLARIFY_OPENED = "clarify_opened"
    TERMINAL_SEARCH_EXCLUSIVE = "terminal_search_exclusive"
    OPEN_CLARIFY_LIMIT = "open_clarify_limit"


class ToolControlAction(str, Enum):
    FINISH_WITH_EVIDENCE = "finish_with_evidence"
    WAIT_BATCH = "wait_batch"
    WAIT_USER = "wait_user"
    AWAIT_TERMINAL = "await_terminal"
    HANDOFF_HUMAN = "handoff_human"
    CORRECT_CALL = "correct_call"


CONTROL_ACTIONS = {
    ToolControlReason.NO_PROGRESS: ToolControlAction.FINISH_WITH_EVIDENCE,
    ToolControlReason.TOTAL_LIMIT: ToolControlAction.FINISH_WITH_EVIDENCE,
    ToolControlReason.NATIVE_LIMIT: ToolControlAction.FINISH_WITH_EVIDENCE,
    ToolControlReason.BACKEND_BUDGET: ToolControlAction.FINISH_WITH_EVIDENCE,
    ToolControlReason.SELECTION_MISUSE: ToolControlAction.CORRECT_CALL,
    ToolControlReason.SAME_TOOL_LIMIT: ToolControlAction.FINISH_WITH_EVIDENCE,
    ToolControlReason.RAG_LIMIT: ToolControlAction.FINISH_WITH_EVIDENCE,
    ToolControlReason.TIME_BUDGET: ToolControlAction.FINISH_WITH_EVIDENCE,
    ToolControlReason.PARALLEL_STATE_CONFLICT: ToolControlAction.WAIT_BATCH,
    ToolControlReason.CLARIFY_OPENED: ToolControlAction.WAIT_USER,
    ToolControlReason.TERMINAL_SEARCH_EXCLUSIVE: ToolControlAction.AWAIT_TERMINAL,
    ToolControlReason.OPEN_CLARIFY_LIMIT: ToolControlAction.HANDOFF_HUMAN,
}


class ToolControl(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: ToolControlReason = Field(description="本地准入拒绝或追问停止原因；不表示上游服务故障。")
    action: ToolControlAction = Field(description="本地固定控制动作：基于已有证据收尾、等待同批结果、等待用户、等待卡片终态、转人工或纠正工具/参数。")

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action is not CONTROL_ACTIONS[self.reason]:
            raise ValueError("工具控制原因与动作不匹配")
        return self
