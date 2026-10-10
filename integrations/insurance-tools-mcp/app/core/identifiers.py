"""Identifier matching projection. Source slices/offsets always refer to the caller text."""
from __future__ import annotations
from dataclasses import dataclass
import re
import unicodedata

POLICY_PATTERN = r'(?<![0-9A-Za-z])(?:HP|GP|P)[0-9][0-9A-Z]{2,}(?![0-9A-Za-z])'


def matching_text(text: str) -> str:
    return unicodedata.normalize('NFKC', text).upper()


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str
    raw: str
    start: int
    end: int


def extract_identifiers(text: str, *, kind: str = 'policy', pattern: str = POLICY_PATTERN) -> list[Identifier]:
    # Per-character expansion maps full-width and compatibility characters back
    # to exact source positions; never use normalized offsets to slice raw input.
    projected, offsets = '', []
    for index, char in enumerate(text):
        chunk = matching_text(char)
        projected += chunk
        offsets.extend([index] * len(chunk))
    result = []
    for match in re.finditer(pattern, projected, re.IGNORECASE):
        if not match.group() or not offsets:
            continue
        start, end = offsets[match.start()], offsets[match.end() - 1] + 1
        result.append(Identifier(kind, match.group().upper(), text[start:end], start, end))
    return result


def policy_ids(text: str) -> list[str]:
    return list(dict.fromkeys(item.value for item in extract_identifiers(text)))
