# Tracing 可信度评测

本目录落实用户确认的追踪可信度用例与评分方法。完整基线覆盖 T01–T15：
13 类通过、2 类失败，共 247 项检查。结果位于
`var/build_eval_tracing_20261001/full-baseline/report.html`，诊断与边界见同目录 `findings.md`。
先前五例 pilot 单独保留在 `grader-review`，不能与完整基线重复计数。

2026-10-02 修复后结果另存于
`var/build_eval_tracing_20261001/fix-validation/full/report.html`：15/15 类自动检查
通过，246/246 项检查通过。原始基线保留。浏览器策略阻止本地 file URL，
本轮 T10 实际页面可见性复核未执行；使用显式 `--skip-ui-review` 记录这一缺口，
不能把它与旧基线含人工页面复核的 247 项直接等同。配置与项目修复说明见该目录
`findings.md`。第三方库源码未改，运行中的既有网关未重启。

## 路径与边界

真实路径：合成 HTTP 客户端 → 本项目启动脚本启动的 LiteLLM → 本地受控上游
→ 本项目 callback 日志 → `llmtrace query` 与 `llmtrace report html`。

启动脚本、custom_callbacks 与 callbacks 源码复制到输出目录内的隔离网关目录，
使用现有 `llm_gateway/.venv`。不读取真实 `.env`、不占用既有网关端口，不需要
供应商密钥。仅绑定 `127.0.0.1` 动态端口；服务在 finally 中关闭。测试凭据是
固定假值。`_runtime/client-requests.json` 和 `upstream.jsonl` 保存独立输入/响应证据。

控制上游是被测系统之外的依赖；没有替换 tracing 的 HTTP 入口或内部归属函数。
这验证本地协议集成，不验证真实供应商行为。模型 usage=11/7 是合成字段保真样例，
不是实际账单或成本。

## 复现

完整基线复用已审阅 pilot，并检查其已记录的产品源文件 hash；它是当前版本的
组合基线。产品源码变更后应重跑 pilot、重评并重新检查 UI，不能直接复用旧得分。
当前 hash 校验只覆盖 pilot manifest 列出的文件，不是完整依赖锁或 CI 放行机制。

```sh
llm_gateway/.venv/bin/python eval_harness/evals/tracing/run_baseline.py \
  --pilot var/build_eval_tracing_20261001/grader-review \
  --out var/build_eval_tracing_20261001/my-full-baseline

llm_gateway/.venv/bin/python eval_harness/evals/tracing/confirm_stream_boundary.py \
  --out var/build_eval_tracing_20261001/my-full-baseline/_stream-boundary-control

node "$HOME/.codex/skills/claude-api/shared/evals/report/build-report-lite.mjs" \
  "$PWD/var/build_eval_tracing_20261001/my-full-baseline"
```

T02–T09/T11 走真实 HTTP；T04 包含三次并发调度，共 36 次调用。
T12/T13 是公开 CLI 日志重放；T14 的 runner+wire/runner-only 是公开 CLI 产物导入，
不代表真实业务 runner 完整链路已验证。T01/T10/T15 和 T04/T14 的基础子例复用冻结 pilot。
完整基线逐例把基础设施异常记录为 `status:error`；必须核对 `_manifest.json` 的
`infra_errors`，不能只依赖进程退出码，也不能把未评分场景算作通过。

单独重建 pilot：

从仓库根目录运行，`--out` 必须是尚不存在的目录，以免覆盖实验记录：

```sh
llm_gateway/.venv/bin/python eval_harness/evals/tracing/run_pilot.py \
  --out var/build_eval_tracing_20261001/my-new-pilot

llm_gateway/.venv/bin/python eval_harness/evals/tracing/calibrate_grader.py \
  --out var/build_eval_tracing_20261001/my-new-pilot/_grader-calibration.json

node "$HOME/.codex/skills/claude-api/shared/evals/report/build-report-lite.mjs" \
  "$PWD/var/build_eval_tracing_20261001/my-new-pilot"
```

依赖沿用已有 LiteLLM 环境，具体已测版本记录于输出 `_manifest.json`。
需允许本机回环端口监听。生成报告另需 Node 和已安装的 Anthropic claude-api skill。

## 评分

预期来自固定合成输入与独立上游规则；评分器读取 CLI/HTML 对外产物，
不调用产品的归一化函数生成期望。逐项保存 expected、actual、passed。
六个维度：归属、完整性、内容、状态、来源、脱敏；不适用维度不记分。
所有适用项通过时 `all_checks_pass=1`，否则为 0。

并发样例同时核对总数和每个 execution 的内容，不能靠总数对了掩盖串题。
上游故意返回 HTTP 400 是被测事件，其正确记录是通过；启动失败、测试服务故障
写 `_pilot-error.json`，不伪装成产品得分。T10 另存请求尚未释放时的 pending 快照。

`calibrate_grader.py` 提供一份正确产物与十三份故意损坏产物，检查：丢调用、串执行、
错误变成功、伪造业务成功、清空答复、漏出假密钥、usage 为零、模型标识错误、
什么也没采集，以及错误状态码、错误原因丢失、失败终止时间丢失、页面未显示失败。
校准结果独立保存，不混入产品通过率；这些是评分函数的校准，不是整条 runner 的覆盖证明。

修订评分可以复用原始证据，不必重新请求模型：

```sh
llm_gateway/.venv/bin/python eval_harness/evals/tracing/regrade_pilot.py \
  --flow var/build_eval_tracing_20261001/grader-review
```

此命令要求 `_ui-review.json`：实际浏览器检查结论、HTML SHA256、call_id、
截图与 accessibility 文件路径。HTML 变化或证据文件缺失会拒绝复用；页面判分是
人工浏览器审阅，不宣称全自动。首次重评将原判分保存在 `_grading_history/v1`。
`run_pilot.py` 自动检查错误原因/状态码/终止时间；页面显示检查只在提供实际 UI
审阅时加入。完整基线复用了这份页面证据，另实际检查了 T05 历史开关、T06 批次
筛选和 T14 业务答案来源标签；记录见 `_ui-T*.txt`。

官方 lite report 只展示主指标和 trace 链接。每条 trace 是 `{role, content}` 列表，
包含预期、HTTP 往返、查询记录与逐项判分。产品自己的实际 HTML 在
`baseline/artifacts/<Txx>/direct-report/llm_trace.html`。

产品是否通过以 results.jsonl 的 grade 为准。用户已确认评分口径；原始基线未修改
产品代码，后续修复及验证另存 fix-validation，没有运行自动 hillclimb。脚本尚未具备完整 unattended hillclimb
所需的变体/重复次数参数、resume、依赖完整性审批门及全 runner 的 oracle/null
校准，不应直接作为无人值守发布门禁。
