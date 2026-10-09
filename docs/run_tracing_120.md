# 120 题完整 Trace 演示

入口：`bash scripts/run_tracing_120.sh`。沿用 `docs/triple_0922_shared.md` 的共享
0922 原始题集：A01–F20，每套 harness 120 题；Pi 使用相同问句、PI_ 前缀隔离账本。
四个 harness 是 Claude Code、Insurance QA、Pi、OpenCode，不做答案正确性评分。

## 运行

```bash
cd /Users/xiaozijian/WorkSpace/package/llmtracing

# 只检查本地文件、CLI、120 题一致性、模型别名和中转配置；不调用模型
bash scripts/run_tracing_120.sh --check

# 先看 3 题 × 4 harness 的完整效果（真实模型请求）
# --restart-gateways 让上一轮 callback / httpx transport 修复生效；请在没有其他跑批时使用
bash scripts/run_tracing_120.sh --smoke --restart-gateways

# 完整流程：smoke 成功才跑 120 题 × 4 harness
bash scripts/run_tracing_120.sh --all --restart-gateways

# 可选：明确输出目录、Claude/Pi/OpenCode 并发度
bash scripts/run_tracing_120.sh --all --concurrency 4 --out eval_runs/my_tracing120

# 中断/失败后续跑（使用同一批次路径，不加 --all 或 --smoke）
bash scripts/run_tracing_120.sh --resume eval_runs/my_tracing120
```

`--all` 包含 12 个 smoke case execution 和 480 个全量 case execution；每题可能多轮、
多次调用模型，preflight 也会发送小型探测请求。实际调用现有中转背后的模型，使用对应额度。

## 前端和输出

脚本打印本批目录，并自动用默认浏览器打开该目录的 `index.html`（`--no-open` 可关闭）。
入口每 15 秒刷新；每套 harness 收尾后，出现其现有 `llm_trace.html` 链接。报告中可查看
每题业务输入输出、模型请求回复、tools、耗时、归属和重试历史。它不是 token 级实时流 UI。

```text
eval_runs/tracing120_<时间>/
  index.html                         # 统一前端入口
  launch.json                        # 模式、并发、题集 hash、中转名称；不含密钥
  claude.json / pi.json               # 本批冻结配置
  triple_datasetA_smoke/              # A01,C20,F13
    manifest.json / console.log
    claude/llm_trace.html
    insurance/llm_trace.html
    pi/llm_trace.html
    opencode/llm_trace.html           # 直接从中转导出；CLI 原始轨迹在 opencode/cli/
    four-harness-status.json         # 四套全部完成才是 success
  triple_datasetA_full/               # 120 题，结构同上
  qa-<端口>.log                       # 仅本次启动的 QA 实例日志
```

Claude → Insurance → Pi → OpenCode 顺序执行；Claude/Pi/OpenCode 各自默认并发 4 题，Insurance 每个
独占 lane 一个 worker（当前配置两条 lane）。思考默认由 relay 关闭，Pi 另设 thinking=off；
Claude/Pi 使用每案隔离 cwd 和用户配置，不改变个人全局配置。OpenCode 使用每次执行独立的 XDG 配置、数据及会话目录；题内延续同一个 session，跨题新建。

## 依赖与失败处理

- 使用 `llm_gateway/.venv/bin/python`；可用 `LLMTRACE_PYTHON` 指定已有解释器。
- 主中转沿用 `run/active_env_file` 指定的配置；Insurance 使用 `insurance` lane pool；
  OpenCode 使用 `.env.opencode_4003` 指定的专属中转、CLI、题库、MCP 和提示词。
  `--restart-gateways` 会重启这些中转；不带该参数会复用健康中转，旧进程可能未加载最新修复。
- Insurance 使用既有 `insurance-qa-agent-eval` checkout 和 `.feval_venv`。
  健康实例仅在保存的元信息确认 checkout 与模型中转对应时复用。缺少可信元信息时退出，
  不擅自结束现有业务进程。未运行的实例由脚本启动；结束时只停本次启动的 QA，中转保持运行。
- preflight 保留磁盘、上游、MCP 和工具可用性检查；MCP 同步采用 `check`，
  Insurance 启动禁止自动 align，不自动更新其他仓库。依赖漂移应按 preflight 提示处理后续跑。
- smoke 失败停止，不进入全量；运行目录、失败轨迹和报告保留。目录已存在时拒绝覆盖，
  续跑必须用 `--resume`，跳过成功题。题集或运行配置变化时要求新建批次。
- `--check` 通过只表示本地配置可用；真实服务健康和工具可用性在实际运行的 preflight 判断。

依据：`docs/triple_0922_shared.md`、`docs/dual_eval_production.md`、现有配置隔离 profile，
以及 `eval_runs/par4_nothink_20260930_194500/*/run_meta.json` 的历史记录。

## OpenCode 与原要求对齐

2026-09-28 至 09-29 的历史要求包括：优先直接运行 harness，不依赖 adapter；
按 run/case/execution 三个 ID 避免串题、跨批覆盖；批次内默认最新尝试并可看历史；
保留多轮展示；首帧按首个 text/thinking 字符计时，工具帧不算。

本入口调用现有 `scripts/run_opencode_wire.py`，不把 OpenCode 注册为 adapter。
该脚本把身份信息放在模型 provider 的 headers，原始 CLI 输出留在 `opencode/cli/`；
公开 `llmtrace query` 命令从 4003 中转导出同一 run 的 HTML，并保留历史尝试。
每个 smoke/full 阶段有独立且可续跑的 OpenCode run ID，保存在 `launch.json`。

OpenCode 收尾会额外检查：选中题目的最新 CLI 尝试成功、每个 execution 有完整模型
调用且归属正确、报告多轮数与原始题目一致、每题存在 text/thinking 首字计时。
未通过写入 `opencode/validation.json`，四套总状态不会误报成功；下次续跑重新执行
采集/多轮/首字检查失败的题，旧 attempt 保留。该报告是 wire-only：业务最终答案和
用户前端真实可见时间仍标注为中转观测推断，不把它冒充 runner 直接测量。

原三套脚本创建的批次不在原目录偷偷扩成四套；新版要求新建批次，防止比较口径混淆。
