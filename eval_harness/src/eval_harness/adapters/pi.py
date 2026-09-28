"""Spawn Pi CLI (print + JSON mode + tools + MCP), capture JSONL, multi-turn session."""
from __future__ import annotations

import json
import re
import shutil
import uuid
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .base import CaseRunResult, TurnResult, concat_streams

from eval_harness.paths import PROJECT_ROOT, WORKSPACES_ROOT
from eval_harness.agent_env import (
    PI_PROVIDER_KEY_ENV, build_agent_env, cleanup_case_workdir, finalize_agent_env,
    load_local_gateway_keys, prepare_case_workdir,
)

# Files copied from the profile's project_cwd (template) into the per-case sandbox:
# agent.md (--append-system-prompt) and project .mcp.json (pi-mcp-adapter "shared-project").
PI_SANDBOX_FILES = (".mcp.json",)

# ---------------------------------------------------------------------------------------------
# User-scope config isolation (profile adapter.pi_agent_dir), mirrors Claude's claude_config_dir:
#   "inherit"  -> legacy: PI_CODING_AGENT_DIR from the parent env or ~/.pi/agent (user skills in
#                ~/.agents/skills, ~/.pi/agent/extensions/*, APPEND_SYSTEM.md, AGENTS.md, packages,
#                sessions written to ~/.pi/agent/sessions).
#   "per_case" -> PI_CODING_AGENT_DIR=<agent cwd>/.pi-agent (removed with the sandbox) containing ONLY:
#                  models.json   = the ONE provider block the profile uses, copied from the source
#                                  models.json; apiKey must be an env reference ($VAR) — literal keys
#                                  are refused, so no key material is ever written.
#                  settings.json = {"packages": [pi-mcp-adapter local path], "retry": <source retry>}
#                plus CLI flags --no-skills --no-prompt-templates --no-themes --no-context-files.
#                Pi builtin tools and the insurance-tools MCP (project .mcp.json via pi-mcp-adapter)
#                stay. Session file is copied to <case_dir>/session_transcript.jsonl.
#   <abs path> -> fixed shared agent dir prepared the same way.
PI_AGENT_SUBDIR = ".pi-agent"
PI_ISOLATION_FLAGS = ("--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files")
DEFAULT_PI_PACKAGES = ("~/.pi/agent/npm/node_modules/pi-mcp-adapter",)
PI_SETTINGS_KEEP = ("retry",)
_ENV_REF_RE = re.compile(r"^\$?\{?[A-Za-z_][A-Za-z0-9_]*\}?$")
_LITERAL_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


class PiAgentDirError(RuntimeError):
    """Per-case Pi agent dir could not be prepared safely."""


def source_pi_agent_dir() -> Path:
    raw = str(os.environ.get("PI_CODING_AGENT_DIR") or "").strip()
    return Path(os.path.expanduser(raw)) if raw else Path.home() / ".pi" / "agent"


