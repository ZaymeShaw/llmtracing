"""内外网工具实现共享的纯 Python 契约。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from tool_adapter_sdk.customer_detail_v5 import CustomerDetailV5Fields
from tool_adapter_sdk.customer_detail_fields import CustomerDetailFields
from tool_adapter_sdk.customer_detail_metadata import source_text
from tool_adapter_sdk.customer_search_metadata import require_customer_id, source_text as search_source_text
from tool_adapter_sdk.json_values import StrictJsonValue
from tool_adapter_sdk.policy_detail_fields import PolicyDetailFields
from tool_adapter_sdk.search_source_pages import CustomerSearchSourcePageDto, PolicySearchSourcePageDto

HANDLER_API_VERSION = "3"

ResultT = TypeVar("ResultT")
NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@dataclass(frozen=True)
class HandlerCallResult(Generic[ResultT]):
    """handler 返回值及其已发生的内部重试次数。

    普通 handler 仍可直接返回 Result DTO；只有发生过内部有限重试时才需要
    使用本包装，避免把运行元数据混入业务 Result 契约。
    """

    result: ResultT  # 业务结果 DTO，不包含额外运行元数据。
    retries: int = 0  # 实际已发生的重试次数，零表示未重试。

    def __post_init__(self) -> None:
        if isinstance(self.retries, bool) or not isinstance(self.retries, int) or self.retries < 0:
            raise ValueError("retries 必须是非负整数")


class ContractModel(BaseModel):
    """SDK DTO 基类：边界严格，避免内网字段被静默吞掉。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class BackendContext:
    """由可信运行时构造的身份与链路上下文。"""

    tool_name: str  # 当前工具名称，用于 operation 归属和计量。
    agent_id: str  # 认证权限主体；客户解析将其原值作为 user_id。
    trace_id: str  # 本轮服务端链路标识，不是客户或用户主键。
    request_id: str  # 单次逻辑后端调用标识，用于计量与溯源。
    session_id: str | None = None  # 原生会话标识；不是认证身份，不用 trace_id 代替。
    # 工号供查询/详情及保单解析消费，独立 SDK 调用方须从可信身份注入。
    upstream_user_id: str | None = None  # 本轮代理人工号；/v1/chat 从已核对的身份统一注入。
    empno: str | None = None  # 保单查询工号，与 upstream_user_id 相同时才可同时提供。
    agentno: str | None = None  # 客户查询工号，与 upstream_user_id 相同时才可同时提供。


class PolicyCandidateDto(ContractModel):
    policy_no: str = Field(min_length=1, description="保单号；不是险种行唯一主键。")
    insured_name: str = Field(description="被保人姓名，real 来自 insurant_name；同号险种行缺失或冲突时为空字符串。")
    product_name: str = Field(description="产品名称；真实搜索未提供时为空，不以险种代码替代。")
    status: str = Field(default="", description="保单状态；空字符串表示未提供，不能推断有效或失效。")


class PolicyDetailRequest(ContractModel):
    policy_input: NonBlankText = Field(description="完整号或尾号；内置 real 仅将 P＋数字视作完整号，纯数字走尾号定位。")


class KnowledgeDocumentDto(ContractModel):
    """未经评分或结构化抽取的知识正文；来源与召回序号原样保留。"""

    index: StrictInt | None = Field(default=None, ge=1, exclude_if=lambda value: value is None, description="上游召回序号，从 1 起；缺失为 None，不补造序号。")
    source: str | None = Field(default=None, min_length=1, strict=True, exclude_if=lambda value: value is None, description="文档来源；缺失为 None，消费者须披露来源缺失。")
    context: str = Field(min_length=1, strict=True, description="召回正文；FAQ 含标题内容，产品为同来源合并正文，不是结构化条款。")

    @model_validator(mode="after")
    def validate_content(self) -> "KnowledgeDocumentDto":
        if (self.source is not None and not self.source.strip()) or not self.context.strip():
            raise ValueError("知识来源与正文不能为空白")
        return self


class DocumentDiagnosticsDto(ContractModel):
    """保留条目的原响应位置与丢弃计数，不冒充上游召回序号。"""

    considered_count: StrictInt = Field(gt=0, description="截取后实际逐条校验的条数，严格正整数。")
    discarded_count: StrictInt = Field(ge=0, description="校验失败而丢弃的条数，不包含超量尾部。")
    positions: list[Annotated[int, Field(strict=True, ge=1)]] = Field(
        strict=True, description="保留文档的原响应位置，从 1 起、升序唯一，与上游 index 独立。",
    )

    @model_validator(mode="after")
    def validate_counts(self) -> "DocumentDiagnosticsDto":
        if (self.positions != sorted(set(self.positions))
                or any(position > self.considered_count for position in self.positions)
                or self.considered_count != self.discarded_count + len(self.positions)):
            raise ValueError("文档诊断计数与原位置不一致")
        return self


