"""明文客户解析协议与显式字段适配；不猜测缺失的字段字典。"""
from __future__ import annotations

from tool_adapter_sdk.diagnostics import diagnostic, validation_details

from app.observability.langfuse_calls import observed_call

import math
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator

from app.tools.real.customer_filter import validate_customer_query
from tool_adapter_sdk.adapter import HandlerContractError, HandlerUnavailable
from tool_adapter_sdk.customer_detail_catalog import V14_FIELDS
from tool_adapter_sdk.customer_detail_metadata import query_canonical_path as canonical_path

# 2026-09-17 起本常量经 hint 直达用户正文（app/runtime/direct.py 的 AMBIGUOUS 分支
# 透出），故按对外话术要求写：只举例不枚举全部能力（「等」字兜住不完整），不要求
# 用户重复已给出的条件。改文案请同步 tests/real_tools/customer_search/
# test_customer_search_handler.py 的锁定用例。
UNSUPPORTED_MESSAGE = "未能可靠解析本次筛选条件或排序，尚未执行客户查询；可使用客户姓名、完整客户号、手机号、性别、生日月份等条件"
# 空条件（level=0/4）的浏览等价查询：主键存在性 ≡ 名下全部客户（构造性等价，见
# parse_customer_query 内注释）。仅此一处合成，不经上游字段映射。
BROWSE_ALL_QUERY: dict[str, object] = {
    "queryLogic": "AND",
    "conditions": [{"field": "idicm_clientno", "operator": "EXISTS"}],
    "sort": [],
}
# 仅允许整句明确浏览；不根据关键词、上游改写或调试文本推断用户同意扩大范围。
_BROWSE_QUERY = re.compile(
    r"(?:请)?(?:查询|列出|查看|显示)?我名下(?:的)?(?:全部|所有)客户"
    r"|(?:请)?(?:查询|查看)?我名下(?:都)?有哪些客户"
)


def _explicit_browse(query: str) -> bool:
    text = re.sub(r"\s+", "", query).rstrip("。？！?!")
    return _BROWSE_QUERY.fullmatch(text) is not None


# 保留既有 26 项已验证的 wire 映射，不为形式统一改变旧载荷。
# v2 新字段从共享目录取定义；新 CSV 名原样交服务映射，不机械 lower() 猜 SQL 列。
FIELDS = {
    "searchClientName": "searchclientname",
    "clientNo": "clientno",
    "clientMobile": "clientmobile",
    "clientBirthday": "clientbirthday",
    "birthdayMd": "birthdaymd",
    "idValidDate": "idvaliddate",
    "clientAge": "clientage",
    "clientSex": "clientsex",
    "education": "education",
    "mariSts": "marists",
    "profName": "profname",
    "assetsCondition": "assetscondition",
    "idNo": "idno",
    "idType": "idtype",
    "qkflag": "qkflag",
    "clientChurnTag": "clientchurntag",
    "clientGroupLabel": "clientgrouplabel",
    "clientTemperature": "clienttemperature",
    "criticalMemberFlag": "criticalmemberflag",
    "criticalMemberGrade": "criticalmembergrade",
    "newValueLabel": "newvaluelabel",
    "vipType": "viptype",
    "pointsExpiredDate": "pointsexpireddate",
    "dateCreated": "datecreated",
    "pcustSourcType": "pcustsourcetype",
    # 客户主键：接口文档常用字段表首行，故一并纳入（有意，非漏审）。它同时是响应侧
    # 身份键，收窄依赖它的测试是 tests/.../test_customer_query_protocol.py 的职责；
    # 此处仅表示「可作为查询条件」，不改变任何展示口径（render 仍不向用户暴露该值）。
    "idicm_clientno": "idicm_clientno",
}
# 这三个历史 ES 名并非已确认的 v2 请求名：前两项 v2 明确保留物理名，第三项
# Seg/Seq 拼写不同且缺完整 mapping。响应侧仍可识别含义，请求侧不得据此猜 wire 名。
_UNCONFIRMED_REQUEST_ALIASES = frozenset({"agentNo", "jujiaClientGrade", "insnoSumInsSegNum"})
for _wire in tuple(FIELDS.values()):
    FIELDS.setdefault(_wire, _wire)
for _entry in V14_FIELDS:
    # V14 的 pcustsourctype 已由 v2 更正，不能重新放行旧拼写。
    if _entry.path != "pcustsourctype":
        FIELDS.setdefault(_entry.path, _entry.path)
