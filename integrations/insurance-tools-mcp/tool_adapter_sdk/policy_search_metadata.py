"""搜索返回 v1 契约；只读兼容旧响应，不改变查询或详情字段。"""
from __future__ import annotations

from tool_adapter_sdk import policy_detail_metadata as legacy
from tool_adapter_sdk.search_metadata import SearchMetadata

# 精确登记 v1 与蛇形名称，运行时不做通用大小写猜测。
V1_FIELDS = (
    ('polSource', '保单来源', '保单来源的保险业务条线，如寿险、产险、养老险、健康险、意健险；用于区分保单所属业务来源。本返回字段将产险统一表示为“产险”。', '寿险、产险、养老险、健康险、意健险', 'pol_source'),
    ('isOrphan', '孤儿单类型', '保单按代理人离职及接续情况的分类，返回“在职单”“非纯存续单”或“纯存续单”；用于识别保单服务接续类别。', '在职单、非纯存续单、纯存续单', 'is_orphan'),
    ('polProductName', '产品名称', '保单对应的产品名称：寿险为主险名称，意健险为套餐名称，产险、养老险、健康险为产品名称。用于识别保单产品，区别于保单下各主附险明细的险种名称。', '见说明', 'pol_product_name'),
    ('totalModalPrem', '续期保费（期缴保费）', '保单每个续期缴费期应缴的保费，即续期保费或期缴保费；用于查看当期续期交费金额，区别于年缴保费、首期保费和累计已交保费。', '', 'total_modal_prem'),
    ('applicantIcmId', '投保人客户中心ID', '投保人在客户中心的统一标识，用于识别投保人并关联该人的客户资料；区别于寿险核心系统的投保人客户号 applicantClientNo。', '', 'applicant_icm_id'),
    ('insurantNameCipher', '被保人姓名', '保单被保人姓名的密文。该字段返回加密字符串，需经授权解密后取得被保人姓名；对应被保人角色，不是投保人姓名，也不能把密文直接当作明文姓名展示或匹配。', '', 'insurant_name_cipher'),
    ('applicantNameCipher', '投保人姓名', '保单投保人姓名的密文。该字段返回加密字符串，需经授权解密后取得投保人姓名；对应投保人角色，不是被保人姓名，也不能把密文直接当作明文姓名展示或匹配。', '', 'applicant_name_cipher'),
    ('applicantClientNo', '投保人客户号', '投保人在保险核心系统中的寿险客户号，用于识别投保人的寿险客户身份；区别于客户中心统一标识 applicantIcmId，不能与被保人客户号 insurantClientNo 混用。', '', 'applicant_client_no'),
    ('insurantIcmId', '被保人客户中心 ID', '被保人在客户中心的统一标识，用于识别被保人并关联该人的客户资料；区别于寿险核心系统的被保人客户号 insurantClientNo。', '', 'insurant_icm_id'),
    ('polno', '保单号', '保险合同的编号，用于定位和关联同一保单。同一保单号可对应多个主附险明细；险种明细须结合主附约编号和险种代码区分，不能仅按保单号认定明细唯一。', '', 'polno'),
    ('totalPolno', '总单号(意健险、养老险,其他险没有)', '意健险、养老险保单对应的总单号，用于识别保单所属总单；与本条保单号 polno 区分，其他险种不适用。', '', 'total_polno'),
    ('termClassDesc', '长短险类型', '保单对应险种属于长期险还是短期险，返回“长险”或“短险”；用于按长短险类型区分保单，不表示缴费频率或缴费年期。', '长险、短险', 'term_class_desc'),
    ('securityPeriod', '保障期间', '保单保障期从开始到结束的日期区间，用于查询保障起止范围；返回区间文本，如“2025.9.1-2026.9.1”，不是单一日期，也不是保险年期数。', '', 'security_period'),
    ('polStatusDesc', '保单状态', '保单当前所处的承保、缴费及合同效力状态，返回具体中文状态描述，如“交费有效”“停效”“退保”；与仅表示“有效/无效”的 isValidPol 区分。', '预收、拒保、自垫有效、死亡有效、取消、死亡理赔、展期、迁出、自垫停效、贷款超停、交费有效、自垫交清、贷款超失、停效、到期终止、犹豫期退保、自垫失效、交清、减额交清、退保、转换终止、等待续保、失效、免交、终止效力、效力终止', 'pol_status_desc'),
    ('insureDate', '投保日期', '客户提交投保申请的日期，用于查询投保时间；区别于保险公司确认承保的日期和保单合同首次生效日期。', '', 'insure_date'),
    ('payToDate', '应缴日', '保单续期保费应缴纳的日期，用于查看应交保费的时间；不是实际缴费日期，可与续期保费 totalModalPrem 配合理解。', '', 'pay_to_date'),
    ('aggPayCnt', '累计赔付次数', '保单累计已完成赔付的次数，用于查看已赔付业务的发生次数；不是理赔报案次数，也不是累计赔付金额。', '', 'agg_pay_cnt'),
    ('isValidPol', '是否有效', '保单当前是否有效，返回“有效”或“无效”；用于判断合同当前效力，具体承保、缴费或合同状态另见 polStatusDesc。', '有效、无效', 'is_valid_pol'),
    ('insurantClientNo', '被保人客户号', '被保人在保险核心系统中的寿险客户号，用于识别被保人的寿险客户身份；区别于客户中心统一标识 insurantIcmId，不能与投保人客户号 applicantClientNo 混用。', '', 'insurant_client_no'),
    ('appInsRelationshipDesc', '投被保人关系', '投保人与被保人的关系描述，返回“本人”“配偶”“父母”“子女”等本表所列值；“本人”表示投保人与被保人为同一人。本接口返回具体关系，不能简化成仅有“本人/非本人”的二分类。', '本人、配偶、父母、子女、其他非亲属、不详、受益人、被保人、投保人、雇主、兄弟姐妹、法定、雇员的家属、其他亲属、代理人、保全员、雇员', 'app_ins_relationship_desc'),
)

_metadata = SearchMetadata(
    fields=V1_FIELDS, old_definitions=legacy.DEFINITIONS,
    old_aliases=legacy.SOURCE_ALIASES,
    old_canonical=lambda path: legacy.SOURCE_ALIASES.get(path, path),
    old_describe=legacy.describe_output_field,
    extra_aliases={
        'applicant_clientno': 'applicantClientNo', 'insurant_clientno': 'insurantClientNo',
        'applicant_idicm_clientno': 'applicantIcmId', 'insurant_idicm_clientno': 'insurantIcmId',
        'pay_to_day': 'payToDate',
    },
    v1_keys={'applicantIcmId', 'applicant_icm_id', 'insurantIcmId', 'insurant_icm_id',
             'totalPolno', 'total_polno'},
    legacy_keys={'brno', 'plan_code', 'plan_abbr_name', 'plan_full_name'},
    divergent={'polSource', 'appInsRelationshipDesc', 'polStatusDesc'}, customer=False,
)

DEFINITIONS = _metadata.definitions
SOURCE_ALIASES = _metadata.aliases
canonical_path = _metadata.canonical_path
group_source_fields = _metadata.group_source_fields
source_value = _metadata.source_value
source_text = _metadata.source_text
response_profile = _metadata.response_profile
describe_field = _metadata.describe_field
describe_output_field = _metadata.describe_field
describe_output_data = _metadata.describe_output_data
field_schema = _metadata.field_schema
record_properties = _metadata.record_properties
add_source_alias_schema = _metadata.add_source_alias_schema
resolve_focus = _metadata.resolve_focus
total_description = _metadata.total_description
