"""Thin adapter: insurance-style Agno OpenAIChat → local relay with eval_case_id.

Also owns the **live QA batch** runner that hits Insurance `/v1/chat` (not the
relay smoke path), ingests gateway llm_calls by execution_id,
and calls `build_llm_trace_html(run_dir)` at end of run — same auto-HTML pattern
as Claude `eval_harness.run`.

Does not import or modify the insurance_qa_agent repo.
"""
from __future__ import annotations

from eval_harness.trace_identity import archive_case, new_identity, write_identity, trace_headers

import argparse
import asyncio
import concurrent.futures
import threading
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from eval_harness.relay_inject import apply_openai_compatible_case_id, openai_compatible_kwargs
from eval_harness.llm_gateway_ingest import read_case_llm_jsonl_text

TZ = ZoneInfo("Asia/Shanghai")

# Canonical output root (profile absolute eval_runs_dir) — NOT workspaces/claude/eval_runs.
from eval_harness.paths import PROJECT_ROOT as MOCK_SYSTEM_ROOT
DEFAULT_EVAL_RUNS_DIR = MOCK_SYSTEM_ROOT / "eval_runs"
DEFAULT_GATEWAY_LOG = MOCK_SYSTEM_ROOT / "llm_gateway" / "logs" / "llm_calls.jsonl"
DEFAULT_BUNDLE = (
    MOCK_SYSTEM_ROOT / "eval_harness" / "bundles" / "120_prompt_only_v1.jsonl"
)
DEFAULT_CHAT_URL = "http://127.0.0.1:18063/v1/chat"
DEFAULT_USER_ID = "1050900009"
BUSY_MARKERS = ("模型服务繁忙",)


@dataclass
class InsuranceSmokeResult:
    case_id: str
    ok: bool
    model: str
    base_url: str
    reply_preview: str
    wall_ms: int
    error: Optional[str] = None
    call_meta: Optional[dict[str, Any]] = None


def _rebuild_live_summary(cases_root: Path) -> list[dict[str, Any]]:
    """Rebuild the cumulative run summary from durable per-case artifacts."""
    rows: list[dict[str, Any]] = []
    for case_dir in sorted(cases_root.glob("INS_*")):
        meta_path = case_dir / "meta.json"
        if not case_dir.is_dir() or not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        n_calls = 0
        try:
            _llm_txt = read_case_llm_jsonl_text(case_dir)
            if _llm_txt:
                n_calls = sum(1 for line in _llm_txt.splitlines() if line.strip())
        except OSError:
            n_calls = 0
        answer = str(meta.get("answer") or "")
        rows.append(
            {
                "case_id": str(meta.get("case_id") or case_dir.name),
                "execution_id": meta.get("execution_id"),
                "attribution_status": meta.get("attribution_status"),
                "bundle_case_id": meta.get("bundle_case_id"),
                "http_status": meta.get("http_status"),
                "ms": meta.get("wall_ms"),
                "ok": bool(meta.get("success")),
                "busy": _is_busy_answer(answer),
                "llm_calls": n_calls,
                "answer": answer[:500],
                "error": meta.get("error"),
            }
        )
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_tag() -> str:
    return datetime.now(TZ).strftime("%Y%m%d_%H%M%S")


def default_relay_base_url() -> str:
    return os.environ.get("INSURANCE_RELAY_BASE_URL", "http://127.0.0.1:4001/v1").rstrip("/")


def default_relay_api_key() -> str:
    key = os.environ.get("INSURANCE_RELAY_API_KEY") or os.environ.get("LITELLM_MASTER_KEY")
    if key:
        return key
    env_path = MOCK_SYSTEM_ROOT / "llm_gateway" / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "LITELLM_MASTER_KEY":
                return value.strip().strip('"').strip("'")
    raise RuntimeError("INSURANCE_RELAY_API_KEY or LITELLM_MASTER_KEY is not set")


def default_model_id() -> str:
    return os.environ.get("INSURANCE_RELAY_MODEL", os.environ.get("UPSTREAM_MODEL", "deepseek-v4-flash"))


def _no_proxy_env() -> None:
    """macOS system proxy (e.g. 127.0.0.1:7890) can hijack local relay calls → 502."""
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        # Do not delete user proxies for other hosts; httpx trust_env=False is the real fix.
        pass


