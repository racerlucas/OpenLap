#!/usr/bin/env python3
"""
Download official Node.js Windows x64 **binary** zip and extract ``node.exe`` into
``third_party/node/win64/`` for local dev and portable builds (see ``node_paths.py``).

Usage (from repository root):

  python tools/fetch_node.py

Override version (default is pinned LTS below):

  set OPENLAP_NODE_VERSION=22.14.0

Skip in CI / offline:

  set SKIP_NODE_FETCH=1
"""
from __future__ import annotations

import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST_DIR = ROOT / "third_party" / "node" / "win64"
DEST_EXE = DEST_DIR / "node.exe"

_DEFAULT_VERSION = "22.14.0"


def _version() -> str:
    v = (os.environ.get("OPENLAP_NODE_VERSION") or "").strip()
    return v if v else _DEFAULT_VERSION


def _url(ver: str) -> str:
    return f"https://nodejs.org/dist/v{ver}/node-v{ver}-win-x64.zip"


def main() -> int:
    if sys.platform != "win32":
        print("[fetch_node] Skipping (Windows-only staged binary).")
        return 0

    skip = os.environ.get("SKIP_NODE_FETCH", "").strip().lower() in ("1", "true", "yes")
    if skip:
        print("[fetch_node] SKIP_NODE_FETCH is set — not downloading.")
        return 0

    if DEST_EXE.is_file():
        print(f"[fetch_node] Already present: {DEST_EXE}")
        return 0

    ver = _version()
    url = _url(ver)
    print(f"[fetch_node] Downloading Node.js v{ver} win-x64 …\n  {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "OpenLap-fetch-node/1"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read()
    except OSError as e:
        print(f"[fetch_node] ERROR: download failed: {e}", file=sys.stderr)
        return 3

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    found = False
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for name in zf.namelist():
            if name.endswith("/node.exe") and name.count("/") == 1:
                DEST_EXE.write_bytes(zf.read(name))
                found = True
                break

    if not found:
        print("[fetch_node] ERROR: node.exe not found inside zip", file=sys.stderr)
        return 4

    info = DEST_DIR / "BUILD_INFO.txt"
    try:
        info.write_text(f"version={ver}\nurl={url}\n", encoding="utf-8")
    except OSError:
        pass

    print(f"[fetch_node] → {DEST_EXE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
