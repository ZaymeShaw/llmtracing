#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
# shellcheck disable=SC1091
source "$DIR/.venv/bin/activate"

PROFILE=""
FORCE_RESTART=0
FOREGROUND=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile|-p)
      PROFILE="${2:-}"
      shift 2
      ;;
    --restart|--force)
      FORCE_RESTART=1
      shift
      ;;
    --foreground)
      FOREGROUND=1
      shift
      ;;
    -h|--help)
      cat <<'HELP'
Usage: ./start_litellm.sh [--profile NAME] [--restart] [--foreground]

  (no args)           load .env (current default upstream)
  --profile NAME      load .env.NAME instead (e.g. aliyun_maas, penguin)
  --restart           stop existing listener even if healthy (needed when switching)
  --foreground        run this profile in the current terminal without touching another listener/PID

Examples:
  ./stop_litellm.sh && ./start_litellm.sh
  ./stop_litellm.sh && ./start_litellm.sh --profile aliyun_maas
  ./start_litellm.sh --profile aliyun_maas --restart
HELP
      exit 0
      ;;
    *)
      # bare name as profile for convenience: ./start_litellm.sh aliyun_maas
      if [[ -z "$PROFILE" && -f "$DIR/.env.$1" ]]; then
        PROFILE="$1"
        shift
      else
        echo "unknown arg: $1 (try --help)" >&2
        exit 2
      fi
      ;;
  esac
done

ENV_FILE="$DIR/.env"
if [[ -n "$PROFILE" ]]; then
  ENV_FILE="$DIR/.env.$PROFILE"
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "profile env missing: $ENV_FILE" >&2
    echo "available:" >&2
    ls -1 "$DIR".env.* 2>/dev/null | sed 's|.*/.env.||' | grep -v example || true
    # fix ls path
    ls -1 "$DIR"/.env.* 2>/dev/null | xargs -n1 basename | sed 's/^\.env\.//' | grep -v '^example$' >&2 || true
    exit 1
  fi
fi

[[ -f "$ENV_FILE" ]] || { echo "missing relay profile: $ENV_FILE" >&2; exit 1; }
for key in UPSTREAM_API_BASE UPSTREAM_API_KEY UPSTREAM_PROTOCOL UPSTREAM_MODEL LITELLM_HOST LITELLM_PORT LITELLM_MASTER_KEY LLM_GATEWAY_LOG_DIR LITELLM_CONFIG; do
  grep -Eq "^(export )?${key}=" "$ENV_FILE" || { echo "missing $key in $ENV_FILE" >&2; exit 1; }
done
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
echo "loaded env: $ENV_FILE"

export UPSTREAM_LITELLM_MODEL="${UPSTREAM_PROTOCOL}/${UPSTREAM_MODEL}"
export ANTHROPIC_API_KEY="$UPSTREAM_API_KEY"
# LiteLLM 1.74.8's aiohttp transport swallows truncated chunked responses.
# Its supported httpx transport propagates the error instead of emitting DONE.
export DISABLE_AIOHTTP_TRANSPORT="${DISABLE_AIOHTTP_TRANSPORT:-True}"

# Proxy bypass for Aliyun upstreams. On macOS Python/aiohttp/httpx fall back to the *system*
# proxy (scutil: 127.0.0.1:7890) when no proxy env is set; a flaky/stopped local proxy caused
# "Server disconnected" / "Cannot connect to host 127.0.0.1:7890" -> HTTP 500 (run
# triple_0922shared_nothink_20260927_214436). Setting NO_PROXY makes Python ignore the system
# proxy entirely, so only do it for *.aliyuncs.com upstreams (penguin keeps legacy behaviour).
# Override: GATEWAY_DIRECT_UPSTREAM=0 (keep proxy) / =1 (force direct).
_up_host="$(printf '%s' "$UPSTREAM_API_BASE" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')"
if [[ "${GATEWAY_DIRECT_UPSTREAM:-auto}" == "1" || ( "${GATEWAY_DIRECT_UPSTREAM:-auto}" == "auto" && "$_up_host" == *.aliyuncs.com ) ]]; then
  export NO_PROXY="127.0.0.1,localhost,::1,.aliyuncs.com${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="$NO_PROXY"
  echo "upstream $_up_host: direct (NO_PROXY set, system proxy bypassed)"
