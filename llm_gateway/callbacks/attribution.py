"""Opt-in, fail-closed attribution for an exclusive evaluation gateway lane.

Two configuration modes (see ``lane_mode``):

- ``env`` (preferred): one gateway process == one lane. The launcher
  (gatewayctl) sets ``LLM_GATEWAY_NAME`` and the gateway's ``.env.<name>``
  declares ``LANE_POOL``. Only this process's own master key maps to the lane.
- ``json`` (deprecated): ``LLM_ATTRIBUTION_CONFIG`` points at
  ``attribution_lanes.json`` listing every lane; every lane key env must be set.

The configuration binds an authenticated LiteLLM key hash to a lane. A lane's
registry is written by the evaluation runner; this callback only reads it.
Both case_id and execution_id (typically X-Eval-* headers) take precedence and never consult the registry.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

LANE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
GATEWAY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = GATEWAY_DIR / "attribution_lanes.json"
DEFAULT_REGISTRY_DIR = GATEWAY_DIR / "run" / "attribution"


def lane_mode() -> str | None:
    """``json`` if LLM_ATTRIBUTION_CONFIG is set (legacy wins), ``env`` if LANE_POOL is set, else None."""
    if (os.environ.get("LLM_ATTRIBUTION_CONFIG") or "").strip():
        return "json"
    if (os.environ.get("LANE_POOL") or "").strip():
        return "env"
    return None


def gateway_name() -> str:
    return (os.environ.get("LLM_GATEWAY_NAME") or "").strip()


def config_path() -> Path:
    return Path(os.environ.get("LLM_ATTRIBUTION_CONFIG", str(DEFAULT_CONFIG))).resolve()


def env_registry_dir() -> Path:
    raw = (os.environ.get("LANE_REGISTRY_DIR") or "").strip()
    if not raw:
        return DEFAULT_REGISTRY_DIR
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else GATEWAY_DIR / path).resolve()


def load_env_config() -> dict[str, Any]:
    """Single-lane config for this gateway process (lane_id == gateway name)."""
    lane_id = gateway_name()
    if not LANE_RE.fullmatch(lane_id):
        raise ValueError("LANE_POOL set but LLM_GATEWAY_NAME missing/invalid (start via llmtrace gateway up)")
    pool = (os.environ.get("LANE_POOL") or "").strip()
    if not LANE_RE.fullmatch(pool):
        raise ValueError("invalid LANE_POOL")
    key = (os.environ.get("LITELLM_MASTER_KEY") or "").strip()
    if not key:
        raise ValueError("missing gateway key env LITELLM_MASTER_KEY")
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    return {
        "mode": "env",
        "lanes": {lane_id: {"pool": pool, "chat_url": (os.environ.get("LANE_TARGET_URL") or "").strip() or None,
                            "gateway_key_env": "LITELLM_MASTER_KEY"}},
        "key_to_lane": {key_hash: lane_id},
        "registry_dir": env_registry_dir(),
    }


def load_config(path: Path | None = None) -> dict[str, Any]:
    if path is None and lane_mode() == "env":
        return load_env_config()
    path = Path(path or config_path()).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
        raise ValueError("attribution config must contain lanes object")
    raw_dir = data.get("registry_dir", "run/attribution")
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise ValueError("registry_dir must be a path")
    registry_dir = (path.parent / raw_dir).resolve()
    lanes = {}
    key_to_lane = {}
    urls = set()
    for lane_id, row in data["lanes"].items():
        if not isinstance(lane_id, str) or not LANE_RE.fullmatch(lane_id):
            raise ValueError("invalid lane_id")
        if not isinstance(row, dict):
            raise ValueError("lane must be an object")
        chat_url = row.get("chat_url")
        env_name = row.get("gateway_key_env")
        if not isinstance(chat_url, str) or not chat_url.startswith(("http://", "https://")):
            raise ValueError(f"invalid chat_url for {lane_id}")
        if chat_url in urls:
            raise ValueError("duplicate chat_url in attribution config")
        urls.add(chat_url)
        if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise ValueError(f"invalid gateway_key_env for {lane_id}")
        key = os.environ.get(env_name, "").strip()
        if not key:
            raise ValueError(f"missing gateway key env {env_name}")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        if key_hash in key_to_lane:
            raise ValueError("one gateway key cannot identify two lanes")
        key_to_lane[key_hash] = lane_id
        lanes[lane_id] = {"chat_url": chat_url, "gateway_key_env": env_name}
    return {"mode": "json", "lanes": lanes, "key_to_lane": key_to_lane, "registry_dir": registry_dir}


def gateway_ready_filename(port: int | str) -> str:
    """One ready marker per gateway listen port (avoids :4001/:4002 overwrite)."""
    return f"gateway_ready.port-{port}.json"


def gateway_ready_path(directory: Path, port: int | str) -> Path:
    return Path(directory) / gateway_ready_filename(port)


def mark_gateway_ready() -> None:
    """Signal that this proxy process loaded the attribution callback/config.

    Marker is per listen port so concurrent Claude (:4001) and Insurance (:4002)
    gateways do not overwrite each other.
    """
    mode = lane_mode()
    if mode == "env":
        cfg = load_env_config()
        # The runner compares against the gateway's own .env.<name> file.
        raw_env = (os.environ.get("LLM_GATEWAY_ENV_FILE") or "").strip()
        path = Path(raw_env).resolve() if raw_env else None
    else:
        path = config_path()
        cfg = load_config(path)
    directory = cfg["registry_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    port = str(os.environ.get("LITELLM_PORT", "4001")).strip() or "4001"
    profile = (os.environ.get("LLM_GATEWAY_PROFILE") or os.environ.get("LITELLM_PROFILE") or "").strip()
    marker = gateway_ready_path(directory, port)
    payload = {
        "pid": os.getpid(),
        "port": int(port) if port.isdigit() else port,
        "profile": profile or None,
        "mode": mode,
        "gateway": gateway_name() or None,
        "lane_pool": (os.environ.get("LANE_POOL") or "").strip() or None if mode == "env" else None,
        "config_path": str(path) if path else None,
        "config_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None,
        "started_at_epoch": time.time(),
    }
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def key_hash_from_kwargs(kwargs: dict[str, Any]) -> str | None:
    for source in (kwargs.get("metadata"), (kwargs.get("litellm_params") or {}).get("metadata")):
        if not isinstance(source, dict):
            continue
        value = source.get("user_api_key_hash")
        if isinstance(value, str) and value:
            # LiteLLM exposes a SHA-256 token for virtual keys, but its
            # master-key auth object currently supplies the original value.
            # Normalize both without ever persisting the credential.
            return value if re.fullmatch(r"[0-9a-f]{64}", value) else hashlib.sha256(value.encode()).hexdigest()
    return None


def resolve(kwargs: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a request-local attribution snapshot; never guess a case."""
    cfg = config if config is not None else load_config()
    key_hash = key_hash_from_kwargs(kwargs)
    lane_id = cfg["key_to_lane"].get(key_hash) if key_hash else None
    if not lane_id:
        return {"attribution_status": "unmapped"}
    path = cfg["registry_dir"] / f"{lane_id}.json"
    try:
        if path.is_symlink():
            raise ValueError("symlink registry")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("lane_id") != lane_id:
            raise ValueError("invalid registry")
        if data.get("state") != "active":
            return {"lane_id": lane_id, "attribution_status": str(data.get("state") or "invalid")}
        for field in ("run_id", "case_id", "execution_id", "owner_token"):
            if not isinstance(data.get(field), str) or not data[field]:
                raise ValueError("incomplete registry")
        return {"lane_id": lane_id, "run_id": data["run_id"],
                "case_id": data["case_id"], "execution_id": data["execution_id"],
                "case_id_source": "lane_registry", "attribution_status": "attributed"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"lane_id": lane_id, "attribution_status": "registry_unavailable"}
