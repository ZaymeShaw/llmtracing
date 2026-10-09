"""Query captured gateway calls by execution / case / run id (P3 external consumer API).

Library::

    from eval_harness.query import query_calls
    calls = query_calls(execution_ids=["<hex>"], gateway="insurance_4002")

CLI::

    llmtrace query calls --execution-id ID [--gateway NAME | --log PATH] [--include-rotated]
                         [--since T] [--until T] [--format table|json|detail|jsonl|pairs]
                         [--settle SECS [--quiet SECS]] [--cursor FILE] [--events] [--full] [--html OUT_DIR]
    llmtrace query show CALL_ID [--full]      one call in detail (messages, tool calls, usage, error)
    llmtrace query cursor [--gateway NAME]    end-of-log position for a later ``--cursor``

External consumers (e.g. a verifier collector) should use ``--format json``:
a ``llmtrace.calls/v1`` envelope with ``status`` (complete | partial | no_calls |
failed), per-state ``counts``, ``pending_call_ids``, execution/case ``conflicts``,
a ``cursor`` for incremental reads, and one merged record per call (``state`` =
pending | succeeded | failed; ``--events`` adds its raw gateway records).
Exit codes: 0 complete, 1 no_calls, 2 failed/usage, 3 partial.

A typical collector: ``query cursor`` at prepare, then after the run
``query calls --execution-id X --cursor c.json --settle 30 --format json``.

A local, read-only file reader: there is no network surface. Logs are scanned
with a substring prefilter so only candidate lines are JSON-parsed. Output is
credential-redacted.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from eval_harness.llm_gateway_ingest import LOG_FILE_NAME, pairs_from_lines, pairs_to_trace_calls
from eval_harness.paths import PROJECT_ROOT

DEFAULT_LOG_DIR = PROJECT_ROOT / "llm_gateway" / "logs"
ROTATED_RE = re.compile(r"^llm_calls[._-].*\.jsonl(\.gz)?$")
_CALL_ID_RE = re.compile(r'"call_id":\s*"([^"]+)"')


def log_files(scope: Path, *, include_rotated: bool = False) -> list[Path]:
    """Live ``llm_calls.jsonl`` files under ``scope`` (a file or directory), plus rotated archives."""
    scope = Path(scope)
    if scope.is_file():
        return [scope]
    if not scope.is_dir():
        raise FileNotFoundError(f"gateway log not found: {scope}")
    files = [p for p in scope.rglob("*") if p.is_file()
             and (p.name == LOG_FILE_NAME or (include_rotated and ROTATED_RE.match(p.name)))]
    # Per directory, archives (oldest first) before the live file: a call split by a
    # rotation has its start in the archive and id-less end rows in the live file.
    return sorted(files, key=lambda p: (str(p.parent), p.name == LOG_FILE_NAME, p.name))


def _matches(pair: dict[str, Any], execution_ids: set[str], case_ids: set[str], run_ids: set[str],
             call_ids: set[str] = frozenset()) -> bool:
    if call_ids and pair.get("call_id") not in call_ids:
        return False
    if execution_ids and pair.get("execution_id") not in execution_ids:
        return False
    if case_ids and pair.get("case_id") not in case_ids:
        return False
    if run_ids and not _run_id_matches(pair.get("run_id"), run_ids):
        return False
    return True


def _run_id_matches(value: Any, run_ids: set[str]) -> bool:
    """Header mode records the bare run id; lane leases record ``<eval_runs_dir>/<run_id>``.

    Accept either form: exact match, or the last path segment(s) equal to the query.
    """
    value = str(value or "")
    return any(value == r or value.endswith("/" + r) for r in run_ids)


def resolve_scope(gateway: Optional[str] = None, log: Optional[Path] = None,
                  gateway_dir: Optional[Path] = None) -> Path:
    if gateway and log:
        raise ValueError("pass gateway or log, not both")
    if log:
        return Path(log)
    if gateway:
        from eval_harness.gateways import DEFAULT_GATEWAY_DIR, gatewayctl
        ctl = gatewayctl(gateway_dir or DEFAULT_GATEWAY_DIR)
        try:
            return ctl.load(gateway, gateway_dir or DEFAULT_GATEWAY_DIR).log_dir
        except ctl.GatewayError as exc:
            raise ValueError(str(exc)) from exc
    return DEFAULT_LOG_DIR


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.name.endswith(".gz") else path.open("r", encoding="utf-8")


# ---------------------------------------------------------------------------
# Selection, incremental scanning, settle
# ---------------------------------------------------------------------------

SCHEMA = "llmtrace.calls/v1"
CURSOR_SCHEMA = "llmtrace.cursor/v1"
_TS_RE = re.compile(r'"ts":\s*"([^"]+)"')
_STAGED_RE = re.compile(r"^\.llm_calls_.*\.jsonl\.rotating$")
_DONE_EVENTS = {"success", "response", "response_stream"}
_FAIL_EVENTS = {"failure", "error"}
_SECRET_KEYS = {"authorization", "proxy-authorization", "api_key", "apikey", "x-api-key", "api-key",
                "auth_token", "access_token", "refresh_token", "client_secret", "password", "secret",
                "cookie", "set-cookie"}
_SECRET_VAL_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})")


def redact(obj: Any) -> Any:
    """Mask credential-looking keys and values (defence in depth; the gateway redacts on write)."""
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if str(k).lower() in _SECRET_KEYS and isinstance(v, (str, int, float))
                else redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and ("sk-" in obj or "Bearer" in obj):
        return _SECRET_VAL_RE.sub("[REDACTED]", obj)
    return obj


def parse_when(value: Any) -> Optional[datetime]:
    """``10m`` / ``2h`` / ``1d`` (ago), epoch seconds, or ISO-8601 (naive = local time)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    text = str(value).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)([smhd])$", text)
    if m:
        secs = float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return datetime.now(timezone.utc) - timedelta(seconds=secs)
    if re.match(r"^\d{9,}(?:\.\d+)?$", text):
        return datetime.fromtimestamp(float(text), timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"bad time {value!r}: use 10m/2h/1d, epoch seconds or ISO-8601") from exc
    return dt if dt.tzinfo else dt.astimezone()


