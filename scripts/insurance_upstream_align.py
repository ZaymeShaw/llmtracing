#!/usr/bin/env python3
"""Thin shim → insurance_qa_agent/scripts/insurance_upstream_align.py (SoT on insurance side)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = Path("/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/scripts/insurance_upstream_align.py")

if not TARGET.is_file():
    sys.exit(f"missing insurance-side umbrella: {TARGET}")
sys.argv[0] = str(TARGET)
runpy.run_path(str(TARGET), run_name="__main__")
