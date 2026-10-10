"""模型可见工具的 Agno @tool 薄绑定层(方案 v13 §10.2 第①③层之间 · 切片 5-b/6-a)。

核心函数(app/tools/<tool>.py)保持零 Agno 依赖(修订六归档纪律);本层只做
通道转接:run_context.dependencies 的 agent_id/trace_id(POC-15 传播链)与
session_state 的 budget/entities 注入核心函数,工具语义与信封契约两层不变。
只绑定 IMPLEMENTED 的工具(Owner 纪律:占位不注册、不返回伪造信封)——既有
原子业务工具与两个清单呈现别名全部实现并注册(另有 run_policy_field_lookup
两条 Strict Workflow 薄工具,由 unified_agent 追加装配)。
身份契约(§10.2/§17):agent_id 只取服务端注入的 dependencies,模型与前端传入的
身份不作权限主体；/v1/chat 将同一工号注入 user_id/empno/agentno，模型不可传。
缓存纪律:全局绑定默认 cache_results=False；E-05 按轮克隆两类无状态知识工具启用
同用户同会话缓存，其余工具不变。同轮重复读取由项目 dedupe hook 处理。
"""
from __future__ import annotations

from functools import partial
from typing import Literal

from agno.run import RunContext
from agno.tools import tool

from tool_adapter_sdk.identity import identity_from_dependencies
from app.tools.ask_clarify import ask_clarify as ask_clarify_core
from app.tools.backend import BackendClient, backend_client_from_dependencies
from app.tools.benefit_info import benefit_info as benefit_info_core
from app.tools.customer_search import customer_search as customer_search_core
from app.tools.customer_detail import customer_detail as customer_detail_core
from app.tools.entity_lookup import entity_lookup as entity_lookup_core
from app.tools.general_qa import general_qa as general_qa_core
from app.tools.hooks import boundary_hook, turn_control_hook, metrics_hook, tool_body
from app.tools.policy_detail import fetch_policy_details
from app.core.envelope import make_envelope
from app.schemas import EnvelopeStatus, ToolError
from app.schemas.enums import ErrorCode
from app.tools.policy_search import policy_search as policy_search_core
from app.tools.product_knowledge import product_knowledge as product_knowledge_core
from app.tools.product_confirmation import bind_pending_product

# 与 orchestration/turn_workflow._LOCAL_TRACE 同语义的兜底键(分层只准向下,不 import 上层)
_LOCAL_TRACE = "TR-LOCAL"


def _state(run_context: RunContext) -> dict:
    """Return the exact shared state object, including a valid empty mapping."""
    state = run_context.session_state
    if state is None:
        raise RuntimeError("Tool session_state 未初始化")
    return state


def _deps(run_context: RunContext) -> tuple[str, str, dict, BackendClient | None]:
    """服务端注入的身份、trace、预算与共享 BackendClient 通道转接。"""
    deps = run_context.dependencies or {}
    state = _state(run_context)
    route = state.get("route") or {}
    trace_id = str(deps.get("trace_id") or route.get("trace_id") or _LOCAL_TRACE)
    return (
        str(deps.get("agent_id", "")),
        trace_id,
        state.setdefault("budget", {}),
        backend_client_from_dependencies(deps),
    )


