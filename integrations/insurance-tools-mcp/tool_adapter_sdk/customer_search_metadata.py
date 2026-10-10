"""搜索返回 v1 契约；只读兼容旧响应，不改变查询或详情字段。"""
from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from tool_adapter_sdk.customer_detail_metadata import has_alias_conflict
from tool_adapter_sdk import customer_detail_metadata as legacy
from tool_adapter_sdk.search_metadata import SearchMetadata

# 精确登记 v1 与蛇形名称，运行时不做通用大小写猜测。
V1_FIELDS = (
    ('agentNo', '业务员编号', '服务该客户的代理人员工编号，用于识别客户归属或关联服务人员信息。按编号字符串理解并保留原值，不作为数值计算。', '', 'agent_no'),
    ('clientMainId', '客户中心客户id', '当前客户在客户中心的唯一标识，用于关联搜客户结果、客户详情及相关客户信息。准客和已成单客户均可有此 ID；与仅寿险成交客户可能具有的寿险系统客户号区分，按标识字符串保留原值。', '', 'client_main_id'),
    ('isBuyPension', '是否养老险客户', '客户是否为养老险客户的购买标志，用于区分有购买、没有购买养老险的客户。本接口使用 0、1，现有表格尚未明确它们与有购买、没有购买的对应关系。', '0、1', 'is_buy_pension'),
    ('isBuyHealth', '是否健康险客户', '客户是否为健康险客户的购买标志，用于区分有购买、没有购买健康险的客户。本接口使用 0、1，现有表格尚未明确它们与有购买、没有购买的对应关系。', '0、1', 'is_buy_health'),
    ('clientMobile', '客户手机号', '客户本人手机号的明文，用于识别客户联系方式或按完整号码、尾号查找客户。区别于保单中的投保人、被保人以及家庭成员手机号；按号码字符串保留原值。', '', 'client_mobile'),
    ('clientSex', '性别', '客户本人的性别，用于性别筛选或画像，本接口返回中文“男”“女”。与详情接口中 M=男、F=女属于同一业务含义但表示不同；不表示家庭成员、投保人或被保人的性别。', '男、女', 'client_sex'),
    ('newValueLabel', '客户价值(A1-A1类，A2-A2类，A3-A3类，A4-A4类，B-B类，C-C类，D-D类，E-E类，F-F类)', '客户当前的价值等级，用于客户价值分层查询，不表示金额区间。按字段注释，A1、A2、A3、A4 分别表示 A1类、A2类、A3类、A4类，B、C、D、E、F 分别表示 B类、C类、D类、E类、F类。本表枚举列将后五类写为 B类至 F类；读取时保留实际返回表示，不自行推导等级形成规则或资产阈值。', 'A1、A2、A3、A4、B类、C类、D类、E类、F类', 'new_value_label'),
    ('clientTemperature', '客户温度描述', '客户当前活跃度或接触温度的等级名称：冷却、低温、中温、高温。与详情接口温度编码 0、1、2、3 分别对应；用于温度分层或等级范围查询，不表示最近联系日期、联系次数或完整接触记录，不能从等级直接反推这些信息。', '冷却、低温、中温、高温', 'client_temperature'),
    ('vipType', '平安VIP(1-高价值，2-高潜力，3-高忠诚)', '按本接口中文注释及枚举，表示平安 VIP 客户分类编码：1=高价值，2=高潜力，3=高忠诚，业务口径对应客户详情的 paVipType。虽同名为 vipType，但不能套用其他客户表同名字段的寿险 VIP 等级含义；也不同于终身 VIP 标志和客户价值等级。', '1、2、3', 'vip_type'),
    ('clientGroupLabel', '客户分群(06-奋斗青年,07-都市白领,08-新晋父母,09-社会中坚,10-邻退天命,11-慈爱祖辈,12-创业新贵,13-创富一代,14-荣耀高堂,15-承富二代,16-已退小康)', '客户画像分群编码，用于按明确客群标签查询。06=奋斗青年，07=都市白领，08=新晋父母，09=社会中坚，10=邻退天命，11=慈爱祖辈，12=创业新贵，13=创富一代，14=荣耀高堂，15=承富二代，16=已退小康。按字符串保留前导零，并与 clientGroupLabelValue 配对；本表注释和当前示例均为 08=新晋父母。跨接口分群名称及编码映射需另行核对，不能自行认定不同名称为同义词。', '06、07、08、09、10、11、12、13、14、15、16', 'client_group_label'),
    ('clientName', '客户姓名', '客户本人自然人姓名的明文，用于姓名展示或姓名匹配；不表示家庭成员姓名，也不表示客户作为投保人、被保人等保单角色的专用姓名字段，不用于公司、机构、地址或产品名称查询。', '', 'client_name'),
    ('isBuyProperty', '是否产险客户', '客户是否为产险客户的购买标志，用于区分有购买、没有购买产险的客户。本接口使用 0、1，现有表格尚未明确它们与有购买、没有购买的对应关系。产险客户与车险客户的范围不同。', '0、1', 'is_buy_property'),
    ('mariSts', '婚姻状况(1-未婚，2-已婚，3-离婚，5-丧偶)', '客户本人婚姻状况编码：1=未婚，2=已婚，3=离婚，5=丧偶，用于婚姻状况查询。与搜客户支持表中的中文婚姻标签属于同一业务含义，但本接口没有提供“未知”的编码，不能自行补充映射。', '1、2、3、5', 'mari_sts'),
    ('clientAge', '年龄', '客户本人的年龄，用于精确年龄或年龄区间查询，区别于家庭成员年龄。示例为整数岁，比较时按数值理解；年龄计算时点及算法以接口业务口径为准，不能直接从其他接口含未来年份的生日示例重算年龄。', '', 'client_age'),
    ('newClientType', '客户类型(1-客户，2-准客，3-用户)', '新口径客户类型编码：1=客户，2=准客，3=用户，用于区分客户身份。与客户详情 newClientType 的口径一致，不可套用旧 clientType 的 01=准客户、02=客户。该分类不能单独证明是否持有寿险保单，仍需结合寿险客户号及保单信息判断。', '1、2、3', 'new_client_type'),
    ('lifetimeVipFlag', '终身VIP(Y-是，N-否)', '客户是否为终身 VIP 的标志。按本字段中文注释及客户详情同名字段，Y=是，N=否；N 仅表示不是终身 VIP，不表示不是当前 VIP 或某个会员等级。', 'Y、N', 'lifetime_vip_flag'),
    ('familyInfo', '家庭信息列表', '客户已记录的家庭成员信息数组，用于查看配偶、父母、子女等家庭关系。每个对象中的 familyClientMainId、familyClientSex、familyRelation、familyClientNameCipher 共同描述同一位成员，需成组读取，不能跨成员混配。本接口父路径为 familyInfo，对应详情接口的 familyInfoList；空值不证明客户没有家庭成员。', '', 'family_info'),
    ('clientMainPhoneList', '客户手机号列表', '客户已记录的手机号明文列表，以字符串数组返回，可包含多个号码；用于查看客户全部已记录手机号，应逐项读取。业务含义对应详情的 clientMainPhoneListCipher 解密后的手机号列表，不能将数组整体视为一个号码，也不能把该列表误作家庭成员手机号列表。', '', 'client_main_phone_list'),
    ('clientPolPhoneList', '客户保单手机号列表', '客户保单中留存的手机号明文列表，以字符串数组返回，可包含多个号码；业务含义对应详情的 clientPolPhoneListCipher 解密后的列表。号码具体属于投保人、被保人或其他角色的对应规则未说明，不能把全部号码认定为客户本人当前手机号，也不能按下标擅自关联某张保单或某个角色。', '', 'client_pol_phone_list'),
    ('familyInfo.familyClientMainId', '客户中心客户id', '路径 familyInfo.familyClientMainId。当前家庭成员在客户中心的 ID，用于识别并关联该家庭成员；不是当前客户本人的 clientMainId。必须与同一成员对象的 familyRelation、familyClientSex 和姓名密文成组读取。', '', 'family_info.family_client_main_id'),
    ('familyInfo.familyClientNameCipher', '姓名国密', '路径 familyInfo.familyClientNameCipher。当前家庭成员姓名的国密密文，用于经授权解密后获取家属姓名。与同一成员对象的 familyRelation 和 familyClientMainId 配对；不是客户本人姓名，也不能把密文直接当作明文姓名查询或展示。', '', 'family_info.family_client_name_cipher'),
    ('familyInfo.familyClientSex', '性别 (F-女，M-男)', '路径 familyInfo.familyClientSex。当前家庭成员性别编码：F=女，M=男；与搜客户支持表中的“女/男”属于同一含义但表示不同。查询某关系成员的性别时，应同时读取同一成员对象的 familyRelation，不能误作客户本人性别。', 'F、M', 'family_info.family_client_sex'),
    ('familyInfo.familyRelation', '家庭关系', '路径 familyInfo.familyRelation。当前成员与所查客户的家庭关系，示例有配偶、子女、父母；“子女”表示该成员是所查客户的子女。用于查看家庭构成或定位某种关系的成员，需关联同一对象的成员 ID、姓名密文和性别，不跨成员混配。', '', 'family_info.family_relation'),
)

