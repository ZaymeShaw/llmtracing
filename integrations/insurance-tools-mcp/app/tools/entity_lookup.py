"""entity_lookup 工具核心(§10.1 新增·本地:名称归属速查——产品 vs 服务/权益)。

Owner 2026-09-13 设计对齐:基座模型分不清 query 中的实体是寿险产品还是服务/
权益名称、导致工具选择困惑时,用本地词典(0 后端、0 LLM、毫秒级)做确定性确认。
定位是「存疑必查」的澄清工具:意图明确(问法自带领域语境/名称是强领域信号/
上下文已锁定域)时不必调用;确认归属后的取数仍由 product_knowledge /
benefit_info 等业务工具承载——本工具不产生业务事实、不参与路由。

双通道纪律(Owner 2026-09-13,详见 core/entity_dict 模块注释):名称通道三级
匹配且纯数字词条不入(防保单尾号/手机号误召回);编码通道(险种代码/权益编码/
服务编码)仅 by="code" 显式 exact 全串——传参即意图声明。
"""
from __future__ import annotations

from app import conf as app_conf
from app.core.envelope import make_envelope
from app.schemas import EnvelopeStatus

IMPLEMENTED = True

_MAX_ALIASES = 6  # 信封内单实体别名的展示上限(词典完整别名仍在产物中)


def _hit_payload(hit) -> dict:
    """EntityHit → 信封 data.hits 元素(按类型携带元数据,保持信封精简)。"""
    payload: dict = {"id": hit.entity_id, "type": hit.type, "name": hit.name}
    if hit.aliases:
        payload["aliases"] = list(hit.aliases[:_MAX_ALIASES])
    if hit.codes:
        payload["codes"] = list(hit.codes)
    if hit.sales_status:
        payload["sales_status"] = hit.sales_status
    if hit.period:
        payload["period"] = list(hit.period)
    if hit.families:
        payload["families"] = list(hit.families)
    return payload


async def entity_lookup(
    name: str = "",
    by: str = "name",
    *,
    agent_id: str,
    trace_id: str,
    budget: dict,
) -> str:
    """按名称或编码查实体归属(本地词典),返回 str JSON 信封。"""
    text = (name or "").strip()
    if not text:
        return make_envelope(status=EnvelopeStatus.EMPTY, hint="未提供待确认的名称")

    dictionary = app_conf.entity_dict()
    if (by or "name").strip().lower() == "code":
        hits = dictionary.lookup_code(text)
        if not hits:
            return make_envelope(
                status=EnvelopeStatus.EMPTY,
                hint=f"编码通道未收录「{text}」(仅全串精确匹配;词典版本 {dictionary.version})",
            )
        return make_envelope(
            status=EnvelopeStatus.SUCCESS,
            data={
                "query": text,
                "channel": "code",
                "tier": "code",
                "cross_domain": dictionary.cross_domain(hits),
                "hits": [_hit_payload(hit) for hit in hits],
                "version": dictionary.version,
            },
        )

    result = dictionary.lookup_name(text)
    if result is None:
        hint = f"词典未收录「{text}」(版本 {dictionary.version});可能是新名称或口语说法,不要凭空判断其归属"
        if text.isdigit():
            hint = f"「{text}」是纯数字,名称通道不查询数字;确属编码(险种代码/权益编码/服务编码)时用 by=\"code\" 重查"
        return make_envelope(status=EnvelopeStatus.EMPTY, hint=hint)
    if result.tier == "contains":
        return make_envelope(
            status=EnvelopeStatus.PARTIAL,
            gaps=[f"「{text}」未精确命中词典词条,以下为包含它的候选名称(按长度升序)"],
            hint="输入可能不完整;候选可直接用于业务工具或向用户确认",
            data={
                "query": text,
                "channel": "name",
                "tier": "contains",
                "candidates": list(result.contains_candidates),
                "version": dictionary.version,
            },
        )
    data: dict = {
        "query": text,
        "channel": "name",
        "tier": "exact",
        "cross_domain": dictionary.cross_domain(result.hits),
        "hits": [_hit_payload(hit) for hit in result.hits],
        "version": dictionary.version,
    }
    if result.related_members:
        data["related"] = {
            "family": result.related_family,
            "members": list(result.related_members),
        }
    gaps = None
    status = EnvelopeStatus.SUCCESS
    if result.truncated:
        status = EnvelopeStatus.PARTIAL
        gaps = [f"命中实体较多,仅展示前 {len(result.hits)} 个(词典版本 {dictionary.version})"]
    if not result.hits and result.related_members:
        # 系列名无直接词条:关联层如实呈现,主实体缺位即缺口。
        status = EnvelopeStatus.PARTIAL
        gaps = [f"「{text}」是服务系列名,无单一实体;其下关联成员见 data.related"]
    return make_envelope(status=status, data=data, gaps=gaps)
