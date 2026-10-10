"""客户结构化单页查询；由自然语言 handler 复用，也可独立验证。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details

import asyncio
from tool_adapter_sdk.compat import timeout_at, TIMEOUT_ERRORS
import math
from collections.abc import Mapping

from app.tools.real.auth import HttpClient
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr, ValidationError, field_validator

from app.tools.real.customer_filter import validate_customer_query
from app.tools.real.http import post_json as _post_json
from app.observability.langfuse_calls import observed_call

from app.tools.real.java_response import parse_page, validate_trace_id
from tool_adapter_sdk.identity import resolve_agent_number
from tool_adapter_sdk.adapter import HandlerContractError, HandlerTimeout
from tool_adapter_sdk.contracts import CustomerSearchSourcePageDto
from tool_adapter_sdk.customer_search_metadata import require_customer_id, source_text

post_json = observed_call("backend.customer_query.request")(_post_json)


class CustomerQueryRecord(BaseModel):
    """七列兼容摘要；非身份类型不适用时不强转，全量原值独立保存在 source_page。"""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True, populate_by_name=True)

    customer_id: StrictStr = Field(
        alias="idicm_clientno", min_length=1, max_length=64,
        description="客户中心自有 ID（客户字典第2行）；准客/老客均可有，不代表已购保单。",
    )
    customer_number: StrictStr | None = Field(default=None, alias="clientno", description="业务客户号 clientno，不等于内部主键。")
    name: StrictStr | None = Field(
        default=None, alias="searchclientname",
        description="客户本人姓名 searchclientname（字典第4行）；不是投保人、被保人或家庭成员姓名。",
    )
    age: StrictStr | None = Field(
        default=None, alias="clientage",
        description="客户本人年龄 clientage；字典为数字，HTTP 协议为字符串，保持原值、不重算。",
    )
    birthday: StrictStr | None = Field(
        default=None, alias="clientbirthday",
        description="客户本人出生日期 clientbirthday，HTTP 文档格式 yyyy-MM-dd，不指家庭成员。",
    )
    mobile: StrictStr | None = Field(
        default=None, alias="clientmobile",
        description="客户本人手机号 clientmobile，不指投保人、被保人或联系人；保留原始字符串。",
    )
    sex: StrictStr | None = Field(
        default=None, alias="clientsex",
        description="客户本人性别 clientsex，字典枚举男／女；保留字符串，不推断其他人员性别。",
    )

    @field_validator("customer_id")
    @classmethod
    def nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("客户主键不能为空")
        return value

    @field_validator("customer_number", "name", "age", "birthday", "mobile", "sex")
    @classmethod
    def known_text(cls, value: str | None) -> str | None:
        return value if value is not None and value.strip() else None


class CustomerQueryPage(BaseModel):
    """单次读取的分页快照，不承诺跨页期间数据集不变。"""

    model_config = ConfigDict(strict=True, frozen=True)

    page_no: int = Field(description="当前页码，从 1 起；具体请求页码约束由调用方校验。")
    page_size: int = Field(description="服务返回页容量；不是实际返回记录数。")
    total: int = Field(description="匹配客户总数，不是该客户的保单数量。")
    records: tuple[CustomerQueryRecord, ...] = Field(description="当前页兼容摘要；完整原始字段独立保存在 source_page。")
    source_page: CustomerSearchSourcePageDto | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="成功 data 的完整原始业务页；不按七列摘要裁剪或转换字段。",
    )

    @property
    def has_previous(self) -> bool:
        return self.page_no > 1 and self.total > 0

    @property
    def has_more(self) -> bool:
        return (self.page_no - 1) * self.page_size + len(self.records) < self.total

    @property
    def truncated(self) -> bool:
        return self.has_previous or self.has_more


def _record_fields(row: Mapping[str, JsonValue]) -> dict[str, object]:
    """摘要共用搜索响应识别；身份严格读取别名，源页不补写兼容 DTO 的旧键。"""
    return {
        field.alias: require_customer_id(row) if field.alias == "idicm_clientno" else source_text(row, field.alias)
        for field in CustomerQueryRecord.model_fields.values() if field.alias is not None
    }


async def query_customer_page(
    client: HttpClient, url: str, query: object, *, trace_id: str, timeout_s: float,
    agentno: str | None = None, upstream_user_id: str | None = None,
    page_no: int = 1, page_size: int = 20,
) -> CustomerQueryPage:
    """借用客户端；预算含校验和慢流读取，只调用一次，无内部重试。"""
    try:
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            diagnostic("timeout_value_invalid", stage="deadline")
            raise HandlerTimeout()
    except OverflowError:
        diagnostic("timeout_value_overflow", stage="deadline")
        raise HandlerTimeout() from None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    validate_trace_id(trace_id)
    if (type(page_no) is not int or page_no < 1 or type(page_size) is not int
            or not 1 <= page_size <= 100 or (page_no - 1) * page_size > 100000):
        diagnostic("request_page_invalid", stage="request_validation")
        raise HandlerContractError()
    payload = validate_customer_query(query)
    payload.update(agentno=resolve_agent_number(agentno, upstream_user_id, max_length=64),
                   traceId=trace_id, pageNo=page_no, pageSize=page_size)
    remaining = deadline - loop.time()
    if remaining <= 0:
        diagnostic("handler_deadline_exhausted", stage="deadline")
        raise HandlerTimeout()
    try:
        async with timeout_at(deadline):
            raw = await post_json(client, url, payload, timeout_s=remaining)
            page = parse_page(raw, trace_id)
            offset = (page_no - 1) * page_size
            available = max(0, page.total - offset)
            if (page.page_no != page_no or page.page_size != page_size
                    or len(page.records) > min(page_size, available)
                    or (available > 0 and not page.records)):
                diagnostic(
                    "customer_page_inconsistent",
                    stage="response_validation",
                    returned_count=len(page.records),
                    page_no=page.page_no,
                    page_size=page.page_size,
                    expected_page_no=page_no, expected_page_size=page_size,
                    total_count=page.total,
                )
                raise HandlerContractError()
            if not isinstance(raw, dict):
                diagnostic("response_object_type_invalid", stage="response_validation")
                raise HandlerContractError()
            source_page = CustomerSearchSourcePageDto.model_validate(raw["data"])
            records = tuple(
                CustomerQueryRecord.model_validate(_record_fields(row))
                for row in source_page.records
            )
            if len({row.customer_id for row in records}) != len(records):
                diagnostic("duplicate_customer_identity", stage="response_validation", returned_count=len(records))
                raise HandlerContractError()
            result = CustomerQueryPage(page_no=page_no, page_size=page_size, total=page.total,
                                        records=records, source_page=source_page)
            if loop.time() >= deadline:
                diagnostic("handler_deadline_exhausted", stage="deadline")
                raise HandlerTimeout()
            return result
    except ValidationError as exc:
        diagnostic(
            "response_schema_invalid",
            stage="response_validation",
            **validation_details(exc, {m.__name__: m for m in (CustomerSearchSourcePageDto, CustomerQueryRecord, CustomerQueryPage)}.get(exc.title, CustomerQueryPage)),
        )
        raise HandlerContractError() from None
    except TIMEOUT_ERRORS:
        diagnostic("handler_deadline_exhausted", stage="deadline")
        raise HandlerTimeout() from None
