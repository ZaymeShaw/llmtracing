"""复制到 eval_harness/src/eval_harness/adapters/my_agent.py。

替换 execute_harness 为自己的 CLI、HTTP 或 Python harness 调用。
逐轮执行 turns，题内延续会话、题间隔离；等待结束，失败/超时抛异常。
headers 必须到达真正的模型请求：SDK extra_headers / CLI 自定义请求头；
HTTP 服务需继续透传。模型连接使用本地 LiteLLM 的地址和密钥。
如需工作目录/超时配置，从 run_case 的 case_dir/project_cwd/options 传入。
"""
from time import monotonic

from eval_harness.adapters.base import CaseRunResult
from eval_harness.trace_identity import trace_headers


def execute_harness(turns, *, headers):
    # 这是你的接入位置，不是框架提供的函数。
    raise NotImplementedError("请替换为自己的 harness 调用")


def run_case(*, case_id, turns, case_dir, project_cwd, **options):
    start, error = monotonic(), None
    try:
        execute_harness(turns, headers=trace_headers())
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    return CaseRunResult(
        case_id=case_id, session_id=None, turns=[],
        success=error is None, exit_code=0 if error is None else 1,
        error=error, wall_ms=int((monotonic() - start) * 1000),
    )