def strip_jsonc(text: str) -> str:
    """JSON5-lite -> JSON: drop // and /* */ comments and trailing commas (string-aware)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            i += 1; continue
        if c == '"':
            in_str = True; out.append(c); i += 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        out.append(c); i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_jsonc(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(strip_jsonc(path.read_text(encoding="utf-8", errors="replace")))
    return data if isinstance(data, dict) else {}


def _assert_no_literal_secrets(obj: Any, where: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ("apikey", "api_key", "key", "token", "authorization") and isinstance(v, str):
                if not _ENV_REF_RE.match(v.strip()):
                    raise PiAgentDirError(f"{where}.{k} is not an env reference ($VAR); refusing to copy a literal key")
            _assert_no_literal_secrets(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_no_literal_secrets(v, f"{where}[{i}]")
    elif isinstance(obj, str) and _LITERAL_SECRET_RE.search(obj):
        raise PiAgentDirError(f"{where} looks like a literal secret; refusing to copy")


def resolve_pi_agent_dir(
    mode: Optional[str],
    agent_cwd: Path,
    *,
    provider: str,
    packages: Optional[list[str]] = None,
    source_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Return the isolated agent dir (prepared), or None for legacy "inherit"."""
    m = str(mode or "inherit").strip()
    if m in ("", "inherit", "none", "user"):
        return None
    if m == "per_case":
        d = Path(agent_cwd) / PI_AGENT_SUBDIR
    else:
        d = Path(os.path.expanduser(m))
        if not d.is_absolute():
            raise ValueError(f"pi_agent_dir must be per_case|inherit|absolute path: {mode!r}")
    src = Path(source_dir) if source_dir else source_pi_agent_dir()
    if d.resolve() == src.resolve():
        raise PiAgentDirError(f"isolated pi agent dir must differ from the source dir {src}")
    models = _load_jsonc(src / "models.json")
    block = (models.get("providers") or {}).get(provider)
    if not isinstance(block, dict):
        raise PiAgentDirError(f"provider {provider!r} not found in {src / 'models.json'}")
    _assert_no_literal_secrets(block, f"providers.{provider}")
    settings_src = _load_jsonc(src / "settings.json")
    pkgs: list[str] = []
    for raw in (packages if packages is not None else list(DEFAULT_PI_PACKAGES)):
        pth = Path(os.path.expanduser(str(raw)))
        if not pth.exists():
            raise PiAgentDirError(f"pi package path not found: {pth}")
        pkgs.append(str(pth.resolve()))
    settings = {k: settings_src[k] for k in PI_SETTINGS_KEEP if k in settings_src}
    settings["packages"] = pkgs
    d.mkdir(parents=True, exist_ok=True)
    (d / "models.json").write_text(
        json.dumps({"providers": {provider: block}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (d / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return d


def _pi_session_dir_name(cwd: Path) -> str:
    return "--" + str(cwd).strip("/").replace("/", "-") + "--"


def save_pi_session_transcript(
    case_dir: Path, session_id: Optional[str], agent_dir: Optional[Path], agent_cwd: Path
) -> Optional[Path]:
    """Copy Pi's session file (read-only on the source) to <case_dir>/session_transcript.jsonl."""
    if not session_id:
        return None
    base = (agent_dir or source_pi_agent_dir()) / "sessions"
    cands = sorted((base / _pi_session_dir_name(agent_cwd)).glob(f"*{session_id}*.jsonl"))
    if not cands and agent_dir is not None:
        cands = sorted(base.rglob(f"*{session_id}*.jsonl"))
    if not cands:
        return None
    dest = case_dir / "session_transcript.jsonl"
    try:
        shutil.copyfile(cands[-1], dest)
    except OSError:
        return None
    return dest

DEFAULT_PI_BIN = "pi"
DEFAULT_PROVIDER = "local-relay"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_GATEWAY_ENV = PROJECT_ROOT / "llm_gateway" / ".env"


def load_dotenv_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; do not print values."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k:
            out[k] = v
    return out


def case_id_injection_env(
    case_id: str | None,
    base_env: dict[str, str] | None = None,
    *,
    execution_id: str | None = None,
    thinking: str | bool | None = None,
    local_key_names: Optional[tuple] = ("LITELLM_MASTER_KEY", "INSURANCE_LITELLM_MASTER_KEY"),
) -> dict[str, str]:
    """Stamp PI_EVAL_* for local-relay headers; load gateway .env keys.

    PI_EVAL_THINKING=on when thinking is an opt-in value; models.json maps it to
    X-Eval-Thinking. Default/off omits the env so relay keeps thinking off.
    """
    env: dict[str, str] = dict(base_env if base_env is not None else os.environ)
    # Layer-1: only the LOCAL relay key(s) are filled in (never UPSTREAM_* / provider keys).
    # ``local_key_names`` restricts to the key the chosen provider references in models.json.
    for k, v in load_local_gateway_keys(DEFAULT_GATEWAY_ENV.parent).items():
        if local_key_names is not None and k not in local_key_names:
            continue
        if k not in env or not str(env.get(k) or "").strip():
            env[k] = v
    if case_id:
        env["PI_EVAL_CASE_ID"] = case_id
        # Unique execution id per case run (caller may pass one; otherwise mint once here).
        env["PI_EVAL_EXECUTION_ID"] = execution_id or uuid.uuid4().hex
    # Thinking opt-in for relay (separate from Pi CLI --thinking flag).
    optin = False
    if thinking is True:
        optin = True
    elif isinstance(thinking, str) and thinking.strip().lower() not in ("", "off", "false", "0", "none"):
        optin = True
    # Pi models.json always references $PI_EVAL_THINKING; unset env → hard fail.
    # Relay only treats literal "on" as opt-in; "off"/"" keep thinking default-off.
    env["PI_EVAL_THINKING"] = "on" if optin else "off"
    return env


def build_pi_argv(
    *,
    pi_bin: str,
    prompt: str,
    agent_md_path: Path,
    provider: str,
    model: str,
    thinking: str,
    no_builtin_tools: bool,
    approve: bool,
    session_id: Optional[str],
    append_system_prompt: bool,
    extra_args: Optional[list[str]] = None,
    isolation_flags: Optional[list[str]] = None,
) -> list[str]:
    argv = [
        pi_bin,
        "-p",
        "--mode",
        "json",
        "--provider",
        provider,
        "--model",
        model,
        "--thinking",
        thinking,
    ]
    if no_builtin_tools:
        argv.append("--no-builtin-tools")
    if approve:
        argv.append("--approve")
    if isolation_flags:
        argv.extend(isolation_flags)
    if append_system_prompt and agent_md_path.is_file():
        # Pi accepts file path or text for --append-system-prompt
        argv.extend(["--append-system-prompt", str(agent_md_path)])
    if session_id:
        argv.extend(["--session", session_id])
    if extra_args:
        argv.extend(extra_args)
    argv.append(prompt)
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
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "session" and ev.get("id"):
            return str(ev["id"])
        if ev.get("session_id"):
            return str(ev["session_id"])
        msg = ev.get("message")
        if isinstance(msg, dict) and msg.get("session_id"):
            return str(msg["session_id"])
    return None


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    chunks.append(str(block["text"]))
                elif isinstance(block.get("text"), str):
                    chunks.append(block["text"])
        return "\n".join(chunks).strip()
    return ""


def _extract_final_text(events: list[dict]) -> str:
    # Prefer last assistant message_end / message in agent_end
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "message_end":
            msg = ev.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                t = _message_text(msg)
                if t:
                    return t
        if ev.get("type") == "agent_end":
            msgs = ev.get("messages") or []
            if isinstance(msgs, list):
                for msg in reversed(msgs):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        t = _message_text(msg)
                        if t:
                            return t
        if ev.get("type") == "turn_end":
            msg = ev.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                t = _message_text(msg)
                if t:
                    return t
    return ""


def _detect_first_frame(events: list[dict], t0: float, line_ts: list[float]) -> tuple[Optional[int], Optional[str]]:
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        kind = None
        et = ev.get("type")
        if et == "tool_execution_start":
            kind = "tool_use"
        elif et == "message_update":
            ame = ev.get("assistantMessageEvent") or {}
            if isinstance(ame, dict) and ame.get("type") == "text_delta":
                kind = "text"
        elif et == "message_end":
            msg = ev.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                # tool vs text
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") in ("toolCall", "tool_use", "functionCall"):
                            kind = "tool_use"
                            break
                    else:
                        if _message_text(msg):
                            kind = "text"
                elif _message_text(msg):
                    kind = "text"
        if kind:
            ts = line_ts[i] if i < len(line_ts) else time.time()
            return int((ts - t0) * 1000), kind
    return None, None


def pi_events_to_claude_like(raw: list[dict]) -> list[dict]:
    """Map Pi --mode json events into Claude-like shapes for normalize_trace._events_from_turn."""
    out: list[dict] = []
    session_id = _extract_session_id(raw)

    for ev in raw:
        if not isinstance(ev, dict):
            continue
        et = ev.get("type")
        if et == "session":
            out.append({"type": "system", "subtype": "session", "session_id": ev.get("id"), "raw": {"cwd": ev.get("cwd")}})
            continue
        if et == "tool_execution_start":
            tname = ev.get("toolName")
            args = ev.get("args")
            # Prefer canonical insurance tool when called via mcp proxy
            if isinstance(args, dict) and args.get("tool"):
                tname = str(args.get("tool"))
            out.append(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": ev.get("toolCallId"),
                                "name": tname,
                                "input": args,
                            }
                        ],
                    },
                }
            )
            continue
        if et == "tool_execution_end":
            result = ev.get("result")
            out.append(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": ev.get("toolCallId"),
                                "content": result,
                                "is_error": bool(ev.get("isError")),
                            }
                        ],
                    },
                }
            )
            continue
        if et == "message_end":
            msg = ev.get("message") or {}
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            # Skip pure tool-result user messages already covered; keep assistant text
            if role == "assistant":
                blocks: list[dict] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            if isinstance(block, str) and block.strip():
                                blocks.append({"type": "text", "text": block})
                            continue
                        bt = block.get("type")
                        if bt in ("text", None) and (block.get("text") or bt == "text"):
                            if block.get("text"):
                                blocks.append({"type": "text", "text": block.get("text")})
                        elif bt in ("toolCall", "tool_use", "functionCall"):
                            blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": block.get("id") or block.get("toolCallId"),
                                    "name": block.get("name") or block.get("toolName"),
                                    "input": block.get("input") or block.get("arguments") or block.get("args"),
                                }
                            )
                        elif bt == "thinking":
                            blocks.append({"type": "thinking", "thinking": block.get("thinking") or block.get("text") or ""})
                elif isinstance(content, str) and content.strip():
                    blocks.append({"type": "text", "text": content})
                if blocks:
                    out.append({"type": "assistant", "message": {"role": "assistant", "content": blocks}})
            continue
        if et == "message_update":
            ame = ev.get("assistantMessageEvent") or {}
            if isinstance(ame, dict) and ame.get("type") == "text_delta":
                out.append(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": ame.get("delta") or ""},
                        },
                    }
                )
            continue
        if et == "parse_error":
            out.append(ev)
            continue
        if et in ("agent_start", "agent_end", "turn_start", "turn_end"):
            # turn_end may carry final assistant message — already handled via message_end usually
            continue

    # Synthetic result so Claude-style completeness checks pass.
    # If Pi reported errorMessage (e.g. auth/provider failure) mark is_error.
    final_text = _extract_final_text(raw)
    stream_err = _extract_stream_error(raw)
    out.append(
        {
            "type": "result",
            "session_id": session_id,
            "is_error": bool(stream_err),
            "result": (stream_err[:2000] if stream_err else final_text),
        }
    )
    return out



