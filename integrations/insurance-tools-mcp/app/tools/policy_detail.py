"""policy_detail 工具核心：统一承载保单详情与字段查询。

自然语言内部入口解析 query 中的保单号、尾号和字段口语；模型绑定使用结构化 polno。
真实详情始终保留完整业务数据，指定字段仅决定关注点及缺口；旧响应分支维持字段投影。
Direct 与 policy-field-lookup Workflow 复用 ``fetch_policy_details``，Gate 本身
仍保持零后端调用。
"""
from __future__ import annotations
from app.core.policy_projection import POLICY_FIELDS_MISSING_HINT as POLICY_FIELDS_MISSING_HINT
from app.core.policy_projection import expand_basic_info

import re
from dataclasses import dataclass

from app.conf import fields_dict
from app.core.envelope import make_envelope, status_of, to_fact
from app.core.field_dict import FieldDict
from app.core.resolve import valid_policy_tail, POLICY_NO_RE, resolve_direct_query, scan_fields, strip_stopwords
from app.schemas import EnvelopeStatus, SourceRef
from app.tools.backend import BackendClient, call_backend, error_envelope, error_outcome
from app.tools.backend_contracts import PolicyDetailResponse, parse_backend_response
from app.tools.mode import tool_execution_scope
from app.tools.policy_detail_projection import expand_detail_fields, project_real_detail
from tool_adapter_sdk.contracts import POLICY_DETAIL, PolicyDetailRequest

IMPLEMENTED = True

_UNITS = {
    "cash_value": "CNY",
    "universal_account_balance": "CNY",
    "premium": "CNY",
    "surrender_value": "CNY",
}
_BARE_NO_RE = re.compile(r"\d{3,}")
_TAIL_PATTERN = r"(?<![0-9A-Za-z])\d{3,}(?![0-9A-Za-z])"
_SOURCE = SourceRef(system="policy_svc", endpoint="/policy/full")
POLICY_DETAIL_INTERFACE = "policy_detail"



@dataclass(frozen=True)
class ParsedPolicyQuery:
    """自然语言保单查询的确定性解析结果。"""

    policy_no: str | None
    field_codes: tuple[str, ...]
    spoken_fields: tuple[str, ...]
    residual: str
    interface: str


def parse_policy_query(query: str, field_dict: FieldDict | None = None) -> ParsedPolicyQuery:
    """自然语言 → 广义保单号、标准字段码与剩余文本。"""
    dictionary = field_dict or fields_dict()
    resolved = resolve_direct_query(query, dictionary, tail_pattern=_TAIL_PATTERN)
    if resolved is not None:
        return ParsedPolicyQuery(
            policy_no=resolved.policy_no,
            field_codes=resolved.field_codes,
            spoken_fields=resolved.spoken_fields,
            residual=resolved.residual,
            interface=POLICY_DETAIL_INTERFACE,
        )
    codes, spoken, residual = scan_fields(query, dictionary)
    return ParsedPolicyQuery(
        policy_no=None,
        field_codes=codes,
        spoken_fields=spoken,
        residual=strip_stopwords(residual),
        interface="",
    )


@tool_execution_scope("policy_detail")
async def policy_detail(
    query: str,
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
    upstream_user_id: str | None = None,
    empno: str | None = None,
) -> str:
    """按自然语言问题查询保单详情。"""
    text = query.strip()
    if not text:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供保单详情查询问题")
    parsed = parse_policy_query(text)
    ref = parsed.policy_no or ""
    fields = list(parsed.field_codes) if parsed.field_codes else None
    return await fetch_policy_details(
        ref,
        fields,
        agent_id=agent_id,
        trace_id=trace_id,
        budget=budget,
        backend_client=backend_client, upstream_user_id=upstream_user_id, empno=empno,
    )


