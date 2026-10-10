"""core/audit:TurnAuditRecord 审计承载(方案 v13 §12.1⓪ · D13 · NFR-7)。

每轮一条轻量记录、单行 insert,与 Agno run 记录**解耦**(run 记录 = 90 天回放增强,
不再是审计承载)——badcase 归因直达「哪个版本的哪份策略造成」(评审五:策略版本向量)。
字段集 = D13 原文:归属 / RouteDecision 摘要 / 信封校验结果 / 事实与缺口摘要 / 时延 /
策略版本向量(gate/routes/lexicons/fields/Skill 版本号;routes 随 G-4b 建)。
本模块纯函数 + 注入 sink:落库通道由接入层装配(切片 5;L1 = JSONL,生产 = 独立表,
留存期按合规 conf 初始 ≥365 天)。**审计不含答案原文**(只有摘要与引用)——记录本身
不成为二次 PII 载体。写失败不拒答(可用性优先):异常吞掉、on_failure 告警(P2),
缺口率进周报(spool 重放 M2 建)。
"""
from __future__ import annotations

from contextvars import ContextVar

import json
import hashlib
import logging
import math
import os
import re
import sqlite3
import time
from queue import Empty, Full, Queue
from threading import Event, Thread
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import portalocker
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from tempfile import NamedTemporaryFile
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field
from app.core.citation_policy import CitationAnomaly

from app.schemas.answer import AnswerEnvelope
from app.schemas.execution import ExecutionResult
from app.schemas.route import RouteDecision

AuditSink: TypeAlias = Callable[["TurnAuditRecord"], None]  # 单行 insert 语义
AuditFailureHandler: TypeAlias = Callable[[str], None]

logger = logging.getLogger(__name__)


