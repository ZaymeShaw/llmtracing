#!/usr/bin/env bash
# Install eval_harness into llmtracing on the Mac.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-$SCRIPT_DIR}"
mkdir -p "$TARGET/eval_runs"
if [[ "$(cd "$TARGET" && pwd)" != "$SCRIPT_DIR" ]]; then
  rsync -a --exclude data --exclude bundles --exclude __pycache__ "$SCRIPT_DIR/eval_harness/" "$TARGET/eval_harness/"
  mkdir -p "$TARGET/workspaces/claude" "$TARGET/workspaces/pi"
  cp "$SCRIPT_DIR/workspaces/claude/agent.md" "$TARGET/workspaces/claude/agent.md"
  cp "$SCRIPT_DIR/workspaces/pi/agent.md" "$TARGET/workspaces/pi/agent.md"
fi
echo "Installed -> $TARGET/eval_harness"
echo "eval_runs -> $TARGET/eval_runs"
echo
echo "Smoke:"
echo "  cd \"$TARGET\""
echo "  export PYTHONPATH=eval_harness/src"
echo "  python3 -m eval_harness.run --config eval_harness/configs/runs/claude.yaml --cases A01,E01"
echo
echo "Full:"
echo "  python3 -m eval_harness.run --config eval_harness/configs/runs/claude.yaml --all"
