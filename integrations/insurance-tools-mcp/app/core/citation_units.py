"""引用的展示单元与子句边界；不解析来源、不推断事实支撑。"""
from __future__ import annotations

import re

_PREFIX = re.compile(r'^[ \t]*(?:#{1,6}[ \t]+|[-+*][ \t]+|\d+[.)、](?!\d)[ \t]*|[一二三四五六七八九十]+、[ \t]*)')

_POST_REF = re.compile(r'[ \t]*\[[^\]\n]*\]')


def assertion_text(text: str) -> str:
    """仅遮蔽行首结构标记，保留原始字符偏移和标题/列表内的业务断言。"""
    return ''.join(_PREFIX.sub(lambda m: ' ' * len(m.group()), line, count=1)
                   for line in text.splitlines(keepends=True))


def _split(text: str, *, whole_sentence: bool = False) -> list[str]:
    result, start, i = [], 0, 0
    while i < len(text):
        ch = text[i]
        decimal = ch in '.,' and i > 0 and i + 1 < len(text) and text[i - 1].isdigit() and text[i + 1].isdigit()
        stop = ch in ('。！？!?\n' if whole_sentence else '。！？!?，；;\n') or (
            ch in ('.' if whole_sentence else '.,') and not decimal)
        i += 1
        if stop:
            # 句号后的 [n] 及空格仍属于前句；不越过换行。
            while i < len(text) and text[i] in '。！？!?.':
                i += 1
            if ch != '\n':
                while True:
                    match = _POST_REF.match(text, i)
                    if match is None:
                        break
                    i = match.end()
            result.append(text[start:i])
            start = i
    if start < len(text):
        result.append(text[start:])
    return result


def sentence_units(text: str) -> list[str]:
    """Whole sentence boundaries for record recovery; commas never sever a condition."""
    return _split(text, whole_sentence=True)


def citation_units(text: str) -> list[str]:
    """表格行、列表项和标题整行保留；普通文本按句/子句切分且不丢分隔符。"""
    units = []
    for line in text.splitlines(keepends=True):
        if _PREFIX.match(line) or re.search(r'(?<!\\)\|', line):
            units.append(line)
        else:
            units.extend(_split(line))
    return units


def citation_parts(unit: str) -> list[str]:
    """展示单元内各自承担引用的子句；表格按单元格核对，不能跨格借用引用。"""
    body = _PREFIX.sub('', unit, count=1)
    cells = re.split(r'(?<!\\)\|', body) if re.search(r'(?<!\\)\|', body) else [body]
    return [part for cell in cells for part in _split(cell) if part.strip()]


def without_citations(text: str) -> str:
    """移除标记及其前方行内空格，不吞掉换行或行首缩进。"""
    return re.sub(
        r'(?<=\S)[ \t]+\[[^\]\n]*\]|\[[^\]\n]*\]', '', text)
