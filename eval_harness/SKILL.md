---
name: claude-code-eval-harness
description: >-
  Batch-run llmtracing Dataset Bundle cases through Claude Code CLI, capture
  full Traces (thinking, tools, multi-turn, timing, first-frame), write
  results.xlsx + per-case JSON. Use when evaluating Claude Code on insurance
  mock tools or regenerating eval_runs. No scoring. Scripts only read standard
  Bundle + Profile I/O; markdown import and harness dialect mapping are Skill/adapters.
---

# Claude Code Eval Harness

## Standard I/O (v1)

Scripts freeze only these contracts (see `schemas/`):

1. **Dataset Bundle** (`bundles/*.jsonl`) — cases + turns
2. **Harness Profile** (`configs/profiles/*.yaml`) — cwd / bin / agent / mcp / permissions
3. **Trace** (`eval_runs/<run>/cases/<id>/trace.json`) — normalized events + metrics

Markdown/xlsx/飞书 layouts are **not** runner input. Import them into a Bundle first.

## When to use

- Eval / smoke / batch-run Claude Code on the 120-题 set
- Need traces for tool-calling / multi-turn under `llmtracing`
- New dataset format → write/adjust importer, regenerate Bundle, schema-validate
- New harness CLI dialect → adjust adapter mapping into Trace kinds

## Preconditions

- Mac project cwd from Profile (`project_cwd`)
- Auth only via cwd local settings — never copy secrets into traces
- Harness at `{repo_root}/eval_harness/`; outputs `{repo_root}/eval_runs/`.
- `project_cwd` is the isolated `workspaces/claude/` directory, separate from the repository root.

## Steps

1. Confirm Profile: `configs/profiles/claude.yaml`
2. If dataset text changed, re-import Bundle:
   ```bash
   cd {repo_root}/eval_harness
   PYTHONPATH=src python3 -m eval_harness.import_bundle \
     --md data/120题-仅问句版\(1\).md \
     --out bundles/120_prompt_only_v1.jsonl \
     --bundle-id 120_prompt_only_v1
   ```
3. Dry-parse Bundle:
   ```bash
   PYTHONPATH=src python3 -m eval_harness.run \
     --config configs/runs/claude.yaml --cases A01,E01 --dry-parse
   ```
4. Smoke:
   ```bash
   PYTHONPATH=src python3 -m eval_harness.run \
     --config configs/runs/claude.yaml --cases A01,E01 --run-id smoke_A01_E01
   ```
5. Full serial run only after smoke OK:
   ```bash
   PYTHONPATH=src python3 -m eval_harness.run --config configs/runs/claude.yaml --all
   ```
6. Report: run dir, `results.xlsx`, thinking/tool presence, auth/quota errors without secrets

## Do not

- Do not score / compare to gold in this harness
- Do not feed raw markdown to the runner
- Do not put API tokens into Trace / Excel

## Local relay Trace (analysis)

- Contract: `../docs/local_relay_trace_scheme.md`
- Connect any harness: `../docs/local_relay_harness_connect.md`
- Inject helpers: `src/eval_harness/relay_inject.py`
- Wire-only HTML: `python -m eval_harness.llm_trace_html --from-gateway-log …`
