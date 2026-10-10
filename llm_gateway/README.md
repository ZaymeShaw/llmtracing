# llm_gateway — eval LLM call capture

DeepSeek Harness SDK/CLI lanes: see [DSH lane quickstart](../docs/dsh-lane-quickstart.md).
Their observer config is `config.litellm.dsh.yaml`; use thinking passthrough and
the opt-in empty tool-delta identity compatibility flag documented there.

## Config profiles (switchable)

| File | Role |
|---|---|
| `.env` | **Default** upstream (current / unchanged) |
| `.env.penguin` | Named copy of the current penguin/anthropic upstream |
| `.env.aliyun_maas` | Aliyun MaaS **Anthropic** (`…/apps/anthropic`, `deepseek-v4-flash-0731`) — use for Claude |
| `.env.aliyun_maas_openai` | Aliyun MaaS OpenAI-compatible (`…/compatible-mode/v1`) — Insurance-only / chat experiments |
| `.env.insurance_4002` | Insurance lane gateway on port 4002 (`LANE_POOL=insurance`); managed by `llmtrace gateway` |
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

Every gateway, including the 4001 profiles and `opencode_4003`, is started and stopped with one command
(from the repo root; details in the next section):

```bash
./llmtrace gateway list                       # all .env* files: port, state, log dir
./llmtrace gateway up default                 # .env on :4001
./llmtrace gateway up aliyun_maas --force     # switch :4001 to another profile (stops the running one)
./llmtrace gateway up opencode_4003           # OpenCode relay on :4003
./llmtrace gateway down opencode_4003
```

The 4001 profiles (`.env`, `.env.aliyun_maas`, `.env.bailian`, ...) are alternatives for one port: only one runs at a time.
`gateway up` refuses while another profile holds the port and names it; `--force` switches.
`list` shows the others as `down(:4001 used by <name>)`. Starting a 4001 profile records it in
`run/active_profile` / `run/active_env_file`, which `eval_harness.suite` reads.

`start_litellm.sh [--profile NAME] [--restart]` and `stop_litellm.sh` remain as compatibility wrappers over the same
commands (the start wrapper always switches, like before; `LITELLM_SYNC_CLAUDE_SETTINGS=1` still syncs Claude settings).
`start_litellm.sh --profile NAME --foreground` still runs LiteLLM in the foreground without pid/state management.

## Named gateways and lanes (`llmtrace gateway`)

One `.env.<name>` file is one gateway process; `<name>` is its only identity.
`llmtrace` (repo root) derives everything else from it:

| Derived | Path |
|---|---|
| pid / state | `run/gateways/<name>.pid`, `run/gateways/<name>.state.json` |
| call log | `logs/gateways/<name>/llm_calls.jsonl` (+ `litellm.stdout.log`) |
| lane registry | `run/attribution/<name>.json` |
| ready marker | `run/attribution/gateway_ready.port-<port>.json` |

A gateway is an attribution **lane** when its env file adds:

| Var | Meaning |
|---|---|
| `LANE_POOL` | Pool name; the runner uses every ready gateway in the pool (`--lanes insurance`) |
| `LANE_TARGET_URL` | Business instance this gateway serves (e.g. `http://127.0.0.1:18063/v1/chat`) |

Calls reaching a lane gateway are attributed to the case currently leased on
that lane, so the business side needs no headers and may reuse one key: run N
business instances, each pointing its model `base_url` at its own gateway.
Explicit `X-Eval-*` ids still win over the lane. `LLM_GATEWAY_LOG_DIR` is
optional (non-lane gateways keep using it as before).

```bash
./llmtrace gateway list                 # all gateways, status, lane, log
./llmtrace gateway check                # port / target / field conflicts
./llmtrace gateway show insurance_4002  # derived paths; secrets masked (--reveal)
./llmtrace gateway up insurance         # start every gateway in pool "insurance"
./llmtrace gateway restart insurance_4002 [--force]   # --force: also stop a foreign listener
./llmtrace gateway down insurance
./llmtrace report html --gateway insurance_4002 --out eval_runs/x --executions ID
./llmtrace lane recover insurance_4002 --execution-id ID   # after a blocked case really stopped
```

