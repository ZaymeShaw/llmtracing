"""客户详情 v5 独立目录；来自工作簿定义，不影响搜索 V14。"""
from dataclasses import dataclass
from pydantic import JsonValue, BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator
from tool_adapter_sdk.json_values import StrictJsonValue
from tool_adapter_sdk.output_descriptions import describe_output_page

@dataclass(frozen=True)
class DetailField:
    path: str
    label: str
    meaning: str
    domain: str
    aliases: str = ""

FIELDS = (
    DetailField('clientMainId', '客户中心客户id', '当前客户在客户中心的客户标识，用于关联客户详情、动态、笔记及家庭成员信息。返回字符串，准客和已成单客户均可有此 ID；与寿险系统客户号 clientNo 区分，不根据示例套用搜客户表的 ID 格式。', ''),
    DetailField('agentNo', '业务员编号', '服务该客户的代理人员工编号，用于按代理人识别客户归属或关联服务人员信息。返回编号字符串，保留原值，不作为数值计算。', ''),
    DetailField('clientNo', '寿险客户号', '客户的寿险系统客户号，用于关联寿险业务数据，返回标识字符串并保留前导零。与客户中心 clientMainId 区分；未成单潜客或意向客户可能没有此号。按 mainClientNo 注释，两者应一致。', ''),
    DetailField('mainClientNo', '寿险客户号，和clientNo取值一致', '寿险系统客户号，用于关联寿险业务客户信息。注释明确应与 clientNo 一致；不同于客户中心 ID；未成单潜客或意向客户可能尚无寿险客户号，只有成交了的寿险客户才有这个客户号。', ''),
    DetailField('birthday', '生日', '客户生日相关日期，用于生日关怀或生日月日查询，示例格式 yyyy-MM-dd HH:mm:ss。示例混有历史出生年份和未来生日年份，如 2027-02-26，不能统一认定为出生日期或直接按年份计算年龄；若需出生年月日，必须先确认此接口的年份口径。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。现有示例均按日期补 00:00:00，造数参考保持此表示，不把零点解释为实际业务发生时刻。', ''),
    DetailField('carBrand', '车辆品牌', '客户车辆品牌或品牌车型文本，用于了解车辆信息。示例“北京现代BH7161HAY”包含品牌与型号，不能当作严格的纯品牌枚举；与车牌号码 carNumber、购置价 vehclPriceAmt 区分。', ''),
    DetailField('carNumber', '车牌号码', '客户车辆的车牌号码，用于识别具体车辆或按车牌查找车辆相关信息。返回车牌字符串，不是车险保单号；有车牌不代表车险当前有效，空值也不能直接认定无车。', ''),
    DetailField('clientAge', '年龄', '客户本人的年龄，用于精确年龄或年龄区间查询。返回数字字符串，进行比较时按数值处理，区别于家庭成员年龄。年龄计算时点和算法未说明；birthday 示例有非出生年份日期，不能据此无条件重算年龄。\n示例精度（造数参考）：整数，0 位小数；按数字字符串返回，如 "38"，沿用 String 类型。', ''),
    DetailField('clientMainPhoneListCipher', '客户手机号列国密（解密后为，字符串数组）', '客户手机号列表的国密密文。接口外层返回 String，按注释解密后为字符串数组，可包含多个手机号；用于查询客户全部已记录号码。解密后逐项读取，不能直接把密文、数组整体或数组下标当作某个手机号。', ''),
    DetailField('clientNameCipher', '客户姓名国密', '客户本人姓名的国密密文，用于经授权解密后识别客户姓名。返回加密字符串，不能直接按明文姓名匹配或当作姓名展示；家庭成员姓名另见 familyInfoList.familyClientNameCipher。', ''),
    DetailField('clientPolPhoneListCipher', '客户保单手机号列表国密（解密后为，字符串数组）', '客户保单中记录的手机号列表的国密密文。接口返回 String，按注释解密后为字符串数组，用于查看保单留存联系方式，可能有多个号码。号码具体属于投保人、被保人或其他角色的对应规则未说明，不能全部认定为客户本人当前手机号。', ''),
    DetailField('clientSex', '性别', '客户本人的性别，用于性别筛选或画像。示例使用 M、F；参照本表家庭成员性别注释，M=男、F=女。搜客户表使用中文“男/女”，跨接口比较时需转换表示。家庭成员性别另见 familyInfoList.familyClientSex。', ''),
    DetailField('companyAddressCipher', '公司地址', '客户公司地址的密文，用于在具备解密能力时取得工作地点。返回加密字符串，不能直接按地区或街道文本解释；本表示例均为 null，未提供可核实的明文格式。工作单位名称另见 workUnit。', ''),
    DetailField('compName', '单位任职部门', '客户单位任职部门信息，用于了解工作背景。注释称“单位任职部门”，示例还含单位名称、个体工商户或地区文字，内容口径不完全统一；按原文理解，不强行当作职业编码、规范部门或工作地址。工作单位另见 workUnit。', ''),
    DetailField('contactAddressCipher', '联系地址', '客户联系地址的密文，用于在具备解密能力时取得通信或联系地址。返回加密字符串，不能直接作为地区文本查询；不保证与 homeAddressCipher 家庭地址或 companyAddressCipher 公司地址相同。', ''),
    DetailField('country', '国家代码', '客户国家代码，与 countryName 配套，用于识别客户国家信息。示例配对为 DOM=多米尼加共和国、VRC=中华人民共和国，属于样例观察，完整系统字典未提供。不能自行按其他国家代码标准改写 VRC 或扩展未出现的映射。', ''),
    DetailField('countryName', '国家代码名称', '客户国家信息的中文名称，对应 country。示例有“多米尼加共和国”“中华人民共和国”，用于解释国家代码，区别于户籍或当前居住地。', ''),
    DetailField('education', '学历编码（1=博士后，2=博士研究生，3=硕士研究生，4=大学本科生，5=大学专科，6=中专，7=高中，8=初中，9=小学，10=小学以下，12=其它，99=不详；接口原值可含前导零）', '客户本人的学历编码，用于学历筛选、客户画像及理解学历描述，与 eduDescription 配对。\n已确认字典：1=博士后，2=博士研究生，3=硕士研究生，4=大学本科生，5=大学专科，6=中专，7=高中，8=初中，9=小学，10=小学以下，12=其它，99=不详。\n图中字典以1—9表示，本接口示例使用01—09；解释时分别对应相同学历，返回时保持原字符串及前导零，不统一改写位数。12=其它，99=不详，两者含义不同；编码数值不能作为学历高低排序依据。', ''),
    DetailField('eduDescription', '学历描述（对应education：1=博士后，2=博士研究生，3=硕士研究生，4=大学本科生，5=大学专科，6=中专，7=高中，8=初中，9=小学，10=小学以下，12=其它，99=不详）', '客户学历编码 education 对应的中文描述，用于直接理解、展示或按学历名称查询。\n已确认对应关系：1=博士后，2=博士研究生，3=硕士研究生，4=大学本科生，5=大学专科，6=中专，7=高中，8=初中，9=小学，10=小学以下，12=其它，99=不详。\n接口中的01—09分别按字典1—9解释；12对应“其它”，99对应“不详”。“其它”表示字典所列其他学历类别，“不详”表示学历信息不明确，不能相互替换或推断具体学历层级。', ''),
    DetailField('email', '电子邮件', '客户电子邮箱地址，用于查看邮件联系方式或按邮箱识别客户。返回邮箱文本；空值表示未提供邮箱，不能据此判断其他联系方式是否存在。', ''),
    DetailField('homeAddressCipher', '家庭地址', '客户家庭地址的密文，用于在具备解密能力时取得家庭住址，支持居住地信息查询。返回加密字符串，不能直接当作明文地址、地区名称或联系方式解释；与联系地址 contactAddressCipher、公司地址 companyAddressCipher 区分。', ''),
    DetailField('houseStatus', '房产状况 (1单位分配 2自购无贷款 3自购有贷款 4租用 5亲属住房 9其它)', '客户住房占有或居住方式，用于了解住房条件及贷款状况。1=单位分配，2=自购无贷款，3=自购有贷款，4=租用，5=亲属住房，9=其它；对应 houseStatusDesc。该字段比“有房/无房”更细，不能把全部非空值都视为自有房产。', ''),
    DetailField('houseStatusDesc', '房产状况描述', '客户房产或居住状况的中文描述，对应 houseStatus：单位分配、自购无贷款、自购有贷款、租用、亲属住房、其它。用于解释住房条件及贷款状况；租用或亲属住房不能直接归为自有房产。', ''),
    DetailField('mariDescription', '婚姻状况描述', '客户本人婚姻状况的中文描述，对应 mariSts：未婚、已婚、离婚、丧偶。用于直接解释婚姻状态。', ''),
    DetailField('mariSts', '婚姻状况(1-未婚，2-已婚，3-离婚，5-丧偶)', '客户本人婚姻状况编码：1=未婚，2=已婚，3=离婚，5=丧偶；对应 mariDescription。用于婚姻状况查询。搜客户表虽含“未知”，本接口未给其编码，不能自行补 0、4、9 等映射。', ''),
    DetailField('phoneNoCipher', '手机号国密', '客户本人手机号的国密密文，用于经授权解密后取得联系电话。返回加密字符串，不能直接进行明文号码、尾号匹配，也不能把密文当作手机号展示。多号码信息另见 clientMainPhoneListCipher、clientPolPhoneListCipher。', ''),
    DetailField('position', '职务', '客户在单位担任的职务或岗位，用于了解工作角色，示例“企业职能部门经理或主管”。与职业大类 profName、工作单位 workUnit、任职部门 compName 区分，不是单位名称。', ''),
    DetailField('profCode', '职业', '客户职业代码，对应 profName 职业描述，用于职业信息关联。示例为 3010202、3010101 等字符串，完整代码字典未提供；不能凭数字大小推断职业、危险等级或职业类别，也不能直接套用搜客户职业名称枚举。', ''),
    DetailField('profName', '职业描述', '客户本人的职业中文描述，对应 profCode，用于职业或从业类型查询。职业不等于工作单位、任职部门或地址。', ''),
    DetailField('registerPlace', '户籍', '客户户籍所在地编码，用于识别户籍地区，与 regPlaceDescription 配套。示例 V20 配“北京”，但未提供完整地区字典，不据此推导其他代码。户籍不同于当前住址、工作地址或国籍。', ''),
    DetailField('regPlaceDescription', '户籍描述', '客户户籍所在地的中文描述，对应 registerPlace 编码，示例为“北京”。用于查询或解释户籍地区，不能当作当前居住地、联系地址或国籍。', ''),
    DetailField('revenue', '年收入档位编码（1=年收入30万以上，2=年收入10万-30万，3=年收入3万-10万，4=年收入1万-3万，5=年收入1万以下，10=年收入30万-50万，11=年收入50万-100万，12=年收入100万-300万，13=年收入300万以上；历史示例另有金额样式字符串）', '客户年收入档位编码，用于识别收入层级或查询某个收入档位，与 revenueDescription 配对。\n已确认字典：1=年收入30万以上，2=年收入10万-30万，3=年收入3万-10万，4=年收入1万-3万，5=年收入1万以下，10=年收入30万-50万，11=年收入50万-100万，12=年收入100万-300万，13=年收入300万以上。本表代码样例“013”对应13（年收入300万以上），“002”对应2（年收入10万-30万）；代码保留原字符串及前导零，不能解释为13元或2万元。\n历史样例仍有“60.00”“18.00”“0.00”等金额样式字符串，实际金额单位及与编码混用的返回规则尚未确认；本次字典补充不意味着这些值可按档位编码解释，也不能默认按万元换算。数字样式保留2位小数，代码不作金额精度转换。\n码1“年收入30万以上”与10—13的细分档位存在覆盖关系；按返回编码精确理解，不擅自合并、替换，也不推定区间端点包含规则。', ''),
    DetailField('revenueDescription', '年收入档位描述（对应revenue：1=年收入30万以上，2=年收入10万-30万，3=年收入3万-10万，4=年收入1万-3万，5=年收入1万以下，10=年收入30万-50万，11=年收入50万-100万，12=年收入100万-300万，13=年收入300万以上）', '客户年收入档位的中文描述，与 revenue 的同一档位编码配套，用于解释收入水平及按档位查询。\n已确认对应关系：1=年收入30万以上，2=年收入10万-30万，3=年收入3万-10万，4=年收入1万-3万，5=年收入1万以下，10=年收入30万-50万，11=年收入50万-100万，12=年收入100万-300万，13=年收入300万以上。“013”对应“年收入300万以上”，“002”对应“年收入10万-30万”。\n这些名称表示档位或区间，不是精确收入金额；不能据此补出一个具体收入数值。码1是宽口径“年收入30万以上”，与10—13的细分档位重叠，保留原描述；区间端点规则未提供。历史金额样式 revenue 的单位仍未确认，描述为空不等于年收入为0。', ''),
    DetailField('vehclPriceAmt', '车辆购置价(具体数值)', '客户车辆购置价格，用于了解车辆价值或购置价区间。返回数字形式字符串，如 86800、40000；金额单位、币种及是否含税未说明，使用前需确认。示例 0 的业务含义未明确，不能直接认定为无车或车辆免费。\n示例精度（造数参考）：整数形式，0 位小数；按数字字符串返回，如 "86800"、"40000"，沿用 String 类型。', ''),
    DetailField('workUnit', '工作单位', '客户工作单位名称，用于查询任职机构或了解职业背景。返回原始文本，示例也包含个体工商户、地区或地址文字；不能当作职业类别 profName 或单位任职部门 compName 的等价字段。', ''),
    DetailField('workYears', '现单位工作年数', '客户在现单位的工作年数或年限区间，用于了解任职稳定性。返回字符串，示例“5-8”表示工作年数区间，不能直接按单个年数计算；区间是否含边界及其他档位未说明。\n示例精度（造数参考）：年数区间两端均为整数，0 位小数；保留 "5-8" 这样的区间字符串，不转成单个数值。', ''),
    DetailField('idNoCipher', '证件号国密', '客户证件号码的国密密文，用于经授权解密后识别证件号码或与证件类型配合查询。返回加密字符串，不能直接做明文证件号匹配，也不能从密文推算出生日期、性别或地区；证件种类见 idType/idTypeName。', ''),
    DetailField('idType', '证件类型 (A-出生证,1-身份证,0-户口本,D-港澳台居住证,3-军人证,6-港澳台证件,2-护照,C-外国人居留', '客户证件类型编码，用于识别证件种类：A=出生证，1=身份证，0=户口本，D=港澳台居住证，3=军人证，6=港澳台证件，2=护照，C=外国人居留证（注释尾部截断，名称参照搜客户表）。对应 idTypeName，不表示证件号码或有效期。', ''),
    DetailField('idTypeName', '证件名称', '客户证件类型的中文名称，用于解释 idType 编码，如身份证、出生证、户口本、护照、军人证、港澳台证件、港澳台居住证、外国人居留证。应与同一客户的 idType 配对读取；当前示例中 1=身份证、A=出生证、0=户口本。', ''),
    DetailField('idValidDate', '证件有效期至', '客户证件有效期截止时间，用于判断证件到期、即将到期或过期。类型为 String，示例按 yyyy-MM-dd HH:mm:ss 返回，区别于有效期开始日 idValidDateStart。示例 9999-12-31 的特殊含义未正式定义，不能未经确认当作普通到期日或直接解释为长期有效。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。现有示例均按日期补 00:00:00，造数参考保持此表示，不把零点解释为实际业务发生时刻。', ''),
    DetailField('idValidDateStart', '证件有效期开始日', '客户证件有效期开始日期，与 idValidDate 截止时间组成有效期间。用于查询证件何时开始有效，示例 yyyy-MM-dd HH:mm:ss。不得直接等同于证件办理、签发或客户出生日期。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。现有示例均按日期补 00:00:00，造数参考保持此表示，不把零点解释为实际业务发生时刻。', ''),
    DetailField('height', '身高', '客户本人身高，用于补充体格信息。返回数字形式的字符串，示例 175.00；表中未注明计量单位，使用范围条件或体格计算前需确认单位。空值表示未提供。\n示例精度（造数参考）：数字字符串保留 2 位小数，包括末尾的 0，如 "175.00"；沿用 String 类型。', ''),
    DetailField('isInsurantMedicare', '是否有社保(Y-是，N-否)', '客户是否有社保的标识，用于了解基础社会保障配置：Y=是，N=否。依据注释理解为社保，不等同于购买商业医疗保险；空值仅表示未提供信息。', ''),
    DetailField('weight', '体重', '客户本人体重，用于补充体格信息。返回数字形式字符串，示例 60.00；表中未注明单位，进行范围查询或 BMI 等计算前需确认单位。空值表示未提供，不能解释为 0。\n示例精度（造数参考）：数字字符串保留 2 位小数，包括末尾的 0，如 "60.00"；沿用 String 类型。', ''),
    DetailField('clientType', '客户类型 01：准客户,02：客户', '旧口径客户类型编码：01=准客户，02=客户，用于区分客户身份。按字符串返回并保留前导零，当前示例均为“02”。与 newClientType 的 1=客户、2=准客、3=用户属于不同编码体系，不可混用，不能把本字段“02”解释为准客。', ''),
    DetailField('isBuyCar', '产险产品:(Y车险，N非车险)', '客户产险产品分类标识，用于区分车险与非车险：Y=车险，N=非车险。对应 isBuyCarValue。N 不等同于没有购买产险，不能由此判断名下是否有车或某张保单是否有效。', ''),
    DetailField('isBuyCarValue', '产险产品描述:(Y车险，N非车险)', '客户产险产品属于车险还是非车险的中文描述：车险对应 isBuyCar=Y，非车险对应 N。用于查看车险类别；“非车险”不能解释为未购买任何产险，也不能推断购买日期或保单是否有效。', ''),
    DetailField('newClientType', '客户类型(1-客户,2-准客,3-用户)', '新口径客户类型编码，用于区分客户身份：1=客户，2=准客，3=用户；对应 newClientTypeValue。不可与 clientType 的 01=准客户、02=客户混用。搜客户表将客户/准客描述为买过保险、用户为未买过保险，具体是否成单及寿险持有情况仍需结合业务信息。', ''),
    DetailField('newClientTypeValue', '客户类型描述', '新口径客户类型的中文描述，对应 newClientType：1=客户，2=准客，3=用户。用于解释当前客户身份，不能套用 clientType 的 01/02 编码。是否持有寿险保单还需结合客户号和保单信息判断。', ''),
    DetailField('orphanType', '存量客户类型(1-纯存续单客户,2-非纯存续单客户,3-在职有效客户)', '存量客户服务关系类型编码：1=纯存续单客户，2=非纯存续单客户，3=在职有效客户；对应 orphanTypeValue。用于查询存续客户类型，不能据此直接计算保单数量或判断每张保单的有效状态；未提供类型划分算法。', ''),
    DetailField('orphanTypeValue', '存量客户类型描述', '存量客户的服务关系类型描述，对应 orphanType：1=纯存续单客户，2=非纯存续单客户，3=在职有效客户。用于存续客户类型查询；具体划分算法未说明，不能仅凭该标签断定每张保单的有效状态。', ''),
    DetailField('trusteeshipFlag', '保单托管(Y-已托管,N-未托管)', '客户保单是否托管的标识：Y=已托管，N=未托管，用于托管状态查询。搜客户表使用“是/否”，跨接口需转换表示。未托管不等于没有保单，空值也不等于 N。', ''),
    DetailField('clientGroupLabel', '客户分群(06-奋斗青年,07-都市白领,08-新晋父母,09-社会中坚,10-邻退天命,11-慈爱祖辈,12-创业新贵,13-创富一代,14-荣耀高堂,15-承富二代,16-已退小康)', '客户画像分群编码，用于按明确客群标签查询。06=奋斗青年，07=都市白领，08=新晋父母，09=社会中坚，10=邻退天命，11=慈爱祖辈，12=创业新贵，13=创富一代，14=荣耀高堂，15=承富二代，16=已退小康。按字符串保留前导零，并与 clientGroupLabelValue 配对；本表注释和当前示例均为 08=新晋父母。跨接口分群名称及编码映射需另行核对，不能自行认定不同名称为同义词。', ''),
    DetailField('clientGroupLabelValue', '客户分群描述', '客户分群的中文描述，对应 clientGroupLabel，表示画像标签，不应仅凭年龄或资产自动归类。本接口 08 对应“新晋父母”，与当前示例一致。与搜客户表关联时，应另行核对分群名称及编码映射，不自行合并或替换不同名称。', ''),
    DetailField('clientTemperature', '客户温度(3-高温,2-中温,1-低温,0-冷却)', '客户当前活跃度或接触温度的编码：3=高温，2=中温，1=低温，0=冷却。用于精确温度或温度等级范围查询；搜客户表返回中文标签，需转换表示。具体接触解释见 clientTemperatureDesc，不能从等级直接推算联系日期或次数。', ''),
    DetailField('clientTemperatureDesc', '客户温度描述', '当前客户温度的解释文案，用于回答温度形成的接触背景。示例为“近6个月有效接触1次”或面访日期、次数等文字；具体等级见 clientTemperature/clientTemperatureValue。该文案不是标准时间字段，也不能当作完整接触流水或由单条示例推导全套温度计算规则。', ''),
    DetailField('clientTemperatureValue', '客户温度描述', '客户当前活跃度或接触温度的等级名称，对应 clientTemperature：3=高温，2=中温，1=低温，0=冷却。用于温度分层查询；具体接触事实见 clientTemperatureDesc，上月末等级见 premClientTemperatureValue。', ''),
    DetailField('familyOfficeFlag', '家办高客标识（Y：有效，N：无效已删除,null：无家办信息）', '家办高客信息状态，用于识别客户是否存在有效家办信息：Y=有效，N=无效已删除，null=无家办信息。N 与 null 含义不同，不能都归为从未有过家办信息，也不能据此推断资产金额或其他会员等级。', ''),
    DetailField('lifetimeVipFlag', '终身VIP(Y-是,N-否)', '客户是否为终身 VIP 的标识：Y=是，N=否，用于终身会员身份查询。N 只表示不是终身 VIP，不表示不是当前 VIP，也不表示当前会员等级。', ''),
    DetailField('newValueLabel', '客户价值(A1-A1类,A2-A2类,A3-A3类,A4-A4类,B-B类,C-C类,D-D类,E-E类,F-F类)', '客户当前价值等级编码，用于价值分层查询：A1=A1类，A2=A2类，A3=A3类，A4=A4类，B=B类，C=C类，D=D类，E=E类，F=F类。对应 newValueLabelValue，不表示金额区间；历史等级见 premValueLabel。具体等级比较顺序、资产阈值和形成规则不能仅由代码或示例推导。', ''),
    DetailField('newValueLabelDesc', '客户价值描述', '客户当前价值标签的解释文案，用于了解价值分层依据，示例包含“可投资资产:P3,VIP等级:黄金V2”。本字段是说明文本，不是价值等级本身或精确资产金额；P1/P2/P3 的资产区间和等级计算规则未提供，不能自行换算。\n生产数据说明：本字段文案中的“白银V1/白银V2”与 vipTypeValue 中的“白银1/白银2”存在名称写法差异。按生产实际返回的原文保留，不自行统一名称或删除 V。', ''),
    DetailField('newValueLabelValue', '客户价值描述', '客户当前价值等级的中文标签，对应 newValueLabel，如 A1类客户、B类客户、E类客户。用于解释价值分层，不是资产金额。与 premValueLabelValue 的上月末标签区分；等级形成依据另见 newValueLabelDesc。', ''),
    DetailField('paVipType', '平安VIP(1-高价值,2-高潜力,3-高忠诚)', '平安 VIP 客户分类编码：1=高价值，2=高潜力，3=高忠诚；对应 paVipTypeValue。用于查询该 VIP 体系分类，与寿险 VIP 等级 vipType、终身会员标志 lifetimeVipFlag 和客户价值等级 newValueLabel 分开理解。', ''),
    DetailField('paVipTypeValue', '平安VIP描述', '平安 VIP 的客户分类描述，对应 paVipType：1=高价值，2=高潜力，3=高忠诚。用于查看该体系的客户分类，与寿险 VIP 等级 vipTypeValue 及 A/B/C 类客户价值标签区分。', ''),
    DetailField('premClientTemperature', '客户温度(上月末)(3-高温,2-中温,1-低温,0-冷却)', '客户上月末的活跃度温度编码：3=高温，2=中温，1=低温，0=冷却。用于历史温度查询或与当前 clientTemperature 比较，对应 premClientTemperatureValue；上月末的具体日期依接口统计时点确定。', ''),
    DetailField('premClientTemperatureValue', '客户温度(上月末)描述', '客户上月末温度等级名称，对应 premClientTemperature：3=高温，2=中温，1=低温，0=冷却。用于查看历史活跃度或与当前 clientTemperatureValue 比较，不是具体联系日期、次数或当前温度。', ''),
    DetailField('premValueLabel', '客户价值(上月末)(A1-A1类,A2-A2类,A3-A3类,A4-A4类,B-B类,C-C类,D-D类,E-E类,F-F类)', '客户上月末的价值等级编码，用于查询历史分层或与当前 newValueLabel 比较。A1=A1类，A2=A2类，A3=A3类，A4=A4类，B=B类，C=C类，D=D类，E=E类，F=F类。对应 premValueLabelValue；“上月末”随接口统计时点确定，不表示当前等级。', ''),
    DetailField('premValueLabelValue', '客户价值(上月末)描述', '客户上月末价值等级的中文标签，对应 premValueLabel：A1/A2/A3/A4/B/C/D/E/F 分别表示对应类别客户。用于查看历史价值分层，与当前 newValueLabelValue 区分，不代表资产金额或上月末保费。', ''),
    DetailField('vipType', '客户寿险VIP等级编码（C=原黄金VIP，B=原铂金VIP，C1=黄金V1，C2=黄金V2，C3=黄金V3，B1=铂金V1，B2=铂金V2，A=钻石VIP，F=金钻VIP，E=黑钻VIP，S1=白银1，S2=白银2，S3=白银3，G=准会员）', '客户当前寿险VIP等级编码，用于按会员等级筛选、查询或解释会员身份，与 vipTypeValue 配对。\n已确认字典：C=原黄金VIP，B=原铂金VIP，C1=黄金V1，C2=黄金V2，C3=黄金V3，B1=铂金V1，B2=铂金V2，A=钻石VIP，F=金钻VIP，E=黑钻VIP，S1=白银1，S2=白银2，S3=白银3，G=准会员。\nG已明确表示准会员，不能解释为普客、无会员或未知等级。若生产返回的 vipTypeValue 为空，仍可依据 vipType=G 识别为准会员，但不改写原始返回值。本字段与 paVipType 平安VIP分类及 lifetimeVipFlag 终身VIP标志属于不同维度，不能相互替代。', ''),
    DetailField('vipTypeValue', '客户寿险VIP等级描述（对应vipType：C=原黄金VIP，B=原铂金VIP，C1=黄金V1，C2=黄金V2，C3=黄金V3，B1=铂金V1，B2=铂金V2，A=钻石VIP，F=金钻VIP，E=黑钻VIP，S1=白银1，S2=白银2，S3=白银3，G=准会员）', '客户当前寿险VIP等级的中文名称，与 vipType 编码配对，用于解释、展示或按会员等级名称查询。\n已确认对应关系：C=原黄金VIP，B=原铂金VIP，C1=黄金V1，C2=黄金V2，C3=黄金V3，B1=铂金V1，B2=铂金V2，A=钻石VIP，F=金钻VIP，E=黑钻VIP，S1=白银1，S2=白银2，S3=白银3，G=准会员；因此 vipType=G 对应“准会员”。生产样例中G的描述可能为空，空值仅表示该描述未返回，不改变G的编码含义。\n与 paVipTypeValue 平安VIP分类区分，空值不直接等同于普客。生产数据中本字段“白银1/白银2”与 newValueLabelDesc 文案“白银V1/白银V2”有名称写法差异，保留各字段实际返回值，不自行统一。', ''),
    DetailField('typcGrpDtyp', '典型客群需求编码（多值英文逗号拼接；A2=【保财富】保底增值，A4=【保财富】资产隔离，B1=【保养老】优享天年，B3=【保养老】自由规划，C1=【保传承】摇钱树，C2=【保传承】一池水，C4=【保传承】自由规划，D1=【保健康】大病补偿，D2=【保健康】就医补偿，D3=【保健康】意外保障，其他=其他）', '客户典型客群需求类型代码，用于识别或筛选财富、养老、传承、健康等需求标签，与 typcGrpDtypValue 配对；该标签表示需求，不等于已经持有对应保险产品。\n已确认映射：A2=【保财富】保底增值，A4=【保财富】资产隔离，B1=【保养老】优享天年，B3=【保养老】自由规划，C1=【保传承】摇钱树，C2=【保传承】一池水，C4=【保传承】自由规划，D1=【保健康】大病补偿，D2=【保健康】就医补偿，D3=【保健康】意外保障，其他=其他。\n多个代码按原顺序用英文逗号连接，例如“B1,A2,D2”；对应描述按相同顺序以“/”连接为“【保养老】优享天年/【保财富】保底增值/【保健康】就医补偿”。\n不能借用 newValueLabel 的同名代码含义，也不能将B3与C4仅因都含“自由规划”而合并。', ''),
    DetailField('typcGrpDtypValue', '典型客群需求描述（对应typcGrpDtyp；多值以“/”拼接；A2=【保财富】保底增值，A4=【保财富】资产隔离，B1=【保养老】优享天年，B3=【保养老】自由规划，C1=【保传承】摇钱树，C2=【保传承】一池水，C4=【保传承】自由规划，D1=【保健康】大病补偿，D2=【保健康】就医补偿，D3=【保健康】意外保障，其他=其他）', '客户典型需求的中文描述，用于理解、展示或查询财富、养老、传承、健康等需求，与 typcGrpDtyp 中各代码逐项配对。\n已确认对应关系：A2=【保财富】保底增值，A4=【保财富】资产隔离，B1=【保养老】优享天年，B3=【保养老】自由规划，C1=【保传承】摇钱树，C2=【保传承】一池水，C4=【保传承】自由规划，D1=【保健康】大病补偿，D2=【保健康】就医补偿，D3=【保健康】意外保障，其他=其他。\n多项描述沿用原示例以“/”连接，顺序与代码一致；保留“【保财富】”“【保养老】”“【保传承】”“【保健康】”分类前缀。例如B3为“【保养老】自由规划”，C4为“【保传承】自由规划”，两者不是同一需求。“其他”对应代码“其他”。', '字符串，多个值以'),
    DetailField('familyInfoList', '家庭信息', '客户已记录的家庭成员信息数组，用于查看配偶、父母、子女等关系。每条对象中的成员 ID、性别、关系、姓名密文属于同一个成员，需成组读取。null 不证明无家庭成员。\n空值约定：示例中的 null 表示 JSON 空值，输出为 null，不加引号；[] 表示空数组。有记录时返回对象数组，数组内字段按下方展开结构生成。', ''),
    DetailField('clientChildrenCnt', '客户子女个数', '客户子女数量，用于家庭结构和子女人数查询。返回整数，0 表示登记的子女数为零，null 表示未提供；不表示家庭总人数。familyInfoList 可能仅返回已登记关系，不能默认其中“子女”条数必等于本字段。\n示例精度（造数参考）：整数，0 位小数，如 0、1、2、3；沿用 Integer 类型。', ''),
    # 2026-09-24 用户确认 V5 查询字段 C76/D76、C77/D77 的角色描述写反，按确认结果纠正。
    DetailField('isClientApp', 'Y:是投保人', '表示客户是否为投保人，Y=是投保人，用于识别保单角色。示例还有 N，注释未明确其含义；其余值及具体保单范围需确认，null 不直接等同于否。', ''),
    DetailField('isClientIns', 'Y:是被保人', '表示客户是否为被保人，Y=是被保人，用于识别客户的保单角色。其他取值映射及具体保单范围未说明，null 不直接等同于否。', ''),
    DetailField('agentProdType', '持有综拓产品类别(caris-车辆交强险,vehcl-车辆商业险,esb-e生保,mid-中高端医疗,hjh-合家欢,homns-家财险,stins-学平险,welth-财富,healt-健康,life-生活)', '客户持有的综拓产品类别编码，用于识别综拓保障：caris=车辆交强险，vehcl=车辆商业险，esb=e生保，mid=中高端医疗，hjh=合家欢，homns=家财险，stins=学平险，welth=财富，healt=健康，life=生活。多个编码以英文逗号拼接，对应 agentProdTypeValue；不是具体险种名称。', '字符串，多个值以'),
    DetailField('agentProdTypeValue', '持有综拓产品类别描述', '客户持有的综拓产品类别名称，用于查看已配置的综拓保障。与 agentProdType 编码配套，示例以英文逗号拼接多个名称，如 e生保,健康。类别包括车辆交强险、车辆商业险、e生保、中高端医疗、合家欢、家财险、学平险、财富、健康、生活；不表示具体产品名称。', '字符串，多个值以'),
    DetailField('holdLifeRsty', '持有寿险产品责任类型(li-寿险(Life Insurance)、ci-重疾(Critical Illness)、acc-意外(accident)、med-医疗(medicine)、weal-财富(wealth)、pens养老', '客户持有的寿险产品责任类型编码，用于查询责任覆盖：li=寿险(Life Insurance)，ci=重疾(Critical Illness)，acc=意外(accident)，med=医疗(medicine)，weal=财富(wealth)，pens=养老。多个编码以英文逗号拼接，对应 holdLifeRstyValue。责任类别不等同于具体产品名或 pcategorys 的险种分类。', '字符串，多个值以'),
    DetailField('holdLifeRstyValue', '持有寿险产品责任类型描述', '客户持有的寿险产品责任类型描述，与 holdLifeRsty 配套，用于了解保障责任覆盖。描述包括寿险(Life Insurance)、重疾(Critical Illness)、意外(accident)、医疗(medicine)、财富(wealth)、养老(pension)，示例以英文逗号拼接多项。属于责任分类，不能直接替代具体险种名称或搜客户表 pcategorys 分类。', '字符串，多个值以'),
    DetailField('annPremAmt', '年缴保费(具体值)', '接口返回的客户年缴保费具体金额，用于查询年缴保费精确值或区间，区别于累计已缴保费、保单总保费和总保额。返回数值；金额单位、纳入保单范围及按投保人/被保人汇总的口径未说明，不自行重算或将 0 等同于无保单。\n示例精度（造数参考）：最多 1 位小数，如 910.2、37223.1；示例也有整数 0、41745。按该精度控制造数，不据此推断接口支持的最大小数位数或金额单位。', ''),
    DetailField('insnoSeqAmt', '产品总保额(具体值)', '接口返回的客户产品总保额具体金额，用于总保额精确值或区间查询，区别于年缴保费、保单总保费和已交保费。返回数值；金额单位、产品范围、角色及重复保障合并规则未说明，不自行相加推算，0 也不能单独证明无保单。\n示例精度（造数参考）：最多 2 位小数，如 794208.78、40374.55；示例也有整数 0。按该精度控制造数，不据此推断接口支持的最大小数位数或金额单位。', ''),
    DetailField('dateCreated', '创建时间(客户添加时间)', '客户被添加到系统中的日期时间，用于查询建档时间、新增客户或添加时间区间，示例 yyyy-MM-dd HH:mm:ss。参照搜客户口径，未另指定范围的“新客户/新客”指近1个月内添加；该时间不同于首次投保、承保或保单生效时间。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。保留实际示例中的时、分、秒，不统一截成日期或补成零点。', ''),
    DetailField('effAnnDate', '保单周年日', '接口返回的保单周年日期，用于周年提醒或周年时间查询。示例为含年份的 yyyy-MM-dd HH:mm:ss，而搜客户表以月日表达，跨接口需区分表示。客户多保单时如何选取此单值未说明，不可认定为所有保单周年日，也不等于投保或生效日期。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。现有示例均按日期补 00:00:00，造数参考保持此表示，不把零点解释为实际业务发生时刻。', ''),
    DetailField('effEndDate', '缴费期满日', '接口返回的保单缴费期满日期，用于查询交费义务何时结束。示例 yyyy-MM-dd HH:mm:ss，区别于保障到期 validMatuDate；多保单时单值取数规则未说明。示例 2999-12-31 的占位含义未定义，不能直接解释为终身缴费。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。现有示例均按日期补 00:00:00，造数参考保持此表示，不把零点解释为实际业务发生时刻。', ''),
    DetailField('latelySegDate', '最近承保时间(长险最近一次成交的具体时间)', '客户长险最近一次承保的具体时间，用于查询最近承保或最近长险成交。示例格式 yyyy-MM-dd HH:mm:ss；按本字段注释取长险最近一次，区别于投保申请、客户添加或保单生效时间。不能泛化为所有险种的最近承保时间。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。保留实际示例中的时、分、秒，不统一截成日期或补成零点。', ''),
    DetailField('validMatuDate', '保单到期时间(具体日期)', '接口返回的保单到期时间，用于查询到期日或近期到期情况，类型为 String，示例 yyyy-MM-dd HH:mm:ss。区别于缴费期满 effEndDate、周年日 effAnnDate；本接口未说明险种范围及多保单下取哪一个日期，不直接套用搜客户表的寿险范围或任一/全部保单判断。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。现有示例均按日期补 00:00:00，造数参考保持此表示，不把零点解释为实际业务发生时刻。', ''),
    DetailField('isLifePayout', '生存金是否领取(Y-已领取 N-未领取)', '客户生存金领取状态：Y=已领取，N=未领取，用于查询领取情况。未提供领取日期、次数或金额，也未说明多保单汇总规则；N 不等于一定有应领未领生存金，Y 也不代表全部生存金已领完。', ''),
    DetailField('occurPassRegst', '综拓理赔(regst-综拓理赔报案,fini-综拓理赔结案)', '客户综拓理赔状态编码，用于识别理赔报案或结案：regst=综拓理赔报案，fini=综拓理赔结案。仅表示状态，不提供金额、次数或时间。与 occurPassRegstValue 配对；当前非空示例为 regst，对应“综拓理赔报案”。', ''),
    DetailField('occurPassRegstValue', '综拓理赔描述', '客户综拓理赔状态的中文描述，应与 occurPassRegst 对应：regst=综拓理赔报案，fini=综拓理赔结案。用于解释状态，不表示理赔明细。当前非空示例“综拓理赔报案”与 regst 对应。', ''),
    DetailField('pcustBuyPtype', '准客购买产品分类(o2o-寿险O2O客户、pbs-产险、gbs-养老险、hbs-健康险、other-其他准客)', '准客购买产品所属业务分类，用于了解准客已有业务：o2o=寿险O2O客户，pbs=产险，gbs=养老险，hbs=健康险，other=其他准客。多个编码以英文逗号拼接，对应 pcustBuyPtypeValue；与搜客户表“准客来源”的枚举不是同一组取值。', '字符串，多个值以'),
    DetailField('pcustBuyPtypeValue', '准客购买产品分类描述', '准客购买产品所属业务分类的中文描述，对应 pcustBuyPtype：o2o=寿险O2O客户、pbs=产险、gbs=养老险、hbs=健康险、other=其他准客。示例以英文逗号拼接多项，如“产险,养老险,健康险”；不表示具体产品名称。', '字符串，多个值以'),
    DetailField('validSinsPol', '有效短险保单(综拓cyj,o2o-O2O,ahi-意健险)', '客户有效短险所属类别编码，用于查询有效短险类别：cyj=综拓，o2o=O2O，ahi=意健险。多个值以英文逗号拼接，对应 validSinsPolValue；不是保单号，也不是保单数量。空值不能单独证明没有任何长期或短期保险。', '字符串，多个值以'),
    DetailField('validSinsPolValue', '有效短险保单描述', '客户有效短险所属类别的描述，与 validSinsPol 对应：cyj=综拓，o2o=O2O，ahi=意健险。示例“综拓cyj,O2O”包含多类，使用英文逗号分隔。用于查看有效短险类别，不是具体保单号、险种名或保单数量。', '字符串，多个值以'),
    DetailField('clientDynamicInfoList', '客户动态信息列表', '客户动态记录数组，用于查看客户的活动或服务使用经历。每条对象包含客户 ID、动态 ID、一级分类、文案、模板编号和发生时间，需在同一对象内关联读取，详见下方展开结构。[] 表示本次返回无记录。\n空值约定：示例中的 null 表示 JSON 空值，输出为 null，不加引号；[] 表示空数组。有记录时返回对象数组，数组内字段按下方展开结构生成。', ''),
    DetailField('clientNoteInfoList', '客户笔记信息列表', '客户笔记记录数组，用于查看跟进沟通内容，含笔记 ID、客户 ID、笔记日期、类型、类型描述和正文。按同一笔记对象关联读取，具体结构见下方展开区。[] 表示本次返回无记录。\n空值约定：示例中的 null 表示 JSON 空值，输出为 null，不加引号；[] 表示空数组。有记录时返回对象数组，数组内字段按下方展开结构生成。', ''),
    DetailField('clientDynamicInfoList.activityCode', '动态模板编号', '路径 clientDynamicInfoList.activityCode。当前动态的模板或事件类型编号，用于识别动态模板，示例 JGJ_POLICY_SERVICE_2026。不是活动实例 ID，也不等同于 activityId；未提供完整代码字典，不能仅凭字符串后缀确定发生年份或推导其他类型。', ''),
    DetailField('clientDynamicInfoList.activityDate', '动态时间', '路径 clientDynamicInfoList.activityDate。当前动态发生时间，用于按日期范围查看动态或排列先后。返回时间字符串，示例 yyyy-MM-dd HH:mm:ss；与同一对象的 activityId、activityMsg 配对，不是客户添加日期或保单承保时间。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。保留实际示例中的时、分、秒，不统一截成日期或补成零点。', ''),
    DetailField('clientDynamicInfoList.activityId', '客户动态id', '路径 clientDynamicInfoList.activityId。一条客户动态的标识，用于定位、关联或去重动态记录。返回字符串，示例为带连字符的 UUID；与客户 ID、保单号及动态模板编号 activityCode 区分。', ''),
    DetailField('clientDynamicInfoList.activityMsg', '动态文案', '路径 clientDynamicInfoList.activityMsg。当前动态的说明文案，用于了解发生了什么服务或活动。示例含续期交费方式变更、客户资料变更，以及产品和保单号。文案内信息不是独立结构化字段，应结合同一对象的 activityDate 阅读，不能跨记录拼接。', ''),
    DetailField('clientDynamicInfoList.clientMainId', '客户中心客户id', '路径 clientDynamicInfoList.clientMainId。当前动态所属客户的客户中心 ID，用于将动态归属到客户详情；应在同一个动态对象内与 activityId、activityMsg、activityDate 一起读取，不是动态 ID。', ''),
    DetailField('clientDynamicInfoList.parentClassifyName', '动态一级分类名称', '路径 clientDynamicInfoList.parentClassifyName。当前动态的一级业务分类名称，用于按动态类别查看服务经历，示例“保单服务”。这是动态内容分类，不是本表字段的一级分类；应与同一对象的文案和时间关联。', ''),
    DetailField('familyInfoList.familyClientMainId', '客户中心客户id', '路径 familyInfoList.familyClientMainId。当前家庭成员在客户中心的 ID，用于识别并关联该家庭成员；不是当前客户本人的 clientMainId。必须与同一成员对象的 familyRelation、familyClientSex 和姓名密文成组读取。', ''),
    DetailField('familyInfoList.familyClientNameCipher', '姓名国密', '路径 familyInfoList.familyClientNameCipher。当前家庭成员姓名的国密密文，用于经授权解密后获取家属姓名。与同一成员对象的 familyRelation 和 familyClientMainId 配对；不是客户本人姓名，也不能把密文直接当作姓名查询或展示。', ''),
    DetailField('familyInfoList.familyClientSex', '性别 (F-女，M-男)', '路径 familyInfoList.familyClientSex。当前家庭成员性别编码：F=女，M=男；搜客户表用“女/男”表示。用于查询某关系成员的性别时，需同时读取同一成员对象的 familyRelation，不能误作客户本人性别。', ''),
    DetailField('familyInfoList.familyRelation', '家庭关系', '路径 familyInfoList.familyRelation。当前成员与所查客户的家庭关系，示例配偶、子女、父母；“子女”表示该成员是所查客户的子女。用于查看家庭构成或定位某关系的成员，需关联同一对象的成员 ID、姓名和性别，不跨成员混配。', ''),
    DetailField('clientNoteInfoList.clientMainId', '客户中心客户id', '路径 clientNoteInfoList.clientMainId。当前笔记所属客户的客户中心 ID，用于关联客户详情。与同一笔记对象的 noteId、dateRemark、noteContent 一起读取，不是记录笔记的员工编号。', ''),
    DetailField('clientNoteInfoList.dateRemark', '笔记日期', '路径 clientNoteInfoList.dateRemark。当前笔记的记录日期时间，用于按时间查看沟通记录或排列笔记。示例 yyyy-MM-dd HH:mm:ss。笔记正文中提到的保单到期日等业务日期不等同于本字段，也不能直接认定为实际面访或电话发生时间。\n时间表示精度（造数参考）：到秒，格式 yyyy-MM-dd HH:mm:ss，现有示例不含毫秒。保留实际示例中的时、分、秒，不统一截成日期或补成零点。', ''),
    DetailField('clientNoteInfoList.noteContent', '笔记内容', '路径 clientNoteInfoList.noteContent。当前笔记的正文，用于理解客户跟进情况、沟通内容或记录原因。示例包含 <p> 等 HTML 标签，应按富文本内容解读；笔记中的产品、日期及保单状态是记录者的描述，不能直接替代保单系统的结构化事实。', ''),
    DetailField('clientNoteInfoList.noteId', '笔记ID', '路径 clientNoteInfoList.noteId。一条客户笔记的标识，用于定位、关联或去重笔记记录。返回字符串，区别于客户 ID、动态 ID 和保单号；与同一笔记对象的日期、类型及正文关联。', ''),
    DetailField('clientNoteInfoList.noteType', '笔记类型 (面访笔记1,常规记录2,电话沟通3)', '路径 clientNoteInfoList.noteType。当前笔记的记录类型编码：1=面访笔记，2=常规记录，3=电话沟通。用于按沟通或记录方式查询，对应同一笔记对象的 noteTypeValue；不是客户类型或温度等级。', ''),
    DetailField('clientNoteInfoList.noteTypeValue', '笔记类型描述', '路径 clientNoteInfoList.noteTypeValue。当前笔记类型的中文描述，与同一对象 noteType 对应：1=面访笔记，2=常规记录，3=电话沟通。用于解释沟通记录类别，区别于笔记正文 noteContent。', ''),
)
DEFINITIONS = {entry.path: entry for entry in FIELDS}

