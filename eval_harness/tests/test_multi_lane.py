"""Multi-lane Insurance runs: one gateway process (.env.<name>) per business instance."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval_harness" / "src"))

from eval_harness.adapters import insurance as insurance_qa_adapter
from eval_harness.gateways import lane_not_ready, resolve_lanes
from eval_harness.llm_gateway_ingest import load_gateway_pairs, snapshot_offsets
from eval_harness.llm_trace_html import materialize_wire_run

ENV_TEMPLATE = """UPSTREAM_API_BASE=https://example.invalid/v1
UPSTREAM_API_KEY=sk-upstream-{name}
UPSTREAM_PROTOCOL=openai
UPSTREAM_MODEL=m
LITELLM_HOST=127.0.0.1
LITELLM_PORT={port}
LITELLM_MASTER_KEY=sk-master-{name}
LITELLM_CONFIG=./config.yaml
"""


def _gateway_dir(tmp_path: Path, lanes: dict[str, tuple[int, str]]) -> Path:
    gw = tmp_path / "llm_gateway"
    gw.mkdir()
    shutil.copy(ROOT / "llm_gateway" / "gatewayctl.py", gw / "gatewayctl.py")
    (gw / "config.yaml").write_text("{}\n")
    for name, (port, url) in lanes.items():
        (gw / f".env.{name}").write_text(ENV_TEMPLATE.format(name=name, port=port)
                                          + f"LANE_POOL=ins\nLANE_TARGET_URL={url}\n")
    (gw / ".env.other").write_text(ENV_TEMPLATE.format(name="other", port=4999))
    (gw / ".env.example").write_text("# ignored\n")
    (gw / ".env.ins_a.bak_1").write_text("# ignored\n")
    return gw


def _mark_ready(spec, pid: int | None = None) -> None:
    spec.ready_marker.parent.mkdir(parents=True, exist_ok=True)
    spec.ready_marker.write_text(json.dumps({
        "pid": pid or os.getpid(), "port": int(spec.port), "mode": "env", "gateway": spec.name,
        "lane_pool": spec.pool, "config_path": str(spec.env_file),
        "config_sha256": hashlib.sha256(spec.env_file.read_bytes()).hexdigest()}))


def _write_call(log: Path, call_id: str, execution_id: str, case_id: str, gateway: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    base = {"call_id": call_id, "case_id": case_id, "execution_id": execution_id,
            "attribution_status": "attributed", "lane_id": gateway, "gateway": gateway,
            "ts": "2026-01-01T00:00:00+00:00", "model": "m", "protocol": "chat_completions"}
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**base, "event": "pre_api_call", "messages": []}) + "\n")
        f.write(json.dumps({**base, "event": "success", "response": {"choices": []}}) + "\n")


def test_directory_ingest_reads_every_gateway_log(tmp_path: Path):
    logs = tmp_path / "logs"
    legacy = logs / "llm_calls.jsonl"
    _write_call(legacy, "old", "e0", "C0", "legacy")
    offsets = snapshot_offsets(logs)
    assert offsets == {str(legacy): legacy.stat().st_size}
    _write_call(legacy, "c1", "e1", "C1", "legacy")
    _write_call(logs / "gateways" / "ins_a" / "llm_calls.jsonl", "c2", "e2", "C2", "ins_a")  # new file after snapshot
    (logs / "llm_calls_20260101.jsonl.gz").write_bytes(b"not read")
    pairs = load_gateway_pairs(logs, start_offset=offsets, strict=True)
    assert sorted(p["call_id"] for p in pairs) == ["c1", "c2"]
    assert {p["call_id"]: p.get("gateway") for p in pairs}["c2"] == "ins_a"
    assert len(load_gateway_pairs(logs)) == 3
    legacy.write_text("")
    with pytest.raises(RuntimeError, match="truncated"):
        load_gateway_pairs(logs, start_offset=offsets)
    out = materialize_wire_run(gateway_log=logs, out_run_dir=tmp_path / "wire", execution_ids=["e2"])
    assert (out / "llm_trace.html").is_file()


def test_resolve_lanes_and_readiness(tmp_path: Path):
    gw = _gateway_dir(tmp_path, {"ins_a": (4101, "http://127.0.0.1:18101/v1/chat"),
                                 "ins_b": (4102, "http://127.0.0.1:18102/v1/chat")})
    specs = resolve_lanes("ins", gw)
    assert [s.name for s in specs] == ["ins_a", "ins_b"]
    a = specs[0]
    assert a.gateway_log == gw / "logs/gateways/ins_a/llm_calls.jsonl"
    assert a.registry_dir == (gw / "run/attribution").resolve()
    assert [s.name for s in resolve_lanes("ins_b", gw)] == ["ins_b"]
    with pytest.raises(RuntimeError, match="not a lane"):
        resolve_lanes("other", gw)
    assert "not ready" in lane_not_ready(a)
    _mark_ready(a)
    assert lane_not_ready(a) is None
    a.env_file.write_text(a.env_file.read_text() + "# edit\n")
    assert "changed since start" in lane_not_ready(a)


def test_live_batch_runs_one_worker_per_lane(tmp_path: Path, monkeypatch):
    urls = {"ins_a": "http://127.0.0.1:18101/v1/chat", "ins_b": "http://127.0.0.1:18102/v1/chat"}
    gw = _gateway_dir(tmp_path, {"ins_a": (4101, urls["ins_a"]), "ins_b": (4102, urls["ins_b"])})
    specs = {s.name: s for s in resolve_lanes("ins", gw)}
    for spec in specs.values():
        _mark_ready(spec)
    by_url = {s.chat_url: s for s in specs.values()}
    bundle = tmp_path / "bundle.jsonl"
    ids = ["A01", "A02", "A03", "A04"]
    bundle.write_text("\n".join(json.dumps({"schema_version": "1.0", "case_id": c, "turns": [f"q {c}"]})
                                for c in ids) + "\n")
    both_busy = threading.Barrier(2, timeout=10)
    seen: list[tuple[str, str]] = []

    def fake_post(*, chat_url: str, prompt: str, case_id: str, **_kw):
        # The "business instance" behind chat_url calls its own gateway with only the
        # gateway key; the gateway attributes via its lane registry (simulated here).
        spec = by_url[chat_url]
        reg = json.loads((spec.registry_dir / f"{spec.name}.json").read_text())
        assert reg["state"] == "active" and reg["case_id"] == case_id
        if prompt in ("q A01", "q A02"):
            both_busy.wait()  # proves the two lanes run concurrently
        _write_call(spec.gateway_log, f"{spec.name}-{case_id}", reg["execution_id"], case_id, spec.name)
        seen.append((spec.name, case_id))
        return 200, {"answer": "ok " + prompt}, None

    monkeypatch.setattr(insurance_qa_adapter, "_post_live_chat", fake_post)
    monkeypatch.setenv("INSURANCE_LIVE_CONCURRENCY", "7")  # ignored in lane mode
    result = insurance_qa_adapter.run_live_batch(
        bundle_case_ids=ids, run_id="lanes", bundle_path=bundle, eval_runs_dir=tmp_path / "runs",
        lanes="ins", gateway_dir=gw)
    assert result["n_ok"] == 4 and result["n_cases"] == 4 and result["n_unrun"] == 0
    assert {lane for lane, _ in seen} == {"ins_a", "ins_b"}
    run_dir = Path(result["run_dir"])
    for lane, case in seen:
        meta = json.loads((run_dir / "cases" / case / "meta.json").read_text())
        assert meta["lane_id"] == lane and meta["chat_url"] == urls[lane]
        assert meta["attribution_status"] == "captured"
        calls = [json.loads(x) for x in (run_dir / "cases" / case / "llm_calls.jsonl").read_text().splitlines()]
        assert len(calls) == 1 and calls[0]["execution_id"] == meta["execution_id"]
    run_meta = json.loads((run_dir / "run_meta.json").read_text())
    assert run_meta["lanes"]["used"] == ["ins_a", "ins_b"]
    assert sorted(run_meta["gateway_log"]) == sorted(str(s.gateway_log) for s in specs.values())
    for spec in specs.values():
        assert json.loads((spec.registry_dir / f"{spec.name}.json").read_text())["state"] == "idle"


def test_unready_lane_is_skipped_and_blocked_lane_dropped(tmp_path: Path, monkeypatch):
    urls = {"ins_a": "http://127.0.0.1:18101/v1/chat", "ins_b": "http://127.0.0.1:18102/v1/chat"}
    gw = _gateway_dir(tmp_path, {"ins_a": (4101, urls["ins_a"]), "ins_b": (4102, urls["ins_b"])})
    specs = {s.name: s for s in resolve_lanes("ins", gw)}
    _mark_ready(specs["ins_a"])  # ins_b never started
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text("\n".join(json.dumps({"schema_version": "1.0", "case_id": c, "turns": [f"q {c}"]})
                                for c in ("A01", "A02")) + "\n")

    def fake_post(**_kw):
        return 0, None, "ReadTimeout: simulated"

    monkeypatch.setattr(insurance_qa_adapter, "_post_live_chat", fake_post)
    result = insurance_qa_adapter.run_live_batch(
        bundle_case_ids=["A01", "A02"], run_id="drop", bundle_path=bundle,
        eval_runs_dir=tmp_path / "runs", lanes="ins", gateway_dir=gw)
    # First case blocks ins_a, the only ready lane; the second case is reported as not run.
    assert result["n_ok"] == 0 and result["n_cases"] == 2 and result["n_unrun"] == 1
    run_meta = json.loads((Path(result["run_dir"]) / "run_meta.json").read_text())
    assert "ins_b" in run_meta["lanes"]["skipped"] and "ins_a" in run_meta["lanes"]["dropped"]
    assert json.loads((specs["ins_a"].registry_dir / "ins_a.json").read_text())["state"] == "blocked"
    # A blocked lane is not silently reused by the next run.
    with pytest.raises(RuntimeError, match="no lane could be leased"):
        insurance_qa_adapter.run_live_batch(
            bundle_case_ids=["A02"], run_id="drop2", bundle_path=bundle,
            eval_runs_dir=tmp_path / "runs", lanes="ins", gateway_dir=gw)


def test_no_ready_lane_fails_fast(tmp_path: Path):
    gw = _gateway_dir(tmp_path, {"ins_a": (4101, "http://127.0.0.1:18101/v1/chat")})
    with pytest.raises(RuntimeError, match="no ready lane"):
        insurance_qa_adapter.run_live_batch(bundle_case_ids=["A01"], run_id="x",
                                            eval_runs_dir=tmp_path / "runs", lanes="ins", gateway_dir=gw)