def build_openai_chat(
    *,
    case_id: str,
    model_id: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
):
    """Construct an Agno OpenAIChat pointed at the local relay with case_id stamped."""
    import httpx
    from agno.models.openai import OpenAIChat

    _no_proxy_env()
    mid = model_id or default_model_id()
    url = base_url or default_relay_base_url()
    key = api_key or default_relay_api_key()
    # trust_env=False: ignore macOS/urllib system proxy for 127.0.0.1
    http_client = httpx.Client(trust_env=False, timeout=120.0)
    async_http_client = httpx.AsyncClient(trust_env=False, timeout=120.0)
    model = OpenAIChat(
        id=mid,
        base_url=url,
        api_key=key,
        max_tokens=64,
        http_client=http_client,  # sync path
        **openai_compatible_kwargs(case_id),
    )
    # Agno stores one http_client; force async client after init
    model.http_client = async_http_client
    model.client = None
    model.async_client = None
    apply_openai_compatible_case_id(model, case_id)
    return model


async def _achat_once(model: Any, prompt: str) -> str:
    # Prefer async OpenAI path used by Agno
    if hasattr(model, "ainvoke"):
        # Agno Model.ainvoke signature varies; use get_async_client chat.completions
        pass
    client = model.get_async_client()
    kwargs = model.get_request_params() if hasattr(model, "get_request_params") else {}
    # get_request_params may need args — fall back to explicit extra_*
    create_kwargs: dict[str, Any] = {
        "model": model.id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": getattr(model, "max_tokens", 64) or 64,
    }
    if getattr(model, "extra_headers", None):
        create_kwargs["extra_headers"] = model.extra_headers
    if getattr(model, "extra_body", None):
        create_kwargs["extra_body"] = model.extra_body
    resp = await client.chat.completions.create(**create_kwargs)
    choice = (resp.choices or [None])[0]
    if choice is None:
        return ""
    msg = choice.message
    return (msg.content or "") if msg is not None else ""


