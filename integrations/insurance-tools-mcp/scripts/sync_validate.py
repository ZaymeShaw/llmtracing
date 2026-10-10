"""同步产物验证(在 MCP 的 .venv 里、以候选树为根运行)。

    .venv/bin/python scripts/sync_validate.py --root <候选树> --modules-file m.json

步骤(任何一步失败 → 退出码 1,stdout 打一行 JSON 结果):
  a. py_compile 候选树里全部 .py;
  b. 逐个 import 同步闭包模块 + itools 自身模块,并核对 __file__ 确实落在候选树里
     (防止 editable 安装把 import 解析回旧树,"验证了个寂寞");
  c. `HarnessConf.model_rebuild()` + 用真实 conf 文件跑 `app.conf.harness_conf()`;
  d. 按 stdio 真起一个 MCP 服务:tools/list + 一次真实只读 tools/call,
     要求返回业务信封(EMPTY/未找到也算通过;异常文本/isError/ERROR 信封不通过);
  e. 服务列出的工具名/描述/inputSchema 与候选树 itools/tools_spec.json 逐项一致。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import py_compile
import sys
import tempfile
import time
import traceback
from pathlib import Path

INFRA_SIGNATURES = (
    "Traceback (most recent call last)", "PydanticUserError", "is not fully defined",
    "PydanticUndefinedAnnotation", "ModuleNotFoundError", "ImportError", "NameError",
    "AttributeError", "Connection refused", "ConnectError", "未登记的工具",
)
COMPILE_DIRS = ("app", "tool_adapter_sdk", "capabilities", "itools")
BUSINESS_OK_STATUSES = {"SUCCESS", "PARTIAL", "EMPTY", "AMBIGUOUS", "NEEDS_CLARIFICATION"}


def step_compile(root: Path) -> dict:
    bad = []
    n = 0
    paths = [p for d in COMPILE_DIRS if (root / d).is_dir() for p in (root / d).rglob("*.py")]
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(paths):
            if "__pycache__" in path.parts:
                continue
            n += 1
            try:
                py_compile.compile(str(path), cfile=os.path.join(tmp, f"{n}.pyc"), doraise=True)
            except py_compile.PyCompileError as exc:
                bad.append(f"{path.relative_to(root)}: {exc.msg.strip().splitlines()[-1]}")
    return {"ok": not bad, "files": n, "errors": bad[:20]}


def step_import(root: Path, modules: list[str]) -> dict:
    errors = []
    outside = []
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}"[:400])
            continue
        f = getattr(mod, "__file__", None)
        if f and not Path(f).resolve().is_relative_to(root):
            outside.append(f"{name} -> {f}")
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] in ("app", "tool_adapter_sdk", "itools", "capabilities"):
            f = getattr(mod, "__file__", None)
            if f and not Path(f).resolve().is_relative_to(root) and f"{name} -> {f}" not in outside:
                outside.append(f"{name} -> {f}")
    return {"ok": not errors and not outside, "modules": len(modules),
            "errors": errors[:20], "resolved_outside_root": outside[:20]}


def step_conf() -> dict:
    try:
        from app.conf import harness_conf
        conf = harness_conf()
        type(conf).model_rebuild(force=True)
        return {"ok": True, "harness_conf_version": getattr(conf, "version", None),
                "agent_profile_max_tool_calls": conf.agent_profile().max_tool_calls}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600],
                "traceback_tail": traceback.format_exc().splitlines()[-3:]}


def classify_tool_text(text: str, is_error: bool) -> tuple[bool, str]:
    """(通过?, 说明)。只认业务信封;异常文本、MCP isError、ERROR/TIMEOUT 信封一律不通过。"""
    for sig in INFRA_SIGNATURES:
        if sig in text:
            return False, f"infra signature {sig!r}"
    if is_error:
        return False, "MCP isError"
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        return False, "not a JSON envelope"
    if not isinstance(env, dict) or "status" not in env:
        return False, "JSON without envelope status"
    status = str(env.get("status"))
    if status not in BUSINESS_OK_STATUSES:
        code = (env.get("error") or {}).get("code") if isinstance(env.get("error"), dict) else None
        return False, f"envelope status={status} error.code={code}"
    return True, f"envelope status={status}"


async def _mcp_probe(root: Path, env: dict, probe: dict, spec: dict, timeout: float) -> dict:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "itools"], env=env, cwd=str(root))
    out: dict = {"ok": False}
    t0 = time.monotonic()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            served = [{"name": t.name, "description": t.description, "parameters": t.input_schema}
                      for t in listed.tools]
            out["tools_list"] = [t["name"] for t in served]
            spec_tools = [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
                          for t in spec["tools"]]
            mismatch = []
            if [t["name"] for t in served] != [t["name"] for t in spec_tools]:
                mismatch.append(f"names served={out['tools_list']} spec={[t['name'] for t in spec_tools]}")
            else:
                for a, b in zip(served, spec_tools):
                    if json.dumps(a["parameters"], sort_keys=True) != json.dumps(b["parameters"], sort_keys=True):
                        mismatch.append(f"{a['name']}: inputSchema differs")
                    if a["description"] != b["description"]:
                        mismatch.append(f"{a['name']}: description differs")
            out["spec_compare"] = {"ok": not mismatch, "mismatch": mismatch}
            result = await session.call_tool(probe["tool"], probe["arguments"])
            text = result.content[0].text if result.content else ""
            passed, why = classify_tool_text(text, bool(result.is_error))
            out["tool_call"] = {"tool": probe["tool"], "ok": passed, "why": why,
                                "is_error": bool(result.is_error), "text_head": text[:160]}
    out["elapsed_s"] = round(time.monotonic() - t0, 2)
    out["ok"] = bool(out.get("tool_call", {}).get("ok")) and bool(out.get("spec_compare", {}).get("ok"))
    return out


def step_mcp(root: Path, env_overrides: dict, probe: dict, timeout: float) -> dict:
    spec = json.loads((root / "itools" / "tools_spec.json").read_text(encoding="utf-8"))
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "TMPDIR")}
    env.update({"NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1"})
    env.update(env_overrides)
    env["PYTHONPATH"] = str(root)
    try:
        return asyncio.run(asyncio.wait_for(_mcp_probe(root, env, probe, spec, timeout), timeout))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--modules-file", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()
    root = args.root.resolve()
    cfg = json.loads(args.modules_file.read_text(encoding="utf-8"))
    os.chdir(root)
    sys.path.insert(0, str(root))
    for k, v in cfg.get("env", {}).items():
        os.environ[k] = v
    result: dict = {"root": str(root)}
    result["a_py_compile"] = step_compile(root)
    result["b_import"] = step_import(root, cfg["modules"]) if result["a_py_compile"]["ok"] else {"ok": False, "skipped": True}
    result["c_harness_conf"] = step_conf() if result["b_import"]["ok"] else {"ok": False, "skipped": True}
    if result["c_harness_conf"]["ok"]:
        mcp = step_mcp(root, cfg.get("env", {}), cfg["probe"], args.timeout)
        result["d_mcp_stdio"] = {k: v for k, v in mcp.items() if k != "spec_compare"}
        result["e_spec_compare"] = mcp.get("spec_compare", {"ok": False, "skipped": True})
    else:
        result["d_mcp_stdio"] = {"ok": False, "skipped": True}
        result["e_spec_compare"] = {"ok": False, "skipped": True}
    steps = ("a_py_compile", "b_import", "c_harness_conf", "d_mcp_stdio", "e_spec_compare")
    result["ok"] = all(result[s].get("ok") for s in steps)
    result["failed_step"] = next((s for s in steps if not result[s].get("ok")), None)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
