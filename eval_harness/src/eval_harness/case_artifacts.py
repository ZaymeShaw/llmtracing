"""Lossless storage for complete per-case call archives, never live gateway logs."""
from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path
import tempfile
import zlib
from typing import Iterable


class CaseArtifactError(RuntimeError):
    """A captured archive cannot be read; never substitute an empty trace."""


def calls_path(case_dir: Path) -> Path | None:
    # Existing plain archives retain their format when resumed. A stale gzip
    # sibling must never take precedence over the legacy authoritative file.
    for name in ("llm_calls.jsonl", "llm_calls.jsonl.gz"):
        path = Path(case_dir) / name
        if path.is_file():
            return path
    return None


def read_calls_bytes(case_dir: Path) -> bytes | None:
    path = calls_path(case_dir)
    if path is None:
        return None
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as source:
                return source.read()  # Reading to EOF checks the gzip CRC/trailer.
        return path.read_bytes()
    except (OSError, EOFError, zlib.error) as exc:
        raise CaseArtifactError(f"Cannot read complete call archive: {path}") from exc


def atomic_write_calls(path: Path, chunks: Iterable[bytes]) -> Path:
    """Caller holds the case writer lock; atomically publish one verified snapshot.

    New cases use gzip; existing plain cases remain plain. No two-file migration,
    field changes, cutoffs, pruning, or modifications to live append-only logs.
    """
    path = Path(path)
    if path.name != "llm_calls.jsonl":
        raise ValueError("expected logical artifact path llm_calls.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    compressed = not path.is_file()
    target = path.with_name(path.name + ".gz") if compressed else path
    expected, expected_bytes = hashlib.sha256(), 0
    fd, temporary = tempfile.mkstemp(prefix=".llm-calls-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as raw:
            if compressed:
                sink = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0)
            else:
                sink = raw
            try:
                for chunk in chunks:
                    expected.update(chunk)
                    expected_bytes += len(chunk)
                    sink.write(chunk)
            finally:
                if compressed:
                    sink.close()
            raw.flush()
            os.fsync(raw.fileno())
        actual, actual_bytes = hashlib.sha256(), 0
        opener = gzip.open if compressed else open
        with opener(temporary, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                actual.update(chunk)
                actual_bytes += len(chunk)
        if (actual.digest(), actual_bytes) != (expected.digest(), expected_bytes):
            raise CaseArtifactError("Call archive verification failed before publication")
        os.replace(temporary, target)
        # Persist the directory entry as well as the file before reporting success.
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        temporary.unlink(missing_ok=True)