@tool_execution_scope("policy_detail")
async def fetch_policy_details(
    policy_input: str,
    fields_spoken: list[str] | None = None,
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
    upstream_user_id: str | None = None,
    empno: str | None = None,
) -> str:
    """按已解析保单号取详情；real 的 fields_spoken 仅指定关注点，不裁剪原始数据。"""
    from app.core.resolve import normalize_policy_text
    ref = normalize_policy_text(policy_input).strip()
    if POLICY_NO_RE.fullmatch(ref) is None and not valid_policy_tail(ref):
        return make_envelope(
            status=EnvelopeStatus.AMBIGUOUS,
            hint="保单未锁定,请提供保单号(支持尾号)或先定位保单",
        )

    requested: list[str] | None = None
    unknown: list[str] = []
    if fields_spoken:
        # 词典在调用点取当前生效版本（与 Gate/resolve 同源），不做模块级固化——
        # fields_{ENV_TYPE}_args.yaml 版本化变更后本工具与 Gate 的归一口径同轮一致。
        dictionary = fields_dict()
        from tool_adapter_sdk.policy_detail_metadata import resolve_focus, DEFINITIONS
        normalized = [(str(value), dictionary.normalize(str(value)) or
                       (focus if (focus := resolve_focus(str(value))) in DEFINITIONS or focus == "pay_record" else None))
                      for field in fields_spoken for value in expand_basic_info(str(field))]
        requested = expand_detail_fields(
            list(dict.fromkeys(code for _, code in normalized if code is not None))
        )
        unknown = list(dict.fromkeys(value for value, code in normalized if code is None))

    raw = await call_backend(
        POLICY_DETAIL,
        PolicyDetailRequest(policy_input=ref),
        agent_id=agent_id,
        trace_id=trace_id,
        budget=budget,
        upstream_user_id=upstream_user_id, empno=empno,
        client=backend_client,
    )
    if (outcome := error_outcome(raw)) is not None:
        return error_envelope(outcome, source=_SOURCE, raw=raw)
    parsed = parse_backend_response(PolicyDetailResponse, raw, source=_SOURCE)
    if isinstance(parsed, str):
        return parsed
    if parsed.detail_page is not None or parsed.candidate_page is not None:
        return project_real_detail(parsed, fields_spoken or None, [], agent_id)
    if fields_spoken and not requested:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint=f"字段词未命中词典:{unknown or fields_spoken}")
    if parsed.policy_no is None:
        if parsed.candidates:
            candidates = parsed.candidates[:5]
            facts = [
                to_fact(
                    predicate="policy_candidate",
                    typed_value=f"{candidate.insured_name}·{candidate.product_name}",
                    subject_id=candidate.policy_no,
                    subject_type="policy",
                    source_call_id=parsed.request_id,
                    identity_scope=agent_id,
                    fact_id=f"F-{parsed.request_id}-{index + 1:03d}",
                )
                for index, candidate in enumerate(candidates)
            ]
            return make_envelope(
                status=EnvelopeStatus.AMBIGUOUS,
                facts=facts,
                hint=f"尾号匹配到 {len(candidates)} 张保单:{'、'.join(c.policy_no for c in candidates)},请确认完整保单号",
                source=_SOURCE,
                request_id=parsed.request_id,
                latency_ms=parsed.latency_ms,
            )
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint="未查询到该保单,请核对保单号",
            source=_SOURCE,
            request_id=parsed.request_id,
            latency_ms=parsed.latency_ms,
        )

    got = parsed.fields
    # Compatibility adapters expose logical scalar fields; translate only the
    # requested view, never rewrite their source values or fact predicates.
    legacy = {"insurant_name": "insured_name", "pol_status_desc": "policy_status",
              "year_pay_prem": "premium", "pol_product_name": "product_name"}
    if requested:
        requested = [legacy.get(code, code) if code not in got else code for code in requested]
    returned_codes = list(got) if requested is None else [code for code in requested if code in got]
    facts = [
        to_fact(
            predicate=code,
            typed_value=got[code],
            subject_id=parsed.policy_no,
            source_call_id=parsed.request_id,
            identity_scope=agent_id,
            unit=_UNITS.get(code),
            as_of=parsed.as_of if _UNITS.get(code) else None,
            fact_id=f"F-{parsed.request_id}-{index + 1:03d}",
        )
        for index, code in enumerate(returned_codes)
    ]
    gaps = [
        *([] if requested is None else [f"字段未返回:{code}" for code in requested if code not in got]),
        *[f"字段未识别:{value}" for value in unknown],
    ]
    if not facts and not gaps:
        gaps = ["本次未返回保单详情字段"]
    expected = returned_codes if requested is None else requested
    status = status_of(returned_codes, expected)
    if facts and gaps:
        status = EnvelopeStatus.PARTIAL
    return make_envelope(
        status=status,
        facts=facts,
        gaps=gaps,
        hint=None if facts else POLICY_FIELDS_MISSING_HINT,
        source=_SOURCE,
        request_id=parsed.request_id,
        latency_ms=parsed.latency_ms,
    )
