"""有界批量导出；OTel 1.44 的 force_flush 忽略 timeout，不能用于请求停机边界。"""
from __future__ import annotations

import threading
import time
from collections import deque
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter
from app.conf.langfuse import LangfuseConf


class BoundedBatch:
    def __init__(self, exporter: SpanExporter, conf: LangfuseConf) -> None:
        self.exporter, self.conf = exporter, conf
        self.queue: deque[ReadableSpan] = deque()
        self.condition = threading.Condition()
        self.submitted = self.completed = self.dropped = 0
        self.closing = self.flush_requested = False
        self.deadline = float("inf")
        self.worker = threading.Thread(target=self._run, name="langfuse-export", daemon=True)
        self.worker.start()

    def on_end(self, span: ReadableSpan) -> None:
        with self.condition:
            if self.closing or len(self.queue) >= self.conf.max_queue_size:
                self.dropped += 1
                return
            self.queue.append(span)
            self.submitted += 1
            if len(self.queue) >= self.conf.max_export_batch_size:
                self.condition.notify_all()

    def _run(self) -> None:
        try:
            while True:
                with self.condition:
                    if not self.closing and not self.flush_requested and len(self.queue) < self.conf.max_export_batch_size:
                        self.condition.wait(self.conf.schedule_delay_ms / 1000)
                    if self.closing and (not self.queue or time.monotonic() >= self.deadline):
                        self.dropped += len(self.queue)
                        self.queue.clear()
                        return
                    batch = [self.queue.popleft() for _ in range(min(len(self.queue), self.conf.max_export_batch_size))]
                    self.flush_requested = False
                if batch:
                    self.exporter.export(batch)
                    with self.condition:
                        self.completed += len(batch)
                        self.condition.notify_all()
        finally:
            # 只有后台拥有 exporter；不在调用线程关闭正在使用的 HTTP session。
            self.exporter.shutdown()

    def force_flush(self, timeout_millis: int) -> bool:
        deadline = time.monotonic() + timeout_millis / 1000
        with self.condition:
            target = self.submitted
            while self.completed < target and not self.closing:
                self.flush_requested = True
                self.condition.notify_all()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return self.completed >= target

    def shutdown(self, timeout_millis: int) -> None:
        with self.condition:
            if self.closing:
                return
            self.closing = True
            self.deadline = time.monotonic() + timeout_millis / 1000
            self.condition.notify_all()
        self.worker.join(timeout_millis / 1000)
