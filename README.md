# llmtracing

llmtracing 用于记录和查看 LLM 驱动的 harness / agent 的模型交互。让模型请求经过本地 LiteLLM 中转，就可以采集请求上下文、模型回复、工具调用和首字耗时，生成可在浏览器中打开的 HTML 追踪报告。

它按批次、题目和每次执行区分记录，支持并发、多轮和重试历史。你可以继续使用自己的 harness 和运行脚本；需要统一批跑时，再使用可选的 adapter 接入。中转能记录发给模型的工具结果，未进入模型请求的本地执行过程不在采集范围内。

## Harness 最小接入指引

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

### 1. 配置并启动中转

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

### 2. 自己运行 harness：让请求带上题目信息

每道题开始执行时生成一个 UUID；这道题本次执行的所有模型请求都带上以下三个 HTTP 请求头：

```http
X-Eval-Run-Id: my_batch
X-Eval-Case-Id: A01
X-Eval-Execution-Id: 8e761017-9848-4474-96ae-d102234f4c61
```

它们分别是**批次、题号、本题本次执行**。题内多轮共用这些值；整题重试换 execution ID，新批次换 run ID。用 SDK/CLI 的自定义请求头配置传入；若经过自己的 HTTP 服务，继续透传到内部模型请求。并发时为每题保存自己的头，勿改共享客户端的全局 headers。同批比较多个 harness 时再加 `X-Eval-Harness`。

然后照常用自己的脚本并发跑题，题内延续会话、题间隔离。**不需要 Bundle、adapter 或 CaseRunResult，也不需要导入本项目模块。**

现成参考：[OpenCode 执行脚本](scripts/run_opencode_wire.py)。本机测试使用 `.env.opencode_4003`，其中 `OPENCODE_*` 配置指定 CLI、题库、MCP、提示词及临时执行目录；脚本会读取它们并生成上述请求头。中转已启动后：

```bash
python3 scripts/run_opencode_wire.py --relay-env llm_gateway/.env.opencode_4003 --run-id my_batch --concurrency 4
```

默认跑配置中的全部题，加 `--cases A01,E01` 可选题；同一 run ID 再执行会跳过已成功题。新实验请换 run ID。这个示例脚本读取 Bundle，是它的数据读取方式，不是中转的要求。

### 3. 导出并查看 tracing

从 **`.env` 的 `LLM_GATEWAY_LOG_DIR` 下的 `llm_calls.jsonl`** 读取日志，按 run ID 导出。默认配置示例：

```bash
python -m eval_harness.llm_trace_html --from-gateway-log llm_gateway/logs/llm_calls.jsonl --run-id my_batch --out-run-dir eval_runs/my_batch
open eval_runs/my_batch/llm_trace.html
```

本机 OpenCode 的日志是 `eval_runs/opencode_gateway_4003/llm_calls.jsonl`，使用上面命令时替换 `--from-gateway-log`。`--out-run-dir` 只决定报告放哪里，不决定中转日志位置。新终端先执行 `source llm_gateway/.venv/bin/activate`。

用 `llmtrace gateway` 管理的具名中转（如 Insurance lane）日志在 `llm_gateway/logs/gateways/<名>/`，可直接 `./llmtrace report html --gateway <名> --out eval_runs/x --run-id my_batch`；多实例 lane 见 `llm_gateway/README.md`。

报告展示模型调用、各轮输入输出和首字耗时；同批同题默认看最新尝试，可勾选历史。新增日志后重新导出即可；模型请求之外的本地执行过程不会被中转采集。

新产生的逐题调用归档默认无损写入 `llm_calls.jsonl.gz`，HTML、Excel 和增量导出自动读取；旧明文归档继续兼容，历史目录不自动迁移。完整 JSONL 解压后字节不变，不删字段或历史消息，也不加大小限制。实现边界与真实批次验证见 [无损调用归档](docs/lossless-call-storage.md)。

基础版的 `--zip`、runner 的 `--pack-zip` 和多 harness 的 `product_zip` 已统一使用 share 打包规则。已有 HTML 会原样装包，不在打包时重新生成，也不再要求存在 Excel：

```bash
./llmtrace report html eval_runs/my_batch --zip
```

输出仍为 `eval_runs/my_batch/my_batch_share.zip`；再次打包保留一个 `.prev.zip`，新包不包含这些历史包。需要刷新报告时，先运行不带 `--zip` 的 HTML 导出。日志导出形式 `report html --gateway NAME --out DIR --zip` 会先正常生成报告，再应用相同的打包规则。

也可指定输出位置，或从已有完整归档生成离线查看包：

```bash
./llmtrace report share eval_runs/my_batch --out eval_runs/my_batch-share.zip
# 也可从已有完整归档生成分享包
./llmtrace report share eval_runs/my_batch.zip --out eval_runs/my_batch-share.zip
```

所有原 HTML 按字节保留；页面 `href/src` 链接到的本地附件保留，缺失附件会报错。包内省略未被页面链接的 JSONL、gzip、Excel、CSV/TSV 和调试日志；其他文件沿用原样。原始数据仍在运行目录，继续用于分析和重建。导出复用密钥扫描，并验证保留文件的 SHA256 与 ZIP 校验；清单在包内 `LLMTRACE-SHARE-MANIFEST.json`。没有新增大小限制；显式 `report share --out` 不覆盖已有输出。

