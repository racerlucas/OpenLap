#!/usr/bin/env python3
"""
After ``pyinstaller OpenLap.spec``, copy only public track templates into ``<onedir>/tracks/``.

Real ``*.json`` track files must never ship in the bundle (they stay local / user-added next to the exe).
``openlap_paths.tracks_dir()`` points at that same ``tracks/`` folder when frozen.

Usage (from repository root):

  pyinstaller OpenLap.spec --clean -y
  python tools/stage_dist_library_ffmpeg.py   # Windows
  python tools/stage_dist_tracks.py

Optional argument: onedir output folder (default: dist/OpenLap). For a macOS ``.app``, pass
``dist/OpenLap.app`` — the script places files under ``Contents/MacOS/tracks/`` when that layout exists.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SRC = HERE / "tracks"


def _bundle_exe_parent(dist_path: Path) -> Path:
    """Directory that contains the frozen executable (``app_data_dir`` parent when frozen)."""
    p = dist_path.resolve()
    if p.suffix == ".app" and p.is_dir():
        macos = p / "Contents" / "MacOS"
        if macos.is_dir():
            return macos
    return p


def main() -> int:
    root_arg = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (HERE / "dist" / "OpenLap")
    root = _bundle_exe_parent(root_arg)
    if not root.is_dir():
        print(f"[stage_dist_tracks] Missing dist folder: {root_arg}", file=sys.stderr)
        return 1
    if not SRC.is_dir():
        print(f"[stage_dist_tracks] Missing source tracks dir: {SRC}", file=sys.stderr)
        return 1

    dst = root / "tracks"
    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    readme = SRC / "README.txt"
    if readme.is_file():
        shutil.copy2(readme, dst / "README.txt")
        copied += 1

    for p in sorted(SRC.glob("*.template.json")):
        if p.is_file():
            shutil.copy2(p, dst / p.name)
            copied += 1

    if copied == 0:
        print("[stage_dist_tracks] No README.txt or *.template.json under tracks/.", file=sys.stderr)
        return 1

    print(f"[stage_dist_tracks] → {dst} ({copied} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
