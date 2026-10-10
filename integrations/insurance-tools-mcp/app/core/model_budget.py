"""每轮模型准入：实际 provider 调用计数、已知用量和剩余时间。"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.budget import current_deadline
from app.core.turn_control import StopKind, TurnControl

logger = logging.getLogger(__name__)


class ModelBudgetExceeded(RuntimeError):
    pass


BUDGET_GAP = "本轮已停止继续查询，以下答复仅依据已取得的资料；未完成部分仍需补充核实。"


@dataclass
class ModelBudget:
    max_calls: int
    max_tokens: int
    calls: int = 0
    tokens: int = 0
    reserved: int = 0
    reserve_calls: int = 2
    soft_token_ratio: float = 0.85
    finalize_reserve_s: float = 25.0
    answer_reserve_s: float = 0.0
    control: TurnControl = field(default_factory=TurnControl)
    state: dict | None = None
    max_tools: int | None = None
    context_recovery_attempted: bool = False
    tool_views: list[dict] = field(default_factory=list)
    tool_view_chars: int = 0

    cached_input_tokens: int = 0
    cached_reports: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    input_reports: int = 0
    output_reports: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    consumed_view_chars: int = 0
    finish_overage_used: bool = False
    finish_retry_used: bool = False

    @property
    def enabled(self) -> bool:
        """旧消费方的只读契约；BE-02 起所有入口固定使用预算控制。"""
        return True

    def next_input_tokens(self, estimate: int = 0) -> int:
        # 可见工具视图采用 UTF-8 字节/3 的保守估算；原始结果不参与改写。
        growth = sum(len(view['visible'].encode('utf-8')) for view in self.tool_views) - self.consumed_view_chars
        return max(estimate, self.last_input_tokens + self.last_output_tokens + max(0, growth + 2) // 3)

    def _has_finish_material(self) -> bool:
        route = (self.state or {}).get('route', {})
        bucket = (self.state or {}).get('evidence', {}).get('by_turn', {}).get(route.get('trace_id'), {})
        return bucket.get('confirmed', True) is True and bool(bucket.get('facts') or bucket.get('evidence'))

    @property
    def has_progress(self) -> bool:
        """轮中准入优先：本轮已经尝试模型或取得可用于交付的工具材料。"""
        return self.calls > 0 or self._has_finish_material()

    def can_retry_finish(self) -> bool:
        return ((self.calls < self.max_calls and self.tokens + self.reserved < self.max_tokens)
                or (self.control.stop is not None and self.control.stop.kind is StopKind.FINISH
                    and not self.finish_overage_used and self._has_finish_material()))

    @property
    def request_finish_only(self) -> bool:
        return self.control.stop is not None

    @request_finish_only.setter
    def request_finish_only(self, value: bool) -> None:
        if value:
            self.control.set(StopKind.FINISH, "finish_only")
        elif self.control.stop is not None and self.control.stop.kind is StopKind.FINISH:
            self.control.stop = None
            self.control.publish()

    @property
    def finish_reason(self) -> str | None:
        stop = self.control.stop
        return (stop.reason if stop is not None and stop.kind is StopKind.FINISH
                and stop.reason != 'finish_only' else None)

    @finish_reason.setter
    def finish_reason(self, reason: str | None) -> None:
        if reason:
            self.control.set(StopKind.FINISH, reason)

    @property
    def exhausted_reason(self) -> str | None:
        stop = self.control.stop
        return stop.reason if stop is not None and stop.kind is StopKind.EXHAUSTED else None

    def publish_usage(self) -> None:
        """同步标量到轮级账本，截止快照不依赖执行任务的 ContextVar。"""
        if self.state is not None:
            self.state.setdefault("budget", {}).update(
                llm_calls=self.calls,
                cached_input_tokens=self.cached_input_tokens if self.calls and self.cached_reports == self.calls else None,
                input_tokens=self.input_tokens if self.calls and self.input_reports == self.calls else None,
                output_tokens=self.output_tokens if self.calls and self.output_reports == self.calls else None,
            )

    def stop_tools(self, reason: str, *, blocked: bool = False) -> None:
        # Reserving finalization is not proof of an incomplete answer. Publish
        # the delivery limitation only when work was actually refused.
        self.control.set(StopKind.FINISH, reason)
        if blocked and self.state is not None:
            self.state.setdefault("route", {})["budget_finish_reason"] = self.finish_reason or reason

    def soft_reason(self, *, include_calls: bool = True, include_tools: bool = True, output_cap: int = 0, input_estimate: int = 0) -> str | None:
        deadline = current_deadline(self.finalize_reserve_s, self.answer_reserve_s)
        reason = (
            (self.finish_reason if include_tools or self.finish_reason != "tools" else None) or
            ("calls" if include_calls and self.calls >= self.max_calls - self.reserve_calls else
             "tools" if include_tools and self.max_tools is not None and self.state is not None and self.state.get("budget", {}).get("model_tool_calls", 0) >= self.max_tools else
             "tokens" if self.tokens + self.reserved + self.next_input_tokens(input_estimate) + output_cap >= self.max_tokens * self.soft_token_ratio else
             "deadline" if deadline is not None and not deadline.can_start('tool') else None)
        )
        return reason

    def soft_limit(self, *, include_calls: bool = True) -> bool:
        reason = self.soft_reason(include_calls=include_calls)
        if reason:
            self.stop_tools(reason)
        return reason is not None

    def exhaust(self, reason: str) -> ModelBudgetExceeded:
        self.control.set(StopKind.EXHAUSTED, reason)
        if self.state is not None:
            self.state.setdefault("route", {})["budget_exhausted"] = reason
        return ModelBudgetExceeded(f"model budget exhausted: {reason}")

    def admit(self, output_cap: int, admission_s: float, *, input_estimate: int = 0) -> int:
        deadline = current_deadline(self.finalize_reserve_s, self.answer_reserve_s)
        soft = self.soft_reason(output_cap=output_cap, input_estimate=input_estimate)
        if soft:
            self.stop_tools(soft)
        reason = (
            "deadline" if deadline is not None and not deadline.can_start('model', admission_s) else
            "calls" if self.calls >= self.max_calls else
            "tokens" if self.tokens + self.reserved >= self.max_tokens else
            None
        )
        # 只给已经取得工具结果的轮次一次超限收尾。每个真实尝试仍计数；
        # 输出严格受本次配额约束，输入计入真实用量，不能声称总 tokens 未超限。
        overage = (reason in {'calls', 'tokens'} and self.control.stop is not None
                   and self.control.stop.kind is StopKind.FINISH and not self.finish_overage_used
                   and self._has_finish_material())
        if overage:
            self.finish_overage_used = True
            reason = None
        if reason:
            if self.state is not None:
                from app.core.audit import count_refusal
                count_refusal(self.state.setdefault("budget", {}), "model_" + reason)
            logger.warning("model_budget_refused", extra={"observability": {"reason": reason}})
            raise self.exhaust(reason)
        allowance = output_cap if overage else min(output_cap, self.max_tokens - self.tokens - self.reserved)
        self.calls += 1
        self.publish_usage()
        self.reserved += allowance
        self.consumed_view_chars = sum(len(view['visible'].encode('utf-8')) for view in self.tool_views)
        if overage and self.state is not None:
            self.state.setdefault('budget', {})['finish_overage_calls'] = 1
        return allowance

    def finish(self, allowance: int, reported: int | None, *,
               input_tokens: int | None = None, output_tokens: int | None = None,
               cached_input_tokens: int | None = None) -> None:
        self.reserved -= allowance
        if type(cached_input_tokens) is int and cached_input_tokens >= 0:
            self.cached_input_tokens += cached_input_tokens
            self.cached_reports += 1
        if type(input_tokens) is int and input_tokens >= 0:
            self.last_input_tokens = input_tokens
            self.input_tokens += input_tokens
            self.input_reports += 1
        if type(output_tokens) is int and output_tokens >= 0:
            self.last_output_tokens = output_tokens
            self.output_tokens += output_tokens
            self.output_reports += 1
        self.publish_usage()
        # 未报告 usage 时按本次输出预留计费；输入未知，不能声称是精确总 token 硬顶。
        self.tokens += allowance if reported is None else reported
        logger.info("model_usage", extra={"observability": {
            "total": self.tokens, "attempts": self.calls,
            "outcome": "usage_missing" if reported is None else "reported",
        }})


_budget: ContextVar[ModelBudget | None] = ContextVar("model_budget", default=None)
output_allowance: ContextVar[int | None] = ContextVar("model_output_allowance", default=None)


def current_model_budget() -> ModelBudget | None:
    return _budget.get()


@contextmanager
def model_budget_scope() -> Iterator[ModelBudget]:
    from app.conf.runtime import readonly_config_scope
    with readonly_config_scope():
        with _model_budget_scope() as budget:
            yield budget


@contextmanager
def _model_budget_scope() -> Iterator[ModelBudget]:
    current = _budget.get()
    if current is not None:
        yield current
        return
    from app.conf import harness_conf
    from app.conf.runtime import read_once
    config = read_once("harness", harness_conf)
    profile = config.agent_profile()
    value = ModelBudget(profile.max_llm_calls, profile.max_llm_tokens,
                        max_tools=profile.max_tool_calls if config.tool_call_limits_enabled else None,
                        answer_reserve_s=config.answer_reserve_s,
                        **config.budget_controller.model_dump())
    token = _budget.set(value)
    try:
        yield value
    finally:
        _budget.reset(token)
