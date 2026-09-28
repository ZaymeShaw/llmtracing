"""Export run results to results.xlsx (cases / turns / tool_calls / run sheets)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# Excel cell hard limit
_EXCEL_CELL_MAX = 32000


def _pretty_json(v: Any) -> str:
    """Render value as indented JSON text for readable Excel cells."""
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return ""
        try:
            obj = json.loads(s)
        except Exception:
            return v
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return json.dumps(v, ensure_ascii=False, indent=2, default=str)


def _style_llm_sheet(ws) -> None:
    """Wrap text, freeze header, set widths — no schema change."""
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths = {
        "A": 10,  # case_id
        "B": 6,   # seq
        "C": 18,  # html_link
        "D": 36,  # call_id
        "E": 26,  # ts
        "F": 22,  # model
        "G": 8,   # stream
        "H": 16,  # path
        "I": 10,  # status
        "J": 12,  # latency
        "K": 56,  # request
        "L": 56,  # response
        "M": 24,  # error
    }
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    header_font = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    for cell in ws[1]:
        cell.font = header_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap
        # keep rows usable but not absurdly tall
        ws.row_dimensions[row[0].row].height = 120


def _cell(s: Optional[str], *, max_len: int = _EXCEL_CELL_MAX) -> str:
    """Write full text into a cell; only trim at Excel's hard limit."""
    if not s:
        return ""
    s = str(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 32] + "\n…[truncated for Excel cell limit]"


def _join_turns(parts: list[tuple[int, str]], *, label: str) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][1]
    blocks = []
    for idx, text in parts:
        blocks.append(f"【第{idx}轮·{label}】\n{text}")
    return "\n\n".join(blocks)


