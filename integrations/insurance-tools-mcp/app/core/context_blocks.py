"""Escape reserved framing tags in untrusted text without changing normal prose."""
import re

_RESERVED = re.compile(r'\[/?(?:当前用户问题|本轮执行余量|确定性路由候选|本轮直达已取证|本轮直达查询失败|'
                       r'确定性澄清事件|会话上下文|最近对话历史|历史中段已省略)[^\]\n]*\]')


def escape_context_tags(text: str) -> str:
    return _RESERVED.sub(lambda m: '［' + m.group()[1:-1] + '］', text)
