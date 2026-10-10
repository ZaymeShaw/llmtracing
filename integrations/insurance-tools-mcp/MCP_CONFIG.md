# MCP 配置

服务端已就绪。**注册的是一个 server,它对外发布 7 个工具**——客户端只写一条配置,
连上后自动拿到全部 7 个(见文末《为什么只注册一次》)。

---

## 0. 两个绝对路径(所有配置都用这两个)

| 用途 | 路径 |
|---|---|
| Python 解释器 | `/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python` |
| 启动参数 | `["-m", "itools"]` |

工程已 `pip install -e .`,所以**不依赖 cwd**,从任何目录启动都行。

自检(能连上就说明配置正确):

```bash
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
| ENV_TYPE=dev ITOOLS_AGENT_ID=probe ITOOLS_USER_ID=EMP000000 \
  /Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python -m itools 2>/dev/null \
| python3 -c 'import sys,json
for l in sys.stdin:
    m=json.loads(l)
    for t in (m.get("result") or {}).get("tools") or []: print(" ✓", t["name"])'
```

期望输出:

```
 ✓ product_knowledge
 ✓ benefit_info
 ✓ general_qa
 ✓ policy_search
 ✓ customer_search
 ✓ policy_detail
 ✓ customer_detail
```

---

## 0.5 最小可用配置(推荐先用这个)

只需要 **2 个**环境变量:

```json
{
  "mcpServers": {
    "insurance-tools": {
      "command": "/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python",
      "args": ["-m", "itools"],
      "env": {
        "ITOOLS_AGENT_ID": "claude-code",
        "ITOOLS_USER_ID": "EMP000000"
      }
    }
  }
}
```

`ITOOLS_EMPNO` / `ITOOLS_AGENTNO` **不填会自动用 `ITOOLS_USER_ID` 顶上**,效果一样。
只有"认证人与业务工号不是同一个值"时才需要显式写。

下面各节给的是写全版;知道自己在干什么的话用上面这份就行。

---

## 1. Claude Code

项目根目录建 `.mcp.json`(`claude mcp add` 也可以,等价):

```json
{
  "mcpServers": {
    "insurance-tools": {
      "command": "/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python",
      "args": ["-m", "itools"],
      "env": {
        "ENV_TYPE": "dev",
        "ITOOLS_AGENT_ID": "claude-code",
        "ITOOLS_USER_ID": "EMP000000",
        "ITOOLS_EMPNO": "EMP000000",
        "ITOOLS_AGENTNO": "EMP000000"
      }
    }
  }
}
```

等价的命令行写法:

```bash
claude mcp add insurance-tools -s project \
  -e ENV_TYPE=dev \
  -e ITOOLS_AGENT_ID=claude-code \
  -e ITOOLS_USER_ID=EMP000000 \
  -e ITOOLS_EMPNO=EMP000000 \
  -e ITOOLS_AGENTNO=EMP000000 \
  -- /Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python -m itools
```

工具在模型侧的全名(注意双下划线):

```
mcp__insurance-tools__product_knowledge
mcp__insurance-tools__benefit_info
mcp__insurance-tools__general_qa
mcp__insurance-tools__policy_search
mcp__insurance-tools__customer_search
mcp__insurance-tools__policy_detail
mcp__insurance-tools__customer_detail
```

自检:`claude mcp list` → `insurance-tools: … - ✓ Connected`

---

## 2. Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.insurance-tools]
command = "/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python"
args = ["-m", "itools"]
env = { ENV_TYPE = "dev", ITOOLS_AGENT_ID = "codex", ITOOLS_USER_ID = "EMP000000", ITOOLS_EMPNO = "EMP000000", ITOOLS_AGENTNO = "EMP000000" }
```

不想改全局配置,用运行时注入:

```bash
codex -c 'mcp_servers.insurance-tools.command="/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python"' \
      -c 'mcp_servers.insurance-tools.args=["-m","itools"]' \
      -c 'mcp_servers.insurance-tools.env={ENV_TYPE="dev",ITOOLS_AGENT_ID="codex",ITOOLS_USER_ID="EMP000000",ITOOLS_EMPNO="EMP000000",ITOOLS_AGENTNO="EMP000000"}'
```

**注意**:Codex 默认要求对 MCP 调用逐次审批,非交互场景(`codex exec`)会被拦下并报
`MCP tool call requires approval, but approval policy is never`。要么交互式确认,
要么加 `--dangerously-bypass-approvals-and-sandbox`,要么配 `default_tools_approval_mode`。

自检:`codex mcp list` → `insurance-tools  …  enabled`

---

## 3. Cursor / 其它 JSON 型客户端

`.cursor/mcp.json`(Cursor),或任何吃 `mcpServers` 的客户端:

```json
{
  "mcpServers": {
    "insurance-tools": {
      "command": "/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python",
      "args": ["-m", "itools"],
      "env": {
        "ENV_TYPE": "dev",
        "ITOOLS_AGENT_ID": "cursor",
        "ITOOLS_USER_ID": "EMP000000",
        "ITOOLS_EMPNO": "EMP000000",
        "ITOOLS_AGENTNO": "EMP000000"
      }
    }
  }
}
```

---

## 4. Agno(当 MCP client 接)

```python
from agno.tools.mcp import MCPTools

