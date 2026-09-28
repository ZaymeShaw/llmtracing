#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
# shellcheck disable=SC1091
source "$DIR/.venv/bin/activate"

# Additive --profile: load .env.NAME instead of .env.
# Never touches :4001 / Claude. Insurance LiteLLM only on :4002.
PROFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile|-p)
      PROFILE="${2:-}"; shift 2 ;;
    -h|--help)
      cat <<'HELP'
Usage: ./start_insurance_litellm.sh [--profile NAME]

  Starts dedicated Insurance LiteLLM on :4002 only (does not restart :4001).

  (no args)       load .env
  --profile NAME  load .env.NAME (e.g. bailian, bailian_openai, aliyun_maas, penguin)
HELP
      exit 0 ;;
    *)
      if [[ -z "$PROFILE" && -f "$DIR/.env.$1" ]]; then PROFILE="$1"; shift
      else echo "unknown arg: $1" >&2; exit 2; fi ;;
  esac
done

# Sticky profile: if dual_run/preflight calls us with no --profile, keep the
# last Insurance upstream (e.g. bailian_openai) instead of clobbering with .env.
# Explicit --profile always wins. Never touches :4001.
ENV_FILE="$DIR/.env"
if [[ -z "$PROFILE" && -f "$DIR/run/active_insurance_profile" ]]; then
  sticky="$(cat "$DIR/run/active_insurance_profile" 2>/dev/null || true)"
  if [[ -n "$sticky" && "$sticky" != "default" && -f "$DIR/.env.$sticky" ]]; then
    PROFILE="$sticky"
    echo "Insurance LiteLLM sticky profile=$PROFILE (from run/active_insurance_profile)"
  fi
fi
if [[ -n "$PROFILE" ]]; then
  ENV_FILE="$DIR/.env.$PROFILE"
  [[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; exit 1; }
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
echo "Insurance LiteLLM env=$ENV_FILE profile=${PROFILE:-default}"

export LITELLM_HOST=127.0.0.1
export LITELLM_PORT=4002
export LITELLM_MASTER_KEY="${INSURANCE_LITELLM_MASTER_KEY:?missing Insurance gateway key}"
export UPSTREAM_LITELLM_MODEL="${UPSTREAM_PROTOCOL}/${UPSTREAM_MODEL}"
export ANTHROPIC_API_KEY="$UPSTREAM_API_KEY"
# Proxy bypass for *.aliyuncs.com upstreams (see start_litellm.sh for rationale).
_up_host="$(printf '%s' "$UPSTREAM_API_BASE" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')"
if [[ "${GATEWAY_DIRECT_UPSTREAM:-auto}" == "1" || ( "${GATEWAY_DIRECT_UPSTREAM:-auto}" == "auto" && "$_up_host" == *.aliyuncs.com ) ]]; then
  export NO_PROXY="127.0.0.1,localhost,::1,.aliyuncs.com${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="$NO_PROXY"
  echo "upstream $_up_host: direct (NO_PROXY set, system proxy bypassed)"
fi
export LLM_GATEWAY_LOG_DIR="$DIR/logs"
export LLM_ATTRIBUTION_CONFIG="$DIR/attribution_lanes.json"
export LLM_ATTRIBUTION_GATEWAY_PROCESS=1
export LLM_GATEWAY_PROFILE="${PROFILE:-default}"
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"

PID_FILE="$DIR/run/litellm_insurance.pid"
DIAG_LOG="$DIR/logs/litellm_insurance.stdout.log"
CFG="${LITELLM_CONFIG:-$DIR/config.litellm.insurance.yaml}"
mkdir -p "$DIR/run" "$DIR/logs"

# Record active insurance upstream (no secrets)
echo "${PROFILE:-default}" >"$DIR/run/active_insurance_profile"
echo "$ENV_FILE" >"$DIR/run/active_insurance_env_file"
echo "$UPSTREAM_API_BASE" >"$DIR/run/active_insurance_upstream_base"

# If healthy but we want a specific profile, restart :4002 only when profile requested
if curl -fsS -m 2 "http://$LITELLM_HOST:$LITELLM_PORT/v1/models" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" >/dev/null 2>&1; then
  prev_profile="$(cat "$DIR/run/active_insurance_profile" 2>/dev/null || true)"
  prev_base="$(cat "$DIR/run/active_insurance_upstream_base" 2>/dev/null || true)"
  want_profile="${PROFILE:-default}"
  if [[ "$prev_profile" == "$want_profile" && "$prev_base" == "$UPSTREAM_API_BASE" && "${FORCE_INSURANCE_RESTART:-0}" != "1" ]]; then
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
      echo "Insurance LiteLLM already healthy on :$LITELLM_PORT profile=$want_profile"
      # refresh markers
      echo "$want_profile" >"$DIR/run/active_insurance_profile"
      echo "$ENV_FILE" >"$DIR/run/active_insurance_env_file"
      echo "$UPSTREAM_API_BASE" >"$DIR/run/active_insurance_upstream_base"
      exit 0
    fi
    echo "Insurance attribution callback not ready on :$LITELLM_PORT; restarting"
  fi
  if [[ "${FORCE_INSURANCE_RESTART:-0}" == "1" ]]; then
    echo "FORCE_INSURANCE_RESTART=1 — restarting :4002"
  fi
  echo "Restarting :4002 to switch profile $prev_profile -> $want_profile (Claude :4001 untouched)"
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
pid="$(cat "$PID_FILE" 2>/dev/null || true)"
echo "Insurance LiteLLM healthy pid=$pid port=$LITELLM_PORT upstream=$UPSTREAM_API_BASE protocol=$UPSTREAM_PROTOCOL model=$UPSTREAM_MODEL cfg=$CFG"
