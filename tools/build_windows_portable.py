#!/usr/bin/env python3
"""Run PyInstaller onedir build then stage FFmpeg into dist/OpenLap/Library/ffmpeg/."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def main() -> int:
    extra = list(sys.argv[1:])
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "OpenLap.spec", "--clean", "-y", *extra],
        cwd=str(HERE),
    )
    if r.returncode != 0:
        return r.returncode
    return subprocess.call([sys.executable, str(HERE / "tools" / "stage_dist_library_ffmpeg.py")], cwd=str(HERE))


if __name__ == "__main__":
    raise SystemExit(main())
