"""Start the local Insurance QA service used by the eval harness.

Default code root is the eval worktree (branch eval/current) with eval header
passthrough ON.  Override with INSURANCE_QA_ROOT=<repo> to start other code.
Before start, runs insurance_upstream_align.py ensure (agent/eval + MCP); set
INSURANCE_UPSTREAM_ALIGN=0 to skip. Writes <STATE_DIR>/server-<port>.pid and
server-<port>.meta.json (root, git commit, passthrough flag) so the harness can
record what is actually running.
"""
import atexit, json, os, subprocess, sys
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = "/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent-eval"
STATE_DIR = Path("/Users/xiaozijian/.insurance-qa-agent-real")

root = Path(os.environ.get("INSURANCE_QA_ROOT") or DEFAULT_ROOT).expanduser().resolve()
if not (root / "app" / "main.py").is_file():
    sys.exit(f"[insurance-qa] INSURANCE_QA_ROOT has no app/main.py: {root}")

# Upstream align gate (agent/eval branch + MCP tools). Ownership on insurance side;
# skip with INSURANCE_UPSTREAM_ALIGN=0. Prefer auto-sync on drift (see umbrella ensure).
_ALIGN = Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_upstream_align.py")
if os.environ.get("INSURANCE_UPSTREAM_ALIGN", "1").strip().lower() not in {"0", "false", "no", "off"}:
    if not _ALIGN.is_file():
        sys.exit(f"[insurance-qa] missing upstream align umbrella: {_ALIGN}")
    print("[insurance-qa] upstream align ensure…", flush=True)
    _rc = subprocess.call([sys.executable, str(_ALIGN), "ensure"])
    if _rc != 0:
        sys.exit(f"[insurance-qa] upstream align ensure failed (exit {_rc}); "
                 f"fix with: python3 {_ALIGN} status")

port = int(os.environ.get("INSURANCE_QA_PORT", "18063"))
env_type = os.environ.setdefault("ENV_TYPE", "dev")
os.environ.setdefault("IQA_EVAL_HEADER_PASSTHROUGH", "1")  # read by create_app()
db_file = Path(os.environ.get("INSURANCE_QA_DB") or STATE_DIR / "runtime-real.db")
tool_config_path = root / "app" / "conf" / "tool" / f"tool_config_{env_type}_args.yaml"

# .feval_venv's editable finder maps app/capabilities/skills/tool_adapter_sdk -> MAIN, and an
# inherited PYTHONPATH may point at insurance-tools-mcp (also has `app`). ROOT first, finder out.
sys.meta_path[:] = [f for f in sys.meta_path if "__editable__" not in getattr(f, "__module__", "")]
sys.path[:] = [str(root)] + [p for p in sys.path if "__editable__" not in p and p != str(root)]
os.chdir(root)  # conf is package-relative; var/log + var/turn-audit*.jsonl are cwd-relative

import app as _app_pkg, capabilities as _caps_pkg, skills as _skills_pkg, tool_adapter_sdk as _sdk_pkg  # noqa: E402
for _mod in (_app_pkg, _caps_pkg, _skills_pkg, _sdk_pkg):
    _where = Path(_mod.__file__).resolve()
    if root not in _where.parents:
        sys.exit(f"[insurance-qa] {_mod.__name__} resolved outside root: {_where}")

def _git(*args):
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip()
    except Exception:
        return None

meta = {
    "schema_version": 1, "pid": os.getpid(), "port": port, "root": str(root),
    "git_commit": _git("rev-parse", "HEAD"),
    "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
    "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
    "eval_header_passthrough": os.environ["IQA_EVAL_HEADER_PASSTHROUGH"].strip().lower() in {"1", "true", "yes"},
    "env_type": env_type, "tool_config_path": str(tool_config_path), "db_file": str(db_file),
    "python": sys.executable, "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
}
print("[insurance-qa] " + json.dumps(meta, ensure_ascii=False), flush=True)
if os.environ.get("INSURANCE_QA_DRY_RUN") == "1":
    import app.main  # noqa: F401  import check only; no app built, no files written
    print("[insurance-qa] dry-run ok: " + app.main.__file__); sys.exit(0)

# The service owns request-local ContextVar isolation; extend its explicit allowlist
# here so this checkout's batch labels reach every model call without editing it.
from app.observability.eval_passthrough import EVAL_HEADERS
EVAL_HEADERS.update({name.lower(): name for name in (
    "X-Eval-Run-Id", "X-Eval-Harness", "X-Eval-Started-At",
)})

import uvicorn  # noqa: E402
from app.main import create_app  # noqa: E402
from app.runtime.database import runtime_db  # noqa: E402

STATE_DIR.mkdir(parents=True, exist_ok=True)
pid_path = STATE_DIR / f"server-{port}.pid"
meta_path = STATE_DIR / f"server-{port}.meta.json"
app = create_app(tool_config_path=str(tool_config_path), trusted_body_identity=True,
                 db=runtime_db(str(db_file)))
pid_path.write_text(str(os.getpid()))
_tmp = meta_path.with_suffix(".json.tmp")
_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"); _tmp.replace(meta_path)

@atexit.register
def _cleanup():
    for p in (pid_path, meta_path):
        try:
            if p == meta_path and json.loads(p.read_text()).get("pid") != os.getpid(): continue
            if p == pid_path and p.read_text().strip() != str(os.getpid()): continue
            p.unlink()
        except Exception:
            pass

uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)
