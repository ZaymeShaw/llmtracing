# Harness 最小接入指引

**主要方式：自己运行 harness，把模型请求接到本地 LiteLLM 中转，就能记录和查看 tracing。** 如果希望复用框架的题库加载、并发和续跑，可以再接 adapter；它是可选的运行入口。

链路：`你的数据和运行脚本 → harness → LiteLLM → 上游模型`；LiteLLM 将模型交互写入日志，再按批次导出 HTML。harness 项目可以放在仓库外，数据沿用原格式。

```text
llmtracing/
├── llm_gateway/.env.example          # 中转配置样例
├── llm_gateway/.env.myrelay          # 你复制填写的配置
├── llm_gateway/start_litellm.sh       # 按配置启动中转
├── llm_gateway/logs/llm_calls.jsonl   # 默认日志位置，由 LLM_GATEWAY_LOG_DIR 决定
├── scripts/run_opencode_wire.py      # 已跑通的示例：自己调用 OpenCode、并发执行题目
└── eval_runs/my_batch/llm_trace.html  # 导出后打开这个报告
```

## 1. 配置并启动中转

以下命令在仓库根目录执行。首次安装环境；已有环境只需激活：

```bash
python3 -m venv llm_gateway/.venv
source llm_gateway/.venv/bin/activate
python -m pip install 'litellm[proxy]' -e ./eval_harness
cp -n llm_gateway/.env.example llm_gateway/.env.myrelay
```

编辑 `.env.myrelay`：`UPSTREAM_*` 填上游地址、密钥、协议和模型；`LITELLM_PORT` 填本地端口，`LITELLM_MASTER_KEY` 自设本地访问密钥。其他项按样例即可。`LLM_GATEWAY_LOG_DIR=./logs` 表示日志写到 `llm_gateway/logs/`；需要换目录就在配置里改。

```bash
bash llm_gateway/start_litellm.sh --profile myrelay --foreground
```

在 harness 原有的模型设置里，把**模型服务地址改为这个本地中转地址、访问密钥改为上面设置的本地密钥**。按样例端口 4001：OpenAI 客户端填 `http://127.0.0.1:4001/v1`，Anthropic 客户端填 `http://127.0.0.1:4001`；端口以你的配置为准。默认中转配置转发到 `UPSTREAM_MODEL`，原有模型名可保留。

## 2. 自己运行 harness：让请求带上题目信息

每道题开始执行时生成一个 UUID；这道题本次执行的所有模型请求都带上以下三个 HTTP 请求头：

```http
X-Eval-Run-Id: my_batch
X-Eval-Case-Id: A01
X-Eval-Execution-Id: 8e761017-9848-4474-96ae-d102234f4c61
```

它们分别是**批次、题号、本题本次执行**。题内多轮共用这些值；整题重试换 execution ID，新批次换 run ID。用 SDK/CLI 的自定义请求头配置传入；若经过自己的 HTTP 服务，继续透传到内部模型请求。并发时为每题保存自己的头，勿改共享客户端的全局 headers。同批比较多个 harness 时再加 `X-Eval-Harness`。

然后照常用自己的脚本并发跑题，题内延续会话、题间隔离。**不需要 Bundle、adapter 或 CaseRunResult，也不需要导入本项目模块。**

现成参考：[OpenCode 执行脚本](../scripts/run_opencode_wire.py)。本机测试使用 `.env.opencode_4003`，其中 `OPENCODE_*` 配置指定 CLI、题库、MCP、提示词及临时执行目录；脚本会读取它们并生成上述请求头。中转已启动后：

```bash
python3 scripts/run_opencode_wire.py --relay-env llm_gateway/.env.opencode_4003 --run-id my_batch --concurrency 4
```

默认跑配置中的全部题，加 `--cases A01,E01` 可选题；同一 run ID 再执行会跳过已成功题。新实验请换 run ID。这个示例脚本读取 Bundle，是它的数据读取方式，不是中转的要求。

## 3. 导出并查看 tracing

从 **`.env` 的 `LLM_GATEWAY_LOG_DIR` 下的 `llm_calls.jsonl`** 读取日志，按 run ID 导出。默认配置示例：

```bash
python -m eval_harness.llm_trace_html --from-gateway-log llm_gateway/logs/llm_calls.jsonl --run-id my_batch --out-run-dir eval_runs/my_batch
open eval_runs/my_batch/llm_trace.html
```

本机 OpenCode 的日志是 `eval_runs/opencode_gateway_4003/llm_calls.jsonl`，使用上面命令时替换 `--from-gateway-log`。`--out-run-dir` 只决定报告放哪里，不决定中转日志位置。新终端先执行 `source llm_gateway/.venv/bin/activate`。

