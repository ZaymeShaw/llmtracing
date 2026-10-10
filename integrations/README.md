# 120 题使用的 insurance-tools MCP

[`insurance-tools-mcp/`](insurance-tools-mcp/) 是本次评测所用 MCP 项目的源码快照，包含 7 个工具的定义、执行实现、后端配置、同步脚本和离线测试。Claude、Pi、OpenCode、DSH 可通过 MCP 使用这些工具；Insurance QA 仍运行其独立项目中的原生工具层。

## 安装

在 llmtracing 仓库根目录执行，使用 Python 3.12。MCP 使用独立虚拟环境，避免与 LiteLLM 的依赖混装：

```bash
python3.12 -m venv integrations/insurance-tools-mcp/.venv
integrations/insurance-tools-mcp/.venv/bin/python -m pip install -e './integrations/insurance-tools-mcp[dev]'
```

安装后 MCP 入口为该虚拟环境的 `python -m itools`，使用 stdio，由 harness 启动。无需另起一个 HTTP MCP 服务。

## 客户端配置

以下配置来自实际跑批配置，保留已确认的 mock 用户号／工号、环境选择和 Pi 的 `directTools` 开关；仅将启动命令中的本机绝对路径替换为占位路径：

| 模板 | 使用位置 |
| --- | --- |
| [root.mcp.example.json](insurance-tools-mcp/client-configs/root.mcp.example.json) | 仓库根目录 `.mcp.json` |
| [claude.mcp.example.json](insurance-tools-mcp/client-configs/claude.mcp.example.json) | `workspaces/claude/.mcp.json` |
| [pi.mcp.example.json](insurance-tools-mcp/client-configs/pi.mcp.example.json) | `workspaces/pi/.mcp.json`，DSH 也使用这份配置 |

复制需要的模板到对应位置，并将 `command` 改成新建 MCP 虚拟环境中 Python 的**绝对路径**。例如安装目录为 `/work/llmtracing` 时填写 `/work/llmtracing/integrations/insurance-tools-mcp/.venv/bin/python`。已有可用配置可以继续使用，不必覆盖。

OpenCode 的 `.env.opencode_4003` 中 `OPENCODE_MCP_CONFIG` 指向本地准备好的 MCP 配置文件，原生 runner 负责接入。其余启动命令见 [主 README 的 120 题说明](../README.md#用-5-个-harness-跑同一套-120-题)。

开发环境后端地址保留在 `app/conf/tool/tool_config_dev_args.yaml`，与现有 MCP 一致；能够启动 MCP 不等于能够访问这些后端，运行机器仍需具备对应网络访问条件。切换环境或后端时修改该环境配置。mock ID 不是访问密钥，不应把它当作鉴权措施。

## 快照范围与验证

- `app/`、`tool_adapter_sdk/`、`capabilities/`、`itools/` 按来源逐文件保留，没有改工具 schema、业务实现或后端地址。
- 保留上游同步脚本；`scripts/sync_config.json` 的 `source` 改为待填写路径。只有主动更新工具源码时才需要配置上游 checkout，安装和正常 MCP 运行不依赖它。
- 两项原来依赖作者机器上外部 checkout 的同步测试改用固定结构与临时副本，继续验证符号依赖提取和失败时不修改受管树；测试无需真实后端或外部问答项目。
- `.sync_backups/`、虚拟环境、缓存、安装产物和生成的 `SYNC_STATE.json` 未纳入快照。仓库忽略规则阻止备份和本地凭据被普通 `git add` 加入。
- 快照复制前的 291 个选中文件，其相对路径与 SHA256 汇总摘要为 `298d7416eb9653c5b3e29e7366247ea3a661ab5a5f5e54a06e58548322344593`。发布时额外修改了同步路径、忽略规则及上述两项测试，并增加客户端模板；该摘要用于标识来源，不代表修改后的整个目录摘要。

运行离线测试，不调用真实后端：

```bash
cd integrations/insurance-tools-mcp
.venv/bin/python -m pytest tests -q
```

发布前已通过 34 项离线测试，并构建 wheel 核对工具规格、环境 YAML 和 capability 资源均进入安装包。验证时确认 `app`、`itools` 和 `tool_adapter_sdk` 从本目录加载，没有误用原项目的 editable 安装。未重新请求真实后端。

原 MCP 的工具说明及维护文档见 [README](insurance-tools-mcp/README.md) 和 [MCP_CONFIG](insurance-tools-mcp/MCP_CONFIG.md)。其中旧机器路径和同步记录属于来源项目的说明，安装时以本页路径为准。
