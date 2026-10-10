"""Bounded, clause-local numeric tracing without semantic unit conversion."""

from __future__ import annotations

from app.core.quantities import (
    UNITS as _UNITS, MULTIPLIERS, decimal_value as _decimal, quantities as _quantities,
)

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal
from itertools import islice

from app.schemas import Fact

NumericStatus = Literal["supported", "unsupported", "unverifiable", "not_applicable"]


@dataclass(frozen=True)
class NumericValue:
    value: Decimal
    unit: str
    subject: str
    predicate: str


def _mention_pattern(names, *, subjects=False):
    # Lookahead retains overlapping positions. Prefix tables expand shorter
    # aliases at each position before applying the existing nearest-field rule.
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=lambda name: (-len(name), name)))
    if not alternatives:
        return re.compile(r"(?!)")
    return re.compile((r"(?<![a-z0-9_])(?=(" + alternatives + r")(?![a-z0-9_]))")
                      if subjects else r"(?=(" + alternatives + r"))")


@dataclass(frozen=True)
class NumericSupportIndex:
    values: tuple[NumericValue, ...]
    aliases: dict[str, tuple[str, ...]]
    incomplete: bool = False
    _subjects: re.Pattern = field(init=False, repr=False, compare=False)
    _predicates: re.Pattern = field(init=False, repr=False, compare=False)
    _subject_keys: dict[str, tuple[tuple[int, str], ...]] = field(init=False, repr=False, compare=False)
    _predicate_keys: dict[str, tuple[tuple[int, str], ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        subjects: dict[str, set[str]] = {}
        predicates: dict[str, set[str]] = {}
        for entry in self.values:
            if entry.subject:
                subjects.setdefault(entry.subject.lower(), set()).add(entry.subject)
        for key in {entry.predicate for entry in self.values} | set(self.aliases):
            for alias in (key, *self.aliases.get(key, ())):
                if alias:
                    predicates.setdefault(alias.lower(), set()).add(key)
        object.__setattr__(self, "_subject_keys", {
            name: tuple((len(alias), key) for alias, keys in subjects.items()
                        if name.startswith(alias) and not re.match(r"[a-z0-9_]", name[len(alias):])
                        for key in keys)
            for name in subjects
        })
        object.__setattr__(self, "_predicate_keys", {
            name: tuple((len(alias), key) for alias, keys in predicates.items()
                        if name.startswith(alias) for key in keys)
            for name in predicates
        })
        object.__setattr__(self, "_subjects", _mention_pattern(subjects, subjects=True))
        object.__setattr__(self, "_predicates", _mention_pattern(predicates))

    @classmethod
    def build(
        cls,
        facts: list[Fact],
        aliases: dict[str, tuple[str, ...]] | None = None,
        *,
        max_facts: int = 64,
        max_chars: int = 64000,
        max_values: int = 512,
    ):
        values = []
        incomplete = len(facts) > max_facts
        remaining = max_chars
        for fact in facts[:max_facts]:
            text = str(fact.typed_value)
            remaining -= len(text)
            if remaining < 0:
                incomplete = True
                break
            # A serialized row is not a sentence expressing a value with a unit.
            # Nested descriptions/identifiers are not business numeric authority.
            if isinstance(fact.typed_value, str) and re.match(r"^records\[\d+\] 原始", text):
                incomplete = True
                continue
            unit = _UNITS.get(fact.unit or "", fact.unit or "")
            number = _decimal(fact.typed_value)
            if number is not None and unit:
                # Numeric strings and Decimal-style precision are common in source adapters.
                if unit == "CNY" and not fact.as_of:
                    incomplete = True
                    continue
                number *= MULTIPLIERS.get(fact.unit or "", 1)
                values.append(NumericValue(number, unit, fact.subject_id, fact.predicate))
            elif isinstance(fact.typed_value, str):
                extracted = list(islice(_quantities(text), max_values - len(values) + 1))
                values.extend(
                    NumericValue(value, unit, fact.subject_id, fact.predicate) for _, value, unit in extracted
                )
                if number is not None:
                    incomplete = True  # Unit missing; do not guess from the field name.
            elif not isinstance(fact.typed_value, bool):
                incomplete = True
            if len(values) > max_values:
                values = values[:max_values]
                incomplete = True
                break
        return cls(tuple(values), dict(aliases or {}), incomplete)

    def classify(self, claim: str) -> NumericStatus:
        quantities = list(_quantities(claim))
        if not quantities:
            return "not_applicable"
        if not self.values:
            return "unverifiable"
        # Clause-local caller avoids matching fields from neighboring sentences.
        folded = claim.lower()
        subject_mentions = [
            (match.start(1) + length, match.start(1), key)
            for match in self._subjects.finditer(folded)
            for length, key in self._subject_keys[match.group(1)]
        ]
        predicate_mentions = [
            (match.start(1) + length, match.start(1), key)
            for match in self._predicates.finditer(folded)
            for length, key in self._predicate_keys[match.group(1)]
        ]
        if "分别" in folded and (
            len({item[2] for item in predicate_mentions}) > 1 or len({item[2] for item in subject_mentions}) > 1
        ):
            return "unverifiable"

        def local(mentions, position):
            prior = [item for item in mentions if item[0] <= position]
            if prior:
                nearest = max((item[0], -item[1]) for item in prior)
                return {item[2] for item in prior if (item[0], -item[1]) == nearest}
            return {item[2] for item in mentions}

        for match, value, unit in quantities:
            subjects = local(subject_mentions, match.start())
            predicates = local(predicate_mentions, match.start())
            if len(subjects) > 1 or len(predicates) > 1:
                return "unverifiable"
            if not any(
                entry.value == value
                and entry.unit == unit
                and (not subjects or entry.subject in subjects)
                and (not predicates or entry.predicate in predicates)
                for entry in self.values
            ):
                # Calendar durations and currencies require conversion context;
                # absence of the same unit is not proof of a wrong value.
                comparable = any(
                    entry.unit == unit
                    and (not subjects or entry.subject in subjects)
                    and (not predicates or entry.predicate in predicates)
                    for entry in self.values
                )
                return "unverifiable" if self.incomplete or not comparable else "unsupported"
        return "supported"
