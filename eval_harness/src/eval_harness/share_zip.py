"""Package existing offline reports without changing HTML or source artifacts."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

from eval_harness.secret_scan import SecretScanError, gate

# These are omitted from the viewing package only, and restored if a page links to them.
OMITTED_SUFFIXES = {".jsonl", ".gz", ".xlsx", ".csv", ".tsv", ".log", ".stderr"}
MANIFEST = "LLMTRACE-SHARE-MANIFEST.json"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "base":
            raise ValueError("share packaging does not support HTML <base>; use the full archive")
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.links.append(value)


def _safe_name(name: str) -> None:
    if (not name or "\\" in name or name.startswith("/")
            or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ValueError(f"unsafe archive path: {name!r}")


def _digest(fh) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def pack_share_zip(src: Path, out: Path, *, allow_local_gateway_keys: bool = False,
                   include_root: bool = True, exclude_paths: tuple[Path, ...] = ()) -> dict:
    """Accept a report directory or full ZIP; never delete, rebuild, or overwrite either."""
    src, out = Path(src).resolve(), Path(out).absolute()
    if out.exists() or out.is_symlink():
        raise FileExistsError(f"refusing to overwrite existing zip: {out}")
    if src.is_dir() and out.resolve().is_relative_to(src):
        raise ValueError("put the share ZIP outside the source directory")
    excluded = {Path(p).absolute() for p in exclude_paths}
    with ExitStack() as stack:
        if src.is_dir():
            paths = {}
            for path in sorted(src.rglob("*")):
                if path in excluded:
                    continue
                if path.is_symlink():
                    raise ValueError(f"source contains a symlink: {path}")
                if path.is_file():
                    relative = path.relative_to(src).as_posix()
                    name = f"{src.name}/{relative}" if include_root else relative
                    _safe_name(name)
                    paths[name] = path
            sizes = {name: p.stat().st_size for name, p in paths.items()}
            def open_member(name):
                return paths[name].open("rb")
        elif src.is_file() and zipfile.is_zipfile(src):
            archive = stack.enter_context(zipfile.ZipFile(src))
            infos = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                _safe_name(info.filename)
                if info.filename in infos:
                    raise ValueError(f"duplicate archive path: {info.filename}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f"archive contains a symlink: {info.filename}")
                infos[info.filename] = info
            sizes = {name: i.file_size for name, i in infos.items()}
            def open_member(name):
                return archive.open(infos[name])
        else:
            raise ValueError(f"expected a report directory or ZIP: {src}")

        htmls = sorted(name for name in sizes if PurePosixPath(name).suffix.lower() == ".html")
        if not htmls:
            raise ValueError("no existing HTML reports; export HTML first")
        selected = {name for name in sizes
                    if PurePosixPath(name).suffix.lower() not in OMITTED_SUFFIXES
                    and not PurePosixPath(name).name.startswith("PACKAGE-")}
        links, html_hashes = [], {}
        external_links = 0
        for name in htmls:
            with open_member(name) as fh:
                data = fh.read()
            html_hashes[name] = hashlib.sha256(data).hexdigest()
            parser = Links()
            parser.feed(data.decode("utf-8"))
            for value in parser.links:
                url = urlsplit(value)
                if url.scheme or url.netloc:
                    external_links += 1
                    continue
                if not url.path:
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(url.path)))
                if target not in sizes:
                    raise ValueError(f"broken local link: {name} -> {value}")
                selected.add(target)
                links.append({"page": name, "target": target})
        selected.update(htmls)
        prefix = posixpath.commonpath([posixpath.dirname(name) for name in sizes])
        manifest_name = posixpath.join(prefix, MANIFEST)
        if manifest_name in sizes:
            raise ValueError("source is already a share package; use the original archive")
        manifest = {
            "source": src.name,
            "scope": "original HTML, linked attachments and ancillary files; viewing package only",
            "html_count": len(htmls), "selected_files": len(selected),
            "omitted_files": sorted(set(sizes) - selected),
            "local_links": links, "external_links": external_links,
            "files": [],
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=out.name + ".", suffix=".zip", dir=out.parent, delete=False) as fh:
            tmp = Path(fh.name)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as dst:
                for name in sorted(selected):
                    h = hashlib.sha256()
                    size = 0
                    with open_member(name) as reader, dst.open(name, "w", force_zip64=True) as writer:
                        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                            h.update(chunk)
                            size += len(chunk)
                            writer.write(chunk)
                    if size != sizes[name] or (name in html_hashes and h.hexdigest() != html_hashes[name]):
                        raise ValueError(f"source changed during packaging: {name}")
                    manifest["files"].append({"path": name, "bytes": size, "sha256": h.hexdigest()})
                dst.writestr(manifest_name, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            # Reuse the existing export gate, scanning selected bytes before publishing the ZIP.
            gate([tmp], what=f"share zip {out.name}", allow_local_gateway_keys=allow_local_gateway_keys)
            with zipfile.ZipFile(tmp) as dst:
                for item in manifest["files"]:
                    with dst.open(item["path"]) as fh:
                        if _digest(fh) != item["sha256"]:
                            raise ValueError(f"output bytes changed: {item['path']}")
            # Exclusive creation: a concurrently created output must not be overwritten.
            os.link(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
    return {"zip": str(out), "zip_bytes": out.stat().st_size,
            "html_count": len(htmls), "selected_files": len(selected),
            "omitted_files": len(sizes) - len(selected), "local_links_checked": len(links),
            "all_html_bytes_unchanged": True, "crc_and_hashes_verified": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path, help="existing report directory or full ZIP")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--allow-local-gateway-keys", action="store_true")
    args = ap.parse_args(argv)
    try:
        result = pack_share_zip(args.source, args.out, allow_local_gateway_keys=args.allow_local_gateway_keys)
    except SecretScanError:
        return 3
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"[share-zip] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
