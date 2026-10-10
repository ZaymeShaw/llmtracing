"""实体归属词典(core 纯函数 · 名称/编码双通道 · Owner 2026-09-13 设计对齐)。

用途:基座模型分不清 query 中的名称是寿险产品还是服务/权益时,entity_lookup 工具
用本词典做毫秒级确定性确认(存疑必查,意图明确不查)。词典由全量数据生成
(datas/平安寿险产品信息.json × datas/平安寿险服务&权益信息.json),产物版本化
提交为 conf/entity_dict/entity_dict.json——判据是数据不是代码,更新数据后重新生成。

双通道纪律(Owner 2026-09-13):
- 名称通道(默认):全称/简称/别称/名称/关键词,三级匹配(exact → 包含 → 未收录)。
  纯数字词条一律不进名称通道(与保单尾号/手机号碰撞会误召回)。
- 编码通道(显式 by="code"):险种代码/权益编码/服务编码 + 纯数字别称,仅 exact
  全串匹配——传参即意图声明,数字永不模糊。
- 跨域词(如「安医保星耀版」两侧同名)不消歧,如实返回双归属交上层仲裁。
"""
from __future__ import annotations

from functools import cached_property

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENTITY_DICT_VERSION = "entity-dict-v1"

# 归一化:全半角折叠 + 大小写 + 去空白/连字符/中点(「安有医 易核26版」=「安有医-易核26版」)。
_STRIP_RE = re.compile(r"[\s\-－—·・_]")
# 服务数据已知脏词(平安居家1+N 的关键词「产品1/产品N/1产品/N产品」):显式黑名单
# 留痕,不静默。登记归一化形态(小写),与词条归一化同口径比较。
_JUNK_TERMS = frozenset({"产品1", "产品n", "1产品", "n产品"})

MAX_HITS = 8          # 直接命中实体上限(超出截断并在 gaps 披露)
MAX_CONTAINS = 8      # 包含级候选词条上限
MAX_RELATED = 10      # 关联命中(系列成员)上限
MIN_TERM_LEN = 2      # 名称词条最小长度(归一化后)


class EntityDictError(ValueError):
    """实体词典数据违例——fail 显式,不静默取先者。"""


def normalize_name(term: str) -> str:
    """名称通道归一化:构建与查询共用同一单点(漂移 = 召回不一致)。"""
    folded = unicodedata.normalize("NFKC", term).casefold()
    return _STRIP_RE.sub("", folded)


def normalize_code(code: str) -> str:
    """编码通道归一化:大写语义由 casefold 统一,分隔符(含下划线)一并剥除。"""
    return normalize_name(code)


def _iter_alias_values(raw: str) -> list[str]:
    """产品别称字段按顿号/逗号等分隔符拆分(实测两种分隔符并存,共 ~750 处)。"""
    return [part.strip() for part in re.split(r"[、，,;；/]", raw or "") if part.strip()]