def write_results_xlsx(
    path: Path,
    *,
    case_rows: list[dict],
    turn_rows: list[dict],
    tool_rows: list[dict],
    run_meta: dict[str, Any],
    llm_rows: Optional[list[dict]] = None,
) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError as e:
        raise SystemExit(
            "openpyxl is required to write results.xlsx. Install with: python3 -m pip install openpyxl"
        ) from e

    wb = Workbook()
    ws = wb.active
    ws.title = "cases"
    case_cols = [
        "id",
        "class",
        "n_turns",
        "success",
        "exit_code",
        "wall_ms",
        "api_ms",
        "first_frame_ms",
        "first_frame_kind",
        "num_tool_calls",
        "thinking_present",
        "user_input",
        "final_output",
        "trace_relpath",
        "error",
        "error_class",
        "tool_ok_calls",
        "tool_infra_errors",
        "upstream_status",
        "upstream_errors",
    ]
    ws.append(case_cols)
    for r in case_rows:
        ws.append([_cell(r.get(c)) if c in ("user_input", "final_output", "error") else r.get(c) for c in case_cols])

    ws_turns = wb.create_sheet("turns")
    turn_cols = ["case_id", "turn_index", "user_input", "final_output"]
    ws_turns.append(turn_cols)
    for r in turn_rows:
        ws_turns.append(
            [
                r.get("case_id"),
                r.get("turn_index"),
                _cell(r.get("user_input")),
                _cell(r.get("final_output")),
            ]
        )

    ws2 = wb.create_sheet("tool_calls")
    tool_cols = ["case_id", "turn_index", "seq", "tool_name", "input_preview", "output_preview"]
    ws2.append(tool_cols)
    for r in tool_rows:
        # tool payloads can be huge; keep a longer but bounded preview
        row = []
        for c in tool_cols:
            v = r.get(c)
            if c in ("input_preview", "output_preview"):
                row.append(_cell(v, max_len=8000))
            else:
                row.append(v)
        ws2.append(row)

    ws3 = wb.create_sheet("run")
    ws3.append(["key", "value"])
    for k, v in run_meta.items():
        ws3.append([k, "" if v is None else _cell(str(v), max_len=8000)])

    # full per-call LLM request/response captured via local gateway
    ws_llm = wb.create_sheet("llm_calls")
    llm_cols = [
        "case_id",
        "seq",
        "html_link",
        "call_id",
        "ts",
        "model",
        "stream",
        "path",
        "status_code",
        "latency_ms",
        "request",
        "response",
        "error",
    ]
    ws_llm.append(llm_cols)
    link_font = Font(color="0563C1", underline="single")
    for r in llm_rows or []:
        row = []
        for c in llm_cols:
            if c == "html_link":
                row.append("打开 trace")
                continue
            v = r.get(c)
            if c in ("request", "response"):
                row.append(_cell(_pretty_json(v)))
            elif c == "error":
                row.append(_cell(_pretty_json(v) if v not in (None, "") else ""))
            else:
                row.append(v)
        ws_llm.append(row)
        # relative link sits next to results.xlsx in the same run dir / share zip
        case_id = r.get("case_id") or ""
        seq = r.get("seq")
        if case_id and seq is not None:
            cell = ws_llm.cell(row=ws_llm.max_row, column=3)  # html_link
            cell.value = f"打开 {case_id}#{seq}"
            cell.hyperlink = f"llm_trace.html#{case_id}/{seq}"
            cell.font = link_font
    if ws_llm.max_row >= 1:
        _style_llm_sheet(ws_llm)

    # glossary sheet for metric meanings
    ws4 = wb.create_sheet("metrics_glossary")
    ws4.append(["metric", "meaning"])
    ws4.append(["wall_ms", "整案墙钟耗时（从拉起 Claude 进程到该案结束），毫秒"])
    ws4.append(["api_ms", "接口/模型侧累计耗时（来自 Claude result 事件的 duration_ms 等，若有），毫秒；可能小于 wall_ms"])
    ws4.append(["first_frame_ms", "从进程启动到首个客户端可见行动（文本或 tool_use）的耗时，毫秒"])
    ws4.append(["first_frame_kind", "首帧类型：text 或 tool_use"])
    ws4.append(["num_llm_calls", "经本地 LLM 网关捕获的本案 /v1/messages 调用次数"])

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def case_row_from_trace(trace: dict, *, class_letter: str, n_turns: int, trace_relpath: str) -> dict:
    metrics = trace.get("metrics") or {}
    turns = trace.get("turns") or []
    user_parts = [(int(t.get("index") or i + 1), t.get("prompt") or "") for i, t in enumerate(turns)]
    final_parts = [(int(t.get("index") or i + 1), t.get("final_text") or "") for i, t in enumerate(turns)]
    return {
        "id": trace.get("case_id"),
        "class": class_letter,
        "n_turns": n_turns,
        "success": bool(trace.get("success")),
        "exit_code": trace.get("exit_code"),
        # accept both naming variants from older/newer normalize
        "wall_ms": metrics.get("wall_ms", metrics.get("wall_ms")),
        "api_ms": metrics.get("api_ms", metrics.get("api_ms")),
        "first_frame_ms": metrics.get("first_frame_ms", metrics.get("first_frame_ms")),
        "first_frame_kind": metrics.get("first_frame_kind", metrics.get("first_frame_kind")),
        "num_tool_calls": metrics.get("num_tool_calls", metrics.get("num_tool_calls")),
        "thinking_present": metrics.get("thinking_present", metrics.get("thinking_present")),
        "user_input": _join_turns(user_parts, label="用户输入"),
        "final_output": _join_turns(final_parts, label="最终回复"),
        "trace_relpath": trace_relpath,
        "error": trace.get("error"),
        # case-level tool health (eval_harness.tool_health): tool_infra_error when every
        # insurance tool call hit an infra-class error even though the agent exited 0
        "error_class": trace.get("error_class") or ("none" if trace.get("success") else None),
        "tool_ok_calls": (trace.get("tool_health") or {}).get("ok"),
        "tool_infra_errors": (trace.get("tool_health") or {}).get("infra"),
        # backend/upstream-side trouble: exposed, never counted as our failure
        "upstream_status": trace.get("upstream_status") or "none",
        "upstream_errors": "; ".join(f"{k} x{v}" for k, v in sorted(
            (trace.get("upstream_errors") or {}).items(), key=lambda kv: -kv[1])) or None,
    }


def turn_rows_from_trace(trace: dict) -> list[dict]:
    case_id = trace.get("case_id")
    rows = []
    for i, t in enumerate(trace.get("turns") or []):
        rows.append(
            {
                "case_id": case_id,
                "turn_index": t.get("index") or (i + 1),
                "user_input": t.get("prompt") or "",
                "final_output": t.get("final_text") or "",
            }
        )
    return rows