@tool(cache_results=False)
@tool_body
async def product_knowledge(run_context: RunContext, query: str) -> str:
    """检索某一个具体产品的条款知识：保险责任、等待期、犹豫期、责任免除、投保规则、理赔责任。

    适用：问某款产品的条款口径。query 写产品名（可口语、简称）加关注点，一次一个产品；多产品客观对比时每款各调用一次。
    仅问“哪个好、性价比、怎么选”且没有客观维度时，先 ask_clarify 再检索。
    不用于：不针对具体产品的办理流程 → general_qa；权益或增值服务 → benefit_info；客户或保单数据 → 对应查询工具。
    名称未锁定时返回待确认候选（AMBIGUOUS）；本轮没有其他可答事实时用候选调用 ask_clarify，不改写问法或换工具重试。
    读结果：reference_content 是参考正文，不是结构化四维条款，有正文仍可能为 PARTIAL，也不保证全部属于该产品；
    召回序号不等于产品版本或条款定位。结果确有 product_version 或 locator 时答案必须携带；
    未提供时不得凭记忆补版本或条款号，也不补造停售日期。

    Args:
        query: 产品名加关注点的自足问题，如“平安福的等待期是多久？”；一次只查一个产品。
    """
    agent_id, trace_id, budget, backend_client = _deps(run_context)
    return await product_knowledge_core(
        query, agent_id=agent_id, trace_id=trace_id, budget=budget,
        backend_client=backend_client, session_state=_state(run_context),
    )


@tool(cache_results=False)
@tool_body
async def benefit_info(
    run_context: RunContext,
    query: str,
) -> str:
    """查询权益或增值服务的内容、门槛、使用方式、有效期及资格规则。

    适用：“就医绿通怎么用”“安有护有哪些服务”等权益/服务问题，可一次询问多个权益。
    不用于：某产品的条款口径 → product_knowledge；不针对产品的办理流程 → general_qa；
    客户或保单数据 → customer_search / customer_detail / policy_search / policy_detail。
    客户是否已享有某权益：本工具只给资格规则，不能证明某位客户已享有或符合；当前 customer_detail 未提供该关联，
    结论写“是否已享有未确认”。
    读结果：reference_content 是参考正文，不保证覆盖所有权益；raw_score 是未标定检索得分，recall_method 是召回路径，
    都不是客户资格或置信概率。健康、就医类权益只说明服务内容与办理口径，不构成医疗建议。

    Args:
        query: 包含权益或服务名称及关注点的自足问题，如“就医绿通怎么用？”；可询问多个权益。
    """
    agent_id, trace_id, budget, backend_client = _deps(run_context)
    return await benefit_info_core(
        query,
        agent_id=agent_id, trace_id=trace_id, budget=budget,
        backend_client=backend_client,
    )


@tool(cache_results=False)
@tool_body
async def general_qa(run_context: RunContext, query: str) -> str:
    """检索没有专用工具承接的通用寿险办理问答：理赔材料与流程、保全（退保、复效、地址或受益人变更、保单贷款）、缴费等。

    不用于：某个具体产品的条款口径（含该产品的犹豫期、等待期、责任免除、投保规则、理赔责任）→ product_knowledge；
    权益或增值服务 → benefit_info；客户或保单数据 → 对应查询工具。
    读结果：qa_answer 是召回参考正文，真实接口无评分，不要编造相关度；召回序号不是原问题或稳定 QA ID。
    政策、流程和时效类内容可能调整，回答须提示以保险公司现行官方规定为准。

    Args:
        query: 指代消解后的自足问句，如“犹豫期内退保怎么办理”。
    """
    agent_id, trace_id, budget, backend_client = _deps(run_context)
    return await general_qa_core(
        query, agent_id=agent_id, trace_id=trace_id, budget=budget,
        backend_client=backend_client,
    )


