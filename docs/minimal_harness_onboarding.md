# 最小接入指引

在仓库根目录执行，先 `export PYTHONPATH="$PWD/eval_harness/src"`。**只用 tracing：harness → LiteLLM → HTML，无须 adapter、Bundle 或运行 YAML。让框架批跑才需要 adapter。**

## 1. 本地中转（LiteLLM）

复制 `llm_gateway/.env.example` 为 `llm_gateway/.env.myrelay`，填写上游 `UPSTREAM_API_BASE/API_KEY/PROTOCOL/MODEL` 和本地 `LITELLM_HOST/PORT/MASTER_KEY`。脚本使用 `llm_gateway/.venv`：
```bash
python3 -m venv llm_gateway/.venv
llm_gateway/.venv/bin/python -m pip install 'litellm[proxy]'
bash llm_gateway/start_litellm.sh --profile myrelay
```
harness 的模型连接使用本地地址、master key 和 model。OpenAI 兼容地址如 `http://127.0.0.1:4001/v1`，Anthropic 使用 `http://127.0.0.1:4001`。callback 将模型交互写入 `llm_gateway/logs/llm_calls.jsonl`。**中转不依赖 Claude 配置**；仅需同步已有 Claude settings 时，在 `.env.myrelay` 显式设 `LITELLM_SYNC_CLAUDE_SETTINGS=1`。本地 `.env.*` 不提交 Git。

## 2. 不写 adapter：直接并发调用、查看 tracing

下面的 `client` 是已连接中转的 OpenAI SDK 客户端；换用 Chat Completions 或 Anthropic Messages 时，同样把 `trace_headers()` 放到实际模型请求的 `extra_headers`。一个批次只生成一次 `run_id`，同一题内所有模型请求共用一次 execution；重试整题时重新调用 `new_identity`。
```python
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from eval_harness.trace_identity import new_identity, execution_context, trace_headers

run_id = uuid4().hex  # 上午、下午各一批；要续同批，复用保存的 run_id
print(run_id)
def run_one(case):
    with execution_context(**new_identity(run_id, "my_agent", case["case_id"])):
        return client.responses.create(model="my-model", input=case["prompt"],
                                       extra_headers=trace_headers())
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(run_one, cases))
```
helper 自动生成 `X-Eval-Case-Id/Execution-Id/Run-Id/Harness/Started-At` 请求头；上下文按线程/异步任务隔离。**CLI 须配置其自定义请求头；HTTP 业务服务须透传这些头到内部模型请求**，只加在业务接口无效。不要并发修改共享 SDK 的全局 headers；自己新建线程时，在该线程内建立上述上下文。
```bash
RUN_ID=上面打印的批次ID
python3 -m eval_harness.llm_trace_html --from-gateway-log llm_gateway/logs/llm_calls.jsonl --run-id "$RUN_ID" --out-run-dir eval_runs/my_wire
open eval_runs/my_wire/llm_trace.html
```
导出按“批次 / harness / case / execution”保留记录；**批次内默认展示开始时间最新的尝试，勾选「显示历史尝试」可看重试前记录**。省略 `--run-id` 可导出全部批次并在页面筛选；`--cases A01,A02` / `--executions <ID>` 可进一步筛选。重复导出同目录按 call ID 更新，不清除已有记录；要独立筛选结果，用新目录。静态 HTML 需重新导出才能看到新日志。

三种协议均提取输入、回复、thinking（协议返回时）、工具调用/回传结果、token、结束原因和原始数据。模型调用耗时不冒充 harness 总耗时；执行成功、业务加工后的答案、未回传模型的工具结果需 harness 额外上报。Responses 若仅传 `previous_response_id`，页面显示引用；未捕获的前序上下文无法恢复。旧日志缺 execution ID 时逐调用展示、缺批次时不推断批次，也不隐藏为历史重试。

## 3. 用现成 harness 批跑

安装 `eval_harness/requirements.txt` 和对应 CLI/服务，确认运行 YAML 内的 Bundle 路径。Claude/Pi/Insurance 已有配置：
```bash
EVAL_CONCURRENCY=4 python3 -m eval_harness.run --config eval_harness/configs/runs/pi.yaml --all --run-id batch_0928_pm
open eval_runs/batch_0928_pm/llm_trace.html
```
可换 `claude.yaml` / `insurance.yaml`；单题用 `--cases A01` 替换 `--all`。同批失败续跑加 `--resume`：成功题跳过，失败题归档到 `cases/<case_id>/attempts/` 后重试。新一轮实验用新 `--run-id`；省略时自动生成。题间并发、题内顺序；Insurance 的 `INSURANCE_LIVE_CONCURRENCY` 优先。runner 自动管理 ID、结果目录、恢复、HTML/Excel；运行 YAML 是调度配置，`.env` 是模型连接配置。

## 4. 新增 harness：Bundle + 一个执行函数

题库自行转成 `eval_harness/bundles/my_cases.jsonl`，每行一道题、case ID 唯一；runner 已完成 Bundle → `case_id/turns`：
```json
{"schema_version":"1.0","case_id":"A01","turns":["第一轮问题","追问"]}
```
现有 [import_bundle.py](../eval_harness/src/eval_harness/import_bundle.py) 只支持约定的 A–F 两位题号 Markdown。其他格式只需转为以上 JSONL。

在 `eval_harness/src/eval_harness/adapters/my_agent.py` 写 `run_case(*, case_id, turns, case_dir, project_cwd, **options)`，**无需继承基类**；在 [registry.py](../eval_harness/src/eval_harness/adapters/registry.py) 导入并 `register_adapter("my_agent", run_case=run_case)`。`case_dir` 是本次结果目录，`project_cwd` 是 profile 指定的配置模板/工作目录。函数新建本题独立会话/临时目录，将 `turns` 逐轮转为 CLI 参数或 HTTP 请求并执行，处理超时/失败、保存原始输出；模型请求用 `trace_headers()` 取得 runner 已分配的 ID，**不要另生成 execution ID**。按 [base.py](../eval_harness/src/eval_harness/adapters/base.py) 返回 `CaseRunResult` 与各轮 `TurnResult`；执行/解析参考 [pi.py](../eval_harness/src/eval_harness/adapters/pi.py) 的 `run_case/run_turn`。返回值只补充执行状态、逐轮业务答案和本地事件，并服务批跑恢复/Excel；不再是模型 tracing 的前提。

复用现成 `configs/profiles/pi.yaml` 与 `configs/runs/pi.yaml` 的结构，填新 `harness`、程序/服务位置、`project_cwd` 与 `bundle_path`，删去 Pi 专用 `adapter` 参数，保留自己函数需要的参数；输出目录、超时、并发可沿用默认。执行前用上一节命令加 `--dry-parse` 检查题库。Insurance 本地启动脚本已补齐批次请求头透传；正在运行的旧服务需下次重启该脚本后生效。
