from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from eval_harness import agent_env
from eval_harness.agent_env import (
    AgentEnvSecretError, AgentWorkdirError, assert_no_upstream_secrets, build_agent_env,
    load_local_gateway_keys, load_upstream_secrets, prepare_case_workdir, resolve_agent_workdir_root,
    cleanup_case_workdir,
)

FAKE_UP = "sk-upstreamFAKE0123456789abcdef"
FAKE_LOCAL = "sk-local-gateway-fake-1"


@pytest.fixture
def gw(tmp_path, monkeypatch):
    d = tmp_path / "llm_gateway"
    d.mkdir()
    (d / ".env").write_text(
        f"UPSTREAM_API_KEY={FAKE_UP}\nUPSTREAM_PROTOCOL=anthropic   # comment\n"
        f"LITELLM_MASTER_KEY={FAKE_LOCAL}\nINSURANCE_LITELLM_MASTER_KEY=sk-ins-fake-local-22\n"
    )
    (d / ".env.bak_x").write_text('UPSTREAM_API_KEY="sk-oldbackupFAKE9876543210"\n')
    (d / ".env.example").write_text("UPSTREAM_API_KEY=replace-with-your-key\n")
    monkeypatch.setattr(agent_env, "GATEWAY_DIR", d)
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)
    return d


PARENT = {
    "PATH": "/usr/bin", "HOME": "/Users/x", "USER": "x", "LANG": "en_US.UTF-8", "LC_CTYPE": "UTF-8",
    "TMPDIR": "/tmp", "TERM": "xterm", "SHELL": "/bin/zsh",
    "UPSTREAM_API_KEY": FAKE_UP, "ANTHROPIC_API_KEY": FAKE_UP, "OPENAI_API_KEY": "sk-openaiFAKE000000000000",
    "PYTHONPATH": "src", "SSH_AUTH_SOCK": "/tmp/ssh", "HTTPS_PROXY": "http://127.0.0.1:7890",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "1", "DISABLE_AUTOUPDATER": "1", "PI_PACKAGE_DIR": "/p",
}


def test_secrets_loaded_from_env_files_excluding_local_and_placeholder(gw):
    s = load_upstream_secrets()
    assert FAKE_UP in s and "sk-oldbackupFAKE9876543210" in s
    assert FAKE_LOCAL not in s and "replace-with-your-key" not in s
    assert load_local_gateway_keys(environ={})["LITELLM_MASTER_KEY"] == FAKE_LOCAL


def test_claude_allowlist(gw):
    env = build_agent_env("claude", parent=PARENT)
    assert set(env) == {
        "PATH", "HOME", "USER", "LANG", "LC_CTYPE", "TMPDIR", "TERM", "SHELL",
        "CLAUDE_CODE_ATTRIBUTION_HEADER", "DISABLE_AUTOUPDATER", "NO_PROXY", "no_proxy",
    }
    assert "127.0.0.1" in env["NO_PROXY"] and "localhost" in env["no_proxy"]
    assert_no_upstream_secrets(env)


def test_pi_allowlist_and_injection_only_local_key(gw, monkeypatch):
    from eval_harness.adapters import pi
    monkeypatch.setattr(pi, "DEFAULT_GATEWAY_ENV", gw / ".env")
    env = pi.case_id_injection_env(
        "PI_A14", build_agent_env("pi", parent=PARENT), execution_id="e1",
        local_key_names=("LITELLM_MASTER_KEY",),
    )
    assert env["LITELLM_MASTER_KEY"] == FAKE_LOCAL
    assert "INSURANCE_LITELLM_MASTER_KEY" not in env
    for bad in ("UPSTREAM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PYTHONPATH", "SSH_AUTH_SOCK",
                "HTTPS_PROXY", "UPSTREAM_PROTOCOL"):
        assert bad not in env
    assert env["PI_PACKAGE_DIR"] == "/p" and env["PI_EVAL_CASE_ID"] == "PI_A14"
    assert FAKE_UP not in "".join(env.values())
    assert_no_upstream_secrets(env)


def test_proxy_opt_in(gw):
    env = build_agent_env("pi", parent={**PARENT, "EVAL_AGENT_PASS_PROXY": "1"})
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_guard_raises_on_secret_value_under_any_name(gw):
    env = build_agent_env("claude", parent=PARENT, extra={"SOME_INNOCENT_NAME": FAKE_UP})
    with pytest.raises(AgentEnvSecretError) as ei:
        assert_no_upstream_secrets(env)
    assert "SOME_INNOCENT_NAME" in str(ei.value) and FAKE_UP not in str(ei.value)
    with pytest.raises(AgentEnvSecretError):
        assert_no_upstream_secrets({"X": "Bearer sk-oldbackupFAKE9876543210"})


def test_workdir_root_validation(gw, tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_AGENT_WORKDIR_ROOT", raising=False)
    with pytest.raises(AgentWorkdirError):
        resolve_agent_workdir_root(str(agent_env.PROJECT_ROOT / "tmp_sandbox"))
    with pytest.raises(AgentWorkdirError):
        resolve_agent_workdir_root(str(gw / "sub"))  # ancestor has .env files
    with pytest.raises(AgentWorkdirError):
        resolve_agent_workdir_root("relative/dir")
    ok = tmp_path / "sbx"
    assert resolve_agent_workdir_root(str(ok)) == Path(str(ok)).resolve()
    monkeypatch.setenv("EVAL_AGENT_WORKDIR_ROOT", str(agent_env.PROJECT_ROOT))
    with pytest.raises(AgentWorkdirError):
        resolve_agent_workdir_root(str(ok))  # env override wins and is validated


def test_prepare_and_cleanup_case_workdir(gw, tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_AGENT_WORKDIR_ROOT", raising=False)
    tpl = tmp_path / "tpl"
    (tpl / ".claude").mkdir(parents=True)
    (tpl / "agent.md").write_text("hi")
    (tpl / ".mcp.json").write_text("{}")
    (tpl / ".claude" / "settings.local.json").write_text('{"env":{"ANTHROPIC_AUTH_TOKEN":"%s"}}' % FAKE_LOCAL)
    (tpl / "secret.txt").write_text(FAKE_UP)
    root = tmp_path / "sbx"
    wd = prepare_case_workdir(harness="claude", execution_id="abc", template_dir=tpl,
                              files=["agent.md", ".mcp.json", ".claude/settings.local.json"], root=str(root))
    assert wd == root.resolve() / "claude" / "abc"
    assert sorted(str(p.relative_to(wd)) for p in wd.rglob("*") if p.is_file()) == [
        ".claude/settings.local.json", ".mcp.json", "agent.md"]
    with pytest.raises(AgentEnvSecretError):
        prepare_case_workdir(harness="claude", execution_id="bad", template_dir=tpl,
                             files=["secret.txt"], root=str(root))
    assert not (root / "claude" / "bad").exists()
    assert cleanup_case_workdir(wd, root.resolve()) and not wd.exists()
