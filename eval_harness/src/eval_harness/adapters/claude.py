"""Spawn Claude Code CLI, capture stream-json, multi-turn resume."""
from __future__ import annotations

from eval_harness.trace_identity import current_identity

import json
import re
import uuid
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any, Optional


from .base import CaseRunResult, TurnResult, concat_streams, redact_obj
from eval_harness.agent_env import (
    build_agent_env, cleanup_case_workdir, finalize_agent_env, prepare_case_workdir,
)

# Files copied from the profile's project_cwd (template) into the per-case sandbox.
# Claude reads project settings (.claude/settings.local.json: local relay URL/token, model,
# CLAUDE_CODE_ATTRIBUTION_HEADER, enabledMcpjsonServers) and .mcp.json relative to cwd.
CLAUDE_SANDBOX_FILES = (".claude/settings.local.json", ".mcp.json")

# User-scope config isolation (profile adapter.claude_config_dir):
#   "per_case" -> CLAUDE_CONFIG_DIR=<agent cwd>/.claude-config (empty, removed with the sandbox):
#                user skills (~/.claude/skills), plugins, user settings.json, CLAUDE.md, history
#                are NOT loaded; Claude Code's bundled skills/built-in tools stay. Project
#                settings (<cwd>/.claude/settings.local.json: relay URL/token) still apply.
#   "inherit"  -> legacy: whatever the parent env / ~/.claude provides.
#   <abs path> -> fixed shared config dir.
CLAUDE_CONFIG_SUBDIR = ".claude-config"


def resolve_claude_config_dir(mode: Optional[str], agent_cwd: Path) -> Optional[Path]:
    m = str(mode or "inherit").strip()
    if m in ("", "inherit", "none", "user"):
        return None
    if m == "per_case":
        d = Path(agent_cwd) / CLAUDE_CONFIG_SUBDIR
    else:
        d = Path(os.path.expanduser(m))
        if not d.is_absolute():
            raise ValueError(f"claude_config_dir must be per_case|inherit|absolute path: {mode!r}")
    d.mkdir(parents=True, exist_ok=True)
    return d


CASE_ID_HEADER = "X-Eval-Case-Id"
EXECUTION_ID_HEADER = "X-Eval-Execution-Id"


def case_id_injection_env(
    case_id: str | None,
    base_env: dict[str, str] | None = None,
    *,
    execution_id: str | None = None,
    thinking: bool | None = None,
) -> dict[str, str]:
    """Stamp X-Eval-Case-Id / X-Eval-Execution-Id / optional X-Eval-Thinking.

    Mirrors Pi: when case_id is set, mint a unique execution_id unless the caller
    passes one. Also sets EVAL_EXECUTION_ID in the child env for meta recovery
    (wire delivery is still the Anthropic custom header).
    thinking=True → X-Eval-Thinking: on (relay opt-in); default/None → omit
    (relay forces thinking off).
    """
    from eval_harness.relay_inject import anthropic_cli_env

    if not case_id:
        return dict(base_env if base_env is not None else os.environ)
    eid = execution_id or uuid.uuid4().hex
    env = anthropic_cli_env(case_id, base_env, execution_id=eid, thinking=thinking)
    env["EVAL_EXECUTION_ID"] = eid
    return env


def encode_project_dir(cwd: str) -> str:
    # Claude Code maps every non-alphanumeric char ("/", "_", ".") to "-"
    # (e.g. ~/eval_sandbox/... -> -Users-x-eval-sandbox-...).
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def find_session_transcript(
    cwd: str, session_id: str, config_dir: Optional[Path] = None
) -> Optional[Path]:
    if not session_id:
        return None
    encoded = encode_project_dir(cwd)
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    root = base / "projects" / encoded
    candidates = [
        root / f"{session_id}.jsonl",
        root / "sessions" / f"{session_id}.jsonl",
    ]
    for c in candidates:
        if c.is_file():
            return c
    if root.is_dir():
        hits = list(root.rglob(f"*{session_id}*.jsonl"))
        if hits:
            return hits[0]
    return None


