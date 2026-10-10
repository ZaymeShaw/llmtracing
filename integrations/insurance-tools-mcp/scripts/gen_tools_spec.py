"""导出模型可见工具面规格。

**必须在源工程环境运行**(需要 agno 才能拿到 `@tool` 装饰后的对象)。同步脚本会自动用
源工程的解释器调用它,一般不需要手工执行。

规格里放的是被测系统实际发布给模型的东西:name / description / inputSchema。
本工程对外发布的工具定义直接读这份规格,不做二次推导,因此两端逐字节一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 发布到 MCP 的工具:7 个查询工具。entity_lookup 是本地词典、ask_clarify 需要跨轮挂起态,
# 都不在本次范围内。
PUBLISHED_TOOLS = [
    "product_knowledge", "benefit_info", "general_qa", "policy_search",
    "customer_search", "policy_detail", "customer_detail",
]
SPEC_SOURCE = "insurance-qa-agent app/tools/bindings.py AGENT_TOOLS"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="上游工程根目录")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(args.source.resolve()))
    from app.tools.bindings import AGENT_TOOLS  # noqa: PLC0415  (依赖 sys.path,必须延迟导入)

    by_name = {tool.name: tool for tool in AGENT_TOOLS}
    missing = [name for name in PUBLISHED_TOOLS if name not in by_name]
    if missing:
        raise SystemExit(f"上游未发布这些工具,规格导出中止:{missing}")

    tools = [
        {"name": name, "description": by_name[name].description, "parameters": by_name[name].parameters}
        for name in PUBLISHED_TOOLS
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"source": SPEC_SOURCE, "tools": tools}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {len(tools)} tools -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
