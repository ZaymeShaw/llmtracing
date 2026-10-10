# DeepSeek Harness 接入 llmtracing 调研

后续进展：2026-10-09 已完成真实 lane 试跑，单轮、多轮、工具往返和双 lane 并发通过；使用其他 harness 的原题 A01/C20/F13、原业务提示词和真实 insurance-tools MCP，10 次模型请求、9 次 MCP 调用完成，均经中转到原百炼模型。接入通过，但 A01 累计保费、C20 条款内容仍有业务缺口；见 [实跑结果与复现](dsh-lane-quickstart.md)。下文保留最初调研范围。

调研日期：2026-10-09。DSH 本地源码：`../dsh/deepseek-harness`，HEAD `99f6f02fec`。本文依据本地实现及官方协议文档；未启动真实模型请求，未完成端到端接入验证。llmtracing 存在未提交的在途改动，以下判断以当前工作区为准。

## 结论

可行。第一阶段复用现有 LiteLLM 中转、独占 lane 归因和 HTML 报告；用 DSH Python SDK 驱动逐题多轮会话，并保存其原生 notifications。第二阶段把原生事件纳入报告，补齐工具执行、子 agent、压缩和重试语义。正式接入不必修改 DSH agent loop。

DSH 也能直接导出原生会话日志和 OTLP Logs，因此不强制经过中转。若目标只是分析 DSH 运行过程，可以优先做原生事件 importer；若目标是与现有 Claude/Pi/Insurance 报告比较模型交互，中转路径的复用程度更高。

## 已核对的源码事实

| 能力 | 证据 | 对接意义 |
| --- | --- | --- |
| 原生 DeepSeek provider 使用 fetch + SSE，POST `{baseURL}/chat/completions` | `packages/llm/llm-deepseek/src/adapter.ts:287` | 配置 `http://127.0.0.1:<port>/v1` 可指向本地中转；不需要替换 provider |
| 支持配置 baseURL、DEEPSEEK_BASE_URL、独立 apiKeyEnv | `packages/llm/llm-deepseek/README.md` | 可以为测试建立隔离配置；已保存 settings 会覆盖部分底层默认值，必须核对实际生效配置 |
| 自动发送 `x-deepseek-harness-session-id`，压缩请求另带 `x-deepseek-harness-compact: 1` | `packages/llm/llm-deepseek/src/adapter.ts:287` | 可保留 session 和压缩归属；该 provider 当前没有通用自定义 headers 配置 |
| `llm/stream` waterfall 支持包装流式调用 | `packages/llm/llm/README.md` | 要独立采集 LLM 语义事件时可开发旁路插件；监听器必须调用 next，不修改已冻结的请求 |
| session 记录 request/header、step、assistant/chunk、assistant/message、tool/call、tool/result 等 | `packages/core/session/README.md`、`packages/core/agent-loop/src/agent.ts` | 能补全中转看不到的工具执行及生命周期 |
| 默认持久化格式是 zstd + 可打包的 chunk 行 | `packages/session/session-persistence-jsonl/README.md` | 不能把默认文件当普通逐事件 JSONL；优先使用官方读取/解码能力 |
| 已有 session-telemetry-otel，默认 DISABLED | `packages/bundle/base/cordis.patch.yml:148` | 可配置 FULL 并发送到自建 OTLP Logs collector，无需另写 DSH exporter |
| 遥测仅保留每个 turn/step 的首个 assistant/chunk | `packages/session/session-telemetry/README.md` | 不是完整流式轨迹；不能直接用于逐 token 回放，首 chunk 也不保证是首个非空文本 token |
| SDK 支持 start_session(session_id)，Session.run 可连续多轮 | `python/sdk/src/deepseek_harness/api.py:113` | 用同一个 Session 跑一道多轮题；整题重试创建新的 execution ID 和 session |
| SDK result.events 仅含根 session，notifications 包括已知后代 | `python/sdk/README.md` | 要保留子 agent 轨迹必须保存 notifications，不能只读 events |
| headless 每次运行一个新会话 | `packages/bundle/headless/README.md` | 适合单题冒烟；不能逐轮重启 headless 来模拟连续对话 |

