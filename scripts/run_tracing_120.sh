#!/usr/bin/env bash
# No arguments: validate local setup only. --smoke / --all execute real model calls.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${LLMTRACE_PYTHON:-$ROOT/llm_gateway/.venv/bin/python}"
exec "$PYTHON" "$ROOT/scripts/run_tracing_120.py" "$@"
