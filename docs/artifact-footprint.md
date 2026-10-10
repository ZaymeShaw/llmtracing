# 产物占用与重复输出

最新实施状态：按用户后续“先做无损的”要求，正常流程中新 case 的逐题调用归档已默认使用标准 gzip，完整 JSONL 原样保存并在读取时解压；已有明文 case 不自动迁移。10 个真实批次验证净少存 557.41 MiB，调用归档减少 75.69%，全部 HTML 原字节一致。这是标准压缩的独立第一步，此前字段引用和过程副本方案仍仅为实验。详见 [无损调用归档](lossless-call-storage.md)。下文保留此前各轮调查、撤回和实验的记录。

2026-10-09 检查：`comparison_20261008/results/new-run` 中 HTML 合计约 848 MiB，逐题 `llm_calls.jsonl` 合计约 734 MiB；OpenCode 的 `export.log` 另有约 135 MiB 完整调用数据。模型调用会携带不断增长的历史上下文，网关记录、逐题归档和独立 HTML 各自保存完整内容，因此长对话和工具返回会放大占用。历史备份还复制过约 325 MiB 的 `.venv`。

本轮只消除确定的冗余：

- Pi 的 `concat_streams()` 已写入完整 `stream.jsonl`，不再重复生成相同的 `stream_pi.jsonl`。逐轮流、映射调试文件、session transcript、失败尝试和规范化 trace 保留。
- 四 harness runner 导出 OpenCode HTML 时，将 CLI stdout 从完整 `pairs` 改为调用摘要表。完整请求、回复、工具参数及错误仍在逐题调用文件和 HTML 中；`export.log` 保存导出状态与摘要。
- HTML 生成保持原有序列化和加载逻辑。曾加入的紧凑 JSON 分隔符已按用户“HTML 不能有任何变化”的要求撤回。

此前尝试的大小限制、磁盘配额、写入阻断和默认压缩均已撤回。未加入历史批次自动删除策略。

已完成批次的清理仅针对逐字节相同的副本：在获取 run 的 writer lock、确认 run_meta.finished_at 和 HTML 存在后，比较 `stream_pi.jsonl` 与同目录 `stream.jsonl`，相同才删除前者。20261008 的 Pi full/smoke 共删除 123 份相同副本，释放约 98.7 MiB；保留文件校验和一致，已有 HTML 文件字节未改变。记录在 `var/llmtracing-dedup-20261009/cleanup.json`。

相关 75 项回归测试通过；真实 DSH 三题报告的全部 DATA 字段和 viewer JavaScript 等价核验见 `var/llmtracing-dedup-20261009/display-verification.json`。

## 复用分享包逻辑

今天交付的 `comparison_20261008-share.zip` 从完整 ZIP 的 1,057,271,208 字节降至 383,932,022 字节，缩小 63.7%。43 个原 HTML 字节全部相同，55 个本地链接目标保留，8 份新旧报告的浏览器交互检查通过。证据在 `eval_runs/comparison_20261008/checks/share-unchanged-build.json` 和 `share-original-browser-check.json`。

这次交付使用的是筛选打包成员和标准 ZIP deflate；曾试验过的 HTML 字符串池/gzip 加载方案不在交付包中。该试验不复用到报告生成。

通用入口为 `./llmtrace report share RUN_DIR_OR_FULL_ZIP --out SHARE.zip`，实现位于 `eval_harness/share_zip.py`。它直接读取已有目录或归档，保留每个 HTML 原始字节和所有 `href/src` 本地链接目标，未链接的原始数据及调试日志只从包内省略。包内清单列出保留文件的 SHA256 和省略项，导出前复用密钥扫描，重新读取 ZIP 验证字节及 CRC。

