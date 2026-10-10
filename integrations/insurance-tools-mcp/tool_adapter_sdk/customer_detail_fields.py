"""客户详情原字段声明；严格 JSON extra 仅作用于本详情记录。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from tool_adapter_sdk.customer_detail_metadata import add_source_alias_schema, canonical_path, describe_field, field_schema
from tool_adapter_sdk.json_values import StrictJsonValue


class CustomerDetailFields(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="allow", strict=True, allow_inf_nan=False, json_schema_extra=add_source_alias_schema,
    )
    __pydantic_extra__: dict[str, StrictJsonValue] = Field(init=False)

    @model_validator(mode="before")
    @classmethod
    def validate_physical_basic_fields(cls, value: object) -> object:
        # ES 命名仅用于定位说明；物理别名也须遵守原六列字符串线协议，不借 extra 绕过。
        if isinstance(value, dict):
            basic_paths = {"clientno", "searchclientname", "clientage", "clientbirthday", "clientmobile", "clientsex"}
            for key, item in value.items():
                if isinstance(key, str) and canonical_path(key) in basic_paths and item is not None and not isinstance(item, str):
                    raise ValueError("客户基本资料原字段必须为字符串或 null")
        return value

    idicm_clientno: str = Field(strict=True, min_length=1, max_length=64, description=describe_field("idicm_clientno"))
    agentno: StrictJsonValue = Field(
        default=None, description=describe_field("agentno"), json_schema_extra=field_schema("agentno"),
    )
    agentperspproducttype: StrictJsonValue = Field(
        default=None, description=describe_field("agentperspproducttype"), json_schema_extra=field_schema("agentperspproducttype"),
    )
    annpremsegnum: StrictJsonValue = Field(
        default=None, description=describe_field("annpremsegnum"), json_schema_extra=field_schema("annpremsegnum"),
    )
    assetscondition: StrictJsonValue = Field(
        default=None, description=describe_field("assetscondition"), json_schema_extra=field_schema("assetscondition"),
    )
    ayhmembergradeinfo: StrictJsonValue = Field(
        default=None, description=describe_field("ayhmembergradeinfo"), json_schema_extra=field_schema("ayhmembergradeinfo"),
    )
    ayymembergradeinfo: StrictJsonValue = Field(
        default=None, description=describe_field("ayymembergradeinfo"), json_schema_extra=field_schema("ayymembergradeinfo"),
    )
    birthdaymd: StrictJsonValue = Field(
        default=None, description=describe_field("birthdaymd"), json_schema_extra=field_schema("birthdaymd"),
    )
    clientage: str | None = Field(default=None, strict=True, description=describe_field("clientage"))
    clientbirthday: str | None = Field(default=None, strict=True, description=describe_field("clientbirthday"))
    clientchurntag: StrictJsonValue = Field(
        default=None, description=describe_field("clientchurntag"), json_schema_extra=field_schema("clientchurntag"),
    )
    clientgrouplabel: StrictJsonValue = Field(
        default=None, description=describe_field("clientgrouplabel"), json_schema_extra=field_schema("clientgrouplabel"),
    )
    clientmobile: str | None = Field(default=None, strict=True, description=describe_field("clientmobile"))
    clientno: str | None = Field(default=None, strict=True, description=describe_field("clientno"))
    clientsex: str | None = Field(default=None, strict=True, description=describe_field("clientsex"))
    clienttemperature: StrictJsonValue = Field(
        default=None, description=describe_field("clienttemperature"), json_schema_extra=field_schema("clienttemperature"),
    )
    criticalmemberflag: StrictJsonValue = Field(
        default=None, description=describe_field("criticalmemberflag"), json_schema_extra=field_schema("criticalmemberflag"),
    )
    criticalmembergrade: StrictJsonValue = Field(
        default=None, description=describe_field("criticalmembergrade"), json_schema_extra=field_schema("criticalmembergrade"),
    )
    datecreated: StrictJsonValue = Field(
        default=None, description=describe_field("datecreated"), json_schema_extra=field_schema("datecreated"),
    )
    education: StrictJsonValue = Field(
        default=None, description=describe_field("education"), json_schema_extra=field_schema("education"),
    )
    effanniversarydate: StrictJsonValue = Field(
        default=None, description=describe_field("effanniversarydate"), json_schema_extra=field_schema("effanniversarydate"),
    )
    effappenddate: StrictJsonValue = Field(
        default=None, description=describe_field("effappenddate"), json_schema_extra=field_schema("effappenddate"),
    )
    familyinfo: StrictJsonValue = Field(
        default=None, description=describe_field("familyinfo"), json_schema_extra=field_schema("familyinfo"),
    )
    gdkymembergradeinfo: StrictJsonValue = Field(
        default=None, description=describe_field("gdkymembergradeinfo"), json_schema_extra=field_schema("gdkymembergradeinfo"),
    )
    idno: StrictJsonValue = Field(
        default=None, description=describe_field("idno"), json_schema_extra=field_schema("idno"),
    )
    idtype: StrictJsonValue = Field(
        default=None, description=describe_field("idtype"), json_schema_extra=field_schema("idtype"),
    )
    idvaliddate: StrictJsonValue = Field(
        default=None, description=describe_field("idvaliddate"), json_schema_extra=field_schema("idvaliddate"),
    )
    insnosuminsseqnum: StrictJsonValue = Field(
        default=None, description=describe_field("insnosuminsseqnum"), json_schema_extra=field_schema("insnosuminsseqnum"),
    )
    isbuyhealth: StrictJsonValue = Field(
        default=None, description=describe_field("isbuyhealth"), json_schema_extra=field_schema("isbuyhealth"),
    )
    isbuyinsurance: StrictJsonValue = Field(
        default=None, description=describe_field("isbuyinsurance"), json_schema_extra=field_schema("isbuyinsurance"),
    )
    isbuyinsurancecar: StrictJsonValue = Field(
        default=None, description=describe_field("isbuyinsurancecar"), json_schema_extra=field_schema("isbuyinsurancecar"),
    )
    isbuypension: StrictJsonValue = Field(
        default=None, description=describe_field("isbuypension"), json_schema_extra=field_schema("isbuypension"),
    )
    isbuyproperty: StrictJsonValue = Field(
        default=None, description=describe_field("isbuyproperty"), json_schema_extra=field_schema("isbuyproperty"),
    )
    jujiaclientgrade: StrictJsonValue = Field(
        default=None, description=describe_field("jujiaclientgrade"), json_schema_extra=field_schema("jujiaclientgrade"),
    )
    kangyangclientgrade: StrictJsonValue = Field(
        default=None, description=describe_field("kangyangclientgrade"), json_schema_extra=field_schema("kangyangclientgrade"),
    )
    latelyundwrtsegtime: StrictJsonValue = Field(
        default=None, description=describe_field("latelyundwrtsegtime"), json_schema_extra=field_schema("latelyundwrtsegtime"),
    )
    marists: StrictJsonValue = Field(
        default=None, description=describe_field("marists"), json_schema_extra=field_schema("marists"),
    )
    newvaluelabel: StrictJsonValue = Field(
        default=None, description=describe_field("newvaluelabel"), json_schema_extra=field_schema("newvaluelabel"),
    )
    occurpasspayregst: StrictJsonValue = Field(
        default=None, description=describe_field("occurpasspayregst"), json_schema_extra=field_schema("occurpasspayregst"),
    )
    orphantype: StrictJsonValue = Field(
        default=None, description=describe_field("orphantype"), json_schema_extra=field_schema("orphantype"),
    )
    pajjmembergradeinfo: StrictJsonValue = Field(
        default=None, description=describe_field("pajjmembergradeinfo"), json_schema_extra=field_schema("pajjmembergradeinfo"),
    )
    pcategorys: StrictJsonValue = Field(
        default=None, description=describe_field("pcategorys"), json_schema_extra=field_schema("pcategorys"),
    )
    pcustsourcetype: StrictJsonValue = Field(
        default=None, description=describe_field("pcustsourcetype"), json_schema_extra=field_schema("pcustsourcetype"),
    )
    pcustsourctype: StrictJsonValue = Field(
        default=None, description=describe_field("pcustsourctype"), json_schema_extra=field_schema("pcustsourctype"),
    )
    planabbrnames: StrictJsonValue = Field(
        default=None, description=describe_field("planabbrnames"), json_schema_extra=field_schema("planabbrnames"),
    )
    pointsbalanceamt: StrictJsonValue = Field(
        default=None, description=describe_field("pointsbalanceamt"), json_schema_extra=field_schema("pointsbalanceamt"),
    )
    pointsexpireddate: StrictJsonValue = Field(
        default=None, description=describe_field("pointsexpireddate"), json_schema_extra=field_schema("pointsexpireddate"),
    )
    polnoinfo: StrictJsonValue = Field(
        default=None, description=describe_field("polnoinfo"), json_schema_extra=field_schema("polnoinfo"),
    )
    profname: StrictJsonValue = Field(
        default=None, description=describe_field("profname"), json_schema_extra=field_schema("profname"),
    )
    ptypes: StrictJsonValue = Field(
        default=None, description=describe_field("ptypes"), json_schema_extra=field_schema("ptypes"),
    )
    qkflag: StrictJsonValue = Field(
        default=None, description=describe_field("qkflag"), json_schema_extra=field_schema("qkflag"),
    )
    sdbjymembergradeinfo: StrictJsonValue = Field(
        default=None, description=describe_field("sdbjymembergradeinfo"), json_schema_extra=field_schema("sdbjymembergradeinfo"),
    )
    searchclientname: str | None = Field(default=None, strict=True, description=describe_field("searchclientname"))
    selectedlistinfolist: StrictJsonValue = Field(
        default=None, description=describe_field("selectedlistinfolist"), json_schema_extra=field_schema("selectedlistinfolist"),
    )
    trusteeshipflag: StrictJsonValue = Field(
        default=None, description=describe_field("trusteeshipflag"), json_schema_extra=field_schema("trusteeshipflag"),
    )
    validsinsmatudatetime: StrictJsonValue = Field(
        default=None, description=describe_field("validsinsmatudatetime"), json_schema_extra=field_schema("validsinsmatudatetime"),
    )
    validsinspol: StrictJsonValue = Field(
        default=None, description=describe_field("validsinspol"), json_schema_extra=field_schema("validsinspol"),
    )
    viptype: StrictJsonValue = Field(
        default=None, description=describe_field("viptype"), json_schema_extra=field_schema("viptype"),
    )
    yxgymembergradeinfo: StrictJsonValue = Field(
        default=None, description=describe_field("yxgymembergradeinfo"), json_schema_extra=field_schema("yxgymembergradeinfo"),
    )
    zhenxiangrunequitygrade: StrictJsonValue = Field(
        default=None, description=describe_field("zhenxiangrunequitygrade"), json_schema_extra=field_schema("zhenxiangrunequitygrade"),
    )
    zxjyequitygrade: StrictJsonValue = Field(
        default=None, description=describe_field("zxjyequitygrade"), json_schema_extra=field_schema("zxjyequitygrade"),
    )
    zxjymembergradeinfo: StrictJsonValue = Field(
        default=None, description=describe_field("zxjymembergradeinfo"), json_schema_extra=field_schema("zxjymembergradeinfo"),
    )

    @model_validator(mode="after")
    def validate_identity(self) -> "CustomerDetailFields":
        if not self.idicm_clientno.strip():
            raise ValueError("客户内部主键不能为空白")
        return self

    @model_serializer(mode="wrap")
    def preserve_present_fields(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        # 缺省与显式 null 不同；不声明返回注解，以免 Pydantic 替换逐字段序列化 Schema。
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}
