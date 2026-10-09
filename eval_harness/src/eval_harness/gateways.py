"""Runner-side view of llm_gateway lanes (``.env.<name>`` with ``LANE_POOL``).

``llm_gateway/gatewayctl.py`` is the single source of name -> path derivation;
this module loads it by file path (llm_gateway is not a Python package) and
adds the runner checks: the lane's gateway process is up and was started from
the current env file.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Optional

from eval_harness.paths import PROJECT_ROOT

DEFAULT_GATEWAY_DIR = PROJECT_ROOT / "llm_gateway"
_MODULES: dict[str, ModuleType] = {}


def gatewayctl(gateway_dir: Optional[Path] = None) -> ModuleType:
    path = (Path(gateway_dir or DEFAULT_GATEWAY_DIR) / "gatewayctl.py").resolve()
    key = str(path)
    if key not in _MODULES:
        spec = importlib.util.spec_from_file_location(f"_llmtrace_gatewayctl_{len(_MODULES)}", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
        spec.loader.exec_module(module)
        _MODULES[key] = module
    return _MODULES[key]


@dataclass(frozen=True)
class LaneSpec:
    name: str
    pool: str
    chat_url: str
    port: str
    gateway_log: Path
    registry_dir: Path
    ready_marker: Path
    env_file: Path


def resolve_lanes(selector: str, gateway_dir: Optional[Path] = None) -> list[LaneSpec]:
    """``selector`` is a LANE_POOL value or comma-separated gateway names."""
    ctl = gatewayctl(gateway_dir)
    try:
        gws = ctl.lanes(selector, Path(gateway_dir or DEFAULT_GATEWAY_DIR))
    except ctl.GatewayError as exc:
        raise RuntimeError(str(exc)) from exc
    return [LaneSpec(name=g.name, pool=g.lane_pool, chat_url=g.target_url, port=g.port,
                     gateway_log=g.log_file, registry_dir=g.registry_dir, ready_marker=g.ready_marker,
                     env_file=g.env_file) for g in gws]


def lane_not_ready(spec: LaneSpec) -> Optional[str]:
    """None when the lane's gateway is running from the current env file, else the reason."""
    try:
        marker = json.loads(spec.ready_marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"gateway not ready on :{spec.port} (run: llmtrace gateway up {spec.name})"
    except (OSError, ValueError) as exc:
        return f"unreadable ready marker {spec.ready_marker.name}: {exc}"
    if marker.get("mode") != "env" or marker.get("gateway") != spec.name:
        who = (f"gateway {marker['gateway']}" if marker.get("gateway")
               else f"a legacy launcher (profile={marker.get('profile') or '-'})")
        return f":{spec.port} is served by {who}, not lane {spec.name} (run: llmtrace gateway restart {spec.name})"
    if marker.get("config_path") != str(spec.env_file):
        return f"gateway {spec.name} started from a different env file"
    if marker.get("config_sha256") != hashlib.sha256(spec.env_file.read_bytes()).hexdigest():
        return f"{spec.env_file.name} changed since start (run: llmtrace gateway restart {spec.name})"
    pid = marker.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return "invalid gateway pid in ready marker"
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except ProcessLookupError:
        return f"gateway {spec.name} is not running (pid={pid})"
    return None