def build_claude_argv(
    *,
    claude_bin: str,
    prompt: str,
    agent_md_path: Path,
    permission_mode: str,
    dangerously_skip_permissions: bool,
    output_format: str,
    verbose: bool,
    include_partial_messages: bool,
    mcp_config: Optional[str],
    session_id: Optional[str],
    extra_args: Optional[list[str]] = None,
    case_id: Optional[str] = None,
) -> list[str]:
    argv = [claude_bin, "-p", prompt]
    if agent_md_path.is_file():
        sys_prompt = agent_md_path.read_text(encoding="utf-8")
        if case_id:
            # Text fallback (Phase 2 still keeps it): gateway prefers header/body when present.
            sys_prompt = sys_prompt.rstrip() + f"\n\n<!--eval_case_id:{case_id}-->\n"
        argv.extend(["--append-system-prompt", sys_prompt])
    if permission_mode:
        argv.extend(["--permission-mode", permission_mode])
    if dangerously_skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if output_format:
        argv.extend(["--output-format", output_format])
    if verbose:
        argv.append("--verbose")
    if include_partial_messages:
        argv.append("--include-partial-messages")
    if mcp_config:
        argv.extend(["--mcp-config", mcp_config])
    if session_id:
        argv.extend(["--resume", session_id])
    if extra_args:
        argv.extend(extra_args)
    return argv


def _iter_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    if not path.is_file():
        return events
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"type": "parse_error", "raw": line[:2000]})
    return events


def _extract_session_id(events: list[dict]) -> Optional[str]:
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result" and ev.get("session_id"):
            return ev["session_id"]
        if ev.get("session_id"):
            return ev["session_id"]
        for key in ("result", "data", "message"):
            nested = ev.get(key)
            if isinstance(nested, dict) and nested.get("session_id"):
                return nested["session_id"]
    return None


