"""Export secret-scan gate (blocks; never redacts/patches).

Scans files (streamed in chunks; .zip/.xlsx entries streamed too) for:
  * exact upstream secret values from llm_gateway/.env*           -> kind "upstream_secret"
  * exact LOCAL relay keys (LITELLM_MASTER_KEY, ...)                -> kind "local_gateway_key"
  * generic ``sk-[A-Za-z0-9_-]{16,}``                              -> kind "generic_sk"
  * ``Bearer <20+ token chars>``                                  -> kind "bearer"
  * ``x-api-key`` followed by a 16+ char value (placeholder "***" ignored) -> kind "x_api_key"

Any finding fails the gate (local gateway keys too, unless allow_local_gateway_keys=True, which
downgrades ONLY exact local-key hits — and generic hits equal to a local key — to warnings).
Only masked prefixes (first 6 chars) are ever printed.

CLI:
  PYTHONPATH=src python3 -m eval_harness.secret_scan PATH [PATH ...] [--allow-local-gateway-keys]
exit 0 = clean, 3 = findings.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Tuple

from eval_harness.agent_env import load_local_gateway_keys, load_upstream_secrets, mask

CHUNK = 4 * 1024 * 1024
OVERLAP = 1024
NESTED_MAX_BYTES = 256 * 1024 * 1024
ZIP_SUFFIXES = (".zip", ".xlsx", ".docx", ".pptx")
SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".venv"}

GENERIC_PATTERNS: Tuple[Tuple[str, "re.Pattern[bytes]"], ...] = (
    ("generic_sk", re.compile(rb"sk-[A-Za-z0-9_\-]{16,}")),
    ("bearer", re.compile(rb"Bearer\s+[A-Za-z0-9._\-~+/=]{20,}")),
    ("x_api_key", re.compile(rb"(?i)x-api-key[\"'\\]*\s*[:=]\s*[\"'\\]*[A-Za-z0-9._\-]{16,}")),
)


class SecretScanError(RuntimeError):
    def __init__(self, report: "ScanReport") -> None:
        self.report = report
        super().__init__(report.summary_text())


@dataclass
class ScanReport:
    files_scanned: int = 0
    bytes_scanned: int = 0
    # (file, kind, masked) -> count
    findings: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    warnings: Dict[Tuple[str, str, str], int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, file: str, kind: str, masked: str, n: int, warn: bool) -> None:
        tgt = self.warnings if warn else self.findings
        key = (file, kind, masked)
        tgt[key] = tgt.get(key, 0) + n

    def to_dict(self) -> dict:
        def rows(d):
            return [
                {"file": f, "kind": k, "masked": m, "count": c}
                for (f, k, m), c in sorted(d.items(), key=lambda x: (-x[1], x[0]))
            ]
        return {
            "ok": self.ok,
            "files_scanned": self.files_scanned,
            "bytes_scanned": self.bytes_scanned,
            "total_findings": sum(self.findings.values()),
            "findings": rows(self.findings),
            "warnings": rows(self.warnings),
        }

    def summary_text(self, limit: int = 40) -> str:
        d = self.to_dict()
        head = (
            f"SECRET SCAN {'PASS' if self.ok else 'FAIL'}: files={d['files_scanned']} "
            f"bytes={d['bytes_scanned']} findings={d['total_findings']} warnings={sum(self.warnings.values())}"
        )
        lines = [head]
        for r in d["findings"][:limit]:
            lines.append(f"  FAIL {r['file']}  kind={r['kind']}  value={r['masked']}  count={r['count']}")
        if len(d["findings"]) > limit:
            lines.append(f"  ... {len(d['findings']) - limit} more finding rows")
        for r in d["warnings"][:10]:
            lines.append(f"  warn {r['file']}  kind={r['kind']}  value={r['masked']}  count={r['count']}")
        return "\n".join(lines)


class SecretScanner:
    def __init__(
        self,
        *,
        upstream_secrets: Optional[Iterable[str]] = None,
        local_keys: Optional[Iterable[str]] = None,
        allow_local_gateway_keys: bool = False,
    ) -> None:
        ups = set(load_upstream_secrets() if upstream_secrets is None else upstream_secrets)
        loc = set(load_local_gateway_keys().values() if local_keys is None else local_keys) - ups
        self.exact: List[Tuple[str, bytes, str]] = []  # (kind, bytes, masked)
        for s in sorted(ups):
            if s:
                self.exact.append(("upstream_secret", s.encode(), mask(s)))
        for s in sorted(loc):
            if s:
                self.exact.append(("local_gateway_key", s.encode(), mask(s)))
        self.local_bytes = {s.encode() for s in loc if s}
        self.allow_local = allow_local_gateway_keys
        self.report = ScanReport()

    # -- core streaming scan
    def _scan_stream(self, fh: BinaryIO, label: str) -> None:
        tail = b""
        counts: Dict[Tuple[str, str], int] = {}
        while True:
            chunk = fh.read(CHUNK)
            last = not chunk
            buf = tail + chunk
            if not buf:
                break
            limit = len(buf) if last else max(0, len(buf) - OVERLAP)
            self.report.bytes_scanned += len(chunk)
            for kind, needle, masked in self.exact:
                start = 0
                while True:
                    i = buf.find(needle, start)
                    if i < 0 or i >= limit:
                        break
                    counts[(kind, masked)] = counts.get((kind, masked), 0) + 1
                    start = i + 1
            for kind, pat in GENERIC_PATTERNS:
                for m in pat.finditer(buf, 0, len(buf)):
                    if m.start() >= limit:
                        break
                    val = m.group(0)
                    if kind == "generic_sk" and any(e[1] == val or val.startswith(e[1]) for e in self.exact):
                        continue  # already counted as exact
                    if kind == "bearer":
                        tok = val.split()[-1]
                        if any(tok.startswith(e[1]) for e in self.exact) or tok.startswith(b"sk-"):
                            continue  # counted by exact / generic_sk
                    txt = val.decode("latin-1")
                    if kind == "x_api_key":
                        txt = re.split(r"[:=]\s*[\"'\\]*", txt, maxsplit=1)[-1]
                    elif kind == "bearer":
                        txt = txt.split()[-1]
                    counts[(kind, mask(txt))] = counts.get((kind, mask(txt)), 0) + 1
            if last:
                break
            tail = buf[limit:]
        for (kind, masked), n in counts.items():
            warn = self.allow_local and kind == "local_gateway_key"
            self.report.add(label, kind, masked, n, warn)

    def _scan_zip(self, zf: zipfile.ZipFile, label: str, depth: int = 0) -> None:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = f"{label}!{info.filename}"
            # Nested office/zip members (e.g. results.xlsx inside the product zip): their XML is
            # deflated, so scan the inner entries. Bounded in size/depth; others are streamed.
            if (depth < 2 and info.filename.lower().endswith(ZIP_SUFFIXES)
                    and info.file_size <= NESTED_MAX_BYTES):
                data = zf.read(info)
                if zipfile.is_zipfile(io.BytesIO(data)):
                    with zipfile.ZipFile(io.BytesIO(data)) as inner:
                        self._scan_zip(inner, name, depth + 1)
                    continue
                self._scan_stream(io.BytesIO(data), name)
                continue
            with zf.open(info) as fh:
                self._scan_stream(fh, name)

    def scan_file(self, path: Path, label: Optional[str] = None) -> None:
        path = Path(path)
        label = label or str(path)
        self.report.files_scanned += 1
        if path.suffix.lower() in ZIP_SUFFIXES and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                self._scan_zip(zf, label)
            return
        with path.open("rb") as fh:
            self._scan_stream(fh, label)

    def scan_paths(self, paths: Iterable[Path]) -> ScanReport:
        for p in paths:
            p = Path(p)
            if p.is_dir():
                for f in sorted(iter_files(p)):
                    self.scan_file(f)
            elif p.is_file():
                self.scan_file(p)
        return self.report


def iter_files(root: Path) -> Iterable[Path]:
    for p in Path(root).rglob("*"):
        if any(part in SKIP_DIR_NAMES for part in p.parts):
            continue
        if p.is_file():
            yield p


def scan(paths: Iterable[Path], *, allow_local_gateway_keys: bool = False) -> ScanReport:
    return SecretScanner(allow_local_gateway_keys=allow_local_gateway_keys).scan_paths(paths)


def gate(paths: Iterable[Path], *, what: str = "export", allow_local_gateway_keys: bool = False) -> ScanReport:
    """Scan; on any finding print loudly and raise SecretScanError (caller must not produce output)."""
    rep = scan(list(paths), allow_local_gateway_keys=allow_local_gateway_keys)
    if not rep.ok:
        bar = "!" * 78
        print(f"{bar}\n[secret-scan] BLOCKED {what}\n{rep.summary_text()}\n{bar}", file=sys.stderr, flush=True)
        raise SecretScanError(rep)
    print(f"[secret-scan] {what}: {rep.summary_text().splitlines()[0]}", flush=True)
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Secret scan gate (read-only)")
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--allow-local-gateway-keys", action="store_true")
    ap.add_argument("--json", type=Path, default=None, help="write masked report JSON here")
    args = ap.parse_args(argv)
    rep = scan(args.paths, allow_local_gateway_keys=args.allow_local_gateway_keys)
    print(rep.summary_text())
    if args.json:
        args.json.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if rep.ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
