#!/usr/bin/env python3
"""Audit completed Claude and Insurance cases in a dual evaluation run."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval_harness/src"))

from eval_harness.llm_trace_html import build_payload  # noqa: E402


def jsonl(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--gateway-log",
        type=Path,
        default=ROOT / "llm_gateway/logs/llm_calls.jsonl",
    )
    parser.add_argument("--expected-count", type=int, default=120)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    execution_owners: dict[str, str] = {}
    completed: dict[str, list[str]] = {"claude": [], "insurance": []}
    observed: dict[str, list[str]] = {"claude": [], "insurance": []}
    failed_cases: dict[str, list[str]] = {"claude": [], "insurance": []}
    calls_by_channel: Counter[str] = Counter()
    insurance_zero_llm: list[str] = []
    insurance_foreign_prompt_calls: list[dict[str, str]] = []
    business_failure_text: list[dict[str, str]] = []

    gateway_events = jsonl(args.gateway_log)
    events_by_call: dict[str, list[dict]] = defaultdict(list)
    for event in gateway_events:
        call_id = event.get("call_id")
        if call_id:
            events_by_call[str(call_id)].append(event)

    payloads = {
        "claude": build_payload(run_dir / "claude"),
        "insurance": build_payload(run_dir / "insurance"),
    }
    payload_cases = {
        channel: {str(row.get("id")): row for row in payload.get("cases", [])}
        for channel, payload in payloads.items()
    }
    expected_insurance_turns: dict[str, list[str]] = {}
    insurance_meta_path = run_dir / "insurance" / "run_meta.json"
    if insurance_meta_path.is_file():
        try:
            insurance_run_meta = json.loads(insurance_meta_path.read_text(encoding="utf-8"))
            bundle_path = Path(str(insurance_run_meta.get("bundle_path") or ""))
            for line in bundle_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                expected_insurance_turns[f"INS_{row['case_id']}"] = list(row.get("turns") or [])
        except (OSError, ValueError, KeyError):
            warnings.append("could not load Insurance bundle turn definitions")

    for channel, case_glob in (("claude", "*"), ("insurance", "INS_*")):
        cases_root = run_dir / channel / "cases"
        for case_dir in sorted(cases_root.glob(case_glob)):
            if not case_dir.is_dir():
                continue
            metadata_path = case_dir / ("trace.json" if channel == "claude" else "meta.json")
            if not metadata_path.is_file():
                continue
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            case_id = case_dir.name
            observed[channel].append(case_id)
            complete = bool(metadata.get("success"))
            if channel == "insurance" and metadata.get("attribution_status") not in (
                "captured",
                "unknown_no_calls",
            ):
                complete = False
            if complete:
                completed[channel].append(case_id)
            else:
                failed_cases[channel].append(case_id)
                errors.append(
                    f"{channel}/{case_id}: case incomplete "
                    f"success={metadata.get('success')} "
                    f"attribution={metadata.get('attribution_status')} "
                    f"error={metadata.get('error')}"
                )
            calls = jsonl(case_dir / "llm_calls.jsonl")
            calls_by_channel[channel] += len(calls)

            execution_id = metadata.get("execution_id")
            if channel == "insurance":
                expected_case_turns = expected_insurance_turns.get(case_id) or []
                recorded_turns = metadata.get("turns") if isinstance(metadata.get("turns"), list) else []
                recorded_prompts = [
                    str(turn.get("prompt") or "") for turn in recorded_turns
                    if isinstance(turn, dict)
                ]
                recorded_count = int(
                    metadata.get("num_turns") or len(recorded_turns) or 1
                )
                if expected_case_turns and recorded_count != len(expected_case_turns):
                    errors.append(
                        f"{case_id}: recorded {recorded_count}/{len(expected_case_turns)} turns"
                    )
                if len(expected_case_turns) > 1 and recorded_prompts != expected_case_turns:
                    errors.append(f"{case_id}: recorded turn prompts differ from bundle")
                if not isinstance(execution_id, str) or not execution_id:
                    errors.append(f"{case_id}: missing execution_id")
                elif execution_id in execution_owners:
                    errors.append(
                        f"{case_id}: execution_id reused by {execution_owners[execution_id]}"
                    )
                else:
                    execution_owners[execution_id] = case_id

            for call in calls:
                call_id = call.get("call_id")
                if not call_id:
                    errors.append(f"{case_id}: call without call_id")
                    continue
                call_id = str(call_id)
                owners[call_id].add((channel, case_id))
                if call.get("case_id") != case_id:
                    errors.append(f"{case_id}/{call_id}: wrong case_id={call.get('case_id')}")
                if channel == "claude":
                    if call.get("case_id_source") != "header":
                        errors.append(f"{case_id}/{call_id}: Claude source is not header")
                    if call.get("attribution_status") != "explicit":
                        errors.append(f"{case_id}/{call_id}: Claude attribution is not explicit")
                    # Dual-id (P0): Claude mints execution_id and sends X-Eval-Execution-Id.
                    # Only lane_id indicates Insurance lane contamination.
                    if call.get("lane_id") is not None:
                        errors.append(f"{case_id}/{call_id}: Claude contains Insurance lane_id")
                    if not call.get("execution_id"):
                        errors.append(f"{case_id}/{call_id}: Claude missing execution_id (dual-id)")
                else:
                    src = call.get("case_id_source")
                    status = call.get("attribution_status")
                    if src == "lane_registry":
                        if status != "attributed":
                            errors.append(f"{case_id}/{call_id}: Insurance call is not attributed")
                        if call.get("lane_id") != "insurance_1":
                            errors.append(f"{case_id}/{call_id}: wrong lane_id={call.get('lane_id')}")
                    elif src == "header" and status == "explicit":
                        # Concurrent header path: both X-Eval-* forwarded through Insurance.
                        if call.get("lane_id") is not None:
                            errors.append(f"{case_id}/{call_id}: explicit Insurance call has lane_id")
                    else:
                        errors.append(
                            f"{case_id}/{call_id}: Insurance source/status "
                            f"unsupported ({src!r}/{status!r})"
                        )
                    if call.get("execution_id") != execution_id:
                        errors.append(f"{case_id}/{call_id}: execution_id mismatch")
                    user_texts: list[str] = []
                    request = call.get("request") or {}
                    messages = request.get("messages") if isinstance(request, dict) else []
                    for message in messages or []:
                        if not isinstance(message, dict) or message.get("role") != "user":
                            continue
                        content = message.get("content")
                        if isinstance(content, str):
                            user_texts.append(content)
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and isinstance(block.get("text"), str):
                                    user_texts.append(block["text"])
                    meta_turns = metadata.get("turns") if isinstance(metadata.get("turns"), list) else []
                    prompts = [
                        str(turn.get("prompt") or "") for turn in meta_turns
                        if isinstance(turn, dict) and turn.get("prompt")
                    ] or [str(metadata.get("prompt") or "")]
                    if prompts and not any(prompt in "\n".join(user_texts) for prompt in prompts):
                        preview = "\n".join(user_texts)[-240:]
                        insurance_foreign_prompt_calls.append(
                            {"case_id": case_id, "call_id": call_id, "user_text_preview": preview}
                        )
                        errors.append(f"{case_id}/{call_id}: request does not contain case prompt")

                events = events_by_call.get(call_id, [])
                event_counts = Counter(str(row.get("event")) for row in events)
                if event_counts["pre_api_call"] < 1:
                    errors.append(
                        f"{case_id}/{call_id}: pre_api_call count={event_counts['pre_api_call']}"
                    )
                terminal_count = event_counts["success"] + event_counts["failure"]
                if terminal_count != event_counts["pre_api_call"]:
                    errors.append(
                        f"{case_id}/{call_id}: pre={event_counts['pre_api_call']} "
                        f"terminal={terminal_count}"
                    )
                for event in events:
                    if event.get("case_id") != case_id:
                        errors.append(f"{case_id}/{call_id}: global event case mismatch")
                    if channel == "insurance" and event.get("execution_id") != execution_id:
                        errors.append(f"{case_id}/{call_id}: global event execution mismatch")

            frontend = payload_cases[channel].get(case_id)
            if frontend is None:
                errors.append(f"{case_id}: absent from frontend payload")
                continue
            if int(frontend.get("n_calls") or 0) != len(calls):
                errors.append(f"{case_id}: frontend call count mismatch")
            if channel == "insurance":
                meta_turns = metadata.get("turns") if isinstance(metadata.get("turns"), list) else []
                expected_turns = [
                    {
                        "prompt": str(turn.get("prompt") or ""),
                        "final_text": str(turn.get("final_text", turn.get("answer")) or ""),
                    }
                    for turn in meta_turns if isinstance(turn, dict)
                ] or [{
                    "prompt": str(metadata.get("prompt") or ""),
                    "final_text": str(metadata.get("answer") or ""),
                }]
                frontend_turns = (frontend.get("overview") or {}).get("turns") or []
                actual_turns = [
                    {
                        "prompt": str(turn.get("prompt") or ""),
                        "final_text": str(turn.get("final_text") or ""),
                    }
                    for turn in frontend_turns if isinstance(turn, dict)
                ]
                if actual_turns != expected_turns:
                    errors.append(f"{case_id}: frontend turns differ from business input/output")
                answer = expected_turns[-1]["final_text"]
                if (
                    any(not turn["prompt"] or not turn["final_text"] for turn in expected_turns)
                    or not (case_dir / "response.json").is_file()
                ):
                    errors.append(f"{case_id}: missing business input/output artifact")
                if not calls:
                    insurance_zero_llm.append(case_id)
                    note = str((frontend.get("overview") or {}).get("note") or "")
                    if "未观测到 LLM 调用" not in note:
                        errors.append(f"{case_id}: zero-LLM frontend note missing")
                lowered = answer.lower()
                markers = ("工具调用失败", "系统繁忙", "请稍后再试", "未能获取相关信息")
                if any(marker.lower() in lowered for marker in markers):
                    business_failure_text.append(
                        {"case_id": case_id, "answer_preview": answer[:160]}
                    )

    multi_owner = {
        call_id: sorted(f"{channel}/{case_id}" for channel, case_id in owner_set)
        for call_id, owner_set in owners.items()
        if len(owner_set) != 1
    }
    if multi_owner:
        errors.append(f"{len(multi_owner)} call_id values have multiple owners")
    for channel in ("claude", "insurance"):
        if len(observed[channel]) != args.expected_count:
            errors.append(
                f"{channel}: observed {len(observed[channel])}/{args.expected_count} case artifacts"
            )
        if len(completed[channel]) != args.expected_count:
            errors.append(
                f"{channel}: completed {len(completed[channel])}/{args.expected_count} cases"
            )

    report = {
        "run_dir": str(run_dir),
        "completed_cases": {key: len(value) for key, value in completed.items()},
        "observed_cases": {key: len(value) for key, value in observed.items()},
        "failed_cases": failed_cases,
        "completed_case_ids": completed,
        "calls": dict(calls_by_channel),
        "unique_call_ids": len(owners),
        "multi_owner_call_ids": len(multi_owner),
        "unique_insurance_execution_ids": len(execution_owners),
        "insurance_zero_llm": insurance_zero_llm,
        "insurance_multi_turn_cases": sum(
            1 for turns in expected_insurance_turns.values() if len(turns) > 1
        ),
        "insurance_foreign_prompt_calls": insurance_foreign_prompt_calls,
        "business_failure_text": business_failure_text,
        "errors": errors,
        "warnings": warnings,
        "status": "pass" if not errors else "fail",
    }
    out = run_dir / "attribution_audit.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
