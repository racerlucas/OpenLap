#!/usr/bin/env python3
"""
Download FFmpeg (Windows x64) into ``third_party/ffmpeg/win64/bin/``
for local dev and PyInstaller (see ``OpenLap.spec`` / ``ffmpeg_paths.py``).

Usage:
  python tools/fetch_ffmpeg.py              # same as --latest
  python tools/fetch_ffmpeg.py --latest   # BtbN GitHub ``releases/latest`` win64 GPL zip
  python tools/fetch_ffmpeg.py --stable   # fixed Gyan ``ffmpeg-release-essentials.zip``

Override URL (wins over --latest / --stable):
  set OPENLAP_FFMPEG_URL=https://...
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST_BIN = ROOT / 'third_party' / 'ffmpeg' / 'win64' / 'bin'

STABLE_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
GH_API_LATEST = 'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest'


def latest_btb_win64_gpl_zip_url() -> str:
    """Resolve browser_download_url for latest BtbN win64 GPL (non-shared) .zip asset."""
    req = urllib.request.Request(
        GH_API_LATEST,
        headers={
            'User-Agent': 'OpenLap-fetch-ffmpeg/2',
            'Accept': 'application/vnd.github+json',
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.loads(r.read().decode('utf-8'))
    assets = payload.get('assets') or []
    cands: list[tuple[str, str]] = []
    for a in assets:
        name = str(a.get('name') or '')
        if not name.lower().endswith('.zip'):
            continue
        ln = name.lower()
        if 'win64' not in ln or 'gpl' not in ln:
            continue
        if 'shared' in ln or 'lgpl' in ln:
            continue
        url = str(a.get('browser_download_url') or '').strip()
        if url:
            cands.append((name, url))
    if not cands:
        raise RuntimeError(
            'No suitable win64 GPL .zip in BtbN FFmpeg-Builds latest release '
            f'(see {GH_API_LATEST})'
        )
    # Prefer smaller "essentials" style name when multiple match
    cands.sort(key=lambda x: (0 if 'essential' in x[0].lower() else 1, len(x[0])))
    return cands[0][1]


def download_and_extract(url: str) -> int:
    print(f'Downloading:\n  {url}\n→ {DEST_BIN}')

    req = urllib.request.Request(url, headers={'User-Agent': 'OpenLap-fetch-ffmpeg/2'})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        print(f'ERROR: HTTP {e.code} downloading FFmpeg: {e.reason}', file=sys.stderr)
        return 3
    except OSError as e:
        print(f'ERROR: download failed: {e}', file=sys.stderr)
        return 3

    wanted = {'ffmpeg.exe', 'ffprobe.exe'}
    found: set[str] = set()
    DEST_BIN.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                base = Path(name).name
                if base.lower() not in wanted:
                    continue
                out = DEST_BIN / base
                out.write_bytes(zf.read(name))
                found.add(base.lower())
                print(f'  extracted {base}')
    except zipfile.BadZipFile as e:
        print(f'ERROR: not a valid zip: {e}', file=sys.stderr)
        return 4

    if found != {x.lower() for x in wanted}:
        print('ERROR: archive did not contain both ffmpeg.exe and ffprobe.exe.', file=sys.stderr)
        return 2

    info = {'url': url, 'dest': str(DEST_BIN)}
    info_path = ROOT / 'third_party' / 'ffmpeg' / 'win64' / 'BUILD_INFO.json'
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(json.dumps(info, indent=2), encoding='utf-8')
    print(f'Wrote {info_path}')
    print('Done.')
    return 0


def resolve_url(mode: str) -> str:
    env = (os.environ.get('OPENLAP_FFMPEG_URL') or '').strip()
    if env:
        return env
    if mode == 'stable':
        return STABLE_URL
    # latest
    return latest_btb_win64_gpl_zip_url()


def main(argv: list[str] | None = None) -> int:
    if sys.platform != 'win32':
        print('This script only stages the Windows x64 layout used by OpenLap.spec.', file=sys.stderr)
        return 1

    p = argparse.ArgumentParser(description='Stage ffmpeg.exe / ffprobe.exe for OpenLap.')
    p.add_argument(
        '--latest',
        action='store_const',
        const='latest',
        dest='mode',
        help='use BtbN FFmpeg-Builds GitHub latest release (default)',
    )
    p.add_argument(
        '--stable',
        action='store_const',
        const='stable',
        dest='mode',
        help='use fixed Gyan ffmpeg-release-essentials.zip',
    )
    p.set_defaults(mode='latest')
    args = p.parse_args(argv)

    try:
        url = resolve_url(args.mode)
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 5

    return download_and_extract(url)


if __name__ == '__main__':
    raise SystemExit(main())