@contextmanager
def _audit_file_guard(path: Path) -> Iterator[None]:
    """独立且固定的锁文件保护原子替换前后的同机跨进程追加/确认。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path) + ".lock", mode="a", timeout=1):
        yield


@dataclass(frozen=True)
class AuditFailureSnapshot:
    """进程内审计写失败快照；只记故障摘要，不承载问答正文或审计记录。"""

    total: int
    last_error: str | None
    last_at: datetime | None


# 失败串固定前缀 `turn=<id>; ` 的轮次 token(字符口径与 observability/records 的
# token 白名单一致;turn_id = trace_id,一轮一 trace)。
_TURN_PREFIX = re.compile(r"^turn=(?P<turn_id>[A-Za-z0-9_.:/{}-]{1,160});")


class AuditFailureMonitor:
    """应用级默认告警器：线程安全计数 + 结构化日志 + 可选外部回调。

    计数用于进程监控/灰度缺口率采集；日志字段不包含 TurnAuditRecord，避免
    可观测性通道成为新的业务数据副本。多 worker 部署时由日志平台跨进程汇总。
    """

    def __init__(self, downstream: AuditFailureHandler | None = None) -> None:
        self._downstream = downstream
        self._lock = Lock()
        self._total = 0
        self._last_error: str | None = None
        self._last_at: datetime | None = None

    def __call__(self, error: str) -> None:
        at = datetime.now(timezone.utc)
        with self._lock:
            self._total += 1
            self._last_error = error
            self._last_at = at
            total = self._total
        # 写线程不继承请求 ContextVar、error 自由文本不进白名单——轮次关联靠解析
        # 本模块三处生产者(队列满/写线程双失败/同步路径)统一的 `turn=<id>; ` 前缀;
        # 此前日志面检索该 ERROR 既无 trace 也无 turn,恰在双故障最需归因时刻
        # (2026-09-19 二轮复审)。前缀缺失(外部直接调用)按无轮次优雅降级。
        matched = _TURN_PREFIX.match(error)
        failure: dict[str, object] = {"total": total}
        if matched is not None:
            failure["turn_id"] = matched.group("turn_id")
        logger.error("turn_audit_write_failed", extra={"audit_failure": failure})
        if self._downstream is not None:
            self._downstream(error)

    def snapshot(self) -> AuditFailureSnapshot:
        """返回一致快照，供应用状态/监控采集读取。"""
        with self._lock:
            return AuditFailureSnapshot(
                total=self._total,
                last_error=self._last_error,
                last_at=self._last_at,
            )


class TurnAuditRecord(BaseModel):
    """一轮对话的审计记录(单行 insert;字段 = D13 原文摘要集,不含答案原文)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 归属
    turn_id: str  # = trace_id(一轮一 run 一 trace)
    # 既有审计/存储字段名保持兼容；chat-v1.7 起其值唯一来自对外 session_id。
    conversation_id: str
    agent_id: str
    run_id: str | None = None
    at: datetime
    # RouteDecision 摘要
    route_action: str
    route_matched_check: str | None = None
    route_reason: str = ""
    # 信封校验结果(§12.1⓪ 最终闸门)
    envelope_intercepted: bool
    envelope_intercept_reason: str | None = None  # 信封拦截面 + 接入层 timeout/run_exception
    envelope_status: str | None = None  # 最终降级状态优先，否则记录 ExecutionResult 八态
    completeness: str | None = None  # H-01a：旧记录缺失为 None，不推断完整成功
    exit_checks_version: str | None = None  # 旧版戳记兼容字段；当前正常与降级信封均为 None
    # 答案阶梯(H-01)：最终答案的阶梯级别("L3"…；None=正常完成未走阶梯)与机器可读
    # 降级原因(与 envelope_intercept_reason 同词表)——生产观察按 degrade_reason 分组
    # 统计(排期 §五)，不进用户可见字段。
    ladder_level: str | None = None
    degrade_reason: str | None = None
    citation_anomaly: list[CitationAnomaly] | None = None
    features_digest: str | None = None
    effective_features: dict | None = None
    l2_eligible: bool | None = None
    snapshot_rejections: list[str] | None = None
    context_overflow_recovered: bool | None = None
    sse_terminal: dict | None = None
    retirement_ms: int | None = None
    retirement_slow: bool | None = None
    busy_reason: str | None = None
    exit_reason: str | None = None  # DL-04 resolve_exit owns this field; do not infer here.
    budget_finish_reason: str | None = None
    refusal_categories: dict[str, int] | None = None
    selection_corrections: dict[str, int] | None = None
    cached_input_tokens: int | None = None
    shaping_mode: str | None = None
    shaping_would_truncate: int | None = None
    # 事实与缺口摘要
    fact_count: int = 0
    fact_refs: list[str] = []
    gaps: list[str] = []
    # 时延
    elapsed_ms: int = 0
    # H-01b：旧记录未知为 None；新轮次从同一预算账本投影，不参与预算控制。
    llm_calls: int | None = None
    tool_calls_real: int | None = None
    tool_calls_dedup: int | None = None
    tool_calls_refused: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    backend_rpc_calls: int | None = None
    cache_hits: int | None = None  # CL-02: historical read/schema compatibility only; new turns leave null.
    lane: str | None = None
    # 策略版本向量(D13 评审五:当轮生效 gate/lexicons/fields/skills[/routes] 版本)
    policy_versions: dict[str, str] = {}


@dataclass(frozen=True)
class _ReplayRecord:
    record: TurnAuditRecord
    line: str


