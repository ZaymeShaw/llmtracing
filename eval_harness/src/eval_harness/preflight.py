"""Mandatory preflight health check before ANY eval case launches.

Why: on 2026-09-27 a broken itools sync made every insurance tool call raise, and a full
triple run (120 Claude + 120 Pi) burned hours producing invalid-but-"successful" cases.

Checks (all must pass; order matters — the itools sync may repair the tree first):
  disk            free space on the eval_runs volume >= EVAL_PREFLIGHT_MIN_FREE_GB (default 3)
  secret_guard    agent child env (layer-1 allowlist) + MCP server env + copied template files
                  carry no upstream secret (agent_env.assert_no_upstream_secrets)
  itools_sync     insurance-tools-mcp/scripts/sync_from_upstream.py --ensure: upstream commit /
                  input digest vs SYNC_STATE.json; on drift it runs the validated sync; failure = abort
  gateway_<kind>  a tiny REAL completion through the gateway the profile actually uses
                  (claude: :4001 /v1/messages with the sandbox settings; pi: provider base
                  /chat/completions; insurance: :4002) — tagged X-Eval-Case-Id=__preflight__
                  so it never lands in a case's llm_calls
  insurance_health  GET :18063/health (only when the insurance lane is included)
  mcp_<kind>      spawn the MCP server EXACTLY like the agent: command/args/env from the
                  profile's .mcp.json, env = layer-1 allowlist + server env, cwd = a fresh
                  sandbox dir under agent_workdir_root; initialize + tools/list + one real
                  read-only tools/call (customer_search for the dataset A01 name); the result
                  must be a backend-backed business envelope (no exception / isError / infra text)

CLI:
  PYTHONPATH=src python3 -m eval_harness.preflight --config configs/experiments/claude_0922.yaml \
      --config configs/experiments/pi_0922_shared_bailian_conc.yaml [--insurance] [--out f.json]
Exit 0 = pass, 4 = fail.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from eval_harness import agent_env as ae
from eval_harness.paths import PROJECT_ROOT
from eval_harness.tool_health import PUBLISHED_TOOLS, classify_tool_result, result_text

TZ = ZoneInfo("Asia/Shanghai")
EXIT_PREFLIGHT_FAILED = 4
DEFAULT_MIN_FREE_GB = 3.0
MIN_FREE_ENV = "EVAL_PREFLIGHT_MIN_FREE_GB"
RESULT_ENV = "EVAL_PREFLIGHT_RESULT"
REUSE_MAX_AGE_S = 1800
PREFLIGHT_CASE_ID = "__preflight__"
MCP_SERVER = "insurance-tools"
PROBE_TOOL = {"tool": "customer_search", "arguments": {"query": "伏云平"}}  # dataset A01 customer
INSURANCE_HEALTH_URL = "http://127.0.0.1:18063/health"
INSURANCE_RELAY_PROFILE = PROJECT_ROOT / "llm_gateway" / ".env.insurance_4002"
INSURANCE_MODEL_ENV = "EVAL_PREFLIGHT_INSURANCE_MODEL"
INSURANCE_DEFAULT_MODEL = "deepseek-v4-flash-0731"
# Pi models.json (~/.pi/agent/models.json, JSON5) provider → base URL; key env from agent_env.
PI_PROVIDER_BASE = {
    "local-relay": "http://127.0.0.1:4001/v1",
    "local-relay-bailian": None,  # resolved from the dedicated relay profile
}
MCP_STEP_TIMEOUT_S = 90.0


def _insurance_gateway_base() -> str:
    profile = ae.parse_env_file(INSURANCE_RELAY_PROFILE)
    host = profile.get("LITELLM_HOST")
    port = profile.get("LITELLM_PORT")
    if not host or not port:
        raise ValueError(f"Insurance relay host/port missing in {INSURANCE_RELAY_PROFILE}")
    return f"http://{host}:{port}/v1"


class PreflightFailed(SystemExit):
    def __init__(self, result: Dict[str, Any]):
        self.result = result
        super().__init__(EXIT_PREFLIGHT_FAILED)


@dataclass
class HarnessSpec:
    kind: str                        # claude | pi | insurance
    project_cwd: Optional[str] = None
    mcp_config: str = ".mcp.json"
    agent_workdir_root: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    config_path: Optional[str] = None
    mcp_config_override: Optional[str] = None   # tests/demo only


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def kind_of(harness_name: str) -> str:
    h = str(harness_name or "")
    if "claude" in h:
        return "claude"
    if h.startswith("pi") or "pi_" in h:
        return "pi"
    if "insurance" in h:
        return "insurance"
    return "other"


def spec_from_cfg(cfg: Mapping[str, Any], harness_name: str, config_path: Optional[str] = None) -> HarnessSpec:
    return HarnessSpec(
        kind=kind_of(harness_name),
        project_cwd=str(cfg.get("project_cwd") or "") or None,
        mcp_config=str(cfg.get("mcp_config") or ".mcp.json"),
        agent_workdir_root=cfg.get("agent_workdir_root"),
        provider=cfg.get("provider"),
        model=cfg.get("model"),
        config_path=config_path,
    )


def spec_from_config_path(path: Path) -> HarnessSpec:
    from eval_harness.adapters import resolve_harness
    from eval_harness.run import _load_mapping, _merge_profile, _resolve

    path = Path(path).resolve()
    cfg = _merge_profile(_load_mapping(path), path.parent)
    if cfg.get("project_cwd"):
        cfg["project_cwd"] = str(_resolve(path.parent, str(cfg["project_cwd"])))
    return spec_from_cfg(cfg, resolve_harness(cfg.get("harness")), str(path))


# --------------------------------------------------------------------------- http
def _http(method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
          body: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> tuple[int, str, float]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")[:4000], time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "replace")[:600], time.monotonic() - t0
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"[:300], time.monotonic() - t0


def _tag_headers() -> Dict[str, str]:
    return {"X-Eval-Case-Id": PREFLIGHT_CASE_ID, "X-Eval-Execution-Id": f"preflight-{uuid.uuid4().hex[:12]}",
            "X-Eval-Thinking": "off"}


def _completion_text(body: str) -> Optional[str]:
    try:
        d = json.loads(body)
    except ValueError:
        return None
    if isinstance(d.get("content"), list):  # anthropic
        return "".join(str(b.get("text") or "") for b in d["content"] if isinstance(b, dict)) or "(no text block)"
    ch = d.get("choices")
    if isinstance(ch, list) and ch:
        msg = ch[0].get("message") or {}
        return str(msg.get("content") or msg.get("reasoning_content") or "(empty content)")
    return None


# --------------------------------------------------------------------------- checks
def check_disk(path: Path, min_gb: float) -> Dict[str, Any]:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    free = shutil.disk_usage(str(p)).free
    return {"ok": free >= min_gb * 1024 ** 3, "path": str(p), "free_gb": round(free / 1024 ** 3, 2),
            "min_free_gb": min_gb}


def _settings_env(spec: HarnessSpec) -> Dict[str, str]:
    p = Path(spec.project_cwd or "") / ".claude" / "settings.local.json"
    try:
        return dict((json.loads(p.read_text(encoding="utf-8")).get("env") or {}))
    except (OSError, ValueError):
        return {}


def _mcp_config_path(spec: HarnessSpec) -> Path:
    if spec.mcp_config_override:
        return Path(spec.mcp_config_override)
    return Path(spec.project_cwd or PROJECT_ROOT) / spec.mcp_config


def _mcp_server(spec: HarnessSpec) -> Dict[str, Any]:
    cfg = json.loads(_mcp_config_path(spec).read_text(encoding="utf-8"))
    server = (cfg.get("mcpServers") or {}).get(MCP_SERVER)
    if not server:
        raise ValueError(f"{_mcp_config_path(spec)} has no mcpServers.{MCP_SERVER}")
    return server


def _agent_env(spec: HarnessSpec) -> Dict[str, str]:
    extra: Dict[str, str] = {}
    if spec.kind == "pi":
        key_name = ae.PI_PROVIDER_KEY_ENV.get(str(spec.provider or ""))
        keys = ae.load_local_gateway_keys()
        if key_name and keys.get(key_name):
            extra[key_name] = keys[key_name]
        extra.update({"PI_EVAL_CASE_ID": PREFLIGHT_CASE_ID, "PI_EVAL_THINKING": "off"})
    return ae.build_agent_env(spec.kind, extra=extra)


def _mcp_env(spec: HarnessSpec, server: Mapping[str, Any]) -> Dict[str, str]:
    env = _agent_env(spec)
    env.update({str(k): str(v) for k, v in (server.get("env") or {}).items()})
    return env


def check_secret_guard(specs: List[HarnessSpec]) -> Dict[str, Any]:
    secrets = ae.load_upstream_secrets()
    detail: Dict[str, Any] = {"upstream_secret_values_known": len(secrets), "checked": []}
    try:
        for spec in specs:
            if spec.kind not in ("claude", "pi"):
                continue
            ae.assert_no_upstream_secrets(_agent_env(spec), secrets)
            ae.assert_no_upstream_secrets(_mcp_env(spec, _mcp_server(spec)), secrets)
            for rel in (spec.mcp_config, ".claude/settings.local.json"):
                f = Path(spec.project_cwd or "") / rel
                if f.is_file() and any(s.encode() in f.read_bytes() for s in secrets):
                    raise ae.AgentEnvSecretError(f"template file {f} contains an upstream secret")
            detail["checked"].append(spec.kind)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", **detail}
    return {"ok": True, **detail}


def itools_root_from_server(server: Mapping[str, Any]) -> Optional[Path]:
    cmd = Path(str(server.get("command") or ""))
    # <root>/.venv/bin/python
    if cmd.parent.name == "bin" and cmd.parent.parent.name == ".venv":
        return cmd.parent.parent.parent
    return None


def check_itools_sync(itools_root: Path, *, mode: str = "ensure", target_root: Optional[Path] = None) -> Dict[str, Any]:
    script = itools_root / "scripts" / "sync_from_upstream.py"
    py = itools_root / ".venv" / "bin" / "python"
    if not script.is_file() or not py.is_file():
        return {"ok": False, "error": f"missing {script} or {py}"}
    cmd = [str(py), str(script), f"--{mode}"]
    if target_root is not None:
        cmd += ["--root", str(target_root)]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                           env={k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "TMPDIR")})
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "itools sync timed out (900s)"}
    out = (r.stdout or "").strip()
    detail: Dict[str, Any] = {"ok": r.returncode == 0, "mode": mode, "rc": r.returncode,
                              "elapsed_s": round(time.monotonic() - t0, 1),
                              "root": str(target_root or itools_root)}
    try:
        rep = json.loads(out) if out.startswith("{") else None
    except ValueError:
        rep = None
    if isinstance(rep, dict):
        detail["action"] = rep.get("action") or ("check" if mode == "check" else None)
        up = rep.get("upstream")
        detail["upstream"] = up if isinstance(up, str) else (up or {}).get("short") or (up or {}).get("commit")
        if rep.get("reasons"):
            detail["reasons"] = rep["reasons"][:5]
    if r.returncode != 0:
        detail["stderr_tail"] = (r.stderr or "")[-1500:]
        if not rep:
            detail["stdout_tail"] = out[-600:]
    state = (target_root or itools_root) / "SYNC_STATE.json"
    if state.is_file():
        try:
            st = json.loads(state.read_text(encoding="utf-8"))
            detail["sync_state"] = {"synced_at": st.get("synced_at"),
                                    "upstream_commit": (st.get("upstream") or {}).get("short"),
                                    "upstream_path": (st.get("upstream") or {}).get("path")}
        except ValueError:
            pass
    return detail


def check_gateway(spec: HarnessSpec) -> Dict[str, Any]:
    if spec.kind == "claude":
        env = _settings_env(spec)
        base = (env.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:4001").rstrip("/")
        token = env.get("ANTHROPIC_AUTH_TOKEN") or ae.load_local_gateway_keys().get("LITELLM_MASTER_KEY", "")
        model = env.get("ANTHROPIC_MODEL") or spec.model or "deepseek-v4-flash-0731"
        url = f"{base}/v1/messages"
        headers = {"x-api-key": token, "Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
                   **_tag_headers()}
        body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with OK."}]}
    else:
        if spec.kind == "pi":
            provider = str(spec.provider or "")
            base = _insurance_gateway_base() if provider == "local-relay-bailian" else PI_PROVIDER_BASE.get(provider)
            key_name = ae.PI_PROVIDER_KEY_ENV.get(provider)
            if not base or not key_name:
                return {"ok": False, "error": f"unknown Pi provider {provider!r} (known: {sorted(PI_PROVIDER_BASE)})"}
            model = spec.model or INSURANCE_DEFAULT_MODEL
        else:
            base, key_name = _insurance_gateway_base(), "INSURANCE_LITELLM_MASTER_KEY"
            model = os.environ.get(INSURANCE_MODEL_ENV) or INSURANCE_DEFAULT_MODEL
        token = ae.load_local_gateway_keys().get(key_name, "")
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {token}", **_tag_headers()}
        body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with OK."}]}
    if not token:
        return {"ok": False, "url": url, "error": "no local gateway key available"}
    status, text, dt = _http("POST", url, headers=headers, body=body, timeout=90)
    reply = _completion_text(text) if status == 200 else None
    out = {"ok": status == 200 and reply is not None, "url": url, "model": model, "http": status,
           "elapsed_s": round(dt, 2)}
    if out["ok"]:
        out["reply_head"] = str(reply)[:40]
    else:
        out["error"] = text[:300]
    return out


def check_insurance_health() -> Dict[str, Any]:
    status, text, dt = _http("GET", INSURANCE_HEALTH_URL, timeout=10)
    return {"ok": status == 200, "url": INSURANCE_HEALTH_URL, "http": status, "body": text[:120],
            "elapsed_s": round(dt, 2)}


class _StdioMcp:
    """Minimal newline-delimited JSON-RPC MCP client (stdlib only; harness runs on py3.9)."""

    def __init__(self, argv: List[str], *, env: Dict[str, str], cwd: Path):
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                                     env=env, cwd=str(cwd))
        self.q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._next = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                self.q.put(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
        self.q.put(None)

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _send(self, msg: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self._next += 1
        rid = self._next
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"MCP {method} timed out after {timeout:.0f}s")
            try:
                msg = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if msg is None:
                raise RuntimeError(f"MCP server exited (rc={self.proc.poll()}) during {method}")
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"MCP {method} error: {json.dumps(msg['error'], ensure_ascii=False)[:300]}")
                return msg.get("result") or {}

    def stderr_tail(self, n: int = 1200) -> str:
        try:
            self.stderr.seek(0)
            return self.stderr.read().decode("utf-8", "replace")[-n:]
        except Exception:  # noqa: BLE001
            return ""

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        self.stderr.close()


def _summarize_envelope(text: str) -> Dict[str, Any]:
    try:
        env = json.loads(text)
    except ValueError:
        return {"non_json_head": text[:80]}
    if not isinstance(env, dict):
        return {}
    return {"status": env.get("status"), "facts": len(env.get("facts") or []),
            "source": (env.get("source") or {}).get("endpoint") if isinstance(env.get("source"), dict) else None}


def check_mcp(spec: HarnessSpec, probe: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    probe = probe or PROBE_TOOL
    try:
        server = _mcp_server(spec)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    env = _mcp_env(spec, server)
    ae.assert_no_upstream_secrets(env)
    argv = [str(server["command"]), *[str(a) for a in server.get("args") or []]]
    root = ae.resolve_agent_workdir_root(spec.agent_workdir_root)
    wd = ae.prepare_case_workdir(harness=spec.kind, execution_id=f"preflight_{uuid.uuid4().hex[:12]}",
                                 template_dir=_mcp_config_path(spec).parent,
                                 files=[_mcp_config_path(spec).name], root=spec.agent_workdir_root)
    out: Dict[str, Any] = {"ok": False, "argv": argv, "cwd": str(wd), "mcp_config": str(_mcp_config_path(spec)),
                           "env_keys": sorted(env), "probe_tool": probe["tool"]}
    client: Optional[_StdioMcp] = None
    t0 = time.monotonic()
    try:
        client = _StdioMcp(argv, env=env, cwd=wd)
        init = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "eval-preflight", "version": "1"}},
                              MCP_STEP_TIMEOUT_S)
        out["server"] = (init.get("serverInfo") or {}).get("name")
        client.notify("notifications/initialized")
        listed = client.request("tools/list", {}, MCP_STEP_TIMEOUT_S)
        names = [t.get("name") for t in listed.get("tools") or []]
        out["tools"] = names
        missing = [t for t in PUBLISHED_TOOLS if t not in names]
        res = client.request("tools/call", {"name": probe["tool"], "arguments": probe["arguments"]},
                             MCP_STEP_TIMEOUT_S)
        text = result_text(res.get("content"))
        kind, why = classify_tool_result(text, bool(res.get("isError")))
        out["tool_call"] = {"is_error": bool(res.get("isError")), "class": kind, "why": why,
                            **_summarize_envelope(text)}
        if kind != "ok":
            out["tool_call"]["text_head"] = text[:240]
        out["ok"] = not missing and kind == "ok"
        if missing:
            out["error"] = f"tools/list missing {missing}"
        elif kind != "ok":
            out["error"] = f"tools/call {probe['tool']} not a healthy business response: {why}"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:400]
    finally:
        if client is not None:
            if not out["ok"]:
                out["server_stderr_tail"] = client.stderr_tail()
            client.close()
        ae.cleanup_case_workdir(wd, root)
        out["elapsed_s"] = round(time.monotonic() - t0, 2)
    return out


# --------------------------------------------------------------------------- orchestration
def run_preflight(
    specs: List[HarnessSpec],
    *,
    eval_runs_dir: Optional[Path] = None,
    include_insurance: bool = False,
    itools_sync_mode: str = "ensure",
    itools_root_override: Optional[Path] = None,
    min_free_gb: Optional[float] = None,
    log: Callable[[str], None] = lambda s: print(s, flush=True),
) -> Dict[str, Any]:
    t0 = time.monotonic()
    min_gb = float(min_free_gb if min_free_gb is not None else (os.environ.get(MIN_FREE_ENV) or DEFAULT_MIN_FREE_GB))
    agent_specs = [s for s in specs if s.kind in ("claude", "pi")]
    kinds = [s.kind for s in specs] + (["insurance"] if include_insurance and all(s.kind != "insurance" for s in specs) else [])
    checks: Dict[str, Dict[str, Any]] = {}

    def run(name: str, fn: Callable[[], Dict[str, Any]]) -> None:
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        checks[name] = res
        brief = {k: v for k, v in res.items() if k in ("free_gb", "http", "model", "action", "upstream", "rc",
                                                        "tools", "tool_call", "error", "reasons", "elapsed_s")}
        if "tools" in brief:
            brief["tools"] = len(brief["tools"] or [])
        log(f"[preflight] {'OK  ' if res.get('ok') else 'FAIL'} {name} {json.dumps(brief, ensure_ascii=False)[:600]}")

    run("disk", lambda: check_disk(eval_runs_dir or PROJECT_ROOT / "eval_runs", min_gb))
    if agent_specs:
        run("secret_guard", lambda: check_secret_guard(agent_specs))
        roots = []
        for s in agent_specs:
            try:
                r = itools_root_from_server(_mcp_server(s))
            except Exception:  # noqa: BLE001
                r = None
            if r is not None and r not in roots:
                roots.append(r)
        if not roots:
            checks["itools_sync"] = {"ok": False, "error": "cannot derive insurance-tools-mcp root from .mcp.json"}
        for r in roots:
            run("itools_sync", lambda r=r: check_itools_sync(r, mode=itools_sync_mode, target_root=itools_root_override))
    for s in agent_specs:
        run(f"gateway_{s.kind}", lambda s=s: check_gateway(s))
    if "insurance" in kinds:
        run("gateway_insurance", lambda: check_gateway(HarnessSpec(kind="insurance")))
        run("insurance_health", check_insurance_health)
    for s in agent_specs:
        run(f"mcp_{s.kind}", lambda s=s: check_mcp(s))
    failed = [k for k, v in checks.items() if not v.get("ok")]
    result = {
        "ok": not failed,
        "at": _now(),
        "elapsed_s": round(time.monotonic() - t0, 1),
        "harnesses": kinds,
        "failed": failed,
        "min_free_gb": min_gb,
        "itools_sync_mode": itools_sync_mode,
        "specs": [asdict(s) for s in specs],
        "checks": checks,
        "skipped": False,
    }
    log(f"[preflight] {'PASS' if result['ok'] else 'FAIL'} harnesses={kinds} failed={failed} "
        f"elapsed={result['elapsed_s']}s")
    return result


def skipped_record(reason: str) -> Dict[str, Any]:
    return {"ok": None, "skipped": True, "at": _now(), "reason": reason,
            "warning": "preflight skipped explicitly — results of this run are NOT health-gated"}


def load_reusable(kind: str, *, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A passing preflight from the parent launcher (suite / detach) for this harness, < 30 min old."""
    p = path or os.environ.get(RESULT_ENV)
    if not p or not Path(p).is_file():
        return None
    try:
        res = json.loads(Path(p).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(res["at"])
    except (ValueError, KeyError, OSError):
        return None
    if not res.get("ok") or kind not in (res.get("harnesses") or []):
        return None
    if (datetime.now(TZ) - at).total_seconds() > REUSE_MAX_AGE_S:
        return None
    out = dict(res)
    out["reused_from"] = str(p)
    return out


def fail_message(result: Dict[str, Any]) -> str:
    lines = ["!" * 78, f"[preflight] FAILED — no case was launched. failed checks: {result.get('failed')}"]
    for name in result.get("failed") or []:
        c = result["checks"].get(name) or {}
        msg = c.get("error") or c.get("reasons") or c.get("stderr_tail") or c
        lines.append(f"  - {name}: {json.dumps(msg, ensure_ascii=False)[:800]}")
    lines.append("  fix the cause (or for tests only: --skip-preflight, which is recorded in run_meta)")
    lines.append("!" * 78)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Eval preflight health check (standalone)")
    ap.add_argument("--config", action="append", default=[], help="run config / experiment yaml (repeatable)")
    ap.add_argument("--insurance", action="store_true", help="include insurance lane (:4002 + :18063)")
    ap.add_argument("--mcp-config", default=None, help="override .mcp.json for MCP spawn (tests/demo)")
    ap.add_argument("--itools-sync", choices=("ensure", "check"), default="ensure")
    ap.add_argument("--itools-root", default=None, help="check sync state of this itools tree (demo)")
    ap.add_argument("--min-free-gb", type=float, default=None)
    ap.add_argument("--eval-runs-dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    specs = [spec_from_config_path(Path(c)) for c in args.config]
    if args.mcp_config:
        for s in specs:
            s.mcp_config_override = str(Path(args.mcp_config).resolve())
    res = run_preflight(specs, include_insurance=args.insurance, itools_sync_mode=args.itools_sync,
                        itools_root_override=Path(args.itools_root).resolve() if args.itools_root else None,
                        min_free_gb=args.min_free_gb,
                        eval_runs_dir=Path(args.eval_runs_dir) if args.eval_runs_dir else None)
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not res["ok"]:
        print(fail_message(res), flush=True)
    return 0 if res["ok"] else EXIT_PREFLIGHT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
