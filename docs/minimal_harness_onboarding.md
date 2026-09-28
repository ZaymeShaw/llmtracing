# 最小接入指引（审阅稿）

路径/命令以仓库根目录为准。**只看 tracing：自己的 harness → LiteLLM → HTML，无须 adapter、Bundle 或运行 YAML。交给框架批跑：Bundle → runner → adapter → harness，框架负责恢复和报告。**已有 Claude/Pi/Insurance 直接用对应配置。

## 1. 数据：只需转成 Bundle

保存为 `eval_harness/bundles/my_cases.jsonl`，每行一道题，题号唯一；`turns` 是同一会话依次提出的非空问题，不含标准答案：
```json
{"schema_version":"1.0","case_id":"A01","turns":["第一轮问题","追问"]}
```
任何题库均可自行转成该格式，**Bundle → `case_id/turns` 已由 runner 完成**。现有 [import_bundle.py](../eval_harness/src/eval_harness/import_bundle.py) 只导入 `**A01**` 类 A–F 两位数字题号、“第1轮：”分轮的 Markdown。

## 2. Adapter：写一个“执行整题并返回结果”的函数

新建 `eval_harness/src/eval_harness/adapters/my_agent.py`，**无需基类**，实现 `run_case(*, case_id, turns, case_dir, project_cwd, **options) -> CaseRunResult`；在 [registry.py](../eval_harness/src/eval_harness/adapters/registry.py) 导入函数并 `register_adapter("my_agent", run_case=该函数)`。

`case_id: str`、`turns: list[str]` 来自题库；`case_dir: Path` 是**结果目录**（默认 `eval_runs/<run_id>/cases/A01/`）；`project_cwd: Path` 是 profile 指定的**配置模板/工作目录**（如 `workspaces/my_agent/`），两者不要混用。`options` 含 `harness_bin`、`timeout_sec` 及 `adapter` 自定义参数。

函数必须完成四件事（可拆内部函数；现成参考 [pi.py](../eval_harness/src/eval_harness/adapters/pi.py)）：
1. **准备执行：**生成本次 execution ID，新建本题会话。CLI 复制必要配置到独立目录，可复用 `agent_env.prepare_case_workdir`；HTTP 服务新建 session。不能与其他题共享会话/可变临时文件。
2. **逐轮调用：**把每个问题变成 CLI 参数或 HTTP 请求，实际启动/发送并等待；下一轮延续本题上下文。所有模型请求携带第 3 节的追踪请求头。
3. **收集结果：**解析答案、工具事件，原始输出写入 `case_dir`；超时终止/取消，错误记录为失败。Pi 的 `run_turn` 示范执行/解析，`pi_events_to_claude_like` 示范当前工具事件转换。
4. **返回结果：**按 [base.py](../eval_harness/src/eval_harness/adapters/base.py) 构造 `CaseRunResult(case_id, session_id, turns, success, exit_code, error, wall_ms)`；每轮 `TurnResult` 填写 `index/prompt/exit_code/stream_path/final_text/raw_events`。**这些返回值用于记录真实逐轮答案、执行状态/耗时和本地事件，驱动失败恢复、Excel 与整案总览；只看模型 tracing 不需要它们。**批次调度、恢复、报告无需重写。

## 3. 不写 adapter，直接使用 tracing

将自己 harness 的模型地址指向第 4 节的 LiteLLM。在每次实际模型请求中传：
```python
extra_headers={"X-Eval-Case-Id": case_id, "X-Eval-Execution-Id": execution_id}
```
同次整题执行共用这两个值，整题重跑生成新 `execution_id`（UUID）。CLI 要通过自身支持的 headers 配置传递；HTTP 业务服务须透传至内部模型请求，不能只加在业务接口。可参考 [relay_inject.py](../eval_harness/src/eval_harness/relay_inject.py)。