def describe_field(path: str) -> str:
    entry = DEFINITIONS.get(path)
    if entry is None:
        return "业务含义待确认，不据字段名推断"
    if "Cipher" in path:
        owners = {"familyInfoList.familyClientNameCipher": "同一家庭成员的姓名，与成员 ID、性别和关系成组读取，不是客户本人姓名",
                  "clientMainPhoneListCipher": "当前客户的主手机号列表，与保单手机号列表区分", "clientPolPhoneListCipher": "当前客户保单关联手机号列表，不等同当前主手机号"}
        label = entry.label.replace("国密", "").replace("密文", "")
        return owners.get(path, label) + "；接口透传最终值，不因 Cipher 后缀推断仍加密，不自行解密；保留同一记录的归属与原值"
    meaning = entry.meaning.split("\n示例精度")[0].split("\n时间表示精度")[0].split("\n空值约定")[0]
    return "；".join(part for part in (entry.label, meaning, entry.domain) if part)

def describe_output_data(data: dict[str, JsonValue]) -> dict[str, str]:
    return describe_output_page(data, describe_field=describe_field, canonical_path=lambda path: path,
        defined_paths=DEFINITIONS, page_labels={"total": "真实命中客户数", "records": "客户记录；多命中仅基本候选，其余空值不表示无业务"},
        record_description="客户详情 v5 最终值；多命中仅 ID、姓名、性别、年龄有效；空值不代表零或否")

