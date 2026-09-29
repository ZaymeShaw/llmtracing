#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
# shellcheck disable=SC1091
source "$DIR/.venv/bin/activate"

# This launcher owns the Insurance :4002 process; all connection values come from its profile.
PROFILE="insurance_4002"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile|-p)
      PROFILE="${2:-}"; shift 2 ;;
    -h|--help)
      cat <<'HELP'
Usage: ./start_insurance_litellm.sh [--profile NAME]

  Starts dedicated Insurance LiteLLM on :4002 using .env.insurance_4002.

  --profile NAME  load another dedicated .env.NAME profile configured for :4002
HELP
      exit 0 ;;
    *)
      if [[ -f "$DIR/.env.$1" ]]; then PROFILE="$1"; shift
      else echo "unknown arg: $1" >&2; exit 2; fi ;;
  esac
done

ENV_FILE="$DIR/.env.$PROFILE"
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; exit 1; }
for key in UPSTREAM_API_BASE UPSTREAM_API_KEY UPSTREAM_PROTOCOL UPSTREAM_MODEL LITELLM_HOST LITELLM_PORT LITELLM_MASTER_KEY LLM_GATEWAY_LOG_DIR LITELLM_CONFIG; do
  grep -Eq "^(export )?${key}=" "$ENV_FILE" || { echo "missing $key in $ENV_FILE" >&2; exit 1; }
done

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
echo "Insurance LiteLLM env=$ENV_FILE profile=${PROFILE:-default}"

: "${LITELLM_MASTER_KEY:?missing local gateway key}"
[[ "$LITELLM_PORT" == "4002" ]] || { echo "Insurance profile must specify LITELLM_PORT=4002: $ENV_FILE" >&2; exit 1; }
export UPSTREAM_LITELLM_MODEL="${UPSTREAM_PROTOCOL}/${UPSTREAM_MODEL}"
export ANTHROPIC_API_KEY="$UPSTREAM_API_KEY"
# Proxy bypass for *.aliyuncs.com upstreams (see start_litellm.sh for rationale).
_up_host="$(printf '%s' "$UPSTREAM_API_BASE" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')"
if [[ "${GATEWAY_DIRECT_UPSTREAM:-auto}" == "1" || ( "${GATEWAY_DIRECT_UPSTREAM:-auto}" == "auto" && "$_up_host" == *.aliyuncs.com ) ]]; then
  export NO_PROXY="127.0.0.1,localhost,::1,.aliyuncs.com${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="$NO_PROXY"
  echo "upstream $_up_host: direct (NO_PROXY set, system proxy bypassed)"
fi
export LLM_ATTRIBUTION_CONFIG="$DIR/attribution_lanes.json"
export LLM_ATTRIBUTION_GATEWAY_PROCESS=1
export LLM_GATEWAY_PROFILE="${PROFILE:-default}"
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"

PID_FILE="$DIR/run/litellm_insurance.pid"
DIAG_LOG="$LLM_GATEWAY_LOG_DIR/litellm_insurance.stdout.log"
CFG="$LITELLM_CONFIG"
[[ -f "$CFG" ]] || { echo "missing LiteLLM config: $CFG" >&2; exit 1; }
mkdir -p "$DIR/run" "$LLM_GATEWAY_LOG_DIR"

ENV_SHA256="$(cat "$ENV_FILE" "$CFG" | shasum -a 256 | cut -d ' ' -f 1)"
PREV_PROFILE="$(cat "$DIR/run/active_insurance_profile" 2>/dev/null || true)"
PREV_SHA256="$(cat "$DIR/run/active_insurance_env_sha256" 2>/dev/null || true)"

# If healthy but we want a specific profile, restart :4002 only when profile requested
if curl -fsS -m 2 "http://$LITELLM_HOST:$LITELLM_PORT/v1/models" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" >/dev/null 2>&1; then
  if [[ "$PREV_PROFILE" == "$PROFILE" && "$PREV_SHA256" == "$ENV_SHA256" && "${FORCE_INSURANCE_RESTART:-0}" != "1" ]]; then
    if python3 - "$LLM_ATTRIBUTION_CONFIG" "$PID_FILE" "$LITELLM_PORT" <<'PY'
import hashlib, json, pathlib, sys
try:
    cfg = pathlib.Path(sys.argv[1]).resolve()
    config = json.loads(cfg.read_text())
    port = sys.argv[3]
    marker_path = (cfg.parent / config.get("registry_dir", "run/attribution") / f"gateway_ready.port-{port}.json").resolve()
    marker = json.loads(marker_path.read_text())
    pid = int(pathlib.Path(sys.argv[2]).read_text())
    assert marker["pid"] == pid
    assert str(marker.get("port")) == str(port)
    assert marker["config_path"] == str(cfg)
    assert marker["config_sha256"] == hashlib.sha256(cfg.read_bytes()).hexdigest()
except (OSError, ValueError, KeyError, AssertionError):
    sys.exit(1)
PY
    then
      echo "Insurance LiteLLM already healthy on :$LITELLM_PORT profile=$PROFILE"
      # refresh markers
      echo "$PROFILE" >"$DIR/run/active_insurance_profile"
      echo "$ENV_FILE" >"$DIR/run/active_insurance_env_file"
      echo "$UPSTREAM_API_BASE" >"$DIR/run/active_insurance_upstream_base"
      echo "$ENV_SHA256" >"$DIR/run/active_insurance_env_sha256"
      exit 0
    fi
    echo "Insurance attribution callback not ready on :$LITELLM_PORT; restarting"
  fi
  if [[ "${FORCE_INSURANCE_RESTART:-0}" == "1" ]]; then
    echo "FORCE_INSURANCE_RESTART=1 — restarting :4002"
  fi
  echo "Restarting :4002 to load profile $PROFILE (Claude :4001 untouched)"
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" || true
    sleep 1
  fi
fi
# Clear only :4002 listeners
if command -v lsof >/dev/null 2>&1; then
  while read -r p; do
    [[ -z "$p" ]] || kill "$p" 2>/dev/null || true
  done < <(lsof -nP -iTCP:"$LITELLM_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
  sleep 1
fi

# Durable start (Python start_new_session; Mac Shell teardown-safe)
export LITELLM_CONFIG="$CFG"
: >>"$DIAG_LOG"
python3 "$DIR/_detach_insurance_litellm.py"
echo "$PROFILE" >"$DIR/run/active_insurance_profile"
echo "$ENV_FILE" >"$DIR/run/active_insurance_env_file"
echo "$UPSTREAM_API_BASE" >"$DIR/run/active_insurance_upstream_base"
echo "$ENV_SHA256" >"$DIR/run/active_insurance_env_sha256"
pid="$(cat "$PID_FILE" 2>/dev/null || true)"
echo "Insurance LiteLLM healthy pid=$pid port=$LITELLM_PORT upstream=$UPSTREAM_API_BASE protocol=$UPSTREAM_PROTOCOL model=$UPSTREAM_MODEL cfg=$CFG"