def _extract_final_text(events: list[dict]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "result":
            r = ev.get("result")
            if isinstance(r, str) and r.strip():
                return r
            if isinstance(r, dict):
                for k in ("result", "text", "content"):
                    if isinstance(r.get(k), str) and r[k].strip():
                        return r[k]
    chunks: list[str] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    chunks.append(block["text"])
        elif isinstance(content, str):
            chunks.append(content)
    return "\n".join(chunks).strip()


def _detect_first_frame(events: list[dict], t0: float, line_ts: list[float]) -> tuple[Optional[int], Optional[str]]:
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        kind = None
        et = ev.get("type")
        if et in ("stream_event", "partial", "content_block_delta"):
            inner = ev.get("event") if isinstance(ev.get("event"), dict) else ev
            if isinstance(inner, dict):
                if inner.get("type") == "content_block_start":
                    cb = inner.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        kind = "tool_use"
                    elif cb.get("type") == "text":
                        kind = "text"
                if inner.get("type") == "content_block_delta":
                    d = inner.get("delta") or {}
                    if d.get("type") == "text_delta":
                        kind = "text"
            delta = ev.get("delta") or {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                kind = "text"
        if et == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        kind = "tool_use"
                        break
                    if block.get("type") == "text" and (block.get("text") or "").strip():
                        kind = "text"
                        break
        if et == "tool_use":
            kind = kind or "tool_use"
        if kind:
            ts = line_ts[i] if i < len(line_ts) else time.time()
            return int((ts - t0) * 1000), kind
    return None, None


def _extract_cost_api(events: list[dict]) -> tuple[Optional[float], Optional[int]]:
    cost = api_ms = None
    for ev in events:
        if ev.get("type") != "result":
            continue
        if ev.get("total_cost_usd") is not None:
            try:
                cost = float(ev["total_cost_usd"])
            except (TypeError, ValueError):
                pass
        if ev.get("duration_ms") is not None:
            try:
                api_ms = int(ev["duration_ms"])
            except (TypeError, ValueError):
                pass
    return cost, api_ms



def _stream_has_result(events: list[dict]) -> bool:
    return any(ev.get("type") == "result" for ev in events)


def _kill_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _read_stdout_with_timeouts(
    proc: subprocess.Popen,
    out_f,
    *,
    timeout_sec: int,
    idle_timeout_sec: float,
) -> tuple[list[float], int, Optional[str]]:
    """Read proc.stdout until EOF, overall timeout, or idle timeout.

    Returns (line_timestamps, exit_code, error).
    """
    assert proc.stdout is not None
    line_ts: list[float] = []
    error: Optional[str] = None
    t0 = time.time()
    last_data = t0
    while True:
        now = time.time()
        if now - t0 >= timeout_sec:
            _kill_proc(proc)
            return line_ts, -9, f"timeout after {timeout_sec}s (overall)"
        if line_ts and now - last_data >= idle_timeout_sec:
            _kill_proc(proc)
            return line_ts, -9, f"idle timeout after {idle_timeout_sec:.0f}s with no new stdout"
        # wait at most 1s so we re-check overall/idle bounds promptly
        wait = 1.0
        if line_ts:
            wait = min(wait, max(0.05, idle_timeout_sec - (now - last_data)))
        wait = min(wait, max(0.05, timeout_sec - (now - t0)))
        r, _, _ = select.select([proc.stdout], [], [], wait)
        if r:
            line = proc.stdout.readline()
            if line == "":
                try:
                    exit_code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _kill_proc(proc)
                    return line_ts, -9, "stdout EOF but process did not exit"
                return line_ts, exit_code, error
            line_ts.append(time.time())
            last_data = line_ts[-1]
            out_f.write(line if line.endswith("\n") else line + "\n")
            out_f.flush()
        else:
            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    for ln in rest.splitlines(keepends=True):
                        line_ts.append(time.time())
                        out_f.write(ln if ln.endswith("\n") else ln + "\n")
                    out_f.flush()
                code = proc.returncode if proc.returncode is not None else 0
                return line_ts, code, error


def run_turn(
    *,
    prompt: str,
    turn_index: int,
    case_dir: Path,
    project_cwd: Path,
    claude_bin: str,
    agent_md: str,
    permission_mode: str,
    dangerously_skip_permissions: bool,
    output_format: str,
    verbose: bool,
    include_partial_messages: bool,
    mcp_config: Optional[str],
    session_id: Optional[str],
    timeout_sec: int,
    extra_args: Optional[list[str]] = None,
    case_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    thinking: bool | None = None,
    claude_config_dir: Optional[Path] = None,
) -> TurnResult:
    case_dir.mkdir(parents=True, exist_ok=True)
    stream_path = case_dir / f"stream_turn{turn_index}.jsonl"
    agent_md_path = project_cwd / agent_md
    argv = build_claude_argv(
        claude_bin=claude_bin,
        prompt=prompt,
        agent_md_path=agent_md_path,
        permission_mode=permission_mode,
        dangerously_skip_permissions=dangerously_skip_permissions,
        output_format=output_format,
        verbose=verbose,
        include_partial_messages=include_partial_messages,
        mcp_config=mcp_config,
        session_id=session_id,
        extra_args=extra_args,
        case_id=case_id,
    )
    meta = {
        "turn": turn_index,
        "claude_bin": claude_bin,
        "cwd": str(project_cwd),
        "resume": session_id,
        "argv_flags": [a for a in argv if a != prompt and len(a) <= 200],
        "prompt_preview": prompt[:500],
        "case_id": case_id,
        "execution_id": execution_id,
        "claude_config_dir": str(claude_config_dir) if claude_config_dir else "inherit",
    }

    t0 = time.time()
    line_ts: list[float] = []
    error: Optional[str] = None
    exit_code = -1
    try:
        with stream_path.open("w", encoding="utf-8") as out_f, open(os.devnull, "r") as devnull:
            # Layer-1: explicit allowlist instead of os.environ.copy() (see agent_env.py).
            base_env = build_agent_env("claude")
            if claude_config_dir:
                base_env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
            child_env = case_id_injection_env(
                case_id, base_env, execution_id=execution_id, thinking=thinking,
            )
            finalize_agent_env(child_env)  # raises if any value equals an upstream secret
            meta["env_var_names"] = sorted(child_env)
            if case_id and not execution_id:
                execution_id = child_env.get("EVAL_EXECUTION_ID")
                meta["execution_id"] = execution_id
            meta["case_id"] = case_id
            hdrs = child_env.get("ANTHROPIC_CUSTOM_HEADERS", "")
            meta["case_id_injection"] = {
                "header": bool(case_id),
                "execution_header": bool(
                    case_id and EXECUTION_ID_HEADER.lower() + ":" in hdrs.lower()
                ),
                "extra_body": bool(case_id),
                "text_marker": bool(case_id and agent_md_path.is_file()),
                "EVAL_EXECUTION_ID": bool(str(child_env.get("EVAL_EXECUTION_ID") or "").strip()),
            }
            (case_dir / f"argv_turn{turn_index}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            proc = subprocess.Popen(
                argv,
                cwd=str(project_cwd),
                stdin=devnull,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=child_env,
            )
            assert proc.stdout is not None
            idle_timeout_sec = float(os.environ.get("EVAL_IDLE_TIMEOUT_SEC", "45"))
            line_ts, exit_code, error = _read_stdout_with_timeouts(
                proc,
                out_f,
                timeout_sec=int(timeout_sec),
                idle_timeout_sec=idle_timeout_sec,
            )
    except FileNotFoundError as e:
        error = f"claude binary not found: {e}"
        exit_code = 127
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        exit_code = -1

    wall_ms = int((time.time() - t0) * 1000)
    events = _iter_jsonl(stream_path)
    sid = _extract_session_id(events) or session_id
    final_text = _extract_final_text(events)
    first_frame_ms, first_frame_kind = _detect_first_frame(events, t0, line_ts)
    cost_usd, api_ms = _extract_cost_api(events)

    if not _stream_has_result(events) and error is None:
        error = "incomplete stream: no result event"
        if exit_code == 0:
            exit_code = -2

    for ev in events:
        if ev.get("type") == "result" and ev.get("is_error"):
            err_text = ev.get("result") or ev.get("error") or "is_error=true"
            if isinstance(err_text, str):
                error = error or err_text[:2000]
            else:
                error = error or json.dumps(err_text, ensure_ascii=False)[:2000]
    if exit_code != 0 and not error:
        for ev in events:
            if ev.get("type") == "parse_error":
                raw = str(ev.get("raw", ""))
                if any(x in raw.lower() for x in ("402", "401", "403", "auth", "quota", "credit")):
                    error = raw[:2000]
                    break
        if not error:
            error = f"non-zero exit_code={exit_code}"

    return TurnResult(
        index=turn_index,
        prompt=prompt,
        exit_code=exit_code,
        stream_path=stream_path,
        session_id=sid,
        final_text=final_text,
        wall_ms=wall_ms,
        first_frame_ms=first_frame_ms,
        first_frame_kind=first_frame_kind,
        raw_events=events,
        error=error,
        cost_usd=cost_usd,
        api_ms=api_ms,
    )


def run_case(
    *,
    case_id: str,
    turns: list[str],
    case_dir: Path,
    project_cwd: Path,
    claude_bin: str,
    agent_md: str,
    permission_mode: str,
    dangerously_skip_permissions: bool,
    output_format: str,
    verbose: bool,
    include_partial_messages: bool,
    mcp_config: Optional[str],
    timeout_sec: int,
    extra_args: Optional[list[str]] = None,
    thinking: str | bool | None = None,
    agent_workdir_root: Optional[str] = None,
    claude_config_dir: Optional[str] = None,
) -> CaseRunResult:
    case_dir.mkdir(parents=True, exist_ok=True)
    execution_id = current_identity().get("execution_id") or uuid.uuid4().hex
    # Layer-1: agent cwd = <agent_workdir_root>/claude/<execution_id>/ (outside the repo);
    # profile project_cwd is only the template for agent.md/.mcp.json/.claude settings.
    template_cwd = Path(project_cwd)
    sandbox_files = [agent_md, *CLAUDE_SANDBOX_FILES]
    if mcp_config and not os.path.isabs(mcp_config):
        sandbox_files.append(mcp_config)
    project_cwd = prepare_case_workdir(
        harness="claude", execution_id=execution_id, template_dir=template_cwd,
        files=dict.fromkeys(sandbox_files), root=agent_workdir_root,
    )
    workdir_root = project_cwd.parent.parent
    cfg_dir = resolve_claude_config_dir(claude_config_dir, project_cwd)
    (case_dir / "agent_workdir.json").write_text(
        json.dumps(
            {
                "template_cwd": str(template_cwd),
                "agent_cwd": str(project_cwd),
                "claude_config_dir": str(cfg_dir) if cfg_dir else "inherit",
                "files": sorted(str(p.relative_to(project_cwd)) for p in project_cwd.rglob("*") if p.is_file()),
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    try:
        return _run_case_in(
            case_id=case_id, turns=turns, case_dir=case_dir, project_cwd=project_cwd,
            claude_bin=claude_bin, agent_md=agent_md, permission_mode=permission_mode,
            dangerously_skip_permissions=dangerously_skip_permissions, output_format=output_format,
            verbose=verbose, include_partial_messages=include_partial_messages, mcp_config=mcp_config,
            timeout_sec=timeout_sec, extra_args=extra_args, thinking=thinking, execution_id=execution_id,
            claude_config_dir=cfg_dir,
        )
    finally:
        cleanup_case_workdir(project_cwd, workdir_root)


def _run_case_in(
    *,
    case_id: str,
    turns: list[str],
    case_dir: Path,
    project_cwd: Path,
    claude_bin: str,
    agent_md: str,
    permission_mode: str,
    dangerously_skip_permissions: bool,
    output_format: str,
    verbose: bool,
    include_partial_messages: bool,
    mcp_config: Optional[str],
    timeout_sec: int,
    extra_args: Optional[list[str]],
    thinking: str | bool | None,
    execution_id: str,
    claude_config_dir: Optional[Path] = None,
) -> CaseRunResult:
    # Relay thinking opt-in: only "on"/True set the header; default/off omit (relay OFF).
    thinking_optin: bool | None = None
    if thinking is True or (isinstance(thinking, str) and thinking.strip().lower() in ("on", "true", "1", "adaptive", "enabled")):
        thinking_optin = True
    elif isinstance(thinking, str) and thinking.strip().isdigit() and int(thinking.strip()) > 0:
        thinking_optin = True
    (case_dir / "prompt_turns.json").write_text(
        json.dumps(
            {
                "case_id": case_id,
                "execution_id": execution_id,
                "turns": [{"index": i + 1, "prompt": t} for i, t in enumerate(turns)],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (case_dir / "execution_id.txt").write_text(execution_id + "\n", encoding="utf-8")
    session_id: Optional[str] = None
    turn_results: list[TurnResult] = []
    t0 = time.time()
    overall_error: Optional[str] = None
    last_exit = 0
    max_attempts = int(os.environ.get("EVAL_TURN_MAX_ATTEMPTS", "2"))  # 1 try + 1 retry
    for i, prompt in enumerate(turns, start=1):
        tr: Optional[TurnResult] = None
        for attempt in range(1, max_attempts + 1):
            # On retry, write to a distinct stream file then copy over on success path via turn_index name;
            # run_turn always writes stream_turn{i}.jsonl — archive previous attempt first.
            prev = case_dir / f"stream_turn{i}.jsonl"
            if attempt > 1 and prev.is_file():
                archive = case_dir / f"stream_turn{i}_attempt{attempt-1}.jsonl"
                try:
                    archive.write_text(prev.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                except Exception:
                    pass
                print(f"[retry] {case_id} turn{i} attempt={attempt}/{max_attempts} after: {tr.error if tr else '?'}", flush=True)
            tr = run_turn(
                prompt=prompt,
                turn_index=i,
                case_dir=case_dir,
                project_cwd=project_cwd,
                claude_bin=claude_bin,
                agent_md=agent_md,
                permission_mode=permission_mode,
                dangerously_skip_permissions=dangerously_skip_permissions,
                output_format=output_format,
                verbose=verbose,
                include_partial_messages=include_partial_messages,
                mcp_config=mcp_config,
                session_id=session_id,
                timeout_sec=timeout_sec,
                extra_args=extra_args,
                case_id=case_id,
                execution_id=execution_id,
                thinking=thinking_optin,
                claude_config_dir=claude_config_dir,
            )
            incomplete = (not _stream_has_result(tr.raw_events)) or (
                tr.error is not None and ("idle timeout" in (tr.error or "") or "incomplete stream" in (tr.error or "") or "timeout after" in (tr.error or ""))
            )
            if incomplete and attempt < max_attempts:
                continue
            break
        assert tr is not None
        turn_results.append(tr)
        last_exit = tr.exit_code
        if tr.session_id:
            session_id = tr.session_id
        if tr.error and overall_error is None:
            overall_error = f"turn{i}: {tr.error}"
        if tr.exit_code != 0 or not _stream_has_result(tr.raw_events):
            break
    concat_streams(case_dir, len(turn_results))
    transcript_path = None
    if session_id:
        tp = find_session_transcript(str(project_cwd), session_id, claude_config_dir)
        if tp:
            dest = case_dir / "session_transcript.jsonl"
            try:
                dest.write_text(tp.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                transcript_path = dest
            except OSError:
                transcript_path = tp
    success = last_exit == 0 and overall_error is None and bool(turn_results)
    return CaseRunResult(
        case_id=case_id,
        session_id=session_id,
        turns=turn_results,
        success=success,
        exit_code=last_exit,
        error=overall_error,
        wall_ms=int((time.time() - t0) * 1000),
        transcript_path=transcript_path,
    )
