#!/usr/bin/env python3
"""
After ``pyinstaller OpenLap.spec``, copy staged FFmpeg into ``dist/OpenLap/Library/ffmpeg/``.

FFmpeg is not bundled inside ``_internal/`` so it sits beside ``OpenLap.exe`` (same layout as
``openlap_paths.library_ffmpeg_dir()`` at runtime).

Usage (from repository root):

  pyinstaller OpenLap.spec --clean -y
  python tools/stage_dist_library_ffmpeg.py

Optional argument: path to the onedir output folder (default: dist/OpenLap).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (HERE / "dist" / "OpenLap")
    if not root.is_dir():
        print(f"[stage_dist_library_ffmpeg] Missing dist folder: {root}", file=sys.stderr)
        return 1

    if sys.platform != "win32":
        print("[stage_dist_library_ffmpeg] Skipping (Windows-only staged layout).")
        return 0

    src_bin = HERE / "third_party" / "ffmpeg" / "win64" / "bin"
    dst = root / "Library" / "ffmpeg"
    dst.mkdir(parents=True, exist_ok=True)

    missing = []
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        sp = src_bin / name
        if not sp.is_file():
            missing.append(str(sp))
            continue
        shutil.copy2(sp, dst / name)

    if missing:
        print(
            "[stage_dist_library_ffmpeg] Staged FFmpeg not found. Run fetch first, e.g.\n"
            "  python tools/fetch_ffmpeg.py --latest\n"
            f"  Missing: {missing}",
            file=sys.stderr,
        )
        return 1

    info = HERE / "third_party" / "ffmpeg" / "win64" / "BUILD_INFO.json"
    if info.is_file():
        shutil.copy2(info, dst / "BUILD_INFO.json")

    print(f"[stage_dist_library_ffmpeg] → {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