def _validate_document_diagnostics(
    documents: list[KnowledgeDocumentDto] | list[BenefitDocumentDto] | None,
    diagnostics: DocumentDiagnosticsDto | None,
) -> None:
    if diagnostics is not None and (not documents or len(documents) != len(diagnostics.positions)):
        raise ValueError("文档诊断必须对应非空保留正文及相同位置数量")


class ProductResolveRequest(ContractModel):
    query: NonBlankText = Field(description="必填自然语言问题；去除首尾空白后不能为空。")


class ProductCandidateDto(ContractModel):
    name: str = Field(min_length=1, description="候选产品全称。")
    intro: str = Field(description="候选介绍；real 拼接解析元数据，不等于已确认产品条款。")
    product_code: NonBlankText | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="上游险种代码；未知为 None，不等于产品 ID 级过滤。",
    )


class ProductResolveResult(ContractModel):
    match: Literal["exact", "fuzzy", "empty"] = Field(description="产品匹配三态：精确、模糊待确认、未匹配。")
    standard_name: str = Field(description="精确命中的产品全称；非 exact 时为空。")
    candidates: list[ProductCandidateDto] = Field(description="模糊候选；exact 和 empty 时为空列表。")
    product_code: NonBlankText | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="上游险种代码；未知为 None，不等于产品 ID 级过滤。",
    )

    @model_validator(mode="after")
    def validate_match_shape(self) -> "ProductResolveResult":
        if self.match == "exact" and (not self.standard_name or self.candidates):
            raise ValueError("exact 必须有且只能有 standard_name")
        if self.match == "fuzzy" and (self.standard_name or not self.candidates):
            raise ValueError("fuzzy 必须有 candidates 且不得有 standard_name")
        if self.match != "exact" and self.product_code is not None:
            raise ValueError("未锁定产品时不得携带 product_code")
        if self.match == "empty" and (self.standard_name or self.candidates):
            raise ValueError("empty 不得携带匹配结果")
        return self


class ProductRagRequest(ContractModel):
    query: NonBlankText = Field(description="必填自然语言问题；去除首尾空白后不能为空。")
    standard_name: NonBlankText = Field(description="已锁定产品全称；real 用作检索提示，不是硬过滤。")
    product_code: NonBlankText | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="上游险种代码；未知为 None，不等于产品 ID 级过滤。",
    )


class ProductRagResult(ContractModel):
    standard_name: str = Field(min_length=1, description="已锁定的检索产品名，不证明全部正文属于该产品。")
    found: bool = Field(description="是否有内容；正文分支须与 documents 非空一致。")
    product_version: str | None = Field(default=None, description="旧结构化条款版本；real 正文未提供，不能编造。")
    waiting_period: str | None = Field(default=None, description="旧结构化等待期；None 表示未提供。")
    liability_summary: str | None = Field(default=None, description="旧结构化保险责任摘要；None 表示未提供。")
    exclusion_clause: str | None = Field(default=None, description="旧结构化责任免除；None 表示未提供。")
    underwriting_rules: str | None = Field(default=None, description="旧结构化投保／核保规则；None 表示未提供。")
    documents: list[KnowledgeDocumentDto] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 知识正文；None 为旧分支，空列表为无召回，与结构化字段互斥。",
    )

    document_diagnostics: DocumentDiagnosticsDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="发生丢弃或缺来源／序号时提供的文档质量诊断；正常结果省略。",
    )

    @model_validator(mode="after")
    def validate_documents(self) -> "ProductRagResult":
        if self.documents is not None:
            if any(value is not None for value in (
                self.product_version, self.waiting_period, self.liability_summary,
                self.exclusion_clause, self.underwriting_rules,
            )):
                raise ValueError("产品正文与结构化条款不得混合返回")
            if self.found != bool(self.documents):
                raise ValueError("产品正文与 found 状态不一致")
        _validate_document_diagnostics(self.documents, self.document_diagnostics)
        return self


class BenefitDetailRequest(ContractModel):
    query: NonBlankText = Field(description="必填自然语言问题；去除首尾空白后不能为空。")


class BenefitItemDto(ContractModel):
    standard_name: str = Field(min_length=1, description="旧结构化标准权益名称。")
    content: str | None = Field(default=None, description="权益服务内容；None 为未提供。")
    threshold: str | None = Field(default=None, description="享受门槛；不是指定客户资格已验证。")
    usage: str | None = Field(default=None, description="使用方式；None 为未提供。")
    validity: str | None = Field(default=None, description="有效期；None 为未提供。")
    eligibility: str | None = Field(default=None, description="资格条件说明；不能推断客户已享有。")