class AsyncAuditSink:
    """后台审计写入；下游失败时追加 spool，启动重放成功后确认清理。

    请求只做有界队列入队；队列满交独立补偿线程，关闭后明确拒绝。启动 ``replay()``
    保留磁盘副本，后台 sink 返回成功后按批确认；失败行原地保留，不重复追加。
    交付为至少一次：sink 已提交但确认前崩溃时可能重放，sink 应按 turn_id 幂等。
    同一 spool 只支持单实例访问；多进程须使用独立路径。生命周期由应用接管。
    """

    def __init__(
        self,
        sink: AuditSink,
        spool_path: str | Path,
        maxsize: int = 1024,
        *,
        on_failure: AuditFailureHandler | None = None,
        max_spool_bytes: int = 64 * 1024 * 1024,
        replay_interval_s: float = 30.0,
    ) -> None:
        if maxsize <= 0 or not math.isfinite(replay_interval_s) or replay_interval_s <= 0:
            raise ValueError("审计队列容量和重放间隔必须大于 0")
        if max_spool_bytes <= 0:
            raise ValueError("max_spool_bytes 必须大于 0")
        self._sink = sink
        self._spool = Path(spool_path)
        self._on_failure = on_failure
        self._max_spool_bytes = max_spool_bytes
        self._closed = False
        self._spool_lock = Lock()
        # 一次只准入一批重放；队列中/执行中的记录均保留磁盘副本。
        # 批次结束后一次原子改写，避免逐条确认导致反复重写整个 spool。
        self._replay_pending: set[str] = set()
        self._replay_succeeded: set[str] = set()
        # 串行化 __call__ 的关闭判定与 close 的置位：否则「检查未关闭 → close 排空
        # → 记录才入队」窗口内的记录会落在已无消费者的队列里既不落库也不进 spool。
        self._state_lock = Lock()
        self._queue: Queue[TurnAuditRecord | _ReplayRecord | None] = Queue(maxsize=maxsize)
        self._worker = Thread(target=self._run, daemon=True, name="audit-writer")
        self._stop_replay = Event()
        self._spool_queue: Queue[TurnAuditRecord] = Queue(maxsize=maxsize)
        self._spool_worker = Thread(target=self._run_spool, daemon=True, name="audit-spool")
        self._replayer = Thread(target=self._run_replay, args=(replay_interval_s,), daemon=True, name="audit-replay")
        self._worker.start()
        self._spool_worker.start()
        self._replayer.start()

    def __call__(self, record: TurnAuditRecord) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError(f"audit sink closed; turn={record.turn_id}")
            try:
                self._queue.put_nowait(record)
            except Full as exc:
                try:
                    self._spool_queue.put_nowait(record)
                except Full:
                    raise RuntimeError(f"audit queues full; record not accepted; turn={record.turn_id}") from None
                raise RuntimeError(f"audit queue full; record queued for spool; turn={record.turn_id}") from exc

    def _run_spool(self) -> None:
        while True:
            try:
                record = self._spool_queue.get(timeout=0.1)
            except Empty:
                if self._closed:
                    return
                continue
            try:
                self._append_spool(record)
            except Exception as exc:
                self._report_failure(f"turn={record.turn_id}; spool:{type(exc).__name__}: {exc}")
            finally:
                self._spool_queue.task_done()

    def _run_replay(self, interval: float) -> None:
        while not self._stop_replay.wait(interval):
            try:
                self.replay()
            except Exception:
                logger.exception("turn_audit_spool_replay_failed")

    def snapshot(self) -> dict[str, int | bool]:
        return {"queued": self._queue.qsize(), "spool_queued": self._spool_queue.qsize(),
                "writer_alive": self._worker.is_alive(), "spool_writer_alive": self._spool_worker.is_alive(),
                "closed": self._closed}

    def _append_spool(self, record: TurnAuditRecord) -> None:
        line = record.model_dump_json(ensure_ascii=False)
        with self._spool_lock, _audit_file_guard(self._spool):
            self._spool.parent.mkdir(parents=True, exist_ok=True)
            current_size = self._spool.stat().st_size if self._spool.exists() else 0
            if current_size + len((line + "\n").encode("utf-8")) > self._max_spool_bytes:
                raise OSError("audit spool size limit exceeded")
            with self._spool.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def _report_failure(self, error: str) -> None:
        if self._on_failure is None:
            return
        try:
            self._on_failure(error)
        except Exception:
            # 告警通道故障不得影响写线程存活。
            logger.exception("turn_audit_failure_handler_failed")

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except Empty:
                if self._closed:
                    return
                continue
            if item is None:
                return
            record = item.record if isinstance(item, _ReplayRecord) else item
            persisted = False
            try:
                self._sink(record)
                persisted = True
            except Exception as exc:
                if isinstance(item, _ReplayRecord):
                    # 原副本仍在；重放失败不能再追加一份或消耗 spool 容量。
                    self._report_failure(f"turn={record.turn_id}; {type(exc).__name__}: {exc}")
                    continue
                # spool 也失败（如磁盘满，恰是主 sink 同时故障之时）时记录确实丢失，
                # 但写线程必须存活并告警——线程死亡会让后续所有记录无声滞留队列。
                # 写线程不继承请求 ContextVar：告警串内嵌 turn_id 才能关联到轮次
                # （外部告警通道消费该字符串；2026-09-18 整体检视切片 5 收口）。
                try:
                    self._append_spool(record)
                except Exception as spool_exc:
                    self._report_failure(
                        f"turn={record.turn_id}; "
                        f"sink:{type(exc).__name__}: {exc}; "
                        f"spool:{type(spool_exc).__name__}: {spool_exc}"
                    )
                else:
                    self._report_failure(f"turn={record.turn_id}; {type(exc).__name__}: {exc}")
            finally:
                if isinstance(item, _ReplayRecord):
                    self._finish_replay(item, persisted=persisted)

    def _finish_replay(self, item: _ReplayRecord, *, persisted: bool) -> None:
        try:
            with self._spool_lock:
                self._replay_pending.remove(item.line)
                if persisted:
                    self._replay_succeeded.add(item.line)
                if not self._replay_pending:
                    try:
                        if self._replay_succeeded:
                            self._ack_spool(self._replay_succeeded)
                    finally:
                        # 确认失败后仍允许下一次 replay；磁盘旧副本保留。
                        self._replay_succeeded.clear()
        except Exception as exc:
            # 确认故障不能杀死写线程；告警沿用已有轮次关联契约。
            self._report_failure(f"turn={item.record.turn_id}; replay_ack:{type(exc).__name__}: {exc}")

    def _ack_spool(self, succeeded: set[str]) -> None:
        """持有 spool 锁时确认；读取当前文件，保留期间追加的记录与损坏行。"""
        with _audit_file_guard(self._spool):
            self._ack_spool_locked(succeeded)

    def _ack_spool_locked(self, succeeded: set[str]) -> None:
        if not self._spool.exists():
            return  # 另一 worker 已成功持久化并确认同一批至少一次重放记录。
        lines = self._spool.read_text(encoding="utf-8").splitlines(keepends=True)
        remaining = [line for line in lines if line.rstrip("\r\n") not in succeeded]
        if not remaining:
            self._spool.unlink()
            return
        # 原地 truncate/write 在崩溃或磁盘满时会损坏未确认记录。先写同目录临时
        # 文件并 flush/fsync，再原子替换；替换前失败仍保留完整旧副本。
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._spool.parent,
                prefix=f".{self._spool.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.writelines(remaining)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._spool)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def replay(self) -> int:
        """返回本批入队数（非落库数）；已有批次未完成或已关闭时不重复入队。"""
        with self._spool_lock, _audit_file_guard(self._spool):
            if self._closed or self._replay_pending or not self._spool.exists():
                return 0
            lines = self._spool.read_text(encoding="utf-8").splitlines()
            count = 0
            for line in lines:
                if not line or line in self._replay_pending:
                    continue
                try:
                    record = TurnAuditRecord.model_validate_json(line)
                except ValueError:
                    continue  # 损坏行始终保留，待人工处置。
                try:
                    with self._state_lock:
                        if self._closed:
                            break
                        self._queue.put_nowait(_ReplayRecord(record, line))
                except Full:
                    break  # 未准入记录继续保留在磁盘，供下次 replay。
                self._replay_pending.add(line)
                count += 1
            return count

    def close(self, timeout_s: float = 5.0) -> None:
        """停机排空：通知写线程退出并等待；未及写出的记录落 spool 补偿。"""
        with self._state_lock:
            self._closed = True
        self._stop_replay.set()
        deadline = time.monotonic() + timeout_s
        self._replayer.join(max(0, deadline - time.monotonic()))
        self._worker.join(max(0, deadline - time.monotonic()))
        self._spool_worker.join(max(0, deadline - time.monotonic()))
        while not self._queue.empty():
            try:
                record = self._queue.get_nowait()
            except Empty:
                break
            if isinstance(record, _ReplayRecord):
                self._finish_replay(record, persisted=False)  # 磁盘已有副本，不重复落盘。
            elif record is not None:
                self._append_spool(record)