以上 DSH 文件路径相对其源码根目录。

## 三种接法

| 方案 | 优势 | 需要补的能力 |
| --- | --- | --- |
| DSH → 现有中转 → 模型 | 最大程度复用 llm_calls、耗时统计和 HTML；接入其他 harness 的方式一致 | 归因、DeepSeek 参数透传验证；单靠中转无法测量本地工具执行耗时 |
| SDK notifications / 持久化日志 → importer → 报告 | 不改变模型链路；保留工具、父子 session 和压缩历史 | 新的 DSH 事件规范化器；语义请求不能冒充 provider 原始 HTTP 请求 |
| DSH 原生 OTLP Logs → collector → 转换/存储 | 适合服务化持续收集 | 当前不是标准 LLM span 导出；需要事件转 span/报告，处理丢失和去重；流式 chunk 不完整 |

推荐先做第一种，同时保存第二种的原始数据。无需第一版就部署长期 collector 或另建 dashboard。

## 最小接入步骤

1. 新增独立 `.env.dsh_<port>` 中转配置；端口、密钥、日志和 lane pool 独立。保留 DSH 原来的 provider、模型、工具组合和提示词。
2. 配置 `LLM_GATEWAY_THINKING_POLICY=passthrough`，避免当前中转的默认 thinking 策略改变 DSH 行为。它仅关闭本项目的策略改写，不保证 LiteLLM 本身逐字段透传。
3. 用 Python SDK 指定 `base_url=http://127.0.0.1:<port>/v1` 和本地中转密钥。显式选择相匹配的 runtime/Cordis composition；SDK 默认配置与产品 headless 的工具、persona、插件组合不同，不能直接当成同一评测条件。
4. 最小 runner 每个 worker 独占一个 lane：获取 lease，写入 run/case/execution 标识，再启动该题 DSH runtime。使用 `start_session(execution_id)`，按顺序 `session.run(turn)`。通过参数传子进程环境，避免并发修改全局环境。
5. 每题保存运行状态、根 session ID、notifications 和会话日志位置。等待本题所有子 agent、辅助请求与 runtime 退出后再归还 lane；超时无法证明停止时，沿用 blocked 状态防止下一题串入。
6. 用现有 `llmtrace report html --gateway <name> --out <dir> --run-id <run>` 生成模型交互报告。可先独立运行脚本，确认链路后再注册到统一 eval runner。

独占 lane 方案的并发单位是一个完整 case，题内主 agent、子 agent、压缩与标题调用都属于同一次执行。所有相关 provider 路由必须指向该 lane，否则部分请求会绕过采集。

原生 DeepSeek provider 不支持随意配置 X-Eval 请求头，所以不能照搬 OpenCode 的 headers 接法。它已有 session 请求头，但当前 `llm_gateway/callbacks/case_id.py` 并不识别它；仅换 base URL 可以产生记录，不代表归因完整。

## 正式版本的扩展

- `scripts/run_dsh_wire.py`：负责 SDK runtime、逐题多轮、lane lease、超时和原始事件保存。
- `eval_harness/src/eval_harness/adapters/dsh.py`：可选，接入统一 runner/profile/registry，返回现有 CaseRunResult。
- DSH 事件 importer：按 session/turn/step/attempt 归一化；保留 parentSession、tool call ID、重试、压缩与标题调用分类，复用现有报告可表达的字段，对工具耗时和父子关系扩展展示。
- 中转标签扩展：保留 DSH session ID 和 compaction 请求头；如改为多题共享网关，建立可靠的 `session_id → run_id/case_id/execution_id` 映射。子 session 必须通过祖先关系继承执行归属；未知或冲突关系标记未归因，不能靠请求时间猜测。
- 若需要精确关联每次 LLM 调用与原生 step，另加显式 call ID 的传递机制。session ID 只能关联会话，在并发子调用、标题、压缩和重试时不足以一一匹配。