### 4. 扩展：用 adapter 复用框架批跑

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

#### 4.1. 准备 Bundle

任意题库转成 `eval_harness/bundles/my_cases.jsonl` 即可。每行一道题、题号唯一，`turns` 是同一会话依次提出的问题：

```json
{"schema_version":"1.0","case_id":"A01","turns":["第一轮问题","追问"]}
```

#### 4.2. 实现 run_case 并注册

复制 [最小 adapter 示例](docs/examples/minimal_adapter.py) 到 `eval_harness/src/eval_harness/adapters/my_agent.py`，无需基类。只需替换 `execute_harness`：调用自己的 CLI/HTTP/Python harness，逐轮执行问题，题内延续会话、题间隔离，传递 headers；等待结束，失败/超时抛异常。示例已封装 `run_case` 的返回状态，追踪头由框架提供。

`run_case` 输入：`case_id: str`、`turns: list[str]`、`case_dir: Path`（本题结果目录）、`project_cwd: Path`（工作/模板目录）、`**options`。**只看模型 tracing，返回执行状态即可，结果 `turns=[]` 可留空**；业务逐轮报告才需补 [TurnResult](eval_harness/src/eval_harness/adapters/base.py)。

在 [registry.py](eval_harness/src/eval_harness/adapters/registry.py) 导入 `from .my_agent import run_case`，再 `register_adapter("my_agent", run_case=run_case)`。按树中新建 profile 和 run 两份配置（路径相对各自文件，`project_cwd` 改为你的目录）：
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

#### 4.3. 执行批跑

```bash
python -m eval_harness.run --config eval_harness/configs/runs/my_agent.yaml --all --run-id my_batch
```
按 profile 中的 `concurrency: 4` 并发 4 题；同批续跑加 `--resume`，跳过成功题。新批次换 `--run-id`；追踪标识由框架生成。

框架批跑结束后打开 `eval_runs/my_batch/llm_trace.html`。追踪头必须透传到真正的模型请求，中转才知道请求属于哪道题；HTTP harness 不一定需要本地执行目录。

## 用 5 个 harness 跑同一套 120 题

本仓库已附上实际跑批使用的 **0922 题库：A01–F20，共 120 题、174 轮**。每行 JSONL 是一道题，`turns` 按顺序在同一会话中执行；题间新建会话。

| 文件 | 用途 |
| --- | --- |
| [120_prompt_only_0922_shared.jsonl](eval_harness/bundles/120_prompt_only_0922_shared.jsonl) | Claude Code、Insurance QA、OpenCode、DSH 共用的题库 |
| [120_prompt_only_0922_shared_pi.jsonl](eval_harness/bundles/120_prompt_only_0922_shared_pi.jsonl) | Pi 使用；问句和轮次相同，题号增加 `PI_` 前缀，附带映射元信息 |
| [120_prompt_only_0922.jsonl](eval_harness/bundles/120_prompt_only_0922.jsonl) | 启动器校验用原版，与 shared 文件逐字节相同 |

共享题库 SHA256：`dbf22717b99081fd7aef427c50321acb9537820e3ab544b687d4a8d55c372428`；Pi 版：`c88266625109484cef1782aca78fc65c8d526d90d7911ab645e11a5f35b3523e`。题目保留原文中的客户名和业务查询条件；文件包含题目及来源元信息，MCP 返回、模型回答和运行日志在各次运行后生成。

### 运行前准备

以下命令均在仓库根目录执行。先按前文安装 `llm_gateway/.venv` 和 `eval_harness`，并准备本地依赖：

- **共同配置**：各中转的 `UPSTREAM_*` 指向同一目标。本次实跑为百炼 `dashscope.aliyuncs.com` / `deepseek-v4-flash-0731`，thinking off；提示词使用仓库内的 `workspaces/*/agent.md`，MCP 指向同一 insurance-tools 后端。
- **Claude / Pi**：安装 `claude`、`pi` CLI，准备 `workspaces/claude/.mcp.json`、`workspaces/pi/.mcp.json`。Claude 的 `.claude/settings.local.json` 和 Pi 的 `local-relay` provider 指向主中转；Pi 注册的客户端模型别名为 `deepseek-v4-flash`，实际上游由中转配置决定。
- **OpenCode**：准备 `llm_gateway/.env.opencode_4003`，设置 `OPENCODE_BINARY`、`OPENCODE_MCP_CONFIG`、`OPENCODE_AGENT_PROMPT` 和 `OPENCODE_BUNDLE`；题库路径指向上表 shared 文件。
- **Insurance**：准备外部 `insurance-qa-agent-eval` 项目及其 Python 环境、`insurance` lane pool，其中包含 `insurance_4002`（业务地址 `http://127.0.0.1:18063/v1/chat`）。换机器需调整 [run_tracing_120.py](scripts/run_tracing_120.py) 的 `QA_ROOT`、`QA_PYTHON` 本地路径。每条 lane 的 QA 服务必须指向该 lane 的模型中转。
- **DSH**：准备已构建的 DeepSeek Harness checkout、对应 Node，以及 `dsh_smoke` lane pool；配置步骤见 [DSH lane quickstart](docs/dsh-lane-quickstart.md)。下方 `DSH_CHECKOUT` 改为实际目录，`DSH_NODE` 改为构建该 checkout 所用的 Node。

