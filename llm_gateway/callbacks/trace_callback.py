"""LiteLLM custom logger: persist full request/response for eval traces."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from callbacks.case_id import (
    detect_protocol,
    extract_case_id,
    extract_execution_id,
    extract_trace_labels,
    proxy_request_bits,
)
from callbacks.attribution import load_config as load_lane_config, mark_gateway_ready, resolve as resolve_lane_attribution

try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover
    class CustomLogger:  # type: ignore
        pass


LOG_DIR = Path(os.environ.get("LLM_GATEWAY_LOG_DIR", str(Path(__file__).resolve().parents[1] / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "llm_calls.jsonl"
_LANE_CONFIG: dict[str, Any] | None = None
_LANE_CONFIG_PATH: str | None = None


def _get_lane_config() -> dict[str, Any] | None:
    """Return attribution config for the *current* LLM_ATTRIBUTION_CONFIG.

    The production gateway process warms this once at import, but unit tests
    monkeypatch LLM_ATTRIBUTION_CONFIG after import. Always refresh when the
    env path changes so lane key hashes stay aligned with the active config.
    """
    global _LANE_CONFIG, _LANE_CONFIG_PATH
    raw = os.environ.get("LLM_ATTRIBUTION_CONFIG")
    if not raw:
        _LANE_CONFIG = None
        _LANE_CONFIG_PATH = None
        return None
    resolved = str(Path(raw).resolve())
    if _LANE_CONFIG is not None and _LANE_CONFIG_PATH == resolved:
        return _LANE_CONFIG
    _LANE_CONFIG = load_lane_config()
    _LANE_CONFIG_PATH = resolved
    return _LANE_CONFIG


# Warm cache only for the live gateway worker process.
if os.environ.get("LLM_ATTRIBUTION_CONFIG") and os.environ.get("LLM_ATTRIBUTION_GATEWAY_PROCESS") == "1":
    _get_lane_config()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(obj: Any, *, _depth: int = 0) -> Any:
    """Convert LiteLLM/pydantic objects to JSON-friendly structures (no Python repr)."""
    if _depth > 40:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return repr(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, _depth=_depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v, _depth=_depth + 1) for v in obj]
    # pydantic v2 / v1
    for meth in ("model_dump", "dict"):
        fn = getattr(obj, meth, None)
        if callable(fn):
            try:
                dumped = fn(mode="json") if meth == "model_dump" else fn()
                return _jsonable(dumped, _depth=_depth + 1)
            except TypeError:
                try:
                    return _jsonable(fn(), _depth=_depth + 1)
                except Exception:
                    pass
            except Exception:
                pass
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        try:
            data = {
                k: v
                for k, v in vars(obj).items()
                if not k.startswith("_") and not callable(v)
            }
            if data:
                return _jsonable(data, _depth=_depth + 1)
        except Exception:
            pass
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except Exception:
        return str(obj)


def _safe(obj: Any) -> Any:
    return _jsonable(obj)


def _safe_client_request(body: Any) -> Any:
    """Preserve protocol bodies without writing credentials embedded in them."""
    sensitive = {"api_key", "apikey", "authorization", "auth_token", "access_token", "x-api-key", "password", "secret"}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: "[REDACTED]" if str(k).lower() in sensitive else redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value

    return redact(_safe(body))


def _append(record: dict) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=lambda o: _jsonable(o)) + "\n")


THINKING_HEADER = "x-eval-thinking"


def _headers_lower(data: dict | None) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    psr = data.get("proxy_server_request") or {}
    if not isinstance(psr, dict):
        # litellm_params shape in some hooks
        psr = (data.get("litellm_params") or {}).get("proxy_server_request") or {}
    if not isinstance(psr, dict):
        return {}
    raw = psr.get("headers") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if v is None:
            continue
        out[str(k).lower()] = str(v).strip()
    return out


def _thinking_optin(data: dict, headers: dict[str, str] | None = None) -> bool:
    hdrs = headers if headers is not None else _headers_lower(data)
    if hdrs.get(THINKING_HEADER, "").lower() == "on":
        return True
    model = str(data.get("model") or "")
    if model.endswith("-think"):
        return True
    return False


def _apply_thinking_policy(data: dict, call_type: str | None) -> str:
    """Default thinking OFF at relay; caller override via X-Eval-Thinking: on or *-think.

    Returns thinking_source label for audit logs.
    Separate from Insurance upstream models_common_args enable_thinking:false —
    that is the Insurance agent commit; this is relay-side policy for :4001/:4002.
    """
    hdrs = _headers_lower(data)
    optin = _thinking_optin(data, hdrs)
    model = str(data.get("model") or "")
    ct = (call_type or "").lower()

    # LiteLLM Anthropic /v1/messages uses call_type=text_completion (1.74.x).
    is_anthropic_messages = (
        ct == "text_completion"
        and isinstance(data.get("messages"), list)
        and ("max_tokens" in data or "max_completion_tokens" in data)
    )
    # OpenAI chat completions (Insurance/Pi on :4002).
    is_openai = ct in ("completion", "acompletion", "chat_completion") or (
        not is_anthropic_messages
        and isinstance(data.get("messages"), list)
        and ("enable_thinking" in data or isinstance(data.get("extra_body"), dict) or ct == "")
    )

    if is_anthropic_messages or ("thinking" in data and not is_openai):
        if optin:
            if model.endswith("-think"):
                data["model"] = model[: -len("-think")]
            policy = "optin"
        else:
            data["thinking"] = {"type": "disabled"}
            policy = "relay_forced_off"
    else:
        eb = data.get("extra_body")
        if not isinstance(eb, dict):
            eb = {}
            data["extra_body"] = eb
        if optin:
            if model.endswith("-think"):
                data["model"] = model[: -len("-think")]
            eb["enable_thinking"] = True
            data["enable_thinking"] = True
            policy = "optin"
        elif "enable_thinking" in data or "enable_thinking" in eb:
            policy = "caller"
        else:
            eb["enable_thinking"] = False
            policy = "relay_default_off"

    meta = data.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        data["metadata"] = meta
    meta["eval_thinking_policy"] = policy
    return policy


def _thinking_effective_from_kwargs(optional_params: Any, kwargs: dict) -> tuple[str, str | None]:
    """Derive thinking_effective + thinking_source for pre_api_call audit."""
    litellm_params = kwargs.get("litellm_params") or {}
    meta = {}
    if isinstance(litellm_params, dict):
        m = litellm_params.get("metadata")
        if isinstance(m, dict):
            meta = m
    km = kwargs.get("metadata")
    if isinstance(km, dict):
        meta = {**meta, **km}
    source = meta.get("eval_thinking_policy")
    if isinstance(source, str):
        source = source
    else:
        source = None

    op = optional_params if isinstance(optional_params, dict) else {}
    thinking = op.get("thinking")
    if isinstance(thinking, dict):
        t = str(thinking.get("type") or "").lower()
        if t in ("disabled", "none", "off"):
            return "off", source or "relay_forced_off"
        if t:
            return "on", source or "caller"

    eb = op.get("extra_body") if isinstance(op.get("extra_body"), dict) else {}
    if "enable_thinking" in op:
        return ("on" if op.get("enable_thinking") else "off"), source or "caller"
    if "enable_thinking" in eb:
        return ("on" if eb.get("enable_thinking") else "off"), source or "caller"

    if source in ("relay_forced_off", "relay_default_off"):
        return "off", source
    if source == "optin":
        return "on", source
    return "upstream_default", source


def _resolve_case_and_protocol(messages: Any, kwargs: dict) -> tuple[str | None, str | None, str | None, str, list[str]]:
    litellm_params = kwargs.get("litellm_params") or {}
    headers, body, path = proxy_request_bits(litellm_params)
    optional_params = kwargs.get("optional_params")
    case_id, source, warnings = extract_case_id(
        headers=headers,
        body=body,
        optional_params=optional_params,
        messages=messages,
    )
    execution_id = extract_execution_id(
        headers=headers,
        body=body,
        optional_params=optional_params,
    )
    protocol = detect_protocol(path=path, body=body if isinstance(body, dict) else None, messages=messages)
    return case_id, source, execution_id, protocol, warnings


def _attribution_snapshot(messages: Any, kwargs: dict) -> tuple[dict[str, Any], str, list[str]]:
    """Attribute a request: both case+execution ids win; else lane; else legacy case-only.

    - Both case_id and execution_id present → explicit/header-sourced; never consult lane.
    - Either missing and LLM_ATTRIBUTION_CONFIG set → lane registry (Insurance serial).
    - No lane config (e.g. Claude :4001) → keep case_id-only explicit tagging.
    """
    case_id, source, execution_id, protocol, warnings = _resolve_case_and_protocol(messages, kwargs)
    headers, body, _ = proxy_request_bits(kwargs.get("litellm_params") or {})
    labels = extract_trace_labels(headers=headers, body=body, optional_params=kwargs.get("optional_params"))
    if case_id and execution_id:
        return (
            {
                "case_id": case_id,
                "execution_id": execution_id,
                **labels,
                "case_id_source": source,
                "attribution_status": "explicit",
            },
            protocol,
            warnings,
        )
    if os.environ.get("LLM_ATTRIBUTION_CONFIG"):
        try:
            lane = resolve_lane_attribution(kwargs, config=_get_lane_config())
        except (OSError, ValueError, TypeError, KeyError):
            lane = {"attribution_status": "config_unavailable"}
        return lane, protocol, warnings
    if case_id:
        # Claude / non-lane gateways: case tag alone remains explicit.
        return (
            {"case_id": case_id, **labels, "case_id_source": source, "attribution_status": "explicit"},
            protocol,
            warnings,
        )
    return {"case_id": None, "attribution_status": "unmapped"}, protocol, warnings


class EvalTraceLogger(CustomLogger):

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Thinking-at-relay: default OFF; opt-in via X-Eval-Thinking: on or *-think model."""
        try:
            if isinstance(data, dict):
                _apply_thinking_policy(data, call_type)
        except Exception:
            # Never break the request path for audit policy failures.
            pass
        return data

    def log_pre_api_call(self, model, messages, kwargs):  # sync hook
        call_id = kwargs.get("litellm_call_id") or str(uuid.uuid4())
        kwargs["_eval_trace_call_id"] = call_id
        kwargs["_eval_trace_t0"] = time.time()
        optional_params = kwargs.get("optional_params")
        attribution, protocol, warnings = _attribution_snapshot(messages, kwargs)
        # The first callback freezes attribution, including an orphan decision.
        kwargs["_eval_attribution"] = attribution
        if attribution.get("case_id"):
            kwargs["_eval_case_id"] = attribution["case_id"]
        if attribution.get("execution_id"):
            kwargs["_eval_execution_id"] = attribution["execution_id"]
        kwargs["_eval_protocol"] = protocol
        thinking_effective, thinking_source = _thinking_effective_from_kwargs(optional_params, kwargs)
        rec = {
            "event": "pre_api_call",
            "ts": _now(),
            "call_id": call_id,
            **attribution,
            "protocol": protocol,
            "model": model,
            "thinking_effective": thinking_effective,
            "thinking_source": thinking_source,
            "client_request": _safe_client_request(proxy_request_bits(kwargs.get("litellm_params") or {})[1]),
            "messages": _safe(messages),
            "optional_params": _safe(optional_params),
            "tools": _safe(
                kwargs.get("tools")
                or (optional_params or {}).get("tools")
                if isinstance(optional_params, dict)
                else kwargs.get("tools")
            ),
            "litellm_params_keys": sorted((kwargs.get("litellm_params") or {}).keys()),
        }
        if warnings:
            rec["case_id_warnings"] = warnings
        _append(rec)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        call_id = kwargs.get("_eval_trace_call_id") or kwargs.get("litellm_call_id") or str(uuid.uuid4())
        t0 = kwargs.get("_eval_trace_t0")
        latency_ms = int((time.time() - t0) * 1000) if t0 else None
        messages = kwargs.get("messages")
        attribution = kwargs.get("_eval_attribution")
        if not isinstance(attribution, dict):
            # A missing pre-call snapshot cannot be reconstructed from a later
            # lane file: it may already belong to the next case.
            attribution = {"case_id": kwargs.get("_eval_case_id"),
                           "execution_id": kwargs.get("_eval_execution_id"),
                           "attribution_status": "missing_request_snapshot"}
        protocol = kwargs.get("_eval_protocol")
        warnings: list[str] = []
        if not protocol:
            _, _, _, protocol, warnings = _resolve_case_and_protocol(messages, kwargs)
        rec = {
            "event": "success",
            "ts": _now(),
            "call_id": call_id,
            **attribution,
            "protocol": protocol,
            "model": kwargs.get("model"),
            "messages": _safe(messages),
            "tools": _safe(kwargs.get("tools")),
            "response": _safe(response_obj),
            "latency_ms": latency_ms,
            "usage": _safe(
                getattr(response_obj, "usage", None)
                or (response_obj.get("usage") if isinstance(response_obj, dict) else None)
            ),
        }
        if warnings:
            rec["case_id_warnings"] = warnings
        _append(rec)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        call_id = kwargs.get("_eval_trace_call_id") or kwargs.get("litellm_call_id") or str(uuid.uuid4())
        messages = kwargs.get("messages")
        attribution = kwargs.get("_eval_attribution")
        if not isinstance(attribution, dict):
            attribution = {"case_id": kwargs.get("_eval_case_id"),
                           "execution_id": kwargs.get("_eval_execution_id"),
                           "attribution_status": "missing_request_snapshot"}
        protocol = kwargs.get("_eval_protocol")
        warnings: list[str] = []
        if not protocol:
            _, _, _, protocol, warnings = _resolve_case_and_protocol(messages, kwargs)
        rec = {
            "event": "failure",
            "ts": _now(),
            "call_id": call_id,
            **attribution,
            "protocol": protocol,
            "model": kwargs.get("model"),
            "messages": _safe(messages),
            "error": _safe(response_obj),
        }
        if warnings:
            rec["case_id_warnings"] = warnings
        _append(rec)


if os.environ.get("LLM_ATTRIBUTION_CONFIG") and os.environ.get("LLM_ATTRIBUTION_GATEWAY_PROCESS") == "1":
    mark_gateway_ready()

proxy_handler_instance = EvalTraceLogger()
