# DSH lane 实跑与复现

2026-10-09 已在本机通过真实上游验证：DeepSeek Harness JSON-RPC SDK → 本项目 LiteLLM lane → Bailian OpenAI-compatible `deepseek-v4-flash-0731`。使用本地 DSH 构建产物及配套 Python SDK源码，Node 24.19.0、LiteLLM 1.74.8、OpenAI SDK 3.17.0。

## 验收结果

最终批次：`eval_runs/dsh_lane_final_20261009/`。

| 样例 | lane | 模型请求数 | 结果 |
| --- | --- | --- | --- |
| D01 单轮指定文本 | dsh_4107 | 1 | 通过 |
| D02 同一 session 两轮记忆 | dsh_4109 | 2 | 通过 |
| D03 bash 读取文件，再由模型回答 | dsh_4107 | 2 | 通过 |

D01 与 D02 并发执行，D01 完成后同一 lane 执行 D03。5 次模型请求均成功，归因来源均为 `lane_registry`，没有未结束调用。5 次均采集到首个非空文本/推理片段，耗时约 2.1–2.6 秒。DSH 的原生工具事件确认 bash 成功返回文件内容；工具结果出现在下一次模型请求中。

产物：

- `llm_trace.html`：复用现有 HTML 模型交互报告。
- `summary.json`：逐题、逐轮状态和回答校验。
- `verification.json`：独立检查的调用归因、首字耗时与工具结果回传。
- `raw/<case>/<execution>/`：SDK notifications、运行状态、网关调用、持久化 session 和隔离工作目录。

工具样例按真实成功工具结果和最终答案包含目标内容验收，允许 Markdown 包装；单轮与记忆样例精确匹配答案。此前探测失败批次单独保留，不覆盖最终结果。

## 本机复跑

本机已有私有配置 `.env.dsh_4107`、`.env.dsh_4109`，凭据未提交；它们共用 `LANE_POOL=dsh_smoke`。实跑结束后专用网关停止，复跑时先启动：

```sh
llm_gateway/.venv/bin/python llmtrace gateway up dsh_smoke

llm_gateway/.venv/bin/python scripts/run_dsh_wire.py \
  --dsh-checkout /Users/xiaozijian/WorkSpace/package/dsh/deepseek-harness \
  --node /Users/xiaozijian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  --lanes dsh_smoke \
  --run-id dsh_my_next_run

llm_gateway/.venv/bin/python llmtrace gateway down dsh_smoke
```

每次选择新的 run ID。默认运行上表三个样例；`--cases D01,D02` 可选题，`--bundle /path/cases.jsonl` 可换自己的题库（每行 `case_id`、字符串数组 `turns`）。模型由各 lane 的 `UPSTREAM_MODEL` 决定，默认每次模型输出上限 4096 token、每题超时 180 秒，分别用 `--max-tokens`、`--timeout` 调整。

此脚本当前是独立批跑入口，尚未注册统一 eval adapter。使用 DSH 的 `examples/jsonrpc-agent/cordis.yml`，可用 `--cordis` 指定其他 JSON-RPC composition；默认示例的提示词、工具和插件组合与产品 headless 不完全相同。

## 与其他 harness 相同的业务题库和 MCP

新增 `--mcp-config` / `--agent-prompt`：加载原 Pi/OpenCode 的 insurance-tools stdio 配置及业务提示词，按题生成 DSH MCP composition。业务用户身份与原 MCP 配置一致，`ITOOLS_AGENT_ID=dsh` 用于区分调用来源；不挂本地 bash、文件、skill、subagent 工具。MCP 配置中的环境值通过子进程环境传入，生成的 Cordis 文件不内嵌凭据。MCP 发现失败直接终止启动。

```sh
llm_gateway/.venv/bin/python scripts/run_dsh_wire.py \
  --dsh-checkout /Users/xiaozijian/WorkSpace/package/dsh/deepseek-harness \
  --node /Users/xiaozijian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  --lanes dsh_smoke \
  --bundle eval_harness/bundles/120_prompt_only_0922_shared.jsonl \
  --mcp-config workspaces/pi/.mcp.json \
  --agent-prompt workspaces/pi/agent.md \
  --cases A01,C20,F13 --thinking off --max-tokens 8192 --timeout 600 \
  --run-id dsh_insurance_my_next_run
```

这三题是现有四 harness smoke 的原题；省略 `--cases` 可跑完整共享题库。输入文件路径与 SHA256 写入 `inputs.json`。业务运行会检查非空最终回答、MCP 调用与返回成对、后端及基础设施错误，而非只看 agent 退出码；`summary.json` 的 `mcp_health` 保留工具列表及错误分类，不代表答案正确性评分。

用户明确授权三题及 MCP 返回的客户、保单信息经中转发送至原有 `dashscope.aliyuncs.com` / `deepseek-v4-flash-0731` 后，最终业务批次 `eval_runs/dsh_insurance_mcp_ready_20261009/` 已完成。

