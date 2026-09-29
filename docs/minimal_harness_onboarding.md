# Harness 最小接入指引

**Adapter 模式**由框架调用你的 harness，负责批跑和续跑。**自己跑 harness 模式**沿用自己的数据和脚本，把模型请求接到本地中转即可；两种模式都能看 tracing。

下面的 `myrelay` 是新建中转的示例，端口以实际启动的中转配置为准；`.env.example` 中的 `4001` 只是示例默认值。

先认这几个位置。`myrelay / my_agent / my_batch` 是示例名；标“新建”的文件由你准备，其余工具已提供，日志和报告自动生成。命令均在仓库根目录执行。

```text
llmtracing/
├── llm_gateway/                         # 两种模式共用的 LiteLLM 中转
│   ├── .env.example                    # 中转配置样例
│   ├── .env.myrelay                    # 复制后填写：上游、端口、密钥、日志目录
│   ├── start_litellm.sh                # 启动中转，--profile myrelay 选择上面的配置
│   └── logs/llm_calls.jsonl            # 中转自动写入；各批次共用，导出时按 run_id 筛选
├── eval_harness/                       # 以下接入文件仅 Adapter 模式需要
│   ├── bundles/my_cases.jsonl          # 新建：转成 Bundle 的题库（第 1 节）
│   ├── configs/profiles/my_agent.yaml  # 新建：harness 的运行方式、工作目录和参数
│   ├── configs/runs/my_agent.yaml      # 新建：选哪份 profile、哪份题库
│   └── src/eval_harness/adapters/
│       ├── my_agent.py                # 新建：run_case 调用你自己的 harness
│       ├── registry.py                # 在这里注册新 adapter
│       └── base.py                    # 查看 CaseRunResult / TurnResult 返回格式
├── docs/examples/minimal_adapter.py    # 可复制的最小 adapter 示例
└── eval_runs/my_batch/                 # 自动生成：本批报告目录（第 4 节）
    ├── cases/                         # 按题/执行保存的追踪数据
    └── llm_trace.html                 # 最后打开这个页面
```

**你原有的 harness 项目可以放在仓库外**：在原来的模型配置/调用处改中转地址、密钥和请求头；用 adapter 时，`project_cwd` 指向它需要的工作/模板目录。

## 1. 准备数据

**用 adapter**：任意题库转成 `eval_harness/bundles/my_cases.jsonl` 即可。每行一道题、题号唯一，`turns` 是同一会话依次提出的问题：
```json
{"schema_version":"1.0","case_id":"A01","turns":["第一轮问题","追问"]}
```
**自己跑**：保留原数据格式，为每题指定 `case_id`、每批指定 `run_id`；其余标识按 3b 生成。

## 2. 公共配置：启动本地中转

**harness → LiteLLM Proxy → 上游模型**；中转记录请求与响应，用于生成 tracing，不依赖任何特定 harness。

首次安装并复制树中的配置样例（已有环境跳过安装）：
```bash
python3 -m venv llm_gateway/.venv
source llm_gateway/.venv/bin/activate
python -m pip install 'litellm[proxy]' -e ./eval_harness
cp -n llm_gateway/.env.example llm_gateway/.env.myrelay
```
编辑 `.env.myrelay`：四个 `UPSTREAM_*` 字段分别填上游地址、密钥、协议（`openai/anthropic`）、模型名；`LITELLM_MASTER_KEY` 自设本地密钥，`LITELLM_PORT` 填要使用的端口（示例文件填的是 `4001`）。启动：
```bash
bash llm_gateway/start_litellm.sh --profile myrelay
```
示例端口 4001 可后台启动；如果 profile 填其他端口，启动命令加 `--foreground`，端口仍只取 `.env.myrelay` 中的值。
树中的日志位置来自 `.env.myrelay` 的 `LLM_GATEWAY_LOG_DIR=./logs`（相对 `llm_gateway/`）。通常不必改；改了目录，第 4 节也要读取新目录下的 `llm_calls.jsonl`。

接下来，让你的 harness **把原本直接发给模型服务的请求，改发给本地中转**。在它原有的模型配置里改两项：

- **服务地址**：指向刚启动的 `LITELLM_HOST:LITELLM_PORT`；OpenAI 协议在地址末尾加 `/v1`，Anthropic 协议不加。
- **访问密钥**：填刚才设置的 `LITELLM_MASTER_KEY` 的值。