def _file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def build_entity_payload(
    products_path: Path,
    services_path: Path,
    *,
    version: str = ENTITY_DICT_VERSION,
) -> dict[str, Any]:
    """两份全量数据 → 双通道词典 payload(纯函数,构建脚本与防漂移测试共用)。

    确定性构建:不含时间戳,同输入必同输出(产物可直接做等值比对)。
    """
    products: list[dict[str, Any]] = json.loads(products_path.read_text(encoding="utf-8"))
    services: list[dict[str, Any]] = json.loads(services_path.read_text(encoding="utf-8"))

    entities: dict[str, dict[str, Any]] = {}
    name_terms: dict[str, set[str]] = {}
    code_terms: dict[str, set[str]] = {}
    excluded_numeric: set[str] = set()
    excluded_junk: set[str] = set()

    def register_name(entity_id: str, term: str) -> None:
        normalized = normalize_name(term)
        if not normalized or normalized in _JUNK_TERMS:
            if normalized in _JUNK_TERMS:
                excluded_junk.add(term)
            return
        if len(normalized) < MIN_TERM_LEN:
            return
        if normalized.isdigit():
            # 纯数字词条移入编码通道(「704」「2612」类内部代号),防误召回。
            excluded_numeric.add(term)
            code_terms.setdefault(normalized, set()).add(entity_id)
            return
        name_terms.setdefault(normalized, set()).add(entity_id)

    def register_code(entity_id: str, code: str) -> None:
        normalized = normalize_code(code)
        if not normalized:
            return
        code_terms.setdefault(normalized, set()).add(entity_id)

    # —— 产品侧:全称去重合并(1575 → 1534,31 组重名),简称/别称为名称词条,
    #    险种代码为唯一键;非数字险种代码(「富贵竹」类)兼作名称词条。 ——
    for product in products:
        full_name = (product.get("产品全称") or "").strip()
        if not full_name:
            continue
        entity_id = f"product:{full_name}"
        record = entities.setdefault(entity_id, {
            "id": entity_id, "type": "product", "name": full_name,
            "aliases": [], "codes": [], "sales_status": "", "period": [],
        })
        short = (product.get("产品标准简称") or "").strip()
        aliases = ([short] if short else []) + _iter_alias_values(product.get("产品别称") or "")
        for alias in aliases:
            if alias not in record["aliases"]:
                record["aliases"].append(alias)
        code = (product.get("险种代码") or "").strip()
        if code and code not in record["codes"]:
            record["codes"].append(code)
        status = (product.get("销售状态") or "").strip()
        if status:
            record["sales_status"] = status
        start = (product.get("开始时间") or "").strip()
        end = (product.get("结束时间") or "").strip()
        if start or end:
            record["period"] = [start, end]
        register_name(entity_id, full_name)
        for alias in aliases:
            register_name(entity_id, alias)
        if code:
            register_code(entity_id, code)
            if not code.isdigit():
                register_name(entity_id, code)

    # —— 服务/权益侧:名称/关键词为名称词条(直接命中);所属服务只进系列索引
    #    (关联命中),避免「御享国医」直接命中 21 个实体把主次压平。 ——
    family_terms: dict[str, set[str]] = {}
    for entry in services:
        category = (entry.get("类别") or "").strip()
        names = [str(n).strip() for n in entry.get("名称") or [] if str(n).strip()]
        if not names or category not in {"服务", "权益"}:
            continue
        codes = [str(c).strip() for c in entry.get("编码") or [] if str(c).strip()]
        families = [str(f).strip() for f in entry.get("所属服务") or [] if str(f).strip()]
        keywords = [str(k).strip() for k in entry.get("关键词") or [] if str(k).strip()]
        primary = names[0]
        entity_id = f"{category}:{primary}" if not codes else f"{category}:{codes[-1]}"
        if entity_id in entities:
            entity_id = f"{entity_id}#{len(entities)}"
        # 别名同样过滤脏词(展示层不再出现「产品1」类内部占位)。
        display_aliases = [
            alias for alias in names[1:] + keywords
            if alias not in names and normalize_name(alias) not in _JUNK_TERMS
        ]
        entities[entity_id] = {
            "id": entity_id, "type": "service" if category == "服务" else "benefit",
            "name": primary, "aliases": display_aliases,
            "codes": codes, "families": families,
        }
        for term in names + keywords:
            register_name(entity_id, term)
        for code in codes:
            register_code(entity_id, code)
        for family in families:
            normalized = normalize_name(family)
            if len(normalized) >= MIN_TERM_LEN:
                family_terms.setdefault(normalized, set()).add(entity_id)

    payload = {
        "version": version,
        "sources": {
            "products": {
                "file": products_path.name,
                "sha256": _file_fingerprint(products_path),
                "records": len(products),
            },
            "services": {
                "file": services_path.name,
                "sha256": _file_fingerprint(services_path),
                "records": len(services),
            },
        },
        "counts": {
            "entities": len(entities),
            "products": sum(1 for e in entities.values() if e["type"] == "product"),
            "services": sum(1 for e in entities.values() if e["type"] == "service"),
            "benefits": sum(1 for e in entities.values() if e["type"] == "benefit"),
            "name_terms": len(name_terms),
            "code_terms": len(code_terms),
        },
        "exclusions": {
            "pure_numeric_name_terms": sorted(excluded_numeric),
            "junk_terms": sorted(excluded_junk),
        },
        "entities": [entities[key] for key in sorted(entities)],
        "name_terms": {term: sorted(ids) for term, ids in sorted(name_terms.items())},
        "family_terms": {term: sorted(ids) for term, ids in sorted(family_terms.items())},
        "code_terms": {term: sorted(ids) for term, ids in sorted(code_terms.items())},
    }
    return payload