本次使用的 **insurance-tools MCP 源码与客户端配置模板已包含在 [integrations/insurance-tools-mcp](integrations/insurance-tools-mcp/)**。安装步骤、Claude/Pi/根目录配置模板和后端连接说明见 [MCP 安装与配置](integrations/README.md)。模板保留 mock ID，填好 MCP Python 的本机绝对路径后放到上述 `.mcp.json` 位置即可；`.sync_backups` 和虚拟环境未上传。

私有 `.env`、MCP 凭据、CLI 用户配置和外部 harness 安装由本机提供。前四个 harness 的启动器目前会统一检查四套依赖，即使指定 `--harness`，也需先配齐上述四套配置；实际执行时只启动选中的服务。先做本地检查（不启动服务、不调用模型）：

```bash
bash scripts/run_tracing_120.sh --check
```

### Claude Code

```bash
bash scripts/run_tracing_120.sh --all --harness claude \
  --concurrency 4 --out eval_runs/claude_120_run1 --no-open
```

报告：`eval_runs/claude_120_run1/dual_datasetA_full/claude/llm_trace.html`。

### Pi

```bash
bash scripts/run_tracing_120.sh --all --harness pi \
  --concurrency 4 --out eval_runs/pi_120_run1 --no-open
```

报告：`eval_runs/pi_120_run1/triple_datasetA_full/pi/llm_trace.html`；题号显示为 `PI_A01` 等。

### OpenCode

```bash
bash scripts/run_tracing_120.sh --all --harness opencode \
  --concurrency 4 --out eval_runs/opencode_120_run1 --no-open
```

报告：`eval_runs/opencode_120_run1/triple_datasetA_full/opencode/llm_trace.html`。此入口调用 OpenCode 原生 CLI，并从专属中转导出 tracing。

### Insurance QA

```bash
bash scripts/run_tracing_120.sh --all --harness insurance \
  --out eval_runs/insurance_120_run1 --no-open
```

报告：`eval_runs/insurance_120_run1/dual_datasetA_full/insurance/llm_trace.html`；题号显示为 `INS_A01` 等。并发度由 `insurance` pool 的 lane 数决定，每条 lane 同时一题，`--concurrency` 不控制 Insurance。启动器会启动所需的 QA 实例，结束后只停止本次创建的 QA 实例。

上面四条命令均先运行 A01、C20、F13 三题 smoke，通过后再执行完整 120 题；因此每套实际包含额外 3 次 smoke 题目执行及 preflight 探测。新批次换一个 `--out` 目录；中断后用原目录续跑，例如：

```bash
bash scripts/run_tracing_120.sh --resume eval_runs/pi_120_run1 --no-open
```

最近一次 Insurance 比较批次使用额外的独占单 lane（4111 → QA 18071）和逐题请求闭合审计；上面的命令是维护中的标准 `insurance` pool 入口，不会自动恢复那个临时批次的端口或审计脚本。标准入口的依赖、失败处理和四套一起运行方式见 [120 题运行说明](docs/run_tracing_120.md)。

### DeepSeek Harness（DSH）

```bash
DSH_CHECKOUT="../dsh/deepseek-harness"
DSH_NODE="node"

llm_gateway/.venv/bin/python llmtrace gateway up dsh_smoke

llm_gateway/.venv/bin/python scripts/run_dsh_wire.py \
  --dsh-checkout "$DSH_CHECKOUT" --node "$DSH_NODE" \
  --lanes dsh_smoke \
  --bundle eval_harness/bundles/120_prompt_only_0922_shared.jsonl \
  --mcp-config workspaces/pi/.mcp.json \
  --agent-prompt workspaces/pi/agent.md \
  --thinking off --max-tokens 8192 --timeout 600 --turn-timeout 300 \
  --run-id dsh_120_run1
```

报告：`eval_runs/dsh_120_run1/llm_trace.html`。DSH 不带 `--cases` 即运行题库全部 120 题，按 lane 数并发；每条 lane 独占一个原生 SDK worker。此命令直接跑全量，不自动执行 smoke；首次接入可先加 `--cases A01,C20,F13` 并换用单独的 run ID。DSH 当前要求每批使用新的 run ID，不支持前四套启动器的 `--resume`。

完成后可复核 DSH 采集完整性，并在没有其他任务使用这些 lane 时停止网关：

```bash
llm_gateway/.venv/bin/python scripts/verify_dsh_run.py eval_runs/dsh_120_run1
llm_gateway/.venv/bin/python llmtrace gateway down dsh_smoke
```

五套入口均保留各 harness 原生执行逻辑。新逐题调用归档默认使用无损 `llm_calls.jsonl.gz`，HTML 按原流程生成；运行状态、采集完整性和业务答案准确率应分别判断。