@tool(cache_results=False)
@tool_body
async def policy_search(
    run_context: RunContext, query: str, compact: bool = True, fields: list[str] | None = None,
) -> str:
    """按自然语言条件查找保单，返回匹配保单的搜索页记录（已含保单状态、投被保人、保费、保额、犹豫期、理赔记录等常用字段）。

    适用：列出、筛选、计数名下保单，或读取、比较多张保单的字段。
    不用于：已有保单号查该保单 → policy_detail；无保单号且只针对需要锁定的某一张保单 → run_policy_field_lookup。
    query 须自足并写全筛选条件（内置解析发送空历史）；条件不支持时返回 AMBIGUOUS 且未执行查询，不得删条件扩大范围重试。
    读结果：polno 是保单号，同一保单号可能有多条记录（险种行）；状态、投保人、被保人等须同号各行一致才能作为保单级结论，险种代码不等于产品名。
    data.total 为符合查询条件的结果总条数，是匹配条目数而非保单张数；完整页的保单张数使用 execution.distinct_policies；没有该值时明确说明张数未确认。
    sum_ins 为原始险种保额、agg_paid_prem 为累计已交保费、agg_pay_cnt 为累计赔付次数，不替代当前金额或跨险种汇总；
    金额只按原值表达，币种和业务时点未提供时不得补“元/万元/CNY/截至今天”。
    按问题从 data 选择字段作答，field_descriptions 给出字段含义；返回候选集合不保证字段齐全，execution 披露截断、未取全与缺失。
    缺上游业务 user_id 时返回 ERROR，模型不得提供或以客户标识替代。

    Args:
        query: 包含全部筛选条件的自足问题，如“张三名下的保单”“买过年金险的保单”。
        compact: 默认 true，省略 null 字段（不代表零、否或不存在），只展示搜索页核心字段和问题关注字段；需要本页全部可展示字段时设为 false，不改变查询条件。
        fields: compact 模式下只展示主键和指定字段，英文名或中文名均可；未传时使用默认字段。如 保单状态、被保人姓名、投保人姓名、年缴保费、险种保额、累计已交保费、犹豫期、理赔记录；不改变筛选，也不补查详情。
    """
    return await _policy_search(run_context, query)


async def _policy_search(run_context: RunContext, query: str) -> str:
    """保单搜索绑定共享执行体；普通取证与终止呈现只在控制语义上不同。"""
    agent_id, trace_id, budget, backend_client = _deps(run_context)
    return await policy_search_core(
        query, agent_id=agent_id, trace_id=trace_id, budget=budget,
        backend_client=backend_client, session_id=run_context.session_id, contexts=None,
        **identity_from_dependencies(run_context.dependencies, "policy_search"),
    )


@tool(cache_results=False)
@tool_body
async def customer_search(
    run_context: RunContext, query: str, compact: bool = True, fields: list[str] | None = None,
) -> str:
    """按自然语言条件查找客户，返回匹配客户的搜索页记录。

    适用：按姓名、手机号、寿险客户号、性别、生日、学历、职业、婚姻、资产、证件、会员或分群标签、保单号或尾号、家庭关系等条件定位、筛选、计数客户。
    不用于：目标是保单本身 → policy_search；已有客户中心ID要完整画像 → customer_detail。
    不支持年龄数值比较或区间、跨年生日区间、地址、排序及未登记字段；任一条件不支持时整条不查询，返回 AMBIGUOUS（不是零命中），
    不得删条件扩大范围。无筛选条件时只在用户明确要求浏览时查询首页。
    询问符合条件的客户数量时直接引用 data.total 作答；data.total 为符合查询条件的客户总数，不是当前页记录数或展示条数；保单数量未知不等于零。
    业务客户号是搜索条件，不等于内部标准标识 customer_id；只知道姓名、手机号或业务客户号时先调用本工具，再用搜索结果返回的 customer_id 查详情：
    data.records[].clientMainId（兼容 client_main_id、idicm_clientno）即客户中心ID，寿险客户号 clientNo 不是。
    读结果：准客和已成单客户都有客户中心ID，不代表已购保险；姓名（clientName，兼容 searchclientname）只指客户本人；
    嵌套的保单、家庭、权益资料不证明完整关联或已享资格。已返回字段直接按 data 作答，field_descriptions 给出字段含义，不必再次查询。

    Args:
        query: 包含全部筛选条件的自足问题，如“姓张的女性客户”“手机号尾号1234的客户”；业务客户号不能直接作为 customer_detail 的 customer_id。
        compact: 默认 true，省略 null 字段（不代表零、否或不存在），只展示客户核心资料、保单概况和问题关注字段；需要本页全部源字段时设为 false，不改变查询条件。
        fields: compact 模式下展示主键、姓名和指定字段，英文路径或中文名均可，如 手机号、学历、职业、姓名、生日、VIP、客户等级；未传或全部未识别时使用默认字段；不改变筛选，也不补查详情。
    """
    return await _customer_search(run_context, query)