def _ts(value: Any) -> Optional[datetime]:
    try:
        return parse_when(value) if value else None
    except ValueError:
        return None


class Selector:
    """Which calls a query wants: kinds are ANDed, values within a kind ORed.

    ``execution_ids`` also match a call's inbound W3C ``trace_id``. The time window
    applies to the call start (its first record).
    """

    def __init__(self, *, execution_ids: Iterable[str] = (), case_ids: Iterable[str] = (),
                 run_ids: Iterable[str] = (), call_ids: Iterable[str] = (),
                 since: Any = None, until: Any = None):
        self.ex, self.cs, self.rs, self.ks = ({str(v) for v in values if v}
                                              for values in (execution_ids, case_ids, run_ids, call_ids))
        self.since, self.until = parse_when(since), parse_when(until)
        if not (self.ex or self.cs or self.rs or self.ks or self.since or self.until):
            raise ValueError("pass at least one execution_id, case_id, run_id or call_id, or a --since/--until window")
        # Most selective substring prefilter: call ids, execution ids, run ids, case ids; none = window only.
        if self.ks:
            self.needles = [f'"{n}"' for n in sorted(self.ks)]
        elif self.ex:
            self.needles = [f'"{n}"' for n in sorted(self.ex)]
        elif self.rs:  # bare or dir-prefixed run id (see _run_id_matches)
            self.needles = [f'{q}{n}"' for n in sorted(self.rs) for q in ('"', '/')]
        elif self.cs:
            self.needles = [f'"{n}"' for n in sorted(self.cs)]
        else:
            self.needles = []

    @property
    def windowed(self) -> bool:
        return self.since is not None or self.until is not None

    def in_window(self, value: Any) -> bool:
        t = _ts(value)
        if t is None:
            return not self.windowed
        return (self.since is None or t >= self.since) and (self.until is None or t <= self.until)

    def matches(self, pair: dict[str, Any]) -> bool:
        if self.ex and pair.get("execution_id") not in self.ex and pair.get("trace_id") not in self.ex:
            return False
        if not _matches(pair, set(), self.cs, self.rs, self.ks):
            return False
        return self.in_window(pair.get("ts")) if self.windowed else True

    def describe(self) -> dict[str, Any]:
        return {"execution_ids": sorted(self.ex), "case_ids": sorted(self.cs), "run_ids": sorted(self.rs),
                "call_ids": sorted(self.ks),
                "since": self.since.isoformat() if self.since else None,
                "until": self.until.isoformat() if self.until else None}


