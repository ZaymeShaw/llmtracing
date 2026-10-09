#!/usr/bin/env bash
# Compatibility wrapper. Prefer: llmtrace gateway down <name>
# Stops whichever gateway profile holds the primary port (LITELLM_PORT, default 4001),
# plus any leftover listener there (the old script's behaviour).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${LITELLM_PORT:-4001}"
names="$(python3 "$DIR/gatewayctl.py" list --json | python3 -c '
import json, sys
port = sys.argv[1]
print(" ".join(r["name"] for r in json.load(sys.stdin) if r["port"] == port))' "$PORT")"
[[ -n "$names" ]] || { echo "no gateway configured on :$PORT" >&2; exit 1; }
set -- $names
for n in "$@"; do python3 "$DIR/gatewayctl.py" down "$n"; done
# Then clear any listener left on the port by a pre-gatewayctl launcher.
python3 "$DIR/gatewayctl.py" down "$1" --force
# Pre-gatewayctl launcher state.
rm -f "$DIR/run/litellm.pid"