def ensure_sink_ready(mode: str, path: str | Path) -> None:
    """启动期探测审计落点可写（jsonl 触碰 / sqlite 建表），不可用即拒绝启动。

    与日志预检、策略交叉校验同口径 fail-fast——审计路径故障不能等到首轮请求
    才以「逐轮丢失 + 告警计数」的形式暴露。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "sqlite":
        conn = sqlite3.connect(target)
        try:
            with conn:
                _ensure_audit_table(conn)
        finally:
            conn.close()
    else:
        with target.open("a", encoding="utf-8"):
            pass


def contextual_audit_record(**values) -> TurnAuditRecord:
    """Capture request-scoped configuration before async dispatch; observation never blocks delivery."""
    try:
        from app.conf import harness_conf, current_policy_versions
        config = harness_conf()
        features = config.features.model_dump(mode="json")
        features["context_recovery"] = {"enabled": config.context_recovery.enabled}
        values.setdefault("effective_features", features)
        values.setdefault("features_digest", hashlib.sha256(
            json.dumps(features, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        values.setdefault("shaping_mode", features["tool_output_shaping"]["mode"])
        if values.get("ladder_level") in {"L2", "L3"} and not features["answer_ladder"]["enabled"]:
            values["ladder_level"] = "legacy_fallback"
        if not values.get("policy_versions"):
            values["policy_versions"] = current_policy_versions()
    except Exception:
        logger.warning("turn_audit_context_failed", exc_info=True)
    return TurnAuditRecord(**values)


def audit_observations(state: dict | None) -> dict:
    state = state if isinstance(state, dict) else {}
    route = state.get("route")
    route = route if isinstance(route, dict) else {}
    budget = state.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    shadow = budget.get("output_shaping_shadow")
    shadow = shadow if isinstance(shadow, dict) else {}
    result: dict = {}
    for key in ("l2_eligible", "context_overflow_recovered"):
        value = route.get(key)
        result[key] = value if type(value) is bool else None
    for key in ("budget_finish_reason", "exit_reason"):
        value = route.get(key)
        result[key] = value if isinstance(value, str) else None
    rejections = route.get("snapshot_rejections")
    result["snapshot_rejections"] = rejections if isinstance(rejections, list) and all(isinstance(v, str) for v in rejections) else None
    for key in ("refusal_categories", "selection_corrections"):
        counts = budget.get(key)
        result[key] = dict(counts) if isinstance(counts, dict) and all(
            isinstance(k, str) and type(v) is int and v >= 0 for k, v in counts.items()) else None
    truncated = shadow.get("would_truncate")
    result["shaping_would_truncate"] = truncated if type(truncated) is int and truncated >= 0 else None
    return result


def count_refusal(budget: dict, category: str) -> None:
    counts = budget.get("refusal_categories")
    if not isinstance(counts, dict):
        counts = budget["refusal_categories"] = {}
    previous = counts.get(category)
    counts[category] = (previous if type(previous) is int and previous >= 0 else 0) + 1


def audit_efficiency(state: dict | None) -> dict:
    """只投影标量，快照/取消出口也可使用；缺失账本不伪造零调用。"""
    budget = (state or {}).get("budget")
    if not isinstance(budget, dict):
        return audit_observations(state)
    result = {
        key: budget.get(source)
        for key, source in (
            ("llm_calls", "llm_calls"), ("tool_calls_real", "tool_calls_real"),
            ("tool_calls_dedup", "deduplicated_tool_calls"),
            ("tool_calls_refused", "tool_calls_refused"),
            ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
            ("backend_rpc_calls", "backend_rpc_calls"), ("cached_input_tokens", "cached_input_tokens"),
        )
    }
    # 审计损坏不得阻断交付；未知或非法值留空，不强转估算。
    result = {key: value if type(value) is int and value >= 0 else None for key, value in result.items()}
    route = (state or {}).get("route")
    decision = route.get("decision") if isinstance(route, dict) else None
    lane = decision.get("action") if isinstance(decision, dict) else None
    result["lane"] = lane if isinstance(lane, str) else None
    result.update(audit_observations(state))
    return result


def build_turn_audit(
    *,
    agent_id: str,
    conversation_id: str,
    trace_id: str | None,
    decision: RouteDecision | None,
    envelope: AnswerEnvelope,
    intercepted: bool,
    intercept_reason: str | None,
    elapsed_ms: int,
    policy_versions: dict[str, str],
    execution_status: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    ladder_level: str | None = None,
    degrade_reason: str | None = None,
    citation_anomaly: list[CitationAnomaly] | None = None,
    state: dict | None = None,
) -> TurnAuditRecord:
    """从当轮产物组装审计记录(唯一组装点;策略版本向量由接入层从 conf 汇入)。

    阶梯字段优先使用调用方值，其次按显式 partial/fallback 推导 L2/L3；
    旧信封继续按拦截状态推导。完整度与执行结果分别记录，partial 不计 SUCCESS。
    """
    if degrade_reason is None and envelope.is_incomplete and "exit_reason" in ((state or {}).get("route") or {}):
        degrade_reason = ((state or {}).get("route") or {})["exit_reason"]
    if (degrade_reason is None and "exit_reason" not in ((state or {}).get("route") or {})
            and not intercepted and envelope.completeness == "partial"
            and ((state or {}).get("route") or {}).get("budget_finish_reason") == "no_progress"):
        degrade_reason = "no_progress"
    efficiency = audit_efficiency(state)
    efficiency["lane"] = decision.action.value if decision is not None else "UNAVAILABLE"
    return contextual_audit_record(
        **efficiency,
        turn_id=trace_id or conversation_id,
        conversation_id=conversation_id,
        agent_id=agent_id,
        run_id=run_id,
        at=now or datetime.now(timezone.utc),
        # 超时/异常可能发生在 Gate 之前；显式记 UNAVAILABLE，不伪造成 AGENT 路由。
        route_action=decision.action.value if decision is not None else "UNAVAILABLE",
        route_matched_check=decision.matched_check if decision is not None else None,
        route_reason=decision.reason if decision is not None else "RouteDecision unavailable",
        envelope_intercepted=intercepted,
        envelope_intercept_reason=intercept_reason,
        envelope_status="PARTIAL" if envelope.completeness == "partial" else envelope.status or execution_status,
        completeness=envelope.completeness,
        exit_checks_version=None,  # CL-03：新轮无检查戳记，旧审计行仍可读取版本。
        ladder_level=ladder_level if ladder_level is not None else ("L2" if envelope.completeness == "partial" else
            "L3" if intercepted or envelope.completeness == "fallback" else None),
        degrade_reason=degrade_reason if degrade_reason is not None else (intercept_reason if intercepted else None),
        citation_anomaly=citation_anomaly,
        fact_count=len(envelope.fact_refs),
        fact_refs=list(envelope.fact_refs),
        gaps=list(envelope.gaps),
        elapsed_ms=elapsed_ms,
        policy_versions=dict(policy_versions),
    )


def execution_status_of(session_state: dict, run_id: str | None) -> str | None:
    """从当前 run 绑定的 ExecutionResult 提取八态；残留/损坏状态不猜测。"""
    route = session_state.get("route") or {}
    if not run_id or route.get("last_result_run_id") != run_id:
        return None
    raw = route.get("last_result")
    if not isinstance(raw, str):
        return None
    try:
        return ExecutionResult.model_validate_json(raw).status.value
    except ValueError:
        return None


_audit_dispatch: ContextVar = ContextVar("audit_dispatch", default=None)


@contextmanager
def audit_dispatch_scope(dispatch):
    token = _audit_dispatch.set(dispatch)
    try:
        yield
    finally:
        _audit_dispatch.reset(token)


def write_turn_audit(
    sink: AuditSink, record: TurnAuditRecord, *, on_failure: AuditFailureHandler | None = None
) -> bool:
    """单行写入;失败不拒答(§12.1⓪:可用性优先)——异常转告警回调(P2),返回 False。

    缺口率周报与 spool 重放为 M1/M2 分期(评审五);本函数是「不拒答」承诺的唯一实现点。
    """
    dispatch = _audit_dispatch.get()
    if dispatch is not None:
        return dispatch(sink, record, on_failure)
    try:
        sink(record)
    except Exception as exc:  # 审计通道故障不得影响应答（设计上吞掉一切异常）
        # turn= 前缀让外部告警通道能对回落次（与 AsyncAuditSink 写线程同款口径）。
        error = f"turn={record.turn_id}; {type(exc).__name__}: {exc}"
        try:
            if on_failure is not None:
                on_failure(error)
            else:
                logger.error(
                    "turn_audit_write_failed",
                    extra={"audit_failure": {"total": None, "error": error}},
                )
        except Exception:
            # 外部告警通道同样不得成为回答可用性的反向依赖。
            logger.exception("turn_audit_failure_handler_failed")
        return False
    return True


def jsonl_sink(path: str | Path) -> AuditSink:
    """开发/测试用 JSONL 落盘 sink(L1;生产换独立表 sink,签名不变)。"""
    target = Path(path)

    def _append(record: TurnAuditRecord) -> None:
        with _audit_file_guard(target), target.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json(ensure_ascii=False) + "\n")

    return _append


# 独立表列集 = TurnAuditRecord 字段的一一映射(列表类字段存 JSON 文本;
# 生产建表同型,内网换库/DSN 只改 conf,audit 代码零改动)
_AUDIT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("turn_id", "TEXT NOT NULL PRIMARY KEY"),
    ("conversation_id", "TEXT NOT NULL"),
    ("agent_id", "TEXT NOT NULL"),
    ("run_id", "TEXT"),
    ("at", "TEXT NOT NULL"),
    ("route_action", "TEXT NOT NULL"),
    ("route_matched_check", "TEXT"),
    ("route_reason", "TEXT NOT NULL"),
    ("envelope_intercepted", "INTEGER NOT NULL"),
    ("envelope_intercept_reason", "TEXT"),
    ("envelope_status", "TEXT"),
    ("completeness", "TEXT"),
    ("exit_checks_version", "TEXT"),
    ("ladder_level", "TEXT"),
    ("degrade_reason", "TEXT"),
    ("citation_anomaly", "TEXT"),
    ("fact_count", "INTEGER NOT NULL"),
    ("fact_refs", "TEXT NOT NULL"),
    ("gaps", "TEXT NOT NULL"),
    ("elapsed_ms", "INTEGER NOT NULL"),
    ("policy_versions", "TEXT NOT NULL"),
    ("llm_calls", "INTEGER"),
    ("tool_calls_real", "INTEGER"),
    ("tool_calls_dedup", "INTEGER"),
    ("tool_calls_refused", "INTEGER"),
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cache_hits", "INTEGER"),
    ("backend_rpc_calls", "INTEGER"),
    ("lane", "TEXT"),
    ("features_digest", "TEXT"),
    ("effective_features", "TEXT"),
    ("l2_eligible", "INTEGER"),
    ("snapshot_rejections", "TEXT"),
    ("context_overflow_recovered", "INTEGER"),
    ("sse_terminal", "TEXT"),
    ("exit_reason", "TEXT"),
    ("retirement_ms", "INTEGER"),
    ("retirement_slow", "INTEGER"),
    ("busy_reason", "TEXT"),
    ("budget_finish_reason", "TEXT"),
    ("refusal_categories", "TEXT"),
    ("selection_corrections", "TEXT"),
    ("cached_input_tokens", "INTEGER"),
    ("shaping_mode", "TEXT"),
    ("shaping_would_truncate", "INTEGER"),

)
_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS turn_audit ("
    + ", ".join(f"{name} {col_type}" for name, col_type in _AUDIT_COLUMNS)
    + ")"
)
_JSON_COLUMNS = {"fact_refs", "gaps", "policy_versions", "citation_anomaly", "effective_features", "snapshot_rejections", "sse_terminal", "refusal_categories", "selection_corrections"}


def _ensure_audit_table(conn: sqlite3.Connection) -> None:
    """建表 + 增量补列（H-01 起新增审计列；旧库升级 ALTER 追加缺失列，幂等）。

    新增列均为可空 TEXT / INTEGER——ALTER ADD COLUMN 对已填充行无需回填默认值；
    列集变化只改 _AUDIT_COLUMNS，此处自动跟进。
    """
    conn.execute(_AUDIT_DDL)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(turn_audit)")}
    for name, col_type in _AUDIT_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE turn_audit ADD COLUMN {name} {col_type}")


def sqlite_sink(path: str | Path) -> AuditSink:
    """独立表 sink(SQLite 同型表;生产表随内网部署换库,签名与列集不变)。

    单行 insert 语义与 jsonl 一致;同 turn_id 重放幂等(REPLACE——一轮一行)。
    每次写入独立短连接:审计每轮一行、打开成本毫秒级,免连接池与生命周期治理。
    """
    target = Path(path)

    def _insert(record: TurnAuditRecord) -> None:
        row = record.model_dump(mode="json")
        for key in _JSON_COLUMNS:
            row[key] = json.dumps(row[key], ensure_ascii=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
        try:
            with conn:  # 事务块:建表/补列 + insert 同提交
                _ensure_audit_table(conn)
                conn.execute(
                    f"INSERT OR REPLACE INTO turn_audit ({', '.join(c for c, _ in _AUDIT_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _AUDIT_COLUMNS)})",
                    [row[name] for name, _ in _AUDIT_COLUMNS],
                )
        finally:
            conn.close()

    return _insert


class AuditSinkConf(BaseModel):
    """conf/audit/audit_{ENV_TYPE}_args.yaml sink 段(jsonl 开发缺省 / sqlite 独立表同型)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["jsonl", "sqlite"]
    path: str


