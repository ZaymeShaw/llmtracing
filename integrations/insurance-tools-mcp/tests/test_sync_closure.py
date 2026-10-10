"""闭包分析:按符号抽取的模块(model_router)里被保留的 import 也必须纳入闭包。

回归:上游给 HarnessConf 加了 `from app.core.context_overflow import ContextRecoveryConf`,
旧闭包不沿 model_router 的 import 走,候选树缺 app/core/context_overflow.py,import 验证失败。
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync_from_upstream as sfu  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text), encoding="utf-8")


def test_imports_kept_by_symbol_extraction_join_closure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for pkg in ("app", "app/core", "app/runtime", "app/tools"):
        _write(tmp_path, f"{pkg}/__init__.py", "")
    _write(tmp_path, "app/tools/seed.py", "from app.runtime.model_router import HarnessConf\n")
    _write(tmp_path, "app/runtime/model_router.py", """
        from pydantic import BaseModel
        from agno.agent import Agent
        from app.core.recovery import RecoveryConf
        from app.core.unused import Unused

        class HarnessConf(BaseModel):
            recovery: RecoveryConf

        def build() -> Agent:
            return Unused()
    """)
    _write(tmp_path, "app/core/recovery.py", "from app.core.limits import LIMIT\nclass RecoveryConf: ...\n")
    _write(tmp_path, "app/core/limits.py", "LIMIT = 3\n")
    _write(tmp_path, "app/core/unused.py", "class Unused: ...\n")
    monkeypatch.setattr(sfu, "SEEDS", ["app.tools.seed"])

    closure, wanted = sfu.import_closure(tmp_path)

    assert wanted == {"app.runtime.model_router": {"HarnessConf"}}
    # 抽取保留的 import 及其传递依赖进闭包
    assert {"app.core.recovery", "app.core.limits"} <= set(closure)
    # 只被非抽取部分(依赖 agno 的 build)用到的模块不进闭包
    assert "app.core.unused" not in closure
