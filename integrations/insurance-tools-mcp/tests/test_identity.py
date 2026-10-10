"""身份注入契约:连接级提供,模型不可见,缺值不兜底。

原系统里工号来自请求体,由服务端在运行时注入;MCP 没有请求体,改成连接级 env。
契约本身不变,这几条测试锁的就是"不变"的部分。
"""
from __future__ import annotations

import pytest

from itools.identity import Identity, identity_from_env


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ITOOLS_AGENT_ID", "ITOOLS_USER_ID", "ITOOLS_EMPNO", "ITOOLS_AGENTNO", "ITOOLS_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


def test_missing_agent_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ITOOLS_USER_ID", "EMP000000")
    with pytest.raises(Exception, match="ITOOLS_AGENT_ID"):
        identity_from_env()


@pytest.mark.parametrize("blank", ["", "   "])
def test_explicit_blank_is_refused(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("ITOOLS_AGENT_ID", "A1")
    monkeypatch.setenv("ITOOLS_EMPNO", blank)
    with pytest.raises(Exception, match="ITOOLS_EMPNO"):
        identity_from_env()


def test_optional_fields_default_to_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ITOOLS_AGENT_ID", "A1")
    identity = identity_from_env()
    assert identity.upstream_user_id is None
    assert identity.empno is None
    assert identity.agentno is None
    # 缺业务 user_id 时不注入任何键,交由工具层按原口径报错,不用 agent_id 顶替。
    assert identity.dependency_kwargs() == {}


def test_dependency_kwargs_only_carries_present_values() -> None:
    identity = Identity(agent_id="A1", upstream_user_id="EMP000000", agentno="EMP000000")
    assert identity.dependency_kwargs() == {"upstream_user_id": "EMP000000", "agentno": "EMP000000"}


def test_values_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ITOOLS_AGENT_ID", "  A1  ")
    monkeypatch.setenv("ITOOLS_EMPNO", " EMP1 ")
    identity = identity_from_env()
    assert identity.agent_id == "A1"
    assert identity.empno == "EMP1"
