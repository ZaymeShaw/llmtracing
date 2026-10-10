"""信封枚举字面量(方案 v13 §10.3/§10.4 · M0 冻结)。

status 语义(消费方判据,§10.3 原文内联):
SUCCESS=可答;PARTIAL=部分缺失(披露 gaps);EMPTY=零召回(不得编造,给替代路径);
AMBIGUOUS=实体歧义(触发澄清/定位链路);NEEDS_CLARIFICATION=HITL(run_* 子流程 @pause
时由薄包装返回,§5.4);DENIED=越权(永不重试,防探测);ERROR=系统错(可重试一次);TIMEOUT=超时。
"""
from __future__ import annotations

from app.compat import StrEnum


class EnvelopeStatus(StrEnum):
    """工具信封 / ExecutionResult 共用的状态族(§10.3 八态)。"""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"
    AMBIGUOUS = "AMBIGUOUS"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    DENIED = "DENIED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class ErrorCode(StrEnum):
    """信封 error.code(§10.3):错误归一在 conf/tool/tool_config_{ENV_TYPE}_args.yaml 维护映射。"""

    TIMEOUT = "TIMEOUT"
    UPSTREAM = "UPSTREAM"
    DENIED = "DENIED"
    BUDGET = "BUDGET"
    OVERLOADED = "OVERLOADED"


class SourceType(StrEnum):
    """Evidence 来源类型(§10.4)。"""

    BUSINESS_API = "business_api"
    RAG_DOCUMENT = "rag_document"
    QA_PAIR = "qa_pair"


class ClarifyEvent(StrEnum):
    """澄清挂起事件词汇表(§7.1/§09 expected_event.type)。

    三产出方(Direct 执行器 / Agent ask_clarify / Workflow @pause)与 G-2 查表
    匹配共用;值即历史字面量,存量台账数据无需迁移。
    """

    CUSTOMER_SELECTED = "CUSTOMER_SELECTED"
    POLICY_SELECTED = "POLICY_SELECTED"
    CLARIFY_ANSWERED = "CLARIFY_ANSWERED"
