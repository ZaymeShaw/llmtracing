"""字段字典(core 纯函数 · §10.2 FIELD_DICT · 模型不产 ID,P5)。

口语字段词 → 字段码的确定性归一(Gate/工具/Workflow 三方共享单点);
别名歧义在构造期 fail 显式——一别名映射多码属于词典数据错误,G-5 不放行多候选。
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class FieldDictError(ValueError):
    """字段字典数据违例(别名歧义)——fail 显式,不静默取先者。"""


@dataclass(frozen=True, init=False)
class FieldDict:
    """字段码 ↔ 口语别名;normalize 大小写与首尾空白归一。"""

    version: str
    _fields: Mapping[str, tuple[str, ...]]
    _by_alias: Mapping[str, str]

    def __init__(self, fields: dict[str, list[str]], *, version: str = "unversioned"):
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "_fields", MappingProxyType({code: tuple(aliases) for code, aliases in fields.items()}))
        by_alias: dict[str, str] = {}
        for code, aliases in self._fields.items():
            for alias in aliases:
                norm = alias.strip().lower()
                if norm in by_alias and by_alias[norm] != code:
                    raise FieldDictError(
                        f"字段别名「{alias}」同时映射 {by_alias[norm]} 与 {code}——歧义须在词典侧消解"
                    )
                by_alias[norm] = code
        object.__setattr__(self, "_by_alias", MappingProxyType(by_alias))

    def __deepcopy__(self, memo):
        return self  # All owned values are immutable.

    def normalize(self, spoken: str) -> str | None:
        """口语字段词 → 字段码;字段码自身恒等映射(Gate 已归一的实参复用同一单点);
        未命中返回 None(交上层解析/Agent,不猜)。"""
        key = spoken.strip().lower()
        if key in self._fields:
            return key
        return self._by_alias.get(key)

    def alias_items(self) -> list[tuple[str, str]]:
        """(别名, 字段码) 全集——供 resolve 的最长匹配扫描。"""
        return list(self._by_alias.items())

    def codes(self) -> frozenset[str]:
        return frozenset(self._fields)

    def aliases(self, code: str) -> list[str]:
        return list(self._fields.get(code, []))
