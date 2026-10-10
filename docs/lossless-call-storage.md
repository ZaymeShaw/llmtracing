# 逐题调用归档无损压缩

2026-10-09，按用户“先做无损的”要求接入正常写入流程。

新生成的逐题调用文件默认写为 `cases/<case>/llm_calls.jsonl.gz`，使用标准 gzip（level 6）。解压结果保持旧写入器完整的 UTF-8 JSONL 字节：请求、回复、工具参数、真实路径、重复历史消息、失败记录及字段顺序均保留。没有限额、截断、采样或字段引用格式；HTML 渲染函数及页面内容不改。

## 默认行为

- `write_case_llm_jsonl()` 在新 case 中原子写入 gzip；已有明文 `llm_calls.jsonl` 的 case 继续使用明文，不自动迁移或删除旧文件。
- 已有 gzip 的增量导出先读取完整旧记录，在逐题写锁内合并再写入。请求阶段快照不会清掉已保存的终态，失败与重试各自保留。
- 普通读取者在原子替换前后读到完整旧版或新版。同一 case 的第二个写入者会明确报锁占用，调用方稍后重试，不静默覆盖并发更新。
- 临时文件写完后 fsync，并通过解压、长度及 SHA256 核验，才原子替换目标；再 fsync 父目录。序列化、压缩、校验或替换失败时不发布半文件。极端进程中断可能留下 `.llm-calls-*` 临时文件，读取器只识别正式文件名。
- 压缩归档的 CRC/截断错误会停止 HTML/Excel 读取及增量导出，不能伪装成空数据。runner 的归档写入失败会标记 tracing 不完整，不能将本题记为成功。
- `read_case_llm_jsonl_text()`、trace 引用读取、HTML、Excel、Insurance 恢复、增量导出、逐题审计和隔离脚本均兼容明文/gzip。导出密钥扫描也解压检查直接 gzip 文件和 ZIP 内的 gzip 成员。

`trace.json` 的 `llm_calls_ref` 和 HTML 内的原始文件说明仍使用逻辑名称 `llm_calls.jsonl`，读取层解析实际存储位置。外部工具若直接打开文本文件，可用系统 gzip 恢复到单独位置：

```sh
gzip -dc eval_runs/my_batch/cases/CASE_ID/llm_calls.jsonl.gz > /tmp/calls-restored.jsonl
```

活跃网关日志仍按原流程追加明文，不改变查询游标或轮转规则。历史运行目录、HTML、ZIP、trace、stream、session、mapped 和失败尝试都未批量修改。本次没有启用此前的字段引用/过程副本实验，也没有调用旧的磁盘清理脚本。

## 实测结果

使用正式写入器、正式读取器和现有 HTML 生成器，在临时目录回放五种 harness 的 10 个真实批次：503 份逐题调用文件，3,243 次调用。

| 范围 | 原大小 MiB | 新大小 MiB | 减少 |
| --- | ---: | ---: | ---: |
| 逐题调用归档 | 736.45 | 179.04 | 75.69% |
| 完整运行目录（其他文件计入原大小） | 2,368.59 | 1,811.18 | 23.53% |

净少存 **557.41 MiB**。503 份 JSONL 解压后原字节完全一致，Excel 调用数据行一致；10 份重新生成的 HTML 与已有 HTML SHA256 全部相同；5,658 个源文件在核验前后未变。该数字是新存储方式的逻辑长度，未将临时符号链接大小误计为节省，也不代表本轮已清理历史目录或释放相应 APFS 磁盘块。

前一轮 27.16% 是“字段引用 + 过程副本”的独立实验，本次默认上线的是标准 gzip 的保守第一步。两者不是同一组合，收益不能直接相加。

214 项 harness 测试通过，涵盖默认写入、旧格式读取、增量合并、失败/重试、多 lane、同 case 写锁、写入失败、损坏归档、HTML/Excel、share ZIP 和密钥扫描。测试进程移除了继承的 Anthropic token 等本地凭据环境变量；Pi 配置测试显式隔离环境优先级，避免其他 SDK 测试加载本机 `.env` 影响假数据断言。

随后四种 harness 使用新默认流程各重跑 120 题、174 轮。包含保留的失败尝试，逐题 JSONL 逻辑体积为 727.21 MiB，实际 gzip 为 177.11 MiB，少存 550.09 MiB。四份 HTML 与解压到普通 JSONL 后的重建结果逐字节一致，Excel 调用数据一致。该数字仅统计调用归档，完整批次还包含 HTML、原生会话及网关日志；结果保存在本地 `eval_runs/comparison_gzip_20261009/`。

## 复核

```sh
PYTHONPATH=eval_harness/src llm_gateway/.venv/bin/python scripts/verify_lossless_call_storage.py \
  --run eval_runs/dsh_insurance_mcp_ready_20261009 \
  --out var/lossless-storage-recheck --keep-html
```

脚本仅回放归档，不调用模型；`--run` 可以重复。正式验证的逐文件大小、原始及 gzip SHA256 见 `var/lossless-call-storage-20261009/full/`。可查看的 DSH 重建页面是 `var/lossless-call-storage-20261009/preview/run-00.html`。
