"""agno 侧取样器:在**上游工程环境**里,用 RunContext 直接驱动 AGENT_TOOLS。

从 stdin 读一组用例,每个用例构造一轮完整会话态(assemble + set_budget_profile("agent")),
调用对应 @tool 的 entrypoint,把返回的 str 信封按行写 stdout。不经过 AgentOS、不经模型,
因此两侧比的是同一件事:工具层本身。

用例格式(JSON):
    {"id": "...", "identity": {...}, "tool": "...", "args": {...}}
identity 键:agent_id / upstream_user_id / empno / agentno / session_id

三种模式:
  默认            打真实上游
  --record PATH   打真实上游并把上游返回体录到 PATH
  --replay PATH   用录播替身顶替上游,保证与 MCP 侧拿到逐字节相同的响应
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path.cwd()
SCRIPTS = Path(__file__).resolve().parent

# 上游由模型侧 JSON Schema 强制入参合法性,工具函数看不到非法参数。
# 这里复刻同一层,好让两侧的可比对象是同一件事。
# (上游 venv 没装 jsonschema,不往里塞依赖,按用到的子集自己判。)
INVALID_ARGS = "INVALID_ARGS"

_PY_TYPES = {"string": str, "array": list, "integer": int, "number": (int, float),
             "boolean": bool, "object": dict}


def _args_valid(schema: dict, args: dict) -> bool:
    properties = schema.get("properties") or {}
    if any(name not in args for name in schema.get("required") or []):
        return False
    for name, value in args.items():
        declared = properties.get(name)
        if declared is None:
            return False
        expected = _PY_TYPES.get(declared.get("type"))
        if expected is not None and not isinstance(value, expected):
            return False
    return True


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, default=None)
    parser.add_argument("--replay", type=Path, default=None)
    args = parser.parse_args()
    if args.record and args.replay:
        parser.error("--record 与 --replay 互斥")

    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(SCRIPTS))
    from agno.run import RunContext

    from app.core.budget import set_budget_profile
    from app.orchestration.assembler import assemble
    from app.runtime.backend_provider import build_backend_runtime
    from app.tools.bindings import AGENT_TOOLS
    from replay_client import Recorder, Replayer

    tools = {tool.name: tool for tool in AGENT_TOOLS}
    writer: Recorder | None = None
    if args.replay is not None:
        client = Replayer(args.replay)
    else:
        runtime = build_backend_runtime()
        await runtime.astart()
        if args.record is not None:
            writer = Recorder(runtime.client)
            client = writer
        else:
            client = runtime.client

    cases = [json.loads(line) for line in sys.stdin if line.strip()]
    out = sys.stdout
    try:
        for case in cases:
            identity = case["identity"]
            state: dict = {}
            assemble(state)
            set_budget_profile(state["budget"], "agent")
            dependencies = {
                "agent_id": identity["agent_id"],
                "trace_id": case["id"],
                "backend_client": client,
            }
            for key in ("upstream_user_id", "empno", "agentno"):
                value = identity.get(key)
                if value is not None:
                    dependencies[key] = value
            context = RunContext(
                run_id=case["id"], session_id=identity.get("session_id") or case["id"],
                user_id=identity["agent_id"], dependencies=dependencies, session_state=state,
            )
            tool = tools[case["tool"]]
            if not _args_valid(tool.parameters, case["args"]):
                envelope = INVALID_ARGS
            else:
                try:
                    envelope = await tool.entrypoint(context, **case["args"])
                except Exception as exc:  # 异常也按同样形态记,便于两侧对齐
                    envelope = f"{type(exc).__name__}: {exc}"
            out.write(json.dumps({"id": case["id"], "envelope": envelope}, ensure_ascii=False) + "\n")
            out.flush()
    finally:
        if args.replay is None:
            await runtime.aclose()
        if writer is not None:
            writer.dump(args.record)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
