"""工具面一致性:对外发布的名字/描述/输入 schema 必须与规格逐字节相同。

规格由上游模型可见工具面导出;本工程不做任何二次推导。这条测试是"两边是同一批工具"
的可执行证据——它挂了,就说明对比实验的工具面已经漂移,结论不再成立。
"""
from __future__ import annotations

import json

import pytest

from itools.app import build_server
from itools.spec import SPEC_SOURCE, TOOLS, TOOLS_BY_NAME

EXPECTED_ORDER = [
    "product_knowledge", "benefit_info", "general_qa", "policy_search",
    "customer_search", "policy_detail", "customer_detail",
]


def test_published_tools_and_order() -> None:
    assert [tool["name"] for tool in TOOLS] == EXPECTED_ORDER


def test_spec_carries_its_provenance() -> None:
    assert SPEC_SOURCE == "insurance-qa-agent app/tools/bindings.py AGENT_TOOLS"


def test_spec_is_non_empty_and_well_formed() -> None:
    for tool in TOOLS:
        assert tool["description"].strip(), tool["name"]
        schema = tool["parameters"]
        assert schema["type"] == "object"
        assert set(schema["required"] or []) <= set(schema["properties"]), tool["name"]


@pytest.mark.parametrize("name", EXPECTED_ORDER)
def test_required_parameters_match_contract(name: str) -> None:
    schema = TOOLS_BY_NAME[name]["parameters"]
    required = set(schema.get("required") or [])
    expected = {"policy_detail": {"polno"}, "customer_detail": set()}.get(name, {"query"})
    assert required == expected
    assert set(schema["properties"]) >= expected
    if name == "customer_search":
        assert {"compact", "fields"} <= set(schema["properties"])
    if name == "customer_detail":
        # 上游 FX-05(486c5de0)收窄模型入口:只接受客户中心 ID,姓名/手机号密文条件不再暴露给模型。
        assert {"customer_id", "customer_ids", "compact", "fields"} <= set(schema["properties"])
        assert not {"client_name", "client_mobile"} & set(schema["properties"])


@pytest.mark.asyncio
async def test_mcp_list_tools_is_byte_identical_to_spec() -> None:
    server = build_server(identity=object(), backend_client=object())  # type: ignore[arg-type]
    entry = server.get_request_handler("tools/list")
    assert entry is not None
    result = await entry.handler(None, None)
    published = [
        {"name": tool.name, "description": tool.description, "parameters": tool.input_schema}
        for tool in result.tools
    ]
    assert json.dumps(published, ensure_ascii=False, sort_keys=True) == json.dumps(
        TOOLS, ensure_ascii=False, sort_keys=True
    )
