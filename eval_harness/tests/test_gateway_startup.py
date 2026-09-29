"""Exercise the documented launcher in a clean checkout without Insurance secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("lane_mode", ["disabled", "enabled", "missing_key"])
def test_gateway_lane_is_explicit(tmp_path: Path, lane_mode: str):
    gateway = tmp_path / "llm_gateway"
    gateway.mkdir()
    for name in ("start_litellm.sh", "attribution_lanes.json"):
        shutil.copy(ROOT / "llm_gateway" / name, gateway / name)
    shutil.copytree(ROOT / "llm_gateway/callbacks", gateway / "callbacks", ignore=shutil.ignore_patterns("__pycache__"))
    bin_dir = gateway / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text(f'export PATH="{bin_dir}:$PATH"\n')
    # Keep the real callback import/startup, replace only the long-running proxy.
    launcher = bin_dir / "litellm"
    launcher.write_text(f"#!{sys.executable}\nimport callbacks.trace_callback\nprint('callback ready')\n")
    launcher.chmod(0o755)
    (gateway / "config.yaml").write_text("{}\n")
    config = gateway / "opt_in.json"
    config.write_text(json.dumps({"registry_dir": "run/attribution", "lanes": {}}))
    profile = (ROOT / "llm_gateway/.env.example").read_text().replace(
        "LITELLM_CONFIG=./config.litellm.with_master.yaml", "LITELLM_CONFIG=./config.yaml")
    if lane_mode != "disabled":
        selected = "attribution_lanes.json" if lane_mode == "missing_key" else "opt_in.json"
        profile += f"\nLLM_ATTRIBUTION_CONFIG=./{selected}\n"
    (gateway / ".env.test").write_text(profile)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "LITELLM_", "INSURANCE_", "UPSTREAM_")) and k != "PYTHONPATH"}
    # LiteLLM DEV mode can load a .env beside its installation in the host repo.
    env["LITELLM_MODE"] = "PRODUCTION"
    result = subprocess.run(["bash", str(gateway / "start_litellm.sh"), "--profile", "test", "--foreground"],
                            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=45)
    marker = gateway / "run/attribution/gateway_ready.port-4001.json"
    if lane_mode == "missing_key":
        assert result.returncode != 0
        assert "missing gateway key env INSURANCE_LITELLM_MASTER_KEY" in result.stderr
        assert not marker.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "callback ready" in result.stdout
        assert marker.exists() == (lane_mode == "enabled")
