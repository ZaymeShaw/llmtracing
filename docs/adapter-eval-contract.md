# Adapter eval relay contract (dual-id)

Any harness adapter that sends traffic through the local relay
(`llm_gateway`, default LiteLLM `:4001` / Insurance `:4002`) must meet this
standard so traces attribute correctly under concurrency.

Related: [local_relay_harness_connect.md](local_relay_harness_connect.md),
[local_relay_trace_scheme.md](local_relay_trace_scheme.md),
[adding_harness.md](adding_harness.md).

## Standard

1. **Mint and send both ids**
   - `case_id` — stable case / question id (e.g. `A01`, `INS_A01`).
   - `execution_id` — unique per case *run/request* (new UUID hex on each
     attempt; shared across turns and model retries within that run).

2. **Wire delivery to the relay**
   - Preferred headers (gateway already extracts these):
     - `X-Eval-Case-Id`
     - `X-Eval-Execution-Id`
   - Documented equivalents also accepted where the gateway already looks
     (e.g. body / `metadata.eval_case_id`, `metadata.eval_execution_id`;
     Claude text marker `<!--eval_case_id:…-->` for case only).
   - Helpers: `eval_harness.relay_inject`
     (`openai_compatible_kwargs`, `apply_openai_compatible_case_id`,
     `anthropic_cli_env`).

3. **Gateway attribution**
   - When **both** headers (or equivalents) are present, the gateway treats
     attribution as **explicit** and **skips the lane registry**.
   - Lane is a **fallback only** when ids are incomplete (e.g. case-only).

4. **Thinking-at-relay (P1 — implemented)**
   - Default **OFF** at the local relay (`:4001` / `:4002`).
   - Caller override **ON** via:
     - Header `X-Eval-Thinking: on` (Claude `ANTHROPIC_CUSTOM_HEADERS`,
       Pi `$PI_EVAL_THINKING` → `models.json`, OpenAI-compat helpers), or
     - Model alias suffix `-think` (e.g. `deepseek-v4-flash-0731-think` on `:4002`).
   - `:4001` (Anthropic): `async_pre_call_hook` forces
     `thinking: {"type":"disabled"}` unless opt-in (Claude otherwise sends
     `adaptive`, which would win over deployment config).
   - `:4002` (Bailian OpenAI): deployment `extra_body.enable_thinking: false`
     in `config.litellm.insurance.yaml`; caller / opt-in wins.
   - Auditable: gateway `pre_api_call` records `thinking_effective` and
     `thinking_source`.
   - **Do not confuse** with Insurance upstream
     `models_common_args.yaml` `enable_thinking: false` (agent-side commit).

5. **Missing-header behavior**
   - Missing / incomplete ids → orphan or lane fallback; never invent
     attribution from filesystem or process state alone.
   - Case-only (no `execution_id`) must not steal another run’s lane under
     concurrency; prefer minting `execution_id` whenever `case_id` is sent.

## New-adapter checklist

- [ ] Mint `case_id` + unique `execution_id` per run (persist under
      `cases/<case_id>/` when applicable, e.g. `execution_id.txt`).
- [ ] Send both on the wire as `X-Eval-Case-Id` + `X-Eval-Execution-Id`
      (or documented gateway-equivalent).
- [ ] Confirm gateway takes the **explicit** path (no lane) when both present.
- [ ] Unit / smoke: concurrent cases keep distinct `execution_id`s; no
      cross-attribution.
- [ ] Document missing-header / incomplete-id behavior for this adapter.
- [x] (P1) Thinking-at-relay: default off, caller override via X-Eval-Thinking / *-think.

## Current adapters (P0 bar)

| Adapter   | Dual-id mint | Wire path |
|-----------|--------------|-----------|
| Insurance | yes          | HTTP `X-Eval-*` on `/v1/chat` (+ OpenAI-compat helpers) |
| Pi        | yes (`PI_EVAL_*`) | `~/.pi/agent/models.json` headers for `local-relay` / `local-relay-bailian` |
| Claude    | yes          | `ANTHROPIC_CUSTOM_HEADERS` + `CLAUDE_CODE_EXTRA_BODY` via `anthropic_cli_env` |
