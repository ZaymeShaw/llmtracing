"""gatewayctl (one .env.<name> == one gateway) and env-mode lane attribution."""
from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # llm_gateway/
sys.path.insert(0, str(ROOT))

import gatewayctl  # noqa: E402

BASE = """UPSTREAM_API_BASE=https://example.invalid/v1
UPSTREAM_API_KEY=sk-upstream-secret-value
UPSTREAM_PROTOCOL=openai
UPSTREAM_MODEL=m
LITELLM_HOST=127.0.0.1
LITELLM_PORT={port}
LITELLM_MASTER_KEY=sk-master-{name}-value
LITELLM_CONFIG=./config.yaml
"""

# Fake proxy: answer /v1/models and run the real callback startup (mark_gateway_ready).
STUB = """#!{python}
import http.server, sys
sys.argv[0] = "litellm"
port = int(sys.argv[sys.argv.index("--port") + 1])
from callbacks.attribution import lane_mode, mark_gateway_ready
if lane_mode():
    mark_gateway_ready()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.end_headers()
        self.wfile.write(b'{{"data": []}}')
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(name: str, port: int, extra: str = "") -> str:
    return BASE.format(name=name, port=port) + extra


@pytest.fixture
def gw(tmp_path: Path) -> Path:
    d = tmp_path / "llm_gateway"
    d.mkdir()
    shutil.copy(ROOT / "gatewayctl.py", d / "gatewayctl.py")
    shutil.copytree(ROOT / "callbacks", d / "callbacks", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    (d / "config.yaml").write_text("{}\n")
    return d


def test_discover_select_and_derived_paths(gw: Path):
    (gw / ".env").write_text(_env("default", 4001))
    (gw / ".env.ins_a").write_text(_env("ins_a", 4101, "LANE_POOL=ins\nLANE_TARGET_URL=http://127.0.0.1:18101/v1/chat\n"))
    (gw / ".env.ins_b").write_text(_env("ins_b", 4102, "LANE_POOL=ins\nLANE_TARGET_URL=http://127.0.0.1:18102/v1/chat\n"))
    for ignored in (".env.example", ".env.bak_2026", ".env.ins_a.bak_1"):
        (gw / ignored).write_text("X=1\n")
    assert [g.name for g in gatewayctl.discover(gw)] == ["default", "ins_a", "ins_b"]
    assert [g.name for g in gatewayctl.select(["ins"], gw)] == ["ins_a", "ins_b"]
    assert [g.name for g in gatewayctl.select(["default,ins_b"], gw)] == ["default", "ins_b"]
    with pytest.raises(gatewayctl.GatewayError, match="not a lane"):
        gatewayctl.lanes("default", gw)
    a = gatewayctl.load("ins_a", gw)
    root = gw.resolve()
    assert a.log_file == root / "logs/gateways/ins_a/llm_calls.jsonl"
    assert a.pid_file == root / "run/gateways/ins_a.pid"
    assert a.ready_marker == root / "run/attribution/gateway_ready.port-4101.json"
    assert gatewayctl.check(gatewayctl.discover(gw)) == ([], [])
    assert gatewayctl.mask("UPSTREAM_API_KEY", "sk-upstream-secret-value") == "sk-***"
    assert gatewayctl.mask("LITELLM_PORT", "4101") == "4101"


def test_check_reports_lane_conflicts(gw: Path):
    lane = "LANE_POOL=ins\nLANE_TARGET_URL=http://127.0.0.1:18101/v1/chat\n"
    (gw / ".env.ins_a").write_text(_env("ins_a", 4101, lane))
    (gw / ".env.ins_b").write_text(_env("ins_b", 4101, lane))  # same port + same target
    (gw / ".env.alt1").write_text(_env("alt1", 4200))
    (gw / ".env.alt2").write_text(_env("alt2", 4200))  # non-lane alternatives: warning only
    (gw / ".env.bad").write_text("LITELLM_PORT=x\nLANE_POOL=ins\nLANE_TARGET_URL=ftp://x\n")
    errors, warnings = gatewayctl.check(gatewayctl.discover(gw))
    text = "\n".join(errors)
    assert "port shared by ins_a, ins_b" in text
    assert "LANE_TARGET_URL shared by ins_a, ins_b" in text
    assert "bad: missing" in text and "bad: LITELLM_PORT must be a number" in text
    assert "bad: LANE_TARGET_URL must be http(s)" in text
    assert not any("alt1" in e for e in errors)
    assert any("port shared by alt1, alt2" in w for w in warnings)


def test_child_env_strips_inherited_identity(gw: Path):
    (gw / ".env.ins_a").write_text(_env("ins_a", 4101, "LANE_POOL=ins\nLANE_TARGET_URL=http://h/v1/chat\n"))
    g = gatewayctl.load("ins_a", gw)
    env = gatewayctl.child_env(g, {"PATH": "/bin", "LLM_ATTRIBUTION_CONFIG": "/x.json",
                                   "LLM_GATEWAY_LOG_DIR": "/elsewhere", "LANE_POOL": "other"})
    assert "LLM_ATTRIBUTION_CONFIG" not in env
    assert env["LANE_POOL"] == "ins" and env["LLM_GATEWAY_NAME"] == "ins_a"
    assert env["LLM_GATEWAY_LOG_DIR"] == str(g.log_dir)
    assert env["LANE_REGISTRY_DIR"] == str(g.registry_dir)
    assert env["LLM_GATEWAY_ENV_FILE"] == str(g.env_file)
    assert env["UPSTREAM_LITELLM_MODEL"] == "openai/m"
    assert env["DISABLE_AIOHTTP_TRANSPORT"] == "True"
    assert gatewayctl.child_env(g, {"DISABLE_AIOHTTP_TRANSPORT": "False"})["DISABLE_AIOHTTP_TRANSPORT"] == "False"


def test_env_mode_attribution_and_ready_marker(gw: Path, monkeypatch):
    env_file = gw / ".env.ins_a"
    env_file.write_text(_env("ins_a", 4101, "LANE_POOL=ins\n"))
    for k in ("LLM_ATTRIBUTION_CONFIG", "LLM_GATEWAY_LOG_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {"LANE_POOL": "ins", "LLM_GATEWAY_NAME": "ins_a", "LITELLM_MASTER_KEY": "sk-lane-a",
                 "LITELLM_PORT": "4101", "LANE_REGISTRY_DIR": str(gw / "run/attribution"),
                 "LLM_GATEWAY_ENV_FILE": str(env_file)}.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(gw))
    for mod in [m for m in sys.modules if m == "callbacks" or m.startswith("callbacks.")]:
        monkeypatch.delitem(sys.modules, mod)
    try:
        attribution = importlib.import_module("callbacks.attribution")
    finally:
        sys.path.remove(str(gw))
    assert attribution.lane_mode() == "env"
    cfg = attribution.load_config()
    assert cfg["mode"] == "env" and list(cfg["lanes"]) == ["ins_a"]
    registry = gw / "run/attribution"
    registry.mkdir(parents=True)
    (registry / "ins_a.json").write_text(json.dumps({"lane_id": "ins_a", "state": "active", "case_id": "C1",
                                                     "execution_id": "e1", "run_id": "r", "owner_token": "t"}))
    kw = {"litellm_call_id": "x", "metadata": {"user_api_key_hash": hashlib.sha256(b"sk-lane-a").hexdigest()},
          "litellm_params": {}, "optional_params": {}}
    got = attribution.resolve(kw, cfg)
    assert got["attribution_status"] == "attributed" and got["execution_id"] == "e1" and got["lane_id"] == "ins_a"
    kw["metadata"]["user_api_key_hash"] = "other"
    assert attribution.resolve(kw, cfg)["attribution_status"] == "unmapped"
    attribution.mark_gateway_ready()
    marker = json.loads((registry / "gateway_ready.port-4101.json").read_text())
    assert marker["mode"] == "env" and marker["gateway"] == "ins_a" and marker["lane_pool"] == "ins"
    assert marker["config_path"] == str(env_file.resolve())
    assert marker["config_sha256"] == hashlib.sha256(env_file.read_bytes()).hexdigest()
    # Legacy json config wins when both are present.
    monkeypatch.setenv("LLM_ATTRIBUTION_CONFIG", str(gw / "missing.json"))
    assert attribution.lane_mode() == "json"
    monkeypatch.delenv("LLM_ATTRIBUTION_CONFIG")
    monkeypatch.delenv("LLM_GATEWAY_NAME")
    with pytest.raises(ValueError, match="LLM_GATEWAY_NAME"):
        attribution.load_config()


def test_up_status_down_with_stub_proxy(gw: Path, monkeypatch):
    bin_dir = gw / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "litellm").write_text(STUB.format(python=sys.executable))
    (bin_dir / "litellm").chmod(0o755)
    port = _free_port()
    (gw / ".env.ins_a").write_text(_env("ins_a", port, "LANE_POOL=ins\nLANE_TARGET_URL=http://127.0.0.1:1/v1/chat\n"))
    monkeypatch.setenv("LLM_ATTRIBUTION_CONFIG", "/should/be/stripped.json")
    g = gatewayctl.load("ins_a", gw)
    try:
        st = gatewayctl.up(g, wait=20)
        assert st["running"] and st["healthy"] and st["ready"] is True
        assert gatewayctl._state_label(gatewayctl.status(g)) == "up"
        pid = st["pid"]
        again = gatewayctl.up(g, wait=20)  # idempotent
        assert again["pid"] == pid
        # Editing the env file marks the running gateway stale; up restarts it.
        g.env_file.write_text(g.env_file.read_text() + "# edited\n")
        g = gatewayctl.load("ins_a", gw)
        assert "config-changed" in gatewayctl._state_label(gatewayctl.status(g))
        restarted = gatewayctl.up(g, wait=20)
        assert restarted["pid"] != pid and restarted["ready"] is True
        # A second gateway on the same port refuses to steal it without --force.
        (gw / ".env.intruder").write_text(_env("intruder", port))
        intruder = gatewayctl.load("intruder", gw)
        with pytest.raises(gatewayctl.GatewayError, match="port shared"):
            gatewayctl.up(intruder, wait=5)
    finally:
        gatewayctl.down(g)
    assert not gatewayctl.status(g)["running"]
    assert not g.pid_file.exists()


def test_alternative_profiles_share_a_port(gw: Path):
    """Primary-relay profiles (.env, .env.<alt>) share one port: one runs, switching is explicit."""
    bin_dir = gw / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "litellm").write_text(STUB.format(python=sys.executable))
    (bin_dir / "litellm").chmod(0o755)
    port = _free_port()
    (gw / ".env").write_text(_env("default", port))
    (gw / ".env.alt").write_text(_env("alt", port))
    default, alt = gatewayctl.load("default", gw), gatewayctl.load("alt", gw)
    try:
        gatewayctl.up(default, wait=20)
        assert (gw / "run/active_profile").read_text().strip() == "default"
        st = gatewayctl.status(alt)
        assert st["port_owner"] == "default" and not st["external"]
        assert gatewayctl._state_label(st) == f"down(:{port} used by default)"
        with pytest.raises(gatewayctl.GatewayError, match="used by gateway default"):
            gatewayctl.up(alt, wait=5)
        switched = gatewayctl.up(alt, force=True, wait=20)
        assert switched["running"] and not gatewayctl.status(default)["running"]
        assert not default.pid_file.exists()
        assert (gw / "run/active_env_file").read_text().strip() == str(alt.env_file)
    finally:
        gatewayctl.down(alt)
        gatewayctl.down(default)


def test_external_project_owns_profiles_and_logs(tmp_path: Path, gw: Path, monkeypatch):
    """A consumer can own configuration without copying runtime or callbacks."""
    monkeypatch.setattr(gatewayctl, 'GATEWAY_DIR', gw)
    binary = gw / '.venv/bin/litellm'
    binary.parent.mkdir(parents=True)
    binary.write_text(STUB.format(python=sys.executable))
    binary.chmod(0o755)
    project = tmp_path / 'verifier/.runtime/tracing'
    project.mkdir(parents=True)
    (project / 'config.yaml').write_text('{}\n')
    (project / '.env.verifier').write_text(_env('verifier', _free_port()))
    g = gatewayctl.load('verifier', project)
    assert gatewayctl.litellm_bin(g) == str(binary)
    paths = gatewayctl.child_env(g, {})['PYTHONPATH'].split(':')
    assert paths == [str(gw), str(project)]
    try:
        state = gatewayctl.up(g, wait=20)
        assert state['healthy'] and state['running']
        assert g.log_dir.is_relative_to(project)
        assert g.pid_file.is_relative_to(project)
        assert not (project / 'callbacks').exists()
    finally:
        gatewayctl.down(g)
