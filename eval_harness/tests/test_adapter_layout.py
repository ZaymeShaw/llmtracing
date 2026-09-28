from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval_harness" / "src"))

from eval_harness import run
from eval_harness.trace_identity import current_identity
from eval_harness.adapters import REGISTRY, register_adapter
from eval_harness.adapters import claude, insurance, pi
from eval_harness.adapters.base import CaseRunResult, TurnResult


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    harness = root / "eval_harness"
    shutil.copytree(ROOT / "eval_harness" / "configs", harness / "configs")
    bundles = harness / "bundles"
    bundles.mkdir()
    case = {"schema_version": "1.0", "case_id": "A01", "turns": ["Synthetic test prompt"]}
    for name in ("120_prompt_only_v1.jsonl", "120_prompt_only_0922.jsonl"):
        (bundles / name).write_text(json.dumps(case) + "\n")
    for name in ("claude", "pi"):
        workspace = root / "workspaces" / name
        workspace.mkdir(parents=True)
        (workspace / "agent.md").write_text("Synthetic test instructions")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(run, "PROJECT_ROOT", root)
    monkeypatch.setattr(run, "CURRENT_CASE_ID_FILE", root / "gateway_state" / "case_id")
    monkeypatch.setattr(run, "_harness_version", lambda _: "test-version")
    return root


@pytest.mark.parametrize("name", ["claude", "pi", "insurance"])
def test_moved_profiles_resolve_in_another_checkout_and_cwd(checkout, name, capsys):
    config = checkout / "eval_harness" / "configs" / "runs" / f"{name}.yaml"
    cfg = run._merge_profile(run._load_mapping(config), config.parent)
    assert Path(cfg["eval_runs_dir"]) == checkout / "eval_runs"
    if name != "insurance":
        assert Path(cfg["project_cwd"]) == checkout / "workspaces" / name
        assert cfg["harness_bin"] == name
    else:
        assert cfg["_adapter_options"]["gateway_log"] == checkout / "llm_gateway/logs/llm_calls.jsonl"
    assert run.main(["--config", str(config), "--all", "--dry-parse"]) == 0
    assert "total=1" in capsys.readouterr().out
    assert not (checkout / "eval_runs").exists()


def fake_case(**kwargs):
    case_dir = kwargs["case_dir"]
    case_dir.mkdir(parents=True, exist_ok=True)
    stream = case_dir / "stream_turn1.jsonl"
    event = {"type": "result", "result": "Synthetic response", "session_id": "test-session"}
    stream.write_text(json.dumps(event) + "\n")
    # The runner requires a completed gateway call as well as a CLI result.
    gateway_log = kwargs["project_cwd"].parents[1] / "llm_gateway/logs/llm_calls.jsonl"
    gateway_log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    call_id = current_identity()["execution_id"]
    records = [
        {"event": "request", "call_id": call_id, "case_id": kwargs["case_id"],
         **current_identity(),
         "ts": stamp, "request": {"messages": [{"role": "user", "content": "Synthetic prompt"}]}},
        {"event": "response", "call_id": call_id, "ts": stamp,
         "status_code": 200, "response": {"choices": [{"message": {"content": "Synthetic response"}}]}},
    ]
    with gateway_log.open("a") as out:
        out.write("\n".join(json.dumps(r) for r in records) + "\n")
    return CaseRunResult(
        case_id=kwargs["case_id"], session_id="test-session", success=True,
        exit_code=0, error=None, wall_ms=1,
        turns=[TurnResult(index=1, prompt=kwargs["turns"][0], exit_code=0,
                          stream_path=stream, final_text="Synthetic response", raw_events=[event])],
    )


@pytest.mark.parametrize("name,module,bin_key", [("claude", claude, "claude_bin"), ("pi", pi, "pi_bin")])
def test_cli_adapters_dispatch_and_produce_reports(checkout, monkeypatch, name, module, bin_key):
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return fake_case(**kwargs)

    monkeypatch.setattr(module, "run_case", invoke)
    config = checkout / "eval_harness/configs/runs" / f"{name}.yaml"
    assert run.main(["--config", str(config), "--all", "--run-id", "test", "--skip-preflight"]) == 0
    assert calls[0][bin_key] == name
    assert calls[0]["project_cwd"] == checkout / "workspaces" / name
    report = checkout / "eval_runs/test"
    assert (report / "cases/A01/trace.json").is_file()
    assert (report / "results.xlsx").is_file()
    assert (report / "llm_trace.html").is_file()
    # A repeat in the same batch archives the first execution, keeping both viewable.
    first = json.loads((report / "cases/A01/identity.json").read_text())["execution_id"]
    assert run.main(["--config", str(config), "--all", "--run-id", "test", "--skip-preflight"]) == 0
    from eval_harness.llm_trace_html import build_payload
    attempts = build_payload(report)["cases"]
    assert len(attempts) == 2 and sum(c["latest"] for c in attempts) == 1
    assert any(c["identity"]["execution_id"] == first and not c["latest"] for c in attempts)


