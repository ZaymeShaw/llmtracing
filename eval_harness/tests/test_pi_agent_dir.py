"""Pi user-scope config isolation (profile adapter.pi_agent_dir)."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from eval_harness.adapters import pi as pia  # noqa: E402

MODELS_JSONC = """{
  "providers": {
    // local relay
    "local-relay-bailian": { // comment with // inside
      "baseUrl": "http://127.0.0.1:4002/v1",
      "api": "openai-completions",
      "apiKey": "$INSURANCE_LITELLM_MASTER_KEY",
      "headers": {"X-Eval-Case-Id": "$PI_EVAL_CASE_ID"},
      "models": [ { "id": "deepseek-v4-flash-0731" }, ],
    },
    "other": { "baseUrl": "https://x.example/v1", "apiKey": "sk-THISISLITERALSECRET123" },
  }
}
"""


def _src(tmp: Path, models: str = MODELS_JSONC) -> Path:
    src = tmp / "src_agent"
    (src / "extensions").mkdir(parents=True)
    (src / "extensions" / "user.ts").write_text("export default () => {}")
    (src / "APPEND_SYSTEM.md").write_text("user append")
    (src / "models.json").write_text(models)
    (src / "settings.json").write_text(json.dumps({
        "defaultProvider": "anyrouter", "theme": "light", "packages": ["npm:pi-mcp-adapter"],
        "retry": {"enabled": True, "maxRetries": 30},
    }))
    pkg = tmp / "pkg" / "pi-mcp-adapter"
    pkg.mkdir(parents=True)
    return src


def test_inherit_is_legacy(tmp_path):
    assert pia.resolve_pi_agent_dir(None, tmp_path, provider="local-relay-bailian") is None
    assert pia.resolve_pi_agent_dir("inherit", tmp_path, provider="local-relay-bailian") is None
    assert not (tmp_path / pia.PI_AGENT_SUBDIR).exists()


def test_per_case_writes_only_provider_and_package(tmp_path):
    src = _src(tmp_path)
    cwd = tmp_path / "case"
    cwd.mkdir()
    d = pia.resolve_pi_agent_dir("per_case", cwd, provider="local-relay-bailian",
                                 packages=[str(tmp_path / "pkg" / "pi-mcp-adapter")], source_dir=src)
    assert d == cwd / ".pi-agent"
    assert sorted(p.name for p in d.iterdir()) == ["models.json", "settings.json"]
    models = json.loads((d / "models.json").read_text())
    assert list(models["providers"]) == ["local-relay-bailian"]
    assert models["providers"]["local-relay-bailian"]["apiKey"] == "$INSURANCE_LITELLM_MASTER_KEY"
    assert "sk-THISISLITERALSECRET123" not in (d / "models.json").read_text()
    settings = json.loads((d / "settings.json").read_text())
    assert settings == {"retry": {"enabled": True, "maxRetries": 30},
                        "packages": [str((tmp_path / "pkg" / "pi-mcp-adapter").resolve())]}


def test_literal_key_refused(tmp_path):
    src = _src(tmp_path, MODELS_JSONC.replace("$INSURANCE_LITELLM_MASTER_KEY", "sk-literal-local-key-123"))
    with pytest.raises(pia.PiAgentDirError):
        pia.resolve_pi_agent_dir("per_case", tmp_path, provider="local-relay-bailian",
                                 packages=[str(tmp_path / "pkg" / "pi-mcp-adapter")], source_dir=src)


def test_missing_provider_or_package_fails(tmp_path):
    src = _src(tmp_path)
    with pytest.raises(pia.PiAgentDirError):
        pia.resolve_pi_agent_dir("per_case", tmp_path, provider="nope",
                                 packages=[str(tmp_path / "pkg" / "pi-mcp-adapter")], source_dir=src)
    with pytest.raises(pia.PiAgentDirError):
        pia.resolve_pi_agent_dir("per_case", tmp_path, provider="local-relay-bailian",
                                 packages=[str(tmp_path / "nope")], source_dir=src)


def test_argv_flags_only_when_isolated(tmp_path):
    kw = dict(pi_bin="pi", prompt="hi", agent_md_path=tmp_path / "agent.md", provider="p", model="m",
              thinking="off", no_builtin_tools=False, approve=True, session_id=None, append_system_prompt=True)
    legacy = pia.build_pi_argv(**kw)
    iso = pia.build_pi_argv(**kw, isolation_flags=list(pia.PI_ISOLATION_FLAGS))
    assert not any(f in legacy for f in pia.PI_ISOLATION_FLAGS)
    for f in ("--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files"):
        assert f in iso
    assert "--no-extensions" not in iso and "--no-builtin-tools" not in iso  # MCP + builtin tools stay
    assert iso[-1] == "hi"


def test_run_case_sets_agent_dir_env_and_saves_transcript(tmp_path, monkeypatch):
    """Fake pi binary: records env/argv, writes a session file into $PI_CODING_AGENT_DIR/sessions."""
    src = _src(tmp_path)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(src))
    fake = tmp_path / "fakepi"
    fake.write_text("""#!/usr/bin/env python3
import json, os, sys
d = os.environ.get("PI_CODING_AGENT_DIR", "")
rec = {"agent_dir": d, "argv": sys.argv[1:]}
open(os.environ["FAKE_REC"], "a").write(json.dumps(rec) + "\\n")
cwd = os.getcwd()
sd = os.path.join(d, "sessions", "--" + cwd.strip("/").replace("/", "-") + "--")
os.makedirs(sd, exist_ok=True)
sid = "01aaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
open(os.path.join(sd, "2026-01-01T00-00-00-000Z_" + sid + ".jsonl"), "a").write('{"type":"session"}\\n')
print(json.dumps({"type": "session", "id": sid, "cwd": cwd}))
print(json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}))
print(json.dumps({"type": "agent_end", "messages": []}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    (tpl / "agent.md").write_text("sys")
    (tpl / ".mcp.json").write_text("{}")
    rec = tmp_path / "rec.jsonl"
    monkeypatch.setenv("FAKE_REC", str(rec))
    monkeypatch.setattr(pia, "finalize_agent_env", lambda env: env)
    monkeypatch.setattr(pia, "build_agent_env", lambda h: {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                                           "FAKE_REC": str(rec), "PI_CODING_AGENT_DIR": str(src)})
    case_dir = tmp_path / "case"
    res = pia.run_case(case_id="PI_T01", turns=["q1", "q2"], case_dir=case_dir, project_cwd=tpl,
                       pi_bin=str(fake), provider="local-relay-bailian", model="m",
                       agent_workdir_root=str(tmp_path / "sandbox"), pi_agent_dir="per_case",
                       pi_packages=[str(tmp_path / "pkg" / "pi-mcp-adapter")])
    assert res.success, res.error
    rows = [json.loads(l) for l in rec.read_text().splitlines()]
    assert len(rows) == 2
    for r in rows:
        assert r["agent_dir"].endswith("/.pi-agent") and r["agent_dir"] != str(src)
        assert "--no-skills" in r["argv"] and "--no-context-files" in r["argv"]
    assert "--session" in rows[1]["argv"]
    assert (case_dir / "session_transcript.jsonl").read_text().count('"session"') == 2
    aw = json.loads((case_dir / "agent_workdir.json").read_text())
    assert aw["pi_agent_dir"].endswith("/.pi-agent")
    assert res.transcript_path is None  # Pi session format is not fed to Claude transcript parser
    assert not Path(aw["agent_cwd"]).exists()  # sandbox (incl. .pi-agent) removed
