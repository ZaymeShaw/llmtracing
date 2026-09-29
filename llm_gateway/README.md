# llm_gateway — eval LLM call capture

## Config profiles (switchable)

| File | Role |
|---|---|
| `.env` | **Default** upstream (current / unchanged) |
| `.env.penguin` | Named copy of the current penguin/anthropic upstream |
| `.env.aliyun_maas` | Aliyun MaaS **Anthropic** (`…/apps/anthropic`, `deepseek-v4-flash-0731`) — use for Claude |
| `.env.aliyun_maas_openai` | Aliyun MaaS OpenAI-compatible (`…/compatible-mode/v1`) — Insurance-only / chat experiments |
| `.env.insurance_4002` | Dedicated Insurance/Pi relay on port 4002; its port, local key, LiteLLM YAML and log directory are explicit |
| `.env.opencode_4003` | OpenCode test relay on port 4003; also records the OpenCode executable, sandbox and dataset paths |

Secrets live only in `.env` / `.env.*` (gitignored). Do **not** overwrite `.env` when adding a new upstream — add `.env.<name>` instead.
The dedicated relay profiles are local files because they contain credentials. To change a relay, edit its profile and restart that relay; launchers do not replace its port, key or log directory at runtime.

| Var | Meaning |
|---|---|
| `UPSTREAM_API_BASE` | Upstream base URL |
| `UPSTREAM_API_KEY` | Upstream API key |
| `UPSTREAM_PROTOCOL` | Wire protocol for LiteLLM (`anthropic`, `openai`, …) |
| `UPSTREAM_MODEL` | Upstream model id |
| `LITELLM_HOST` / `LITELLM_PORT` | Local gateway |
| `LITELLM_MASTER_KEY` | Local Bearer for Claude / clients |

`start_litellm.sh` composes LiteLLM’s `provider/model` as `${UPSTREAM_PROTOCOL}/${UPSTREAM_MODEL}` without requiring a Claude workspace. Set `LITELLM_SYNC_CLAUDE_SETTINGS=1` to opt into syncing an existing Claude settings file. Config yaml still has `model_name: "*"` so local clients can keep calling `deepseek-v4-flash` while upstream model id differs.

## Start / switch

```bash
# default (.env) — current upstream
./stop_litellm.sh && ./start_litellm.sh

# Aliyun MaaS extra profile
./stop_litellm.sh && ./start_litellm.sh --profile aliyun_maas
# or: ./start_litellm.sh aliyun_maas --restart

# back to named penguin profile
./stop_litellm.sh && ./start_litellm.sh --profile penguin
```

Active profile is recorded in `run/active_profile`. Switching profiles forces a restart even if port 4001 looks healthy.

The Insurance launcher reads `.env.insurance_4002` by default: `./start_insurance_litellm.sh`. For a second relay, use `./start_litellm.sh --profile opencode_4003 --foreground`; foreground mode does not touch the background listener on port 4001.