@pytest.mark.parametrize("n_ok,expected", [(1, 0), (0, 2)])
def test_insurance_uses_common_entrypoint_and_preserves_batch_options(checkout, monkeypatch, n_ok, expected):
    calls = []

    def batch(**kwargs):
        calls.append(kwargs)
        return {"n_ok": n_ok, "n_cases": 1, "html_path": "synthetic-report.html", "html_error": None}

    monkeypatch.setattr(insurance, "run_live_batch", batch)
    config = checkout / "eval_harness/configs/runs/insurance.yaml"
    assert run.main(["--config", str(config), "--cases", "A01", "--run-id", "test", "--resume", "--skip-preflight"]) == expected
    assert calls[0]["bundle_case_ids"] == ["A01"]
    assert calls[0]["resume"] is True
    assert calls[0]["eval_runs_dir"] == checkout / "eval_runs"
    assert calls[0]["chat_url"] == "http://127.0.0.1:18063/v1/chat"
    assert calls[0]["attribution_config"] == checkout / "llm_gateway/attribution_lanes.json"
    assert "project_cwd" not in calls[0]


def test_new_adapter_receives_custom_options_without_runner_changes(checkout, monkeypatch):
    calls = []

    def custom(**kwargs):
        calls.append(kwargs)
        return fake_case(**kwargs)

    register_adapter("synthetic_harness", run_case=custom)
    profile = checkout / "eval_harness/configs/profiles/synthetic.yaml"
    profile.write_text("harness: synthetic_harness\nproject_cwd: ../../../workspaces/pi\nbin: synthetic-cli\n"
                       "output:\n  eval_runs_dir: ../../../eval_runs\nadapter:\n  custom_setting: passed-through\n")
    config = checkout / "eval_harness/configs/runs/synthetic.yaml"
    config.write_text("profile_path: ../profiles/synthetic.yaml\nbundle_path: ../../bundles/120_prompt_only_v1.jsonl\n")
    try:
        assert run.main(["--config", str(config), "--all", "--run-id", "custom", "--skip-preflight"]) == 0
        assert calls[0]["custom_setting"] == "passed-through"
        assert calls[0]["harness_bin"] == "synthetic-cli"
        with pytest.raises(ValueError, match="already registered"):
            register_adapter("synthetic_harness", run_case=custom)
    finally:
        REGISTRY.pop("synthetic_harness")


def test_all_moved_config_references_resolve():
    for path in (ROOT / "eval_harness/configs").rglob("*.yaml"):
        cfg = run._load_mapping(path)
        for key in ("profile_path", "claude_config", "pi_config"):
            if cfg.get(key):
                assert (path.parent / cfg[key]).resolve().is_file(), (path, key)


def test_batch_resume_requires_existing_run_identity(checkout, monkeypatch):
    def must_not_run(**kwargs):
        pytest.fail("Invalid resume must be rejected before calling the service adapter")

    monkeypatch.setattr(insurance, "run_live_batch", must_not_run)
    config = checkout / "eval_harness/configs/runs/insurance.yaml"
    with pytest.raises(SystemExit, match="--resume requires --run-id"):
        run.main(["--config", str(config), "--all", "--resume"])


@pytest.mark.parametrize("flag", ["--rebuild-excel", "--pack-zip"])
def test_batch_rejects_cli_artifact_flags_before_service_call(checkout, monkeypatch, flag):
    def must_not_run(**kwargs):
        pytest.fail("Unsupported artifact flags must be rejected before calling the service")

    monkeypatch.setattr(insurance, "run_live_batch", must_not_run)
    config = checkout / "eval_harness/configs/runs/insurance.yaml"
    with pytest.raises(SystemExit, match="unavailable for batch adapter"):
        run.main(["--config", str(config), "--all", flag])
