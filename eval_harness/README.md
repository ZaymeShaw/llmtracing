# Eval Harness

评测框架通过统一入口分发到各个 harness adapter，采集调用轨迹、耗时和最终文本，生成 Excel 与 HTML。框架不执行答案评分。

## 组件

- `src/eval_harness/adapters/`：Claude、Pi、Insurance 的适配实现，以及公共类型、注册表。
- `configs/profiles/`：CLI 工作目录、二进制、权限或 HTTP 服务连接信息。
- `configs/runs/`：三个默认运行入口配置，选择 profile 与本地 bundle。
- `configs/experiments/`：历史数据集版本与双路/三路实验组合。
- `configs/legacy/`：旧的扁平配置，仅供参考，不作为当前 runner 输入。
- `schemas/`：Bundle、Profile、Trace 的格式定义。
- `data/`、`bundles/`：本地题库与导入产物，已被 Git 忽略。

Claude 与 Pi 返回 `adapters.base.CaseRunResult`，由通用 runner 归一化轨迹并导出报告。Insurance 注册原生 `run_batch`，保留 HTTP 会话、逐题日志归因和恢复运行逻辑，也通过 `eval_harness.run` 调用。

## CLI

从仓库根目录运行：

```bash
python3 -m pip install -r eval_harness/requirements.txt
export PYTHONPATH="$PWD/eval_harness/src"

python3 -m eval_harness.run --config eval_harness/configs/runs/claude.yaml --cases A01,E01
python3 -m eval_harness.run --config eval_harness/configs/runs/pi.yaml --cases A01
python3 -m eval_harness.run --config eval_harness/configs/runs/insurance.yaml --cases A01

# 仅读取本地 Bundle，不启动 agent 或调用服务
python3 -m eval_harness.run --config eval_harness/configs/runs/claude.yaml --all --dry-parse

# 将自己的 Markdown 题库导入本地 Bundle
python3 -m eval_harness.import_bundle --md /path/to/questions.md --out eval_harness/bundles/my_cases.jsonl --bundle-id my_cases
```

每个 run config 中的路径相对于它自身；profile 中工作目录、输出目录相对于 profile。命令行 `--eval-runs-dir` 指定的相对路径仍相对于 agent 工作目录，建议传绝对路径。CLI 的提示词和 MCP 路径相对于 workspace。

`--run-id` 指定输出目录名；`--resume` 跳过成功题目并恢复运行。`--rebuild-excel` 适用于 Claude/Pi，Insurance 通过原生批处理恢复并重建自己的报告。

默认报告写入仓库根目录的 `eval_runs/<run_id>/`，包括逐题原始事件、规范化 trace、Excel 和 HTML。Claude/Pi 与 Insurance 的原始事件格式不同，分别由对应 adapter 处理。

## 多路评测与扩展

`python3 -m eval_harness.suite --help` 查看现有 Claude/Insurance/Pi 对比运行的参数。`scripts/run_dual_dataset_a.sh` 和 `scripts/run_triple_0922.sh` 保留已有实验预设；原 `eval_harness.dual_run` 命令兼容转发。

添加新框架的接口与配置示例见 [新增 harness](../docs/adding_harness.md)。评测中转双 ID（case_id + execution_id）约定见 [adapter-eval-contract](../docs/adapter-eval-contract.md)。
