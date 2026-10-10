# 正常落盘减量实验（2026-10-09）

## 已完成的结果

5 种 harness、10 个既有批次、503 份逐题调用文件、3,243 次调用。完整目录的未压缩逻辑体积从 **2,368.59 MiB 降到 1,725.28 MiB**，净减少 **643.31 MiB（27.16%）**，包括所有恢复索引开销。

| 实验 | 净节省 MiB | 完整目录降幅 |
| --- | ---: | ---: |
| 仅减少过程副本，调用 JSONL 原格式保留 | 225.87 | 9.54% |
| 仅减少同调用字段副本 | 417.44 | 17.62% |
| 两者合并 | 643.31 | 27.16% |

三组方案重建的 30 份 HTML 全部与相应已有报告的 SHA256 一致；1,172 份被替代文件全部可恢复成原始字节；Excel 调用数据行也全部一致。真实数据中的 5 条失败调用保留，10 项边界测试通过。实验前后 5,658 个源文件的大小与 SHA256 均未变。逐批大小、全部校验和及适用限制见 `var/storage-experiment-20261009/report.md` 和 `full/run-*.json`。

这验证了正常落盘确有可无损消除的冗余。当前只是完成实验，尚未替换正式存储流程。

## 实验边界

用户要求：请求、回复、工具数据、路径和真实重复发送的历史消息保持完整；HTML 不能改变；不采用限额、截断或抽样。

本实验只读取既有真实批次，在临时目录实际写出另一种存储表示，并用当前报告生成器重建 HTML。正式采集、网关日志、既有运行目录及 HTML 均不修改。未重新调用模型或发送客户数据。

比较三组：

1. **仅减少过程副本**：合并 `stream.jsonl` 从逐轮 stream 原样拼回；`events.jsonl` 从 trace.events 恢复；Pi mapped stream 从原始流恢复。每个文件必须先通过原始字节 SHA256 比较，否则保留。
2. **仅减少同调用的字段副本**：仅检查四个固定位置：`request.messages → request.client_request.messages`、`request.tools → request.optional_params.tools`、`response.messages → request.messages`、`response.tools → request.tools`。原始 JSON 文本片段完全相同才保存为引用。实际 client_request 和 response.response 始终保留。读取时恢复原来的字段位置、顺序、空格、数字写法、Unicode 和换行，核对原文件 SHA256。
3. **两者合并**。

没有按文本相似程度合并数据，没有跨调用共享历史消息，没有改变 HTML 内嵌格式。原来的 ZIP、HTML、Excel、session transcript、失败尝试、export.log 及其他未选中文件全部计作保留。先前审计发现的 export.log 正文重复没有计入本次收益，因为不能仅凭正文相同就省略日志内的其他证据。

## 质量判据

- 每一份被替代的原始文件，都要从实际写出的文件与重新加载的 manifest 恢复，字节完全一致。
- 各实验组通过隔离的读取适配器调用现有 HTML 生成函数，输出 SHA256 必须与正常读取的基线相同；另外核对当前基线与磁盘已有 HTML 是否相同。
- 调用数、逐题调用文件数、失败调用数，以及现有 Excel 调用数据行的序列化 SHA256 必须相同。没有把 XLSX 容器本身的 ZIP 时间戳当成业务数据。
- 实验前后对源目录全部文件复核 SHA256，确认原文件未变。
- 独立边界测试覆盖不相等字段、布尔/数字差异、键顺序、null/缺失、失败和重试、Unicode/真实路径、类似引用标记的真实数据、空行/CRLF、损坏引用和内容、兼容旧文件读取。

## 测量口径与限制

大小是**正常运行目录未压缩文件的逻辑字节数**，包含新格式及 manifest 的实际开销，不是 ZIP 的压缩率。实验为避免制造大副本，对保持不变的文件使用临时符号链接，但统计时计入它们的完整原文件大小；不能把符号链接大小当成节省。临时产物测试后清理，只保留小型证据报告。本轮没有声称已释放生产目录或 APFS 的实际磁盘块。

隔离读取适配器只在实验进程中生效，覆盖当前报告与 Excel 调用数据读取。它不是正式存储格式，也没有覆盖第三方直接打开 JSONL、所有查询/导出入口、实时追加、并发采集、轮转、崩溃恢复及迁移。Pi 映射恢复还依赖当前 mapper，正式启用前需要稳定的版本策略。因此实验能证明这批数据可无损减量、现有展示不变；不能把离线通过等同于已经完成全生产链路改造。

## 复现

从仓库根目录执行：

```sh
PYTHONPATH=eval_harness/src:scripts llm_gateway/.venv/bin/python -m unittest scripts/test_experiment_storage_lossless.py -v

PYTHONPATH=eval_harness/src llm_gateway/.venv/bin/python scripts/experiment_storage_lossless.py \
  --run eval_runs/dsh_insurance_mcp_ready_20261009 \
  --run eval_runs/comparison_20261008/results/new-run/pi/triple_datasetA_full/pi \
  --out var/storage-experiment-recheck
```

`--run` 可重复指定。脚本对不满足原字节一致的过程文件保留原件，对精简后反而变大的调用文件也保留原件；发生任何数据/HTML 不一致立即失败。退出时临时目录自动清理。

完整实验结果和逐文件原始/存储校验和保存于 `var/storage-experiment-20261009/full/`。
