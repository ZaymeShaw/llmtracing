# 生产 Dual Eval 入口

一份 stamp，两侧产物同落 `eval_runs/dual_datasetA_<stamp>/`，两侧 runner **各自**自动发出 `llm_trace.html`。  
**失败尝试不是产品**（保留目录仅供 debug）。

## 为什么要一个入口

- 以前的 vibe（手搓 `claude_datasetA_*` / `insurance_datasetA_*`、事后 `--from-gateway-log` 补 HTML）会产生孤儿目录与「像产品但其实是补丁」的结果。
- 生产路径要求：**一次命令、同一 stamp、双侧齐全、HTML 由既有 runner 收尾发出**。
- 入口：`eval_harness.suite`（薄包装 `scripts/run_dual_dataset_a.sh`）。

## 前置条件

| 依赖 | 检查 | 说明 |
|------|------|------|
| Claude LiteLLM | `GET http://127.0.0.1:4001/v1/models` + Claude key | 不健康时 dual_run 会调 `llm_gateway/start_litellm.sh` 再检一次 |
| Insurance LiteLLM | `GET http://127.0.0.1:4002/v1/models` + `INSURANCE_LITELLM_MASTER_KEY` | 不健康时 dual_run 会调 `llm_gateway/start_insurance_litellm.sh` 再检一次 |
| Insurance QA | `GET http://127.0.0.1:18063/health` | 18063 是评测专用实例；不自动拉起，挂了则 fail-fast |
| 本地代理 | `trust_env=False` / `NO_PROXY=127.0.0.1,localhost` | 避免系统代理劫持回环 |
| Claude attribution | `workspaces/claude/.claude/settings.local.json` → `CLAUDE_CODE_ATTRIBUTION_HEADER=1` | 3-block system 需要；dual_run 只读检查并告警 |

**不修改** `insurance-qa-agent` 仓库。

## 命令

```bash
cd /Users/xiaozijian/WorkSpace/package/llmtracing/eval_harness
PYTHONPATH=src python3 -m eval_harness.suite \
  --bundle bundles/120_prompt_only_v1.jsonl \
  --cases A01,A02
```

或：

```bash
/Users/xiaozijian/WorkSpace/package/llmtracing/scripts/run_dual_dataset_a.sh A01,A02
```

行为摘要：

1. Preflight（LiteLLM / Insurance）
2. 创建 `eval_runs/dual_datasetA_<stamp>/`
3. **Claude**：既有 `eval_harness.run` + `configs/profiles/claude.yaml`，强制 `eval_runs_dir=<dual_root>`、`run_id=claude` → `…/claude/`（不会落到 `workspaces/claude/eval_runs`）
4. **Insurance**：既有 `run_live_batch`，`eval_runs_dir=<dual_root>`、`run_id=insurance` → `…/insurance/`
5. 校验两侧 `llm_trace.html` 存在且非空壳；否则 `manifest.status=failed`、进程非 0
6. 写 `manifest.json`；中途失败仍写 failed manifest，**不删目录**，并明确打印「NOT A SUCCESSFUL PRODUCT」

## 输出布局

```
eval_runs/dual_datasetA_<YYYYMMDD_HHMMSS>/
  manifest.json          # stamp / cases / status / 两侧 HTML 路径
  claude/                # 完整 Claude harness run_dir（内含 llm_trace.html）
  insurance/             # 完整 Insurance live-batch run_dir（内含 llm_trace.html）
  console.log
```

**产品只交 `dual_datasetA_*`。** 同 stamp 下不应再出现顶层 `claude_datasetA_*` / `insurance_datasetA_*` 孤儿。

`manifest.json` 关键字段：`stamp`、`cases`、`claude_html`、`insurance_html`、`claude_ok`、`insurance_ok`、`started` / `finished`、`status`、`notes`。

## 明确不支持（不是产品路径）

- 临时 `curl` 打一枪当 eval
- 手动 `python -m eval_harness.llm_trace_html --from-gateway-log …` **补** HTML 当交付物
- 把 failed / probe / orphan 目录当成成功产品
- 为分析去改 insurance 仓库或另写第三套 HTML builder

## Insurance `case_id` 归属

Live QA（`/v1/chat`）**不会**注入 `X-Eval-Case-Id`。评测器在每案开始时登记唯一 `execution_id`，网关按 `insurance_1` lane 固定归属，ingest 只接收该 execution ID 的调用，不使用时间窗回退。Claude 侧仍使用显式 header。Insurance 使用评测专用实例 18063、专用网关 4002 和专用 key；普通开发或 verifier 请求必须留在 18062。若外部请求也打入 18063，在不修改被测系统透传请求 ID 的前提下仍无法区分，相关边界见 `docs/trace_attribution_and_case_io_plan.md` §13.3。

## 相关文档

- 中转接入总览：`docs/local_relay_harness_connect.md`（生产 dual → 本文）
- Trace 方案：`docs/local_relay_trace_scheme.md`