class BenefitDocumentDto(ContractModel):
    """权益检索的原始正文和元数据，不将检索得分解释为置信度。"""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_by_name=True, serialize_by_alias=True)

    index: Annotated[int, Field(strict=True, ge=1)] | Annotated[str, Field(strict=True, min_length=1)] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="本次召回标识；当前兼容整数或非空字符串，不是稳定文档 ID。",
    )
    source: str | None = Field(default=None, min_length=1, strict=True, exclude_if=lambda value: value is None, description="权益文档来源；缺失为 None，不能补造来源。")
    context: str = Field(min_length=1, strict=True, description="权益召回正文，含标题与内容，不保证五要素完整。")
    url: str = Field(strict=True, description="权益来源链接；保留字符串，不自动访问。")
    score: float = Field(strict=True, allow_inf_nan=False, description="原始检索得分；未标定，不是概率或已校准置信度。")
    recall_method: str = Field(
        alias="recallMethod", min_length=1, strict=True,
        description="上游 recallMethod 召回路径，如 keyword/bge；当前保留非空字符串。",
    )

    @model_validator(mode="after")
    def validate_content(self) -> "BenefitDocumentDto":
        values = [self.context, self.recall_method]
        if self.source is not None:
            values.append(self.source)
        if isinstance(self.index, str):
            values.append(self.index)
        if any(not value.strip() for value in values):
            raise ValueError("权益文档标识、来源、正文和召回路径不能为空白")
        return self


class BenefitDetailResult(ContractModel):
    results: list[BenefitItemDto] = Field(description="旧结构化权益列表；real 正文分支为空。")
    unknown: list[str] = Field(description="旧分支未匹配权益名称；real 不从正文猜测未匹配名称。")
    documents: list[BenefitDocumentDto] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 权益正文；None 为旧分支，空列表为无召回。",
    )

    document_diagnostics: DocumentDiagnosticsDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="发生丢弃或缺来源／序号时提供的文档质量诊断；正常结果省略。",
    )

    @model_validator(mode="after")
    def validate_representation(self) -> "BenefitDetailResult":
        if self.documents is not None and (self.results or self.unknown):
            raise ValueError("结构化权益结果和检索正文不得混合返回")
        _validate_document_diagnostics(self.documents, self.document_diagnostics)
        return self


class QaRecallRequest(ContractModel):
    query: NonBlankText = Field(description="必填自然语言问题；去除首尾空白后不能为空。")
    top_n: StrictInt = Field(ge=1, le=20, description="最多召回条数；SDK 限 1～20，模型不可传，取工具配置。")


class QaPairDto(ContractModel):
    question: str = Field(description="旧结构化 QA 原问题。")
    answer: str = Field(description="旧结构化 QA 答案。")
    score: float = Field(ge=0.0, le=1.0, description="旧 QA 相关度分数 0～1；real 无评分正文不使用此字段。")


class QaRecallResult(ContractModel):
    pairs: list[QaPairDto] = Field(description="旧评分 QA 对列表；real 正文分支为空。")
    documents: list[KnowledgeDocumentDto] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 无评分 FAQ 正文；None 为旧分支，空列表为无召回。",
    )

    document_diagnostics: DocumentDiagnosticsDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="发生丢弃或缺来源／序号时提供的文档质量诊断；正常结果省略。",
    )

    @model_validator(mode="after")
    def validate_representation(self) -> "QaRecallResult":
        if self.documents is not None and self.pairs:
            raise ValueError("QA 对和知识正文不得混合返回")
        _validate_document_diagnostics(self.documents, self.document_diagnostics)
        return self


class PolicySearchContextDto(ContractModel):
    """保单解析的安全历史消息，角色标记遵循上游协议。"""

    role: Literal["user", "assistant"] = Field(description="历史消息角色；只接受 user 或 assistant。")
    content: str = Field(strict=True, min_length=1, description="历史正文；内置 real HTTP 不发送该历史。")
    sub_agent: Literal["", "QA_AGENT"] = Field(description="用户消息为空，助手消息为 QA_AGENT。")

    @model_validator(mode="after")
    def validate_message(self) -> "PolicySearchContextDto":
        expected = "" if self.role == "user" else "QA_AGENT"
        if not self.content.strip() or self.sub_agent != expected:
            raise ValueError("历史消息正文或角色标记无效")
        return self


