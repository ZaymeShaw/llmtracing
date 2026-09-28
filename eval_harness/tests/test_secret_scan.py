from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from eval_harness import secret_scan
from eval_harness.secret_scan import SecretScanError, SecretScanner, gate

UP = "sk-upstreamFAKE0123456789abcdef"
LOCAL = "sk-local-gateway-fake-1"


def _scanner(**kw):
    return SecretScanner(upstream_secrets={UP, "ep_FAKEpenguinKEY_0123456789"}, local_keys={LOCAL}, **kw)


def test_clean_file_passes(tmp_path):
    f = tmp_path / "a.html"
    f.write_text('{"x-api-key": "***REDACTED***", "authorization": "***", "model": "deepseek"}')
    rep = _scanner().scan_paths([f])
    assert rep.ok, rep.summary_text()


def test_exact_and_generic_hits_counted_and_masked(tmp_path):
    f = tmp_path / "t.html"
    f.write_text(
        f"UPSTREAM_API_KEY={UP}\n{UP}\nep_FAKEpenguinKEY_0123456789\n"
        f"PI_LITELLM_KEY={LOCAL}\nsk-otherGENERICkey0123456789\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123\n"
        'x-api-key: "ABCDEFGHIJKLMNOPQRSTUV"\n'
    )
    rep = _scanner().scan_paths([f])
    d = {(r["kind"], r["masked"]): r["count"] for r in rep.to_dict()["findings"]}
    assert d[("upstream_secret", "sk-ups***")] == 2
    assert d[("upstream_secret", "ep_FAK***")] == 1
    assert d[("local_gateway_key", "sk-loc***")] == 1
    assert d[("generic_sk", "sk-oth***")] == 1
    assert d[("bearer", "abcdef***")] == 1
    assert d[("x_api_key", "ABCDEF***")] == 1
    assert UP not in rep.summary_text()


def test_chunk_boundary_and_zip_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "CHUNK", 64)
    body = ("x" * 50 + UP + "y" * 37) * 20  # secrets straddle 64-byte chunk boundaries
    f = tmp_path / "big.html"
    f.write_text(body)
    assert _scanner().scan_paths([f]).to_dict()["total_findings"] == 20
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pi/llm_trace.html", body)
        zf.writestr("manifest.json", '{"ok": 1}')
    rep = _scanner().scan_paths([z])
    rows = rep.to_dict()["findings"]
    assert rows == [{"file": f"{z}!pi/llm_trace.html", "kind": "upstream_secret", "masked": "sk-ups***", "count": 20}]


def test_allow_local_keys_downgrades_only_local(tmp_path):
    f = tmp_path / "m.json"
    f.write_text('{"key": "%s"}' % LOCAL)
    assert not _scanner().scan_paths([f]).ok
    rep = _scanner(allow_local_gateway_keys=True).scan_paths([f])
    assert rep.ok and rep.warnings


def test_gate_blocks_pack_product_zip(tmp_path, monkeypatch):
    from eval_harness import product_zip
    src = tmp_path / "triple"
    (src / "pi").mkdir(parents=True)
    (src / "manifest.json").write_text("{}")
    (src / "pi" / "llm_trace.html").write_text("leak " + UP)
    monkeypatch.setattr(secret_scan, "load_upstream_secrets", lambda: {UP})
    monkeypatch.setattr(secret_scan, "load_local_gateway_keys", lambda: {})
    out = tmp_path / "out.zip"
    with pytest.raises(SecretScanError):
        product_zip.pack_product_zip(src, out)
    assert not out.exists() and not (tmp_path / "out.zip.tmp").exists()
    (src / "pi" / "llm_trace.html").write_text("clean")
    assert product_zip.pack_product_zip(src, out).is_file()
    with pytest.raises(FileExistsError):
        product_zip.pack_product_zip(src, out)


def test_nested_xlsx_inside_zip(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", compression=zipfile.ZIP_DEFLATED) as x:
        x.writestr("xl/worksheets/sheet5.xml", "<c>" + UP + "</c>")
    z = tmp_path / "prod.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pi/results.xlsx", inner.getvalue())
    rows = _scanner().scan_paths([z]).to_dict()["findings"]
    assert rows[0]["file"].endswith("!pi/results.xlsx!xl/worksheets/sheet5.xml") and rows[0]["count"] == 1
