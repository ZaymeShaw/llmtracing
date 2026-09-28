"""Layer-1 isolation for agent child processes (Claude Code / Pi).

Two responsibilities:

1. Explicit env ALLOWLIST for the agent process (replaces ``os.environ.copy()``).
   Agents run with builtin shell tools, so anything in their env can end up in
   the conversation (e.g. ``env | grep``) and therefore in llm_trace.html.

2. Per-case working directory OUTSIDE the repo:
   ``<agent_workdir_root>/<harness>/<execution_id>/`` (default root ``~/eval_sandbox``).

Allowlist (only copied from the parent env when present, unless noted):

  * base:     PATH HOME USER LOGNAME SHELL TERM TMPDIR LANG LC_* __CF_USER_TEXT_ENCODING
              SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS
  * proxy:    NO_PROXY/no_proxy are ALWAYS set and always include 127.0.0.1,localhost,::1
              (a :7890 system proxy previously hijacked relay calls -> 502).
              HTTP(S)_PROXY / ALL_PROXY are DROPPED unless EVAL_AGENT_PASS_PROXY=1.
  * claude:   ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL
              ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL ANTHROPIC_CUSTOM_HEADERS
              CLAUDE_CODE_EXTRA_BODY CLAUDE_CODE_ATTRIBUTION_HEADER
              CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC CLAUDE_CODE_EFFORT_LEVEL
              CLAUDE_CODE_MAX_OUTPUT_TOKENS CLAUDE_AUTOCOMPACT_PCT_OVERRIDE API_TIMEOUT_MS
              MAX_THINKING_TOKENS DISABLE_AUTOUPDATER DISABLE_TELEMETRY DISABLE_ERROR_REPORTING
              CLAUDE_CONFIG_DIR
              (+ harness-set: ANTHROPIC_CUSTOM_HEADERS with X-Eval-*, CLAUDE_CODE_EXTRA_BODY,
              EVAL_EXECUTION_ID). NOTE: Claude's own auth/base-url/model normally come from
              <cwd>/.claude/settings.local.json (copied into the sandbox), not the env.
              ANTHROPIC_API_KEY is never passed (start scripts set it = upstream key).
  * pi:       PI_CODING_AGENT_DIR PI_PACKAGE_DIR PI_MCP_CONFIG_MODE
              (+ harness-set: PI_EVAL_CASE_ID PI_EVAL_EXECUTION_ID PI_EVAL_THINKING and the ONE
              local gateway key the chosen provider's models.json entry references:
              local-relay -> LITELLM_MASTER_KEY, local-relay-bailian -> INSURANCE_LITELLM_MASTER_KEY).
  * MCP (insurance-tools itools): needs only ENV_TYPE / ITOOLS_* which come from .mcp.json
              "env", plus PATH/HOME. No LLM key is needed or passed.

Never passed: UPSTREAM_*, ANTHROPIC_API_KEY, provider keys, PYTHONPATH, CONDA_*, SSH_AUTH_SOCK, ...

Guard: ``assert_no_upstream_secrets`` raises if ANY child env value equals (or contains)
an upstream secret loaded from llm_gateway/.env* (local 127.0.0.1 gateway keys excepted).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set

from eval_harness.paths import PROJECT_ROOT

GATEWAY_DIR = PROJECT_ROOT / "llm_gateway"

# Keys that only authenticate against the LOCAL relays on 127.0.0.1 (:4001/:4002).
LOCAL_GATEWAY_KEY_NAMES = ("LITELLM_MASTER_KEY", "INSURANCE_LITELLM_MASTER_KEY", "PI_LITELLM_KEY")
_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
_PLACEHOLDER_RE = re.compile(r"^(replace|changeme|your[-_]|xxx|<)", re.I)

BASE_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "LANG",
    "__CF_USER_TEXT_ENCODING", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
)
BASE_ALLOW_PREFIXES = ("LC_",)
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
LOCAL_NO_PROXY = ("127.0.0.1", "localhost", "::1")

CLAUDE_ALLOW = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_EXTRA_BODY", "CLAUDE_CODE_ATTRIBUTION_HEADER",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "API_TIMEOUT_MS",
    "MAX_THINKING_TOKENS", "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
    "CLAUDE_CONFIG_DIR",
)
PI_ALLOW = ("PI_CODING_AGENT_DIR", "PI_PACKAGE_DIR", "PI_MCP_CONFIG_MODE")
PI_PROVIDER_KEY_ENV = {
    "local-relay": "LITELLM_MASTER_KEY",
    "local-relay-bailian": "INSURANCE_LITELLM_MASTER_KEY",
}

DEFAULT_AGENT_WORKDIR_ROOT = "~/eval_sandbox"
AGENT_WORKDIR_ROOT_ENV = "EVAL_AGENT_WORKDIR_ROOT"
KEEP_WORKDIR_ENV = "EVAL_KEEP_AGENT_WORKDIR"


class AgentEnvSecretError(RuntimeError):
    """Child env would carry an upstream secret."""


class AgentWorkdirError(RuntimeError):
    """Configured agent workdir root is unsafe."""


def mask(value: str) -> str:
    return (value or "")[:6] + "***"


# --------------------------------------------------------------------------- env files
def parse_env_file(path: Path) -> Dict[str, str]:
    """KEY=VALUE parser (``export`` prefix, quotes, trailing `` # comment``). Never prints values."""
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if v[:1] in ("'", '"') and v[:1] in v[1:]:
            v = v[1:v.index(v[0], 1)]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        if k:
            out[k] = v
    return out


def gateway_env_files(gateway_dir: Optional[Path] = None) -> List[Path]:
    """All live/backup env files under llm_gateway/ (``.env``, ``.env.*``), except .env.example."""
    d = Path(gateway_dir) if gateway_dir else GATEWAY_DIR
    if not d.is_dir():
        return []
    files = [p for p in sorted(d.iterdir()) if p.is_file() and (p.name == ".env" or p.name.startswith(".env."))]
    return [p for p in files if p.name != ".env.example"]


def _is_real_secret(value: str) -> bool:
    return len(value) >= 12 and not _PLACEHOLDER_RE.match(value)


def load_upstream_secrets(
    gateway_dir: Optional[Path] = None, *, environ: Optional[Mapping[str, str]] = None
) -> Set[str]:
    """Secret values (upstream/provider keys) from llm_gateway/.env* — local gateway keys excluded."""
    local = set(load_local_gateway_keys(gateway_dir, environ={}).values())
    secrets: Set[str] = set()
    for f in gateway_env_files(gateway_dir):
        for k, v in parse_env_file(f).items():
            if k in LOCAL_GATEWAY_KEY_NAMES or not _SECRET_NAME_RE.search(k):
                continue
            if _is_real_secret(v) and v not in local:
                secrets.add(v)
    env = os.environ if environ is None else environ
    up = str(env.get("UPSTREAM_API_KEY") or "").strip()
    if _is_real_secret(up) and up not in local:
        secrets.add(up)
    return secrets


def load_local_gateway_keys(
    gateway_dir: Optional[Path] = None, *, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """Local 127.0.0.1 relay keys. Precedence: process env > llm_gateway/.env > .env.bailian_openai."""
    d = Path(gateway_dir) if gateway_dir else GATEWAY_DIR
    env = os.environ if environ is None else environ
    out: Dict[str, str] = {}
    sources: List[Mapping[str, str]] = [env, parse_env_file(d / ".env"), parse_env_file(d / ".env.bailian_openai")]
    for name in LOCAL_GATEWAY_KEY_NAMES:
        for src in sources:
            v = str(src.get(name) or "").strip()
            if v:
                out[name] = v
                break
    return out


# --------------------------------------------------------------------------- allowlist
def _merge_no_proxy(current: str) -> str:
    parts = [p.strip() for p in (current or "").split(",") if p.strip()]
    for h in LOCAL_NO_PROXY:
        if h not in parts:
            parts.append(h)
    return ",".join(parts)


def build_agent_env(
    harness: str,
    *,
    parent: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Allowlisted env for an agent child process. ``harness`` in {"claude", "pi"}."""
    src = os.environ if parent is None else parent
    names: List[str] = list(BASE_ALLOW)
    if harness == "claude":
        names += list(CLAUDE_ALLOW)
    elif harness == "pi":
        names += list(PI_ALLOW)
    else:
        raise ValueError(f"unknown agent harness for env allowlist: {harness!r}")
    env: Dict[str, str] = {}
    for k in names:
        v = src.get(k)
        if v is not None and v != "":
            env[k] = str(v)
    for k, v in src.items():
        if any(k.startswith(p) for p in BASE_ALLOW_PREFIXES):
            env[k] = str(v)
    no_proxy = _merge_no_proxy(src.get("NO_PROXY") or src.get("no_proxy") or "")
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    if str(src.get("EVAL_AGENT_PASS_PROXY") or "").strip() == "1":
        for k in PROXY_VARS:
            if src.get(k):
                env[k] = str(src[k])
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def assert_no_upstream_secrets(env: Mapping[str, str], secrets: Optional[Iterable[str]] = None) -> None:
    """Raise if any value in ``env`` equals/contains an upstream secret. Message names vars only."""
    secret_set = set(load_upstream_secrets() if secrets is None else secrets)
    bad = sorted(k for k, v in env.items() if v and any(s and s in str(v) for s in secret_set))
    if bad:
        raise AgentEnvSecretError(
            f"refusing to start agent: env var(s) {bad} carry an upstream secret from llm_gateway/.env*"
        )


def finalize_agent_env(env: Dict[str, str]) -> Dict[str, str]:
    """Guard + return the env (use right before Popen)."""
    assert_no_upstream_secrets(env)
    return env


# --------------------------------------------------------------------------- workdir
def _contains_env_files(d: Path) -> bool:
    try:
        return any(p.name == ".env" or p.name.startswith(".env.") for p in d.iterdir())
    except OSError:
        return False


def resolve_agent_workdir_root(configured: Optional[str] = None) -> Path:
    """``EVAL_AGENT_WORKDIR_ROOT`` env > config ``agent_workdir_root`` > ``~/eval_sandbox``.

    Fails if the root is inside the mock_system repo, or the root / any ancestor holds .env files.
    """
    raw = (os.environ.get(AGENT_WORKDIR_ROOT_ENV) or "").strip() or (configured or "").strip() \
        or DEFAULT_AGENT_WORKDIR_ROOT
    root = Path(os.path.expanduser(raw))
    if not root.is_absolute():
        raise AgentWorkdirError(f"agent_workdir_root must be absolute (or ~/...): {raw!r}")
    root = Path(os.path.realpath(str(root)))
    repo = Path(os.path.realpath(str(PROJECT_ROOT)))
    if root == repo or repo in root.parents:
        raise AgentWorkdirError(f"agent_workdir_root {root} is inside the mock_system repo {repo}")
    for d in [root] + list(root.parents):
        if d.exists() and _contains_env_files(d):
            raise AgentWorkdirError(f"agent_workdir_root {root}: {d} contains .env files")
    return root


def prepare_case_workdir(
    *,
    harness: str,
    execution_id: str,
    template_dir: Path,
    files: Iterable[str],
    root: Optional[str] = None,
) -> Path:
    """Create ``<root>/<harness>/<execution_id>/`` and copy only ``files`` (relative) from template_dir.

    Copied files are checked for upstream secrets (raise) — e.g. .claude/settings.local.json may
    hold the LOCAL gateway token but never an upstream key.
    """
    base = resolve_agent_workdir_root(root)
    wd = base / harness / execution_id
    wd.mkdir(parents=True, exist_ok=False)
    secrets = load_upstream_secrets()
    for rel in files:
        src = Path(template_dir) / rel
        if not src.is_file():
            continue
        data = src.read_bytes()
        for s in secrets:
            if s.encode() in data:
                shutil.rmtree(wd, ignore_errors=True)
                raise AgentEnvSecretError(f"template file {src} contains an upstream secret; not copying")
        dst = wd / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return wd


def cleanup_case_workdir(wd: Optional[Path], root: Optional[Path] = None) -> bool:
    """Remove the per-case sandbox unless EVAL_KEEP_AGENT_WORKDIR=1.

    Only deletes ``<root>/<harness>/<execution_id>`` (root = the resolved agent_workdir_root the
    dir was created under), never anything inside the repo.
    """
    if wd is None or str(os.environ.get(KEEP_WORKDIR_ENV) or "").strip() == "1":
        return False
    real = Path(os.path.realpath(str(wd)))
    repo = Path(os.path.realpath(str(PROJECT_ROOT)))
    if repo == real or repo in real.parents:
        return False
    if root is not None and Path(os.path.realpath(str(root))) != real.parent.parent:
        return False
    if not real.is_dir():
        return False
    shutil.rmtree(real, ignore_errors=True)
    return True
