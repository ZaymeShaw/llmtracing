"""Model-facing read tools: parameters affecting backend requests, not presentation."""
from __future__ import annotations

import json
from typing import Any

# Keep stateful workflows and unknown tools on exact-argument identity.
RPC_ARGUMENTS = {
    "policy_detail": frozenset({"polno"}),
    "customer_detail": frozenset({"customer_id", "customer_ids", "client_name", "client_mobile"}),
    "policy_search": frozenset({"query"}),
    "customer_search": frozenset({"query"}),
}
_VIEW_ARGUMENTS = frozenset({"fields", "compact"})


def tool_rpc_key(tool: str, arguments: dict[str, Any]) -> str:
    """Turn-local identity; unknown parameters remain conservative cache boundaries."""
    declared = RPC_ARGUMENTS.get(tool)
    args = arguments
    if declared is not None:
        args = {key: value for key, value in arguments.items()
                if key in declared or key not in _VIEW_ARGUMENTS}
    return tool + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
