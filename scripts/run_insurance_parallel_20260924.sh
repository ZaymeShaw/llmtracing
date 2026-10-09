#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSURANCE_REPO="/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent"
DUAL_ROOT="$ROOT/eval_runs/dual_datasetA_20260924_150423"
BUNDLE="$ROOT/eval_harness/bundles/120_prompt_only_v1.jsonl"
LANE="${INSURANCE_LANES:-insurance}"   # lane pool or comma-separated gateway names
LLMTRACE="$ROOT/llmtrace"
export NO_PROXY=127.0.0.1,localhost
export no_proxy="$NO_PROXY"

# Gateway lifecycle (pid, per-gateway log dir, lane ready marker) is owned by gatewayctl;
# gateways are left running afterwards. FORCE_INSURANCE_RESTART=1 restarts them.
gateway_action=up
[[ "${FORCE_INSURANCE_RESTART:-0}" == "1" ]] && gateway_action=restart
"$LLMTRACE" gateway "$gateway_action" ${LANE//,/ }

# One Insurance QA instance per lane: port from LANE_TARGET_URL, model base_url = that
# lane's gateway. Healthy instances are reused; only instances started here are stopped.
started_pids=()
cleanup() {
  for pid in "${started_pids[@]:-}"; do [[ -z "$pid" ]] || kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

lane_rows="$("$LLMTRACE" gateway list --json | /opt/homebrew/bin/python3 -c '
import json, sys, urllib.parse
sel = set(sys.argv[1].split(","))
for g in json.load(sys.stdin):
    if g["lane_pool"] and (g["name"] in sel or g["lane_pool"] in sel) and g["target_url"]:
        print(urllib.parse.urlsplit(g["target_url"]).port, g["port"])' "$LANE")"
[[ -n "$lane_rows" ]] || { echo "no lane with LANE_TARGET_URL in $LANE" >&2; exit 1; }

while read -r qa_port gw_port; do
  if curl -fsS -m 2 "http://127.0.0.1:$qa_port/health" >/dev/null 2>&1; then
    echo "Insurance QA :$qa_port already healthy (reused; check it points at :$gw_port)"
    continue
  fi
  (cd "$INSURANCE_REPO" && ENV_TYPE=dev INSURANCE_QA_PORT="$qa_port" \
    INSURANCE_QA_MODEL_BASE_URL="http://127.0.0.1:$gw_port/v1" \
    exec "$INSURANCE_REPO/.feval_venv/bin/python" "$ROOT/scripts/start_insurance_qa_local.py" \
    >>"$ROOT/eval_runs/insurance_qa_server_$qa_port.log" 2>&1) &
  started_pids+=("$!")
done <<<"$lane_rows"

while read -r qa_port _; do
  ready=0
  for _ in $(seq 1 60); do
    if curl -fsS -m 2 "http://127.0.0.1:$qa_port/health" >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
  done
  [[ "$ready" -eq 1 ]] || { echo "Insurance QA :$qa_port failed health check" >&2; exit 1; }
done <<<"$lane_rows"

cases="${INSURANCE_CASES:-}"
if [[ -z "$cases" ]]; then
  cases="$(/opt/homebrew/bin/python3 -c 'import json,sys; print(",".join(json.loads(x)["case_id"] for x in open(sys.argv[1]) if x.strip()))' "$BUNDLE")"
fi
cd "$ROOT/eval_harness"
export PYTHONPATH="$ROOT/eval_harness/src"
"$INSURANCE_REPO/.feval_venv/bin/python" -m eval_harness.adapters.insurance \
  --live-batch \
  --resume \
  --cases "$cases" \
  --run-id insurance \
  --bundle "$BUNDLE" \
  --eval-runs-dir "$DUAL_ROOT" \
  --lanes "$LANE"
