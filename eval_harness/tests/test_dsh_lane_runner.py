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
                           "execution_id": current["execution_id"]}]}

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