**并发由你自己的 harness/脚本调度**；每个请求独立带 ID，中转按 `call_id` 配对请求/响应。结束后执行下面命令，即可使用同一套 HTML 的题目列表、模型调用详情、上下文和原始请求/响应查看功能：
```bash
export PYTHONPATH="$PWD/eval_harness/src"
python3 -m eval_harness.llm_trace_html --from-gateway-log llm_gateway/logs/llm_calls.jsonl --out-run-dir eval_runs/my_wire --cases A01,A02
open eval_runs/my_wire/llm_trace.html
```
**当前导出按 case_id 分组，不按 execution_id 分组。**并发不同题号可用；跨批次、跨 harness 或重跑需使用每次执行唯一的追踪题号（如 `batch1_pi_A01_try1`，原始题号自行保留），或先按 execution_id 截取本次日志再导出；相应修改 `--cases`。新输出目录不会自动排除历史调用。大日志宜先切片。

页面会从模型日志合成总览，但不能据此确认 harness 成功、实际多轮边界、模型调用前后的总耗时，以及未发回模型的工具结果；最终答案仅从模型响应推断。这条命令生成静态 HTML/逐题调用数据，不负责批跑、恢复或生成 Excel；新增日志后需重新导出。

## 4. 配置：仅批跑需要运行 YAML；tracing 只需模型连接

交给 runner 批跑才需要以下配置。创建 `workspaces/my_agent/` 并准备所需配置，新增两个 YAML（路径相对各自文件）：
```yaml
# eval_harness/configs/profiles/my_agent.yaml
{schema_version: "1.0", profile_id: my_agent, harness: my_agent,
 project_cwd: ../../../workspaces/my_agent, bin: my-agent-cli,
 output: {eval_runs_dir: ../../../eval_runs}, adapter: {}}
```
```yaml
# eval_harness/configs/runs/my_agent.yaml
{profile_path: ../profiles/my_agent.yaml, bundle_path: ../../bundles/my_cases.jsonl}
```
**用户真正选择的是 harness、题库，以及不能推断的程序/服务位置。**其余版本号、目录和配置引用可按约定生成；当前尚无自动生成命令，上面是现有 runner 的写法。`bin` 仅 CLI 使用，逐题入口要求 `project_cwd`，服务参数放 `adapter`。题号、问题、结果目录由 runner 提供，执行 ID 由 adapter 生成。

**中转基于 LiteLLM Proxy：**harness → 本地端口 → 上游模型；callback 写 `llm_gateway/logs/llm_calls.jsonl` → runner → tracing。复制 [llm_gateway/.env.example](../llm_gateway/.env.example) 为同目录 `.env.myrelay`，填写 `UPSTREAM_API_BASE`、`UPSTREAM_API_KEY`、`UPSTREAM_PROTOCOL`、`UPSTREAM_MODEL` 及 `LITELLM_HOST`、`LITELLM_PORT`、`LITELLM_MASTER_KEY`。安装 LiteLLM 后执行 `bash llm_gateway/start_litellm.sh --profile myrelay`；已有中转直接复用。

harness 配本地 `base_url`、master key、model：OpenAI 兼容地址如 `http://127.0.0.1:4001/v1`，Anthropic 去掉 `/v1`。**中转本身不依赖 Claude；但现启动脚本硬调用 `sync_claude_settings.py`，缺少 Claude settings 会失败，这是现有脚本耦合，尚未修复，不是正常接入要求。**

## 5. 并发批跑与 tracing

先安装 `eval_harness/requirements.txt` 依赖及所需 CLI/服务；已有 harness 换用 `claude.yaml/pi.yaml/insurance.yaml`，确认 Bundle 路径。
```bash
export PYTHONPATH="$PWD/eval_harness/src"
CFG=eval_harness/configs/runs/my_agent.yaml
python3 -m eval_harness.run --config "$CFG" --all --dry-parse
EVAL_CONCURRENCY=4 python3 -m eval_harness.run --config "$CFG" --all --run-id my_run
open eval_runs/my_run/llm_trace.html
```
并发 4 道题、题内顺序执行；默认 1。逐题 profile 可设 `concurrency`，环境变量优先；Insurance 的 `INSURANCE_LIVE_CONCURRENCY` 优先。单题用 `--cases A01` 替换 `--all`；续跑同一 run-id 加 `--resume`。结果含 HTML、Excel、`cases/`；重建 HTML 用 `python3 -m eval_harness.llm_trace_html eval_runs/my_run`。新 adapter 不自动获得现有专用预检或 suite 编排。