def _line_aligned_end(path: Path) -> int:
    """Offset just after the last complete line (a record being written is left for later)."""
    size = path.stat().st_size
    with path.open("rb") as f:
        pos = size
        while pos > 0:
            step = min(65536, pos)
            f.seek(pos - step)
            chunk = f.read(step)
            nl = chunk.rfind(b"\n")
            if nl >= 0:
                return pos - step + nl + 1
            pos -= step
    return 0


def snapshot_cursor(*, gateway: Optional[str] = None, log: Optional[Path] = None,
                    gateway_dir: Optional[Path] = None) -> dict[str, Any]:
    """End-of-log position; pass it back as ``cursor=`` to read only what was appended since."""
    files = {}
    for p in log_files(resolve_scope(gateway, log, gateway_dir)):
        try:
            files[str(p.resolve())] = {"inode": p.stat().st_ino, "offset": _line_aligned_end(p)}
        except OSError:
            continue
    return {"schema": CURSOR_SCHEMA, "at": time.time(), "files": files}


class Scanner:
    """Incremental reader over a log scope; ``poll()`` reads only bytes appended since the last poll.

    Rotation (inode change / truncation) is detected per file: the live file is
    re-read from 0 and archives written since the last read are scanned too.
    Duplicate lines are dropped, so overlaps are harmless. A cursor only speeds
    things up; filters still decide what matches.
    """

    def __init__(self, scope: Path, selector: Selector, *, include_rotated: bool = False,
                 cursor: Optional[dict[str, Any]] = None):
        self.scope, self.sel, self.include_rotated = Path(scope), selector, include_rotated
        cur = (cursor or {}).get("cursor", cursor) or {}
        self._cursor_files: dict[str, dict[str, int]] = dict(cur.get("files") or {})
        self._cursor_at: Optional[float] = cur.get("at")
        self.state: dict[str, dict[str, Any]] = {}  # path -> {inode, offset, done}
        self.tracked: set[str] = set()
        self._rejected: set[str] = set()
        self._seen: set[int] = set()
        self.lines: list[tuple[str, int, int, str]] = []
        self.rotated = False
        self._last_poll = self._cursor_at

    def _archives(self, directory: Path, since_epoch: Optional[float]) -> list[Path]:
        out = []
        for p in sorted(directory.iterdir()) if directory.is_dir() else []:
            if not (ROTATED_RE.match(p.name) or _STAGED_RE.match(p.name)):
                continue
            try:
                if since_epoch is None or p.stat().st_mtime >= since_epoch - 1:
                    out.append(p)
            except OSError:
                continue
        return out

    def _feed(self, path: str, line: str) -> None:
        if self.sel.needles and not self.tracked:
            if not any(n in line for n in self.sel.needles):
                return
        m = _CALL_ID_RE.search(line)
        if not m:
            return
        cid = m.group(1)
        if cid not in self.tracked:
            if cid in self._rejected:
                return
            if self.sel.needles and not any(n in line for n in self.sel.needles):
                return
            if self.sel.windowed:
                ts = _TS_RE.search(line)
                if not self.sel.in_window(ts.group(1) if ts else None):
                    self._rejected.add(cid)
                    return
            self.tracked.add(cid)
        key = hash(line)
        if key in self._seen:
            return
        self._seen.add(key)
        self.lines.append((path, 0, 0, line))

    def _read(self, path: Path, start: int) -> int:
        """Feed complete lines from ``start``; return the new offset (gz: read whole, return -1)."""
        if path.name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    self._feed(str(path), line)
            return -1
        with path.open("rb") as f:
            f.seek(start)
            pos = start
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # being written; picked up next poll
                pos += len(raw)
                self._feed(str(path), raw.decode("utf-8", "replace"))
        return pos

    def _read_archives(self, directory: Path) -> None:
        for path in self._archives(directory, self._last_poll):
            key = str(path.resolve())
            if self.state.get(key, {}).get("done"):
                continue
            try:
                self._read(path, 0)
                if path.name.endswith(".gz"):
                    self.state[key] = {"inode": path.stat().st_ino, "offset": 0, "done": True}
            except OSError:
                continue

    def poll(self) -> bool:
        """Read new data; True when new matching lines arrived."""
        before = len(self.lines)
        now = time.time()
        for path in log_files(self.scope, include_rotated=self.include_rotated):
            path = path.resolve()
            key = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            prev = self.state.get(key)
            if prev is None:
                prev = self._cursor_files.get(key) if not path.name.endswith(".gz") else None
                prev = {"inode": prev["inode"], "offset": int(prev["offset"]), "done": False} if prev else \
                    {"inode": st.st_ino, "offset": 0, "done": False, "fresh": True}
            if prev["done"]:
                continue
            if not prev.get("fresh") and (prev["inode"] != st.st_ino or st.st_size < prev["offset"]):
                # Rotated (or truncated) since we last read it: catch the tail from the archives.
                # Archives first: a call's id-less end rows in the new file join its start there.
                self.rotated = True
                self._read_archives(path.parent)
                prev = {"inode": st.st_ino, "offset": 0, "done": False}
            try:
                end = self._read(path, prev["offset"])
            except OSError:
                continue
            self.state[key] = {"inode": st.st_ino, "offset": max(end, 0), "done": end < 0}
        self._last_poll = now
        return len(self.lines) != before

    def pairs(self) -> list[dict[str, Any]]:
        pairs = pairs_from_lines(self.lines, sort_by_ts=True)
        return [redact(p) for p in pairs if self.sel.matches(p)]

    def events(self) -> dict[str, list[dict[str, Any]]]:
        by: dict[str, list[dict[str, Any]]] = {}
        for _path, _o, _n, line in self.lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("call_id"):
                by.setdefault(rec["call_id"], []).append(rec)
        return by

    def cursor(self) -> dict[str, Any]:
        files = {k: {"inode": v["inode"], "offset": v["offset"]} for k, v in self.state.items() if not v["done"]}
        return {"schema": CURSOR_SCHEMA, "at": self._last_poll or time.time(), "files": files}


