"""Bounded quantity syntax shared by assertion scanning and numeric provenance."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_DIGITS = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_PLACES = {'十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}
_NUMBER = r'(?:\d{1,8}[万千百]\d{1,8}(?:[千百十]\d{0,8})?|[+-]?\d[\d,，]{0,31}(?:\.\d{1,12})?|[零〇一二两三四五六七八九十百千万亿]{1,32})'
QUANTITY = re.compile(
    rf'(?<![\d.零〇一二两三四五六七八九十百千万亿])(?P<percent>百分之)?(?P<number>{_NUMBER}|半)'
    r'(?P<half>个?半)?\s*(?P<unit>个工作日|工作日|万元|亿元|个月|周岁|元|万|天|日|岁|年|月|周|倍|次|项|种|成|%|％)'
    rf'|百分之(?P<percentage>{_NUMBER})'
)
DATE = re.compile(r'\d{4}(?:年\d{1,2}月\d{1,2}日|[-/.]\d{1,2}[-/.]\d{1,2})')
UNITS = {'元': 'CNY', '万元': 'CNY', '亿元': 'CNY', '万': 'CNY', '％': '%',
         '个月': '月', '周岁': '岁', '日': '天', '个工作日': '工作日', '成': '%'}
MULTIPLIERS = {'万元': 10000, '万': 10000, '亿元': 100000000, '成': 10}


def decimal_value(raw: object) -> Decimal | None:
    if isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).replace(',', '').replace('，', ''))
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def chinese_number(text: str) -> Decimal | None:
    if text == '半':
        return Decimal('0.5')
    if not text or len(text) > 32:
        return None
    if re.fullmatch(r'\d+[万千百]\d+(?:[千百十]\d*)?', text):
        mixed_total = Decimal(0)
        for match in re.finditer(r'(\d+)([万千百十]?)', text):
            mixed_total += Decimal(match.group(1)) * _PLACES.get(match.group(2), 1)
        return mixed_total
    if all(char in _DIGITS for char in text):
        return Decimal(''.join(str(_DIGITS[char]) for char in text))
    total = section = digit = 0
    for char in text:
        if char in _DIGITS:
            digit = _DIGITS[char]
        elif char in _PLACES:
            place = _PLACES[char]
            if place < 10000:
                section += (digit or 1) * place
            elif place == 10000:
                total += (section + digit) * place
                section = 0
            else:
                total = (total + section + digit) * place
                section = 0
            digit = 0
        else:
            return None
    return Decimal(total + section + digit)


def quantity_matches(text: str):
    masked = DATE.sub(lambda match: ' ' * len(match.group()), text)
    return QUANTITY.finditer(masked)


def quantities(text: str):
    """Yield original-offset matches with normalized Decimal value and explicit unit.

    No currency or calendar conversion is inferred. Bare IDs/phones are not values.
    """
    for match in quantity_matches(text):
        raw = match.group('percentage') or match.group('number')
        value = decimal_value(raw)
        if value is None:
            value = chinese_number(raw)
        if value is None:
            continue
        if match.group('half'):
            value += Decimal('0.5')
        unit = '%' if match.group('percentage') or match.group('percent') else match.group('unit')
        yield match, value * MULTIPLIERS.get(unit, 1), UNITS.get(unit, unit)
