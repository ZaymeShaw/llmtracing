"""保险问答工具 MCP 服务:把 7 个查询工具以 MCP 暴露给任意 harness。

本包只做三件事:读工具面规格(`tools_spec.json`)、读连接级身份、把调用转发到
已有的工具核心函数。业务逻辑一行都不在这里。
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
