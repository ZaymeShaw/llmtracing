"""Preflight gate: MCP spawn exactly like the agent, fail on infra errors; skip is recorded."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eval_harness import preflight as pf

FAKE_SERVER = r'''
import json, os, sys
TOOLS = ["product_knowledge","benefit_info","general_qa","policy_search","customer_search","policy_detail","customer_detail"]
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m = msg["method"]
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "insurance-tools"}}
    elif m == "tools/list":
        res = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in TOOLS]}
    else:
        assert os.getcwd() == os.environ.get("EXPECT_CWD_PARENT_CHECK", os.getcwd())
        res = {"content": [{"type": "text", "text": os.environ["FAKE_TEXT"]}], "isError": os.environ.get("FAKE_ERR") == "1"}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
'''
OK_TEXT = json.dumps({"ok": True, "status": "SUCCESS", "facts": [{"x": 1}], "source": {"endpoint": "/customer/search"},
                      "request_id": "r"})


def _spec(tmp_path: Path, text: str, is_error: bool = False) -> pf.HarnessSpec:
    server = tmp_path / "fake_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    tmpl = tmp_path / "tmpl"
    tmpl.mkdir()
    (tmpl / ".mcp.json").write_text(json.dumps({"mcpServers": {"insurance-tools": {
        "command": sys.executable, "args": [str(server)],
        "env": {"FAKE_TEXT": text, "FAKE_ERR": "1" if is_error else "0", "ITOOLS_AGENT_ID": "t"}}}}), encoding="utf-8")
    root = tmp_path / "sandbox"
    root.mkdir()
    return pf.HarnessSpec(kind="claude", project_cwd=str(tmpl), agent_workdir_root=str(root))


def test_mcp_probe_healthy(tmp_path: Path) -> None:
    res = pf.check_mcp(_spec(tmp_path, OK_TEXT))
    assert res["ok"] is True and len(res["tools"]) == 7 and res["tool_call"]["status"] == "SUCCESS"
    assert "HOME" in res["env_keys"] and "FAKE_TEXT" in res["env_keys"]
    assert not any((tmp_path / "sandbox" / "claude").iterdir())  # sandbox cleaned


def test_mcp_probe_fails_on_pydantic_error(tmp_path: Path) -> None:
    res = pf.check_mcp(_spec(tmp_path, "PydanticUserError: `HarnessConf` is not fully defined", is_error=True))
    assert res["ok"] is False and res["tool_call"]["class"] == "infra"


def test_mcp_probe_fails_when_server_cannot_start(tmp_path: Path) -> None:
    spec = _spec(tmp_path, OK_TEXT)
    cfg = json.loads((Path(spec.project_cwd) / ".mcp.json").read_text())
    cfg["mcpServers"]["insurance-tools"]["args"] = ["-c", "import sys; sys.exit(3)"]
    (Path(spec.project_cwd) / ".mcp.json").write_text(json.dumps(cfg))
    res = pf.check_mcp(spec)
    assert res["ok"] is False and "exited" in res["error"]


def test_disk_threshold(tmp_path: Path) -> None:
    assert pf.check_disk(tmp_path, 0.0)["ok"] is True
    assert pf.check_disk(tmp_path, 10 ** 6)["ok"] is False


def test_itools_root_from_server() -> None:
    root = pf.itools_root_from_server({"command": "/x/insurance-tools-mcp/.venv/bin/python"})
    assert root == Path("/x/insurance-tools-mcp")
    assert pf.itools_root_from_server({"command": "python3"}) is None


def test_reuse_rules(tmp_path: Path) -> None:
    p = tmp_path / "pf.json"
    base = {"ok": True, "at": datetime.now(pf.TZ).isoformat(timespec="seconds"), "harnesses": ["claude", "pi"]}
    p.write_text(json.dumps(base))
    assert pf.load_reusable("pi", path=str(p))["reused_from"] == str(p)
    assert pf.load_reusable("insurance", path=str(p)) is None
    p.write_text(json.dumps({**base, "ok": False}))
    assert pf.load_reusable("pi", path=str(p)) is None
    old = (datetime.now(pf.TZ) - timedelta(hours=2)).isoformat(timespec="seconds")
    p.write_text(json.dumps({**base, "at": old}))
    assert pf.load_reusable("pi", path=str(p)) is None


def test_skip_record_and_gate(tmp_path: Path) -> None:
    from eval_harness import run as runmod
    args = runmod.build_arg_parser().parse_args(["--config", "x.yaml", "--cases", "A01", "--skip-preflight"])
    rec = runmod._preflight_gate(args=args, cfg={}, harness_name="claude_code", config_path=tmp_path / "x.yaml",
                                 project_cwd=tmp_path, eval_runs_dir=tmp_path, run_dir=tmp_path / "r")
    assert rec["skipped"] is True and rec["reason"] == "--skip-preflight"


def test_gate_failure_exits_4_and_records(tmp_path: Path, monkeypatch) -> None:
    from eval_harness import run as runmod
    monkeypatch.delenv(pf.RESULT_ENV, raising=False)
    monkeypatch.setattr(pf, "run_preflight", lambda *a, **k: {"ok": False, "failed": ["mcp_claude"],
                        "checks": {"mcp_claude": {"ok": False, "error": "boom"}}, "at": "t"})
    args = runmod.build_arg_parser().parse_args(["--config", "x.yaml", "--cases", "A01"])
    with pytest.raises(SystemExit) as ei:
        runmod._preflight_gate(args=args, cfg={"harness": "claude_code"}, harness_name="claude_code",
                               config_path=tmp_path / "x.yaml", project_cwd=tmp_path, eval_runs_dir=tmp_path,
                               run_dir=tmp_path / "r")
    assert ei.value.code == 4
    meta = json.loads((tmp_path / "r" / "run_meta.json").read_text())
    assert meta["mode"] == "preflight_failed" and meta["preflight"]["ok"] is False
