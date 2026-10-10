"""同输入对拍:agno 侧 AGENT_TOOLS  vs  本工程 MCP 服务。

同一批用例、同一身份、同一轮次启动方式,两边各跑一遍,逐字节比信封。
- 准入/错误路径必须**完全一致**(不触下游,确定性)。
- 真实查询路径因为有上游 LLM 解析,不保证逐字节,转而比结构(status、键集、error.code)。

用法:
    .venv/bin/python scripts/parity.py --agno-repo <上游工程根目录> [--agno-python PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent
AGNO_PROBE = Path(__file__).resolve().parent / "agno_probe.py"

FULL = {"agent_id": "A1", "upstream_user_id": "EMP000000", "empno": "EMP000000",
        "agentno": "EMP000000", "session_id": "S-PARITY"}
BARE = {"agent_id": "A1", "session_id": "S-PARITY"}

QUERIES = {
    "product_knowledge": {"query": "平安福的等待期是多少"},
    "benefit_info": {"query": "就医绿通怎么使用"},
    "general_qa": {"query": "犹豫期内退保怎么办理"},
    "policy_search": {"query": "查询保单号P260000000000001的保单"},
    "customer_search": {"query": "客户号尾号0001的客户"},
    "policy_detail": {"polno": "P260000000000001", "fields": ["policy_status"]},
    "customer_detail": {"customer_id": "MOCKC202609090001"},
}
REQUIRES_UPSTREAM = {"product_knowledge", "benefit_info", "general_qa",
                     "policy_search", "customer_search", "customer_detail"}


def build_cases() -> list[dict]:
    cases: list[dict] = []
    for tool, args in QUERIES.items():
        cases.append({"id": f"bare-{tool}", "identity": BARE, "tool": tool, "args": args,
                      "offline": tool not in REQUIRES_UPSTREAM})
        cases.append({"id": f"full-{tool}", "identity": FULL, "tool": tool, "args": args,
                      "offline": False})
    # 入参非法:上游由模型侧 JSON Schema 拦下,这里应落在协议边界。
    cases.append({"id": "schema-missing-required", "identity": FULL,
                  "tool": "policy_detail", "args": {}, "offline": True})
    cases.append({"id": "schema-wrong-type", "identity": FULL,
                  "tool": "general_qa", "args": {"query": 123}, "offline": True})
    return cases


def run_agno_side(cases: list[dict], repo: Path, python: str) -> dict[str, str]:
    payload = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    proc = subprocess.run(
        [python, str(AGNO_PROBE)], input=payload, text=True, capture_output=True,
        cwd=repo, env={**dict(), "PATH": "/usr/bin:/bin", "ENV_TYPE": "dev",
                       "HOME": str(Path.home())},
    )
    if proc.returncode != 0:
        raise SystemExit(f"agno 侧失败:\n{proc.stderr[-4000:]}")
    return {json.loads(line)["id"]: json.loads(line)["envelope"] for line in proc.stdout.splitlines()}


async def run_mcp_side(cases: list[dict]) -> dict[str, str]:
    results: dict[str, str] = {}
    for label, identity in (("bare", BARE), ("full", FULL)):
        env = {"ENV_TYPE": "dev", "ITOOLS_AGENT_ID": identity["agent_id"],
               "ITOOLS_SESSION_ID": identity["session_id"], "PATH": "/usr/bin:/bin"}
        for key, var in (("upstream_user_id", "ITOOLS_USER_ID"), ("empno", "ITOOLS_EMPNO"),
                         ("agentno", "ITOOLS_AGENTNO")):
            if identity.get(key) is not None:
                env[var] = identity[key]
        params = StdioServerParameters(command=sys.executable, args=["-m", "itools"],
                                       env=env, cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for case in cases:
                    if not case["id"].startswith(f"{label}-"):
                        continue
                    result = await session.call_tool(case["tool"], case["args"])
                    if result.is_error:
                        results[case["id"]] = result.content[0].text
                    else:
                        results[case["id"]] = result.content[0].text
    # schema 用例只跑一次(与身份无关)
    params = StdioServerParameters(command=sys.executable, args=["-m", "itools"],
                                   env={"ENV_TYPE": "dev", "ITOOLS_AGENT_ID": "A1",
                                        "ITOOLS_USER_ID": "EMP000000",
                                        "ITOOLS_EMPNO": "EMP000000",
                                        "ITOOLS_AGENTNO": "EMP000000",
                                        "ITOOLS_SESSION_ID": "S-PARITY",
                                        "PATH": "/usr/bin:/bin"},
                                   cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for case in cases:
                if not case["id"].startswith("schema-"):
                    continue
                result = await session.call_tool(case["tool"], case["args"])
                results[case["id"]] = result.content[0].text
    return results


def compare(case: dict, left: str, right: str) -> tuple[str, str]:
    if case["id"].startswith("schema-"):
        # 两侧的拒绝文案各写各的(上游根本没有这层文案,是模型侧 JSON Schema 拦的),
        # 因此只比"是否被拒",不比文案。
        agno_rejected = left == "INVALID_ARGS"
        mcp_rejected = not right.lstrip().startswith("{")
        return ("EXACT" if agno_rejected == mcp_rejected == True else "DIFF",
                "" if agno_rejected else "agno 侧未拒绝")
    if left == right:
        return "EXACT", ""
    try:
        a, b = json.loads(left), json.loads(right)
    except json.JSONDecodeError:
        return "DIFF", "一侧非 JSON"
    if not isinstance(a, dict) or not isinstance(b, dict):
        return "DIFF", "信封不是对象"
    notes = []
    for key in ("status",):
        if a.get(key) != b.get(key):
            notes.append(f"{key}: {a.get(key)} vs {b.get(key)}")
    acode = (a.get("error") or {}).get("code")
    bcode = (b.get("error") or {}).get("code")
    if acode != bcode:
        notes.append(f"error.code: {acode} vs {bcode}")
    shape = lambda env: {k: (len(v) if isinstance(v, list) else type(v).__name__)
                         for k, v in env.items() if k != "hint"}
    if shape(a) != shape(b):
        notes.append(f"键集/长度: {shape(a)} vs {shape(b)}")
    return ("STRUCTURAL", "; ".join(notes)) if not notes else ("DIFF", "; ".join(notes))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agno-repo", required=True, type=Path)
    parser.add_argument("--agno-python", default=None)
    args = parser.parse_args()
    repo = args.agno_repo.expanduser().resolve()
    python = args.agno_python or str(repo / ".feval_venv" / "bin" / "python")
    cases = build_cases()

    print(f"用例 {len(cases)} 条;上游 = {repo}\n", flush=True)
    mcp = await run_mcp_side(cases)
    print("MCP 侧取样完成,开始 agno 侧…\n", flush=True)
    agno = run_agno_side(cases, repo, python)

    tally: dict[str, int] = {}
    rows = []
    for case in cases:
        verdict, note = compare(case, agno.get(case["id"], ""), mcp.get(case["id"], ""))
        tally[verdict] = tally.get(verdict, 0) + 1
        rows.append((case["id"], verdict, note))

    width = max(len(row[0]) for row in rows)
    for name, verdict, note in rows:
        mark = {"EXACT": "✅ 逐字节", "STRUCTURAL": "≈ 结构一致", "DIFF": "❌ 不一致"}[verdict]
        print(f"  {name:<{width}}  {mark}  {note}")

    print(f"\n合计:{tally}")
    offline = [case["id"] for case in cases if case["offline"]]
    hard = [name for name, verdict, _ in rows if verdict == "DIFF" and name in offline]
    if hard:
        print(f"\n确定性用例不一致,判失败:{hard}")
        return 1
    print("\n全部确定性用例逐字节一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
