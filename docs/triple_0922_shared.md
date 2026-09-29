# 三方评测（Claude + Insurance + Pi）— shared 0922

## 共享题集

- **权威源**：`eval_harness/bundles/120_prompt_only_0922.jsonl`（PDF 重建，0 mismatch）
- **三方共享副本**：`eval_harness/bundles/120_prompt_only_0922_shared.jsonl`（与权威字节一致；原始问句；无 canned suffix；无 ## D类泄漏）
- **Pi 账本隔离副本**：`eval_harness/bundles/120_prompt_only_0922_shared_pi.jsonl`（`PI_A01`…`PI_F20`）

`configs/runs/claude.yaml` / 默认 dual 仍指向 v1，**故意不改**（保护在飞 Claude 跑）。

## 怎么跑

```bash
cd /Users/xiaozijian/WorkSpace/package/llmtracing
# 推荐包装脚本
./scripts/run_triple_0922.sh A01,C20

# 或直接：
cd eval_harness
PYTHONPATH=src python3 -m eval_harness.suite \
  --bundle bundles/120_prompt_only_0922_shared.jsonl \
  --claude-config configs/experiments/claude_0922.yaml \
  --pi-config configs/experiments/pi_0922_shared.yaml \
  --agents claude,insurance,pi \
  --cases A01,C20
```

- 默认 `--agents claude,insurance` → 仍写 `eval_runs/dual_datasetA_<stamp>/`（行为不变）
- 含 `pi` → `eval_runs/triple_datasetA_<stamp>/{claude,insurance,pi}/`
- Pi 侧 case 自动 remap：`A01` → `PI_A01`（`--pi-remap-prefix`，默认 `PI_`）
- Pi：`cwd=workspaces/pi`，`ITOOLS_AGENT_ID=pi`，local-relay，`thinking=off`（profile 现状，未改 models.json）

## 仅 Pi / 仅 Dual 0922

```bash
# Pi only smoke
PYTHONPATH=src python3 -m eval_harness.suite \
  --agents pi --pi-config configs/experiments/pi_0922_shared.yaml \
  --bundle bundles/120_prompt_only_0922_shared.jsonl \
  --cases A01 --stamp my_pi_smoke

# Claude+Insurance on 0922（不碰默认 configs/runs/claude.yaml）
PYTHONPATH=src python3 -m eval_harness.suite \
  --bundle bundles/120_prompt_only_0922_shared.jsonl \
  --claude-config configs/experiments/claude_0922.yaml \
  --agents claude,insurance --cases A01,A02
```