async def _customer_search(run_context: RunContext, query: str) -> str:
    """客户搜索绑定共享执行体；普通取证与终止呈现只在控制语义上不同。"""
    agent_id, trace_id, budget, backend_client = _deps(run_context)
    return await customer_search_core(
        query, agent_id=agent_id, trace_id=trace_id, budget=budget,
        backend_client=backend_client, session_id=run_context.session_id,
        **identity_from_dependencies(run_context.dependencies, "customer_search"),
    )


@tool(cache_results=False)
@tool_body
async def present_policy_search_card(run_context: RunContext) -> str:
    """用本轮用户原话搜索保单并直接展示清单卡片；查到可展示记录后本轮立即结束，你不会看到结果。
    未命中或条件不支持时会返回结果，请据此作答，不要用同一条件再查。

    仅当本轮完整目标就是保单清单、且用户原话本身已写全筛选条件时使用；须单独调用，不与任何其他工具并行或先后混用。
    原话需要结合上文才能理解（如“那李四的呢”“只看有效的”），或还需回答字段、数量、解释、比较等，
    改用 policy_search 并在 query 中写全条件。
    """
    query = str((_state(run_context).get("route") or {}).get("user_text") or "").strip()
    return await _policy_search(run_context, query)


@tool(cache_results=False)
@tool_body
async def present_customer_search_card(run_context: RunContext) -> str:
    """用本轮用户原话搜索客户并直接展示清单卡片；查到可展示记录后本轮立即结束，你不会看到结果。
    未命中或条件不支持时会返回结果，请据此作答，不要用同一条件再查。

    仅当本轮完整目标就是客户清单、且用户原话本身已写全筛选条件时使用；须单独调用，不与任何其他工具并行或先后混用。
    原话需要结合上文才能理解（如“那女性的呢”），或还需回答客户属性、详情、数量、解释、比较等，
    改用 customer_search 并在 query 中写全条件。
    """
    query = str((_state(run_context).get("route") or {}).get("user_text") or "").strip()
    return await _customer_search(run_context, query)


# Agno 对零参数函数默认省略 additionalProperties；显式收紧后模型协议层也无法
# 塞入改写后的 query（运行时本就只读服务端 route.user_text）。
present_policy_search_card.process_schema_for_strict()
present_customer_search_card.process_schema_for_strict()


@tool(cache_results=False)
@tool_body
async def policy_detail(
    run_context: RunContext, polno: str, fields: list[str] | None = None, compact: bool = True,
) -> str:
    """按保单号查询该保单的详情记录。

    适用：已有完整保单号（P/HP/GP 开头加字母数字，如 P002）或至少三位纯数字尾号；尾号多匹配时返回候选，须确认完整号。
    多张保单已有列表字段直接使用 policy_search；只有确需缺失字段才逐张 policy_detail。
    犹豫期是本保单字段；等待期、免责条款使用 product_knowledge。
    不用于：按自然语言条件搜索多张保单 → policy_search；没有保单号 → policy_search 或 run_policy_field_lookup。
    只能查询用户本轮提供、搜索结果或会话已确认的保单号。
    读结果：按 data.records 的险种记录回答，field_descriptions 给出字段含义；同一保单多行字段冲突时不能合成保单级结论；险种代码不等于产品名。
    金额只按原值表达，例如“原始险种保额120,000.00，币种和业务时点未提供”；不得补“元/万元/CNY/截至今天”，不用累计值冒充当前金额。
    PARTIAL 表示已命中但所问字段缺失；精简省略不等于源数据缺失；EMPTY 表示确实无记录。

    Args:
        polno: 完整保单号（P/HP/GP 开头加字母数字）或至少三位纯数字尾号，不得传自然语言。
        fields: 默认视图追加关注字段，英文名或中文名均可，如 保单状态、被保人姓名、投保人姓名、年缴保费、险种保额；只控制展示，不改变取数。
        compact: 默认 true，省略 null 字段（不代表零、否或不存在），只展示核心字段与关注字段；需要本页全部可展示字段时设为 false。
    """
    return await _policy_detail(run_context, polno, fields=fields, compact=compact)


