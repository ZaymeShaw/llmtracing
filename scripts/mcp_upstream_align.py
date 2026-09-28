#!/usr/bin/env python3
"""Thin shim → insurance_upstream_align.py (mcp-only). Kept for existing muscle memory/docs."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_upstream_align.py")

ACTIONS = {"check", "sync", "status"}

if not TARGET.is_file():
    sys.exit(f"missing insurance-side umbrella: {TARGET}")

# Preserve legacy: `mcp_upstream_align.py check|sync|status [--source|--mcp|--python]`
# Map to umbrella mcp-only; ignore legacy --source/--mcp/--python if present (config owns paths).
argv = sys.argv[1:]
action = "check"
rest: list[str] = []
if argv and not argv[0].startswith("-"):
    action = argv[0]
    rest = argv[1:]
else:
    rest = argv
if action not in ACTIONS:
    sys.exit(f"action must be one of {sorted(ACTIONS)}, got {action!r}")
# Drop legacy path overrides; config JSON is SoT. Warn if passed.
filtered: list[str] = []
skip_next = False
warned = False
for i, arg in enumerate(rest):
    if skip_next:
        skip_next = False
        continue
    if arg in {"--source", "--mcp", "--python"}:
        if not warned:
            print(
                "[mcp_upstream_align] note: --source/--mcp/--python ignored; "
                "paths live in insurance_qa_agent/scripts/insurance_upstream_align.json",
                file=sys.stderr,
            )
            warned = True
        # value may be next token or --source=...
        if "=" not in arg and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
            skip_next = True
        continue
    if arg.startswith("--source=") or arg.startswith("--mcp=") or arg.startswith("--python="):
        if not warned:
            print(
                "[mcp_upstream_align] note: --source/--mcp/--python ignored; "
                "paths live in insurance_qa_agent/scripts/insurance_upstream_align.json",
                file=sys.stderr,
            )
            warned = True
        continue
    filtered.append(arg)

sys.argv = [str(TARGET), action, "--mcp-only", *filtered]
runpy.run_path(str(TARGET), run_name="__main__")
