"""两个搜索目录共用的只读别名、开放 Schema 和行级释义。"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from tool_adapter_sdk.customer_detail_metadata import has_alias_conflict
from tool_adapter_sdk.output_descriptions import describe_output_page
from tool_adapter_sdk.json_values import inspect_json_container


# 字段名后缀只是接口命名：按值判断，不按后缀推断仍为密文（与客户详情口径一致）。
CIPHER_NOTE = '字段名含 Cipher 仅为接口命名；值可读即按原值展示，不自行解密或推断仍加密'
# 搜索与详情记录均按险种行返回；金额类字段只描述该行，不代表保单合计。
ROW_AMOUNT_FIELDS = frozenset({
    'sum_ins', 'year_pay_prem', 'total_modal_prem', 'frist_period_prem', 'agg_paid_prem', 'agg_pay_amount',
})
ROW_AMOUNT_NOTE = '险种行口径：只描述该条险种记录，同一保单多行时不得当作保单合计'


class FieldLike(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def aliases(self) -> str: ...


@dataclass(frozen=True)
class SearchField:
    path: str
    label: str
    aliases: str = ""


class SearchMetadata:
    def __init__(
        self, *, fields: tuple[tuple[str, str, str, str, str], ...],
        old_definitions: Mapping[str, FieldLike], old_aliases: Mapping[str, str],
        old_canonical: Callable[[str], str], old_describe: Callable[[str], str],
        extra_aliases: dict[str, str], v1_keys: set[str], legacy_keys: set[str],
        divergent: set[str], customer: bool,
    ) -> None:
        self.v1 = {row[0]: row for row in fields}
        self.old_describe = old_describe
        self.v1_keys, self.legacy_keys = v1_keys, legacy_keys
        self.divergent, self.customer = divergent, customer
        aliases = {row[4]: row[0] for row in fields}
        aliases.update(extra_aliases)
        # 只复用旧目录已经登记的路径，不从任意输入字段推断别名。
        remap: dict[str, str] = {}
        for path, _, _, _, snake in fields:
            old = old_canonical(path)
            if old in old_definitions or old != path:
                remap[old] = path
            if snake in old_definitions:
                remap[snake] = path
        remap.update(extra_aliases)
        def migrate(path: str) -> str:
            if path in remap:
                return remap[path]
            root, sep, suffix = path.partition('.')
            return remap.get(root, root) + sep + suffix
        self.old_paths = {migrate(path): path for path in old_definitions}
        aliases.update({path: migrate(path) for path in old_definitions})
        aliases.update({source: migrate(target) for source, target in old_aliases.items()})
        aliases.update({row[4]: row[0] for row in fields})
        aliases.update(extra_aliases)
        self.aliases = aliases
        self.segments = {(target.rpartition('.')[0], source.rpartition('.')[2]): target.rpartition('.')[2]
                         for source, target in aliases.items()}
        self.definitions = {migrate(path): SearchField(migrate(path), entry.label, entry.aliases)
                            for path, entry in old_definitions.items()}
        for path, label, _, _, _ in fields:
            previous = self.definitions.get(path)
            business_aliases = [re.split(r'[（(]', label)[0]]
            if previous:
                business_aliases.extend([previous.label, previous.aliases])
            self.definitions[path] = SearchField(path, label, '、'.join(business_aliases))

    def canonical_path(self, path: str) -> str:
        parts: list[str] = []
        for part in path.split('.'):
            parts.append(self.segments.get(('.'.join(parts), part), part))
        return '.'.join(parts)

    def group_source_fields(self, record: Mapping[str, JsonValue]) -> dict[str, list[tuple[str, JsonValue]]]:
        grouped: dict[str, list[tuple[str, JsonValue]]] = {}
        for key, value in record.items():
            grouped.setdefault(self.canonical_path(key), []).append((key, value))
        return grouped

    def source_value(self, record: Mapping[str, JsonValue], path: str) -> JsonValue:
        values = self.group_source_fields(record).get(self.canonical_path(path), [])
        return values[0][1] if values and not has_alias_conflict(values) else None

    def source_text(self, record: Mapping[str, JsonValue], path: str) -> str | None:
        value = self.source_value(record, path)
        return value if isinstance(value, str) and value.strip() else None

    def response_profile(self, record: Mapping[str, JsonValue]) -> str:
        new, old = bool(self.v1_keys.intersection(record)), bool(self.legacy_keys.intersection(record))
        return 'v1' if new and not old else 'legacy' if old and not new else 'undetermined'

    def describe_field(self, path: str, profile: str = 'undetermined') -> str:
        canonical = self.canonical_path(path)
        text = self._describe_field(canonical, profile)
        row = self.v1.get(canonical)
        names = {canonical, canonical.rpartition('.')[2], row[4] if row else ''}
        if not self.customer and names & ROW_AMOUNT_FIELDS and ROW_AMOUNT_NOTE not in text:
            text += '；' + ROW_AMOUNT_NOTE
        return text

    def _describe_field(self, canonical: str, profile: str) -> str:
        row = self.v1.get(canonical)
        if row is None:
            return self.old_describe(self.old_paths.get(canonical, canonical))
        _, label, meaning, domain, _ = row
        text = label + '；' + meaning
        if domain:
            text += '；取值：' + domain
        if 'Cipher' in canonical:
            text += '；' + CIPHER_NOTE
        if canonical in self.divergent:
            old = self.old_paths.get(canonical)
            legacy = self.old_describe(old) if old else '旧响应口径待确认'
            if profile == 'legacy':
                return '旧响应口径：' + legacy
            if profile == 'undetermined':
                return '版本待确认；v1 口径：' + text + '；legacy 口径：' + legacy + '；不得跨版本套用枚举'
        return text

    def resolve_focus(self, value: str) -> str:
        text = value.strip()
        canonical = self.canonical_path(text)
        if canonical in self.definitions:
            return canonical
        for path, entry in self.definitions.items():
            labels = [entry.label, re.split(r'[（(]', entry.label)[0]]
            labels.extend(re.split(r'[、,，;；/\n]', entry.aliases))
            if text in labels:
                return path
        return text

    def field_schema(self, path: str) -> dict[str, JsonValue]:
        canonical = self.canonical_path(path)
        children = {p[len(canonical) + 1:].split('.')[0] for p in self.definitions
                    if p.startswith(canonical + '.')}
        children.update(source for (parent, source) in self.segments if parent == canonical)
        if not children:
            return {}
        properties: dict[str, JsonValue] = {
            child: {'description': self.describe_field(path + '.' + child),
                    **self.field_schema(path + '.' + child)} for child in sorted(children)}
        return {'properties': properties, 'items': {'properties': properties}}

    def record_properties(self) -> dict[str, JsonValue]:
        roots = {p.split('.')[0] for p in self.definitions} | {p.split('.')[0] for p in self.aliases}
        return {path: {'description': self.describe_field(path), **self.field_schema(path)} for path in sorted(roots)}

    def add_source_alias_schema(self, schema: dict[str, JsonValue]) -> None:
        properties = schema.get('properties')
        if isinstance(properties, dict):
            for key, value in self.record_properties().items():
                properties.setdefault(key, value)

    def total_description(self, records: JsonValue) -> str:
        if self.customer:
            return '符合查询条件的客户总数，不是当前页记录数或展示条数'
        if isinstance(records, list) and records and all(
            isinstance(row, dict) and self.response_profile(row) == 'legacy' for row in records
        ):
            return '险种行总数，不是保单张数'
        return '接口报告的匹配结果条目总数；未确认保单张数及是否为险种粒度'

    def describe_output_data(self, data: dict[str, JsonValue]) -> dict[str, str]:
        result = describe_output_page(
            data, describe_field=self.describe_field, canonical_path=self.canonical_path,
            defined_paths=self.definitions,
            page_labels={'pageNo': '当前页码', 'pageSize': '服务页容量',
                         'total': self.total_description(data.get('records')), 'records': '本次返回的搜索记录'},
            record_description='单条搜索记录；空值不等于零或否；金额未注明单位、币种或时点时不推断',
        )
        records = data.get('records')
        profiles = {self.response_profile(row) for row in records if isinstance(row, dict)} if isinstance(records, list) else set()
        page_profile = next(iter(profiles)) if len(profiles) == 1 else None

        def walk(value: JsonValue, logical: str, actual: str, profile: str) -> None:
            canonical = self.canonical_path(logical)
            if self.customer and canonical == 'familyInfo' and isinstance(value, str):
                decoded = inspect_json_container(value)
                conflicts: list[str] = []

                def inspect_members(item: JsonValue, location: str) -> None:
                    if isinstance(item, list):
                        for index, member in enumerate(item):
                            inspect_members(member, f'{location}[{index}]')
                    elif isinstance(item, dict):
                        groups: dict[str, list[tuple[str, JsonValue]]] = {}
                        for key, member_value in item.items():
                            groups.setdefault(self.canonical_path(logical + '.' + key), []).append((key, member_value))
                        for values in groups.values():
                            if has_alias_conflict(values):
                                conflicts.append(location + '：' + '、'.join(key for key, _ in values))

                inspect_members(decoded, actual)
                if conflicts:
                    result[actual] = result.get(actual, '') + '；字符串内同源别名冲突：' + '；'.join(conflicts) + '；原值保留，不选取单值'
            top_level = re.fullmatch(r'records\[\d+\]\.([^.\[\]]+)', actual)
            if canonical in self.divergent and page_profile is not None and top_level:
                # 全页同一响应口径：只在共享路径写一次该口径释义，不逐行重复，也不再列出另一版本枚举。
                result['records[].' + top_level.group(1)] = self.describe_field(logical, page_profile)
            elif canonical in self.divergent:
                # 遍历器在实际下标路径只写形态；限制前缀，避免未来改动把共享双版本释义带回本行。
                previous = result.get(actual, '')
                state = previous if previous.startswith('本条为') else ''
                result[actual] = self.describe_field(logical, profile) + ('；' + state if state else '')
            row = self.v1.get(canonical)
            if profile == 'v1' and row and value is not None and row[3] and row[3] != '见说明':
                allowed = row[3].split('、')
                if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) not in allowed:
                    result[actual] = result.get(actual, self.describe_field(logical, profile)) + '；本条取值不符合 v1 所列表示，取值待确认，原值保留'
            if isinstance(value, dict):
                groups: dict[str, list[tuple[str, JsonValue]]] = {}
                for key, item in value.items():
                    child = logical + '.' + key if logical else key
                    groups.setdefault(self.canonical_path(child), []).append((key, item))
                    walk(item, child, actual + '.' + key, profile)
                for values in groups.values():
                    if has_alias_conflict(values):
                        for key, _ in values:
                            target = actual + '.' + key
                            result[target] = result.get(target, '') + '；同源别名值冲突，原值保留，不选取单值'
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, logical, f'{actual}[{index}]', profile)
        if isinstance(records, list):
            for index, record in enumerate(records):
                if isinstance(record, dict):
                    walk(record, '', f'records[{index}]', self.response_profile(record))
        return result
