"""同步失败必须「关门」:上游引入无法解析的名字时,受管树一字不动、退出码非 0。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_unresolvable_upstream_symbol_leaves_tree_untouched(tmp_path: Path) -> None:
    fake_up = tmp_path / "upstream"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "models")
    for d in ("app", "tool_adapter_sdk", "capabilities"):
        shutil.copytree(ROOT / d, fake_up / d, ignore=ignore)
    mr = fake_up / "app" / "runtime" / "model_router.py"
    mr.write_text("class HarnessConf:\n    version: str\n    brand_new: SomethingNotImported = None\n", encoding="utf-8")
    # Attach the malformed contract to a real closure seed. The failure must
    # happen before schema export or a backend probe can run.
    seed = fake_up / "app" / "runtime" / "backend_provider.py"
    seed.write_text(seed.read_text(encoding="utf-8") + "\nfrom app.runtime.model_router import HarnessConf\n", encoding="utf-8")

    managed = tmp_path / "mcp"
    for d in ("app", "tool_adapter_sdk", "capabilities", "itools"):
        shutil.copytree(ROOT / d, managed / d, ignore=shutil.ignore_patterns("__pycache__"))
    before = {p.relative_to(managed).as_posix(): p.read_bytes() for p in managed.rglob("*") if p.is_file()}

    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_from_upstream.py"),
                        "--source", str(fake_up), "--root", str(managed), "--python", sys.executable],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    assert "SomethingNotImported" in r.stderr
    after = {p.relative_to(managed).as_posix(): p.read_bytes() for p in managed.rglob("*") if p.is_file()}
    assert after == before
    assert not list(managed.glob(".sync_staging_*"))
    assert not (managed / "SYNC_STATE.json").exists()
