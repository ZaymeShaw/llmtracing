"""端到端冒烟:按真实 MCP 协议拉起服务,列工具,逐个调 7 个工具。

用法(仓库根目录):
    ENV_TYPE=dev ITOOLS_AGENT_ID=A1 ITOOLS_USER_ID=EMP000000 \
        .venv/bin/python scripts/smoke.py
只调一个:`ITOOLS_ONLY=policy_detail`。离线:`ITOOLS_DRY=1` 跳过调用只列工具。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

CASES: list[tuple[str, dict]] = [
    ("product_knowledge", {"query": "平安福的等待期是多少"}),
    ("benefit_info", {"query": "就医绿通怎么使用"}),
    ("general_qa", {"query": "犹豫期内退保怎么办理"}),
    ("policy_search", {"query": "查询保单号P260000000000001的保单"}),
    ("customer_search", {"query": "客户号尾号0001的客户"}),
    ("policy_detail", {"polno": "P260000000000001", "fields": ["policy_status"]}),
    ("customer_detail", {"customer_id": "MOCKC202609090001"}),
]


def _summarize(envelope: dict) -> str:
    parts = [f"status={envelope.get('status')}"]
    if envelope.get("facts"):
        parts.append(f"facts={len(envelope['facts'])}")
    if envelope.get("evidence"):
        parts.append(f"evidence={len(envelope['evidence'])}")
    if envelope.get("gaps"):
        parts.append(f"gaps={len(envelope['gaps'])}")
    if envelope.get("error"):
        parts.append(f"error={json.dumps(envelope['error'], ensure_ascii=False)}")
    if envelope.get("hint"):
        parts.append(f"hint={str(envelope['hint'])[:40]}")
    return " ".join(parts)


async def main() -> int:
    only = os.environ.get("ITOOLS_ONLY")
    dry = os.environ.get("ITOOLS_DRY") == "1"
    params = StdioServerParameters(command=sys.executable, args=["-m", "itools"], env={**os.environ})

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            print(f"tools/list -> {len(listed.tools)}")
            for tool in listed.tools:
                props = list((tool.input_schema or {}).get("properties", {}))
                print(f"  - {tool.name} {props}")
            if dry:
                return 0

            print()
            failed = 0
            for name, arguments in CASES:
                if only and name != only:
                    continue
                result = await session.call_tool(name, arguments)
                text = result.content[0].text if result.content else ""
                if result.is_error:
                    print(f"{name}: ERROR {text[:160]}")
                    failed += 1
                    continue
                try:
                    envelope = json.loads(text)
                except json.JSONDecodeError:
                    print(f"{name}: 非 JSON 信封 {text[:120]}")
                    failed += 1
                    continue
                print(f"{name}: {_summarize(envelope)}")
            return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
