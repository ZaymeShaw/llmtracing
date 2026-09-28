"""Exclusive evaluation lane lease and atomic case registry writes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def lane_config(path: Path, lane_id: str, chat_url: str) -> tuple[Path, dict[str, Any]]:
    from urllib.parse import urlsplit
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    row = cfg.get("lanes", {}).get(lane_id)
    if not isinstance(row, dict) or row.get("chat_url") != chat_url:
        raise ValueError("selected lane is not bound to the requested Insurance chat_url")
    if not isinstance(cfg.get("registry_dir", "run/attribution"), str):
        raise ValueError("invalid registry_dir")
    directory = (path.parent / cfg.get("registry_dir", "run/attribution")).resolve()
    if not directory.is_relative_to(path.parent):
        raise ValueError("registry_dir must be within gateway directory")
    if not lane_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid lane_id")
    return directory, row


def gateway_ready_filename(port: int | str) -> str:
    """Match llm_gateway.callbacks.attribution: one marker file per listen port."""
    return f"gateway_ready.port-{port}.json"


def require_gateway_ready(
    directory: Path,
    config_path: Path,
    *,
    port: int | str | None = None,
) -> None:
    """A runner must not silently produce empty traces against an old proxy.

    Ready markers are per gateway listen port (e.g. gateway_ready.port-4002.json)
    so Claude :4001 and Insurance :4002 do not clobber each other.
    """
    if port is None:
        port = os.environ.get("LLM_ATTRIBUTION_READY_PORT") or os.environ.get("LITELLM_PORT")
    if port is None or str(port).strip() == "":
        raise RuntimeError("gateway ready port required (pass port= or set LLM_ATTRIBUTION_READY_PORT)")
    port = str(port).strip()
    marker_path = Path(directory) / gateway_ready_filename(port)
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"gateway attribution not ready on :{port} (missing {marker_path.name})") from exc
    pid = marker.get("pid")
    if marker.get("config_path") != str(config_path.resolve()):
        raise RuntimeError("gateway uses a different attribution config")
    if marker.get("config_sha256") != hashlib.sha256(config_path.read_bytes()).hexdigest():
        raise RuntimeError("gateway attribution config changed without restart")
    marker_port = marker.get("port")
    if marker_port is not None and str(marker_port) != port:
        raise RuntimeError(f"gateway ready marker port mismatch: file=:{port} body={marker_port}")
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("invalid gateway attribution process")
    try:
        os.kill(pid, 0)
    except PermissionError:
        # macOS sandbox can deny signal probes for a healthy service started
        # outside it. EPERM still proves that the PID exists.
        pass
    except ProcessLookupError as exc:
        raise RuntimeError(f"gateway attribution process on :{port} is not running (pid={pid})") from exc


def _atomic_json(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".lane-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(row, out, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class LaneLease:
    def __init__(self, directory: Path, lane_id: str):
        self.directory = Path(directory)
        self.lane_id = lane_id
        self.path = self.directory / f"{lane_id}.json"
        self.owner = uuid4().hex
        self.active = False
        self._lock = None

    def __enter__(self) -> "LaneLease":
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = (self.directory / f"{self.lane_id}.lock").open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                previous = json.loads(self.path.read_text(encoding="utf-8"))
                if previous.get("state") != "idle":
                    raise RuntimeError(f"lane {self.lane_id} requires recovery: {previous.get('state')}")
            return self
        except BaseException:
            self._lock.close()
            self._lock = None
            raise

    def start(self, *, run_id: str, case_id: str, execution_id: str) -> None:
        if self._lock is None or self.active:
            raise RuntimeError("lane lease is unavailable or already active")
        _atomic_json(self.path, {"schema_version": 1, "lane_id": self.lane_id,
                    "run_id": run_id, "case_id": case_id, "execution_id": execution_id,
                    "state": "active", "owner_token": self.owner,
                    "owner_pid": os.getpid(), "updated_at": _timestamp()})
        self.active = True

    def finish(self, *, blocked: bool = False, reason: str | None = None) -> None:
        if self._lock is None or not self.active:
            raise RuntimeError("lane is not active")
        row = json.loads(self.path.read_text(encoding="utf-8"))
        if row.get("owner_token") != self.owner or row.get("state") != "active":
            raise RuntimeError("lane ownership changed")
        row["state"] = "blocked" if blocked else "idle"
        row["updated_at"] = _timestamp()
        if reason:
            row["reason"] = reason
        _atomic_json(self.path, row)
        self.active = False

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.active:
                self.finish(blocked=True, reason="runner_interrupted")
        finally:
            if self._lock is not None:
                fcntl.flock(self._lock, fcntl.LOCK_UN)
                self._lock.close()
                self._lock = None


def recover_blocked_lane(directory: Path, lane_id: str, execution_id: str) -> None:
    """Operator-only reset after confirming the old Insurance work has drained."""
    path = Path(directory) / f"{lane_id}.json"
    with (Path(directory) / f"{lane_id}.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("lane_id") != lane_id or row.get("state") != "blocked":
            raise RuntimeError("lane is not blocked")
        if row.get("execution_id") != execution_id:
            raise RuntimeError("execution_id does not match blocked lane")
        row["state"] = "idle"
        row["updated_at"] = _timestamp()
        row["recovered_from_execution_id"] = execution_id
        _atomic_json(path, row)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Recover a blocked Insurance attribution lane after its old work has stopped")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--chat-url", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()
    directory, _ = lane_config(args.config, args.lane, args.chat_url)
    recover_blocked_lane(directory, args.lane, args.execution_id)
    print(f"recovered {args.lane} after {args.execution_id}")


if __name__ == "__main__":
    main()
