"""Package a multi-harness report with the common share policy and secret-scan gate.

Keeps original HTML bytes, linked local attachments, and ancillary metadata.
Unlinked raw JSONL, spreadsheets and logs remain in the source directory.
Never overwrites an existing output.

  PYTHONPATH=src python3 -m eval_harness.product_zip --src eval_runs/triple_... --out var/exports/x.zip
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from eval_harness.secret_scan import SecretScanError
from eval_harness.share_zip import pack_share_zip

def pack_product_zip(src: Path, out_zip: Path, *, allow_local_gateway_keys: bool = False) -> Path:
    pack_share_zip(Path(src), Path(out_zip), include_root=False,
                   allow_local_gateway_keys=allow_local_gateway_keys)
    return Path(out_zip)


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
