"""H-02: deterministic knowledge categories and shared typed assertion fragments.

Absence of a restricted fragment is only a category candidate, never proof of relevance.
Vocabulary/quality limits belong to conf; H-11 reuses this extractor.
"""
from __future__ import annotations

import re
from typing import Literal, NamedTuple

from app.core.quantities import quantity_matches
from app.compat import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator, TypeAdapter


class KnowledgeCategory(StrEnum):
    GENERAL = "general_knowledge_excerpt"
    AMOUNT = "amount"
    ELIGIBILITY = "eligibility"
    LIABILITY = "liability"
    STATUS = "status"
    RESTRICTED = "restricted_assertion"
    UNKNOWN = "unknown"


class ExcerptPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quantitative_terms: list[str] = Field(default_factory=list)
    entity_terms: list[str] = Field(default_factory=list)
    assertion_terms: list[str] = Field(default_factory=list)
    # Deprecated input compatibility only; authoritative names come from entity_dict.
    proper_names: list[str] = Field(default_factory=list)
    topics: dict[str, list[str]] = Field(default_factory=dict)
    query_fillers: list[str] = Field(default_factory=list)
    max_scan_chars: int = Field(default=8000, ge=300, le=16000)
    max_entries: int = Field(default=64, ge=1, le=128)
    max_false_positive_rate: float = Field(default=0.05, ge=0, le=1)
    max_false_negative_rate: float = Field(default=0, ge=0, le=1)
    scan_p95_ms: float = Field(default=5, gt=0, le=50)


    @model_validator(mode="before")
    @classmethod
    def _retired_predicate_categories(cls, value):
        if isinstance(value, dict) and "predicate_categories" in value:
            TypeAdapter(dict[str, KnowledgeCategory]).validate_python(value["predicate_categories"])
            return {k: v for k, v in value.items() if k != "predicate_categories"}
        return value


FragmentKind = Literal["quantity", "assertion", "proper_name", "oversize"]
CITATION_KINDS: frozenset[FragmentKind] = frozenset({"quantity", "assertion", "oversize"})


class AssertionFragment(NamedTuple):
    """Internal immutable value; no external serialization/validation is required."""
    kind: FragmentKind
    text: str
    start: int
    end: int


_MAX_FRAGMENTS = 256
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")


def assertion_fragments(text: str, policy: ExcerptPolicy, *,
                        kinds: frozenset[FragmentKind] | None = None) -> tuple[AssertionFragment, ...]:
    """Bounded literal/regex scan; negated and conditional claims remain assertions."""
    if len(text) > policy.max_scan_chars:
        return (AssertionFragment(kind="oversize", text="", start=0, end=len(text)),)
    from app.core.citation_units import assertion_text
    text = assertion_text(text)
    selected = kinds if kinds is not None else frozenset({"quantity", "assertion", "proper_name", "oversize"})
    fragments: list[AssertionFragment] = []
    if "quantity" in selected:
        for m in quantity_matches(text):
            fragments.append(AssertionFragment("quantity", m.group(), m.start(), m.end()))
            if len(fragments) >= _MAX_FRAGMENTS:
                return (*fragments, AssertionFragment("oversize", "", 0, len(text)))
        for word in policy.quantitative_terms:
            if word and (start := text.find(word)) >= 0:
                fragments.append(AssertionFragment(kind="quantity", text=word, start=start, end=start + len(word)))
                if len(fragments) >= _MAX_FRAGMENTS:
                    return (*fragments, AssertionFragment("oversize", "", 0, len(text)))
    if "proper_name" in selected:
        from app.conf import entity_dict
        for start, end in entity_dict().product_name_spans(text, max_matches=_MAX_FRAGMENTS):
            fragments.append(AssertionFragment(kind="proper_name", text=text[start:end], start=start, end=end))
            if len(fragments) >= _MAX_FRAGMENTS:
                return (*fragments, AssertionFragment("oversize", "", 0, len(text)))
    if "assertion" not in selected:
        return tuple(fragments)
    # A literal absent from the entire input cannot occur in any sentence.
    # Filter once instead of repeating all absent-term searches per sentence.
    assertion_words = tuple(word for word in policy.assertion_terms if word and word in text)
    entity_words = tuple(word for word in policy.entity_terms if word and word in text)
    if not assertion_words or not entity_words:
        return tuple(fragments)
    for sentence in _SENTENCE.finditer(text):
        unit = sentence.group()
        if any(word in unit for word in entity_words):
            for word in assertion_words:
                # Qualifiers are assertions in business context, but reading labels
                # (须知 / 仅供查阅) do not state an eligibility or coverage condition.
                matches = re.finditer(re.escape(word), unit)
                for match in matches:
                    suffix = unit[match.start():]
                    if word == "须" and suffix.startswith("须知"):
                        continue
                    if word == "仅" and suffix.startswith(("仅供查阅", "仅供参考", "仅供浏览", "仅供查看", "仅供阅读")):
                        continue
                    start = match.start()
                    break
                else:
                    continue
                if start >= 0:
                    offset = sentence.start() + start
                    fragments.append(AssertionFragment("assertion", word, offset, offset + len(word)))
                    if len(fragments) >= _MAX_FRAGMENTS:
                        return (*fragments, AssertionFragment("oversize", "", 0, len(text)))
    return tuple(fragments)


def knowledge_category(predicate: str, text: str, policy: ExcerptPolicy) -> KnowledgeCategory:
    if predicate in {"qa_answer", "reference_content"}:
        try:
            return KnowledgeCategory.RESTRICTED if assertion_fragments(text, policy) else KnowledgeCategory.GENERAL
        except Exception:
            # Product vocabulary unavailable: no generic L2 eligibility, no service failure.
            return KnowledgeCategory.UNKNOWN
    return KnowledgeCategory.UNKNOWN


def matching_topics(text: str, policy: ExcerptPolicy) -> tuple[str, ...]:
    return tuple(topic for topic, words in policy.topics.items() if any(word in text for word in words))


def is_general_query(query: str, policy: ExcerptPolicy) -> bool:
    """Only fully recognized general queries qualify; unknown subject words fail closed."""
    remainder = query
    words = [*policy.query_fillers, *(word for words in policy.topics.values() for word in words)]
    for word in sorted(words, key=len, reverse=True):
        if word:
            remainder = remainder.replace(word, "")
    return not remainder.strip(" \t\n，。？！、,?!；;和与及")