@dataclass(frozen=True)
class EntityHit:
    """直接命中的实体(信封 hits 元素的数据来源)。"""

    entity_id: str
    type: str  # product | service | benefit
    name: str
    aliases: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()
    sales_status: str = ""
    period: tuple[str, ...] = ()
    families: tuple[str, ...] = ()


@dataclass(frozen=True)
class NameLookup:
    """名称通道查询结果:tier=exact(直接命中)/contains(包含级候选)/空串(未收录)。"""

    tier: str
    hits: tuple[EntityHit, ...] = ()
    contains_candidates: tuple[str, ...] = ()
    related_family: str = ""
    related_members: tuple[str, ...] = ()
    truncated: bool = False


class EntityDict:
    """双通道实体归属词典(conf/entity_dict/entity_dict.json 的查询单点)。"""

    def __init__(self, payload: dict[str, Any], *, version: str = "unversioned"):
        self.version = payload.get("version") or version
        self.counts: dict[str, int] = dict(payload.get("counts") or {})
        entities_raw = payload.get("entities") or []
        self._entities: dict[str, EntityHit] = {}
        for record in entities_raw:
            entity_id = str(record.get("id") or "")
            if not entity_id:
                raise EntityDictError("实体记录缺少 id")
            if entity_id in self._entities:
                raise EntityDictError(f"实体 id 重复:{entity_id}")
            self._entities[entity_id] = EntityHit(
                entity_id=entity_id,
                type=str(record.get("type") or ""),
                name=str(record.get("name") or ""),
                aliases=tuple(record.get("aliases") or []),
                codes=tuple(record.get("codes") or []),
                sales_status=str(record.get("sales_status") or ""),
                period=tuple(record.get("period") or []),
                families=tuple(record.get("families") or []),
            )
        self._name_terms = self._index(payload.get("name_terms"))
        self._family_terms = self._index(payload.get("family_terms"))
        self._code_terms = self._index(payload.get("code_terms"))
        self._product_terms = tuple(
            term for term, ids in {**self._name_terms, **self._family_terms}.items()
            if len(term) >= MIN_TERM_LEN and not term.isdigit()
            and any(self._entities[entity_id].type == "product" for entity_id in ids)
        )
        self.exclusions: dict[str, list[str]] = dict(payload.get("exclusions") or {})

    def _index(self, raw: dict[str, Any] | None) -> dict[str, tuple[str, ...]]:
        index: dict[str, tuple[str, ...]] = {}
        for term, ids in (raw or {}).items():
            normalized_ids = [str(entity_id) for entity_id in ids]
            known = tuple(i for i in normalized_ids if i in self._entities)
            unknown = sorted(set(normalized_ids) - set(known))
            if unknown:
                raise EntityDictError(f"词典词条「{term}」指向未登记实体:{unknown[:3]}")
            if known:
                index[str(term)] = known
        return index

    def _hits(self, ids: tuple[str, ...]) -> tuple[EntityHit, ...]:
        return tuple(self._entities[entity_id] for entity_id in ids)

    def lookup_name(self, term: str) -> NameLookup | None:
        """名称通道三级匹配:归一化 exact → 包含级候选 → None(未收录,不猜)。

        纯数字输入一律 None(名称通道不服务数字——包含级扫描会捞出「少儿9704」
        类数字噪声,数字只能走编码通道显式 exact;Owner 2026-09-13 防误召回纪律)。
        """
        normalized = normalize_name(term)
        if len(normalized) < MIN_TERM_LEN or normalized.isdigit():
            return None
        direct = self._name_terms.get(normalized)
        if direct is not None:
            members = self._family_terms.get(normalized, ())
            related_members = tuple(
                self._entities[entity_id].name for entity_id in members[:MAX_RELATED]
            )
            hits = self._hits(direct)
            return NameLookup(
                tier="exact",
                hits=hits[:MAX_HITS],
                related_family=normalized if members else "",
                related_members=related_members,
                truncated=len(hits) > MAX_HITS,
            )
        if normalized in self._family_terms:
            # 系列名未登记为直接词条(理论少见):按关联层返回,主实体缺位如实呈现。
            members = self._family_terms[normalized]
            related_members = tuple(
                self._entities[entity_id].name for entity_id in members[:MAX_RELATED]
            )
            return NameLookup(
                tier="exact", hits=(), related_family=normalized,
                related_members=related_members,
                truncated=len(members) > MAX_RELATED,
            )
        candidates = sorted(
            (key for key in self._name_terms if normalized in key and len(key) > len(normalized)),
            key=len,
        )
        if not candidates:
            return None
        return NameLookup(tier="contains", contains_candidates=tuple(candidates[:MAX_CONTAINS]))

    def product_name_spans(self, text: str, *, max_matches: int | None = None) -> tuple[tuple[int, int], ...]:
        """Longest registered product-name spans, with offsets into the original text.

        The cached trie is built once per dictionary, never once per answer.
        Name normalization follows lookup_name; codes are intentionally excluded.
        """
        if max_matches is not None and max_matches <= 0:
            return ()
        normalized_chars = {char: normalize_name(char) for char in set(text)}
        normalized_text = text.translate({ord(char): value for char, value in normalized_chars.items()})
        # Composed sequences spanning code points cannot use this offset map.
        # Fall back to unavailable eligibility rather than report an incorrect span.
        if not unicodedata.is_normalized("NFKC", normalized_text):
            raise EntityDictError("名称跨字符归一化无法保留精确位置")
        prefixes = self._product_prefixes
        spans = []
        resume, length = 0, len(normalized_text)
        # Every registered name has at least two characters. Reject impossible
        # pairs before walking the trie, without one regex Match per repeated
        # first character (e.g. an 8,000-character run of "一").
        for start, pair in enumerate(zip(normalized_text, normalized_text[1:])):
            if start < resume:
                continue
            node = prefixes.get(pair)
            if node is None:
                continue
            end = start + 2
            found = end if '' in node else start
            while end < length and normalized_text[end] in node:
                node = node[normalized_text[end]]
                end += 1
                if '' in node:
                    found = end
            if found > start:
                spans.append((start, found))
                if max_matches is not None and len(spans) >= max_matches:
                    break
                resume = found
        if not spans:
            return ()
        # Most knowledge text has no product name; build offset mapping only on a hit.
        offsets = []
        for offset, char in enumerate(text):
            offsets.extend([offset] * len(normalized_chars[char]))
            if len(offsets) >= spans[-1][1]:
                break
        return tuple((offsets[start], offsets[end - 1] + 1) for start, end in spans)

    @cached_property
    def _product_prefixes(self) -> dict:
        return {(first, second): node for first, children in self._product_trie.items()
                for second, node in children.items() if second}

    @cached_property
    def _product_trie(self) -> dict:
        trie: dict = {}
        for term in self._product_terms:
            node = trie
            for char in term:
                node = node.setdefault(char, {})
            node[''] = {}
        return trie

    def mentions_product(self, text: str) -> bool:
        """Recognize registered product names in text; no fuzzy/code inference."""
        normalized = normalize_name(text)
        return any(term in normalized for term in self._product_terms)

    def lookup_code(self, code: str) -> tuple[EntityHit, ...]:
        """编码通道:仅 exact 全串匹配(数字永不模糊),未命中返回空元组。"""
        normalized = normalize_code(code)
        if not normalized:
            return ()
        return self._hits(self._code_terms.get(normalized, ()))

    def cross_domain(self, hits: tuple[EntityHit, ...]) -> bool:
        """命中同时跨产品与服务/权益两侧(如「安医保星耀版」)——交上层仲裁。"""
        types = {hit.type for hit in hits}
        return "product" in types and bool(types & {"service", "benefit"})
