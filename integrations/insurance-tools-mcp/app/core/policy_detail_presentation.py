"""确定性回答只选择关注点事实；不修改工具全量数据或事实池。"""
from __future__ import annotations

from app.core.policy_projection import expand_basic_info

from typing import Any

from app.schemas import Fact, ToolEnvelope
from tool_adapter_sdk.policy_detail_metadata import SOURCE_ALIASES, resolve_focus

_LOGICAL_FOCUS = {"premium": "year_pay_prem"}


def detail_display_facts(envelope: ToolEnvelope, fields: list[str] | None) -> list[Fact]:
    from app.core.policy_projection import display_policy_facts
    return display_policy_facts(envelope.facts, fields)

def detail_agent_context(envelope: ToolEnvelope, fields: list[str] | None) -> dict[str, Any]:
    """把 Direct 已取证结果投影成最小模型上下文，不注入整份保单源记录。

    冲突字段没有保单级标量 Fact 时，保留对应险种行 Fact ID 与当前关注字段，
    使 Agent 能引用既有证据解释差异，而无需再次调用同一详情接口。
    """
    requested = _requested_predicates(fields)
    context: dict[str, Any] = {
        "status": envelope.status.value,
        "requested_fields": requested,
        "facts": [
            {
                "fact_id": fact.fact_id,
                "subject_id": fact.subject_id,
                "predicate": fact.predicate,
                "typed_value": fact.typed_value,
                "unit": fact.unit,
                "as_of": fact.as_of,
            }
            for fact in detail_display_facts(envelope, fields)
        ],
        "gaps": list(envelope.gaps),
    }
    records = (envelope.data or {}).get("records")
    if not isinstance(records, list) or not requested:
        return context
    record_facts = [fact for fact in envelope.facts if fact.predicate == "policy_risk_record"]
    row_fields = list(dict.fromkeys(["polno", "pol_product_name", "plan_full_name", *requested]))
    projected_rows: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        selected: dict[str, Any] = {}
        for path in row_fields:
            values = _path_scalars(record, path.split("."))
            if values:
                selected[path] = values[0] if len(values) == 1 else values
        if not selected:
            continue
        projected_rows.append({
            "fact_id": record_facts[index].fact_id if index < len(record_facts) else None,
            "fields": selected,
        })
    if projected_rows:
        context["risk_records"] = projected_rows
    return context


def _requested_predicates(fields: list[str] | None) -> list[str]:
    predicates: list[str] = []
    for field in fields or []:
        selected = expand_basic_info(field)
        for item in selected:
            code = _LOGICAL_FOCUS.get(item, resolve_focus(item))
            code = "insured_name" if code == "insurant_name" else code
            canonical = SOURCE_ALIASES.get(code, code)
            if canonical not in predicates:
                predicates.append(canonical)
    return predicates


def _path_scalars(value: Any, parts: list[str]) -> list[str | int | float | bool]:
    if isinstance(value, list):
        return [item for child in value for item in _path_scalars(child, parts)]
    if not parts or not isinstance(value, dict):
        return [value] if isinstance(value, (str, int, float, bool)) else []
    part = parts[0]
    keys = [
        part,
        SOURCE_ALIASES.get(part, part),
        *(source for source, canonical in SOURCE_ALIASES.items() if canonical == part),
    ]
    for key in dict.fromkeys(keys):
        if key in value:
            return _path_scalars(value[key], parts[1:])
    return []


def detail_row_answer(envelope: ToolEnvelope, fields: list[str] | None) -> str | None:
    """Render requested scalar fields per source risk row; never aggregate or infer units."""
    requested = _requested_predicates(fields)
    rows = (envelope.data or {}).get('records')
    if not requested or not isinstance(rows, list) or not rows:
        return None
    from app.conf import fields_dict
    from app.core.render import field_labels, format_amount
    from tool_adapter_sdk.policy_detail_metadata import is_amount_field
    labels = field_labels(fields_dict())
    lines = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        values = [(code, _path_scalars(row, code.split('.'))) for code in requested]
        values = [(code, value) for code, value in values if value]
        if not values:
            continue
        name = row.get('pol_product_name') or row.get('polProductName') or row.get('plan_full_name') or ''
        lines.append(f"保单 {row.get('polno', '')} · 险种第{index}行{' · ' + str(name) if name else ''}：")
        for code, items in values:
            rendered = [format_amount(item) if type(item) in {int, float} and is_amount_field(code) else str(item)
                        for item in items]
            lines.append(f"· {labels.get(code, '查询字段')}：{'、'.join(rendered)}")
    if not lines:
        return None
    lines.append('以上为各险种原始字段值，不合计为保单总额；未提供的币种和业务时点仍需核实。')
    return '\n'.join(lines)


def detail_missing_rows(envelope: ToolEnvelope, fields: list[str] | None) -> str | None:
    """Only requested fields absent from the scalar projection need row rendering."""
    from app.core.policy_projection import display_policy_facts
    missing = [code for code in _requested_predicates(fields)
               if not display_policy_facts(envelope.facts, [code])]
    return detail_row_answer(envelope, missing) if missing else None


def detail_complete_answer(envelope: ToolEnvelope, fields: list[str] | None) -> str | None:
    """Shared Direct/Workflow composition: scalar fields and multi-risk rows coexist."""
    rows = detail_missing_rows(envelope, fields)
    if not rows:
        return None  # Existing template path handles all scalar fields.
    from app.core.render import render_template_facts, field_labels
    from app.conf import fields_dict
    facts = detail_display_facts(envelope, fields)
    scalar = render_template_facts(facts, field_labels(fields_dict())) if facts else ''
    return '\n\n'.join(part for part in (scalar, rows) if part)
