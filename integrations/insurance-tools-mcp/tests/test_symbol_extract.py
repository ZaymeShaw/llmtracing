"""按符号抽取器:必须把依赖定义与 typing/pydantic 等 import 一起带上;解析不了就失败。"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from symbol_extract import ExtractionError, extract_symbols  # noqa: E402

FIXTURE = textwrap.dedent('''
    """fixture upstream module."""
    from __future__ import annotations

    import os
    from typing import TYPE_CHECKING, Literal, Optional
    from pydantic import BaseModel, ConfigDict, Field

    from agno.models.openai import OpenAIChat

    if TYPE_CHECKING:
        from app.secret import OnlyForTyping

    DEFAULT_MODE = "fast"
    _LIMIT: int = 3


    def _helper(x: int) -> int:
        return x * _LIMIT


    class Inner(BaseModel):
        model_config = ConfigDict(frozen=True)

        mode: Literal["fast", "slow"] = DEFAULT_MODE
        note: Optional[str] = None


    # comment kept with Conf
    class Conf(BaseModel):
        inner: Inner = Field(default_factory=Inner)
        size: int = Field(default=1, gt=0)

        def doubled(self) -> "Conf":
            return Conf(inner=self.inner, size=_helper(self.size))


    def model_factory() -> OpenAIChat:
        return OpenAIChat(id=os.environ["X"])


    Conf.model_rebuild()
''')


def _load(tmp_path: Path, text: str, name: str):
    path = tmp_path / f"{name}.py"
    path.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_extracts_dependencies_and_imports(tmp_path: Path) -> None:
    ext = extract_symbols(FIXTURE, ["Conf"], filename="fixture.py", forbidden_modules=["agno"])
    assert ext.symbols == ["DEFAULT_MODE", "_LIMIT", "_helper", "Inner", "Conf"]
    assert "from typing import Literal, Optional" in ext.imports
    assert "from pydantic import BaseModel, ConfigDict, Field" in ext.imports
    assert ext.text.splitlines()[0] == "from __future__ import annotations"
    assert "agno" not in ext.text and "import os" not in ext.text
    assert "TYPE_CHECKING" not in ext.text
    assert "# comment kept with Conf" in ext.text
    assert ext.trailing_statements == ["Conf.model_rebuild()"]
    mod = _load(tmp_path, ext.text, "extracted_fixture_ok")
    conf = mod.Conf()
    assert conf.inner.mode == "fast" and conf.doubled().size == 3
    with pytest.raises(Exception):
        mod.Inner(mode="nope")


def test_old_line_range_style_bug_is_caught(tmp_path: Path) -> None:
    """复现 16:17 事故:没带 `from typing import Literal` 的摘录,pydantic 构建失败。"""
    ext = extract_symbols(FIXTURE, ["Inner"], filename="fixture.py")
    broken = ext.text.replace("from typing import Literal, Optional", "from typing import Optional")
    mod = _load(tmp_path, broken, "extracted_fixture_broken")
    with pytest.raises(Exception, match="not fully defined|Literal"):
        mod.Inner()


def test_unresolved_name_fails() -> None:
    src = FIXTURE + "\n\nclass Bad(BaseModel):\n    x: MissingType = 1\n"
    with pytest.raises(ExtractionError, match="MissingType"):
        extract_symbols(src, ["Bad"], filename="fixture.py")


def test_unresolved_name_in_method_body_fails() -> None:
    src = FIXTURE + "\n\ndef uses_missing():\n    return not_defined_anywhere + 1\n"
    with pytest.raises(ExtractionError, match="not_defined_anywhere"):
        extract_symbols(src, ["uses_missing"], filename="fixture.py")


def test_forbidden_module_fails() -> None:
    with pytest.raises(ExtractionError, match="agno"):
        extract_symbols(FIXTURE, ["model_factory"], filename="fixture.py", forbidden_modules=["agno"])


def test_type_checking_only_name_fails() -> None:
    src = FIXTURE + "\n\nclass UsesTyping(BaseModel):\n    x: OnlyForTyping\n"
    with pytest.raises(ExtractionError, match="TYPE_CHECKING"):
        extract_symbols(src, ["UsesTyping"], filename="fixture.py")


def test_missing_root_fails() -> None:
    with pytest.raises(ExtractionError, match="NoSuchThing"):
        extract_symbols(FIXTURE, ["NoSuchThing"], filename="fixture.py")


def test_locals_and_comprehensions_are_not_globals() -> None:
    src = textwrap.dedent('''
        def f(items):
            total = 0
            for i in items:
                total += i
            return [j * 2 for j in range(total)], (lambda k: k)(1)
    ''')
    ext = extract_symbols(src, ["f"], filename="m.py")
    assert ext.symbols == ["f"] and ext.imports == []


def test_model_router_shape_extracts_cleanly() -> None:
    """固定上游结构:闭包保留 Literal,排除模型工厂,不依赖外部工作树版本。"""
    source = textwrap.dedent('''
        from typing import Literal
        from pydantic import BaseModel
        from agno.agent import Agent

        class FailureRecoveryConf(BaseModel):
            mode: Literal["retry", "stop"] = "stop"

        class HarnessConf(BaseModel):
            version: str
            recovery: FailureRecoveryConf

        def build_agent() -> Agent:
            return Agent()
    ''')
    ext = extract_symbols(source, ["HarnessConf"],
                          filename="model_router.py", forbidden_modules=["agno"])
    assert "HarnessConf" in ext.symbols and "FailureRecoveryConf" in ext.symbols
    assert any("Literal" in line for line in ext.imports)
    assert "agno" not in ext.text