async def _policy_detail(run_context: RunContext, polno: str, fields: list[str] | None = None,
                         compact: bool = True) -> str:
    """Shared ordinary/redirect entry; identity checks and raw results stay identical."""
    agent_id, trace_id, budget, backend_client = _deps(run_context)
    dependencies = run_context.dependencies or {}

    def _valid(key: str) -> bool:
        value = dependencies.get(key)
        return isinstance(value, str) and bool(value.strip())

    if not (_valid("upstream_user_id") or _valid("empno")):
        return make_envelope(
            status=EnvelopeStatus.DENIED, hint="缺少本轮代理人工号，无法查询保单详情",
            error=ToolError(code=ErrorCode.DENIED, retryable=False),
        )
    if not polno.strip():
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供保单号")
    from app.tools.mode import visible_policy_revalidation
    state = run_context.session_state or {}
    visible = [*((state.get('route') or {}).get('visible_policy_refs') or []), *((state.get('route') or {}).get('confirmed_user_policy_refs') or [])]
    # Visibility admits a locator, not an ownership fact. Cards, focused_policy
    # and Agent hints may originate in upstream text; only retained tool evidence
    # can skip revalidation of a historical reference.
    confirmed = {str(fact.get('subject_id') or '').upper()
        for bucket in ((state.get('evidence') or {}).get('by_turn') or {}).values()
        if bucket.get('confirmed', True) is True
        for fact in bucket.get('facts') or [] if fact.get('subject_type') == 'policy'}
    revalidate = polno.upper() in visible and polno.upper() not in confirmed
    token = visible_policy_revalidation.set(revalidate)
    try:
        return await fetch_policy_details(
            polno, None, agent_id=agent_id, trace_id=trace_id, budget=budget,
            backend_client=backend_client,
            **identity_from_dependencies(dependencies, "policy_detail"),
        )
    finally:
        visible_policy_revalidation.reset(token)



@tool(cache_results=False)
@tool_body
async def customer_detail(
    run_context: RunContext, customer_id: str | None = None, compact: bool = True, fields: list[str] | None = None,
    customer_ids: list[str] | None = None,
) -> str:
    """按客户中心ID查询客户详情。

    一个客户传 customer_id，返回完整画像；多个客户传 customer_ids（最多 100 个），只返回客户中心ID、姓名、性别、年龄
    四项基本候选，其余字段未查询。需要其中某人完整画像且目标明确时，再用其 customer_id 单独查询。
    姓名、手机号、寿险客户号不能当客户中心ID，先用 customer_search 定位；多个候选且目标不明确时先消歧，不擅自选择。
    读结果：birthday 是生日相关日期，不能按年份重算年龄；字段名含 Cipher 仅为接口命名，值可读即按原值展示，不自行解密；
    空值不等于零或否，会员等级不证明已享某项权益；不补造金额单位或时点。

    Args:
        customer_id: 单个客户的客户中心ID（customer_search 结果中的 clientMainId）。
        compact: 默认 true，省略 null 字段（不代表零、否或不存在），只展示核心信息；false 保留全部原值。
        fields: 默认视图追加关注字段，英文路径或中文名均可，只控制展示。
        customer_ids: 多个客户的客户中心ID，最多 100 个；只返回基本候选。
    """
    return await _customer_detail(run_context, customer_id, compact=compact, fields=fields, customer_ids=customer_ids)


async def _customer_detail(run_context: RunContext, customer_id: str | None = None, compact: bool = True,
                           fields: list[str] | None = None, customer_ids: list[str] | None = None) -> str:
    agent_id, trace_id, budget, backend_client = _deps(run_context)
    return await customer_detail_core(
        customer_id, customer_ids=customer_ids,
        agent_id=agent_id, trace_id=trace_id, budget=budget, backend_client=backend_client,
        **identity_from_dependencies(run_context.dependencies, "customer_detail"),
    )


