"""Case-level tool health: exit code 0 is not success if the tool layer itself was broken.

Background (2026-09-27): a bad itools sync made EVERY insurance tool call return
``PydanticUserError: `HarnessConf` is not fully defined``; the agents still exited 0, so all
120 Claude + 120 Pi cases were recorded as success.

Rule (Claude Code / Pi traces):
  * only insurance-tools calls count (``mcp__insurance-tools__X``, Pi ``insurance-tools_X``,
    Pi proxy ``mcp({tool: X})`` / bare ``X`` for the 7 published tools); builtin tools
    (bash/read/...) and Pi's ``mcp`` status call are ignored;
  * a tool result is **infra** (OUR side: tool layer / MCP / config) when its text carries a
    Python exception / MCP transport / connection signature;
  * a tool result is **upstream** (backend side, usually not ours to fix) when it is a tool
    envelope ``status=ERROR`` with ``error.code`` UPSTREAM/OVERLOADED/empty, ``status=TIMEOUT``,
    or the tool layer's generic backend-failure text ("本次工具调用失败", "后端服务暂时不可用",
    "上游响应格式不符合接口契约", business_unavailable ...);
  * a tool result is **ok** when it is not ``is_error`` and not infra — business "not found"
    (``EMPTY`` from the backend), ``AMBIGUOUS`` etc. are ok; envelopes answered locally without
    touching the backend (``source``/``request_id`` null, no facts: arg pre-checks such as
    "未提供客户中心 ID") are **neutral** — neither proof of health nor of breakage;
  * the case is flagged ``tool_infra_error`` when >=1 infra result and 0 ok results;
  * upstream errors never change success (``EVAL_TOOL_HEALTH_UPSTREAM_IS_INFRA=1`` restores the
    old strict behaviour); instead the case gets ``upstream_status``:
    ``upstream_blocked`` (>=1 upstream error, 0 ok) or ``upstream_degraded`` (>=1 upstream
    error, >=1 ok), plus ``upstream_errors`` {"CODE(hint)": count}.
  * ``UpstreamMonitor`` surfaces these live during a run (WARN lines, rolling-window ALERT,
    ``upstream_alert.json``).

Flagged cases get ``success=False``, ``error="tool_infra_error: ..."``, ``error_class="tool_infra_error"``;
every trace gets a ``tool_health`` summary. ``apply_tool_health`` is idempotent.

CLI (read-only, never rewrites a run):
  PYTHONPATH=src python3 -m eval_harness.tool_health <run_dir> [<run_dir> ...]
  PYTHONPATH=src python3 -m eval_harness.tool_health --status <run_dir> [...]   # live status line
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ERROR_CLASS = "tool_infra_error"
PUBLISHED_TOOLS = (
    "product_knowledge", "benefit_info", "general_qa", "policy_search",
    "customer_search", "policy_detail", "customer_detail",
)
INSURANCE_SERVER = "insurance-tools"
AGENT_HARNESSES = ("claude_code", "pi_coding")

INFRA_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (label, re.compile(rx)) for label, rx in (
        ("python_traceback", r"Traceback \(most recent call last\)"),
        ("pydantic_user_error", r"PydanticUserError|PydanticUndefinedAnnotation|PydanticSchemaGenerationError"),
        ("not_fully_defined", r"is not fully defined"),
        ("import_error", r"\b(ModuleNotFoundError|ImportError)\b"),
        ("python_exception", r"^(?:Error: )?(NameError|AttributeError|TypeError|KeyError|RuntimeError|"
                             r"AssertionError|ValueError|OSError|UnboundLocalError|RecursionError): "),
        ("connection_refused", r"Connection refused|ConnectionRefusedError|ConnectError|"
                               r"\[Errno 61\]|\[Errno 111\]|Failed to establish a new connection"),
        ("mcp_transport", r"MCP error -?\d+|McpError|MCP server .{0,40}(not connected|failed|disconnected|crashed)|"
                          r"Connection closed|server disconnected|Not connected|No such tool available|"
                          r"Failed to (start|connect to) MCP"),
    )
)
# Backend/upstream-side failures: exposed, but not our failure.
UPSTREAM_TEXT_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (label, re.compile(rx)) for label, rx in (
        ("TOOL_FAILURE_TEXT", r"本次(查询)?工具调用(失败|超时)"),
        ("BACKEND_UNAVAILABLE", r"后端服务暂时不可用|business_unavailable|BUSINESS_UNAVAILABLE"),
        ("UPSTREAM_CONTRACT", r"上游响应格式不符合接口契约"),
        ("OVERLOADED", r"查询服务暂时繁忙"),
    )
)
UPSTREAM_ERROR_CODES = {"UPSTREAM", "OVERLOADED", "TIMEOUT", ""}
UPSTREAM_BLOCKED = "upstream_blocked"
UPSTREAM_DEGRADED = "upstream_degraded"
OK_ENVELOPE_STATUSES = {"SUCCESS", "PARTIAL", "EMPTY", "AMBIGUOUS", "NEEDS_CLARIFICATION", "DENIED"}


def _upstream_is_infra() -> bool:
    """Default 0: upstream errors are exposed but never demote success."""
    return (os.environ.get("EVAL_TOOL_HEALTH_UPSTREAM_IS_INFRA") or "0").strip() == "1"


def result_text(content: Any) -> str:
    """Flatten Claude/Pi/MCP tool-result content to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(result_text(c) for c in content)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return result_text(content.get("content"))
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def _upstream_key(code: str, hint: Any) -> str:
    h = re.sub(r"\s+", " ", str(hint or "")).strip().rstrip("。.")[:24]
    return f"{code or 'ERROR'}({h})" if h else (code or "ERROR")


