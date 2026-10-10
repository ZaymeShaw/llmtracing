# insurance-tools-mcp

把 **7 个保险问答查询工具**以 MCP 暴露给任意 harness。独立工程、独立进程,不依赖上游问答工程的运行时。

用途:让别的 harness(Claude Code / Codex / Cursor / 自研评测 harness)拿到**同一批工具**跑同样的任务,
好把"harness 差异"从"工具差异"里分出来。

---

## 工具注册

MCP 工具服务器已就绪。配置见 [`MCP_CONFIG.md`](MCP_CONFIG.md)。

## 工具面

| 工具 | 入参 | 下游 |
|---|---|---|
| `product_knowledge` | `query` | 产品实体解析 → 产品知识检索(两跳) |
| `benefit_info` | `query` | 服务权益检索 |
| `general_qa` | `query` | FAQ 检索 |
| `policy_search` | `query` | 保单自然语言解析 → 保单结构化查询(两跳) |
| `customer_search` | `query`, `compact?`, `fields?` | 客户意图解析 → 客户结构化搜索(两跳) |
| `policy_detail` | `polno`, `fields?` | 保单明细(纯数字尾号先定位) |
| `customer_detail` | `customer_id?`, `customer_ids?`, `compact?`, `fields?` | 客户画像及详情 v5；至少提供一个客户中心 ID(姓名/手机号先走 `customer_search`,上游 FX-05 起不再对模型暴露) |

工具的名字、描述、输入 schema 全部来自 `itools/tools_spec.json`,**不做二次推导**——
这份规格是上游模型可见工具面的直接导出,两端逐字节相同,由 `tests/test_spec_parity.py` 守住。

返回体是核心函数产出的 `str` JSON 信封(`status` / `facts` / `evidence` / `gaps` / `hint` / `error`),
原样放进 MCP text content,不解析、不重组。

---

## 运行

stdio 传输,由 harness 拉起进程:

```bash
ENV_TYPE=dev \
ITOOLS_AGENT_ID=your-agent-id \
ITOOLS_USER_ID=EMP000000 \
.venv/bin/python -m itools
```

MCP 客户端配置。**一个 server 只注册一次**,连上后 harness 自己就看到里面全部 7 个工具,
不需要逐个挂。

Claude Code(`.mcp.json`,项目根目录;或 `claude mcp add`):

```json
{
  "mcpServers": {
    "insurance-tools": {
      "command": "/abs/path/insurance-tools-mcp/.venv/bin/python",
      "args": ["-m", "itools"],
      "env": {
        "ENV_TYPE": "dev",
        "ITOOLS_AGENT_ID": "your-agent-id",
        "ITOOLS_USER_ID": "EMP000000"
      }
    }
  }
}
```

Codex(`~/.codex/config.toml`):

```toml
[mcp_servers.insurance-tools]
command = "/abs/path/insurance-tools-mcp/.venv/bin/python"
args = ["-m", "itools"]
env = { ENV_TYPE = "dev", ITOOLS_AGENT_ID = "your-agent-id", ITOOLS_USER_ID = "EMP000000" }
```

连接自检:

```bash
claude mcp list        # → insurance-tools: … - ✓ Connected
codex  mcp list        # → insurance-tools  enabled
```

### 身份

工号由**连接级注入**,不进工具 schema——模型不可传,与上游同一条纪律。

| 变量 | 含义 | 必需 |
|---|---|---|
| `ITOOLS_AGENT_ID` | 认证主体 | 是;缺失拒绝启动 |
| `ITOOLS_USER_ID` | 业务 user_id;保单解析/查询要求非空 | 保单系工具必需 |
| `ITOOLS_EMPNO` | 显式保单工号;缺省回退 `ITOOLS_USER_ID` | 否 |
| `ITOOLS_AGENTNO` | 显式客户工号;缺省回退 `ITOOLS_USER_ID` | 否 |
| `ITOOLS_SESSION_ID` | 固定会话标识;缺省每次调用生成 | 否 |
| `ENV_TYPE` | `dev` / `stg` / `prd`,决定下游地址 | 否,缺省 `dev` |

显式设为空串/空白视为配置错误,启动即拒绝。**缺业务工号时不拿 `ITOOLS_AGENT_ID` 顶替**。

> 与上游一致:`dev` 预置了真实连接;`stg` / `prd` 的 `handlers` 是空的,由部署方填本环境地址,
> 缺失时启动即失败(而不是静默跑到错误的下游)。

---

## 与上游的同步

`app/` 与 `tool_adapter_sdk/` 是上游纯工具层的**逐字副本**,不是改写。重新同步一条命令:

