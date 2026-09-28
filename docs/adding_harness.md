# 新增 harness

统一入口为 `python -m eval_harness.run --config <run-config> --cases A01`。

评测中转双 ID 约定见 [adapter-eval-contract.md](adapter-eval-contract.md)。Runner 负责加载 Bundle、选择题目和注册表分发；Adapter 负责具体 CLI 或服务协议。

## 新增 CLI adapter

1. 在 `eval_harness/src/eval_harness/adapters/<name>.py` 实现 `run_case(**kwargs)`，返回 `adapters.base.CaseRunResult`。每轮结果使用同一文件中的 `TurnResult`。
2. 在 `adapters/registry.py` 添加一个延迟导入 wrapper，并调用 `register_adapter("<name>", run_case=wrapper)`。
3. 添加 `configs/profiles/<name>.yaml` 和 `configs/runs/<name>.yaml`。
4. 需要隔离目录时添加 `workspaces/<name>/agent.md`，设置 profile 的 `project_cwd`。

Runner 提供 `case_id`、`turns`、`case_dir`、`project_cwd`、`harness_bin`、`agent_md`、`timeout_sec` 等公共参数，并将 profile 的 `adapter` 字段传给 wrapper。Wrapper 筛选实现所需的参数；额外选项不用加入 runner 的硬编码列表。

示例 profile（位于 `eval_harness/configs/profiles/`）：

```yaml
schema_version: "1.0"
profile_id: example_cli
harness: example_cli
project_cwd: ../../../workspaces/example
bin: example-cli
agent_md: agent.md
output:
  eval_runs_dir: ../../../eval_runs
adapter:
  model: example-model
```

对应 run config（位于 `configs/runs/`）：

```yaml
profile_path: ../profiles/example.yaml
bundle_path: ../../bundles/my_cases.jsonl
```

通用 Trace 归一化目前接收 Claude 风格事件；Pi 在自身 adapter 中完成事件转换。新 adapter 应返回可归一化的 `TurnResult.raw_events`，或同步扩展事件映射。公共结果类型不依赖任何特定 harness。

## 新增 HTTP / 原生批处理 adapter

像 Insurance 一样注册 `register_adapter("<name>", run_batch=wrapper)`，无需 `project_cwd` 或 `bin`。Runner 提供 `bundle_case_ids`、`bundle_path`、`run_id`、`eval_runs_dir`、`resume`，以及 profile 的 `adapter` 选项。

批处理实现负责自己的会话、恢复和报告，返回包含 `n_ok`、`n_cases`、`run_dir`、`html_path`、`html_error` 的字典。CLI 根据结果设置退出码；失败批处理返回非零。`--rebuild-excel`、`--pack-zip` 当前仅适用于逐题 CLI adapter。

`gateway_log` 和 `attribution_config` 两个已有路径选项会按 profile 所在目录解析；新增路径选项由 adapter 明确规定其解析方式。

## 多路对比

`eval_harness.suite` 是现有 Claude/Insurance/Pi 的服务预检和多路运行预设。注册新 adapter 后可以直接通过通用 `run` 入口评测；若要加入 suite，还需定义对应的服务准备、题目 ID 映射和结果汇总。

## 迁移对照

| 原位置 | 当前位置 |
|---|---|
| `claude_adapter.py` | `adapters/claude.py` |
| `pi_adapter.py` | `adapters/pi.py` |
| `insurance_qa_adapter.py` | `adapters/insurance.py` |
| `adapters.py` | `adapters/registry.py` + `adapters/__init__.py` |
| `mock_run/` | `workspaces/claude/` |
| `pi_run/` | `workspaces/pi/` |
| `eval_harness/config.yaml` | `eval_harness/configs/runs/claude.yaml` |
| `eval_harness/config_pi.yaml` | `eval_harness/configs/runs/pi.yaml` |
| `eval_harness/profiles/` | `eval_harness/configs/profiles/` |
| `INSTALL_ON_MAC.sh` | `scripts/install_on_mac.sh` |
| `sync_mock_run_settings.py` | `sync_claude_settings.py` |

直接调用保险 adapter 的命令改为 `python -m eval_harness.adapters.insurance`。旧多路入口 `eval_harness.dual_run` 保留为转发入口。已归档的运行轨迹、日志和备份保留原始路径记录，不批量改写。
