"""工具面规格:名字、描述、输入 schema。

规格文件由 `scripts/gen_tools_spec.py` 从被测系统的模型可见工具面导出,是唯一真源。
本模块不做任何 schema 推导——对外发布的 inputSchema 与导出时逐字节相同,这样
"同一批工具在不同 harness 上表现差异"才不会被工具定义本身的漂移污染。
"""
from __future__ import annotations

import json
from pathlib import Path

_SPEC_PATH = Path(__file__).with_name("tools_spec.json")
_PAYLOAD = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))

SPEC_SOURCE: str = _PAYLOAD["source"]
TOOLS: list[dict] = _PAYLOAD["tools"]
TOOLS_BY_NAME: dict[str, dict] = {tool["name"]: tool for tool in TOOLS}

__all__ = ["SPEC_SOURCE", "TOOLS", "TOOLS_BY_NAME"]
