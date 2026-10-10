"""已完成回答的共享安全投影，不传递内部信封字段。"""
from app.core.identifiers import matching_text

from app.core.envelope import SAFE_TOOL_FAILURE_ANSWERS
from app.core.verify_envelope import DEGRADED_ANSWERS
from app.core.answer_ladder import CONTEXT_OVERFLOW_ANSWER, MODEL_UNAVAILABLE_ANSWER, TIMEOUT_ANSWER
from app.core.admission import MODEL_CAPACITY_MESSAGE
from app.schemas.answer import AnswerEnvelope

# 兼容历史 Agent 文案，仅完整匹配；不把一般回答中的“失败”字样当作失败。
HISTORY_FAILURE_ANSWERS = DEGRADED_ANSWERS | SAFE_TOOL_FAILURE_ANSWERS | {"抱歉，暂时查询失败"}

_UNSAFE_HISTORY_MARKERS = (
    "step skipped",
    "traceback",
    "exception",
    "缺少本轮步骤痕迹",
)


def _failure_body(text: str) -> str:
    """Recognize our rendered limitation suffix without matching failure words inside normal answers."""
    candidate = text.removeprefix("（部分回答）\n")
    first, separator, rest = candidate.partition("\n补充说明：")
    if separator and rest.strip() and first in HISTORY_FAILURE_ANSWERS:
        return first
    return text


def safe_history_answer(raw: str, *, allow_verified_plain_text: bool) -> str | None:
    text = _failure_body(raw.strip())
    if not text or text in HISTORY_FAILURE_ANSWERS or text.lower() in {"none", "null"}:
        return None
    try:
        envelope = AnswerEnvelope.model_validate_json(text)
    except ValueError:
        lowered = text.lower()
        if (
            not allow_verified_plain_text
            or text.startswith(("{", "["))
            or any(marker in lowered for marker in _UNSAFE_HISTORY_MARKERS)
        ):
            return None
        return text
    # 正常信封无需出口检查戳记；运行异常降级信封仍不得进入历史。
    if envelope.status is not None:
        return None
    answer = envelope.answer.strip()
    if not answer or answer in HISTORY_FAILURE_ANSWERS:
        return None
    if envelope.completeness == "partial":
        limits = "；".join(dict.fromkeys(envelope.limitations or envelope.gaps))
        return answer + "\n（部分回答" + ("；局限：" + limits if limits else "；尚未完整回答") + "）"
    return answer


def history_failure_marker(raw: str, *, known_only: bool = False) -> str | None:
    """Retain the question, never echo a failed draft, exception or arbitrary reason.

    Upstream plain-text history only replaces recognized failure answers. Local
    run projections may also mark unparseable/unavailable output as unfinished.
    """
    text = _failure_body(raw.strip())
    known = text in HISTORY_FAILURE_ANSWERS or text == MODEL_CAPACITY_MESSAGE
    try:
        envelope = AnswerEnvelope.model_validate_json(text)
    except ValueError:
        pass
    else:
        if envelope.status is not None or envelope.answer.strip() in HISTORY_FAILURE_ANSWERS:
            text = envelope.answer.strip()
            known = True
    if known_only and not known:
        return None
    reason = {
        TIMEOUT_ANSWER: "处理超时",
        CONTEXT_OVERFLOW_ANSWER: "信息过多，整理未完成",
        MODEL_UNAVAILABLE_ANSWER: "模型暂不可用",
        "模型服务繁忙，请稍后再试。": "模型暂不可用",
        MODEL_CAPACITY_MESSAGE: "服务容量暂不足",
    }.get(text, "未取得完整答复")
    return f"（上一轮未完成：{reason}）"


def model_history_answer(raw: str, answer: str, *, max_chars: int) -> str:
    """Bound only model memory; never modify upstream messages or business contexts.

    Typed list envelopes carry a conversational summary, never their raw card page.
    Plain upstream answers retain their head and tail for references/limitations.
    """
    if max_chars <= 0:
        raise ValueError("历史答复上限必须大于 0")
    try:
        envelope = AnswerEnvelope.model_validate_json(raw)
    except ValueError:
        pass
    else:
        if envelope.status is None and envelope.structured is not None and envelope.structured.tool in {
            "policy_search", "customer_search",
        }:
            kind = "保单" if envelope.structured.tool == "policy_search" else "客户"
            limits = "；".join(dict.fromkeys(envelope.limitations or envelope.gaps))
            answer = f"上一轮已展示{kind}清单；具体记录和数量需本轮重新查询。" + (
                "（部分回答；" + (limits or "存在未完成事项") + "）" if envelope.is_incomplete else ""
            )
    if len(answer) <= max_chars:
        return answer
    marker = "\n[历史中段已省略]\n"
    if max_chars < len(marker) + 2:
        return answer[:max_chars]
    # Preserve middle identifiers as reference hints, never as current evidence.
    import re
    from app.core.resolve import POLICY_NO_RE
    from app.conf import harness_conf
    from app.conf.runtime import read_once
    customer_pattern = read_once('harness', harness_conf).history_window.customer_id_pattern
    identifiers = list(dict.fromkeys(match.group() for match in re.finditer(
        rf"{POLICY_NO_RE.pattern}|{customer_pattern}", matching_text(answer), flags=re.IGNORECASE)))
    prefix = "\n历史标识（需重新核实）："
    kept: list[str] = []
    for identifier in identifiers:
        identifier = identifier.upper()
        if len(prefix) + len("、".join([*kept, identifier])) <= max_chars // 2:
            kept.append(identifier)
    hints = prefix + "、".join(kept) if kept else ""
    available = max_chars - len(marker) - len(hints) - bool(hints)
    head = (available + 1) // 2
    tail_start = len(answer) - (available - head)
    # Never emit a clipped identifier at either excerpt boundary.
    token = re.compile(r"[A-Za-z0-9_-]+")
    for match in token.finditer(answer):
        if match.start() < head < match.end():
            head = match.start()
        if match.start() < tail_start < match.end():
            tail_start = match.end()
    return answer[:head] + marker + hints + "\n" * bool(hints) + answer[tail_start:]