class PolicySearchRequest(ContractModel):
    query: NonBlankText = Field(description="必填自然语言问题；去除首尾空白后不能为空。")
    contexts: list[PolicySearchContextDto] | None = Field(
        default=None, strict=True, exclude_if=lambda value: value is None,
        description="SDK 兼容历史；内置解析服务固定发送空 contexts，不据此声称历史已传递。",
    )


class PolicySearchRecordDto(ContractModel):
    """候选兼容投影；不是实际响应字段白名单，全量记录见 source_page。"""

    polno: str = Field(strict=True, min_length=1, description="保单号；同号可有多条险种记录。")
    applicant_name: str | None = Field(default=None, strict=True, description="投保人姓名，不可代替被保人姓名。")
    insurant_name: str | None = Field(default=None, strict=True, description="被保人姓名；None 为未知，多行冲突时不选取其中之一。")
    sum_ins: str | None = Field(
        default=None, strict=True,
        description="险种保额（字典第43行）；HTTP 保留金额字符串/千分位，币种、时点及跨险种聚合口径未确认。",
    )
    insure_date: str | None = Field(default=None, strict=True, description="投保日期原始字符串；不是金额的业务时点。")
    pol_source: str | None = Field(default=None, strict=True, description="保单来源（字典第3行，如寿险、产险-车险）；保留原始字符串，不推导产品或授权。")
    plan_code: str | None = Field(default=None, strict=True, description="险种代码，不等于产品名称。")
    agg_paid_prem: Annotated[int, Field(strict=True)] | Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = Field(
        default=None,
        description="累计已交保费（即累计已缴保费，字典第46行），HTTP 源 decimal(38,2)；币种、时点及跨险种聚合口径未确认。",
    )
    agg_pay_cnt: Annotated[int, Field(strict=True)] | Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = Field(
        default=None,
        description="累计赔付次数（字典第72行），HTTP 源 decimal(38,0)；不是缴费次数，不自行跨险种求和。",
    )

    @model_validator(mode="after")
    def validate_identity(self) -> "PolicySearchRecordDto":
        if not self.polno.strip():
            raise ValueError("保单号不能为空白")
        return self


class PolicySearchPageDto(ContractModel):
    """单页搜索条目计数；旧接口为险种行，新版粒度待确认。"""

    record_total: StrictInt = Field(ge=0, description="服务报告的搜索条目总数；旧接口为险种行，新版粒度待确认，不是已确认的去重保单张数。")
    records: list[PolicySearchRecordDto] = Field(max_length=100, description="当前搜索首页记录，最多 100 条，不自动翻页。")
    truncated: bool = Field(strict=True, description="总行数大于已返回行数，表示集合不完整。")
    gaps: list[str] = Field(default_factory=list, description="字段缺失、冲突或截断说明，不是上游业务事实。")
    source_page: PolicySearchSourcePageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="实际成功 data 的全量原始页；字段、类型和值以接口为准，文档只辅助语义。旧 handler 可省略。",
    )

    @model_validator(mode="after")
    def validate_counts(self) -> "PolicySearchPageDto":
        if self.record_total < len(self.records) or self.truncated != (self.record_total > len(self.records)):
            raise ValueError("险种行计数与截断状态不一致")
        if self.record_total and not self.records:
            raise ValueError("非空结果首页不得无记录")
        if self.source_page is not None:
            source = self.source_page
            if (source.total != self.record_total or self.truncated != (source.total > len(source.records))
                    or [row["polno"] for row in source.records] != [row.polno for row in self.records]):
                raise ValueError("保单源页与兼容投影的计数、顺序或主键不一致")
        return self


class PolicySearchResult(ContractModel):
    policies: list[PolicyCandidateDto] = Field(description="当前页按保单号去重的候选，不代表全部匹配保单。")
    unsupported_message: NonBlankText | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="条件不能可靠执行的说明；与候选及分页结果互斥。",
    )
    page: PolicySearchPageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 险种首页元数据；None 为旧结构化分支。",
    )

    @model_validator(mode="after")
    def validate_search_branch(self) -> "PolicySearchResult":
        if self.unsupported_message is not None and (self.policies or self.page is not None):
            raise ValueError("未支持解析不得包含查询结果")
        if self.page is not None:
            ids = [item.policy_no for item in self.policies]
            if len(ids) != len(set(ids)) or set(ids) != {row.polno for row in self.page.records}:
                raise ValueError("保单候选必须与当前页去重标识一致")
            if self.page.source_page is not None and ids != list(dict.fromkeys(row.polno for row in self.page.records)):
                raise ValueError("保单候选顺序必须与源页去重顺序一致")
        return self


class PolicyDetailRecordDto(PolicyDetailFields):
    """详情全量源记录，未知 JSON 扩展列保留；导入路径保持兼容。"""


