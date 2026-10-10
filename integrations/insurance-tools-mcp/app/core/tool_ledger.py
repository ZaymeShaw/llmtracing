"""本轮工具来源单点；原文仅存一份，解析结果复用，持久化时整体剥离。

rpc_args_key 只标识真实调用；本地拒绝也可登记为模型可见来源，但不进入去重缓存。
visible 表示已交给模型，direct 表示 Direct 升级来源；两者不能互相推断。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.schemas import ToolEnvelope


@dataclass
class ToolRecord:
    tool: str
    raw: str
    envelope: ToolEnvelope | None
    rpc_args_key: str | None = None
    visible: bool = False
    direct: bool = False
    subjects: frozenset[str] = frozenset()
    unavailable_fields: frozenset[str] = frozenset()

    @property
    def status(self) -> str | None:
        return self.envelope.status.value if self.envelope is not None else None


def records(budget: dict) -> list[ToolRecord]:
    return budget.get("tool_ledger", [])


def record(budget: dict, tool: str, raw: str, *, envelope: ToolEnvelope | None = None,
           rpc_args_key: str | None = None, visible: bool = False, direct: bool = False) -> ToolRecord:
    if rpc_args_key is None and envelope is not None:
        # 同原文但不同 RPC 的并发结果各有自己的解析对象；展示标记绑定实际来源。
        for item in records(budget):
            if item.tool == tool and item.envelope is envelope:
                item.visible |= visible
                item.direct |= direct
                return item
    for item in records(budget):
        if item.tool == tool and item.raw == raw and item.rpc_args_key in (None, rpc_args_key):
            item.visible |= visible
            item.direct |= direct
            item.rpc_args_key = item.rpc_args_key or rpc_args_key
            return item
        if item.tool == tool and item.raw == raw and rpc_args_key is None:
            item.visible |= visible
            item.direct |= direct
            return item
    item = ToolRecord(tool, raw, envelope, rpc_args_key, visible, direct)
    budget.setdefault("tool_ledger", []).append(item)
    return item


def cached(budget: dict, key: str) -> ToolRecord | None:
    return next((item for item in records(budget) if item.rpc_args_key == key), None)


def direct_record(budget: dict) -> ToolRecord | None:
    return next((item for item in reversed(records(budget)) if item.direct), None)


def visible_business_results(budget: dict, names: frozenset[str]) -> dict[str, list[ToolEnvelope]]:
    result: dict[str, list[ToolEnvelope]] = {}
    for item in records(budget):
        if item.visible and item.tool in names and item.envelope is not None:
            result.setdefault(item.tool, []).append(item.envelope)
    return result
