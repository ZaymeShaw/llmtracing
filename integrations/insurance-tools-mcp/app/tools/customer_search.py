"""customer_search 工具核心(§10.1 存量·接口 7:凭自然语言搜代理人名下客户,本方案启用)。

Owner 2026-08-30 口径:入参为自然语言 query;user_id(代理人 ID)服务端注入(同
policy_search,§10.2 身份契约)。越权即 DENIED(不重试)。结果为结构化客户候选,
是「客户→保单」「客户→权益」两条组合链路的共享前置步(修订七);customer_id 为
后端标准标识(接口 4/6 可消费口径,真实出入参确认后校准)。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.envelope import make_envelope, to_fact
from app.schemas import EnvelopeStatus, Evidence, Fact, SourceRef
from app.schemas.enums import SourceType
from app.tools.backend import BackendClient, call_backend, error_envelope, error_outcome
from app.tools.backend_contracts import CustomerSearchResponse, parse_backend_response
from app.tools.mode import require_tool_execution_context, tool_execution_scope
from app.tools.search_source_projection import SOURCE_HINT, append_source_records, source_page_data
from tool_adapter_sdk.contracts import CUSTOMER_SEARCH, CustomerSearchRequest
from tool_adapter_sdk.customer_detail_metadata import has_alias_conflict
from tool_adapter_sdk.customer_search_metadata import group_source_fields, require_customer_id, source_text, source_value

IMPLEMENTED = True

# 空条件浏览的披露 gap（单一常量来源）：消费方（direct 渲染、policy_snapshot.locate
# 的放行判定）按本常量精确匹配，不做子串/正则猜测——与 direct.py 透出 hint 的纪律同款。
BROWSE_DISCLOSURE_GAP = "未识别到明确查询条件，以下为名下全部客户，未必满足提问中的隐含筛选"

_SOURCE = SourceRef(system="policy_svc", endpoint="/customer/search")

# 清单保留基础资料与已购产品；无值项不展示，未知数量仅在 hint 披露。
# 别名识别与冲突规则复用 SDK，不为展示层另设优先级或提前裁剪原值。
def _record_text(record: dict, path: str) -> str | None:
    """先按原值排除同源冲突，再仅对安全摘要裁剪首尾空白。"""
    value = source_text(record, path)
    return value.strip() if value is not None else None


def _customer_list_brief(name: str | None, record: dict, policy_count: int | None) -> str:
    """真实源页路径的清单条目:姓名(身份披露,缺失仍显式)·基础资料·已购产品·保单数。

    首段恒为姓名(render 模板的客户标题取「·」分隔首段);已购产品名单的分隔符
    统一为「/」(源值为全角分号)。
    保单数仅在候选确有计数时展示(真实路径恒 None 即省略,mock/有值路径保留)。"""
    segments = [name or "姓名未知"]
    if sex := _record_text(record, "clientsex"):
        segments.append(sex)
    age_value = source_value(record, "clientAge")
    age = str(age_value) if type(age_value) is int and age_value >= 0 else _record_text(record, "clientAge")
    if age:
        segments.append(f"{age}岁")
    if birthday := _record_text(record, "clientbirthday"):
        segments.append(birthday)
    if mobile := _record_text(record, "clientmobile"):
        segments.append(f"手机{mobile}")
    if client_no := _record_text(record, "clientno"):
        segments.append(f"客户号{client_no}")
    if plans := _record_text(record, "planabbrnames"):
        segments.append(f"已购{plans.replace('；', '/')}")
    if policy_count is not None:
        segments.append(f"名下 {policy_count} 张保单")
    return "·".join(segments)


@tool_execution_scope("customer_search")
async def customer_search(
    query: str,
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
    backend_client: BackendClient | None = None,
    upstream_user_id: str | None = None,
    agentno: str | None = None,
    session_id: str | None = None,
) -> str:
    """按自然语言搜索代理人名下客户,返回 str JSON 信封。"""
    text = query.strip()
    if not text:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供客户搜索条件")
    raw = await call_backend(
        CUSTOMER_SEARCH,
        CustomerSearchRequest(query=text),
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        upstream_user_id=upstream_user_id, agentno=agentno,
        client=backend_client, session_id=session_id,
    )
    if (outcome := error_outcome(raw)) is not None:
        return error_envelope(outcome, source=_SOURCE, raw=raw)

    parsed = parse_backend_response(CustomerSearchResponse, raw, source=_SOURCE)
    if isinstance(parsed, str):
        return parsed
    if parsed.unsupported_message is not None:
        return make_envelope(
            status=EnvelopeStatus.AMBIGUOUS, hint=parsed.unsupported_message,
            gaps=["筛选条件或排序未能可靠解析，尚未执行客户查询"],
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
        )
    data, descriptions = source_page_data(parsed.page.source_page if parsed.page is not None else None, "customer")
    limit = require_tool_execution_context().tool_config.result_limit or 5
    total = len(parsed.customers)
    customers = parsed.customers[:limit]
    if not customers:
        # EMPTY 的 hint 经 direct 分支直达用户正文（2026-09-20 起）：必须如实反映
        # 「查询已执行、零命中」，不得写成「请补充条件后重试」式的失败口径——
        # 「下个月过生日」类查询条件本身自足，零命中是数据真值。
        return make_envelope(
            status=EnvelopeStatus.EMPTY,
            hint=("本次条件查询未命中客户记录，可调整或补充查询条件后再查" if parsed.page is not None
                  else "名下未检索到相关客户，可调整或补充查询条件后再查"),
            source=_SOURCE, request_id=parsed.request_id, latency_ms=parsed.latency_ms,
            data=data, field_descriptions=descriptions,
        )

    facts: list[Fact] = []
    evidence: list[Evidence] = []
    # 候选与源记录按契约一一对应(顺序/计数一致);brief 的基础资料取自原始源页。
    records = (
        list(parsed.page.source_page.records)
        if parsed.page is not None and parsed.page.source_page is not None
        else []
    )
    for i, item in enumerate(customers):
        customer_id = item.customer_id
        if parsed.page is not None:
            brief = _customer_list_brief(
                item.name, records[i] if i < len(records) else {}, item.policy_count,
            )
        elif item.name is not None and item.policy_count is not None:
            brief = f"{item.name}·名下 {item.policy_count} 张保单"
        else:
            count_text = "保单数量未知" if item.policy_count is None else f"{item.policy_count} 张保单"
            brief = f"{item.name or '姓名未知'}·{count_text}"
        evidence_id = f"E-{parsed.request_id}-{i + 1:02d}"
        evidence.append(Evidence(
            evidence_id=evidence_id,
            source_type=SourceType.BUSINESS_API,
            source_id="policy_svc",
            retrieved_at=datetime.now(timezone.utc),
        ))
        facts.append(to_fact(
            predicate="customer_brief",
            typed_value=brief,
            subject_id=customer_id,
            subject_type="customer",
            source_call_id=parsed.request_id,
            identity_scope=agent_id,
            fact_id=f"F-{parsed.request_id}-{len(facts) + 1:03d}",
            evidence_refs=[evidence_id],
        ))
    truncated = total > limit
    gaps = [f"检索到 {total} 位客户，仅返回前 {limit} 位；请补充条件缩小范围"] if truncated else []
    hint = f"检索到 {total} 位名下客户，返回前 {limit} 位" if truncated else f"检索到 {total} 位名下客户"
    if parsed.page is not None:
        truncated = truncated or parsed.page.truncated
        hint = f"本次条件查询报告 {parsed.page.total} 位客户，当前页读取 {total} 位，展示 {len(customers)} 位"
        if any(item.name is None for item in customers):
            hint += "；部分客户姓名未知"
        if any(item.policy_count is None for item in customers):
            hint += "；保单数量未知，未提供不能解释为零"
        gaps = ["当前展示不包含完整客户集合，请补充条件缩小范围"] if truncated else []
    name_conflicts: set[str] = set()
    if parsed.page is not None and parsed.page.source_page is not None:
        for row_index, row in enumerate(parsed.page.source_page.records, 1):
            for path, values in group_source_fields(row).items():
                if has_alias_conflict(values):
                    customer_id = require_customer_id(row)
                    gaps.append(f"第 {row_index} 条客户记录同源字段冲突：{'、'.join(key for key, _ in values)}；"
                                "原值均保留，不选择单值事实")
                    if path == "clientName":
                        name_conflicts.add(customer_id)
    if name_conflicts:
        gaps.append("客户姓名字段冲突，现有按姓名定位保单的组合不可安全继续")
    if any(item.name is None and item.customer_id not in name_conflicts for item in customers):
        gaps.append("客户标识已知但姓名缺失，现有按姓名定位保单的组合不可安全继续")
    if parsed.page is not None and any(item.name is not None and "·" in item.name for item in customers):
        gaps.append("客户姓名包含展示分隔符，现有按姓名定位保单的组合无法无损读取姓名，不可安全继续")
    if parsed.page is None and any(item.name is None or item.policy_count is None for item in customers):
        hint = f"本次条件检索到 {total} 位客户，展示 {len(customers)} 位；部分姓名或保单数量未知"
    if parsed.browsed_all:
        # 仅原句明确浏览（「我名下有哪些客户」）才能到达此分支；有筛选但解析失败
        # 已在解析适配层停止。保留既有范围披露，避免消费者把浏览当作已筛选结果。
        # 信封契约禁止 SUCCESS 携带 gaps，故披露使状态为 PARTIAL；组合工作流
        # （policy_snapshot.locate）对「仅含本披露 gap 的 PARTIAL」显式放行——候选
        # 名单的完整性与安全性同 SUCCESS，截断/缺姓名等其他 gap 仍照常终态。
        gaps.append(BROWSE_DISCLOSURE_GAP)
        hint = f"未识别到明确查询条件，已按名下全部客户返回；{hint}"
    append_source_records(data, "customer", request_id=parsed.request_id, agent_id=agent_id,
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
