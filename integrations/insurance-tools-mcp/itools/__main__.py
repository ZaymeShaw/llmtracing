"""stdio 入口:`python -m itools`。

身份与下游环境在进程启动时一次性解析;身份非法或缺认证主体则拒绝启动,
不做任何隐式兜底。工具调用期间不再读环境。
"""
from __future__ import annotations

import asyncio
import logging
import sys

from itools.app import SERVER_NAME, serve_stdio
from itools.identity import Identity, identity_from_env
from itools.runtime import build_backend_runtime_for_process
from itools.spec import TOOLS

logger = logging.getLogger("itools")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        identity = identity_from_env()
    except Exception as exc:
        print(f"启动失败:{exc}", file=sys.stderr)
        return 2
    asyncio.run(_serve(identity))
    return 0


async def _serve(identity: Identity) -> None:
    runtime, env_type = await build_backend_runtime_for_process()
    logger.info(
        "started server=%s env_type=%s tools=%d agent_id=%s",
        SERVER_NAME, env_type, len(TOOLS), identity.agent_id,
    )
    try:
        await serve_stdio(identity=identity, backend_client=runtime.client)
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    raise SystemExit(main())