class PolicyDetailPageDto(ContractModel):
    """明细首页的原始险种行；超过服务上限的完整性不作保证。"""

    record_total: StrictInt = Field(ge=0, description="服务报告的明细险种行总数，不是保单数。")
    records: list[PolicyDetailRecordDto] = Field(max_length=100, description="同一保单当前页明细行，最多 100 条。")
    truncated: bool = Field(strict=True, description="仍有未返回行，不能认定明细完整。")
    source_metadata: dict[str, StrictJsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="原始 data 除 records 外的全部字段，含 pageNo/pageSize/total；不包含外层协议码。旧 SDK 明细未提供时为 None。",
    )

    @model_validator(mode="after")
    def validate_counts(self) -> "PolicyDetailPageDto":
        if self.record_total < len(self.records) or self.truncated != (self.record_total > len(self.records)):
            raise ValueError("明细行计数与截断状态不一致")
        if self.record_total and not self.records:
            raise ValueError("非空明细首页不得无记录")
        if self.source_metadata is not None:
            metadata = self.source_metadata
            page_size = metadata.get("pageSize")
            if ("records" in metadata or type(metadata.get("total")) is not int
                    or metadata["total"] != self.record_total
                    or type(metadata.get("pageNo")) is not int or metadata["pageNo"] != 1
                    or type(page_size) is not int or page_size < 1
                    or len(self.records) > page_size):
                raise ValueError("原始明细分页与 SDK 计数不一致")
        return self


class PolicyDetailResult(ContractModel):
    policy_no: str | None = Field(description="已锁定保单号；未命中或候选未锁定时为 None。")
    candidates: list[PolicyCandidateDto] = Field(default_factory=list, description="未锁定时的保单候选，不与已锁定号共存。")
    fields: dict[str, float | int | str | bool] = Field(description="旧结构化字段字典；real 原始明细不混填金额事实。")
    as_of: str | None = Field(description="旧结构化金额业务日期，ISO 日期；不能用请求时间补造。")

    detail_page: PolicyDetailPageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 明细页，与旧 fields/as_of 及候选页互斥。",
    )
    candidate_page: PolicySearchPageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 尾号候选页，未锁定详情时使用。",
    )

    @model_validator(mode="after")
    def validate_result_shape(self) -> "PolicyDetailResult":
        if not self.policy_no and self.fields:
            raise ValueError("policy_no 为空时不得返回 fields")
        if self.policy_no and self.candidates:
            raise ValueError("已锁定 policy_no 时不得同时返回 candidates")
        if self.as_of is not None:
            date.fromisoformat(self.as_of)
        amount_fields = {"cash_value", "universal_account_balance", "premium", "surrender_value"}
        if self.policy_no and amount_fields.intersection(self.fields) and self.as_of is None:
            raise ValueError("金额字段必须携带 as_of")
        if self.detail_page is not None:
            if self.candidate_page is not None or self.fields or self.as_of is not None or self.candidates:
                raise ValueError("真实明细不得混合旧字段或候选分支")
            ids = {row.polno for row in self.detail_page.records}
            if ids != ({self.policy_no} if self.policy_no else set()):
                raise ValueError("明细记录必须对应已锁定保单号")
        if self.candidate_page is not None:
            if self.policy_no is not None or self.fields or self.as_of is not None:
                raise ValueError("候选页不得包含已锁定明细")
            candidate_ids = [item.policy_no for item in self.candidates]
            if len(candidate_ids) != len(set(candidate_ids)) or set(candidate_ids) != {row.polno for row in self.candidate_page.records}:
                raise ValueError("候选必须与当前页去重标识一致")
        return self



class CustomerSearchRequest(ContractModel):
    query: NonBlankText = Field(description="必填自然语言问题；去除首尾空白后不能为空。")


class CustomerCandidateDto(ContractModel):
    customer_id: str = Field(
        min_length=1,
        description="客户中心自有 ID clientMainId（兼容 idicm_clientno）；准客/老客均可有，供详情查询，不是业务客户号 clientno。",
    )
    name: str | None = Field(
        min_length=1, strict=True,
        description="客户本人姓名 searchclientname（客户字典第4行），不表示投保人或被保人；None 为未知。",
    )
    policy_count: StrictInt | None = Field(ge=0, description="保单数量；real 未提供时为 None，不得当成零。")