def field_schema(path: str) -> dict[str, JsonValue]:
    children: dict[str, JsonValue] = {entry.path[len(path)+1:]: {"description": describe_field(entry.path)}
                for entry in FIELDS if entry.path.startswith(path + ".")}
    return {"items": {"properties": children}, "properties": children} if children else {}

class CustomerDetailV5Fields(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow", strict=True, allow_inf_nan=False)
    __pydantic_extra__: dict[str, StrictJsonValue] = Field(init=False)
    clientMainId: str = Field(strict=True, min_length=1, max_length=64, description=describe_field("clientMainId"), json_schema_extra=field_schema("clientMainId"))
    agentNo: StrictJsonValue = Field(default=None, description=describe_field("agentNo"), json_schema_extra=field_schema("agentNo"))
    clientNo: StrictJsonValue = Field(default=None, description=describe_field("clientNo"), json_schema_extra=field_schema("clientNo"))
    mainClientNo: StrictJsonValue = Field(default=None, description=describe_field("mainClientNo"), json_schema_extra=field_schema("mainClientNo"))
    birthday: StrictJsonValue = Field(default=None, description=describe_field("birthday"), json_schema_extra=field_schema("birthday"))
    carBrand: StrictJsonValue = Field(default=None, description=describe_field("carBrand"), json_schema_extra=field_schema("carBrand"))
    carNumber: StrictJsonValue = Field(default=None, description=describe_field("carNumber"), json_schema_extra=field_schema("carNumber"))
    clientAge: StrictJsonValue = Field(default=None, description=describe_field("clientAge"), json_schema_extra=field_schema("clientAge"))
    clientMainPhoneListCipher: StrictJsonValue = Field(default=None, description=describe_field("clientMainPhoneListCipher"), json_schema_extra=field_schema("clientMainPhoneListCipher"))
    clientNameCipher: StrictJsonValue = Field(default=None, description=describe_field("clientNameCipher"), json_schema_extra=field_schema("clientNameCipher"))
    clientPolPhoneListCipher: StrictJsonValue = Field(default=None, description=describe_field("clientPolPhoneListCipher"), json_schema_extra=field_schema("clientPolPhoneListCipher"))
    clientSex: StrictJsonValue = Field(default=None, description=describe_field("clientSex"), json_schema_extra=field_schema("clientSex"))
    companyAddressCipher: StrictJsonValue = Field(default=None, description=describe_field("companyAddressCipher"), json_schema_extra=field_schema("companyAddressCipher"))
    compName: StrictJsonValue = Field(default=None, description=describe_field("compName"), json_schema_extra=field_schema("compName"))
    contactAddressCipher: StrictJsonValue = Field(default=None, description=describe_field("contactAddressCipher"), json_schema_extra=field_schema("contactAddressCipher"))
    country: StrictJsonValue = Field(default=None, description=describe_field("country"), json_schema_extra=field_schema("country"))
    countryName: StrictJsonValue = Field(default=None, description=describe_field("countryName"), json_schema_extra=field_schema("countryName"))
    education: StrictJsonValue = Field(default=None, description=describe_field("education"), json_schema_extra=field_schema("education"))
    eduDescription: StrictJsonValue = Field(default=None, description=describe_field("eduDescription"), json_schema_extra=field_schema("eduDescription"))
    email: StrictJsonValue = Field(default=None, description=describe_field("email"), json_schema_extra=field_schema("email"))
    homeAddressCipher: StrictJsonValue = Field(default=None, description=describe_field("homeAddressCipher"), json_schema_extra=field_schema("homeAddressCipher"))
    houseStatus: StrictJsonValue = Field(default=None, description=describe_field("houseStatus"), json_schema_extra=field_schema("houseStatus"))
    houseStatusDesc: StrictJsonValue = Field(default=None, description=describe_field("houseStatusDesc"), json_schema_extra=field_schema("houseStatusDesc"))
    mariDescription: StrictJsonValue = Field(default=None, description=describe_field("mariDescription"), json_schema_extra=field_schema("mariDescription"))
    mariSts: StrictJsonValue = Field(default=None, description=describe_field("mariSts"), json_schema_extra=field_schema("mariSts"))
    phoneNoCipher: StrictJsonValue = Field(default=None, description=describe_field("phoneNoCipher"), json_schema_extra=field_schema("phoneNoCipher"))
    position: StrictJsonValue = Field(default=None, description=describe_field("position"), json_schema_extra=field_schema("position"))
    profCode: StrictJsonValue = Field(default=None, description=describe_field("profCode"), json_schema_extra=field_schema("profCode"))
    profName: StrictJsonValue = Field(default=None, description=describe_field("profName"), json_schema_extra=field_schema("profName"))
    registerPlace: StrictJsonValue = Field(default=None, description=describe_field("registerPlace"), json_schema_extra=field_schema("registerPlace"))
    regPlaceDescription: StrictJsonValue = Field(default=None, description=describe_field("regPlaceDescription"), json_schema_extra=field_schema("regPlaceDescription"))
    revenue: StrictJsonValue = Field(default=None, description=describe_field("revenue"), json_schema_extra=field_schema("revenue"))
    revenueDescription: StrictJsonValue = Field(default=None, description=describe_field("revenueDescription"), json_schema_extra=field_schema("revenueDescription"))
    vehclPriceAmt: StrictJsonValue = Field(default=None, description=describe_field("vehclPriceAmt"), json_schema_extra=field_schema("vehclPriceAmt"))
    workUnit: StrictJsonValue = Field(default=None, description=describe_field("workUnit"), json_schema_extra=field_schema("workUnit"))
    workYears: StrictJsonValue = Field(default=None, description=describe_field("workYears"), json_schema_extra=field_schema("workYears"))
    idNoCipher: StrictJsonValue = Field(default=None, description=describe_field("idNoCipher"), json_schema_extra=field_schema("idNoCipher"))
    idType: StrictJsonValue = Field(default=None, description=describe_field("idType"), json_schema_extra=field_schema("idType"))
    idTypeName: StrictJsonValue = Field(default=None, description=describe_field("idTypeName"), json_schema_extra=field_schema("idTypeName"))
    idValidDate: StrictJsonValue = Field(default=None, description=describe_field("idValidDate"), json_schema_extra=field_schema("idValidDate"))
    idValidDateStart: StrictJsonValue = Field(default=None, description=describe_field("idValidDateStart"), json_schema_extra=field_schema("idValidDateStart"))
    height: StrictJsonValue = Field(default=None, description=describe_field("height"), json_schema_extra=field_schema("height"))
    isInsurantMedicare: StrictJsonValue = Field(default=None, description=describe_field("isInsurantMedicare"), json_schema_extra=field_schema("isInsurantMedicare"))
    weight: StrictJsonValue = Field(default=None, description=describe_field("weight"), json_schema_extra=field_schema("weight"))
    clientType: StrictJsonValue = Field(default=None, description=describe_field("clientType"), json_schema_extra=field_schema("clientType"))
    isBuyCar: StrictJsonValue = Field(default=None, description=describe_field("isBuyCar"), json_schema_extra=field_schema("isBuyCar"))
    isBuyCarValue: StrictJsonValue = Field(default=None, description=describe_field("isBuyCarValue"), json_schema_extra=field_schema("isBuyCarValue"))
    newClientType: StrictJsonValue = Field(default=None, description=describe_field("newClientType"), json_schema_extra=field_schema("newClientType"))
    newClientTypeValue: StrictJsonValue = Field(default=None, description=describe_field("newClientTypeValue"), json_schema_extra=field_schema("newClientTypeValue"))
    orphanType: StrictJsonValue = Field(default=None, description=describe_field("orphanType"), json_schema_extra=field_schema("orphanType"))
    orphanTypeValue: StrictJsonValue = Field(default=None, description=describe_field("orphanTypeValue"), json_schema_extra=field_schema("orphanTypeValue"))
    trusteeshipFlag: StrictJsonValue = Field(default=None, description=describe_field("trusteeshipFlag"), json_schema_extra=field_schema("trusteeshipFlag"))
    clientGroupLabel: StrictJsonValue = Field(default=None, description=describe_field("clientGroupLabel"), json_schema_extra=field_schema("clientGroupLabel"))
    clientGroupLabelValue: StrictJsonValue = Field(default=None, description=describe_field("clientGroupLabelValue"), json_schema_extra=field_schema("clientGroupLabelValue"))
    clientTemperature: StrictJsonValue = Field(default=None, description=describe_field("clientTemperature"), json_schema_extra=field_schema("clientTemperature"))
    clientTemperatureDesc: StrictJsonValue = Field(default=None, description=describe_field("clientTemperatureDesc"), json_schema_extra=field_schema("clientTemperatureDesc"))
    clientTemperatureValue: StrictJsonValue = Field(default=None, description=describe_field("clientTemperatureValue"), json_schema_extra=field_schema("clientTemperatureValue"))
    familyOfficeFlag: StrictJsonValue = Field(default=None, description=describe_field("familyOfficeFlag"), json_schema_extra=field_schema("familyOfficeFlag"))
    lifetimeVipFlag: StrictJsonValue = Field(default=None, description=describe_field("lifetimeVipFlag"), json_schema_extra=field_schema("lifetimeVipFlag"))
    newValueLabel: StrictJsonValue = Field(default=None, description=describe_field("newValueLabel"), json_schema_extra=field_schema("newValueLabel"))
    newValueLabelDesc: StrictJsonValue = Field(default=None, description=describe_field("newValueLabelDesc"), json_schema_extra=field_schema("newValueLabelDesc"))
    newValueLabelValue: StrictJsonValue = Field(default=None, description=describe_field("newValueLabelValue"), json_schema_extra=field_schema("newValueLabelValue"))
    paVipType: StrictJsonValue = Field(default=None, description=describe_field("paVipType"), json_schema_extra=field_schema("paVipType"))
    paVipTypeValue: StrictJsonValue = Field(default=None, description=describe_field("paVipTypeValue"), json_schema_extra=field_schema("paVipTypeValue"))
    premClientTemperature: StrictJsonValue = Field(default=None, description=describe_field("premClientTemperature"), json_schema_extra=field_schema("premClientTemperature"))
    premClientTemperatureValue: StrictJsonValue = Field(default=None, description=describe_field("premClientTemperatureValue"), json_schema_extra=field_schema("premClientTemperatureValue"))
    premValueLabel: StrictJsonValue = Field(default=None, description=describe_field("premValueLabel"), json_schema_extra=field_schema("premValueLabel"))
    premValueLabelValue: StrictJsonValue = Field(default=None, description=describe_field("premValueLabelValue"), json_schema_extra=field_schema("premValueLabelValue"))
    vipType: StrictJsonValue = Field(default=None, description=describe_field("vipType"), json_schema_extra=field_schema("vipType"))
    vipTypeValue: StrictJsonValue = Field(default=None, description=describe_field("vipTypeValue"), json_schema_extra=field_schema("vipTypeValue"))
    typcGrpDtyp: StrictJsonValue = Field(default=None, description=describe_field("typcGrpDtyp"), json_schema_extra=field_schema("typcGrpDtyp"))
    typcGrpDtypValue: StrictJsonValue = Field(default=None, description=describe_field("typcGrpDtypValue"), json_schema_extra=field_schema("typcGrpDtypValue"))
    familyInfoList: StrictJsonValue = Field(default=None, description=describe_field("familyInfoList"), json_schema_extra=field_schema("familyInfoList"))
    clientChildrenCnt: StrictJsonValue = Field(default=None, description=describe_field("clientChildrenCnt"), json_schema_extra=field_schema("clientChildrenCnt"))
    isClientApp: StrictJsonValue = Field(default=None, description=describe_field("isClientApp"), json_schema_extra=field_schema("isClientApp"))
    isClientIns: StrictJsonValue = Field(default=None, description=describe_field("isClientIns"), json_schema_extra=field_schema("isClientIns"))
    agentProdType: StrictJsonValue = Field(default=None, description=describe_field("agentProdType"), json_schema_extra=field_schema("agentProdType"))
    agentProdTypeValue: StrictJsonValue = Field(default=None, description=describe_field("agentProdTypeValue"), json_schema_extra=field_schema("agentProdTypeValue"))
    holdLifeRsty: StrictJsonValue = Field(default=None, description=describe_field("holdLifeRsty"), json_schema_extra=field_schema("holdLifeRsty"))
    holdLifeRstyValue: StrictJsonValue = Field(default=None, description=describe_field("holdLifeRstyValue"), json_schema_extra=field_schema("holdLifeRstyValue"))
    annPremAmt: StrictJsonValue = Field(default=None, description=describe_field("annPremAmt"), json_schema_extra=field_schema("annPremAmt"))
    insnoSeqAmt: StrictJsonValue = Field(default=None, description=describe_field("insnoSeqAmt"), json_schema_extra=field_schema("insnoSeqAmt"))
    dateCreated: StrictJsonValue = Field(default=None, description=describe_field("dateCreated"), json_schema_extra=field_schema("dateCreated"))
    effAnnDate: StrictJsonValue = Field(default=None, description=describe_field("effAnnDate"), json_schema_extra=field_schema("effAnnDate"))
    effEndDate: StrictJsonValue = Field(default=None, description=describe_field("effEndDate"), json_schema_extra=field_schema("effEndDate"))
    latelySegDate: StrictJsonValue = Field(default=None, description=describe_field("latelySegDate"), json_schema_extra=field_schema("latelySegDate"))
    validMatuDate: StrictJsonValue = Field(default=None, description=describe_field("validMatuDate"), json_schema_extra=field_schema("validMatuDate"))
    isLifePayout: StrictJsonValue = Field(default=None, description=describe_field("isLifePayout"), json_schema_extra=field_schema("isLifePayout"))
    occurPassRegst: StrictJsonValue = Field(default=None, description=describe_field("occurPassRegst"), json_schema_extra=field_schema("occurPassRegst"))
    occurPassRegstValue: StrictJsonValue = Field(default=None, description=describe_field("occurPassRegstValue"), json_schema_extra=field_schema("occurPassRegstValue"))
    pcustBuyPtype: StrictJsonValue = Field(default=None, description=describe_field("pcustBuyPtype"), json_schema_extra=field_schema("pcustBuyPtype"))
    pcustBuyPtypeValue: StrictJsonValue = Field(default=None, description=describe_field("pcustBuyPtypeValue"), json_schema_extra=field_schema("pcustBuyPtypeValue"))
    validSinsPol: StrictJsonValue = Field(default=None, description=describe_field("validSinsPol"), json_schema_extra=field_schema("validSinsPol"))
    validSinsPolValue: StrictJsonValue = Field(default=None, description=describe_field("validSinsPolValue"), json_schema_extra=field_schema("validSinsPolValue"))
    clientDynamicInfoList: StrictJsonValue = Field(default=None, description=describe_field("clientDynamicInfoList"), json_schema_extra=field_schema("clientDynamicInfoList"))
    clientNoteInfoList: StrictJsonValue = Field(default=None, description=describe_field("clientNoteInfoList"), json_schema_extra=field_schema("clientNoteInfoList"))

    @model_validator(mode="after")
    def validate_identity(self) -> "CustomerDetailV5Fields":
        if not self.clientMainId.strip():
            raise ValueError("客户中心 ID 不能为空白")
        return self

    @model_serializer(mode="wrap")
    def preserve_present_fields(self, handler: SerializerFunctionWrapHandler):
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}