def classify_tool_result_ex(text: str, is_error: bool) -> Tuple[str, str, Optional[str]]:
    """Return (kind, reason, upstream_key); kind in {"infra","upstream","ok","neutral","error"}."""
    body = text or ""
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            env = json.loads(stripped)
        except (ValueError, TypeError):
            env = None
        if isinstance(env, dict) and "status" in env:
            status = str(env.get("status"))
            err = env.get("error") if isinstance(env.get("error"), dict) else {}
            code = str((err or {}).get("code") or "")
            if (status == "ERROR" and code in UPSTREAM_ERROR_CODES) or status == "TIMEOUT":
                key = _upstream_key(code or ("TIMEOUT" if status == "TIMEOUT" else ""), env.get("hint"))
                return "upstream", f"envelope status={status} code={code or '-'}", key
            if status in OK_ENVELOPE_STATUSES and not is_error:
                if env.get("source") is None and env.get("request_id") is None and not env.get("facts"):
                    # answered locally (arg pre-check / local dictionary) without reaching the
                    # backend: a fine business answer, but no proof the tool layer works
                    return "neutral", f"local envelope status={status}", None
                return "ok", f"envelope status={status}", None
            return ("error" if is_error or status == "ERROR" else "ok"), f"envelope status={status} code={code or '-'}", None
    for label, rx in INFRA_PATTERNS:
        if rx.search(body):
            return "infra", label, None
    for label, rx in UPSTREAM_TEXT_PATTERNS:
        m = rx.search(body)
        if m:
            return "upstream", f"text {label}", _upstream_key(label, m.group(0))
    if is_error:
        return "error", "is_error", None
    return "ok", "non-error result", None


def classify_tool_result(text: str, is_error: bool) -> Tuple[str, str]:
    """Return (kind, reason) with kind in {"infra", "upstream", "ok", "neutral", "error"}."""
    k, why, _ = classify_tool_result_ex(text, is_error)
    return k, why


def format_upstream_errors(errors: Optional[Dict[str, int]], limit: int = 4) -> str:
    items = sorted((errors or {}).items(), key=lambda kv: -kv[1])
    out = ", ".join(f"{k}x{v}" for k, v in items[:limit])
    return out + (f", +{len(items) - limit} more" if len(items) > limit else "")


def _insurance_tool_name(name: Optional[str], tool_input: Any) -> Optional[str]:
    n = str(name or "")
    if n.startswith(f"mcp__{INSURANCE_SERVER}__"):
        return n[len(f"mcp__{INSURANCE_SERVER}__"):]
    if n.startswith(f"{INSURANCE_SERVER}_"):
        return n[len(f"{INSURANCE_SERVER}_"):]
    if n in PUBLISHED_TOOLS:
        return n
    if n == "mcp" and isinstance(tool_input, dict) and tool_input.get("tool") in PUBLISHED_TOOLS:
        return str(tool_input["tool"])
    return None