Query and rotation (read-only local files, no network service):

```bash
./llmtrace query calls --execution-id ID                   # all live logs under logs/
./llmtrace query calls --run-id R --case-id C --gateway insurance_4002 --format jsonl
./llmtrace query calls --execution-id ID --include-rotated # also llm_calls_*.jsonl.gz
./llmtrace query calls --since 30m --gateway insurance_4002    # time window (lane + window, no ids)
./llmtrace gateway rotate insurance_4002 --keep 10         # safe while running; refused while leased
```

For external consumers (collectors), use the JSON contract `llmtrace.calls/v1`:

```bash
./llmtrace query cursor --gateway insurance_4002 > cursor.json        # at prepare
./llmtrace query calls --execution-id ID --gateway insurance_4002 \
    --cursor cursor.json --settle 30 --format json [--events]          # at collect
```

- `status`: `complete` (every matched call ended, log quiet for `--quiet` s) |
  `partial` (timed out with calls in flight) | `no_calls` | `failed`. Exit 0 / 3 / 1 / 2.
- `counts` {total, pending, succeeded, failed}, `pending_call_ids`, `conflicts`
  (one execution_id seen with several case_ids), `rotated`, `cursor` (feed back as `--cursor`).
- `calls[]`: one merged record per call with `state` (pending | succeeded | failed),
  ids and their sources, `trace_id`, status/latency/usage/error, `warnings`;
  `--events` adds the raw gateway records.
- `--execution-id` also matches a call's inbound W3C `trace_id`. Output is credential-redacted.
- The cursor only skips bytes already read; rotation is detected (inode/size) and the
  archives written since are scanned, so results stay correct across `gateway rotate`.

Library: `eval_harness.query.collect(...)` returns the JSON envelope;
`query_calls(execution_ids=[...], gateway=..., since=..., cursor=...)`
returns joined call records (same shape `load_gateway_pairs` yields). `--format jsonl`
prints rows in the per-case `llm_calls.jsonl` shape.

Adding a second Insurance instance: copy `.env.insurance_4002` to
`.env.insurance_4004`, change `LITELLM_PORT` and `LANE_TARGET_URL`, then
`./llmtrace gateway check && ./llmtrace gateway up insurance`. Start the business
instance with `INSURANCE_QA_PORT=18064 INSURANCE_QA_MODEL_BASE_URL=http://127.0.0.1:4004/v1`
(`scripts/start_insurance_qa_local.py` overrides the checkout's model base_url; the
parallel script does this per lane automatically).
The runner runs one case per lane concurrently; a lane whose case ends
`blocked` is dropped for the rest of the run (its remaining cases count as unrun).

`start_insurance_litellm.sh` is now a wrapper for `gatewayctl up` (hands over
from the old launcher's pid once). `attribution_lanes.json` is legacy: used only
when a caller passes `--attribution-config` explicitly.

### Stream failures and error records

Both launchers default `DISABLE_AIOHTTP_TRANSPORT=True`, LiteLLM's supported
httpx transport switch. In the tested LiteLLM 1.74.8 environment, the default
aiohttp transport silently ends a truncated chunked response, producing a false
successful completion. No third-party package source is patched. This setting
applies when a gateway next starts; existing processes need a restart. An explicit
environment/profile override is respected for diagnostics.

Failure callbacks capture the exception type/message, observed HTTP status and
latency. A transport error with no known HTTP status remains `null`, rather than
an invented 500. For a Chat Completions stream interrupted after output began,
`stream_partial` preserves received text, reasoning and tool argument fragments;
the report labels these as incomplete. Fragments do not imply a completed tool
call. The client may already have received HTTP 200 headers: it must process the
SSE error event rather than treating those headers alone as successful completion.
