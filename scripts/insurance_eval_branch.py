#!/usr/bin/env python3
"""Thin shim → insurance_qa_agent/scripts/insurance_eval_branch.py (SoT on insurance side).

After a successful `sync` (or `rollback <branch>`) that moves the eval worktree, the
insurance-tools MCP is re-synced from that worktree via its validated sync
(`sync_from_upstream.py --ensure`: symbol extraction → staging → py_compile/import/
harness_conf/stdio tools/call/spec compare → atomic swap). A failed itools sync makes
this command exit non-zero; the eval preflight would also refuse to start a run.
"""
from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

TARGET = Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_eval_branch.py")
ITOOLS = Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-tools-mcp")

if not TARGET.is_file():
    sys.exit(f"missing insurance-side eval-branch tool: {TARGET}")

argv = sys.argv[1:]
positionals = [a for a in argv if not a.startswith("-")]
action = positionals[0] if positionals else None
moves_worktree = (action == "sync" or (action == "rollback" and len(positionals) > 1)) and "--dry-run" not in argv

sys.argv[0] = str(TARGET)
rc = 0
try:
    runpy.run_path(str(TARGET), run_name="__main__")
except SystemExit as exc:
    rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)

if rc == 0 and moves_worktree:
    print("[insurance_eval_branch] eval worktree moved → itools MCP validated sync (--ensure)", flush=True)
    rc = subprocess.call([str(ITOOLS / ".venv" / "bin" / "python"),
                          str(ITOOLS / "scripts" / "sync_from_upstream.py"), "--ensure"])
    if rc != 0:
        print("[insurance_eval_branch] itools sync FAILED — MCP left on its previous (working) version; "
              "eval preflight will block runs until fixed", file=sys.stderr, flush=True)
sys.exit(rc)
