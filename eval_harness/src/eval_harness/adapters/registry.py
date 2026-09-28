"""Thin harness → adapter registry. Default remains Claude."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .base import CaseRunResult

DEFAULT_HARNESS = "claude_code"

_CLAUDE_KEYS = {
    "case_id",
    "turns",
    "case_dir",
    "project_cwd",
    "claude_bin",
    "agent_md",
    "permission_mode",
    "dangerously_skip_permissions",
    "output_format",
    "verbose",
    "include_partial_messages",
    "mcp_config",
    "timeout_sec",
    "extra_args",
    "thinking",
    "agent_workdir_root",
    "claude_config_dir",
}


def _claude_run_case(**kwargs: Any) -> CaseRunResult:
    from .claude import run_case as claude_run_case

    filtered = {k: kwargs[k] for k in _CLAUDE_KEYS if k in kwargs}
    if kwargs.get("harness_bin"):
        filtered["claude_bin"] = kwargs["harness_bin"]
    return claude_run_case(**filtered)


def _pi_run_case(**kwargs: Any) -> CaseRunResult:
    from eval_harness.adapters.pi import run_case as pi_run_case

    return pi_run_case(
        case_id=kwargs["case_id"],
        turns=kwargs["turns"],
        case_dir=kwargs["case_dir"],
        project_cwd=kwargs["project_cwd"],
        pi_bin=kwargs.get("harness_bin") or kwargs.get("claude_bin") or "pi",
        agent_md=kwargs.get("agent_md", "agent.md"),
        timeout_sec=int(kwargs.get("timeout_sec") or 300),
        extra_args=list(kwargs.get("extra_args") or []),
        append_system_prompt=bool(kwargs.get("append_system_prompt", True)),
        provider=str(kwargs.get("provider") or "local-relay"),
        model=str(kwargs.get("model") or "deepseek-v4-flash"),
        thinking=str(kwargs.get("thinking") or "off"),
        no_builtin_tools=bool(kwargs.get("no_builtin_tools", False)),
        approve=bool(kwargs.get("approve", True)),
        agent_workdir_root=kwargs.get("agent_workdir_root"),
        pi_agent_dir=kwargs.get("pi_agent_dir"),
        pi_packages=kwargs.get("pi_packages"),
    )


def _insurance_run_batch(**kwargs: Any) -> dict[str, Any]:
    from .insurance import run_live_batch

    return run_live_batch(**kwargs)


@dataclass(frozen=True)
class Adapter:
    """Declare a per-case CLI adapter or a service's native batch adapter."""

    run_case: Callable[..., CaseRunResult] | None = None
    run_batch: Callable[..., dict[str, Any]] | None = None


REGISTRY: dict[str, Adapter] = {}


def register_adapter(
    name: str, *,
    run_case: Callable[..., CaseRunResult] | None = None,
    run_batch: Callable[..., dict[str, Any]] | None = None,
) -> None:
    if not name or name != name.strip():
        raise ValueError("Adapter name must be nonempty and have no surrounding whitespace")
    if name in REGISTRY:
        raise ValueError(f"Adapter already registered: {name}")
    if (run_case is None) == (run_batch is None):
        raise ValueError("Provide exactly one of run_case or run_batch")
    if not callable(run_case if run_case is not None else run_batch):
        raise TypeError("Adapter runner must be callable")
    REGISTRY[name] = Adapter(run_case=run_case, run_batch=run_batch)


register_adapter("claude_code", run_case=_claude_run_case)
register_adapter("pi_coding", run_case=_pi_run_case)
register_adapter("insurance_qa_agno", run_batch=_insurance_run_batch)


def resolve_harness(name: str | None) -> str:
    h = (name or DEFAULT_HARNESS).strip() or DEFAULT_HARNESS
    if h not in REGISTRY:
        raise SystemExit(f"Unknown harness={h!r}; known={sorted(REGISTRY)}")
    return h


def dispatch_run_case(harness: str | None, **kwargs: Any) -> CaseRunResult:
    h = resolve_harness(harness)
    runner = REGISTRY[h].run_case
    if runner is None:
        raise ValueError(f"{h} uses batch dispatch")
    return runner(**kwargs)


def get_adapter(harness: str | None) -> Adapter:
    return REGISTRY[resolve_harness(harness)]


def dispatch_run_batch(harness: str | None, **kwargs: Any) -> dict[str, Any]:
    h = resolve_harness(harness)
    runner = REGISTRY[h].run_batch
    if runner is None:
        raise ValueError(f"{h} uses case dispatch")
    return runner(**kwargs)