def _extract_stream_error(raw: list[dict]) -> str | None:
    """Return first non-empty Pi message errorMessage, if any."""
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message")
        if isinstance(msg, dict):
            err = msg.get("errorMessage")
            if isinstance(err, str) and err.strip():
                return err.strip()
        msgs = ev.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                err = m.get("errorMessage")
                if isinstance(err, str) and err.strip():
                    return err.strip()
        err = ev.get("errorMessage")
        if isinstance(err, str) and err.strip():
            return err.strip()
    return None


def _stream_has_result(events: list[dict]) -> bool:
    return any(isinstance(ev, dict) and ev.get("type") == "result" for ev in events)


def _pi_stream_complete(raw: list[dict]) -> bool:
    """Pi print mode is done when agent_end appears (or we synthesized result after parse)."""
    return any(isinstance(ev, dict) and ev.get("type") == "agent_end" for ev in raw) or any(
        isinstance(ev, dict) and ev.get("type") == "session" for ev in raw
    ) and any(isinstance(ev, dict) and ev.get("type") in ("message_end", "turn_end", "agent_end") for ev in raw)


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
    pi_bin: str,
    agent_md: str,
    provider: str,
    model: str,
    thinking: str,
    no_builtin_tools: bool,
    approve: bool,
    append_system_prompt: bool,
    session_id: Optional[str],
    timeout_sec: int,
    extra_args: Optional[list[str]] = None,
    case_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    pi_agent_dir: Optional[Path] = None,
) -> TurnResult:
    case_dir.mkdir(parents=True, exist_ok=True)
    stream_path = case_dir / f"stream_turn{turn_index}.jsonl"
    agent_md_path = project_cwd / agent_md
    argv = build_pi_argv(
        pi_bin=pi_bin,
        prompt=prompt,
        agent_md_path=agent_md_path,
        provider=provider,
        model=model,
        thinking=thinking,
        no_builtin_tools=no_builtin_tools,
        approve=approve,
        session_id=session_id,
        append_system_prompt=append_system_prompt,
        extra_args=extra_args,
        isolation_flags=list(PI_ISOLATION_FLAGS) if pi_agent_dir else None,
    )
    meta = {
        "turn": turn_index,
        "pi_bin": pi_bin,
        "cwd": str(project_cwd),
        "session": session_id,
        "argv_flags": [a for a in argv if a != prompt and len(a) <= 200],
        "prompt_preview": prompt[:500],
        "case_id": case_id,
        "execution_id": execution_id,
        "provider": provider,
        "model": model,
        "pi_agent_dir": str(pi_agent_dir) if pi_agent_dir else "inherit",
    }

    t0 = time.time()
    line_ts: list[float] = []
    error: Optional[str] = None
    exit_code = -1
    try:
        with stream_path.open("w", encoding="utf-8") as out_f, open(os.devnull, "r") as devnull:
            # Layer-1: explicit allowlist instead of os.environ.copy() (see agent_env.py).
            key_env = PI_PROVIDER_KEY_ENV.get(provider)
            child_env = case_id_injection_env(
                case_id, build_agent_env("pi"), execution_id=execution_id, thinking=thinking,
                local_key_names=(key_env,) if key_env else None,
            )
            if pi_agent_dir:
                child_env["PI_CODING_AGENT_DIR"] = str(pi_agent_dir)
            finalize_agent_env(child_env)  # raises if any value equals an upstream secret
            meta["env_var_names"] = sorted(child_env)
            if case_id and not execution_id:
                execution_id = child_env.get("PI_EVAL_EXECUTION_ID")
                meta["execution_id"] = execution_id
            meta["case_id_injection"] = {
                "PI_EVAL_CASE_ID": bool(case_id),
                "PI_EVAL_EXECUTION_ID": bool(str(child_env.get("PI_EVAL_EXECUTION_ID") or "").strip()),
                "PI_EVAL_THINKING": child_env.get("PI_EVAL_THINKING"),
                "LITELLM_MASTER_KEY_present": bool(str(child_env.get("LITELLM_MASTER_KEY") or "").strip()),
                "INSURANCE_LITELLM_MASTER_KEY_present": bool(
                    str(child_env.get("INSURANCE_LITELLM_MASTER_KEY") or "").strip()
                ),
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
            idle_timeout_sec = float(os.environ.get("EVAL_IDLE_TIMEOUT_SEC", "120"))
            line_ts, exit_code, error = _read_stdout_with_timeouts(
                proc,
                out_f,
                timeout_sec=int(timeout_sec),
                idle_timeout_sec=idle_timeout_sec,
            )
    except FileNotFoundError as e:
        error = f"pi binary not found: {e}"
        exit_code = 127
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        exit_code = -1

    wall_ms = int((time.time() - t0) * 1000)
    raw_pi = _iter_jsonl(stream_path)
    sid = _extract_session_id(raw_pi) or session_id
    final_text = _extract_final_text(raw_pi)
    first_frame_ms, first_frame_kind = _detect_first_frame(raw_pi, t0, line_ts)
    mapped = pi_events_to_claude_like(raw_pi)

    stream_err = _extract_stream_error(raw_pi)
    if stream_err and error is None:
        error = stream_err[:2000]

    # Persist both raw Pi stream (already in stream_turn) and mapped view for debugging
    (case_dir / f"stream_turn{turn_index}_mapped.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in mapped) + ("\n" if mapped else ""),
        encoding="utf-8",
    )

    complete = _pi_stream_complete(raw_pi)
    if not complete and error is None:
        # Allow exit 0 with mapped result if we got any assistant text or tools
        if final_text or any(e.get("type") == "tool_execution_start" for e in raw_pi):
            complete = True
        else:
            error = "incomplete stream: no agent_end/message_end"
            if exit_code == 0:
                exit_code = -2

    # Surface Pi/model errors from parse_error lines
    if exit_code != 0 and not error:
        for ev in raw_pi:
            if ev.get("type") == "parse_error":
                raw = str(ev.get("raw", ""))
                if any(x in raw.lower() for x in ("402", "401", "403", "auth", "quota", "credit", "error")):
                    error = raw[:2000]
                    break
        if not error:
            error = f"non-zero exit_code={exit_code}"

    # If process failed hard, mark synthetic result as error
    if error and mapped and mapped[-1].get("type") == "result":
        mapped[-1]["is_error"] = True
        mapped[-1]["result"] = error[:2000]

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
        raw_events=mapped,  # Claude-like for normalize_trace
        error=error,
        cost_usd=None,
        api_ms=None,
    )