class CustomerSearchPageDto(ContractModel):
    """客户查询首页总数；候选摘要与完整源页分离。"""

    total: StrictInt = Field(ge=0, description="上游报告的匹配客户总数，不是保单数量。")
    truncated: bool = Field(strict=True, description="总数大于首页候选数，表示结果集合不完整。")
    source_page: CustomerSearchSourcePageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="实际成功 data 的全量原始页；字段、类型和值以接口为准，文档只辅助语义。旧 handler 可省略。",
    )

    @model_validator(mode="after")
    def validate_source_page(self) -> "CustomerSearchPageDto":
        if self.source_page is not None:
            source = self.source_page
            if (source.page_no != 1 or source.page_size != 100 or source.total != self.total
                    or self.truncated != (source.total > len(source.records))):
                raise ValueError("客户源页必须与搜索首页计数和截断状态一致")
        return self


class CustomerSearchResult(ContractModel):
    customers: list[CustomerCandidateDto] = Field(description="有界首页客户候选；主键来自 clientMainId 或已登记同义旧键。")
    unsupported_message: NonBlankText | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="条件不能可靠执行的说明；与候选及分页结果互斥。",
    )
    page: CustomerSearchPageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 客户分页说明；None 为旧结构化分支。",
    )
    browsed_all: bool | None = Field(
        default=None, strict=True, exclude_if=lambda value: value is None,
        description="2026-09-20 空条件浏览标记：解析器未识别出条件，按名下全部客户返回首页；消费方必须披露「未按条件筛选」。",
    )

    @model_validator(mode="after")
    def validate_search_branch(self) -> "CustomerSearchResult":
        if self.unsupported_message is not None and (self.customers or self.page is not None):
            raise ValueError("未支持解析不得包含查询结果")
        if self.page is not None:
            count = len(self.customers)
            ids = [item.customer_id for item in self.customers]
            if count > 100 or len(ids) != len(set(ids)) or any(not item.strip() for item in ids):
                raise ValueError("客户首页必须包含有界唯一标识")
            if self.page.total < count or self.page.truncated != (self.page.total > count):
                raise ValueError("客户总数与截断状态不一致")
            if self.page.total and not count:
                raise ValueError("非空结果首页不得无记录")
            if self.page.source_page is not None:
                rows = self.page.source_page.records
                if ids != [require_customer_id(row) for row in rows]:
                    raise ValueError("客户候选必须与源页记录的主键和顺序一致")
                if any(item.name != search_source_text(row, "clientName")
                       for item, row in zip(self.customers, rows, strict=True)):
                    raise ValueError("客户候选姓名必须与源页同源字段一致")
        return self


class CustomerDetailRequest(ContractModel):
    customer_id: str | None = Field(default=None, strict=True, min_length=1, max_length=64, description="客户中心 ID；兼容单客户调用，不是寿险客户号。")
    customer_ids: list[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]] | None = Field(default=None, strict=True, min_length=1, max_length=100, description="客户中心 ID 集合，最多 100 项；与姓名和手机号按 AND 组合。")
    client_name: str | None = Field(default=None, strict=True, min_length=1, max_length=64, description="姓名密文等值查询（clientNameCipher EQ）；明文姓名无法命中。")
    client_mobile: str | None = Field(default=None, strict=True, min_length=1, max_length=64, description="手机号密文等值查询（phoneNoCipher EQ）；明文手机号无法命中。")

    @model_validator(mode="after")
    def validate_conditions(self) -> "CustomerDetailRequest":
        values = [self.customer_id, self.client_name, self.client_mobile, *(self.customer_ids or [])]
        if not any(value is not None for value in values) or any(value is not None and not value.strip() for value in values):
            raise ValueError("至少提供客户中心 ID、姓名或手机号，条件不得为空白")
        if len(self.to_wire().get("clientMainIdSet", [])) > 100:
            raise ValueError("客户中心 ID 最多 100 项")
        return self

    def to_wire(self) -> dict:
        ids = list(dict.fromkeys(([self.customer_id] if self.customer_id else []) + (self.customer_ids or [])))
        return {key: value for key, value in {"clientMainIdSet": ids or None, "clientName": self.client_name, "clientMobile": self.client_mobile}.items() if value is not None}


class CustomerBasicInfoDto(ContractModel):
    """已明确的客户基本字段；保留上游字符串，不推导年龄或身份关系。"""

    customer_number: str | None = Field(
        default=None, strict=True, min_length=1,
        description="业务客户号 clientno，不等于内部主键 idicm_clientno。",
    )
    age: str | None = Field(default=None, strict=True, min_length=1, description="客户年龄原始字符串，不从生日重新计算。")
    birthday: str | None = Field(default=None, strict=True, min_length=1, description="出生日期，文档为 yyyy-MM-dd；保留原始字符串。")
    mobile: str | None = Field(default=None, strict=True, min_length=1, description="手机号原始字符串；None 为未提供。")
    sex: str | None = Field(default=None, strict=True, min_length=1, description="性别原始字符串，文档示例男／女；不推断缺失值。")

    @model_validator(mode="after")
    def validate_nonblank_fields(self) -> "CustomerBasicInfoDto":
        if any(value is not None and not value.strip() for value in (
            self.customer_number, self.age, self.birthday, self.mobile, self.sex,
        )):
            raise ValueError("客户基本资料不得为空白字符串")
        return self