数据身份建议：run 表示批次；case 表示题目；execution 表示一次整题执行；session 表示 DSH 会话；call/attempt 表示一次实际请求。根 session 可以直接采用 execution ID，但不能把所有子 session 改成同一个值。

## 必须验证的兼容点

1. **模型参数保真。** 默认中转配置使用 `drop_params: true` 且通配模型路由映射到固定 UPSTREAM_MODEL。为 DSH 检查 thinking、reasoning_effort、max_tokens、tools、reasoning_content、usage 和实际 model；多模型场景必须显式路由。观察模式应对不支持的字段显式报错或确认透传，不能默默删除。
2. **推理回传。** 当前 DSH serializer 仅在带 tool calls 的历史 assistant 消息中回传 reasoning_content；当前官方 Thinking Mode 文档要求携带 tools 的后续请求完整回传历史 reasoning，包括未产生 tool call 的轮次。这是本地实现与当前协议文档的差异，需按实际模型/端点做跨用户轮次回归，不能将潜在 400 一概归咎于 tracing。
3. **完整流式结束。** 验证首个非空 reasoning/text、tool 参数分片、尾部 usage、DONE，以及中途断流/取消，不能把已收到 HTTP 200 视为最终成功。
4. **归因隔离。** 两题并发不得串记录；同题重试换 execution；子 agent、压缩、标题调用明确归属，并且辅助调用可独立筛选。
5. **测量口径。** 网关 TTFT、DSH step/start 到首 chunk、到首个非空文本/推理的耗时不同；报告标注采集来源与口径，不混合统计。工具真实耗时从原生 tool/call → tool/result 计算。
6. **日志输入。** 原始持久化 importer 使用官方解码器；若为临时实验改为 compression:none + packChunks:false，应使用新的日志根目录。回放/恢复产生的合成闭合事件应标识为恢复结果。

建议验收集：单轮、同 session 两轮、一次真实工具往返、两题并发、整题重试、子 agent、压缩、流中断。先用本地合成 SSE 上游验证请求字段和错误状态，再做小规模真实模型对照。

## 原生 OTLP 配置方向

在包含 base bundle 的隔离运行配置中，显式同时设置：

```sh
DSH_TELEMETRY_MODE=FULL
DSH_TELEMETRY_OTLP_URL=http://127.0.0.1:4318/v1/logs
```

本地 collector 必须已支持 OTLP/HTTP Logs。单独设置 FULL 会使用 bundle 自带的默认远端地址，因此本地调试要同时指定目标。非空 DSH_TELEMETRY_DISABLED 会禁用这一路；SDK 自定义 composition 则须核对是否加载 telemetry 插件。

接收端按 `(session.id, event.seq)` 去重，用父 session 和 seed_length 连接 fork 历史。导出采用 best-effort 队列，不保证崩溃时零丢失；需完整原始数据时以持久化会话日志补齐。不能把该接口的 `/v1/logs` 直接替换为 `/v1/traces` 并期待得到 spans。

## 调研依据

- [DeepSeek 官方 Thinking Mode 文档](https://api-docs.deepseek.com/guides/thinking_mode/)
- [LiteLLM 官方 DeepSeek provider 文档](https://docs.litellm.ai/docs/providers/deepseek)
- [DeepSeek Harness 官方仓库](https://github.com/deepseek-ai/deepseek-harness)
- 当前仓库：`README.md`、`llm_gateway/README.md`、`llm_gateway/callbacks/case_id.py`、`llm_gateway/callbacks/trace_callback.py`、`llm_gateway/config.litellm.with_master.yaml`、`eval_harness/src/eval_harness/attribution_lane.py`。

实现完成的判定应是小规模端到端测试通过、字段与归因核验通过、HTML 可用；本次完成的是可行性和实现路径调研。
