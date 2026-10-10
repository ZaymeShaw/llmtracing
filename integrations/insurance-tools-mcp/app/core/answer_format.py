"""答案呈现层：保留来源转述净化，不重写数值或模型已有的 Markdown。"""
from __future__ import annotations

import re

# 口吻净化(切片 4,恒开,无 conf 开关):删除句首/分句首的
# 来源转述插入语,词表与切片 1 指令禁令同源。两条防线防误伤:①只匹配句读
# 边界后的句首形态——「请根据资料上的要求办理」「依据保险合同约定」等句中/
# 正常业务用法不命中;②中段不得跨句读(排除 ,。;\n),删除不产生双句读。
_SOURCE_PREFIX_RE = re.compile(
    r"(?:(?<=^)|(?<=[。！？；;\n]))\s*(?:根据|依据)[^，。；\n]{0,8}?"
    r"(?:背景知识|资料|条款显示|资料表明|检索到的[^，。；\n]{0,4})[，,]?\s*"
)
_SOURCE_ASSERT_RE = re.compile(
    r"(?:(?<=^)|(?<=[。！？；;\n]))\s*(?:现有|上述|以下|本次)?(?:资料|背景知识|检索结果|条款)"
    r"(?:显示|表明)[，,：:]?\s*"
)


def sanitize_source_phrases(answer: str) -> str:
    """删除来源转述插入语(恒开;幂等——删除式变换再跑无变化)。"""
    answer = _SOURCE_PREFIX_RE.sub("", answer)
    return _SOURCE_ASSERT_RE.sub("", answer)


def format_answer(answer: str) -> str:
    """仅删除来源转述插入语，数值、限定词与原有格式保持不变。"""
    return sanitize_source_phrases(answer)
