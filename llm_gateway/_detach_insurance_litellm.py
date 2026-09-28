#!/usr/bin/env python3
"""Start Insurance LiteLLM (:4002) in a new session (Mac-durable; not setsid/nohup)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DIR = Path(__file__).resolve().parent
cfg = os.environ.get("LITELLM_CONFIG") or str(DIR / "config.litellm.insurance.yaml")
host = os.environ.get("LITELLM_HOST", "127.0.0.1")
port = os.environ.get("LITELLM_PORT", "4002")
master = os.environ.get("LITELLM_MASTER_KEY", "")
log = Path(os.environ.get("LLM_GATEWAY_LOG_DIR", DIR / "logs")) / "litellm_insurance.stdout.log"
pid_path = DIR / "run" / "litellm_insurance.pid"
log.parent.mkdir(parents=True, exist_ok=True)
pid_path.parent.mkdir(parents=True, exist_ok=True)

cmd = [
    "litellm",
    "--config",
    cfg,
    "--host",
    host,
    "--port",
    port,
    "--telemetry",
    "False",
]
child_env = os.environ.copy()
child_env["LLM_ATTRIBUTION_GATEWAY_PROCESS"] = "1"
pid_path.unlink(missing_ok=True)
with log.open("ab") as out:
    p = subprocess.Popen(
        cmd,
        stdout=out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=child_env,
        cwd=str(DIR),
        start_new_session=True,
    )
pid_path.write_text(f"{p.pid}\n", encoding="utf-8")
print(f"started Insurance LiteLLM pid={p.pid} http://{host}:{port} cfg={cfg}")

url = f"http://{host}:{port}/v1/models"
deadline = time.time() + 25
last_err: Exception | None = None
while time.time() < deadline:
    if p.poll() is not None:
        print(f"insurance litellm exited early code={p.returncode}; see {log}", file=sys.stderr)
        sys.exit(1)
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {master}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                print(f"health ok; insurance litellm pid={p.pid}")
                sys.exit(0)
    except Exception as e:  # noqa: BLE001
        last_err = e
    time.sleep(0.5)
print(f"insurance litellm started but health check failed: {last_err}", file=sys.stderr)
sys.exit(1)