```bash
.venv/bin/python scripts/sync_from_upstream.py --source <上游工程根目录>
```

它做六件事:

1. 以 7 个工具 + 配置装载 + adapter 工厂为种子,AST 求**运行期 import 闭包**(含函数内惰性 import,跳过 `TYPE_CHECKING` 块);
2. 复制闭包内文件;
3. 重新生成 `app/runtime/model_router.py`;
4. 复制 `app/conf/`(排除 `models/`,内含开发期明文凭据);
5. 复制 `capabilities/`(`app/conf/capabilities.py` 在 import 期即解析该目录);
6. 用上游解释器重新导出 `itools/tools_spec.json`(该步需要 agno)。

同步是**幂等**的:连跑两次,文件校验和无差异。

### 唯一的偏离

上游 `app/runtime/model_router.py` 里既有 conf 契约模型,也有模型工厂。后者 `import agno`。
本工程只需要前者(`call_backend` 的预算准入要读 `HarnessConf`),所以:

- **排除** `app/runtime/model_router.py` 与 `app/runtime/model_stream.py`;
- 由同步脚本从上游源文件**逐字摘出** `ProfileLimits` / `AgentProfileLimits` / `HistoryWindow` /
  `ModelTimeoutLimits` / `HarnessConf` 五个纯 pydantic 模型,写成
  `app/runtime/model_router.py`。

摘出后的文件保持同样的 import 路径,因此 `app/conf/__init__.py` **一个字符都不用改**。
结果是本工程运行时 `sys.modules` 里没有 `agno`:

```bash
.venv/bin/python -c "import app.conf; app.conf.harness_conf(); import sys; \
  print([m for m in sys.modules if m.startswith('agno')])"   # -> []
```

---

## 与上游的一致性证据

三条证据,依次加强:

| 层次 | 手段 | 结果 |
|---|---|---|
| 工具面 | `tests/test_spec_parity.py`:MCP `tools/list` vs 上游导出的 name/description/inputSchema | **逐字节相同** |
| 确定性路径 | `scripts/parity.py`:16 条用例(缺工号准入、非法入参)两侧各跑一遍 | **逐字节相同** |
| 查询路径 | `scripts/parity_stub.py`:录下真实上游响应,两侧用同一份录播重跑 7 个工具 | **逐字节相同**(仅 trace_id 派生字段已归一) |

```bash
.venv/bin/python scripts/parity.py      --agno-repo <上游工程根目录>   # 打真实上游
.venv/bin/python scripts/parity_stub.py --agno-repo <上游工程根目录>   # 录播,可复现
```

`parity_stub.py` 的归一化只做一件事:把 `request_id` / `fact_id` / `evidence_id` /
`source_call_id` / `evidence_refs` / `retrieved_at` 里由 trace_id 派生的部分换成占位符。
两侧 trace_id 不同是**输入差异**——同一个系统连跑两次也不同。除此之外的一切
(`status`、`data`、`field_descriptions`、`gaps`、`hint`、facts 内容与顺序)都是逐字节比。

---

## 与上游的行为差异

工具层的**语义**(参数、编排、投影、信封、错误归一)完全一致。以下差异来自 MCP 无状态调用模型:

| 差异 | 说明 |
|---|---|
| 四条工具 hook 不生效 | 上游 `budget` / `selection_guard` / `dedupe` / `metrics` 挂在 Agno 的工具钩子机制上。本工程保留 `call_backend` 的**单次调用 RPC 预算**(每次调用新建 `agent` 档预算,上限 10),不做轮级上限与同轮去重 |
| 跨轮产品确认降级 | 上游靠会话态记住"上一轮用户确认的产品";MCP 每次调用新建空会话态,歧义时直接返回候选,由上层把标准名写进下一轮 query |
| `session_id` 每次调用新生成 | 上游保单解析固定发送空历史,故与现状等价 |
| `ask_clarify` / `entity_lookup` 未发布 | 前者需要跨轮挂起态,后者是本地词典;都不在本次工具面内 |

---

## 测试

```bash
.venv/bin/python -m pytest -q                                   # 离线,不触网
ENV_TYPE=dev ITOOLS_AGENT_ID=A1 ITOOLS_USER_ID=EMP000000 \
  .venv/bin/python scripts/smoke.py                             # 真实 MCP 协议 + 真实下游,逐个调 7 个工具
```

`scripts/smoke.py` 支持 `ITOOLS_ONLY=<tool>` 只调一个,`ITOOLS_DRY=1` 只列工具。