def call_state(events: list[dict[str, Any]]) -> str:
    """pending (started, no end record yet) | succeeded | failed."""
    kinds = {e.get("event") for e in events}
    if kinds & _FAIL_EVENTS:
        return "failed"
    if kinds & _DONE_EVENTS:
        codes = [e.get("status_code") for e in events if e.get("event") in ("response", "response_stream")]
        return "failed" if any(isinstance(c, int) and c >= 400 for c in codes) else "succeeded"
    return "pending"


def call_record(pair: dict[str, Any], events: list[dict[str, Any]], *, include_events: bool = False) -> dict[str, Any]:
    """Stable per-call view (schema ``llmtrace.calls/v1``)."""
    terminal_kinds = _FAIL_EVENTS if any(e.get("event") in _FAIL_EVENTS for e in events) else _DONE_EVENTS
    ended = [e.get("ts") for e in events if e.get("event") in terminal_kinds]
    case_ids = sorted({e["case_id"] for e in events if e.get("case_id")})
    warnings = [w for e in events for w in (e.get("case_id_warnings") or [])]
    if len(case_ids) > 1:
        warnings.append(f"case_id conflict across records: {case_ids}")
    rec = {key: pair.get(key) for key in (
        "call_id", "ts", "gateway", "lane_id", "case_id", "execution_id", "trace_id", "run_id", "harness",
        "project_id", "role", "stage", "verifier_trace_id",
        "attribution_status", "case_id_source", "execution_id_source", "protocol", "model", "stream",
        "status_code", "latency_ms", "first_frame_ms")}
    rec.update(state=call_state(events), ended_at=ended[-1] if ended else None,
               usage=_usage(pair) or None, error=pair.get("error"), warnings=warnings)
    if include_events:
        rec["events"] = redact(events)
    return rec


def settle(scanner: Scanner, *, timeout: float, quiet: float = 2.0, interval: float = 0.5,
           clock=time.monotonic, sleep=time.sleep) -> str:
    """Poll until every matched call has ended and nothing new arrived for ``quiet`` s.

    Returns complete | partial (timed out with calls pending or still changing) | no_calls.
    """
    deadline = clock() + max(timeout, 0.0)
    last_change = clock()
    while True:
        pairs = scanner.pairs()
        events = scanner.events()
        pending = any(call_state(events.get(p["call_id"], [])) == "pending" for p in pairs)
        now = clock()
        if pairs and not pending and now - last_change >= quiet:
            return "complete"
        if now >= deadline:
            return "partial" if pairs else "no_calls"
        sleep(max(0.0, min(interval, deadline - now)))
        if scanner.poll():
            last_change = clock()