async def execute_selection_target(run_context: RunContext, name: str, arguments: dict) -> str:
    """Internal closed dispatch, called only through turn control and target metrics.

    Arguments come from resolve_redirect (typed, no extra keys). Projection-only
    fields/compact do not change the shared search execution body.
    """
    if name == 'policy_detail':
        return await _policy_detail(run_context, **arguments)
    if name == 'customer_detail':
        return await _customer_detail(run_context, **arguments)
    if name == 'policy_search':
        return await _policy_search(run_context, arguments['query'])
    raise ValueError('Unsupported selection target')


@tool(cache_results=False)
@tool_body
async def entity_lookup(run_context: RunContext, name: str, by: Literal["name", "code"] = "name") -> str:
    """从本地词典查名称归属（产品还是权益/服务），帮助选择 product_knowledge 或 benefit_info；不返回条款或权益详情。

    在以下情况调用：只有名称没有领域语境（如“御享国医怎么样”）、名称不认识，或名称可能同时是产品和服务。
    问法已明确领域（如“平安福的等待期”“就医绿通怎么用”）时直接调用业务工具。
    命中同时含 product 与 service/benefit 时是跨域同名，须结合语境选择或澄清。
    只返回名称归属和别名，不提供销售状态或停售日期结论；未收录返回 EMPTY，不据此猜测归属。

    Args:
        name: 待确认的名称或编码，名称可为口语、简称或别称，如“金越”；保单号、手机号不要传入。
        by: name（默认，名称匹配）或 code（险种、权益、服务编码的全串精确匹配，以词典返回的完整编码为准）。
    """
    agent_id, trace_id, budget, _ = _deps(run_context)
    return await entity_lookup_core(
        name, by,
        agent_id=agent_id, trace_id=trace_id, budget=budget,
    )


@tool(cache_results=False)
@tool_body
async def ask_clarify(
    run_context: RunContext, question: str, options: list[str] | None = None
) -> str:
    """关键目标或实体不明确时向用户发起澄清，登记挂起并结束本轮；须单独调用。

    适用：取证前发现关键歧义，或工具返回待确认候选且本轮尚无可回答的事实。
    本轮已取得可回答的事实或正文后不得调用，应先作答并把未完成项写入 gaps。
    返回 NEEDS_CLARIFICATION 后本轮结束；连续开放澄清达上限时返回 ERROR 并转人工。

    Args:
        question: 务必填写：一句话说明需要用户确认什么，如“请问要查询哪张保单？”。
        options: 候选标签，最多 5 个，须与工具返回的候选一致；超过 5 个时先缩小范围。开放性问题可不传。
    """
    agent_id, trace_id, budget, _ = _deps(run_context)
    state = _state(run_context)
    result = await ask_clarify_core(
        question, options,
        session_state=state,
        agent_id=agent_id, trace_id=trace_id, budget=budget,
    )
    # 只关联已登记的产品候选；挂起结构与生命周期仍归 core/clarify 和 bridge。
    bind_pending_product(state, agent_id=agent_id, trace_id=trace_id)
    return result


# 原子工具固定清单；Workflow 薄工具由 runtime/unified_agent 按共享 DB 追加装配。
AGENT_TOOLS = [
    product_knowledge,
    benefit_info,
    general_qa,
    policy_search,
    customer_search,
    present_policy_search_card,
    present_customer_search_card,
    policy_detail,
    customer_detail,
    entity_lookup,
    ask_clarify,
]

# 三个原生hook：模型边界、轮级控制、真实执行计量。
AGENT_TOOL_HOOKS = [
    boundary_hook, turn_control_hook,
    partial(metrics_hook, envelope_tools=frozenset(tool.name for tool in AGENT_TOOLS)),
]
