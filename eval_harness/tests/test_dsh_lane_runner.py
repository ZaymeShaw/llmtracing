"""Exercise lane retirement when an SDK worker fails or capture remains open."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("dsh_wire", ROOT / "scripts/run_dsh_wire.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_runtime_readiness_rejects_startup_failure_and_missing_marker(tmp_path):
    marker = tmp_path / "ready.json"
    with pytest.raises(TimeoutError, match="readiness"):
        runner.wait_runtime_ready(marker, timeout=0)
    marker.write_text(json.dumps({"ready": False, "error": "MCP discovery failed"}))
    with pytest.raises(RuntimeError, match="MCP discovery failed"):
        runner.wait_runtime_ready(marker, timeout=1)
    marker.write_text(json.dumps({"ready": True, "tools": ["mcp__insurance-tools__get_user_id"]}))
    assert runner.wait_runtime_ready(marker, timeout=1)["tools"]


@pytest.mark.parametrize("outcome", ["success", "timeout", "partial", "failed_worker"])
def test_lane_not_reused_after_uncertain_worker_or_capture(tmp_path, monkeypatch, outcome):
    args = SimpleNamespace(output=tmp_path / "run", dsh_checkout=tmp_path, node="node",
                           cordis=tmp_path / "cordis.yml", max_tokens=64, timeout=1, run_id="batch")
    gw = SimpleNamespace(name="dsh_test", registry_dir=tmp_path / "registry", port="4107",
                         env={"UPSTREAM_MODEL": "test", "LITELLM_MASTER_KEY": "local-test-key"})
    jobs = []

    class Worker:
        pid = 999999
        returncode = 1 if outcome == "failed_worker" else 0

        def __init__(self, command, **kwargs):
            job_path = Path(command[-1])
            jobs.append(json.loads(job_path.read_text()))
            runner.save(job_path.parent / "sdk_result.json", {
                "success": self.returncode == 0,
                "turns": [{"finish_reason": "completed", "final_response": "ok"}],
            })

        def wait(self, timeout):
            if outcome == "timeout":
                raise subprocess.TimeoutExpired("worker", timeout)
            return self.returncode

    def capture(**kwargs):
        current = jobs[-1]
        return {"status": "partial" if outcome == "partial" else "complete",
                "counts": {"total": 1, "failed": 0}, "conflicts": [],
                "calls": [{"case_id": current["case"]["case_id"], "run_id": "batch",
                           "execution_id": current["execution_id"], "case_id_source": "lane_registry",
                           "lane_id": gw.name}]}

    terminated = []
    monkeypatch.setattr(runner.subprocess, "Popen", Worker)
    monkeypatch.setattr(runner, "snapshot_cursor", lambda **kwargs: {})
    monkeypatch.setattr(runner, "collect", capture)
    monkeypatch.setattr(runner, "terminate_group", lambda proc: terminated.append(proc.pid))
    cases = [{"case_id": c, "turns": ["hello"]} for c in ("A", "B")]
    result = runner.run_lane(gw, cases, args)
    registry = json.loads((gw.registry_dir / "dsh_test.json").read_text())
    if outcome == "success":
        assert len(result) == 2
        assert all(r["success"] for r in result)
        assert registry["state"] == "idle"
        assert jobs[0]["execution_id"] != jobs[1]["execution_id"]
    else:
        assert len(result) == 1  # the next case must never take this lane
        assert not result[0]["success"]
        assert registry["state"] == "blocked"
    if outcome in ("timeout", "failed_worker"):
        assert terminated == [999999]


def test_bundle_rejects_path_traversal_before_launch(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"case_id": "../outside", "turns": ["hi"]}))
    with pytest.raises(ValueError, match="case_id"):
        runner.load_cases(path)


def test_tool_requires_successful_result_in_the_same_session():
    def event(session, kind, data):
        return {"payload": {"sessionId": session, "event": {"type": kind, "data": data}}}
    call = event("a", "tool/call", {"callId": "c", "name": "bash"})
    result = {"message": {"content": [{"type": "tool-result", "toolCallId": "c", "isError": False,
                                      "content": [{"type": "text", "text": "fixture\n"}]}]}}
    assert not runner.tool_succeeded([call], "bash", "fixture")
    assert not runner.tool_succeeded([call, event("b", "tool/result", result)], "bash", "fixture")
    assert runner.tool_succeeded([call, event("a", "tool/result", result)], "bash", "fixture")
    result["message"]["content"][0]["isError"] = True
    assert not runner.tool_succeeded([call, event("a", "tool/result", result)], "bash", "fixture")


@pytest.mark.parametrize("body,is_error,kind,healthy", [
    ({"status": "SUCCESS", "source": "backend", "request_id": "r", "facts": ["fixture"]}, False, "ok", True),
    ({"status": "ERROR", "error": {"code": "UPSTREAM"}}, False, "upstream", False),
    ("Traceback (most recent call last)\nRuntimeError: broken", True, "infra", False),
])
def test_mcp_backend_failures_do_not_pass_on_agent_exit(body, is_error, kind, healthy):
    call = {"payload": {"sessionId": "s", "event": {"type": "tool/call", "data": {
        "callId": "c", "name": "mcp__insurance-tools__policy_search"}}}}
    text = json.dumps(body) if isinstance(body, dict) else body
    result = {"payload": {"sessionId": "s", "event": {"type": "tool/result", "data": {
        "message": {"content": [{"type": "tool-result", "toolCallId": "c", "isError": is_error,
                                  "content": [{"type": "text", "text": text}]}]}}}}}
    health = runner.mcp_health([call, result])
    assert health["healthy"] is healthy
    assert health["counts"][kind] == 1
    assert not runner.mcp_health([call])["healthy"]


def test_insurance_composition_uses_shared_prompt_and_keeps_credentials_out_of_file(tmp_path):
    import yaml
    repo = tmp_path / "repo"
    for path in ("sdk/server", "examples/agent-spine-demo", "llm/llm-deepseek",
                 "session/session-persistence-jsonl", "session/session-checkpoint-policy", "mcp/mcp-client"):
        lib = repo / "packages" / path / "lib/index.js"
        lib.parent.mkdir(parents=True)
        lib.touch()
    mcp, prompt = tmp_path / "mcp.json", tmp_path / "prompt.md"
    mcp.write_text(json.dumps({"mcpServers": {"insurance-tools": {
        "command": "/test/python", "args": ["-m", "itools"],
        "env": {"ITOOLS_AGENT_ID": "pi", "AUTH_TOKEN": "private-test-token"}}}}))
    prompt.write_text("Use the shared insurance MCP only.")
    config, env = runner.insurance_composition({"checkout": str(repo), "mcp_config": str(mcp),
                                               "agent_prompt": str(prompt), "thinking": "off"}, tmp_path)
    text = config.read_text()
    assert "private-test-token" not in text
    assert "!!js" in text and "JSON.parse(process.env.DSH_INSURANCE_MCP_ENV)" in text
    assert "toolBash: false" in text and "toolJobs: false" in text
    assert "thinking: disabled" in text and "failOnStartupError: true" in text
    assert prompt.read_text() in text
    assert env == {"ITOOLS_AGENT_ID": "dsh", "AUTH_TOKEN": "private-test-token"}
