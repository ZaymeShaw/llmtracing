#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATEWAY="$ROOT/llm_gateway"
INSURANCE_REPO="/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent"
DUAL_ROOT="$ROOT/eval_runs/dual_datasetA_20260924_150423"
BUNDLE="$ROOT/eval_harness/bundles/120_prompt_only_v1.jsonl"
set -a
# shellcheck disable=SC1091
source "$GATEWAY/.env.insurance_4002"
set +a
GATEWAY_LOG="$LLM_GATEWAY_LOG_DIR/llm_calls.jsonl"
export UPSTREAM_LITELLM_MODEL="${UPSTREAM_PROTOCOL}/${UPSTREAM_MODEL}"
export ANTHROPIC_API_KEY="$UPSTREAM_API_KEY"
export LLM_ATTRIBUTION_CONFIG="$GATEWAY/attribution_lanes.json"
export LLM_ATTRIBUTION_GATEWAY_PROCESS=1
export PYTHONPATH="$GATEWAY${PYTHONPATH:+:$PYTHONPATH}"
export NO_PROXY=127.0.0.1,localhost
export no_proxy="$NO_PROXY"

gateway_pid=""
insurance_pid=""
cleanup() {
  [[ -z "$insurance_pid" ]] || kill "$insurance_pid" 2>/dev/null || true
  [[ -z "$gateway_pid" ]] || kill "$gateway_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

old_insurance_pid=""
if [[ -f /Users/xiaozijian/.insurance-qa-agent-real/server-18063.pid ]]; then
  old_insurance_pid="$(cat /Users/xiaozijian/.insurance-qa-agent-real/server-18063.pid 2>/dev/null || true)"
fi
if [[ -n "$old_insurance_pid" ]] && kill -0 "$old_insurance_pid" 2>/dev/null; then
  kill "$old_insurance_pid"
  sleep 1
fi

# A cancelled foreground run can briefly leave its dedicated listener alive.
# Clear only the configured Insurance listener; Claude remains isolated.
if command -v lsof >/dev/null 2>&1; then
  while read -r stale_gateway_pid; do
    [[ -z "$stale_gateway_pid" ]] || kill "$stale_gateway_pid" 2>/dev/null || true
  done < <(lsof -nP -iTCP:"$LITELLM_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
  sleep 1
fi

cd "$GATEWAY"
"$GATEWAY/.venv/bin/litellm" --config "$LITELLM_CONFIG" \
  --host "$LITELLM_HOST" --port "$LITELLM_PORT" --telemetry False \
  >>"$LLM_GATEWAY_LOG_DIR/litellm_insurance.stdout.log" 2>&1 &
gateway_pid=$!
echo "$gateway_pid" >"$GATEWAY/run/litellm_insurance.pid"

cd "$INSURANCE_REPO"
ENV_TYPE=dev INSURANCE_QA_PORT=18063 "$INSURANCE_REPO/.feval_venv/bin/python" \
  "$ROOT/scripts/start_insurance_qa_local.py" \
  >>"$ROOT/eval_runs/insurance_qa_server_20260924.log" 2>&1 &
insurance_pid=$!

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  gateway_ok=0
  insurance_ok=0
  curl -fsS -m 2 "http://$LITELLM_HOST:$LITELLM_PORT/v1/models" \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" >/dev/null 2>&1 && gateway_ok=1
  curl -fsS -m 2 http://127.0.0.1:18063/health >/dev/null 2>&1 && insurance_ok=1
  if [[ "$gateway_ok" -eq 1 && "$insurance_ok" -eq 1 ]]; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "Insurance parallel services failed health check" >&2
  exit 1
fi

dedicated_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" "http://$LITELLM_HOST:$LITELLM_PORT/v1/models")"
wrong_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H 'Authorization: Bearer invalid-key-for-healthcheck' "http://$LITELLM_HOST:$LITELLM_PORT/v1/models")"
echo "[parallel] gateway=$LITELLM_PORT dedicated_key_http=$dedicated_status shared_key_http=$wrong_status"

cases="${INSURANCE_CASES:-}"
if [[ -z "$cases" ]]; then
  cases="$(/opt/homebrew/bin/python3 -c 'import json,sys; print(",".join(json.loads(x)["case_id"] for x in open(sys.argv[1]) if x.strip()))' "$BUNDLE")"
fi
cd "$ROOT/eval_harness"
export PYTHONPATH="$ROOT/eval_harness/src:$GATEWAY"
exec "$INSURANCE_REPO/.feval_venv/bin/python" -m eval_harness.adapters.insurance \
  --live-batch \
  --resume \
  --cases "$cases" \
  --run-id insurance \
  --chat-url http://127.0.0.1:18063/v1/chat \
  --bundle "$BUNDLE" \
  --eval-runs-dir "$DUAL_ROOT" \
  --gateway-log "$GATEWAY_LOG" \
  --lane insurance_1 \
  --attribution-config "$GATEWAY/attribution_lanes.json"