| 原题 | lane | 模型请求数 | MCP 调用数 | 接入结果 | 回答观察 |
| --- | --- | --- | --- | --- | --- |
| A01 | dsh_4107 | 3 | 2 | 通过 | 找到两张保单；返回中无累计已交保费字段，模型说明缺失但未继续查详情，任务未完全满足 |
| C20 | dsh_4109 | 2 | 3 | 通过 | 查询两张保单的人员信息；条款检索为 PARTIAL，模型未声称确认到目标免责内容 |
| F13（两轮） | dsh_4107 | 5 | 4 | 通过 | 首轮查询档案与保单，第二轮保持会话并按投保人条件重新查询 |

10 次模型请求全部完成，9 次真实 MCP 调用均成对返回，没有基础设施或上游调用错误。三题首个请求都带上全部 7 个 MCP 工具；工具结果均出现在后续模型请求中。`verification.json` 独立核验模型、端点、thinking off、与现有 OpenCode lane 相同的上游配置、首帧、归因及 F13 两轮历史；不输出访问凭据。`llm_trace.html` 可查看模型交互，`business_review.md` 记录回答缺口。`summary.json` 的 success 表示接入及工具调用健康，不能当作业务准确率。

早期 `dsh_mcp_preflight_20261009/result.json` 仅完成 SDK initialize，无法证明 MCP 发现已结束。随后的 `dsh_insurance_shared_verified_20261009/` 暴露首轮工具为空的启动竞态：SDK transport 可先于 MCP plugin 就绪。已新增 `scripts/dsh_jsonrpc_ready.mjs`，等待 DSH `boot()` 完成全部插件激活并核验工具列表后，原子写入 readiness 文件；Python SDK 等到该信号才 initialize 和发题。发现失败或超时会终止本题，避免用固定延时猜测是否就绪。失败批次保留供排查。

## 新建 lane 所需配置

从 `.env.example` 建立独立私有 profile，设置实际 `UPSTREAM_*`、空闲端口和本地访问密钥，再添加：

```dotenv
LITELLM_CONFIG=./config.litellm.dsh.yaml
LANE_POOL=dsh_smoke
LLM_GATEWAY_THINKING_POLICY=passthrough
LLM_GATEWAY_OMIT_EMPTY_TOOL_DELTAS=1
```

DSH 由 SDK 子进程驱动，没有 HTTP 业务端点，因此无需 `LANE_TARGET_URL`。公共 HTTP runner 仍要求该字段，DSH 脚本直接选择具名网关并检查 ready marker。每个 worker 独占一个 lane，退出 runtime 并等待采集完成后才释放；超时、worker 失败或采集未结束会标记 blocked，停止该 lane 的后续题目。确认旧进程停止后可用 `llmtrace lane recover <name> --execution-id <id>` 恢复。

## 实跑发现并处理的兼容问题

1. **thinking 参数转发。** 当前 LiteLLM 的 `openai/` 路径不原生认可目标模型的 reasoning 参数。observer 模式将请求中明确存在的 `thinking`、`reasoning_effort` 搬到 SDK `extra_body`，最终 HTTP JSON 仍是顶层参数。移除了 deployment 的 `allowed_openai_params`：它会在调用方没有 effort 时添加 null，触发百炼 400。不开启 `drop_params`，也不硬编码 thinking 开关。真实 LiteLLM/OpenAI SDK 和内存 HTTP endpoint 的测试验证 enabled/disabled、effort 的存在性及输出上限转发。
2. **工具流式续片的空身份字段。** 实际续片携带空的 `id` / `function.name`，DSH 原生 provider 会覆盖先前的有效名称。DSH lane 启用 `LLM_GATEWAY_OMIT_EMPTY_TOOL_DELTAS=1` 后，仅从发给客户端的副本中省略这些空字段，保留工具 index 和参数分片；内部原始捕获对象不修改。未启用的其他网关行为不变。

未修改第三方包源码或 DSH 源码。若更换 LiteLLM/DSH 版本，应重新运行工具往返和参数透传测试。

## 验证范围与限制

最初 smoke 覆盖真实单轮、同会话多轮、保险 MCP 工具往返、两个 lane 并发归因、HTML 导出。针对超时/失败不复用 lane、工具结果校验、启动 readiness、thinking 转发和流式工具分片的相关回归测试共 33 项通过。

后续完整批次 `eval_runs/dsh_datasetA_120_20261009/` 已完成共享题库的 120 题、174 轮：507 次模型请求全部闭合，其中 1 次连接失败由原生 SDK 重试恢复；523 次 MCP 调用均有对应返回，其中 17 次为上游错误。SDK、网关采集及 HTML 的原题、回答、工具结果和归因一致性核验通过。`scripts/verify_dsh_run.py RUN_DIR` 可复核这些完整性检查。运行完整性通过不表示后端无错误或业务答案全部正确；尚未评答案准确率、子 agent 或原生上下文压缩。

随后 Claude、Pi、OpenCode、Insurance 也各完成同一题库的 120 题、174 轮重跑，结果在本地 `eval_runs/comparison_gzip_20261009/`。四份 HTML 与普通 JSONL 恢复后生成的结果逐字节一致，Excel 调用数据一致，失败与重试历史保留。业务原始结果和私有配置不随代码提交。

观察到一个现有采集限制：LiteLLM 在 `pre_api_call` 之前拒绝不支持参数时，失败记录可能缺少 lane 归因。初次参数探测出现过，最终正确配置没有触发。runner 对无调用/失败结果不会报成功，但该早期错误的逐题归因还需单独补齐。