报告展示模型调用、各轮输入输出和首字耗时；同批同题默认看最新尝试，可勾选历史。新增日志后重新导出即可；模型请求之外的本地执行过程不会被中转采集。

## 4. 扩展：用 adapter 复用框架批跑

需要框架统一加载题库、并发和续跑时，可以接入 adapter。它把框架的 `run_case` 调用连接到你的 harness；模型请求仍经过前面的 LiteLLM 中转，tracing 的采集和查看方式相同。

完整目录如下。`myrelay / my_agent / my_batch` 是示例名；标“新建”的文件由你准备，运行产物自动生成。

```text
llmtracing/
├── llm_gateway/                         # 共用的 LiteLLM 中转
│   ├── .env.example                    # 中转配置样例
│   ├── .env.myrelay                    # 新建：上游、端口、密钥、日志目录
│   ├── start_litellm.sh                # --profile myrelay 选择这份配置
│   └── logs/llm_calls.jsonl            # 默认日志位置；导出时按 run_id 筛选
├── eval_harness/
│   ├── bundles/my_cases.jsonl          # 新建：转成 Bundle 的题库
│   ├── configs/profiles/my_agent.yaml  # 新建：harness 的工作目录和运行参数
│   ├── configs/runs/my_agent.yaml      # 新建：选哪份 profile、哪份题库
│   └── src/eval_harness/adapters/
│       ├── my_agent.py                # 新建：run_case 调用你的 harness
│       ├── registry.py                # 在这里注册 adapter
│       └── base.py                    # CaseRunResult / TurnResult 返回格式
├── docs/examples/minimal_adapter.py    # 可复制的最小 adapter 示例
├── scripts/run_opencode_wire.py        # 自己运行 harness 的参考，无需注册 adapter
└── eval_runs/my_batch/                 # 自动生成：本批结果目录
    ├── cases/                         # 各题及各次执行的追踪数据
    └── llm_trace.html                 # 打开查看报告
```

### 4.1. 准备 Bundle

任意题库转成 `eval_harness/bundles/my_cases.jsonl` 即可。每行一道题、题号唯一，`turns` 是同一会话依次提出的问题：

```json
{"schema_version":"1.0","case_id":"A01","turns":["第一轮问题","追问"]}
```

### 4.2. 实现 run_case 并注册

复制 [最小 adapter 示例](examples/minimal_adapter.py) 到 `eval_harness/src/eval_harness/adapters/my_agent.py`，无需基类。只需替换 `execute_harness`：调用自己的 CLI/HTTP/Python harness，逐轮执行问题，题内延续会话、题间隔离，传递 headers；等待结束，失败/超时抛异常。示例已封装 `run_case` 的返回状态，追踪头由框架提供。

`run_case` 输入：`case_id: str`、`turns: list[str]`、`case_dir: Path`（本题结果目录）、`project_cwd: Path`（工作/模板目录）、`**options`。**只看模型 tracing，返回执行状态即可，结果 `turns=[]` 可留空**；业务逐轮报告才需补 [TurnResult](../eval_harness/src/eval_harness/adapters/base.py)。

在 [registry.py](../eval_harness/src/eval_harness/adapters/registry.py) 导入 `from .my_agent import run_case`，再 `register_adapter("my_agent", run_case=run_case)`。按树中新建 profile 和 run 两份配置（路径相对各自文件，`project_cwd` 改为你的目录）：
```yaml
# eval_harness/configs/profiles/my_agent.yaml
{harness: my_agent, project_cwd: ../../.., concurrency: 4, output: {eval_runs_dir: ../../../eval_runs}, adapter: {}}
# eval_harness/configs/runs/my_agent.yaml
profile_path: ../profiles/my_agent.yaml
bundle_path: ../../bundles/my_cases.jsonl
llm_gateway_log: /你的中转日志目录/llm_calls.jsonl
```
`llm_gateway_log` 填 `.env` 中 `LLM_GATEWAY_LOG_DIR` 对应日志的绝对路径；框架从这里收集每题的模型调用。本机 OpenCode 填仓库下 `eval_runs/opencode_gateway_4003/llm_calls.jsonl` 的绝对路径。

CLI 路径填 profile 的 `bin`；自定义参数放 `adapter`，由 `options` 接收。已有 harness 直接改用同目录的 `claude.yaml / pi.yaml / insurance.yaml`。

### 4.3. 执行批跑

```bash
python -m eval_harness.run --config eval_harness/configs/runs/my_agent.yaml --all --run-id my_batch
```
按 profile 中的 `concurrency: 4` 并发 4 题；同批续跑加 `--resume`，跳过成功题。新批次换 `--run-id`；追踪标识由框架生成。

框架批跑结束后打开 `eval_runs/my_batch/llm_trace.html`。追踪头必须透传到真正的模型请求，中转才知道请求属于哪道题；HTTP harness 不一定需要本地执行目录。
