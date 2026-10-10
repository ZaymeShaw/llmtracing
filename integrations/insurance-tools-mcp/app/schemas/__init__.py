"""七核心契约 + 类型化实体记忆:M0 冻结后代码 schema 即权威(评审六·文档权威移交)。

Fact / Evidence / ToolEnvelope(本切片)/ ExecutionResult(含 AgentOutput 子契约,D16)/
AnswerEnvelope(保留 checks:null 兼容字段)/ RouteDecision / Ledger——按切片分批冻结；PolicyEntityRef 承载 G-3 记忆。
纯 pydantic 数据结构,禁止 import agno(CI 强制)——契约与 core/ 在任何 V-R13 档位下原样复用。
"""
from app.schemas.answer import (
    HANDOFF_ACTION,
    STRUCTURED_METADATA_KEY,
    AnswerEnvelope,
    StructuredToolResult,
)
from app.schemas.enums import ClarifyEvent, EnvelopeStatus, ErrorCode, SourceType
from app.schemas.entity import PolicyEntityRef
from app.schemas.evidence import Evidence
from app.schemas.execution import AgentOutput, ClarifyPayload, ExecutionResult, RenderHint
from app.schemas.fact import Fact
from app.schemas.ledger import ExpectedEvent, LedgerEntry, Lifecycle, TaskLedger, TaskRole
from app.schemas.route import RouteAction, RouteCandidate, RouteDecision
from app.schemas.tool_envelope import SourceRef, ToolEnvelope, ToolError

__all__ = [
    "AnswerEnvelope",
    "HANDOFF_ACTION",
    "STRUCTURED_METADATA_KEY",
    "StructuredToolResult",
    "ClarifyPayload",
    "ClarifyEvent",
    "EnvelopeStatus",
    "ErrorCode",
    "SourceType",
    "PolicyEntityRef",
    "Fact",
    "Evidence",
    "AgentOutput",
    "ExecutionResult",
    "RenderHint",
    "ExpectedEvent",
    "LedgerEntry",
    "Lifecycle",
    "TaskLedger",
    "TaskRole",
    "RouteAction",
    "RouteCandidate",
    "RouteDecision",
    "SourceRef",
    "ToolError",
    "ToolEnvelope",
]
