#!/usr/bin/env python3
"""
Resolve BtbN FFmpeg-Builds ``releases/latest`` identity for CI pin / release notes.

BtbN keeps ``tag_name`` as ``latest`` while publishing new zips; the GitHub **release**
and **asset** numeric ids change when a new build ships, so ``pin`` uses those ids.

Writes GitHub Actions output when ``GITHUB_OUTPUT`` is set; otherwise prints pin to stdout.

  pin       — ``{release_id}|{asset_id}`` for ``.github/ffmpeg-release-pin.txt``
  tag       — GitHub release ``tag_name`` (often ``latest``)
  asset     — chosen win64 GPL (non-shared) .zip asset name
  zip_url   — ``browser_download_url`` for that asset
  published — release ``published_at`` (ISO8601)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

GH_API_LATEST = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"


def resolve_payload() -> tuple[str, str, str, str, str, int, int]:
    req = urllib.request.Request(
        GH_API_LATEST,
        headers={
            "User-Agent": "OpenLap-ffmpeg-release-identity/1",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.loads(r.read().decode("utf-8"))
    rel_id = int(payload.get("id") or 0)
    if rel_id <= 0:
        raise RuntimeError("BtbN latest release missing id")
    published = str(payload.get("published_at") or "").strip()
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise RuntimeError("BtbN latest release missing tag_name")
    assets = payload.get("assets") or []
    cands: list[tuple[str, str]] = []
    for a in assets:
        name = str(a.get("name") or "")
        if not name.lower().endswith(".zip"):
            continue
        ln = name.lower()
        if "win64" not in ln or "gpl" not in ln:
            continue
        if "shared" in ln or "lgpl" in ln:
            continue
        url = str(a.get("browser_download_url") or "").strip()
        if url:
            cands.append((name, url))
    if not cands:
        raise RuntimeError("No suitable win64 GPL .zip in BtbN FFmpeg-Builds latest release")
    cands.sort(key=lambda x: (0 if "essential" in x[0].lower() else 1, len(x[0])))
    asset_name, zip_url = cands[0]
    asset_id = 0
    for a in assets:
        if str(a.get("name") or "") == asset_name:
            asset_id = int(a.get("id") or 0)
            break
    if asset_id <= 0:
        raise RuntimeError("Could not resolve asset id for chosen zip")
    pin = f"{rel_id}|{asset_id}"
    return pin, tag, asset_name, zip_url, published, rel_id, asset_id


def main() -> int:
    pin, tag, asset_name, zip_url, published, rel_id, asset_id = resolve_payload()
    go = (os.environ.get("GITHUB_OUTPUT") or "").strip()
    if go:
        with open(go, "a", encoding="utf-8") as f:
            f.write(f"pin={pin}\n")
            f.write(f"tag={tag}\n")
            f.write(f"asset={asset_name}\n")
            f.write(f"zip_url={zip_url}\n")
            f.write(f"published={published}\n")
            f.write(f"release_id={rel_id}\n")
            f.write(f"asset_id={asset_id}\n")
    else:
        print(pin)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