fi

# Header-based tracing needs no lane configuration. Insurance enables its
# exclusive lane explicitly in start_insurance_litellm.sh.

if [[ -z "$UPSTREAM_API_KEY" ]]; then
  echo "UPSTREAM_API_KEY missing — set it in $ENV_FILE" >&2
  exit 1
fi
if [[ -z "$LITELLM_MASTER_KEY" ]]; then
  echo "LITELLM_MASTER_KEY missing — set it in $ENV_FILE" >&2
  exit 1
fi

export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
if [[ "${LITELLM_STDOUT_DEBUG:-0}" == "1" ]]; then
  export LITELLM_LOG="${LITELLM_LOG:-DEBUG}"
else
  export LITELLM_LOG=INFO
fi
mkdir -p "$DIR/logs" "$DIR/run"

ACTIVE_FILE="$DIR/run/active_profile"
PREV_PROFILE=""
[[ -f "$ACTIVE_FILE" ]] && PREV_PROFILE="$(cat "$ACTIVE_FILE" 2>/dev/null || true)"
NEW_PROFILE="${PROFILE:-default}"
export LLM_GATEWAY_PROFILE="$NEW_PROFILE"

# Switching profiles requires restart even if port looks healthy
if [[ "$FOREGROUND" -eq 0 && -n "$PREV_PROFILE" && "$PREV_PROFILE" != "$NEW_PROFILE" ]]; then
  FORCE_RESTART=1
  echo "profile change: $PREV_PROFILE -> $NEW_PROFILE (will restart)"
fi

CFG="$LITELLM_CONFIG"
[[ -f "$CFG" ]] || { echo "missing LiteLLM config: $CFG" >&2; exit 1; }
export LITELLM_CONFIG="$CFG"
PROFILE_SHA256="$(cat "$ENV_FILE" "$CFG" | shasum -a 256 | cut -d ' ' -f 1)"
PREV_SHA256="$(cat "$DIR/run/active_env_sha256" 2>/dev/null || true)"
if [[ "$FOREGROUND" -eq 0 && "$PREV_SHA256" != "$PROFILE_SHA256" ]]; then
  FORCE_RESTART=1
fi

# Relay startup is harness-independent; config synchronization is explicitly optional.
sync_claude_if_requested() {
  if [[ "${LITELLM_SYNC_CLAUDE_SETTINGS:-0}" == "1" ]]; then
    python3 "$DIR/sync_claude_settings.py"
  fi
}

if [[ "$FOREGROUND" -eq 1 ]]; then
  sync_claude_if_requested
  mkdir -p "$LLM_GATEWAY_LOG_DIR"
  export LLM_ATTRIBUTION_GATEWAY_PROCESS=1
  echo "starting profile=$NEW_PROFILE http://${LITELLM_HOST}:${LITELLM_PORT} log_dir=$LLM_GATEWAY_LOG_DIR"
  exec litellm --config "$CFG" --host "$LITELLM_HOST" --port "$LITELLM_PORT" --telemetry False
fi

# Background mode is a compatibility wrapper: gatewayctl owns pid/log/state.
# Prefer: llmtrace gateway up <name>  (this wrapper keeps the old "take over the port" behaviour).
sync_claude_if_requested
UP_ARGS=(up "$NEW_PROFILE" --force)
[[ "$FORCE_RESTART" -eq 1 ]] && UP_ARGS+=(--restart)
exec python3 "$DIR/gatewayctl.py" "${UP_ARGS[@]}"