_metadata = SearchMetadata(
    fields=V1_FIELDS, old_definitions=legacy.DEFINITIONS,
    old_aliases=legacy.SOURCE_ALIASES, old_canonical=legacy.canonical_path,
    old_describe=legacy.describe_output_field,
    extra_aliases={
        'agentno': 'agentNo', 'idicm_clientno': 'clientMainId',
        'searchClientName': 'clientName', 'searchclientname': 'clientName',
        'familyinfo': 'familyInfo',
        'familyinfo.familyclientmainid': 'familyInfo.familyClientMainId',
        'familyinfo.familyclientnamecipher': 'familyInfo.familyClientNameCipher',
        'familyinfo.familyrelation': 'familyInfo.familyRelation',
        'familyinfo.familyclientsex': 'familyInfo.familyClientSex',
    },
    v1_keys={'clientMainId', 'client_main_id', 'clientName', 'client_name',
             'newClientType', 'new_client_type', 'lifetimeVipFlag', 'lifetime_vip_flag',
             'clientMainPhoneList', 'client_main_phone_list', 'clientPolPhoneList', 'client_pol_phone_list'},
    legacy_keys={'idicm_clientno', 'searchClientName', 'searchclientname', 'clientNo',
                 'clientno', 'planabbrnames', 'polNoInfo', 'polnoinfo'},
    divergent={'vipType', 'isBuyPension', 'isBuyHealth', 'isBuyProperty', 'mariSts',
               'clientGroupLabel', 'newValueLabel', 'familyInfo.familyClientSex'},
    customer=True,
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

IDENTITY_ALIASES = frozenset({'clientMainId', 'client_main_id', 'idicm_clientno'})


def require_customer_id(record: Mapping[str, JsonValue]) -> str:
    """身份必须严格一致，null、空白、类型漂移均不能回退到另一别名。"""
    values = [(key, record[key]) for key in IDENTITY_ALIASES if key in record]
    if not values or has_alias_conflict(values):
        raise ValueError('客户身份缺失或同源别名冲突')
    value = values[0][1]
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError('客户身份必须为非空白且不超过 64 字符的字符串')
    return value