def assess_events(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    uses: Dict[str, Optional[str]] = {}
    ok = infra = err = neutral = upstream = 0
    infra_reasons: Dict[str, int] = {}
    upstream_errors: Dict[str, int] = {}
    sample: Optional[str] = None
    up_sample: Optional[str] = None
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if kind == "tool_use":
            tid = str(p.get("id") or "")
            tool = _insurance_tool_name(p.get("name"), p.get("input"))
            if tool or tid not in uses:
                uses[tid] = tool or uses.get(tid)
        elif kind == "tool_result":
            tid = str(p.get("tool_use_id") or "")
            if not uses.get(tid):
                continue
            text = result_text(p.get("content"))
            k, why, key = classify_tool_result_ex(text, bool(p.get("is_error")))
            if k == "ok":
                ok += 1
            elif k == "infra":
                infra += 1
                infra_reasons[why] = infra_reasons.get(why, 0) + 1
                if sample is None:
                    sample = text.strip().replace("\n", " ")[:160]
            elif k == "upstream":
                upstream += 1
                upstream_errors[key or why] = upstream_errors.get(key or why, 0) + 1
                if up_sample is None:
                    up_sample = text.strip().replace("\n", " ")[:160]
            elif k == "neutral":
                neutral += 1
            else:
                err += 1
    strict = _upstream_is_infra()
    flagged = (infra + (upstream if strict else 0)) > 0 and ok == 0
    if upstream and ok == 0:
        up_status: Optional[str] = UPSTREAM_BLOCKED
    elif upstream:
        up_status = UPSTREAM_DEGRADED
    else:
        up_status = None
    return {
        "insurance_tool_results": ok + infra + err + neutral + upstream,
        "ok": ok,
        "local_only": neutral,
        "infra": infra,
        "upstream": upstream,
        "other_errors": err,
        "infra_reasons": infra_reasons,
        "infra_sample": sample,
        "upstream_errors": upstream_errors,
        "upstream_sample": up_sample,
        "upstream_status": up_status,
        "upstream_is_infra": strict,
        "flagged": flagged,
    }


def apply_tool_health(trace: Dict[str, Any], *, harness: Optional[str] = None) -> Dict[str, Any]:
    """Attach ``tool_health`` / ``upstream_status``; demote success only for OUR-side infra
    breakage. Idempotent, and undoes an earlier demotion made under different rules."""
    if not isinstance(trace, dict):
        return trace
    h = str(harness or trace.get("harness") or "")
    if h not in AGENT_HARNESSES:
        return trace
    summary = assess_events(trace.get("events") or [])
    trace["tool_health"] = summary
    if summary["upstream_status"]:
        trace["upstream_status"] = summary["upstream_status"]
        trace["upstream_errors"] = summary["upstream_errors"]
    else:
        trace.pop("upstream_status", None)
        trace.pop("upstream_errors", None)
    ours = str(trace.get("error") or "").startswith(f"{ERROR_CLASS}:")
    if summary["flagged"]:
        trace["error_class"] = ERROR_CLASS
        if trace.get("success") or not trace.get("error") or ours:
            trace["success"] = False
            n_bad = summary["infra"] + (summary["upstream"] if summary["upstream_is_infra"] else 0)
            trace["error"] = (
                f"{ERROR_CLASS}: {n_bad} infra-class tool error(s), 0 successful "
                f"insurance tool calls; e.g. {(summary['infra_sample'] or summary['upstream_sample'])!r}"
            )
    elif ours or trace.get("error_class") == ERROR_CLASS:
        # demoted earlier by this module under stricter rules -> restore
        if ours:
            trace["success"] = True
            trace["error"] = None
        trace.pop("error_class", None)
    return trace


def summarize_traces(traces: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-side counts for run_meta / manifest / html."""
    n = infra = 0
    blocked: List[str] = []
    degraded: List[str] = []
    errors: Dict[str, int] = {}
    for tr in traces:
        if not isinstance(tr, dict):
            continue
        n += 1
        if tr.get("error_class") == ERROR_CLASS:
            infra += 1
        st = tr.get("upstream_status")
        if st == UPSTREAM_BLOCKED:
            blocked.append(str(tr.get("case_id")))
        elif st == UPSTREAM_DEGRADED:
            degraded.append(str(tr.get("case_id")))
        for k, v in (tr.get("upstream_errors") or {}).items():
            errors[k] = errors.get(k, 0) + int(v)
    return {"cases": n, "tool_infra_error": infra, UPSTREAM_BLOCKED: len(blocked),
            UPSTREAM_DEGRADED: len(degraded), "blocked_ids": blocked, "degraded_ids": degraded,
            "upstream_errors": dict(sorted(errors.items(), key=lambda kv: -kv[1]))}


class UpstreamMonitor:
    """Live exposure of backend/upstream trouble during a run (never stops the run).

    * every blocked/degraded case -> ``[upstream] WARN ...`` line (+ fields on its progress row);
    * rolling window of the last ``EVAL_UPSTREAM_ALERT_WINDOW`` (10) finished cases: when
      blocked >= ``EVAL_UPSTREAM_ALERT_THRESHOLD`` (3) -> prominent ALERT line + progress row
      ``action=upstream_alert`` + ``<run_dir>/upstream_alert.json`` (rewritten while it holds).
    """

    def __init__(self, run_dir: Path, side: str, *, window: Optional[int] = None,
                 threshold: Optional[int] = None, emit=None, progress_writer=None) -> None:
        import collections
        import threading
        self.run_dir = Path(run_dir)
        self.side = side
        self.window = int(window or os.environ.get("EVAL_UPSTREAM_ALERT_WINDOW") or 10)
        self.threshold = int(threshold or os.environ.get("EVAL_UPSTREAM_ALERT_THRESHOLD") or 3)
        self.recent = collections.deque(maxlen=self.window)
        self.totals = {"cases": 0, UPSTREAM_BLOCKED: 0, UPSTREAM_DEGRADED: 0}
        self.codes: Dict[str, int] = {}
        self.alerts = 0
        self.first_alert_at: Optional[str] = None
        self.active = False
        self._lock = threading.Lock()
        self._emit = emit or (lambda line: print(line, flush=True))
        self._progress = progress_writer

    def observe(self, case_id: str, trace: Dict[str, Any]) -> Dict[str, Any]:
        from datetime import datetime
        st = (trace or {}).get("upstream_status")
        errs = (trace or {}).get("upstream_errors") or {}
        th = (trace or {}).get("tool_health") or {}
        with self._lock:
            self.totals["cases"] += 1
            self.recent.append((str(case_id), st == UPSTREAM_BLOCKED))
            if st in (UPSTREAM_BLOCKED, UPSTREAM_DEGRADED):
                self.totals[st] += 1
                for k, v in errs.items():
                    self.codes[k] = self.codes.get(k, 0) + int(v)
                self._emit(f"[upstream] WARN side={self.side} case={case_id} status={st} "
                           f"upstream_errors={th.get('upstream', sum(errs.values()))} ok_calls={th.get('ok')} "
                           f"codes={format_upstream_errors(errs)} (backend-side; success unchanged)")
            blocked_ids = [c for c, b in self.recent if b]
            alert = len(blocked_ids) >= self.threshold
            rec = {"side": self.side, "alert": alert, "window": self.window, "threshold": self.threshold,
                   "recent_blocked": len(blocked_ids), "recent_cases": len(self.recent),
                   "recent_blocked_ids": blocked_ids, "totals": dict(self.totals),
                   "upstream_errors": dict(sorted(self.codes.items(), key=lambda kv: -kv[1]))}
            if alert and st == UPSTREAM_BLOCKED:
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                self.alerts += 1
                self.first_alert_at = self.first_alert_at or now
                rec.update({"alerts": self.alerts, "first_alert_at": self.first_alert_at,
                            "last_alert_at": now, "last_case": str(case_id)})
                bar = "!" * 78
                self._emit(f"{bar}\n[upstream] ALERT side={self.side} {len(blocked_ids)}/{len(self.recent)} of the "
                           f"last cases are upstream_blocked (threshold {self.threshold}/{self.window}): "
                           f"{','.join(blocked_ids)}; top codes: {format_upstream_errors(self.codes, 3)} "
                           f"-- backend looks unhealthy; run continues, results for these cases are "
                           f"not meaningful\n{bar}")
                try:
                    self.run_dir.mkdir(parents=True, exist_ok=True)
                    (self.run_dir / "upstream_alert.json").write_text(
                        json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                except OSError:
                    pass
                if self._progress is not None:
                    try:
                        self._progress({"ts": now, "case_id": str(case_id), "action": "upstream_alert", **rec})
                    except Exception:
                        pass
            elif self.active and not alert:
                self._emit(f"[upstream] alert cleared side={self.side} recent_blocked={len(blocked_ids)}/{len(self.recent)}")
            self.active = alert
            return rec


def status_line(run_dir: Path) -> Dict[str, Any]:
    """Live status from progress.jsonl (latest row per case) + upstream_alert.json."""
    run_dir = Path(run_dir)
    latest: Dict[str, Dict[str, Any]] = {}
    pp = run_dir / "progress.jsonl"
    if pp.is_file():
        with pp.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("case_id") and r.get("action") in ("run", "rerun", "rejudge"):
                    latest[str(r["case_id"])] = r
    rows = list(latest.values())
    rep = {"run_dir": str(run_dir), "done": len(rows),
           "success": sum(1 for r in rows if r.get("success")),
           "tool_infra_error": sum(1 for r in rows if r.get("error_class") == ERROR_CLASS),
           UPSTREAM_BLOCKED: sorted(str(r["case_id"]) for r in rows if r.get("upstream_status") == UPSTREAM_BLOCKED),
           UPSTREAM_DEGRADED: sum(1 for r in rows if r.get("upstream_status") == UPSTREAM_DEGRADED),
           "alert": None}
    ap = run_dir / "upstream_alert.json"
    if ap.is_file():
        try:
            a = json.loads(ap.read_text(encoding="utf-8"))
            rep["alert"] = {k: a.get(k) for k in ("alert", "recent_blocked", "recent_cases", "last_alert_at",
                                                  "alerts", "recent_blocked_ids")}
        except ValueError:
            pass
    return rep


def format_status(rep: Dict[str, Any], side: Optional[str] = None) -> str:
    b = rep[UPSTREAM_BLOCKED]
    al = rep.get("alert") or {}
    alert_s = (f"ALERT(x{al.get('alerts')} last={al.get('last_alert_at')})" if al.get("alerts") else "no")
    return (f"[upstream-status] {side or Path(rep['run_dir']).name} done={rep['done']} ok={rep['success']} "
            f"tool_infra_error={rep['tool_infra_error']} upstream_blocked={len(b)}"
            f"{(' ' + ','.join(b[:8])) if b else ''} upstream_degraded={rep[UPSTREAM_DEGRADED]} alert={alert_s}")


def rejudge_run_dir(run_dir: Path) -> Dict[str, Any]:
    """Read-only: re-derive status for every case trace under run_dir/cases."""
    run_dir = Path(run_dir)
    n = was_ok = now_ok = flagged = 0
    traces: List[Dict[str, Any]] = []
    flagged_ids: List[str] = []
    reasons: Dict[str, int] = {}
    for tp in sorted((run_dir / "cases").glob("*/trace.json")):
        try:
            tr = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            continue
        n += 1
        before = bool(tr.get("success"))
        was_ok += before
        apply_tool_health(tr)
        now_ok += bool(tr.get("success"))
        traces.append({k: tr.get(k) for k in ("case_id", "error_class", "upstream_status", "upstream_errors")})
        if tr.get("tool_health", {}).get("flagged"):
            flagged += 1
            flagged_ids.append(str(tr.get("case_id")))
            for k, v in tr["tool_health"]["infra_reasons"].items():
                reasons[k] = reasons.get(k, 0) + v
    return {"run_dir": str(run_dir), "cases": n, "success_before": was_ok, "success_after": now_ok,
            "flagged_tool_infra_error": flagged, "flagged_ids": flagged_ids, "infra_reasons": reasons,
            "upstream": summarize_traces(traces)}


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--status":
        for d in args[1:]:
            print(format_status(status_line(Path(d))))
        return 0
    for d in args:
        rep = rejudge_run_dir(Path(d))
        ids = rep.pop("flagged_ids")
        rep["flagged_ids_head"] = ids[:10]
        print(json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
