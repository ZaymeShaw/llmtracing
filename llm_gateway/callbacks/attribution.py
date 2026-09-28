"""Opt-in, fail-closed attribution for an exclusive Insurance gateway lane.

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
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "attribution_lanes.json"


def config_path() -> Path:
    return Path(os.environ.get("LLM_ATTRIBUTION_CONFIG", str(DEFAULT_CONFIG))).resolve()


def load_config(path: Path | None = None) -> dict[str, Any]:
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
    return {"lanes": lanes, "key_to_lane": key_to_lane, "registry_dir": registry_dir}


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
        "config_path": str(path),
        "config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
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