tools = MCPTools(
    command="/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp/.venv/bin/python -m itools",
    env={
        "ENV_TYPE": "dev",
        "ITOOLS_AGENT_ID": "agno-mcp",
        "ITOOLS_USER_ID": "EMP000000",
        "ITOOLS_EMPNO": "EMP000000",
        "ITOOLS_AGENTNO": "EMP000000",
    },
)
```

---

## 5. 身份环境变量(全部走 `env`,模型看不到、也传不了)

| 变量 | 含义 | 必需 |
|---|---|---|
| `ITOOLS_AGENT_ID` | 认证主体。**缺了进程直接拒绝启动(exit 2)** | ✅ 是 |
| `ITOOLS_USER_ID` | 业务 `user_id`。保单解析/查询要求非空 | 保单系必需 |
| `ITOOLS_EMPNO` | 显式保单工号。缺省回退到 `ITOOLS_USER_ID` | 否 |
| `ITOOLS_AGENTNO` | 显式客户工号。缺省回退到 `ITOOLS_USER_ID` | 否 |
| `ITOOLS_SESSION_ID` | 固定会话标识。缺省每次调用新生成 | 否 |
| `ENV_TYPE` | `dev` / `stg` / `prd`,缺省 `dev` | 否 |

三条硬规则:

1. **没有 `ITOOLS_AGENT_ID` 起不来**,报错退出,不用别的值兜底。
2. **显式空串/空白视为配置错误**,同样拒绝启动——别写 `ITOOLS_USER_ID=""`。
3. **缺工号不会拿 `ITOOLS_AGENT_ID` 顶替**。只给 `ITOOLS_AGENT_ID` 不给 `ITOOLS_USER_ID` 时,
   `policy_detail` 返回 `status=DENIED` + `error.code=DENIED` + hint《缺少本轮代理人工号》。

回退链(只在"工号"一档上生效):

```
ITOOLS_EMPNO     ┐
                 ├─ 显式值优先
ITOOLS_AGENTNO   ┘
        └─ 都不给 → 回退 ITOOLS_USER_ID
                └─ User_ID 也没有 → DENIED(绝不用 ITOOLS_AGENT_ID) 
```

### 换工号怎么办

身份是**进程级**的,所以换工号 = 换一份 server 配置:

```json
{
  "mcpServers": {
    "insurance-tools-A1": { "command": "…", "args": ["-m","itools"],
      "env": { "ITOOLS_AGENT_ID": "A1", "ITOOLS_USER_ID": "EMP000001" } },
    "insurance-tools-A2": { "command": "…", "args": ["-m","itools"],
      "env": { "ITOOLS_AGENT_ID": "A2", "ITOOLS_USER_ID": "EMP000002" } }
  }
}
```

这是刻意的:原系统工号由上游请求体注入、模型碰不到;MCP 没有请求体,只能落到连接级——
**保证模型无法伪造身份**。

---

## 6. 为什么只注册一次就有 7 个工具

MCP 里 **server 是打包单位,tool 是调用单位**。

```
   一条配置  ──▶  insurance-tools(一个进程)
                  ├── product_knowledge
                  ├── benefit_info
                  ├── general_qa
                  ├── policy_search
                  ├── customer_search
                  ├── policy_detail
                  └── customer_detail
                      │
        客户端连上后自己发 tools/list,拿到 7 个,
        平铺进模型的工具列表
                      ▼
                 模型看到:7 个独立函数
```

客户端 `initialize` 之后会主动发一次 `tools/list`,一次拿全 7 个工具(name / description /
inputSchema)。**不需要逐个挂,也没有地方逐个挂。**

权限可以按单个工具管,例如 Claude Code:

```bash
--allowedTools "mcp__insurance-tools__policy_search"     # 只放行这一个
```

---

## 7. 按业务拆成多个 server?当前不支持

**一个 server 固定发布全部 7 个工具。** 想按业务拆(搜索类 / 详情类 / 知识类)得改服务端代码
挑工具发布,配置层做不到。

不拆是有意的:agno 侧就是"一个 agent 挂 7 个工具",拆开会把"工具分组方式"这个无关变量
混进对比实验。

若确实需要分组,在**客户端侧**过滤,不要动服务端:

```bash
# Claude Code:按工具名逐个控制
--allowedTools "mcp__insurance-tools__policy_search" --disallowedTools "mcp__insurance-tools__customer_detail"
```
