"""policy_search 工具核心(§10.1 存量·接口 6:凭自然语言搜代理人名下保单,本方案启用)。

认证 agent_id 保持服务端权限主体；真实解析 agentCode 单独取上游请求体 user_id，
经运行时元数据 upstream_user_id 传入，不由模型填写，也不从认证身份兜底。
检索结果为结构化候选(保单号/被保人/产品/状态),供定位链路与实体记忆消费
(last_policy_set 写入在 Direct 执行器/上层,本工具无状态)。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, ErrorCode, Evidence, Fact, SourceRef, ToolError
from app.schemas.enums import SourceType
from app.tools.backend import BackendClient, call_backend, error_envelope, error_outcome
from app.tools.backend_contracts import PolicySearchResponse, parse_backend_response
from app.tools.mode import require_tool_execution_context, tool_execution_scope
from app.tools.real.policy_query import CONTRACT_GAP_NOTES
from app.tools.search_source_projection import SOURCE_HINT, append_source_records, source_page_data
from tool_adapter_sdk.contracts import POLICY_SEARCH, PolicySearchContextDto, PolicySearchRequest
from tool_adapter_sdk.policy_search_metadata import response_profile, source_text

IMPLEMENTED = True

_SOURCE = SourceRef(system="policy_svc", endpoint="/policy/search")

# 清单 brief 的聚合字段(Owner 2026-09-18 口径:核心六件——产品/状态/投保人/被保人/
# 投保日期;测试账号实测全量 13/13 有值)。brief 不含保单号:render 模板主体头
# 会补「保单 P123」标识,值里重复保单号即双重回显。保单是一单多险种行:产品取各行简称去重保序并列
# (不做汇总),状态/投保人/投保日期仅各行一致才展示(与被保人冲突纪律同向);
# 无值/冲突一律省略——「没有数据的信息不用展示」,不再硬编码「产品：未知」。
_BRIEF_PLAN_ABBR = "plan_abbr_name"
_BRIEF_STATUS = "pol_status_desc"
_BRIEF_APPLICANT = "applicant_name"
_BRIEF_INSURE_DATE = "insure_date"

# 行级 gap 的保单号锚(「保单 P260000000000019 的被保人…」;gap 文本由
# real/policy_query 固定生成,短号/长号都兼容):用于把缺口对齐到展示集合——
# 未展示保单的行级缺口不进用户正文(用户没看到该保单,无从核对)。
_GAP_POLICY_RE = re.compile(r"保单\s?(P\d+)")


def _row_text(record: dict, key: str) -> str | None:
    value = source_text(record, key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _unanimous_text(rows: list[dict], key: str) -> str | None:
    """同保单各险种行均有非空文本且一致才返回；任一行缺失不能用其余行代填。"""
    values = {_row_text(row, key) for row in rows}
    return values.pop() if len(values) == 1 and None not in values else None


def _policy_list_brief(insured_name: str | None, rows: list[dict]) -> str:
    """真实源页路径的清单条目:产品列表·状态·投保人·被保人·投保日期。

    被保人沿用候选投影(已有「各行完整且一致才映射」纪律),records 不重取——
    避免同一事实双源;金额字段(保额/保费)不进 brief:行级口径且无业务时点,
    跨行并列易被误读为汇总(契约纪律「两个险种保额不等于保单总额」)。"""
    segments: list[str] = []
    plans = list(dict.fromkeys(
        text for text in (_row_text(row, _BRIEF_PLAN_ABBR) for row in rows) if text
    ))
    # 旧险种清单继续并列简称；v1 产品字段是保单产品，不与险种名当作同义值合并。
    if rows and not all(response_profile(row) == "legacy" for row in rows):
        product = _unanimous_text(rows, "polProductName")
        if product:
            plans = [product]
        elif any("polProductName" in row or "pol_product_name" in row for row in rows):
            plans = []
    if plans:
        segments.append("/".join(plans))
    if status := _unanimous_text(rows, _BRIEF_STATUS):
        segments.append(status)
    if applicant := _unanimous_text(rows, _BRIEF_APPLICANT):
        segments.append(f"投保人{applicant}")
    if insured_name:
        segments.append(f"被保人{insured_name}")
    if insure_date := _unanimous_text(rows, _BRIEF_INSURE_DATE):
        segments.append(f"{insure_date}投保")
    return "·".join(segments) if segments else "保单资料未返回"


@tool_execution_scope("policy_search")
async def policy_search(
    query: str,
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
    session_id: str | None = None,
    upstream_user_id: str | None = None,
    empno: str | None = None,
    contexts: list[PolicySearchContextDto] | list[dict] | None = None,
) -> str:
    """按自然语言搜索代理人名下保单,返回 str JSON 信封。"""
    if not isinstance(upstream_user_id, str) or not upstream_user_id.strip():
        missing = "保单解析缺少上游请求体 user_id，尚未执行查询；请上游补充该字段"
        return make_envelope(
            status=EnvelopeStatus.ERROR, hint=missing, gaps=[missing], source=_SOURCE,
            error=ToolError(code=ErrorCode.UPSTREAM, retryable=False),
        )
    text = query.strip()
    if not text:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供保单搜索条件")
    raw = await call_backend(
        POLICY_SEARCH,
        PolicySearchRequest.model_validate({"query": text, "contexts": contexts}),
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        client=backend_client, session_id=session_id, upstream_user_id=upstream_user_id, empno=empno,
    )
    if (outcome := error_outcome(raw)) is not None:
        return error_envelope(outcome, source=_SOURCE, raw=raw)

    parsed = parse_backend_response(PolicySearchResponse, raw, source=_SOURCE)
    if isinstance(parsed, str):
        return parsed
    if parsed.unsupported_message is not None:
        return make_envelope(
            status=EnvelopeStatus.AMBIGUOUS,
            hint=parsed.unsupported_message,
            gaps=["未生成可靠保单查询条件，尚未执行查询；请补充明确的保单筛选条件"],
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )
    data, descriptions = source_page_data(parsed.page.source_page if parsed.page is not None else None, "policy")
    limit = require_tool_execution_context().tool_config.result_limit or 5
    total = len(parsed.policies)
    policies = parsed.policies[:limit]
    if not policies:
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint=("本次条件查询未命中保单记录，可提供完整保单号等自足条件" if parsed.page is not None
                  else "名下未检索到相关保单,可提供客户姓名、产品名或完整保单号"),
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
            data=data, field_descriptions=descriptions,
        )

    facts: list[Fact] = []
    evidence: list[Evidence] = []
    rows_by_polno: dict[str, list[dict]] = {}
    if parsed.page is not None and parsed.page.source_page is not None:
        for record in parsed.page.source_page.records:
            polno = record.get("polno")
            if isinstance(polno, str) and polno.strip():
                rows_by_polno.setdefault(polno, []).append(record)
    for i, item in enumerate(policies):
        policy_no = item.policy_no
        insured_name = item.insured_name
        product_name = item.product_name
        brief = (_policy_list_brief(insured_name, rows_by_polno.get(policy_no, []))
                 if parsed.page is not None else f"{insured_name}·{product_name}·{item.status}")
        evidence_id = f"E-{parsed.request_id}-{i + 1:02d}"
        evidence.append(Evidence(
            evidence_id=evidence_id,
            source_type=SourceType.BUSINESS_API,
            source_id="policy_svc",
            retrieved_at=datetime.now(timezone.utc),
        ))
        facts.append(to_fact(
            predicate="policy_brief",
            typed_value=brief,
            subject_id=policy_no,
            subject_type="policy",
            source_call_id=parsed.request_id,
            identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
            evidence_refs=[evidence_id],
        ))
        # 7-b3 名称回指只消费类型化字段，不从 policy_brief/答案文本反解析。
        # 两条名称 Fact 与摘要共享同一后端证据，仍由工具信封保持唯一权威。
        for predicate, value in (("product_name", product_name), ("insured_name", insured_name)):
            if value:
                facts.append(to_fact(
                    predicate=predicate,
                    typed_value=value,
                    subject_id=policy_no,
                    subject_type="policy",
                    source_call_id=parsed.request_id,
                    identity_scope=agent_id,
                    fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
                    evidence_refs=[evidence_id],
                ))
    truncated = total > limit
    gaps = [f"检索到 {total} 张保单，仅返回前 {limit} 张；请补充条件缩小范围"] if truncated else []
    hint = f"检索到 {total} 张名下保单，返回前 {limit} 张" if truncated else f"检索到 {total} 张名下保单"
    if parsed.page is not None:
        gaps = list(parsed.page.gaps)
        # 2026-09-18 用户面 gap 收口(检视切片 3):信封 gaps 会被出口渲染逐条追加
        # 到用户正文,故只保留用户可行动的缺口——①契约级取数口径披露滤出并入
        # hint(模型/审计可见,模型视角纪律常驻工具描述);②「保单 Pxxx …」行级
        # 缺口只保留展示集合内的保单(未展示保单的数据缺口用户无从核对)。
        contract_notes = [gap for gap in gaps if gap in CONTRACT_GAP_NOTES]
        gaps = [gap for gap in gaps if gap not in CONTRACT_GAP_NOTES]
        visible_polnos = {item.policy_no for item in policies}
        gaps = [
            gap for gap in gaps
            if (match := _GAP_POLICY_RE.search(gap)) is None or match.group(1) in visible_polnos
        ]
        if truncated:
            gaps.append(f"当前页去重 {total} 张保单，仅展示前 {limit} 张，请缩小查询范围")
        elif parsed.page.truncated:
            gaps.append("检索结果超出单页读取上限，可能仍有未返回保单，建议补充条件缩小范围")
        source_rows = parsed.page.source_page.records if parsed.page.source_page is not None else None
        legacy = source_rows is None or bool(source_rows) and all(response_profile(row) == "legacy" for row in source_rows)
        row_label = "险种记录" if legacy else "查询结果记录"
        hint = (f"查询报告 {parsed.page.record_total} 条{row_label}，当前页读取 {len(parsed.page.records)} 条，"
                f"去重为 {total} 张保单，展示 {len(policies)} 张；字段完整性以实际返回为准")
        if not legacy:
            hint += "；total 的保单/险种粒度待确认，当前页去重数不代表全量保单张数"
        truncated = truncated or parsed.page.truncated
        if not truncated:
            # SUCCESS 表示保单候选集合完整可用，不表示所有展示字段齐全。
            # 严格快照流程只消费 SUCCESS 候选；缺字段仍必须在 hint 披露。
            if gaps:
                hint += "；" + "；".join(gaps)
            gaps = []
        if contract_notes:
            hint += "；" + "；".join(contract_notes)
    append_source_records(data, "policy", request_id=parsed.request_id, agent_id=agent_id,
                          facts=facts, evidence=evidence)
    if data is not None:
        hint += SOURCE_HINT
    return make_envelope(
        status=EnvelopeStatus.PARTIAL if truncated or gaps else EnvelopeStatus.SUCCESS,
        facts=facts,
        evidence=evidence,
        gaps=gaps or None,
        hint=hint,
        source=_SOURCE,
        request_id=parsed.request_id,
        latency_ms=parsed.latency_ms,
        data=data, field_descriptions=descriptions,
    )
