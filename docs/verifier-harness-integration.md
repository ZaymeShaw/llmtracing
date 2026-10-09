# Verifier 自身接入 llmtracing

已于 2026-10-02 在 verifier-C 实施。运行仍由 verifier 的 CLI / 前端触发，没有新增 eval_harness adapter，也没有修改业务 trace provider。

## 本机配置

Verifier 路径：`/Users/xiaozijian/WorkSpace/projects/claude_code/verifier-runC/verifier`。
其 `.env` 已恢复原始上游地址与密钥。通过 verifier 的 `tracing run` 启动时，仅向子进程覆盖以下本地地址及对应密钥；模型、角色策略和流式参数保持原配置。

| 中转 | 本地模型地址 | 模型 |
|---|---|---|
| verifier_primary | http://127.0.0.1:4005/v1 | gemini-3.8-flash-medium；live_stub 的 deepseek-chat |
| verifier_fallback1 | http://127.0.0.1:4006/v1 | qwen3.8-flash，原备用端点 1 |
| verifier_fallback2 | http://127.0.0.1:4007/v1 | qwen3.8-flash，原备用端点 2 |

配置和生命周期归 verifier 管理：

- `tracing/config.yaml`：llmtracing 安装路径、本地端口、LiteLLM 参数和 callback 配置。
- `scripts/tracing.py`：从原始 verifier 配置生成本地中转路由，调用 llmtracing 的 gatewayctl 启停。
- `.runtime/tracing/`：自动生成的配置、0600 的凭证文件、PID 和采集日志，已 gitignore。历史日志已迁入。

三个中转分别保留各自的上游地址和凭证，使用明确模型路由。设置 `LLM_GATEWAY_THINKING_POLICY=passthrough` 并显式允许 `reasoning_effort`。没有修改第三方 LiteLLM 源码。

在 verifier-C 根目录执行：

```bash
bash run.sh tracing show                       # 原始上游与本地路由（无密钥）
bash run.sh tracing run server --port 8022     # 经中转启动前端服务
bash run.sh tracing run cli --help             # CLI 同样支持
bash run.sh tracing status
bash run.sh tracing down
```

启动时自动复用/启动所需中转，配置变化则重启对应中转；应在没有评测运行时调整配置。直接 `bash run.sh server` 使用 `.env` 原始上游；仅 `tracing up` 不会改变已运行的服务。当前 8021/8022 服务已按 tracing 方式重新启动。

llmtracing 仅提供运行环境、callback 和查询/报告工具；gatewayctl 支持把配置/日志/PID 放在调用方目录，不要求复制 callback 或另建虚拟环境。由于当前 LiteLLM 按配置文件位置寻找 callback，launcher 自动在运行目录生成 `custom_callbacks.py` 软链接，指向 llmtracing 的现有入口。

## Verifier 中的改动

- `impl/core/llm_tracing.py`：请求作用域、身份头、线程池上下文传播。
- `impl/core/llm_client.py`：公共模型调用注入请求身份；健康探测独立标记。
- `impl/core/pipeline.py`：单次/批次入口、case 工作线程和重评尝试建立作用域。
- `impl/core/state_machine.py`、`impl/core/context/runtime.py`：每个并行任务单独复制上下文。
- `impl/server/batch_jobs.py`：批次 job_id 作为 tracing run_id。
- `tests/test_llm_tracing.py`：并发、重试、异常清理、请求边界及查询身份测试。

身份规则：

- `X-Eval-Harness: verifier`。
- `run_id` 为一次评测批次；前端批次使用已有 job_id。
- `case_id` 使用真实 case ID。非 ASCII 等不适合 HTTP 头的字符按确定性规则编码并带哈希，避免归一化碰撞。
- 同 case 的整案重评使用新 execution_id；同次评测中的模型重试和自动降级保留 execution_id。
- 两条重复 case 也有独立 execution_id；角色、阶段、项目和 verifier trace 引用作为附加标签采集。
- 批次汇总使用 `__batch__`，模型健康探测使用独立的 `__health_probe__` 身份，不归到业务 case。
- 直接使用 LlmClient 而未进入 pipeline 的调用仍有独立身份，但无法自动知道外层未提供的批次；外层要成组展示时使用 trace_scope。
- 查询使用前端批次 job_id 或 run_chain 返回的 llmtracing.run_id；身份通过请求头采集，不向业务 RunTrace 增加字段。

这里的身份只注入 verifier 自身模型请求。业务 provider 的身份与采集行为未修改。

## 查询与 HTML

用前端批次的 job_id 或 run_chain 结果中的 llmtracing.run_id 查询。在 verifier-C 根目录执行：

```bash
bash run.sh tracing runs                    # 查看已有批次
bash run.sh tracing report --open           # 默认导出并打开最新批次
bash run.sh tracing report --run-id '<run_id>' --out .runtime/tracing/reports/my-run
```

指定上述输出目录时生成 `.runtime/tracing/reports/my-run/llm_trace.html`；省略 `--out` 时自动按 run_id 分目录保存并打印路径。这里只读取已采集日志，不重新调用模型；合并这个 verifier 的主/备用中转记录及轮转日志。需要 JSON 时，在 llmtracing 根目录显式指定日志范围：

```bash
llm_gateway/.venv/bin/python llmtrace query calls \
  --log /Users/xiaozijian/WorkSpace/projects/claude_code/verifier-runC/verifier/.runtime/tracing/logs \
  --run-id '<run_id>' --include-rotated --format json
```

JSON 保留 project_id、role、stage、verifier_trace_id。模型请求成功不等于 verifier 的评测结论通过。

## 验证证据

`var/verifier_integration_20261002/validation.json` 与 `report/llm_trace.html`：

- verifier 相关测试累计 78 项通过，中转/query 测试 39 项通过。
- 两个并发主模型请求及两个备用端点请求均使用真实 verifier LlmClient 和真实模型成功返回。
- 模拟主端点返回 503，真实备用模型接管，主请求与备用请求保持同一 execution_id。健康探测有自己的身份。
- 最终采集中的五个测试 case 身份、harness、角色、终态及 HTML case 集合核对通过，保留验证期间的尝试历史。
- 此次验证未触发被测业务接口，也没有运行完整的领域 judge / 120 题评测；live_stub 的 deepseek-chat 路由已配置，但未单独调用该模型。

原配置备份位于 `var/verifier_integration_20261002/verifier.env.before`（含凭证、0600，不应分享）。代码修改前的受影响文件保存在同目录 `before/`。这些备份包含当时已有改动，不应使用 git reset 覆盖整个 verifier 工作区。

配置归属调整与服务切换记录保存在 `var/verifier_owner_migration_20261002/`。verifier 根目录也保留 `.env.before-llmtracing.20261002_211458` 原始备份，以及切换前本地中转版 `.env` 的独立备份，均为 0600、已忽略。

本次归属调整验证：80 项 verifier 相关测试及 7 项中转管理测试通过；通过 `tracing run python` 发起两个并发主通道请求和两个备用通道请求，4/4 成功，身份无串线，HTML 已生成。验证期间 `.env` 与原始备份保持逐字节一致，三条中转健康且无配置漂移，8021/8022 健康接口正常。详情见 `var/verifier_owner_migration_20261002/validation.json`；未运行业务领域评测。
