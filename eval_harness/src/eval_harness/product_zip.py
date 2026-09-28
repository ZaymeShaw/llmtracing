"""Build the shareable product zip for a dual/triple root, behind the secret-scan gate.

Default members (mirrors var/exports/triple_results_*.zip):
  <root>/*.json                               (manifest.json, notes)
  <root>/<agent>/{run_meta.json,progress.jsonl,llm_trace.html,results.xlsx,summary.json}

Every member is scanned (streamed) BEFORE any zip byte is written; on a finding the build fails
with file + count + masked prefix and no zip is produced. Never overwrites an existing zip.

  PYTHONPATH=src python3 -m eval_harness.product_zip --src eval_runs/triple_... --out var/exports/x.zip
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from eval_harness.secret_scan import SecretScanError, gate

AGENT_DIRS = ("claude", "insurance", "pi")
AGENT_FILES = ("run_meta.json", "progress.jsonl", "llm_trace.html", "results.xlsx", "summary.json")


def product_members(src: Path) -> List[Tuple[Path, str]]:
    src = Path(src)
    out: List[Tuple[Path, str]] = []
    for p in sorted(src.glob("*.json")):
        if p.is_file() and p.name != "secret_scan.json":
            out.append((p, p.name))
    for agent in AGENT_DIRS:
        for name in AGENT_FILES:
            p = src / agent / name
            if p.is_file():
                out.append((p, f"{agent}/{name}"))
    return out


def pack_product_zip(src: Path, out_zip: Path, *, allow_local_gateway_keys: bool = False) -> Path:
    src, out_zip = Path(src), Path(out_zip)
    if out_zip.exists():
        raise FileExistsError(f"refusing to overwrite existing zip: {out_zip}")
    members = product_members(src)
    if not members:
        raise FileNotFoundError(f"no product files under {src}")
    gate([m[0] for m in members], what=f"product zip {out_zip.name}",
         allow_local_gateway_keys=allow_local_gateway_keys)
    tmp = out_zip.with_name(out_zip.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p, arc in members:
            zf.write(p, arcname=arc)
    tmp.replace(out_zip)
    return out_zip


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--allow-local-gateway-keys", action="store_true")
    a = ap.parse_args(argv)
    try:
        z = pack_product_zip(a.src, a.out, allow_local_gateway_keys=a.allow_local_gateway_keys)
    except SecretScanError:
        print(f"[product-zip] NOT produced: {a.out}", file=sys.stderr)
        return 3
    print(f"[product-zip] wrote {z}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