FIELDS["polNo"] = "polNo"  # v2 明确的仅查询别名，服务端映射至 polnoinfo.polno。

# 类型只决定操作能力，不转换字段名或源值；周年月日不冒充完整日期。
DATES = {entry.path for entry in V14_FIELDS if entry.logical_type == "日期"}
MONTH_DAYS = {"birthdaymd"}
TEMPORAL_FIELDS = DATES | MONTH_DAYS
# 周年字段只补明确的月日匹配/集合/存在性，不顺带开放周期区间或排序。
_MONTH_DAY_MATCH_FIELDS = {"effanniversarydate"}
_TEMPORAL_MATCH_FIELDS = TEMPORAL_FIELDS | _MONTH_DAY_MATCH_FIELDS
# 可排序白名单与 DATES 独立：日期字段能条件比较不等于上游可排序（排序语义另有一套
# 定义，见接口 7 文档「30 个可排序字段」）。两者混用会让 DATES 扩容静默放开排序，
# 故显式写死；成员维持既有 5 项不变，扩充排序需单独评估并同步 CONTRACT_REFERENCE。
SORTABLE_FIELDS = frozenset({
    "clientNo", "clientMobile", "clientAge", "clientBirthday", "idValidDate",
})
_SORTABLE_PATHS = frozenset(canonical_path(field) for field in SORTABLE_FIELDS)


class _Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field: StrictStr = Field(min_length=1, description="解析逻辑字段名；必须经显式映射才可查询。")
    operator: StrictStr = Field(min_length=1, description="比较／匹配操作符，需同时满足字段类型支持范围。")
    value: object = Field(description="条件值；存在性操作使用 null，集合与日期须匹配各自协议。")
    match_mode: StrictStr | None = Field(default=None, description="可选尾号模式；仅客户号／手机号／保单号 MATCH 支持 suffix。")

    @model_validator(mode="after")
    def validate_value(self) -> _Condition:
        value, operator = self.value, self.operator
        if operator == "MATCH" and not (isinstance(value, str) or _number(value)):
            raise ValueError("精确匹配值类型错误")
        if operator in {"GT", "GTE", "LT", "LTE"} and not (isinstance(value, str) or _number(value)):
            raise ValueError("比较值类型错误")
        if operator in {"CONTAINS", "NOT_CONTAINS"} and (
            not isinstance(value, list) or not 1 <= len(value) <= 100 or any(not isinstance(v, str) for v in value)
        ):
            raise ValueError("集合条件类型错误")
        if operator == "RANGE":
            if not isinstance(value, dict) or set(value) != {"min", "max"}:
                raise ValueError("区间结构错误")
            lo, hi = value["min"], value["max"]
            if not ((isinstance(lo, str) and isinstance(hi, str)) or (_number(lo) and _number(hi))):
                raise ValueError("区间边界类型错误")
        if operator in {"EXISTS", "NOT_EXISTS"} and value is not None:
            raise ValueError("存在性条件不得包含取值")
        return self