class AuditConf(BaseModel):
    """审计落库配置(NFR-7;机制在文档、数字在 conf)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    sink: AuditSinkConf
    async_enabled: bool = True
    spool_path: str = "var/turn-audit.spool.jsonl"
    queue_maxsize: int = Field(default=1024, ge=2)
    max_spool_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    retention_days: int = Field(ge=1, description="仅声明外部留存目标；服务不自动删除审计或 spool 文件")


def elapsed_ms_of(budget: dict, *, now: float | None = None) -> int:
    """从预算账本 t0(§12.3,S1 初始化)计算当轮耗时毫秒——审计时延字段的数据源。"""
    t0 = budget.get("t0")
    if t0 is None:
        return int(budget.get("elapsed_ms", 0))
    return int(((now if now is not None else time.time()) - t0) * 1000)


def write_busy_audit(sink: AuditSink, *, trace_id: str, conversation_id: str, agent_id: str,
                     reason: str, on_failure=None) -> bool:
    """Admission refusal is observable without inventing a business answer."""
    return write_turn_audit(sink, contextual_audit_record(
        turn_id=trace_id, conversation_id=conversation_id, agent_id=agent_id,
        at=datetime.now(timezone.utc), route_action="UNAVAILABLE", envelope_status="UNAVAILABLE",
        envelope_intercepted=True, envelope_intercept_reason=reason,
        exit_reason=reason, busy_reason=reason,
    ), on_failure=on_failure)
