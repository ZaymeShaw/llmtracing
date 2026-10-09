#!/usr/bin/env bash
# Compatibility wrapper: the Insurance gateway is now a named gateway managed by
# gatewayctl (one .env.<name> == one process). Prefer: llmtrace gateway up insurance_4002
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE="insurance_4002"
FORCE=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile|-p) PROFILE="${2:-}"; shift 2 ;;
    --force) FORCE=(--force); shift ;;
    -h|--help)
      cat <<'HELP'
Usage: ./start_insurance_litellm.sh [--profile NAME] [--force]

  Starts gateway NAME (default insurance_4002) via gatewayctl; no-op when it is
  already up with an unchanged config. FORCE_INSURANCE_RESTART=1 restarts it.
  --force  also stop a process on the port that llmtrace did not start.
HELP
      exit 0 ;;
    *)
      if [[ -f "$DIR/.env.$1" ]]; then PROFILE="$1"; shift
      else echo "unknown arg: $1" >&2; exit 2; fi ;;
  esac
done

# One-time handover from the old launcher: its pid file identifies a process this
# script started, so stopping it is not "foreign" (other listeners still need --force).
LEGACY_PID_FILE="$DIR/run/litellm_insurance.pid"
if [[ -f "$LEGACY_PID_FILE" ]]; then
  old_pid="$(cat "$LEGACY_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "stopping legacy Insurance launcher pid=$old_pid"
    kill "$old_pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$old_pid" 2>/dev/null || break; sleep 0.5; done
  fi
  rm -f "$LEGACY_PID_FILE" "$DIR"/run/active_insurance_*
fi

ACTION=up
[[ "${FORCE_INSURANCE_RESTART:-0}" == "1" ]] && ACTION=restart
exec python3 "$DIR/gatewayctl.py" "$ACTION" "$PROFILE" ${FORCE[@]+"${FORCE[@]}"}
