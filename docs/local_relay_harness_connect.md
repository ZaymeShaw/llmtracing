# 本地中转接入指南（Phase 3）

> 配套：`docs/local_relay_trace_scheme.md`  
> 原则：**分析不绑 adapter**。只要流量走本地中转（默认 LiteLLM `:4001`），并带上 `eval_case_id`，就能出统一 `llm_calls` / wire-only HTML。


## 生产 Dual Eval（Claude + Insurance）

**交付入口**：`docs/dual_eval_production.md`  
命令：`PYTHONPATH=src python3 -m eval_harness.suite --bundle … --cases A01,A02`
（或 `scripts/run_dual_dataset_a.sh`）

一次 stamp，两侧落在 `eval_runs/dual_datasetA_<stamp>/{claude,insurance}/`，两侧 runner **自动**发出 `llm_trace.html`。  
**不要**把 ad-hoc `--from-gateway-log` 手补 HTML、或顶层孤儿 `claude_datasetA_*` / `insurance_datasetA_*` 当作生产交付。


## 1. 中转地址

| 服务 | 默认 | 用途 |
|------|------|------|
| LiteLLM | `http://127.0.0.1:4001` | 主中转（OpenAI Chat / Responses / Anthropic Messages） |
| thin | `http://127.0.0.1:4000` | raw 边车 / Anthropic 兜底 |

鉴权：LiteLLM master key（见 `llm_gateway/.env` 的 `LITELLM_MASTER_KEY`）。

## 2. `eval_case_id` 怎么打（三种协议通用）

双 ID（`case_id` + `execution_id`）完整约定见 [adapter-eval-contract.md](adapter-eval-contract.md)。

优先级（中转侧）：

1. Header：`X-Eval-Case-Id: A01`
2. Body 字段：`eval_case_id`（Responses 可双写 `metadata.eval_case_id`）
3. 文本兜底：`<!--eval_case_id:A01-->`

缺失 → orphan，**不会**用文件/进程态猜归属。

代码辅助：`eval_harness.relay_inject`  
（`openai_compatible_kwargs` / `apply_openai_compatible_case_id` / `anthropic_cli_env`）

## 3. 各 harness 怎么接

### 3.1 Claude Code（已有批跑 adapter）

- Profile：`eval_harness/configs/profiles/claude.yaml`
- Adapter 已注入：`ANTHROPIC_CUSTOM_HEADERS` + `CLAUDE_CODE_EXTRA_BODY`，文本标记兜底
- `ANTHROPIC_BASE_URL` 指向 `:4001`（`sync_claude_settings.py` / `workspaces/claude/.claude/settings.local.json`）

### 3.2 自研 OpenAI 兼容 Agent（如 insurance_qa_agent / Agno）

**mock_system 薄 adapter（不改保险仓库，用于实测）：**

```bash
cd /Users/xiaozijian/WorkSpace/package/mock_system/eval_harness
PYTHONPATH=src \
  /Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent/.feval_venv/bin/python \
  -m eval_harness.adapters.insurance --case-id INS_SMOKE_001
```

Profile：`configs/profiles/insurance.yaml`

Live batch（自动 HTML，等同 Claude `eval_harness.run` 收尾）：`PYTHONPATH=src python3 -m eval_harness.adapters.insurance --live-batch --cases A01,A02 --run-id insurance_datasetA_<stamp>` → `eval_runs/<run_id>/llm_trace.html`。 **双侧生产对比请用** `eval_harness.suite`（见 `docs/dual_eval_production.md`），不要再手搓两套顶层 orphan run_id。

业务侧接入（可选，自行改 conf）也能分析：

1. 把 `models_*_args.yaml`（或等价 conf）的 `base_url` 改成  
   `http://127.0.0.1:4001/v1`（注意是否已含 `/v1`）
2. `api_key` 用本地 master key
3. **每个 case 开跑前**给模型打上 case_id（同一 case 含降级链共用一个 id）：

```python
from eval_harness.relay_inject import apply_openai_compatible_case_id, openai_compatible_kwargs

# 构造时：
model = OpenAIChat(id=..., base_url=..., api_key=..., **openai_compatible_kwargs(case_id))

# 或实例已建好、换 case 时：
apply_openai_compatible_case_id(model, case_id)
# fallback 链上的每个 model 都要打同一 case_id
```

4. **生产交付请走 dual / live-batch 自动 HTML**（见 `docs/dual_eval_production.md`）。  
   下面的 `--from-gateway-log` **仅限 debug / 切片排查**，不是产品路径：

```bash
cd /Users/xiaozijian/WorkSpace/package/mock_system/eval_harness
PYTHONPATH=src python3 -m eval_harness.llm_trace_html \
  --from-gateway-log ../llm_gateway/logs/llm_calls.jsonl \
  --out-run-dir ../eval_runs/wire_insurance_demo \
  --cases bs-001,bs-002
```

> 大账本请先按时间/PROBE 切片再喂 `--from-gateway-log`，避免整文件进内存。  
> **不要**用这种方式「补 HTML」冒充 dual / live-batch 产品。

### 3.3 Pi agent / 其它 CLI

当前仓库 `pi-agent/` 为空，**暂不加批跑 adapter**。待有可跑 CLI 时：

1. 先验证：只改 upstream `base_url` + 按案注入 header/body，能否在 `llm_calls` 看到非空 `case_id`
2. 需要代跑 Bundle 时，再仿 `adapters/claude.py` 做薄 adapter（只负责起停/喂题/注入，不定义分析 schema）

## 4. 成功标准（Phase 3）

- [x] 文档写清「base_url + case_id → 可分析」
- [x] OpenAI 兼容 / Claude CLI 注入辅助可复用
- [ ] 第二个批跑 adapter：等具体 harness（pi 等）就绪再开

## 5. 不要做的事

- 不要为分析再写一套 per-harness Trace schema
- 不要恢复「当前 case 文件」回退
- 不要假设没过中转的流量能进账本
