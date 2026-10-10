"""条款维度关键词(core 纯函数 · 工具 2 查询感知召回的规则口径)。

Owner 2026-09-01 契约:RAG 类工具入参 = 自然语言 query、接口按 query 裁剪召回。
本模块是「query → 请求条款维度」的规则单点，工具核心据此计算缺口。
命中集为空 = 未指明维度,
由调用方按全维度处理(缺省整卡口径)。
"""
from __future__ import annotations

# 维度码 → 口语关键词(与 ProductRagResponse 字段码一一对应)
CLAUSE_DIMENSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("waiting_period", ("等待期", "观察期")),
    # 不含裸「责任」——避免「责任免除」问法误扩到保险责任维度
    ("liability_summary", ("保险责任", "保障责任", "保障", "保什么", "赔付")),
    ("exclusion_clause", ("免责", "责任免除", "除外责任", "除外")),
    ("underwriting_rules", ("投保规则", "投保条件", "投保年龄", "投保要求",
                            "投保限制", "缴费期", "交费", "投保")),
)


def scan_clause_dimensions(query: str) -> tuple[str, ...]:
    """query 命中的条款维度码(按声明序去重);空元组 = 未指明维度。"""
    return tuple(
        code for code, keywords in CLAUSE_DIMENSIONS
        if any(word in query for word in keywords)
    )