class CustomerDetailRecordDto(CustomerDetailFields):
    """旧 SDK 回放契约；真实 v5 handler 不接收此结构。"""


class CustomerDetailV5RecordDto(CustomerDetailV5Fields):
    """v5 最终值记录；业务可选字段不强制转换类型。"""


class CustomerDetailPageDto(ContractModel):
    records: list[CustomerDetailV5RecordDto | CustomerDetailRecordDto] = Field(strict=True, description="本次客户记录；真实接口仅 v5，旧结构仅用于 SDK 回放。")
    source_metadata: dict[str, StrictJsonValue] = Field(description="data 内 records 之外的原始元数据；total 为命中数。")
    gaps: list[str] = Field(default_factory=list, exclude_if=lambda value: not value, description="部分记录不可用或计数不一致的明确缺口。")
    candidates_only: bool = Field(default=False, exclude_if=lambda value: not value, description="多命中仅返回基本候选，其他空值不作业务结论。")

    @model_validator(mode="before")
    @classmethod
    def select_record_protocol(cls, value: object) -> object:
        # v5 键一旦出现就禁止降级到旧回放模型，避免 union 将非法新身份解释为旧身份。
        if isinstance(value, dict) and isinstance(value.get("records"), list):
            value = dict(value)
            value["records"] = [CustomerDetailV5RecordDto.model_validate(row)
                if isinstance(row, dict) and "clientMainId" in row else row for row in value["records"]]
        return value

    @model_validator(mode="after")
    def validate_page(self) -> "CustomerDetailPageDto":
        total = self.source_metadata.get("total")
        if "records" in self.source_metadata or type(total) is not int or total < 0:
            raise ValueError("客户详情必须包含非负整数 total，元数据不重复 records")
        if total > 0 and not self.records:
            raise ValueError("服务报告有命中但无可用客户记录，不能当作空结果")
        if total != len(self.records):
            gap = "服务报告命中总数与可用记录数不同，结果不完整；不据此推断未返回客户资料"
            if gap not in self.gaps:
                object.__setattr__(self, "gaps", [*self.gaps, gap])
        if total > 1 or len(self.records) > 1:
            object.__setattr__(self, "candidates_only", True)
        legacy = [isinstance(row, CustomerDetailRecordDto) for row in self.records]
        if any(legacy):
            if not all(legacy) or len(legacy) > 1 or type(self.source_metadata.get("pageNo")) is not int or type(self.source_metadata.get("pageSize")) is not int or self.source_metadata.get("pageNo") != 1 or self.source_metadata.get("pageSize") != 1 or total != len(legacy):
                raise ValueError("旧 SDK 回放必须是完整单条分页，不能与 v5 混用")
        return self


class CustomerDetailResult(ContractModel):
    customer_id: str | None = Field(description="命中客户内部主键；未命中为 None。")
    name: str = Field(description="客户姓名；real 未提供时为空字符串。")
    policies: list[PolicyCandidateDto] | None = Field(description="关联保单；None 为未知，空列表才表示返回了空关联。")
    benefits: list[str] | None = Field(description="已享权益关联；None 为未知，不等于没有权益。")
    basic_info: CustomerBasicInfoDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="real 基本资料；缺项为未知，旧分支未携带时为 None。",
    )
    detail_page: CustomerDetailPageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="客户详情全量源页；None 为旧分支。与旧 basic_info、保单及已享权益投影互斥。",
    )

    @model_validator(mode="after")
    def validate_result_shape(self) -> "CustomerDetailResult":
        if not self.customer_id and (self.name or self.policies or self.benefits or self.basic_info is not None):
            raise ValueError("customer_id 为空时不得返回客户数据")
        if self.detail_page is not None:
            if self.basic_info is not None or self.policies is not None or self.benefits is not None:
                raise ValueError("客户源页不得混入旧关联或基本资料投影")
            rows = self.detail_page.records
            if not rows:
                if self.customer_id is not None or self.name:
                    raise ValueError("空客户源页不得包含客户标识或姓名")
            else:
                record = rows[0]
                if isinstance(record, CustomerDetailV5RecordDto):
                    expected_name = record.clientNameCipher if isinstance(record.clientNameCipher, str) else ""
                    expected_id = record.clientMainId
                else:
                    expected_name = source_text(record.model_dump(mode="json"), "searchclientname") or ""
                    expected_id = record.idicm_clientno
                if self.customer_id != expected_id or self.name != expected_name:
                    raise ValueError("客户源页与结果主键或兼容姓名不一致")
        return self


