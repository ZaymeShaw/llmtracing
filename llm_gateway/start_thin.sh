#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PROFILE="${1:-thin_4000}"
ENV_FILE="$DIR/.env.$PROFILE"
[[ -f "$ENV_FILE" ]] || { echo "missing thin proxy profile: $ENV_FILE" >&2; exit 1; }
for key in LLM_GATEWAY_UPSTREAM LLM_GATEWAY_HOST LLM_GATEWAY_PORT LLM_GATEWAY_LOG_DIR; do
  grep -Eq "^(export )?${key}=" "$ENV_FILE" || { echo "missing $key in $ENV_FILE" >&2; exit 1; }
done
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
mkdir -p "$LLM_GATEWAY_LOG_DIR" "$DIR/run"
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
if [[ -f "$DIR/run/thin.pid" ]] && kill -0 "$(cat "$DIR/run/thin.pid")" 2>/dev/null; then
  echo "thin proxy already running; stop it before loading $ENV_FILE" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$DIR/.venv/bin/activate"
nohup python "$DIR/thin_proxy.py" --port "$LLM_GATEWAY_PORT" >"$LLM_GATEWAY_LOG_DIR/thin_proxy.stdout.log" 2>&1 &
echo $! >"$DIR/run/thin.pid"
sleep 0.8
echo "started thin proxy pid=$(cat "$DIR/run/thin.pid") http://$LLM_GATEWAY_HOST:$LLM_GATEWAY_PORT upstream=$LLM_GATEWAY_UPSTREAM"