class _Sort(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field: StrictStr = Field(min_length=1, description="解析逻辑字段名；必须经显式映射才可查询。")
    order: Literal["asc", "desc"] = Field(description="升序 asc 或降序 desc。")
    priority: StrictInt = Field(description="排序优先级，严格整数。")
    mode: StrictStr | None = Field(default=None, description="排序模式；当前仅支持未填或空，非空周期模式不支持。")


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    query: StrictStr = Field(description="解析对应自然语言问题；客户解析需与请求原文一致。")
    query_logic: Literal["AND", "OR"] = Field(description="条件组合 AND/OR，不可静默删除不支持条件。")
    conditions: list[_Condition] = Field(max_length=50, description="解析条件列表；任一条件不支持则整条不查询。")
    sort: list[_Sort] = Field(max_length=50, description="排序条件；字段及操作必须全部可执行。")
    matched_level: StrictInt = Field(description="匹配层级 0/1/2/4；空条件仅允许 0/4，不代表用户要求浏览全部。")

    @model_validator(mode="after")
    def validate_match(self) -> _Output:
        if self.matched_level not in (0, 1, 2, 4):
            raise ValueError("解析层级与条件不一致")
        # 线上解析器把“未识别到明确查询条件”表示为 matched_level=4 + 空条件；
        # 这是可预期的 unsupported 结果，不应上升为上游契约故障。旧版 level=0
        # 空条件继续兼容；任何有条件却标 level=0 的响应仍按坏契约拒绝。
        if self.conditions and self.matched_level == 0:
            raise ValueError("解析层级与条件不一致")
        if not self.conditions and self.matched_level not in (0, 4):
            raise ValueError("解析层级与条件不一致")
        return self


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    robot_text: StrictStr = Field(description="解析器辅助文本；仅校验协议，不传为事实。")
    end_flag: StrictInt = Field(ge=1, le=1, description="解析结束标记，当前只接受 1。")
    trace_id: StrictStr = Field(description="上游返回链路标识，必须与本轮请求一致。")
    extra_output_params: _Output = Field(description="解析器结构化输出，不将调试文案作为查询条件。")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    code: StrictInt = Field(description="客户解析业务状态码；0 表示成功，500 表示服务不可用，其他值为契约错误。")
    msg: StrictStr = Field(description="上游状态说明；不直接作为业务事实或向模型透传错误原文。")
    data: _Data | None = Field(description="业务数据容器；是否允许为空由具体响应分支验证。")


def _number(value: object) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


# 解析服务日期边界自带当日首末时刻（接口文档示例 "1981-12-31 00:00:00"；robot_text
# 会去掉 00:00:00/23:59:59 后缀），查询服务只接受纯 yyyy-MM-dd——在此归一化，
# 其他时刻携带真实时间语义，截断会改变边界，保持不支持。
_CANONICAL_TIME_SUFFIXES = (" 00:00:00", " 23:59:59")


def _normalize_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value
    for suffix in _CANONICAL_TIME_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) is None:
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _normalize_month_day(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{2}-[0-9]{2}", value) is None:
        return None
    try:
        date(2000, int(value[:2]), int(value[3:]))  # 闰年允许 02-29
    except ValueError:
        return None
    return value


def _normalize_temporal(field: str, value: object) -> str | None:
    return _normalize_month_day(value) if field in MONTH_DAYS | _MONTH_DAY_MATCH_FIELDS else _normalize_date(value)


def _wire_field(field: str) -> str | None:
    if field in _UNCONFIRMED_REQUEST_ALIASES:
        return None
    if field in FIELDS:
        return FIELDS[field]
    # 逐前缀识别共享目录中的完整叶子，允许逻辑根与物理子名混用；只返回原请求名，
    # 不把字典路径当 SQL 映射。容器/未知字段及无映射逻辑名仍不准入。
    return field if canonical_path(field) in FIELDS else None


def _condition(item: _Condition) -> dict[str, object] | None:
    if (wire := _wire_field(item.field)) is None:
        diagnostic("parser_field_unsupported", stage="query_normalization")
        return None
    path = "polnoinfo.polno" if item.field == "polNo" else canonical_path(item.field)
    result: dict[str, object] = {"field": wire, "operator": item.operator}
    if item.match_mode is not None:
        if (item.match_mode != "suffix" or item.operator != "MATCH"
                or path not in {"clientno", "clientmobile", "polnoinfo.polno"}):
            diagnostic("parser_match_mode_unsupported", stage="query_normalization")
            return None
        result["matchMode"] = "suffix"
    value, operator = item.value, item.operator
    if operator in {"EXISTS", "NOT_EXISTS"}:
        return result
    if operator == "MATCH":
        if not isinstance(value, str) or not value.strip():
            diagnostic("parser_match_value_empty", stage="query_normalization")
            return None
        if path in _TEMPORAL_MATCH_FIELDS and (normalized := _normalize_temporal(path, value)) is None:
            diagnostic("parser_match_date_invalid", stage="query_normalization")
            return None
        result["value"] = normalized if path in _TEMPORAL_MATCH_FIELDS else value
    elif operator in {"CONTAINS", "NOT_CONTAINS"}:
        assert isinstance(value, list)
        if any(not v.strip() for v in value):
            diagnostic("parser_set_value_empty", stage="query_normalization")
            return None
        if path in _TEMPORAL_MATCH_FIELDS:
            normalized_values = [_normalize_temporal(path, v) for v in value]
            if any(v is None for v in normalized_values):
                diagnostic("parser_set_date_invalid", stage="query_normalization")
                return None
            result["values"] = normalized_values
        else:
            result["values"] = value
    elif operator in {"GT", "GTE", "LT", "LTE"}:
        # 2026-09-20 Owner 决策放开字段维度：比较不再限日期字段，数值任意字段透传。
        # 值类型防线保留：非日期字符串仍不支持——查询服务对字符串退化字典序并静默
        # 返回错误结果（2026-09-17/09-20 两次实测 LTE "9" 全命中、LTE 9 正确）。
        if path in TEMPORAL_FIELDS:
            if (normalized := _normalize_temporal(path, value)) is None:
                diagnostic("parser_comparison_date_invalid", stage="query_normalization")
                return None
            result["value"] = normalized
        elif _number(value):
            result["value"] = value
        else:
            diagnostic("parser_comparison_type_unsupported", stage="query_normalization")
            return None
    elif operator == "RANGE":
        assert isinstance(value, dict)
        if path in TEMPORAL_FIELDS:
            lower = _normalize_temporal(path, value["min"])
            upper = _normalize_temporal(path, value["max"])
            if lower is None or upper is None or (path in MONTH_DAYS and lower > upper):
                diagnostic("parser_temporal_range_invalid", stage="query_normalization")
                return None
            result["value"] = {"min": lower, "max": upper}
        elif _number(value["min"]) and _number(value["max"]) and value["min"] <= value["max"]:
            # 数值区间须有序：与 customer_filter._range 的 min<=max 校验对齐，
            # 逆序在解析层即判不支持，不上升为契约错误；非日期字符串区间同不支持。
            result["value"] = {"min": value["min"], "max": value["max"]}
        else:
            diagnostic("parser_numeric_range_invalid", stage="query_normalization")
            return None
    else:
        diagnostic("parser_operator_unsupported", stage="query_normalization")
        return None
    return result


@observed_call("result.parse_customer_query")
def parse_customer_query(raw: object, *, query: str, trace_id: str) -> dict[str, object] | None:
    """返回完整可执行条件，或固定未支持分支；任何条件都不能静默丢弃。"""
    try:
        response = _Response.model_validate(raw)
    except ValidationError as exc:
        diagnostic("response_schema_invalid", stage="response_validation", **validation_details(exc, _Response))
        raise HandlerContractError() from None
    if response.code == 500:
        diagnostic("business_unavailable", stage="response_validation", business_code=response.code)
        raise HandlerUnavailable()
    if response.code != 0 or response.data is None:
        diagnostic(
            "business_code_rejected" if response.code != 0 else "response_data_missing",
            stage="response_validation",
            business_code=response.code,
        )
        raise HandlerContractError()
    output = response.data.extra_output_params
    if response.data.trace_id != trace_id or output.query != query:
        diagnostic("parser_echo_mismatch", stage="response_validation")
        raise HandlerContractError()
    if not output.conditions:
        # 解析失败不等于浏览意图；带排序也不能被此分支静默丢弃。
        if not _explicit_browse(query) or output.sort:
            diagnostic("parser_browse_not_confirmed", stage="query_normalization")
            return None
        return validate_customer_query(BROWSE_ALL_QUERY)
    conditions = [_condition(item) for item in output.conditions]
    if any(item is None for item in conditions):
        diagnostic("parser_conditions_unsupported", stage="query_normalization")
        return None
    identity_exists = {"field": "idicm_clientno", "operator": "EXISTS"}
    # 单独/重复的主键存在性，以及 OR 中的该条件，均会放宽为名下全部。
    # AND 中同时存在有效筛选则不改变该筛选的既有行为。
    unfiltered = (all(item == identity_exists for item in conditions)
                  or (output.query_logic == "OR" and identity_exists in conditions))
    if unfiltered and (not _explicit_browse(query) or output.sort):
        diagnostic("parser_unfiltered_query_rejected", stage="query_normalization")
        return None
    sorts: list[dict[str, object]] = []
    # clientAge 是解析文档的示例排序字段（数值可排序）；searchClientName 不在
    # 上游 30 个可排序字段内，保持不支持。
    for item in output.sort:
        if ((wire := _wire_field(item.field)) is None or canonical_path(item.field) not in _SORTABLE_PATHS
                or item.mode not in (None, "")):
            diagnostic("parser_sort_unsupported", stage="query_normalization")
            return None
        sorts.append({"field": wire, "order": item.order, "priority": item.priority})
    return validate_customer_query({"queryLogic": output.query_logic, "conditions": conditions, "sort": sorts})
