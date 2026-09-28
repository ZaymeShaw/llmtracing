"""Execution identity, scoped to one worker (never a process-global current case)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid

_identity: ContextVar[dict] = ContextVar("trace_identity", default={})


def current_identity() -> dict:
    return dict(_identity.get())


@contextmanager
def execution_context(**identity):
    token = _identity.set(identity)
    try:
        yield identity
    finally:
        _identity.reset(token)


def new_identity(run_id: str, harness: str, case_id: str) -> dict:
    return dict(run_id=run_id, harness=harness, case_id=case_id,
                execution_id=uuid.uuid4().hex, started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))


def archive_case(case_dir: Path) -> None:
    """Keep prior attempts, including failed/incomplete ones. Never overwrite an archive."""
    case_dir = Path(case_dir)
    if case_dir.is_dir() and any(p.name != "attempts" for p in case_dir.iterdir()):
        archive = case_dir / "attempts" / uuid.uuid4().hex
        archive.mkdir(parents=True)
        for p in list(case_dir.iterdir()):
            if p.name != "attempts":
                shutil.move(str(p), str(archive / p.name))
    case_dir.mkdir(parents=True, exist_ok=True)


def write_identity(case_dir: Path, identity: dict) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "identity.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n")
    (case_dir / "execution_id.txt").write_text(identity["execution_id"] + "\n")


def trace_headers(*, run_id=None, harness=None, case_id=None, execution_id=None) -> dict[str, str]:
    identity = current_identity()
    values = {"X-Eval-Case-Id": case_id or identity.get("case_id"),
              "X-Eval-Run-Id": run_id or identity.get("run_id"),
              "X-Eval-Harness": harness or identity.get("harness"),
              "X-Eval-Execution-Id": execution_id or identity.get("execution_id"),
              "X-Eval-Started-At": identity.get("started_at")}
    for value in values.values():
        if value and any(c in str(value) for c in "\r\n"):
            raise ValueError("Trace identity cannot contain newlines")
    return {k: str(v) for k, v in values.items() if v}


@contextmanager
def writer_lock(directory: Path):
    """One writer per output directory; per-case worker concurrency stays unchanged."""
    import fcntl
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".trace-writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another writer owns {directory}; choose another run_id or wait") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
