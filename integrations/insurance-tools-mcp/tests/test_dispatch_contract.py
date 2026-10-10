"""通道层契约:身份准入、缺值报错、信封透传——全部离线,不触网。

这里锁的是"同一个工具在两端的准入行为一致":缺工号时返回的信封状态与提示,
必须和被对比的另一端逐字相同,否则差异归因会被工具层污染。
"""
from __future__ import annotations

import json

import pytest

from itools.dispatch import dispatch
from itools.identity import Identity


class _UnusedClient:
    """任何真实调用都应被准入拦在门外;真被调到就说明测试意图失效。"""

    async def call(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("离线测试不应触达后端")


@pytest.mark.asyncio
async def test_policy_denied_without_agent_number() -> None:
    envelope = json.loads(await dispatch(
        "policy_detail", {"polno": "P260000000000001"},
        identity=Identity(agent_id="A1"), backend_client=_UnusedClient(),
    ))
    assert envelope["status"] == "DENIED"
    assert envelope["hint"] == "缺少本轮代理人工号，无法查询保单详情"
    assert envelope["error"] == {"code": "DENIED", "retryable": False}


@pytest.mark.asyncio
async def test_policy_search_error_without_business_user_id() -> None:
    envelope = json.loads(await dispatch(
        "policy_search", {"query": "查一下保单"},
        identity=Identity(agent_id="A1"), backend_client=_UnusedClient(),
    ))
    assert envelope["status"] == "ERROR"
    assert envelope["error"]["code"] == "UPSTREAM"
    assert "上游请求体 user_id" in envelope["hint"]


@pytest.mark.asyncio
async def test_agent_id_does_not_substitute_business_number() -> None:
    """认证主体不得顶替业务工号——两端同一条纪律。"""
    envelope = json.loads(await dispatch(
        "customer_detail", {"customer_id": "MOCKC202609090001"},
        identity=Identity(agent_id="EMP000000"), backend_client=_UnusedClient(),
    ))
    assert envelope["status"] != "SUCCESS"


@pytest.mark.asyncio
async def test_empty_query_returns_empty_envelope() -> None:
    """空串是合法入参(schema 通过),由核心函数按原口径回 EMPTY。"""
    envelope = json.loads(await dispatch(
        "general_qa", {"query": ""},
        identity=Identity(agent_id="A1"), backend_client=_UnusedClient(),
    ))
    assert envelope["status"] == "EMPTY"


@pytest.mark.asyncio
async def test_customer_detail_forwards_new_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_detail(customer_id, **kwargs):
        captured["customer_id"] = customer_id
        captured["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr("itools.dispatch.customer_detail_core", fake_detail)
    result = await dispatch(
        "customer_detail",
        {"customer_ids": ["ID-1", "ID-2"], "client_name": "张三", "client_mobile": "13800000000"},
        identity=Identity(agent_id="A1", upstream_user_id="EMP123"),
        backend_client=_UnusedClient(),
    )
    assert result == "ok"
    assert captured["customer_id"] is None
    assert captured["kwargs"]["customer_ids"] == ["ID-1", "ID-2"]
    assert captured["kwargs"]["client_name"] == "张三"
    assert captured["kwargs"]["client_mobile"] == "13800000000"
    assert captured["kwargs"]["upstream_user_id"] == "EMP123"


@pytest.mark.asyncio
async def test_unknown_tool_is_refused() -> None:
    with pytest.raises(KeyError):
        await dispatch("not_a_tool", {}, identity=Identity(agent_id="A1"),
                       backend_client=_UnusedClient())
