"""受控原因的安全固定文案，投影层不得信任动态 hint。"""
import re

from pydantic import ValidationError

from app.schemas.tool_control import CONTROL_ACTIONS, ToolControl, ToolControlAction, ToolControlReason
from app.core.envelope import make_envelope
from app.schemas import EnvelopeStatus, ErrorCode, ToolError

CONTROL_MESSAGES = {
    ToolControlAction.FINISH_WITH_EVIDENCE: "本次工具未执行：已达调用或时间预算。请基于已有证据回答并披露缺口，不再取数；没有可答证据且缺关键目标或实体时，仅可用 ask_clarify 登记澄清。",
    ToolControlAction.WAIT_BATCH: "本次工具未执行：同批已有读取或状态流程在执行。这不是服务故障，请等待本批结果后再判断是否需要继续。",
    ToolControlAction.WAIT_USER: "本轮已登记澄清，请结束本轮并等待用户回复，不再调用工具。",
    ToolControlAction.AWAIT_TERMINAL: "本轮已启动终止型清单查询，请等待卡片终态，不再调用其他工具。",
    ToolControlAction.HANDOFF_HUMAN: "连续多轮未能确认所需信息，为避免误查已停止追问，请转人工渠道办理。",
    ToolControlAction.CORRECT_CALL: "本次工具未执行：工具或参数不适用于当前任务。请按工具说明选择普通查询或纠正参数；不要重试原调用。已有证据时优先作答，目标不明确时再澄清。",
}

# 选择拒绝仅允许本地固定指导；不会把参数、上游hint或异常正文传给模型。
SELECTION_MESSAGES = {
    "product_compare": "产品对比前置条件不足：用户只询问性价比、哪个好或怎么选，尚未明确具体产品版本和客观比较维度；本轮禁止检索，请先使用 ask_clarify 一次性确认",
    "policy_shape": "工具选择不匹配：policy_detail 只接收完整保单号或纯数字尾号；自然语言条件请使用 policy_search",
    "policy_unconfirmed": "工具选择不匹配：该保单号既非用户本轮明确提供，也不是保单搜索或会话已确认结果；请先使用 policy_search",
    "policy_search": "工具选择不匹配：已提供完整保单号，应使用 policy_detail 查询保单详情",
    "customer_shape": "工具选择不匹配：customer_detail 只接收客户中心 ID，不能把姓名、手机号或描述当 ID；请先使用 customer_search 定位客户，再用返回的客户中心 ID 查询详情。本次未执行客户查询，不能据此判断客户不存在",
    "customer_search": "工具选择不匹配：该 customer_id 已确认，应使用 customer_detail 查询客户详情",
    "terminal_route": "当前轮不是可直接结束的独立清单搜索；请改用普通搜索工具继续完成问答",
    "terminal_prior": "本轮已有其他工具结果，不能重复查询并改走清单卡片；请基于现有结果继续回答",
}
INVALID_ARGUMENT_HINT = "本次工具未执行：参数类型或结构不符合工具定义，请按工具定义修正后调用；不要重复原错误参数。"
_SELECTION_HINTS = frozenset({*SELECTION_MESSAGES.values(), INVALID_ARGUMENT_HINT})


_ARGUMENT_TYPES = {
    'missing': '必填', 'missing_argument': '必填',
    'string_type': '字符串', 'list_type': '数组', 'dict_type': '对象',
    'bool_type': '布尔值', 'bool_parsing': '布尔值',
    'int_type': '整数', 'int_parsing': '整数', 'int_from_float': '整数',
    'float_type': '数值', 'float_parsing': '数值',
    'unexpected_keyword_argument': '工具定义中的参数',
}
_ARGUMENT_DETAIL = re.compile(
    r"参数纠正：(?!$)(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*："
    r"(?:必填|字符串|数组|对象|布尔值|整数|数值|工具定义中的参数|符合工具定义的类型)(?:；|$)){1,3}"
)


def argument_hint(error: ValidationError) -> str:
    """Only schema paths and expected types; never exception text or input values."""
    details = []
    for item in error.errors(include_input=False, include_context=False, include_url=False)[:3]:
        path = ''
        for segment in item['loc']:
            if isinstance(segment, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', segment):
                path += ('.' if path else '') + segment
            elif type(segment) is int and 0 <= segment <= 9999 and path:
                path += f'[{segment}]'
            else:
                path = ''
                break
        if path and len(path) <= 192:
            details.append(path + '：' + _ARGUMENT_TYPES.get(item['type'], '符合工具定义的类型'))
    return INVALID_ARGUMENT_HINT + (' 参数纠正：' + '；'.join(details) if details else '')


def _safe_argument_hint(hint: str | None) -> bool:
    return bool(hint and hint.startswith(INVALID_ARGUMENT_HINT + ' ')
                and _ARGUMENT_DETAIL.fullmatch(hint[len(INVALID_ARGUMENT_HINT) + 1:]))


def control_for(reason: ToolControlReason) -> ToolControl:
    return ToolControl(reason=reason, action=CONTROL_ACTIONS[reason])


def refusal_envelope(reason: ToolControlReason, hint: str | None = None) -> str:
    """统一本地拒绝格式；纠正选择和等待同批结果不伪装成服务故障。"""
    control = control_for(reason)
    recoverable = reason in {ToolControlReason.PARALLEL_STATE_CONFLICT, ToolControlReason.SELECTION_MISUSE}
    return make_envelope(
        status=EnvelopeStatus.AMBIGUOUS if recoverable else EnvelopeStatus.ERROR,
        error=None if recoverable else ToolError(code=ErrorCode.BUDGET, retryable=False),
        hint=control_message(control, hint), control=control,
    )


def control_message(control: ToolControl, hint: str | None = None) -> str:
    if control.reason is ToolControlReason.SELECTION_MISUSE and (hint in _SELECTION_HINTS or _safe_argument_hint(hint)):
        return str(hint)
    if control.reason is ToolControlReason.NO_PROGRESS:
        return "本次工具未执行：连续查询未取得新信息。请基于已有证据回答并明确未确认部分，不再换措辞或换工具重复查询。"
    if control.reason is ToolControlReason.SAME_TOOL_LIMIT:
        return "本次工具未执行：该工具已达本轮调用上限，不再调用该工具。优先基于已有证据回答；其他独立子任务可在剩余预算内使用适用工具。"
    if control.reason is ToolControlReason.RAG_LIMIT:
        return "本次工具未执行：知识检索已达本轮合计上限，不再调用知识检索工具。优先基于已有证据回答；其他独立子任务可在剩余预算内使用非检索工具。"
    return CONTROL_MESSAGES[control.action]