def run_case(
    *,
    case_id: str,
    turns: list[str],
    case_dir: Path,
    project_cwd: Path,
    pi_bin: str = DEFAULT_PI_BIN,
    agent_md: str = "agent.md",
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
    thinking: str = "off",
    no_builtin_tools: bool = False,
    approve: bool = True,
    append_system_prompt: bool = True,
    timeout_sec: int = 300,
    extra_args: Optional[list[str]] = None,
    # Accepted for compatibility with legacy callers (ignored)
    claude_bin: Optional[str] = None,
    permission_mode: Optional[str] = None,
    dangerously_skip_permissions: Optional[bool] = None,
    output_format: Optional[str] = None,
    verbose: Optional[bool] = None,
    include_partial_messages: Optional[bool] = None,
    mcp_config: Optional[str] = None,
    agent_workdir_root: Optional[str] = None,
    pi_agent_dir: Optional[str] = None,
    pi_packages: Optional[list[str]] = None,
) -> CaseRunResult:
    if claude_bin:
        pi_bin = claude_bin
    case_dir.mkdir(parents=True, exist_ok=True)
    execution_id = uuid.uuid4().hex
    # Layer-1: agent cwd = <agent_workdir_root>/pi/<execution_id>/ (outside the repo);
    # profile project_cwd is only the template for agent.md/.mcp.json.
    template_cwd = Path(project_cwd)
    project_cwd = prepare_case_workdir(
        harness="pi", execution_id=execution_id, template_dir=template_cwd,
        files=dict.fromkeys([agent_md, *PI_SANDBOX_FILES]), root=agent_workdir_root,
    )
    workdir_root = project_cwd.parent.parent
    try:
        agent_dir = resolve_pi_agent_dir(pi_agent_dir, project_cwd, provider=provider, packages=pi_packages)
    except Exception:
        cleanup_case_workdir(project_cwd, workdir_root)
        raise
    (case_dir / "agent_workdir.json").write_text(
        json.dumps(
            {
                "template_cwd": str(template_cwd),
                "agent_cwd": str(project_cwd),
                "pi_agent_dir": str(agent_dir) if agent_dir else "inherit",
                "pi_isolation_flags": list(PI_ISOLATION_FLAGS) if agent_dir else [],
                "files": sorted(str(p.relative_to(project_cwd)) for p in project_cwd.rglob("*") if p.is_file()),
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    try:
        return _run_case_in(
            case_id=case_id, turns=turns, case_dir=case_dir, project_cwd=project_cwd, pi_bin=pi_bin,
            agent_md=agent_md, provider=provider, model=model, thinking=thinking,
            no_builtin_tools=no_builtin_tools, approve=approve,
            append_system_prompt=append_system_prompt, timeout_sec=timeout_sec,
            extra_args=extra_args, execution_id=execution_id, pi_agent_dir=agent_dir,
        )
    finally:
        cleanup_case_workdir(project_cwd, workdir_root)


def _run_case_in(
    *,
    case_id: str,
    turns: list[str],
    case_dir: Path,
    project_cwd: Path,
    pi_bin: str,
    agent_md: str,
    provider: str,
    model: str,
    thinking: str,
    no_builtin_tools: bool,
    approve: bool,
    append_system_prompt: bool,
    timeout_sec: int,
    extra_args: Optional[list[str]],
    execution_id: str,
    pi_agent_dir: Optional[Path] = None,
) -> CaseRunResult:
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
    max_attempts = int(os.environ.get("EVAL_TURN_MAX_ATTEMPTS", "2"))
    for i, prompt in enumerate(turns, start=1):
        tr: Optional[TurnResult] = None
        for attempt in range(1, max_attempts + 1):
            prev = case_dir / f"stream_turn{i}.jsonl"
            if attempt > 1 and prev.is_file():
                archive = case_dir / f"stream_turn{i}_attempt{attempt - 1}.jsonl"
                try:
                    archive.write_text(prev.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                except Exception:
                    pass
                print(
                    f"[retry] {case_id} turn{i} attempt={attempt}/{max_attempts} after: {tr.error if tr else '?'}",
                    flush=True,
                )
            tr = run_turn(
                prompt=prompt,
                turn_index=i,
                case_dir=case_dir,
                project_cwd=project_cwd,
                pi_bin=pi_bin,
                agent_md=agent_md,
                provider=provider,
                model=model,
                thinking=thinking,
                no_builtin_tools=no_builtin_tools,
                approve=approve,
                append_system_prompt=append_system_prompt,
                session_id=session_id,
                timeout_sec=timeout_sec,
                extra_args=extra_args,
                case_id=case_id,
                execution_id=execution_id,
                pi_agent_dir=pi_agent_dir,
            )
            incomplete = (not _stream_has_result(tr.raw_events)) or (
                tr.error is not None
                and (
                    "idle timeout" in (tr.error or "")
                    or "incomplete stream" in (tr.error or "")
                    or "timeout after" in (tr.error or "")
                )
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
        # Stop on hard turn failure including stream errorMessage (exit may still be 0).
        if tr.exit_code != 0 or tr.error or not _stream_has_result(tr.raw_events):
            break
    concat_streams(case_dir, len(turn_results))
    # Session file (Pi's own JSONL) -> case_dir/session_transcript.jsonl. Not fed to
    # normalize_trace (transcript_path stays None: its parser is Claude-format).
    save_pi_session_transcript(case_dir, session_id, pi_agent_dir, project_cwd)
    # Also keep raw Pi JSON as stream_pi.jsonl for debugging
    raw_pi_out = case_dir / "stream_pi.jsonl"
    with raw_pi_out.open("w", encoding="utf-8") as w:
        for i in range(1, len(turn_results) + 1):
            sp = case_dir / f"stream_turn{i}.jsonl"
            if not sp.is_file():
                continue
            w.write(f"### TURN {i}\n")
            body = sp.read_text(encoding="utf-8", errors="replace")
            w.write(body)
            if body and not body.endswith("\n"):
                w.write("\n")

    # Optimize success: any turn error (incl. Pi stream errorMessage) => not success,
    # even when the CLI process exited 0 and emitted agent_end.
    success = (
        last_exit == 0
        and overall_error is None
        and bool(turn_results)
        and all(not t.error for t in turn_results)
        and not any(
            isinstance(ev, dict) and ev.get("type") == "result" and ev.get("is_error")
            for t in turn_results
            for ev in (t.raw_events or [])
        )
    )
    return CaseRunResult(
        case_id=case_id,
        session_id=session_id,
        turns=turn_results,
        success=success,
        exit_code=last_exit,
        error=overall_error,
        wall_ms=int((time.time() - t0) * 1000),
        transcript_path=None,
    )


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Smoke one Pi case (direct adapter)")
    p.add_argument("--case-id", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--case-dir", type=Path, required=True)
    p.add_argument(
        "--cwd",
        type=Path,
        default=WORKSPACES_ROOT / "pi",
    )
    p.add_argument("--timeout", type=int, default=300)
    args = p.parse_args()
    res = run_case(
        case_id=args.case_id,
        turns=[args.prompt],
        case_dir=args.case_dir,
        project_cwd=args.cwd,
        timeout_sec=args.timeout,
    )
    print(json.dumps({"success": res.success, "exit_code": res.exit_code, "error": res.error, "session_id": res.session_id, "final": (res.turns[-1].final_text[:500] if res.turns else "")}, ensure_ascii=False, indent=2))