按用户后续要求，基础版 `report html --zip`、runner 的 `--pack-zip` 和 `product_zip` 也复用相同规则，无需换用专门的 share 命令。`pack_run_zip` 保留原有包内相对路径布局和一个旧包备份，排除自身与旧包，直接打包已有 HTML，不再重复生成 HTML、不再要求 Excel。普通 HTML 导出的内容、原始请求路径和原始数据目录保留。显式 `report html RUN_DIR --zip` 在已有 HTML 时直接打包；缺失时先按原生成逻辑导出。网关日志导出的 `--zip` 也接入同一规则。这减少基础版导出包的占用，不自动删除运行目录中的 JSONL/表格。

基础版实测：DSH 三题 / 10 调用经 `pack_run_zip` 导出为 1,115,766 字节，同压缩参数的完整目录包为 1,633,572 字节，减少 31.7%。已有 HTML、包内 HTML 及所有原始文件校验一致；ZIP CRC 和保留文件 SHA256 全部通过。证据见 `var/llmtracing-dedup-20261009/basic-share-verification.json`。39 项相关回归测试通过，覆盖原 CLI、网关导出、runner 的 `--pack-zip`、多 harness、链接附件、重复打包、失败保护和密钥扫描。

## 正常流程的清理边界

用户要求数据真实、HTML 不变，只处理确定的错误输出或无用副本。真实请求和回复中的重复消息、工具定义、路径、原始结构均保留；不替换为引用，不合并请求，不根据文本相似程度删除记录。失败、重试、错误日志也可能是重要证据，不能仅因执行失败而删除。

此前字符串引用试算发生在内存中，结果保存在 `var/llmtracing-dedup-20261009/normal-flow-analysis.json`；没有写入正式报告。该方案会改变 HTML 内嵌格式，因此不采用，其试算节省率不属于本项目在现有要求下承诺的节省。HTML 生成器的紧凑 JSON 空格优化也已撤回，恢复原有代码。

继续保留的正常流程优化只有：不生成与 `stream.jsonl` 完全相同且没有消费者的 `stream_pi.jsonl`；不把已有完整归档的请求/回复额外打印到 `export.log`，导出状态和摘要继续记录。它们不改变模型请求或报告生成的数据输入。

撤回后，使用现存 DSH 三题 / 10 调用的同一份数据在临时目录重新生成 HTML，与原报告逐字节相同（2,865,013 字节），原报告未改写。验证记录为 `var/llmtracing-dedup-20261009/html-unchanged-verification.json`；后续基础版改动只涉及打包函数及 CLI 分派，HTML 内容生成函数仍沿用原逻辑。

不能直接套用分享包规则清理正常运行目录：逐题 JSONL 支持重建报告和增量导出；网关日志支持重查、归因与未导出调用恢复；harness stream/session 可能记录未发送给模型的工具执行过程。已有报告不重写；后续处理必须先证明是多余副本、保留完整原件且不影响消费者。

后续详细只读审计纠正了此前“正常流程只能有限减量”的笼统判断：20261008 new-run 的 734.35 MiB 逐题 JSONL 中，416.79 MiB 是同一次调用中四处重复字段的值；另有 367.85 MiB 辅助输出已验证重复或可完整重建。前者涉及存储/读取兼容，后者也有现存消费者，均不表示可以立即删掉。完整拆解、早期版本同输入比较与逐文件证据见 `var/storage-audit-20261009/report.md`。这些统计不合并不同真实调用、不修改 HTML，也不是将此前被拒绝的 HTML 字符串引用方案重新上线。

随后按用户要求完成独立落盘实验：5 种 harness、10 个批次、3,243 次调用，过程副本和同调用附加字段采用可恢复表示后，完整运行目录逻辑体积减少 643.31 MiB（27.16%）。30 份重建 HTML 与已有报告逐字节一致，1,172 份被替代文件全部恢复为原始字节，原始目录未修改。这是实际写出/读取验证的净收益，已计入索引；没有计入省略 export.log 的收益，也没有修改默认流程。方法、结果与实时写入尚未覆盖的边界见 [正常落盘无损实验](storage-lossless-experiment.md)。