def run_smoke_turn(
    *,
    case_id: str,
    prompt: str = "ping insurance relay adapter",
    case_dir: Optional[Path] = None,
    model_id: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> InsuranceSmokeResult:
    """One Chat Completions turn through the relay; optional artifact dir."""
    mid = model_id or default_model_id()
    url = base_url or default_relay_base_url()
    key = api_key or default_relay_api_key()
    t0 = time.time()
    error: Optional[str] = None
    preview = ""
    meta: dict[str, Any] = {
        "case_id": case_id,
        "model": mid,
        "base_url": url,
        "ts": _now(),
        "injection": openai_compatible_kwargs(case_id),
    }
    try:
        model = build_openai_chat(case_id=case_id, model_id=mid, base_url=url, api_key=key)
        preview = asyncio.run(_achat_once(model, prompt))
        ok = True
    except Exception as e:
        ok = False
        error = f"{type(e).__name__}: {e}"
    wall_ms = int((time.time() - t0) * 1000)
    result = InsuranceSmokeResult(
        case_id=case_id,
        ok=ok,
        model=mid,
        base_url=url,
        reply_preview=(preview or "")[:300],
        wall_ms=wall_ms,
        error=error,
        call_meta=meta,
    )
    if case_dir is not None:
        case_dir = Path(case_dir)
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "insurance_smoke.json").write_text(
            json.dumps(
                {
                    "case_id": result.case_id,
                    "ok": result.ok,
                    "model": result.model,
                    "base_url": result.base_url,
                    "reply_preview": result.reply_preview,
                    "wall_ms": result.wall_ms,
                    "error": result.error,
                    "meta": result.call_meta,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return result


# ---------------------------------------------------------------------------
# Live QA batch (Insurance /v1/chat) — auto HTML like Claude eval_harness.run
# ---------------------------------------------------------------------------


def _bundle_case_id_to_ins(bundle_case_id: str) -> str:
    """Map bundle A01 → INS_A01 (already-prefixed ids pass through)."""
    cid = bundle_case_id.strip()
    if cid.upper().startswith("INS_"):
        return cid if cid.startswith("INS_") else "INS_" + cid[4:]
    return f"INS_{cid}"


def _load_prompts_from_bundle(
    bundle_path: Path, bundle_case_ids: list[str]
) -> list[tuple[str, str, list[str]]]:
    """Return list of (bundle_id, ins_case_id, turns)."""
    from eval_harness.bundle_io import filter_bundle_cases, load_bundle

    cases = load_bundle(bundle_path)
    selected = filter_bundle_cases(cases, bundle_case_ids)
    if len(selected) != len(bundle_case_ids):
        found = {c.case_id for c in selected}
        missing = [i for i in bundle_case_ids if i not in found]
        raise SystemExit(f"Bundle cases not found: {missing} in {bundle_path}")
    out: list[tuple[str, str, list[str]]] = []
    for c in selected:
        turns = [turn.strip() for turn in c.turns]
        if not turns or any(not turn for turn in turns):
            raise SystemExit(f"Empty prompt for bundle case {c.case_id}")
        out.append((c.case_id, _bundle_case_id_to_ins(c.case_id), turns))
    return out


def _answer_from_response(body: Any) -> str:
    if isinstance(body, dict):
        ans = body.get("answer")
        if ans is not None:
            return str(ans)
        return json.dumps(body, ensure_ascii=False)
    return str(body or "")


def _is_busy_answer(answer: str) -> bool:
    return any(m in (answer or "") for m in BUSY_MARKERS)


def _post_live_chat(
    *,
    chat_url: str,
    user_id: str,
    session_id: str,
    prompt: str,
    timeout_sec: float,
    messages: Optional[list[dict[str, str]]] = None,
    case_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    run_id: Optional[str] = None,
    started_at: Optional[str] = None,
) -> tuple[int, Any, Optional[str]]:
    """POST Insurance /v1/chat with proxy bypass. Returns (status, body, error).

    When case_id + execution_id are provided, stamp X-Eval-* headers so the
    Insurance eval passthrough can forward them to LiteLLM for concurrent attribution.
    """
    import httpx

    _no_proxy_env()
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "stream": False,
        "messages": messages or [{"role": "user", "content": prompt}],
    }
    headers: dict[str, str] = trace_headers(run_id=run_id, harness="insurance_qa_agno")
    if started_at:
        headers["X-Eval-Started-At"] = started_at
    if case_id:
        headers["X-Eval-Case-Id"] = case_id
    if execution_id:
        headers["X-Eval-Execution-Id"] = execution_id
    try:
        with httpx.Client(trust_env=False, timeout=timeout_sec) as client:
            r = client.post(chat_url, json=payload, headers=headers or None)
        try:
            body: Any = r.json()
        except Exception:
            body = {"raw_text": r.text}
        return r.status_code, body, None
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def _ingest_case_gateway(
    *,
    case_dir: Path,
    case_id: str,
    gateway_log: Path,
    execution_id: str,
    start_offset: int,
) -> tuple[int, bool]:
    """Ingest only model calls bound by the gateway to this execution.

    LiteLLM writes terminal callback events asynchronously.  The business API
    can therefore return a few milliseconds before the final success record is
    durable.  Drain that bounded race without ever widening the execution-id
    filter or accepting an incomplete call.
    """
    from eval_harness.llm_gateway_ingest import (
        filter_pairs_by_execution_id,
        load_gateway_pairs,
        pairs_to_trace_calls,
        write_case_llm_jsonl,
    )

    import time

    deadline = time.monotonic() + 30.0
    while True:
        pairs = filter_pairs_by_execution_id(
            load_gateway_pairs(gateway_log, start_offset=start_offset, strict=True),
            execution_id=execution_id, case_id=case_id,
        )
        complete = all(
            p.get("request") is not None
            and (p.get("status_code") is not None or p.get("error") is not None)
            for p in pairs
        )
        if complete or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    calls = pairs_to_trace_calls(pairs, case_id=case_id)
    write_case_llm_jsonl(case_dir / "llm_calls.jsonl", calls)
    return len(calls), complete


def _case_submit_delay(index: int) -> float:
    """Seconds to sleep before submitting case index (>0) into the pool."""
    import random as _random
    stagger = 0.0
    raw = (os.environ.get("EVAL_CASE_STAGGER_SEC") or "").strip()
    if raw:
        try:
            stagger = max(0.0, float(raw))
        except ValueError:
            stagger = 0.0
    jitter = 0.0
    raw_j = (os.environ.get("EVAL_CASE_JITTER_SEC") or "").strip()
    if raw_j:
        try:
            jitter = max(0.0, float(raw_j))
        except ValueError:
            jitter = 0.0
    if index <= 0:
        return 0.0
    delay = stagger
    if jitter > 0:
        delay += _random.uniform(0.0, jitter)
    return delay


def run_live_batch(
    *,
    bundle_case_ids: list[str],
    run_id: Optional[str] = None,
    chat_url: str = DEFAULT_CHAT_URL,
    user_id: str = DEFAULT_USER_ID,
    bundle_path: Optional[Path] = None,
    eval_runs_dir: Optional[Path] = None,
    gateway_log: Optional[Path] = None,
    timeout_sec: float = 180.0,
    resume: bool = False,
    lane_id: str = "insurance_1",
    attribution_config: Optional[Path] = None,
) -> dict[str, Any]:
    """Serialize writers to a batch directory while cases still run concurrently."""
    from eval_harness.trace_identity import writer_lock
    run_id = run_id or f"insurance_datasetA_{_now_tag()}_{uuid.uuid4().hex[:8]}"
    with writer_lock(Path(eval_runs_dir or DEFAULT_EVAL_RUNS_DIR) / run_id):
        return _run_live_batch(
            bundle_case_ids=bundle_case_ids, run_id=run_id, chat_url=chat_url,
            user_id=user_id, bundle_path=bundle_path, eval_runs_dir=eval_runs_dir,
            gateway_log=gateway_log, timeout_sec=timeout_sec, resume=resume,
            lane_id=lane_id, attribution_config=attribution_config,
        )


def _run_live_batch(
    *,
    bundle_case_ids: list[str],
    run_id: Optional[str] = None,
    chat_url: str = DEFAULT_CHAT_URL,
    user_id: str = DEFAULT_USER_ID,
    bundle_path: Optional[Path] = None,
    eval_runs_dir: Optional[Path] = None,
    gateway_log: Optional[Path] = None,
    timeout_sec: float = 180.0,
    resume: bool = False,
    lane_id: str = "insurance_1",
    attribution_config: Optional[Path] = None,
) -> dict[str, Any]:
    """Live Insurance QA batch: per-case chat + gateway ingest + auto llm_trace.html."""
    from eval_harness.llm_trace_html import build_llm_trace_html
    from eval_harness.attribution_lane import LaneLease, lane_config, require_gateway_ready

    bundle_path = Path(bundle_path or DEFAULT_BUNDLE)
    eval_runs_dir = Path(eval_runs_dir or DEFAULT_EVAL_RUNS_DIR)
    gateway_log = Path(gateway_log or DEFAULT_GATEWAY_LOG)
    run_id = run_id or f"insurance_datasetA_{_now_tag()}_{uuid.uuid4().hex[:8]}"
    attribution_config = Path(attribution_config or MOCK_SYSTEM_ROOT / "llm_gateway" / "attribution_lanes.json")
    lane_directory, _ = lane_config(attribution_config, lane_id, chat_url)
    # Insurance live batch attributes via the dedicated :4002 gateway process.
    ready_port = os.environ.get("INSURANCE_LITELLM_PORT") or os.environ.get("LLM_ATTRIBUTION_READY_PORT") or "4002"
    require_gateway_ready(lane_directory, attribution_config, port=ready_port)
    if not gateway_log.is_file():
        raise RuntimeError(f"gateway log unavailable: {gateway_log}")
    full_run_id = f"{eval_runs_dir.name}/{run_id}"
    run_dir = eval_runs_dir / run_id
    cases_root = run_dir / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)

    # Parallel-safe: if another live-batch owns this run_dir (or already finished),
    # do not start a second writer. Dual sequential phase can reuse the parallel result.
    import time as _time
    _lock = run_dir / ".live_batch.pid"
    _html = run_dir / "llm_trace.html"
    _summary = run_dir / "summary.json"

    def _batch_done() -> bool:
        return (
            _html.is_file()
            and _html.stat().st_size > 500
            and _summary.is_file()
            and _summary.stat().st_size > 20
        )

    def _lock_holder_pid() -> Optional[int]:
        if not _lock.is_file():
            return None
        try:
            pid = int(_lock.read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            return None
        if pid <= 0 or pid == os.getpid():
            return None
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None

    def _result_from_existing() -> dict[str, Any]:
        rows = json.loads(_summary.read_text(encoding="utf-8"))
        if isinstance(rows, dict) and "cases" in rows:
            rows = rows["cases"]
        n_cases = len(rows) if isinstance(rows, list) else 0
        n_ok = sum(1 for r in rows if r.get("ok")) if isinstance(rows, list) else 0
        print(
            f"[live-batch] reuse existing run_dir={run_dir} "
            f"success={n_ok}/{n_cases} html={_html}",
            flush=True,
        )
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "html_path": str(_html),
            "html_error": None,
            "n_ok": n_ok,
            "n_cases": n_cases,
            "summary": rows if isinstance(rows, list) else [],
            "auto_html": True,
            "reused": True,
        }

    if _batch_done() and not resume:
        return _result_from_existing()

    holder = _lock_holder_pid()
    if holder is not None:
        print(
            f"[live-batch] waiting for parallel owner pid={holder} under {run_dir}",
            flush=True,
        )
        _deadline = _time.time() + 60 * 60 * 6
        while _time.time() < _deadline:
            if _batch_done():
                return _result_from_existing()
            if _lock_holder_pid() is None:
                break
            _time.sleep(5.0)
        if _batch_done():
            return _result_from_existing()

    _lock.write_text(f"{os.getpid()}\n", encoding="utf-8")

    selected = _load_prompts_from_bundle(bundle_path, bundle_case_ids)
    started_at = datetime.now(TZ).isoformat(timespec="seconds")
    summary_rows: list[dict[str, Any]] = []

    print(f"[live-batch] run_id={run_id}", flush=True)
    print(f"[live-batch] run_dir={run_dir}", flush=True)
    print(f"[live-batch] chat_url={chat_url} user_id={user_id}", flush=True)
    print(f"[live-batch] bundle={bundle_path} cases={','.join(b for b, _, _ in selected)}", flush=True)
    print(f"[live-batch] gateway_log={gateway_log}", flush=True)
    if resume:
        print("[live-batch] resume=1 (skip completed cases; archive incomplete attempts)", flush=True)

    n_skipped = 0
    _conc_raw = (os.environ.get("INSURANCE_LIVE_CONCURRENCY") or os.environ.get("EVAL_CONCURRENCY") or "1").strip()
    try:
        concurrency = max(1, int(_conc_raw))
    except ValueError:
        concurrency = 1
    print(f"[live-batch] concurrency={concurrency}", flush=True)
    _summary_lock = threading.Lock()

    def _append_summary(row: dict[str, Any]) -> None:
        with _summary_lock:
            summary_rows.append(row)

    class _NullLane:
        def start(self, **kwargs):  # dual-id path; lane registry unused
            return None
        def finish(self, **kwargs):
            return None

    def _run_one_insurance_case(bundle_id: str, ins_id: str, turns: list, lane) -> None:
        nonlocal n_skipped
        case_dir = cases_root / ins_id
        if resume:
            meta_path = case_dir / "meta.json"
            prev_ok = False
            prev_meta: Optional[dict[str, Any]] = None
            if meta_path.is_file() and meta_path.stat().st_size > 20:
                try:
                    prev_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    prev_ok = (
                        bool(prev_meta.get("success"))
                        and prev_meta.get("attribution_status") in ("captured", "unknown_no_calls")
                        and int(prev_meta.get("num_turns") or 0) == len(turns)
                    )
                except Exception:
                    prev_meta = None
                    prev_ok = False
            if prev_ok and prev_meta is not None:
                n_calls = 0
                try:
                    _llm_txt = read_case_llm_jsonl_text(case_dir)
                    if _llm_txt:
                        n_calls = sum(1 for line in _llm_txt.splitlines() if line.strip())
                except Exception:
                    n_calls = 0
                _append_summary(
                    {
                        "case_id": ins_id,
                        "execution_id": prev_meta.get("execution_id"),
                        "attribution_status": prev_meta.get("attribution_status"),
                        "bundle_case_id": bundle_id,
                        "http_status": prev_meta.get("http_status"),
                        "ms": prev_meta.get("wall_ms"),
                        "ok": True,
                        "busy": False,
                        "llm_calls": n_calls,
                        "answer": (prev_meta.get("answer") or "")[:500],
                        "error": None,
                        "skipped": True,
                    }
                )
                with _summary_lock:
                    n_skipped += 1
                print(f"[case] {ins_id} SKIP success (resume)", flush=True)
                return
            if case_dir.exists():
                print(f"[case] {ins_id} ARCHIVE incomplete then run", flush=True)
                archive_case(case_dir)
        if not resume:
            archive_case(case_dir)
        case_dir.mkdir(parents=True, exist_ok=True)
        session_id = f"eval_{ins_id}_{uuid.uuid4().hex[:12]}"
        identity = new_identity(str(run_id), "insurance_qa_agno", ins_id)
        execution_id = identity["execution_id"]
        write_identity(case_dir, identity)
        start_offset = gateway_log.stat().st_size
        case_t0 = datetime.now(TZ)
        (case_dir / "prompt.txt").write_text(turns[0], encoding="utf-8")
        (case_dir / "prompt_turns.json").write_text(
            json.dumps(
                {"case_id": ins_id, "turns": [
                    {"index": index, "prompt": prompt}
                    for index, prompt in enumerate(turns, start=1)
                ]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        lane.start(run_id=full_run_id, case_id=ins_id, execution_id=execution_id)
        print(
            f"[case] {ins_id} (bundle={bundle_id}, turns={len(turns)}) ...",
            flush=True,
        )

        history: list[dict[str, str]] = []
        turn_rows: list[dict[str, Any]] = []
        status = 0
        body: Any = None
        err: Optional[str] = None
        for turn_index, prompt in enumerate(turns, start=1):
            turn_t0 = datetime.now(TZ)
            request_messages = [*history, {"role": "user", "content": prompt}]
            status, body, err = _post_live_chat(
                chat_url=chat_url,
                user_id=user_id,
                session_id=session_id,
                prompt=prompt,
                timeout_sec=timeout_sec,
                messages=request_messages,
                case_id=ins_id,
                execution_id=execution_id,
                run_id=str(run_id),
                started_at=identity["started_at"],
            )
            turn_t1 = datetime.now(TZ)
            answer = _answer_from_response(body) if body is not None else ""
            turn_busy = _is_busy_answer(answer)
            turn_ok = (
                status == 200 and err is None and not turn_busy and bool(answer.strip())
            )
            turn_error = err or (("busy:" + answer[:80]) if turn_busy else None)
            turn_row = {
                "index": turn_index,
                "prompt": prompt,
                "answer": answer,
                "final_text": answer,
                "http_status": status,
                "wall_ms": int((turn_t1 - turn_t0).total_seconds() * 1000),
                "t0": turn_t0.isoformat(timespec="milliseconds"),
                "t1": turn_t1.isoformat(timespec="milliseconds"),
                "success": turn_ok,
                "error": turn_error,
            }
            turn_rows.append(turn_row)
            (case_dir / f"response_turn{turn_index}.json").write_text(
                json.dumps(
                    body if body is not None else {"error": err},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"[case] {ins_id} turn={turn_index}/{len(turns)} "
                f"{'OK' if turn_ok else 'FAIL'} http={status} "
                f"wall_ms={turn_row['wall_ms']}",
                flush=True,
            )
            if not turn_ok:
                break
            history.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer},
                ]
            )
        case_t1 = datetime.now(TZ)
        wall_ms = int((case_t1 - case_t0).total_seconds() * 1000)
        answer = turn_rows[-1]["answer"] if turn_rows else ""
        busy = any(_is_busy_answer(str(row.get("answer") or "")) for row in turn_rows)
        ok = len(turn_rows) == len(turns) and all(row["success"] for row in turn_rows)
        case_error = next((row["error"] for row in turn_rows if row["error"]), None)

        # Case-dir artifacts (Claude-like layout)
        (case_dir / "response.json").write_text(
            json.dumps(body if body is not None else {"error": err}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (case_dir / "responses.json").write_text(
            json.dumps(turn_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        meta = {
            "case_id": ins_id,
            "execution_id": execution_id,
            "lane_id": lane_id,
            "run_id": full_run_id,
            "bundle_case_id": bundle_id,
            "http_status": status,
            "wall_ms": wall_ms,
            "t0": case_t0.isoformat(timespec="milliseconds"),
            "t1": case_t1.isoformat(timespec="milliseconds"),
            "user_id": user_id,
            "session_id": session_id,
            "chat_url": chat_url,
            "prompt": turns[0],
            "answer": answer,
            "num_turns": len(turn_rows),
            "expected_turns": len(turns),
            "turns": turn_rows,
            "success": ok,
            "error": case_error,
            "response": body if isinstance(body, dict) else {"raw": body},
        }
        (case_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # insurance_only-style sidecar next to cases/<id>/
        sidecar = {
            "case_id": ins_id,
            "execution_id": execution_id,
            "lane_id": lane_id,
            "bundle_case_id": bundle_id,
            "http_status": status,
            "wall_ms": wall_ms,
            "ms": wall_ms,
            "t0": meta["t0"],
            "t1": meta["t1"],
            "user_id": user_id,
            "session_id": session_id,
            "success": ok,
            "num_turns": len(turn_rows),
            "expected_turns": len(turns),
            "turns": turn_rows,
            "answer": answer,
            "error": meta["error"],
            "response": body if isinstance(body, dict) else {"raw": body},
        }
        (cases_root / f"{ins_id}.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        n_calls = 0
        calls_complete = False
        ingest_error = None
        try:
            n_calls, calls_complete = _ingest_case_gateway(
                case_dir=case_dir,
                case_id=ins_id,
                gateway_log=gateway_log,
                execution_id=execution_id,
                start_offset=start_offset,
            )
            print(f"[case] {ins_id} llm_calls={n_calls} from {gateway_log.name}", flush=True)
        except Exception as e:
            ingest_error = f"{type(e).__name__}: {e}"
            print(f"[case] {ins_id} llm_calls ingest failed: {e!r}", flush=True)
        transport_failed = any(
            row.get("error") and not str(row.get("error")).startswith("busy:")
            for row in turn_rows
        ) or any(row.get("http_status") == 0 for row in turn_rows)
        blocked = bool(transport_failed or ingest_error or not calls_complete)
        meta["attribution_status"] = (
            "capture_error" if ingest_error else "incomplete_calls" if not calls_complete
            else "unknown_no_calls" if n_calls == 0 else "captured"
        )
        (case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        sidecar["attribution_status"] = meta["attribution_status"]
        (cases_root / f"{ins_id}.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lane.finish(blocked=blocked, reason=err or ingest_error or ("incomplete_calls" if blocked else None))
        if blocked:
            msg = f"lane {lane_id} blocked after {ins_id}: {err or ingest_error or 'incomplete_calls'}"
            # Continue batch on timeout/block for all concurrencies (was: raise when
            # concurrency<=1, which aborted the whole live-batch on one ReadTimeout).
            # LaneLease.start() overwrites state within the same lease, so later cases
            # can proceed; failed cases remain for --resume / operator retry.
            print(f"[case] {ins_id} BLOCKED (continue): {msg}", flush=True)
            _append_summary(
                {
                    "case_id": ins_id,
                    "execution_id": execution_id,
                    "attribution_status": meta.get("attribution_status"),
                    "bundle_case_id": bundle_id,
                    "http_status": status,
                    "ms": wall_ms,
                    "ok": False,
                    "busy": busy,
                    "llm_calls": n_calls,
                    "answer": (answer or "")[:500],
                    "error": msg,
                }
            )
            return

        preview = (answer or "")[:120].replace("\n", " ")
        status_s = "OK" if ok else "FAIL"
        print(
            f"[case] {ins_id} {status_s} http={status} wall_ms={wall_ms} busy={busy} "
            f"err={err!r} answer={preview!r}",
            flush=True,
        )
        _append_summary(
            {
                "case_id": ins_id,
                "execution_id": execution_id,
                "attribution_status": meta["attribution_status"],
                "bundle_case_id": bundle_id,
                "http_status": status,
                "ms": wall_ms,
                "ok": ok,
                "busy": busy,
                "llm_calls": n_calls,
                "answer": (answer or "")[:500],
                "error": meta["error"],
            }
        )

    if concurrency <= 1:
        with LaneLease(lane_directory, lane_id) as lane:
            for bundle_id, ins_id, turns in selected:
                _run_one_insurance_case(bundle_id, ins_id, turns, lane)
    else:
        # Dual-id headers attribute; exclusive LaneLease would serialize — skip it.
        null_lane = _NullLane()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            import time as _stagger_time
            futs = []
            for _si, (bundle_id, ins_id, turns) in enumerate(selected):
                _delay = _case_submit_delay(_si)
                if _delay > 0:
                    print(f"[live-batch] stagger submit i={_si} sleep={_delay:.2f}s", flush=True)
                    _stagger_time.sleep(_delay)
                futs.append(pool.submit(_run_one_insurance_case, bundle_id, ins_id, turns, null_lane))
            errors = []
            for fut in concurrent.futures.as_completed(futs):
                exc = fut.exception()
                if exc is not None:
                    errors.append(exc)
                    print(f"[live-batch] worker error: {exc!r}", flush=True)
            if errors:
                raise errors[0]

    # A subset resume must not replace the full run summary with only the
    # selected cases. Reconstruct from the durable case directories, as the
    # Claude runner already does for its spreadsheet and HTML.
    summary_rows = _rebuild_live_summary(cases_root)
    finished_at = datetime.now(TZ).isoformat(timespec="seconds")
    run_meta = {
        "schema_version": "1.0",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "harness": "insurance_qa_live",
        "profile_id": "insurance_qa_local_relay",
        "bundle_path": str(bundle_path),
        "chat_url": chat_url,
        "user_id": user_id,
        "gateway_log": str(gateway_log),
        "eval_runs_dir": str(eval_runs_dir),
        "n_cases": len(summary_rows),
        "n_ok": sum(1 for r in summary_rows if r.get("ok")),
        "timezone": "Asia/Shanghai",
        "mode": "live_batch_resume" if resume else "live_batch",
        "n_skipped_success": n_skipped,
        "notes": (
            "Live Insurance /v1/chat batch. Per-case gateway ingest via "
            "lane registry and execution_id matching; no time-window fallback. "
            "llm_trace.html built automatically at end of run "
            "(same pattern as eval_harness.run)."
        ),
        "cases": [r["case_id"] for r in summary_rows],
    }
    (run_dir / "run_meta.json").write_text(
        json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "summary.json").write_text(
        json.dumps(summary_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Auto HTML — exact same call site pattern as Claude run.py end-of-run
    html_path: Optional[Path] = None
    html_error: Optional[str] = None
    try:
        html_path = build_llm_trace_html(run_dir)
        print(f"[live-batch] llm_trace_html={html_path}", flush=True)
    except Exception as e:
        html_error = repr(e)
        print(f"[live-batch] llm_trace_html failed: {e!r}", flush=True)

    result = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "html_path": str(html_path) if html_path else None,
        "html_error": html_error,
        "n_ok": run_meta["n_ok"],
        "n_cases": run_meta["n_cases"],
        "summary": summary_rows,
        "auto_html": True,
    }
    print(
        f"[done] run_dir={run_dir} success={run_meta['n_ok']}/{run_meta['n_cases']} "
        f"html={html_path}",
        flush=True,
    )
    try:
        if _lock.is_file() and _lock.read_text(encoding="utf-8").strip().startswith(str(os.getpid())):
            _lock.unlink()
    except Exception:
        pass
    return result


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Insurance QA adapter: relay smoke OR live /v1/chat batch (auto HTML)"
    )
    ap.add_argument(
        "--live-batch",
        action="store_true",
        help="Run live Insurance /v1/chat batch with per-case gateway ingest + auto llm_trace.html",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Skip completed cases; archive incomplete attempts before re-run",
    )
    ap.add_argument(
        "--cases",
        default="A01,A02",
        help="Bundle case ids (comma-separated), e.g. A01,A02 — mapped to INS_A01, INS_A02",
    )
    ap.add_argument(
        "--run-id",
        default=None,
        help="Run id under eval_runs (default: insurance_datasetA_<stamp>)",
    )
    ap.add_argument("--chat-url", default=DEFAULT_CHAT_URL)
    ap.add_argument("--user-id", default=DEFAULT_USER_ID)
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    ap.add_argument("--eval-runs-dir", type=Path, default=DEFAULT_EVAL_RUNS_DIR)
    ap.add_argument("--gateway-log", type=Path, default=DEFAULT_GATEWAY_LOG)
    ap.add_argument("--timeout-sec", type=float, default=180.0)
    ap.add_argument("--lane", default="insurance_1")
    ap.add_argument("--attribution-config", type=Path, default=None)
    # smoke-mode args (ignored when --live-batch)
    ap.add_argument("--case-id", default="INS_SMOKE_001")
    ap.add_argument("--prompt", default="ping insurance relay adapter")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    args = ap.parse_args(argv)

    if args.live_batch:
        ids = [x.strip() for x in str(args.cases).split(",") if x.strip()]
        if not ids:
            raise SystemExit("--cases empty")
        result = run_live_batch(
            bundle_case_ids=ids,
            run_id=args.run_id,
            chat_url=args.chat_url,
            user_id=args.user_id,
            bundle_path=args.bundle,
            eval_runs_dir=args.eval_runs_dir,
            gateway_log=args.gateway_log,
            timeout_sec=args.timeout_sec,
            resume=bool(args.resume),
            lane_id=args.lane,
            attribution_config=args.attribution_config,
        )
        print(json.dumps({
            "ok": result["n_ok"] == result["n_cases"] and bool(result.get("html_path")),
            "run_id": result["run_id"],
            "run_dir": result["run_dir"],
            "html_path": result["html_path"],
            "auto_html": True,
            "n_ok": result["n_ok"],
            "n_cases": result["n_cases"],
        }, ensure_ascii=False, indent=2))
        if result["n_ok"] != result["n_cases"]:
            return 2
        if not result.get("html_path"):
            return 3
        return 0

    out = args.out_dir
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = MOCK_SYSTEM_ROOT / "eval_runs" / f"_probe_insurance_{stamp}" / "cases" / args.case_id
    r = run_smoke_turn(
        case_id=args.case_id,
        prompt=args.prompt,
        case_dir=out,
        model_id=args.model,
        base_url=args.base_url,
    )
    print(json.dumps({
        "ok": r.ok,
        "case_id": r.case_id,
        "model": r.model,
        "base_url": r.base_url,
        "wall_ms": r.wall_ms,
        "error": r.error,
        "reply_preview": r.reply_preview,
        "out_dir": str(out),
    }, ensure_ascii=False, indent=2))
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
