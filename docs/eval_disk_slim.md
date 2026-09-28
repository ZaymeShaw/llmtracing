# Eval artifact disk slim

## What `llm_trace.html` depends on

| Phase | Needs on disk |
|-------|----------------|
| **View** | Only `llm_trace.html`. Payload is fully inlined in `<script id="DATA">`. |
| **Rebuild HTML** | `cases/<id>/llm_calls.jsonl` **or** `.jsonl.gz`; plus `trace.json` / Insurance `meta.json` when present. Optional enrichment: `stream.jsonl`, `events.jsonl`, runner meta. |
| **Rebuild Excel** | `trace.json` + `llm_calls.jsonl(.gz)`. |

**Not** read at HTML rebuild/view: `stream_turn*.jsonl`, `stream_pi.jsonl`, `*_mapped.jsonl`, `*_attempt*.jsonl`.

## Safe vs unsafe

**Safe (script default):**

- Delete `stream_turnN.jsonl` when `stream.jsonl` exactly matches harness `concat_streams` reconstruction.
- Delete `stream_pi.jsonl` when byte-identical to `stream.jsonl`.
- Delete `stream_turn*_mapped.jsonl` and `stream_turn*_attempt*.jsonl`.
- Gzip `llm_calls.jsonl` → `llm_calls.jsonl.gz` after `llm_trace.html` exists (loaders accept `.gz`).

**Keep:**

- `llm_trace.html`, `results.xlsx`, `stream.jsonl`, `events.jsonl`, `trace.json`, `meta.json`, responses, `llm_calls.jsonl.gz`.

**Unsafe / deferred:**

- Do not slim an in-flight run’s live case files.
- Do not delete active `llm_gateway/logs/llm_calls.jsonl`.
- Rotated `llm_calls.jsonl.rotated_*.gz`: optional only **after** all cases have per-case `llm_calls` and no run needs manual re-ingest (`--gateway-rotated`). Ingest code reads the live jsonl only, not the rotated gz.

## How to slim future runs

```bash
# dry-run
python3 scripts/slim_eval_artifacts.py eval_runs/<run_or_parent>

# apply (streams + gzip llm_calls)
python3 scripts/slim_eval_artifacts.py eval_runs/<run_or_parent> --apply --gzip-llm-calls
```

In-flight stamp `115051_aliyun_maas4001_claude120` is refused by default. After that run finishes:

```bash
python3 scripts/slim_eval_artifacts.py eval_runs/dual_datasetA_20260927_115051_aliyun_maas4001_claude120 \
  --allow-live --apply --gzip-llm-calls
# optional, only when no recovery needed:
#   --gateway-rotated --gateway-logs-dir llm_gateway/logs
```
