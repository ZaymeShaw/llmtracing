"""录播对拍:把上游钉死,验证两条通道跑出**逐字节相同**的信封。

上一版 `parity.py` 打的是真实上游,检索内容每次不同,只能比到"结构一致"。这里先录一次
真实上游响应,再让两侧都用同一份录播跑,于是差异只剩通道层本身——有差异就是真的有差异。

流程(一条命令):
    1. 用上游工程环境打一次真实上游,把响应录进 fixture;
    2. 两侧都用录播重跑同一批用例;
    3. 逐字节比。

用法:
    .venv/bin/python scripts/parity_stub.py --agno-repo <上游工程根目录>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from replay_client import Replayer  # noqa: E402  (依赖上面的 sys.path)

from itools.dispatch import dispatch  # noqa: E402
from itools.identity import Identity  # noqa: E402

AGNO_PROBE = SCRIPTS / "agno_probe.py"

FULL = {"agent_id": "A1", "upstream_user_id": "EMP000000", "empno": "EMP000000",
        "agentno": "EMP000000", "session_id": "S-PARITY"}

QUERIES = {
    "product_knowledge": {"query": "平安福的等待期是多少"},
    "benefit_info": {"query": "就医绿通怎么使用"},
    "general_qa": {"query": "犹豫期内退保怎么办理"},
    "policy_search": {"query": "查询保单号P260000000000001的保单"},
    "customer_search": {"query": "客户号尾号0001的客户"},
    "policy_detail": {"polno": "P260000000000001", "fields": ["policy_status"]},
    "customer_detail": {"customer_id": "MOCKC202609090001"},
}


def build_cases() -> list[dict]:
    return [
        {"id": f"live-{tool}", "identity": FULL, "tool": tool, "args": args}
        for tool, args in QUERIES.items()
    ]


# 信封里的标识符全部由调用方给定的 trace_id 派生(request_id = CALL-<trace>-NN,
# 再派生出 evidence_id / fact_id / source_call_id / evidence_refs)。两侧 trace_id 不同
# 是**输入差异**,不是行为差异——同一个系统连跑两次也会不同。故比对前把它归一掉,
# 其余一切(含 facts 内容与顺序、data、field_descriptions、gaps、hint)逐字节比。
TRACE_PATTERN = re.compile(r"^CALL-(?P<trace>.+)-\d+$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z$")


def _trace_of(envelope: dict) -> str | None:
    request_id = envelope.get("request_id")
    if not isinstance(request_id, str):
        return None
    match = TRACE_PATTERN.match(request_id)
    return match.group("trace") if match else None


def canonicalize(raw: str) -> str:
    """把 trace 派生标识与取数时间戳换成占位符,留下可与另一侧逐字节比的部分。"""
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(envelope, dict):
        return raw
    trace = _trace_of(envelope)

    def scrub(value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            if TIMESTAMP_PATTERN.match(value):
                return "<TS>"
            return value.replace(trace, "<TRACE>") if trace else value
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {name: scrub(item, name) for name, item in value.items()}
        return value

    return json.dumps(scrub(envelope), ensure_ascii=False, sort_keys=True)


def run_agno(cases: list[dict], repo: Path, python: str, *extra: str) -> dict[str, str]:
    payload = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    proc = subprocess.run(
        [python, str(AGNO_PROBE), *extra], input=payload, text=True, capture_output=True,
        cwd=repo, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "ENV_TYPE": "dev"},
    )
    if proc.returncode != 0:
        raise SystemExit(f"agno 侧失败:\n{proc.stderr[-4000:]}")
    return {json.loads(line)["id"]: json.loads(line)["envelope"] for line in proc.stdout.splitlines()}


async def run_mcp(cases: list[dict], fixture: Path) -> dict[str, str]:
    identity = Identity(
        agent_id=FULL["agent_id"], upstream_user_id=FULL["upstream_user_id"],
        empno=FULL["empno"], agentno=FULL["agentno"], session_id=FULL["session_id"],
    )
    results: dict[str, str] = {}
    for case in cases:
        # 每个用例换一个 Replayer:录播按调用次序消费,用例之间不能互抢。
        results[case["id"]] = await dispatch(
            case["tool"], case["args"], identity=identity, backend_client=Replayer(fixture),
        )
    return results


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agno-repo", required=True, type=Path)
    parser.add_argument("--agno-python", default=None)
    parser.add_argument("--fixture", type=Path, default=None,
                        help="录播文件位置;给定时若已存在则跳过录制")
    args = parser.parse_args()
    repo = args.agno_repo.expanduser().resolve()
    python = args.agno_python or str(repo / ".feval_venv" / "bin" / "python")
    cases = build_cases()

    if args.fixture is None:
        fixture = Path(tempfile.mkdtemp(prefix="parity_stub_")) / "backend_replay.json"
    else:
        fixture = args.fixture

    if not fixture.exists():
        print(f"① 打真实上游录制 {len(cases)} 条用例 → {fixture}", flush=True)
        run_agno(cases, repo, python, "--record", str(fixture))
    else:
        print(f"① 复用已有录播 → {fixture}", flush=True)

    print("② 两侧用录播重跑", flush=True)
    agno = run_agno(cases, repo, python, "--replay", str(fixture))
    mcp = await run_mcp(cases, fixture)

    print("\n③ 归一 trace_id / 取数时间戳后逐字节比对\n", flush=True)
    width = max(len(case["id"]) for case in cases)
    failures = []
    for case in cases:
        name = case["id"]
        left, right = agno.get(name, ""), mcp.get(name, "")
        raw_same = left == right
        if raw_same or canonicalize(left) == canonicalize(right):
            mark = "✅ 逐字节相同" if raw_same else "✅ 相同(trace/时间戳已归一)"
            print(f"  {name:<{width}}  {mark}  ({len(left)} 字符)")
            continue
        failures.append(name)
        try:
            a, b = json.loads(left), json.loads(right)
            print(f"  {name:<{width}}  ❌ 不同  status {a.get('status')} vs {b.get('status')}; "
                  f"键集 {sorted(a)} vs {sorted(b)}")
        except json.JSONDecodeError:
            print(f"  {name:<{width}}  ❌ 不同  {left[:80]!r} vs {right[:80]!r}")

    total = len(cases)
    print(f"\n{total - len(failures)}/{total} 条逐字节相同。")
    if failures:
        print(f"不一致:{failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
