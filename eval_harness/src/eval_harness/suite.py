"""Production dual/triple-eval entry: one stamp, selected agents under eval_runs/.

Default agents remain Claude + Insurance (dual_datasetA_*). Optional Pi via
`--agents claude,insurance,pi` → triple_datasetA_* with pi/ (PI_* case ids).

Layout:
  eval_runs/dual_datasetA_<YYYYMMDD_HHMMSS>/
    manifest.json
    claude/          # full Claude harness run_dir (incl. llm_trace.html)
    insurance/       # full Insurance live-batch run_dir (incl. llm_trace.html)
    console.log

Entry:
  PYTHONPATH=src python3 -m eval_harness.suite --bundle … --cases A01,A02

Does not modify insurance-qa-agent. Reuses eval_harness.run + run_live_batch.
Failed attempts are NOT the product.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TextIO
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

from eval_harness.paths import PROJECT_ROOT as MOCK_SYSTEM_ROOT
from eval_harness.agent_env import parse_env_file
EVAL_HARNESS_ROOT = MOCK_SYSTEM_ROOT / "eval_harness"
DEFAULT_EVAL_RUNS_DIR = MOCK_SYSTEM_ROOT / "eval_runs"
DEFAULT_BUNDLE = EVAL_HARNESS_ROOT / "bundles" / "120_prompt_only_v1.jsonl"
DEFAULT_CLAUDE_CONFIG = EVAL_HARNESS_ROOT / "configs/runs/claude.yaml"
DEFAULT_PI_CONFIG = EVAL_HARNESS_ROOT / "configs/runs/pi.yaml"
KNOWN_AGENTS = ("claude", "insurance", "pi")
PRIMARY_RELAY_DIR = MOCK_SYSTEM_ROOT / "llm_gateway"
INSURANCE_RELAY_PROFILE = MOCK_SYSTEM_ROOT / "llm_gateway" / ".env.insurance_4002"
INSURANCE_CHAT_URL = "http://127.0.0.1:18063/v1/chat"
INSURANCE_HEALTH_URL = "http://127.0.0.1:18063/health"
START_LITELLM_SH = MOCK_SYSTEM_ROOT / "llm_gateway" / "start_litellm.sh"
START_INSURANCE_LITELLM_SH = (
    MOCK_SYSTEM_ROOT / "llm_gateway" / "start_insurance_litellm.sh"
)
MIN_HTML_BYTES = 512

_CANDIDATE_PYTHONS = [
    Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent/.feval_venv/bin/python"),
    MOCK_SYSTEM_ROOT / "llm_gateway" / ".venv" / "bin" / "python",
]


class _Tee:
    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self.primary = primary
        self.secondary = secondary

    def write(self, data: str) -> int:
        self.primary.write(data)
        self.primary.flush()
        self.secondary.write(data)
        self.secondary.flush()
        return len(data)

    def flush(self) -> None:
        self.primary.flush()
        self.secondary.flush()


def _now_tag() -> str:
    return datetime.now(TZ).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _no_proxy_env() -> None:
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])


def _master_key() -> str:
    key = parse_env_file(_primary_relay_profile()).get("LITELLM_MASTER_KEY", "")
    if not key:
        raise RuntimeError(f"LITELLM_MASTER_KEY missing in {_primary_relay_profile()}")
    return key


def _primary_relay_profile() -> Path:
    active = PRIMARY_RELAY_DIR / "run" / "active_env_file"
    if active.is_file():
        path = Path(active.read_text(encoding="utf-8").strip())
        if path.is_file():
            return path
    return PRIMARY_RELAY_DIR / ".env"


def _litellm_url() -> str:
    profile = parse_env_file(_primary_relay_profile())
    host = profile.get("LITELLM_HOST")
    port = profile.get("LITELLM_PORT")
    if not host or not port:
        raise RuntimeError(f"Primary relay host/port missing in {_primary_relay_profile()}")
    return f"http://{host}:{port}/v1/models"


def _primary_start_command() -> list[str]:
    command = ["bash", str(START_LITELLM_SH)]
    profile_name = _primary_relay_profile().name
    if profile_name != ".env":
        command.extend(["--profile", profile_name.removeprefix(".env.")])
    return command


def _insurance_master_key() -> str:
    return parse_env_file(INSURANCE_RELAY_PROFILE).get("LITELLM_MASTER_KEY", "")


def _insurance_litellm_url() -> str:
    profile = parse_env_file(INSURANCE_RELAY_PROFILE)
    host = profile.get("LITELLM_HOST")
    port = profile.get("LITELLM_PORT")
    if not host or not port:
        raise RuntimeError(f"Insurance relay host/port missing in {INSURANCE_RELAY_PROFILE}")
    return f"http://{host}:{port}/v1/models"


def _http_get(
    url: str, *, headers: Optional[dict[str, str]] = None, timeout: float = 5.0
) -> tuple[int, str]:
    _no_proxy_env()
    try:
        import httpx

        with httpx.Client(trust_env=False, timeout=timeout) as client:
            r = client.get(url, headers=headers or {})
            return r.status_code, (r.text or "")[:500]
    except Exception:
        pass
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:500]
            return int(getattr(resp, "status", 200) or 200), body
    except Exception as e:
        return 0, repr(e)


def _litellm_ok() -> bool:
    code, _ = _http_get(
        _litellm_url(),
        headers={"Authorization": f"Bearer {_master_key()}"},
        timeout=5.0,
    )
    return code == 200


def _insurance_litellm_ok() -> bool:
    key = _insurance_master_key()
    if not key:
        return False
    code, _ = _http_get(
        _insurance_litellm_url(),
        headers={"Authorization": f"Bearer {key}"},
        timeout=5.0,
    )
    return code == 200


def _insurance_ok() -> bool:
    code, _ = _http_get(INSURANCE_HEALTH_URL, timeout=5.0)
    return code == 200


def preflight(*, try_start_litellm: bool = True) -> None:
    _no_proxy_env()
    print(f"[preflight] LiteLLM {_litellm_url()}", flush=True)
    if not _litellm_ok():
        if try_start_litellm and START_LITELLM_SH.is_file():
            print(f"[preflight] LiteLLM down — starting via {START_LITELLM_SH}", flush=True)
            subprocess.run(
                _primary_start_command(),
                cwd=str(START_LITELLM_SH.parent),
                check=False,
                timeout=90,
            )
            import time

            time.sleep(2.0)
            if not _litellm_ok():
                raise SystemExit(
                    "[preflight] FAIL: LiteLLM still unhealthy after start_litellm.sh — abort"
                )
            print("[preflight] LiteLLM healthy after start", flush=True)
        else:
            raise SystemExit("[preflight] FAIL: LiteLLM unhealthy — abort")
    else:
        print("[preflight] LiteLLM OK", flush=True)

    print(f"[preflight] Insurance LiteLLM {_insurance_litellm_url()}", flush=True)
    if not _insurance_litellm_ok():
        if try_start_litellm and START_INSURANCE_LITELLM_SH.is_file():
            print(
                f"[preflight] Insurance LiteLLM down — starting via "
                f"{START_INSURANCE_LITELLM_SH}",
                flush=True,
            )
            subprocess.run(
                ["bash", str(START_INSURANCE_LITELLM_SH)],
                cwd=str(START_INSURANCE_LITELLM_SH.parent),
                check=False,
                timeout=90,
            )
        if not _insurance_litellm_ok():
            raise SystemExit(
                "[preflight] FAIL: dedicated Insurance LiteLLM unhealthy — abort"
            )
    print("[preflight] Insurance LiteLLM OK", flush=True)

    print(f"[preflight] Insurance {INSURANCE_HEALTH_URL}", flush=True)
    if not _insurance_ok():
        raise SystemExit("[preflight] FAIL: Insurance /health unhealthy — abort")
    print("[preflight] Insurance OK", flush=True)


def _python_with_httpx() -> str:
    for py in _CANDIDATE_PYTHONS:
        if not py.is_file():
            continue
        try:
            r = subprocess.run(
                [str(py), "-c", "import httpx; print('ok')"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if r.returncode == 0 and "ok" in (r.stdout or ""):
                return str(py)
        except Exception:
            continue
    try:
        import httpx  # noqa: F401

        return sys.executable
    except Exception:
        return sys.executable


def _html_ok(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= MIN_HTML_BYTES
    except OSError:
        return False


def _run_claude(*, dual_root: Path, cases: str, config: Path, resume: bool = False,
                skip_preflight: bool = False) -> dict[str, Any]:
    from eval_harness.run import main as claude_main

    argv = [
        "--config",
        str(config),
        "--cases",
        cases,
        "--run-id",
        "claude",
        "--eval-runs-dir",
        str(dual_root),
    ]
    if resume:
        argv.append("--resume")
    if skip_preflight:
        argv.append("--skip-preflight")
    print(
        f"[dual] Claude start config={config} run_id=claude "
        f"eval_runs_dir={dual_root} cases={cases}",
        flush=True,
    )
    rc = 0
    err: Optional[str] = None
    try:
        rc = int(claude_main(argv) or 0)
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else (1 if e.code else 0)
        err = f"SystemExit({e.code!r})"
    except Exception as e:
        rc = 1
        err = repr(e)
        traceback.print_exc()

    run_dir = dual_root / "claude"
    html = run_dir / "llm_trace.html"
    ok = (rc == 0) and _html_ok(html)
    print(f"[dual] Claude done rc={rc} html_ok={_html_ok(html)} run_dir={run_dir}", flush=True)
    return {
        "rc": rc,
        "ok": ok,
        "run_dir": str(run_dir),
        "html": str(html),
        "html_ok": _html_ok(html),
        "error": err,
    }


def _run_insurance(*, dual_root: Path, bundle: Path, case_ids: list[str], resume: bool = False) -> dict[str, Any]:
    print(
        f"[dual] Insurance start run_id=insurance cases={','.join(case_ids)} "
        f"eval_runs_dir={dual_root}",
        flush=True,
    )
    err: Optional[str] = None
    result: dict[str, Any] = {}

    def _pack(result: dict[str, Any], err: Optional[str]) -> dict[str, Any]:
        run_dir = dual_root / "insurance"
        html = run_dir / "llm_trace.html"
        html_path = Path(result["html_path"]) if result.get("html_path") else html
        n_ok = int(result.get("n_ok") or 0)
        n_cases = int(result.get("n_cases") or len(case_ids))
        html_ok = _html_ok(html_path)
        ok = (err is None) and html_ok and (n_ok == n_cases) and (n_cases > 0)
        print(
            f"[dual] Insurance done ok={ok} n_ok={n_ok}/{n_cases} "
            f"html_ok={html_ok} html={html_path}",
            flush=True,
        )
        return {
            "rc": 0 if ok else 1,
            "ok": ok,
            "run_dir": str(run_dir),
            "html": str(html_path),
            "html_ok": html_ok,
            "n_ok": n_ok,
            "n_cases": n_cases,
            "error": err or result.get("html_error"),
        }

    try:
        import httpx  # noqa: F401
        from eval_harness.adapters import dispatch_run_batch

        try:
            result = dispatch_run_batch(
                "insurance_qa_agno",
                bundle_case_ids=case_ids,
                run_id="insurance",
                bundle_path=bundle,
                eval_runs_dir=dual_root,
                chat_url=INSURANCE_CHAT_URL,
                resume=resume,
            )
        except Exception as e:
            err = repr(e)
            traceback.print_exc()
        return _pack(result, err)
    except ImportError:
        pass

    py = _python_with_httpx()
    print(f"[dual] Insurance via subprocess python={py}", flush=True)
    cmd = [
        py,
        "-m",
        "eval_harness.adapters.insurance",
        "--live-batch",
        "--cases",
        ",".join(case_ids),
        "--run-id",
        "insurance",
        "--bundle",
        str(bundle),
        "--eval-runs-dir",
        str(dual_root),
        "--chat-url",
        INSURANCE_CHAT_URL,
    ]
    if resume:
        cmd.append("--resume")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(EVAL_HARNESS_ROOT / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    env.setdefault("no_proxy", env["NO_PROXY"])
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)

    proc_rc = 1
    try:
        proc = subprocess.run(cmd, cwd=str(EVAL_HARNESS_ROOT), env=env, timeout=60 * 60 * 6)
        proc_rc = int(proc.returncode)
        if proc_rc != 0:
            err = f"insurance adapter exit={proc_rc}"
    except Exception as e:
        err = repr(e)
        traceback.print_exc()

    run_dir = dual_root / "insurance"
    html = run_dir / "llm_trace.html"
    summary_path = run_dir / "summary.json"
    n_ok = 0
    n_cases = len(case_ids)
    if summary_path.is_file():
        try:
            rows = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                n_cases = len(rows)
                n_ok = sum(1 for r in rows if r.get("ok"))
            elif isinstance(rows, dict) and "cases" in rows:
                cases = rows["cases"]
                n_cases = len(cases)
                n_ok = sum(1 for r in cases if r.get("ok"))
        except Exception:
            pass

    # Adapter exit 0 => success; also accept when n_ok matches and html exists
    if proc_rc == 0:
        err = None
        n_ok = n_cases = max(n_cases, len(case_ids))
        # Prefer counting from summary when present
        if summary_path.is_file():
            try:
                rows = json.loads(summary_path.read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    n_cases = len(rows)
                    n_ok = sum(1 for r in rows if r.get("ok"))
            except Exception:
                n_ok = len(case_ids)
                n_cases = len(case_ids)
        else:
            n_ok = len(case_ids)
            n_cases = len(case_ids)

    result = {
        "html_path": str(html) if html.is_file() else None,
        "n_ok": n_ok,
        "n_cases": n_cases,
    }
    return _pack(result, err)



def _parse_agents(raw: str) -> list[str]:
    parts = [x.strip().lower() for x in str(raw or "").split(",") if x.strip()]
    if not parts:
        raise SystemExit("--agents empty")
    unknown = [a for a in parts if a not in KNOWN_AGENTS]
    if unknown:
        raise SystemExit(f"Unknown agents {unknown}; known={list(KNOWN_AGENTS)}")
    out: list[str] = []
    for a in parts:
        if a not in out:
            out.append(a)
    return out


def _pi_case_ids(case_ids: list[str], prefix: str = "PI_") -> list[str]:
    """Map A01→PI_A01; leave already-prefixed ids alone."""
    out: list[str] = []
    for cid in case_ids:
        if cid.startswith(prefix):
            out.append(cid)
        else:
            out.append(f"{prefix}{cid}")
    return out


def _run_pi(*, dual_root: Path, cases: str, config: Path, resume: bool = False,
            skip_preflight: bool = False) -> dict[str, Any]:
    """Pi lane via eval_harness.run (profile harness=pi_coding). run_id=pi."""
    from eval_harness.run import main as pi_main

    argv = [
        "--config",
        str(config),
        "--cases",
        cases,
        "--run-id",
        "pi",
        "--eval-runs-dir",
        str(dual_root),
    ]
    if resume:
        argv.append("--resume")
    if skip_preflight:
        argv.append("--skip-preflight")
    print(
        f"[dual] Pi start config={config} run_id=pi "
        f"eval_runs_dir={dual_root} cases={cases}",
        flush=True,
    )
    rc = 0
    err: Optional[str] = None
    try:
        rc = int(pi_main(argv) or 0)
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else (1 if e.code else 0)
        err = f"SystemExit({e.code!r})"
    except Exception as e:
        rc = 1
        err = repr(e)
        traceback.print_exc()

    run_dir = dual_root / "pi"
    html = run_dir / "llm_trace.html"
    ok = (rc == 0) and _html_ok(html)
    print(f"[dual] Pi done rc={rc} html_ok={_html_ok(html)} run_dir={run_dir}", flush=True)
    return {
        "rc": rc,
        "ok": ok,
        "run_dir": str(run_dir),
        "html": str(html),
        "html_ok": _html_ok(html),
        "error": err,
    }


def lane_upstream(run_dir: Path, side: str) -> dict[str, Any]:
    """Backend/upstream exposure of one agent lane (run_meta.upstream + live alert file);
    prints a status line. Upstream trouble never fails the lane, it is only surfaced."""
    from eval_harness.tool_health import format_status, status_line

    run_dir = Path(run_dir)
    out: dict[str, Any] = {}
    try:
        meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
        u = meta.get("upstream") or {}
        out = {k: u.get(k) for k in ("cases", "tool_infra_error", "upstream_blocked", "upstream_degraded",
                                     "blocked_ids", "upstream_errors")}
    except Exception:
        pass
    try:
        st = status_line(run_dir)
        out["alert"] = st.get("alert")
        line = format_status(st, side)
        if out.get("upstream_blocked") or out.get("upstream_degraded") or st.get("alert"):
            line = "!" * 10 + " " + line
        print(f"[dual] {line}", flush=True)
    except Exception as e:
        print(f"[dual] WARN upstream status unavailable for {side}: {e!r}", flush=True)
    return out


def mandatory_preflight(
    *, agents: list[str], claude_config: Optional[Path], pi_config: Optional[Path], out_dir: Path,
) -> dict[str, Any]:
    """New mandatory health gate (eval_harness.preflight) for the selected lanes; exits 4 on failure.

    The passing result is exported via EVAL_PREFLIGHT_RESULT so the per-lane run.py calls reuse it.
    """
    from eval_harness import preflight as pf

    specs = []
    if "claude" in agents and claude_config is not None:
        specs.append(pf.spec_from_config_path(claude_config))
    if "pi" in agents and pi_config is not None:
        specs.append(pf.spec_from_config_path(pi_config))
    res = pf.run_preflight(specs, eval_runs_dir=out_dir, include_insurance="insurance" in agents)
    path = out_dir / "preflight.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not res["ok"]:
        print(pf.fail_message(res), flush=True)
        raise SystemExit(pf.EXIT_PREFLIGHT_FAILED)
    os.environ[pf.RESULT_ENV] = str(path)
    return res


def _write_manifest(dual_root: Path, manifest: dict[str, Any]) -> Path:
    path = dual_root / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Production dual/triple-eval: Claude + Insurance (+ optional Pi) "
            "under one dual_datasetA_* / triple_datasetA_* stamp"
        )
    )
    p.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    p.add_argument("--cases", type=str, default="A01,A02")
    p.add_argument("--eval-runs-dir", type=Path, default=DEFAULT_EVAL_RUNS_DIR)
    p.add_argument("--stamp", type=str, default=None)
    p.add_argument("--claude-config", type=Path, default=DEFAULT_CLAUDE_CONFIG)
    p.add_argument(
        "--pi-config",
        type=Path,
        default=DEFAULT_PI_CONFIG,
        help="Pi runner config (configs/profiles/pi.yaml). Default configs/runs/pi.yaml",
    )
    p.add_argument(
        "--agents",
        type=str,
        default="claude,insurance",
        help="Comma list from {claude,insurance,pi}. Default claude,insurance (unchanged dual).",
    )
    p.add_argument(
        "--pi-remap-prefix",
        type=str,
        default="PI_",
        help="Prefix applied to --cases for Pi lane (ledger isolation). Default PI_.",
    )
    p.add_argument("--skip-preflight", action="store_true",
                   help="TESTS ONLY: skip gateway + mandatory preflight (recorded in manifest/run_meta)")
    p.add_argument("--claude-only", action="store_true", help="Deprecated alias: --agents claude")
    p.add_argument("--insurance-only", action="store_true", help="Deprecated alias: --agents insurance")
    p.add_argument("--pi-only", action="store_true", help="Alias: --agents pi")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Reuse same stamp folders: Claude/Pi skip success traces; Insurance skips meta.success cases",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    only_flags = [args.claude_only, args.insurance_only, args.pi_only]
    if sum(1 for x in only_flags if x) > 1:
        raise SystemExit("Choose at most one of --claude-only / --insurance-only / --pi-only")
    if args.claude_only:
        agents = ["claude"]
    elif args.insurance_only:
        agents = ["insurance"]
    elif args.pi_only:
        agents = ["pi"]
    else:
        agents = _parse_agents(args.agents)

    bundle = Path(args.bundle).expanduser().resolve()
    if not bundle.is_file():
        raise SystemExit(f"bundle not found: {bundle}")
    config = Path(args.claude_config).expanduser().resolve()
    if "claude" in agents and not config.is_file():
        raise SystemExit(f"claude config not found: {config}")
    pi_config = Path(args.pi_config).expanduser().resolve()
    if "pi" in agents and not pi_config.is_file():
        raise SystemExit(f"pi config not found: {pi_config}")

    case_ids = [x.strip() for x in str(args.cases).split(",") if x.strip()]
    if not case_ids:
        raise SystemExit("--cases empty")
    cases_csv = ",".join(case_ids)
    pi_ids = _pi_case_ids(case_ids, prefix=str(args.pi_remap_prefix or "PI_"))
    pi_cases_csv = ",".join(pi_ids)

    stamp = args.stamp or _now_tag()
    eval_runs_dir = Path(args.eval_runs_dir).expanduser().resolve()
    if "pi" in agents:
        kind = "triple_datasetA"
    else:
        kind = "dual_datasetA"
    dual_root = eval_runs_dir / f"{kind}_{stamp}"
    dual_root.mkdir(parents=True, exist_ok=True)

    console_path = dual_root / "console.log"
    console_f = console_path.open("a", encoding="utf-8")
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(real_out, console_f)  # type: ignore[assignment]
    sys.stderr = _Tee(real_err, console_f)  # type: ignore[assignment]

    started = _now_iso()
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": kind,
        "stamp": stamp,
        "cases": case_ids,
        "agents": agents,
        "pi_cases": pi_ids if "pi" in agents else [],
        "dual_root": str(dual_root),
        "started": started,
        "finished": None,
        "status": "running",
        "claude_ok": False,
        "insurance_ok": False,
        "pi_ok": False,
        "claude_html": str(dual_root / "claude" / "llm_trace.html"),
        "insurance_html": str(dual_root / "insurance" / "llm_trace.html"),
        "pi_html": str(dual_root / "pi" / "llm_trace.html"),
        "bundle": str(bundle),
        "claude_config": str(config) if "claude" in agents else None,
        "pi_config": str(pi_config) if "pi" in agents else None,
        "notes": (
            "Production dual/triple entry (eval_harness.suite). One stamp; selected "
            "agents under this dual_root; each auto-emits llm_trace.html via existing "
            "runners (eval_harness.run / insurance live-batch / pi_coding). "
            "Pi uses PI_* case ids for ledger isolation. Failed attempts are NOT the product."
        ),
    }
    _write_manifest(dual_root, manifest)

    claude_info: dict[str, Any] = {"ok": False, "skipped": False}
    insurance_info: dict[str, Any] = {"ok": False, "skipped": False}
    pi_info: dict[str, Any] = {"ok": False, "skipped": False}
    exit_code = 0

    try:
        print(f"[dual] root={dual_root} kind={kind} agents={agents}", flush=True)
        print(
            f"[dual] cases={cases_csv} pi_cases={pi_cases_csv if 'pi' in agents else '-'} "
            f"stamp={stamp} resume={bool(args.resume)}",
            flush=True,
        )
        print(
            "[dual] product path only when status=success and all selected agents' llm_trace.html exist",
            flush=True,
        )

        if not args.skip_preflight:
            if "insurance" in agents:
                preflight(try_start_litellm=True)
            else:
                # Claude/Pi-only: main LiteLLM only (skip Insurance stack)
                _no_proxy_env()
                print(f"[preflight] LiteLLM {_litellm_url()}", flush=True)
                if not _litellm_ok():
                    if START_LITELLM_SH.is_file():
                        print(
                            f"[preflight] LiteLLM down — starting via {START_LITELLM_SH}",
                            flush=True,
                        )
                        subprocess.run(
                            ["bash", str(START_LITELLM_SH)],
                            cwd=str(START_LITELLM_SH.parent),
                            check=False,
                            timeout=90,
                        )
                        import time

                        time.sleep(2.0)
                    if not _litellm_ok():
                        raise SystemExit("[preflight] FAIL: LiteLLM unhealthy — abort")
                print("[preflight] LiteLLM OK (insurance stack skipped)", flush=True)
            _pf = mandatory_preflight(
                agents=agents,
                claude_config=config if "claude" in agents else None,
                pi_config=pi_config if "pi" in agents else None,
                out_dir=dual_root,
            )
            manifest["preflight"] = {k: _pf[k] for k in ("ok", "at", "harnesses", "failed", "elapsed_s")}
            manifest["preflight"]["file"] = str(dual_root / "preflight.json")
            _write_manifest(dual_root, manifest)
        else:
            from eval_harness.preflight import skipped_record
            print("[preflight] skipped via --skip-preflight (tests only; recorded)", flush=True)
            manifest["preflight"] = skipped_record("--skip-preflight")
            _write_manifest(dual_root, manifest)

        settings = MOCK_SYSTEM_ROOT / "workspaces" / "claude" / ".claude" / "settings.local.json"
        if "claude" in agents and settings.is_file():
            try:
                s = json.loads(settings.read_text(encoding="utf-8"))
                attr = str((s.get("env") or {}).get("CLAUDE_CODE_ATTRIBUTION_HEADER", ""))
                print(f"[dual] CLAUDE_CODE_ATTRIBUTION_HEADER={attr!r} ({settings})", flush=True)
                if attr not in ("1", "true", "TRUE", "yes"):
                    print(
                        "[dual] WARN: attribution header not enabled — "
                        "Claude 3-block system may be incomplete",
                        flush=True,
                    )
            except Exception as e:
                print(f"[dual] WARN: could not read settings: {e!r}", flush=True)

        # Extra idle headroom vs default 45s for dual runs
        os.environ.setdefault("EVAL_IDLE_TIMEOUT_SEC", "600")

        if "claude" in agents:
            claude_info = _run_claude(
                dual_root=dual_root, cases=cases_csv, config=config, resume=bool(args.resume),
                skip_preflight=bool(args.skip_preflight),
            )
        else:
            claude_info = {"ok": False, "skipped": True, "html_ok": False}
            print("[dual] Claude skipped (not in --agents)", flush=True)

        if "insurance" in agents:
            insurance_info = _run_insurance(
                dual_root=dual_root, bundle=bundle, case_ids=case_ids, resume=bool(args.resume)
            )
        else:
            insurance_info = {"ok": False, "skipped": True, "html_ok": False}
            print("[dual] Insurance skipped (not in --agents)", flush=True)

        if "pi" in agents:
            pi_info = _run_pi(
                dual_root=dual_root,
                cases=pi_cases_csv,
                config=pi_config,
                resume=bool(args.resume),
                skip_preflight=bool(args.skip_preflight),
            )
        else:
            pi_info = {"ok": False, "skipped": True, "html_ok": False}
            print("[dual] Pi skipped (not in --agents)", flush=True)

        claude_ok = bool(claude_info.get("ok")) and not claude_info.get("skipped")
        insurance_ok = bool(insurance_info.get("ok")) and not insurance_info.get("skipped")
        pi_ok = bool(pi_info.get("ok")) and not pi_info.get("skipped")

        selected_ok = True
        if "claude" in agents and not claude_ok:
            selected_ok = False
        if "insurance" in agents and not insurance_ok:
            selected_ok = False
        if "pi" in agents and not pi_ok:
            selected_ok = False
        both = selected_ok and len(agents) >= 1

        status = "success" if both else "failed"
        exit_code = 0 if both else 2

        # Secret-scan gate over the whole stamp root (incl. insurance, which has no per-run gate).
        from eval_harness.secret_scan import scan as _secret_scan
        _rep = _secret_scan([dual_root])
        (dual_root / "secret_scan.json").write_text(
            json.dumps(_rep.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest["secret_scan"] = "PASS" if _rep.ok else "FAILED"
        if not _rep.ok:
            print("!" * 78 + "\n[dual] SECRET SCAN FAILED — NOT A PRODUCT (llm_trace.html not final)\n"
                  + _rep.summary_text() + "\n" + "!" * 78, flush=True)
            both = False
            status = "failed_secret_scan"
            exit_code = 3
        else:
            print(f"[dual] {_rep.summary_text().splitlines()[0]}", flush=True)

        manifest["upstream"] = {
            side: lane_upstream(dual_root / side, side)
            for side in ("claude", "pi") if side in agents
        }
        manifest.update(
            {
                "finished": _now_iso(),
                "status": status,
                "claude_ok": claude_ok,
                "insurance_ok": insurance_ok,
                "pi_ok": pi_ok,
                "claude_html": claude_info.get("html") or manifest["claude_html"],
                "insurance_html": insurance_info.get("html") or manifest["insurance_html"],
                "pi_html": pi_info.get("html") or manifest["pi_html"],
                "claude": {k: v for k, v in claude_info.items() if k != "result"},
                "insurance": {k: v for k, v in insurance_info.items() if k != "result"},
                "pi": {k: v for k, v in pi_info.items() if k != "result"},
            }
        )
        _write_manifest(dual_root, manifest)

        if both:
            print(f"[dual] SUCCESS status=success root={dual_root}", flush=True)
            if "claude" in agents:
                print(f"[dual] claude_html={manifest['claude_html']}", flush=True)
            if "insurance" in agents:
                print(f"[dual] insurance_html={manifest['insurance_html']}", flush=True)
            if "pi" in agents:
                print(f"[dual] pi_html={manifest['pi_html']}", flush=True)
        else:
            print(
                "[dual] NOT A SUCCESSFUL PRODUCT — manifest.status=failed "
                f"(kept for debug under {dual_root})",
                flush=True,
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)

    except SystemExit as e:
        exit_code = int(e.code) if isinstance(e.code, int) else (1 if e.code else 0)
        manifest.update(
            {
                "finished": _now_iso(),
                "status": "failed",
                "claude_ok": bool(claude_info.get("ok")),
                "insurance_ok": bool(insurance_info.get("ok")),
                "pi_ok": bool(pi_info.get("ok")),
                "error": f"SystemExit({e.code!r})",
            }
        )
        _write_manifest(dual_root, manifest)
        print(
            f"[dual] NOT A SUCCESSFUL PRODUCT — preflight/abort exit={exit_code} root={dual_root}",
            flush=True,
        )
        raise
    except Exception as e:
        exit_code = 1
        traceback.print_exc()
        manifest.update(
            {
                "finished": _now_iso(),
                "status": "failed",
                "claude_ok": bool(claude_info.get("ok")),
                "insurance_ok": bool(insurance_info.get("ok")),
                "pi_ok": bool(pi_info.get("ok")),
                "error": repr(e),
            }
        )
        _write_manifest(dual_root, manifest)
        print(f"[dual] NOT A SUCCESSFUL PRODUCT — exception root={dual_root}", flush=True)
    finally:
        sys.stdout = real_out
        sys.stderr = real_err
        console_f.close()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