def open_scanner(*, execution_ids: Iterable[str] = (), case_ids: Iterable[str] = (),
                 run_ids: Iterable[str] = (), call_ids: Iterable[str] = (), since: Any = None, until: Any = None,
                 gateway: Optional[str] = None, log: Optional[Path] = None, include_rotated: bool = False,
                 gateway_dir: Optional[Path] = None, cursor: Optional[dict[str, Any]] = None) -> Scanner:
    selector = Selector(execution_ids=execution_ids, case_ids=case_ids, run_ids=run_ids, call_ids=call_ids,
                        since=since, until=until)
    scanner = Scanner(resolve_scope(gateway, log, gateway_dir), selector,
                      include_rotated=include_rotated, cursor=cursor)
    scanner.poll()
    return scanner


def query_calls(*, execution_ids: Iterable[str] = (), case_ids: Iterable[str] = (),
                run_ids: Iterable[str] = (), call_ids: Iterable[str] = (), since: Any = None, until: Any = None,
                gateway: Optional[str] = None, log: Optional[Path] = None, include_rotated: bool = False,
                gateway_dir: Optional[Path] = None, cursor: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Captured call pairs matching every given filter, ordered by time (credentials redacted).

    Default scope is all live logs under ``llm_gateway/logs`` (legacy shared
    file + per-gateway dirs). Needs an id filter or a since/until window.
    """
    return open_scanner(execution_ids=execution_ids, case_ids=case_ids, run_ids=run_ids, call_ids=call_ids,
                        since=since, until=until, gateway=gateway, log=log, include_rotated=include_rotated,
                        gateway_dir=gateway_dir, cursor=cursor).pairs()


def collect(*, settle_timeout: float = 0.0, quiet: float = 2.0, interval: float = 0.5,
            include_events: bool = False, **query: Any) -> dict[str, Any]:
    """One JSON envelope for external consumers: status, counts, pending ids, conflicts, cursor, calls.

    ``settle_timeout > 0`` waits for in-flight calls (see :func:`settle`); 0 is a snapshot.
    """
    scanner = open_scanner(**query)
    if settle_timeout > 0:
        status = settle(scanner, timeout=settle_timeout, quiet=quiet, interval=interval)
    else:
        status = None
    pairs, events = scanner.pairs(), scanner.events()
    calls = [call_record(p, events.get(p["call_id"], []), include_events=include_events) for p in pairs]
    counts = {"total": len(calls), **{s: sum(c["state"] == s for c in calls) for s in ("pending", "succeeded", "failed")}}
    if status is None:
        status = "no_calls" if not calls else ("partial" if counts["pending"] else "complete")
    by_exec: dict[str, set[str]] = {}
    for c in calls:
        if c.get("execution_id") and c.get("case_id"):
            by_exec.setdefault(c["execution_id"], set()).add(c["case_id"])
    return {
        "schema": SCHEMA,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": {**scanner.sel.describe(), "scope": str(scanner.scope), "include_rotated": scanner.include_rotated},
        "counts": counts,
        "pending_call_ids": [c["call_id"] for c in calls if c["state"] == "pending"],
        "conflicts": [{"execution_id": e, "case_ids": sorted(v)} for e, v in sorted(by_exec.items()) if len(v) > 1],
        "rotated": scanner.rotated,
        "cursor": scanner.cursor(),
        "calls": calls,
    }


def trace_rows(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairs as per-case ``llm_calls.jsonl`` rows (seq numbered per case), in time order."""
    by_case: dict[str, list[dict[str, Any]]] = {}
    for p in pairs:
        by_case.setdefault(p.get("case_id") or "", []).append(p)
    rows = [row for case_id, group in by_case.items() for row in pairs_to_trace_calls(group, case_id=case_id)]
    return sorted(rows, key=lambda r: str(r.get("ts") or ""))


def call_details(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Structured per-call detail: system, messages, tools, assistant text, tool calls, usage, error."""
    from eval_harness.llm_trace_html import _normalize_call
    out = []
    for row in trace_rows(pairs):
        d = _normalize_call(row)
        d["case_id"] = row.get("case_id")
        out.append(d)
    return out


def _clip(text: Any, limit: Optional[int]) -> str:
    text = "" if text is None else str(text)
    if limit is None or len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars, --full to show)"


def format_detail(d: dict[str, Any], *, full: bool = False) -> str:
    limit = None if full else 400
    u = d.get("usage") or {}
    head = [f"== call {d.get('call_id')}  case={d.get('case_id')}  seq={d.get('seq')}",
            f"   execution_id={d.get('execution_id')}  run_id={d.get('run_id')}",
            f"   gateway={d.get('gateway')}  attribution={d.get('attribution_status')}  model={d.get('model')}",
            f"   start={d.get('ts_start')}  status={d.get('status_code')}  latency={d.get('latency_ms')}ms"
            f"  first_frame={d.get('first_frame_ms')}ms",
            f"   tokens in={u.get('input_tokens', u.get('prompt_tokens'))} out={u.get('output_tokens', u.get('completion_tokens'))}"
            f" cached={u.get('cache_read_tokens', u.get('cached_tokens'))}"]
    lines = head[:]
    if d.get("error") or d.get("response_error"):
        lines.append(f"-- error: {_clip(d.get('error') or d.get('response_error'), limit)}")
    if d.get("system"):
        lines.append(f"-- system ({len(d['system'])} chars): {_clip(d['system'], None if full else 160)}")
    lines.append(f"-- tools offered ({len(d.get('tool_names') or [])}): {', '.join(d.get('tool_names') or []) or '-'}")
    msgs = d.get("messages") or []
    if d.get("system") and msgs and msgs[0].get("role") == "system":
        msgs = msgs[1:]  # already printed above
    lines.append(f"-- messages ({len(msgs)}):")
    for i, m in enumerate(msgs, 1):
        lines.append(f"   [{i}] {m.get('role')}: {_clip(m.get('text'), limit)}")
    if d.get("thinking"):
        lines.append(f"-- thinking: {_clip(d['thinking'], limit)}")
    lines.append(f"-- assistant: {_clip(d.get('assistant'), limit) or '(no text)'}")
    for tc in d.get("tool_calls") or []:
        lines.append(f"   tool_call {tc.get('name')}({_clip(tc.get('arguments'), limit)})")
    if d.get("stop_reason"):
        lines.append(f"-- stop_reason: {d['stop_reason']}")
    return "\n".join(lines)


def export_html(pairs: list[dict[str, Any]], out_dir: Path) -> Path:
    """Write the matched calls as a wire-only run dir and build its llm_trace.html."""
    from eval_harness.llm_trace_html import build_llm_trace_html
    from eval_harness.trace_identity import writer_lock
    from eval_harness.trace_store import export_executions
    out_dir = Path(out_dir)
    with writer_lock(out_dir):
        export_executions(pairs, out_dir, include_orphans=True)
        return build_llm_trace_html(out_dir, title=f"{out_dir.name} · query")


def _usage(pair: dict[str, Any]) -> dict[str, Any]:
    resp = pair.get("response")
    if not isinstance(resp, dict):
        return {}
    # success record keeps LiteLLM's usage beside the model response; fall back to the body's own.
    body = resp.get("response")
    usage = resp.get("usage") or (body.get("usage") if isinstance(body, dict) else None)
    return usage if isinstance(usage, dict) else {}


def _split(values: Optional[list[str]]) -> list[str]:
    return [v.strip() for raw in values or [] for v in raw.split(",") if v.strip()]


def _load_cursor(value: Optional[str]) -> Optional[dict[str, Any]]:
    """``--cursor FILE`` (a cursor or a previous json envelope) or ``-`` for stdin."""
    if not value:
        return None
    text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    data = json.loads(text)
    data = data.get("cursor", data) if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("schema") != CURSOR_SCHEMA:
        raise ValueError(f"not a {CURSOR_SCHEMA} cursor: {value}")
    return data


_EXIT = {"complete": 0, "no_calls": 1, "partial": 3}


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv and argv[0] in ("show", "cursor", "calls") else "calls"
    if argv and argv[0] == sub:
        argv = argv[1:]
    show = sub == "show"
    ap = argparse.ArgumentParser(prog="llmtrace query " + sub, description={
        "show": "Show captured calls in detail.",
        "cursor": "Print the current end-of-log cursor (JSON); pass it to `query calls --cursor` later.",
        "calls": "Query captured gateway calls."}[sub])
    if show:
        ap.add_argument("call_id", nargs="+")
    elif sub == "calls":
        ap.add_argument("--execution-id", action="append", help="repeatable or comma-separated; also matches W3C trace_id")
        ap.add_argument("--case-id", action="append")
        ap.add_argument("--run-id", action="append")
        ap.add_argument("--call-id", action="append")
        ap.add_argument("--since", help="call start >= (10m / 2h / 1d ago, epoch, or ISO-8601; naive = local)")
        ap.add_argument("--until", help="call start <= (same forms)")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--gateway", help="named gateway (llmtrace gateway list)")
    scope.add_argument("--log", type=Path, help="log file or directory")
    if sub == "cursor":
        args = ap.parse_args(argv)
        try:
            print(json.dumps(snapshot_cursor(gateway=args.gateway, log=args.log), ensure_ascii=False))
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    ap.add_argument("--include-rotated", action="store_true", help="also scan rotated llm_calls_*.jsonl[.gz]")
    ap.add_argument("--full", action="store_true", help="detail: do not truncate texts")
    if not show:
        ap.add_argument("--format", choices=("table", "json", "detail", "jsonl", "pairs"), default="table",
                        help="table: one line per call; json: llmtrace.calls/v1 envelope (status, counts, "
                             "cursor, calls); detail: messages/tool calls/usage/error per call; "
                             "jsonl: per-case llm_calls rows; pairs: raw joined records")
        ap.add_argument("--cursor", metavar="FILE", help="read only what was appended after this cursor "
                                                         "(`query cursor` output or a previous json envelope; - = stdin)")
        ap.add_argument("--settle", type=float, default=0.0, metavar="SECS",
                        help="wait up to SECS until every matched call ended and the log went quiet")
        ap.add_argument("--quiet", type=float, default=2.0, metavar="SECS", help="settle quiet period (default 2)")
        ap.add_argument("--events", action="store_true", help="json: include each call's raw gateway records")
        ap.add_argument("--html", type=Path, metavar="OUT_DIR", help="also export matches as a browsable llm_trace.html")
    args = ap.parse_args(argv)
    try:
        if show:
            query = dict(call_ids=_split(args.call_id))
        else:
            query = dict(execution_ids=_split(args.execution_id), case_ids=_split(args.case_id),
                         run_ids=_split(args.run_id), call_ids=_split(args.call_id),
                         since=args.since, until=args.until, cursor=_load_cursor(args.cursor))
        query.update(gateway=args.gateway, log=args.log, include_rotated=args.include_rotated)
        if not show and args.format == "json":
            env = collect(settle_timeout=args.settle, quiet=args.quiet, include_events=args.events, **query)
            print(json.dumps(env, ensure_ascii=False, default=str))
            print(f"{env['status']}: {env['counts']}", file=sys.stderr)
            return _EXIT[env["status"]]
        scanner = open_scanner(**query)
        status = settle(scanner, timeout=args.settle, quiet=args.quiet) if not show and args.settle > 0 else None
        pairs, events = scanner.pairs(), scanner.events()
    except (ValueError, FileNotFoundError, OSError) as exc:
        if not show and getattr(args, "format", None) == "json":
            print(json.dumps({"schema": SCHEMA, "status": "failed", "error": str(exc)}))
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not show and args.html:
        print(f"html {export_html(pairs, args.html)}", file=sys.stderr)
    if status:
        print(f"settle: {status}", file=sys.stderr)
    rc = _EXIT[status] if status else 0
    if show or args.format == "detail":
        details = call_details(pairs)
        print("\n\n".join(format_detail(d, full=args.full) for d in details))
        print(f"{len(details)} call(s)", file=sys.stderr)
        return (0 if details else 1) if show else rc
    if args.format == "pairs":
        for p in pairs:
            print(json.dumps(p, ensure_ascii=False, default=str))
    elif args.format == "jsonl":
        # Same row shape as cases/<id>/llm_calls.jsonl; seq numbered per case.
        for row in trace_rows(pairs):
            print(json.dumps(row, ensure_ascii=False, default=str))
    else:
        cols = ("ts", "call_id", "gateway", "case_id", "execution_id", "attribution_status", "state",
                "status_code", "latency_ms", "tok_in", "tok_out", "model")
        print("\t".join(cols))
        for p in pairs:
            usage = _usage(p)
            vals = {**p, "state": call_state(events.get(p["call_id"], [])),
                    "tok_in": usage.get("prompt_tokens", usage.get("input_tokens")),
                    "tok_out": usage.get("completion_tokens", usage.get("output_tokens"))}
            print("\t".join("-" if vals.get(c) is None else str(vals.get(c)) for c in cols))
        print(f"{len(pairs)} call(s)", file=sys.stderr)
    return rc


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `| head` should end quietly
    raise SystemExit(main())
