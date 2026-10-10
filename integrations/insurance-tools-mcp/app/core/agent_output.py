"""Bounded model-boundary decoding; this never authorizes facts or references."""
from __future__ import annotations

import ast
import json
import re

_FULL_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL | re.IGNORECASE)
_MAX_STRUCTURE_CHARS = 65536
_OUTPUT_FIELDS = frozenset({"answer_draft", "answer", "fact_refs", "gaps", "needs_user_input", "task_updates", "claims", "derived"})
_FIELD_LINE = re.compile(
    r"^\s*(?:[-+>#]\s*)?[`*_\"'“]*"
    r"(answer_draft|answer|fact_refs|gaps|needs_user_input|task_updates|claims|derived)"
    r"[`*_\"'”]*\s*[:：]\s*(.*)$", re.IGNORECASE,
)
_PROTOCOL_KEY = re.compile(r"[\"'“](?:answer_draft|fact_refs|needs_user_input|task_updates|claims|derived)[\"'”]\s*[:：]")
_PROTOCOL_MARKDOWN = re.compile(
    r"(?:\*\*|__|`)(?:answer_draft|fact_refs|needs_user_input|task_updates|claims|derived)(?:\*\*|__|`)\s*[:：]",
    re.IGNORECASE,
)


def _decode_value(text: str) -> object:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        # literal_eval accepts data only, never executable expressions. Keep its
        # input bounded; deeply nested or malformed literals simply fail closed.
        if len(text) <= _MAX_STRUCTURE_CHARS and text.startswith("{"):
            try:
                value = ast.literal_eval(text)
                if isinstance(value, dict):
                    return value
            except (ValueError, SyntaxError, RecursionError):
                pass
    return text


def _objects(text: str):
    """Yield non-overlapping balanced objects in one pass, skipping quoted braces."""
    start = -1
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char == "{":
                start, depth = index, 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                yield text[start:index + 1]
                start = -1


def _labelled_output(text: str) -> dict[str, object] | None:
    if text.startswith(("{", "[")):
        return None  # Broken JSON belongs to JSON/smart-quote recovery, not line labels.
    lines = text.splitlines()
    markers = [(i, match) for i, line in enumerate(lines) if (match := _FIELD_LINE.match(line))]
    if not markers or markers[0][1].group(1).lower() not in {"answer_draft", "answer"}:
        return None
    fields: dict[str, object] = {}
    for position, (index, match) in enumerate(markers):
        key = match.group(1).lower()
        end = markers[position + 1][0] if position + 1 < len(markers) else len(lines)
        raw = "\n".join([match.group(2), *lines[index + 1:end]]).strip()
        if key in {"answer_draft", "answer"}:
            value = _decode_value(raw)
            fields[key] = value if isinstance(value, str) else raw
        else:
            fields[key] = _decode_value(raw)
    return fields


def decode_output_content(content: object) -> object:
    candidate = getattr(content, "content", content)
    if not isinstance(candidate, str):
        return candidate
    text = candidate.strip()
    fence = _FULL_FENCE.fullmatch(text)
    if fence is not None:
        text = fence.group(1).strip()
    value = _decode_value(text)
    if not isinstance(value, str) or value != text:
        return value
    if len(text) <= _MAX_STRUCTURE_CHARS:
        nested_protocol = False
        for raw in _objects(text):
            value = _decode_value(raw)
            # Incidental business JSON is not an AgentOutput. Nor may a nested
            # object's key steal the outer prose/contract.
            if isinstance(value, dict) and _OUTPUT_FIELDS.intersection(value):
                prefix = re.sub(r'```(?:json)?', '', text[:text.find(raw)], flags=re.IGNORECASE).strip()
                for incidental in _objects(prefix):
                    prefix = prefix.replace(incidental, '').strip()
                if prefix and not {"answer", "answer_draft"}.intersection(value) and not has_protocol_fragment(prefix):
                    value = {"answer_draft": prefix, **value}
                return value
            if isinstance(value, dict) and has_protocol_fragment(raw):
                nested_protocol = True
        labelled = _labelled_output(text)
        if labelled is not None:
            return labelled
        if nested_protocol:
            return text  # A nested example is not a top-level output contract.
        if _PROTOCOL_KEY.search(text):
            match = re.search(r'"(answer_draft|answer)"\s*:\s*("(?:[^"\\]|\\.)*")', text)
            if match:
                try:
                    answer = json.loads(match.group(2))
                    return {'answer_draft': answer, 'fact_refs': [], 'gaps': ['部分引用或附加信息未能解析，请结合原始资料核实。']}
                except ValueError:
                    pass
        # A truncated string can still deliver its complete sentences. Never
        # invent a closing quote/reference or forward protocol fragments.
        start = re.search(r'"(?:answer_draft|answer)"\s*:\s*"', text)
        if start:
            fragment = text[start.end():]
            boundary = max(fragment.rfind("。"), fragment.rfind("！"), fragment.rfind("？"))
            if boundary >= 0 and not has_protocol_fragment(fragment[:boundary + 1]):
                return {'answer_draft': fragment[:boundary + 1], 'fact_refs': [],
                        'gaps': ['回答整理中断，仅保留已完成的部分，请结合原始资料核实。']}
    return text


def has_protocol_fragment(text: str) -> bool:
    """Identify internal fields even after narration or Markdown wrappers."""
    return bool(_PROTOCOL_KEY.search(text) or _PROTOCOL_MARKDOWN.search(text)
                or any(_FIELD_LINE.match(line) for line in text.splitlines())
                or re.match(r'^\s*(?:\{\s*(?:[\"\'“}]|$)|\[\s*(?:[\"{\]]|$))', text))


def normalize_list_field(value: object) -> object:
    return [] if value is None else [value] if isinstance(value, str) else value


def normalize_fact_refs(refs: object) -> object:
    """Normalize separators/blank entries; the citation resolver owns authority."""
    if refs is None:
        return []
    values = [refs] if isinstance(refs, str) else refs
    if not isinstance(values, list):
        return values
    return list(dict.fromkeys(part.strip() for value in values for part in re.split(r'[,，]', value) if part.strip())) if all(isinstance(value, str) for value in values) else values