@dataclass(frozen=True)
class OperationSpec:
    """语义 operation 到既有 BackendClient 路由的唯一映射。"""

    name: str  # 语义 operation 名称。
    system: str  # 逻辑服务名称，不是实际网络地址。
    endpoint: str  # 逻辑路由，不保证等于真实 HTTP 路径。
    owner_tool: str  # 所属模型工具。
    request_model: type[ContractModel]  # 请求契约类型。
    result_model: type[ContractModel]  # 结果契约类型。

    @property
    def handler_name(self) -> str:
        return self.name.replace(".", "_")


POLICY_DETAIL = OperationSpec(
    "policy.detail", "policy_svc", "/policy/full", "policy_detail",
    PolicyDetailRequest, PolicyDetailResult,
)
PRODUCT_RESOLVE = OperationSpec(
    "product.resolve", "product_svc", "/product/resolve", "product_knowledge",
    ProductResolveRequest, ProductResolveResult,
)
PRODUCT_RAG = OperationSpec(
    "product.rag", "product_svc", "/product/rag", "product_knowledge",
    ProductRagRequest, ProductRagResult,
)
BENEFIT_DETAIL = OperationSpec(
    "benefit.detail", "benefit_svc", "/benefit/detail", "benefit_info",
    BenefitDetailRequest, BenefitDetailResult,
)
QA_RECALL = OperationSpec(
    "qa.recall", "qa_svc", "/qa/recall", "general_qa",
    QaRecallRequest, QaRecallResult,
)
POLICY_SEARCH = OperationSpec(
    "policy.search", "policy_svc", "/policy/search", "policy_search",
    PolicySearchRequest, PolicySearchResult,
)
CUSTOMER_SEARCH = OperationSpec(
    "customer.search", "policy_svc", "/customer/search", "customer_search",
    CustomerSearchRequest, CustomerSearchResult,
)
CUSTOMER_DETAIL = OperationSpec(
    "customer.detail", "policy_svc", "/customer/detail", "customer_detail",
    CustomerDetailRequest, CustomerDetailResult,
)

OPERATION_SPECS = {
    operation.name: operation
    for operation in (
        POLICY_DETAIL,
        PRODUCT_RESOLVE,
        PRODUCT_RAG,
        BENEFIT_DETAIL,
        QA_RECALL,
        POLICY_SEARCH,
        CUSTOMER_SEARCH,
        CUSTOMER_DETAIL,
    )
}
OPERATION_BY_ROUTE = {(operation.system, operation.endpoint): operation for operation in OPERATION_SPECS.values()}

if len(OPERATION_BY_ROUTE) != len(OPERATION_SPECS):
    raise RuntimeError("OperationSpec 的 system/endpoint 必须唯一")


__all__ = [
    "HANDLER_API_VERSION",
    "HandlerCallResult",
    "BackendContext",
    "ContractModel",
    "OperationSpec",
    "OPERATION_SPECS",
    "OPERATION_BY_ROUTE",
    "POLICY_DETAIL",
    "PRODUCT_RESOLVE",
    "PRODUCT_RAG",
    "BENEFIT_DETAIL",
    "QA_RECALL",
    "POLICY_SEARCH",
    "CUSTOMER_SEARCH",
    "CUSTOMER_DETAIL",
    "PolicyCandidateDto",
    "PolicyDetailRequest",
    "PolicyDetailResult",
    "PolicyDetailRecordDto",
    "PolicyDetailPageDto",
    "ProductResolveRequest",
    "ProductResolveResult",
    "ProductCandidateDto",
    "ProductRagRequest",
    "ProductRagResult",
    "BenefitDetailRequest",
    "BenefitDetailResult",
    "BenefitItemDto",
    "BenefitDocumentDto",
    "QaRecallRequest",
    "QaRecallResult",
    "QaPairDto",
    "KnowledgeDocumentDto",
    "DocumentDiagnosticsDto",
    "PolicySearchRequest",
    "PolicySearchContextDto",
    "PolicySearchResult",
    "PolicySearchPageDto",
    "PolicySearchRecordDto",
    "PolicySearchSourcePageDto",
    "CustomerSearchRequest",
    "CustomerSearchResult",
    "CustomerSearchPageDto",
    "CustomerSearchSourcePageDto",
    "CustomerCandidateDto",
    "CustomerDetailRequest",
    "CustomerDetailResult",
    "CustomerDetailRecordDto",
    "CustomerDetailPageDto",
    "CustomerBasicInfoDto",
]
