"""Evidence:来源定位(方案 v13 §10.4 · M0 冻结)。

RAG 证据带检索元数据——字段自 M1 就位,agentic RAG(M3)启用时零改协议;
business_api 来源时文档/检索字段为空。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from app.schemas.enums import SourceType


RetrievalGap = Literal["missing_source", "missing_index", "discarded_documents"]


class Evidence(BaseModel):
    """证据定位:来源系统 + 文档/块定位 + 检索元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(description="证据引用标识，供 Fact.evidence_refs 关联来源。")
    source_type: SourceType = Field(description="证据来源类型：business_api 业务接口、rag_document 检索文档或 qa_pair 问答对。")
    source_id: str = Field(description="来源系统或检索服务标识，如 product-rag 或 policy_svc。")
    document_id: str | None = Field(default=None, description="检索文档标识；业务接口来源或未提供文档标识时为 None。")
    chunk_id: str | None = Field(default=None, description="检索文档的块标识；未提供时为 None。")
    product_version: str | None = Field(default=None, description="产品条款版本，如 2019-03；未提供时为 None，不从正文猜测。")
    locator: str | None = Field(default=None, description="文内来源定位，如责任条款 §3.2；未提供时为 None。")
    bm25_score: float | None = Field(default=None, description="BM25 检索评分元数据；未提供时为 None，不解释为概率。")
    embedding_score: float | None = Field(default=None, description="向量检索评分元数据；未提供时为 None，不假定统一评分范围或概率含义。")
    rerank_score: float | None = Field(default=None, description="重排序评分元数据；未提供时为 None，具体口径由来源决定，不将原始得分自动视为已校准置信度。")
    # 原始检索元数据独立于已标定评分；缺省省略以保持旧信封输出。
    retrieval_index: StrictInt | StrictStr | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="上游本次召回标识，保留整数或字符串；不是稳定文档 ID，未提供时省略。",
    )
    source_url: str | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="上游来源链接，保留原始字符串，不自动访问；未提供时省略。",
    )
    raw_score: float | None = Field(
        default=None, allow_inf_nan=False, exclude_if=lambda value: value is None,
        description="未经标定的上游原始检索得分，必须为有限数值，不是概率；未提供时省略。",
    )
    recall_method: str | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="上游召回路径，如 keyword 或 bge；未提供时省略。",
    )
    retrieval_gaps: list[RetrievalGap] | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None,
        description="工具确认的检索质量缺口：文档来源缺失、上游序号缺失或部分条目已丢弃；非模型推断。",
    )
    retrieval_round: int = Field(default=1, description="检索轮次，默认第 1 轮；用于区分后续多轮检索。")
    query_rewrite: str | None = Field(default=None, description="多轮检索的查询改写文本；未使用或未提供时为 None。")
    retrieved_at: datetime = Field(description="取得该证据的时间，不是金额事实的业务时点。")
