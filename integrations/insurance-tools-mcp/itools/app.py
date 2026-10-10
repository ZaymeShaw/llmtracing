"""MCP 服务面:7 个工具的 list/call。

工具的名字、描述、输入 schema 全部读 `tools_spec.json`,不做二次推导——客户端看到的
工具定义与规格导出源逐字节相同。

调用前按同一份 schema 校验入参:上游那边这件事由模型侧 JSON Schema 强制,工具函数
从来看不到非法入参。这里落在协议边界,让 dispatch 能把参数原值直传给核心函数。

返回体是核心函数产出的 str JSON 信封,原样放进 text content,不解析、不重组。
"""
from __future__ import annotations

import logging

import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from app.tools.backend import BackendClient

from itools.dispatch import dispatch
from itools.identity import Identity
from itools.spec import TOOLS, TOOLS_BY_NAME

logger = logging.getLogger("itools")

SERVER_NAME = "insurance-tools"


def _tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool["parameters"],
        )
        for tool in TOOLS
    ]


def _validate(name: str, arguments: dict) -> str | None:
    """合法返回 None,否则返回给模型看的错误说明。"""
    schema = TOOLS_BY_NAME[name]["parameters"]
    validator = jsonschema.Draft202012Validator(schema)
    problems = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
    if not problems:
        return None
    return "参数不符合工具签名:" + "; ".join(
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in problems
    )


def build_server(*, identity: Identity, backend_client: BackendClient) -> Server:
    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_definitions())

    async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        if name not in TOOLS_BY_NAME:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"未登记的工具:{name}")],
                isError=True,
            )
        arguments = params.arguments or {}
        if (problem := _validate(name, arguments)) is not None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=problem)], isError=True,
            )
        try:
            result = await dispatch(
                name, arguments, identity=identity, backend_client=backend_client
            )
        except Exception as exc:  # 工具异常按 MCP 错误内容回传,与原系统把工具异常交还模型的语义一致
            logger.exception("tool_failed tool=%s", name)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{type(exc).__name__}: {exc}")],
                isError=True,
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=result)])

    return Server(SERVER_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve_stdio(*, identity: Identity, backend_client: BackendClient) -> None:
    server = build_server(identity=identity, backend_client=backend_client)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


__all__ = ["build_server", "serve_stdio", "SERVER_NAME"]
