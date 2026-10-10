"""搜索业务源页；字典只提供语义，不充当实际字段或非身份类型白名单。"""
from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, model_validator

from tool_adapter_sdk.customer_detail_fields import CustomerDetailFields
from tool_adapter_sdk import customer_search_metadata, policy_search_metadata
from tool_adapter_sdk.json_values import StrictJsonValue, validate_json
from tool_adapter_sdk.policy_detail_fields import PolicyDetailFields


def _record_schema(*, customer: bool) -> dict[str, JsonValue]:
    # 复用既有源字段清单及目录说明，但不复制详情 DTO 的强类型约束或默认值。
    fields = CustomerDetailFields.model_fields if customer else PolicyDetailFields.model_fields
    metadata = customer_search_metadata if customer else policy_search_metadata
    properties: dict[str, JsonValue] = {
        key: {"description": metadata.describe_field(key), **metadata.field_schema(key)} for key in fields
    }
    properties.update(metadata.record_properties())
    identities = sorted(customer_search_metadata.IDENTITY_ALIASES) if customer else ["polno"]
    for identity in identities:
        properties[identity] = {"description": metadata.describe_field(identity), "type": "string",
                                "minLength": 1, "pattern": r"\S", **({"maxLength": 64} if customer else {})}
    schema: dict[str, JsonValue] = {"properties": properties}
    if customer:
        # 至少一个已登记身份键；同值与冲突仍由运行时严格验证，不能补写旧键。
        schema["anyOf"] = [{"required": [identity]} for identity in identities]
    else:
        schema["required"] = ["polno"]
    return schema


PolicySourceRecord = Annotated[dict[str, StrictJsonValue], Field(json_schema_extra=_record_schema(customer=False))]
CustomerSourceRecord = Annotated[dict[str, StrictJsonValue], Field(json_schema_extra=_record_schema(customer=True))]


class _SearchSourcePage(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, frozen=True, serialize_by_alias=True)
    __pydantic_extra__: dict[str, StrictJsonValue] = Field(init=False)
    identity_key: ClassVar[str]

    page_no: StrictInt = Field(alias="pageNo", ge=1, description="实际返回页码；搜索工具只查询首页。")
    page_size: StrictInt = Field(alias="pageSize", ge=1, le=100, description="实际返回页容量，最多 100 行；不是记录总数。")
    total: StrictInt = Field(ge=0, description="上游匹配行总数；不得当作保单数量或嵌套关系数量。")
    records: list[dict[str, StrictJsonValue]] = Field(max_length=100, description="实际返回的全部业务记录，保留原始顺序和扩展字段。")

    @model_validator(mode="before")
    @classmethod
    def strict_json_page(cls, value: object) -> object:
        if isinstance(value, cls):
            return value
        return validate_json(value)

    @model_validator(mode="after")
    def validate_source_page(self) -> _SearchSourcePage:
        offset = (self.page_no - 1) * self.page_size
        available = max(0, self.total - offset)
        if (offset > 100000 or len(self.records) > min(self.page_size, available)
                or (available > 0 and not self.records)):
            raise ValueError("源页记录数量与分页不一致")
        for row in self.records:
            if self.identity_key == "clientMainId":
                customer_search_metadata.require_customer_id(row)
                continue
            identity = row.get(self.identity_key)
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("源记录主键必须为非空白字符串")
        return self


class PolicySearchSourcePageDto(_SearchSourcePage):
    """成功响应 data 的原始保单搜索首页；旧险种行及新条目均不得合并。"""

    identity_key = "polno"
    records: list[PolicySourceRecord] = Field(max_length=100, description="整页原始搜索记录；旧接口为险种行，新版粒度待确认；所有 JSON 业务字段透传。")

    @model_validator(mode="after")
    def validate_policy_page(self) -> PolicySearchSourcePageDto:
        if self.page_no != 1 or self.page_size != 100:
            raise ValueError("保单搜索源页必须为容量 100 的首页")
        return self


class CustomerSearchSourcePageDto(_SearchSourcePage):
    """成功响应 data 的原始客户页；结构化 helper 可读取指定页。"""

    identity_key = "clientMainId"
    records: list[CustomerSourceRecord] = Field(max_length=100, description="整页原始客户记录；所有 JSON 业务字段透传，不推断保单数量或关联资格。")

    @model_validator(mode="after")
    def validate_customer_page(self) -> CustomerSearchSourcePageDto:
        ids = [customer_search_metadata.require_customer_id(row) for row in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("客户源页主键不得重复")
        return self
