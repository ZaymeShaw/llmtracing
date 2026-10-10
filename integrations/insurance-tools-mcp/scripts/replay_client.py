"""上游响应的录播替身:两侧共用,保证拿到的 upstream 载荷逐字节相同。

只有把上游钉死,才能判别"两边跑出来不一样"到底是通道层分叉,还是上游检索本身每次不同。
键 = (system, endpoint, 去掉易变字段后的 payload 摘要),因此调用顺序变了也能自证。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.tools.backend import BackendCallResult

# 两侧天然不同、与业务无关的字段,不参与匹配。
VOLATILE_FIELDS = ("request_id", "trace_id", "session_id", "agent_id")


def call_key(system: str, endpoint: str, payload: dict[str, Any]) -> str:
    stable = {k: v for k, v in payload.items() if k not in VOLATILE_FIELDS}
    blob = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{system}|{endpoint}|{digest}"


class Recorder:
    """代理真实 client,把上游返回体按 key 记下来。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: dict[str, list[Any]] = {}

    async def call(self, system: str, endpoint: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        response = await self._inner.call(system, endpoint, payload, **kwargs)
        record = {
            "payload": response.payload if isinstance(response, BackendCallResult) else response,
            "retries": response.retries if isinstance(response, BackendCallResult) else 0,
        }
        self.calls.setdefault(call_key(system, endpoint, payload), []).append(record)
        return response

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(self.calls, ensure_ascii=False, indent=1), encoding="utf-8")


class Replayer:
    """按 key 依序吐回录下的上游返回体;没有记录就立刻炸,不静默兜底。"""

    def __init__(self, fixture: Path) -> None:
        self.calls: dict[str, list[Any]] = json.loads(fixture.read_text(encoding="utf-8"))
        self.used: dict[str, int] = {}
        self.misses: list[str] = []

    async def call(self, system: str, endpoint: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        key = call_key(system, endpoint, payload)
        bucket = self.calls.get(key)
        index = self.used.get(key, 0)
        if not bucket or index >= len(bucket):
            self.misses.append(key)
            raise AssertionError(f"录播缺少上游响应:{key}")
        self.used[key] = index + 1
        record = bucket[index]
        return BackendCallResult(record["payload"], retries=record["retries"])


__all__ = ["Recorder", "Replayer", "call_key", "VOLATILE_FIELDS"]
