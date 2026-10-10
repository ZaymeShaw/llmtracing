"""证据池按轮分桶的唯一入池单点(方案 v13 §8.1/§10.4 · D16,切片 5-b)。

facts/evidence 的权威产生方 = 工具信封;Direct / Agent 共用 dict 形态入池，
同轮按 fact_id/evidence_id 合并去重；BE-05 只保留本轮桶，跨轮选择依据为
known_subjects，旧事实不能重新进入当前引用校验。零 Agno 依赖。
"""
from __future__ import annotations

from app.schemas import Fact
from app.schemas.evidence import Evidence

# 仅本轮引用可核验；跨轮实体选择由 known_subjects 承载，旧 fact_id 不重新授权。
KEEP_TURNS = 1


def merge_into_pool(
    session_state: dict,
    trace_id: str,
    facts: list[Fact],
    evidence: list[Evidence],
) -> None:
    """把一次工具信封的 facts/evidence 合并进 by_turn[trace_id] 桶(去重保序)。"""
    pool = session_state.setdefault("evidence", {}).setdefault("by_turn", {})
    bucket = pool.setdefault(trace_id, {"facts": [], "evidence": []})
    if bucket.get("confirmed", True) is not True:
        return  # 迟到结果不能把已中断轮重新确认为成功，必须在新轮重新取证。
    known_fact_ids = {f["fact_id"] for f in bucket["facts"]}
    for fact in facts:
        dumped = fact.model_dump()
        if dumped["fact_id"] not in known_fact_ids:
            bucket["facts"].append(dumped)
            known_fact_ids.add(dumped["fact_id"])
    known_evidence_ids = {e["evidence_id"] for e in bucket["evidence"]}
    for item in evidence:
        dumped = item.model_dump(mode="json")
        if dumped["evidence_id"] not in known_evidence_ids:
            bucket["evidence"].append(dumped)
            known_evidence_ids.add(dumped["evidence_id"])
    for stale in list(pool):
        if stale != trace_id:
            del pool[stale]



def collect_pool(evidence_by_turn: dict) -> tuple[dict[str, Fact], dict[str, Evidence]]:
    """摊平按轮分桶的证据池(§8.1):fact_id/evidence_id → 契约对象。"""
    facts: dict[str, Fact] = {}
    evidences: dict[str, Evidence] = {}
    for bucket in (evidence_by_turn or {}).values():
        if bucket.get("confirmed", True) is not True:
            continue  # H-03：未确认材料不是 D16 可引用证据；开关回滚也不能复活。
        for raw in bucket.get("facts", []):
            fact = Fact.model_validate(raw)
            facts[fact.fact_id] = fact
        for raw in bucket.get("evidence", []):
            evidence = Evidence.model_validate(raw)
            evidences[evidence.evidence_id] = evidence
    return facts, evidences