继续用原来的语言、SDK 和调用方式即可。默认中转配置统一转发到 `UPSTREAM_MODEL`，harness 原来的模型名可保留。

## 3. 接入并运行（选一种）

**a) 用 adapter 交给框架批跑**

复制 [最小 adapter 示例](examples/minimal_adapter.py) 到 `eval_harness/src/eval_harness/adapters/my_agent.py`，无需基类。只需替换 `execute_harness`：调用自己的 CLI/HTTP/Python harness，逐轮执行问题，题内延续会话、题间隔离，传递 headers；等待结束，失败/超时抛异常。示例已封装 `run_case` 的返回状态，追踪头由框架提供。

`run_case` 输入：`case_id: str`、`turns: list[str]`、`case_dir: Path`（本题结果目录）、`project_cwd: Path`（工作/模板目录）、`**options`。**只看模型 tracing，返回执行状态即可，结果 `turns=[]` 可留空**；业务逐轮报告才需补 [TurnResult](../eval_harness/src/eval_harness/adapters/base.py)。

在树中的 [registry.py](../eval_harness/src/eval_harness/adapters/registry.py) 导入 `from .my_agent import run_case`，再 `register_adapter("my_agent", run_case=run_case)`。按树中新建 profile 和 run 两份配置（路径相对各自文件，`project_cwd` 改为你的目录）：
```yaml
# eval_harness/configs/profiles/my_agent.yaml
{harness: my_agent, project_cwd: ../../.., output: {eval_runs_dir: ../../../eval_runs}, adapter: {}}
# eval_harness/configs/runs/my_agent.yaml
{profile_path: ../profiles/my_agent.yaml, bundle_path: ../../bundles/my_cases.jsonl}
```
CLI 路径填 profile 的 `bin`；自定义参数放 `adapter`，由 `options` 接收。已有 harness 直接改用同目录的 `claude.yaml / pi.yaml / insurance.yaml`。
```bash
EVAL_CONCURRENCY=4 python -m eval_harness.run --config eval_harness/configs/runs/my_agent.yaml --all --run-id my_batch
```
并发 4 题；同批续跑加 `--resume`，跳过成功题。新批次换 `--run-id`；追踪标识由框架生成。

**b) 自己跑 harness，只接中转**

完成第 2 节后，只需让每次模型请求带上三个 HTTP 请求头；不需要导入本项目的 Python 模块：
```http
X-Eval-Run-Id: my_batch
X-Eval-Case-Id: A01
X-Eval-Execution-Id: 8e761017-9848-4474-96ae-d102234f4c61
```
分别表示**批次、题号、本题本次执行**。每次开始执行一道题时生成一个全局唯一的 execution ID（如 UUID），题内所有模型请求共用。整题重试只换 execution ID；新批次换 run ID，题号不变。这样即可沿用自己的并发脚本，各题、各次尝试分开记录。

可选：同一批比较多个 harness 时加 `X-Eval-Harness` 区分；`X-Eval-Started-At` 可填整题开始时间（UTC ISO 格式），不填则以首次模型请求时间排列尝试。

**两种模式的头都必须到达中转**：用现有 SDK 的自定义 headers 参数、CLI 的请求头配置，或让 HTTP 服务透传到内部模型请求。并发时逐请求传入，勿修改共享客户端的全局 headers。

## 4. 查看 tracing

使用第 2 节已安装 `eval_harness` 的虚拟环境；新终端先执行 `source llm_gateway/.venv/bin/activate`。

Adapter 批跑后直接打开树中的 `eval_runs/my_batch/llm_trace.html`。自己跑时，先从**中转日志**按 `run_id` 选出本批请求，生成到**报告目录**。沿用默认路径，只需把命令中的 `my_batch` 换成自己的批次名：
```bash
python -m eval_harness.llm_trace_html --from-gateway-log llm_gateway/logs/llm_calls.jsonl --run-id my_batch --out-run-dir eval_runs/my_batch
open eval_runs/my_batch/llm_trace.html
```
`--from-gateway-log` 是已有的中转日志文件，`--run-id` 是要看的批次，`--out-run-dir` 是生成报告的目录；后者不决定中转把日志写在哪里。

同批同题默认显示最新尝试，勾选「显示历史尝试」可看旧记录；新增日志后重新导出。展示模型调用、上下文和原始请求/响应，不包含未发给模型的本地执行过程。
