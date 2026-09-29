# llmtracing

用于批量评测 Claude Code、Pi、Insurance QA 等 harness，记录模型与工具调用，生成轨迹、Excel 和 HTML 报告。

## 目录

```text
llmtracing/
├── eval_harness/                    # 评测框架
│   ├── src/eval_harness/
│   │   ├── adapters/                # harness 接入实现
│   │   │   ├── base.py              # 公共结果类型与文件工具
│   │   │   ├── registry.py          # 注册与分发
│   │   │   ├── claude.py
│   │   │   ├── pi.py
│   │   │   └── insurance.py
│   │   ├── run.py                   # 单个 harness 的统一评测入口
│   │   ├── suite.py                 # 现有双路/三路对比运行
│   │   └── ...                      # 数据导入、轨迹归一化、报告
│   ├── configs/
│   │   ├── profiles/                # 每个 harness 的连接与运行方式
│   │   ├── runs/                    # 默认运行配置：profile + bundle
│   │   ├── experiments/             # 历史实验与特定数据集配置
│   │   ├── examples/                # 配置示例
│   │   └── legacy/                  # 历史扁平配置参考
│   ├── schemas/                     # 数据格式定义
│   └── tests/
├── workspaces/                      # CLI agent 的隔离工作目录
│   ├── claude/                      # agent.md + 本地 MCP/鉴权/会话
│   └── pi/                          # agent.md + 本地 MCP/鉴权/会话
├── llm_gateway/                     # 共享模型网关与调用记录
├── scripts/                         # 安装、服务启动、评测维护脚本
└── docs/                            # 设计和操作文档
```

Adapter 是 Python 中的协议适配实现；workspace 是启动 CLI 时使用的工作目录。Claude 和 Pi 各有一个 workspace，供它们加载独立的 MCP 与鉴权设置，避免评测代码和答案文件落在 agent 的工作目录中。Insurance 通过外部 HTTP 服务运行，因此只需要 adapter 和 profile。

## 运行

在仓库根目录安装 Python 依赖，并按各 harness 的要求安装 CLI 或启动服务：

```bash
python3 -m pip install -r eval_harness/requirements.txt
export PYTHONPATH="$PWD/eval_harness/src"

# 三个 harness 使用同一个入口；数据集需在本地准备
python3 -m eval_harness.run --config eval_harness/configs/runs/claude.yaml --cases A01
python3 -m eval_harness.run --config eval_harness/configs/runs/pi.yaml --cases A01
python3 -m eval_harness.run --config eval_harness/configs/runs/insurance.yaml --cases A01
```

`--dry-parse` 只检查并解析题目，`--resume` 恢复未完成运行。现有多路对比入口为 `python3 -m eval_harness.suite`；旧的 `eval_harness.dual_run` 仍转发到该入口。

配置文件中的 `profile_path`、`bundle_path` 相对于该配置文件；profile 中的 `project_cwd` 和输出路径相对于 profile 文件。`agent_md` 与 `mcp_config` 相对于 agent 的 workspace。仓库位置默认从代码路径确定，也可设置 `MOCK_SYSTEM_ROOT`。

## 本地数据与配置

`eval_harness/data/`（题库）、`eval_harness/bundles/`（派生输入）、`eval_runs/`（报告）、`llm_gateway/logs/` 和 `run/`、`backups/`、虚拟环境及本地密钥不提交。每个 workspace 只提交经过检查的 `agent.md` 和说明文件。

复制 `llm_gateway/.env.example` 到 `.env` 并填写自己的密钥；在各 workspace 中配置 `.mcp.json`，Claude 还需 `.claude/settings.local.json`。Insurance 服务及 Pi provider 设置在本仓库之外管理。历史实验、外部服务启动脚本可能仍含本机路径，换机器时按需修改。

详见 [评测框架说明](eval_harness/README.md) 和 [新增 harness](docs/adding_harness.md)。
