#!/usr/bin/env python3
"""Build Windows portable onedir and create a zip artifact.

Build pipeline:
- Run PyInstaller using ``OpenLap.spec`` (onedir: dist/OpenLap/)
- Stage FFmpeg into ``dist/OpenLap/Library/ffmpeg/``
- Zip the whole onedir folder into one file for GitHub Releases
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def main() -> int:
    if sys.platform != "win32":
        print("[build_windows_portable] This helper is Windows-only.")
        return 2

    extra = list(sys.argv[1:])
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "OpenLap.spec", "--clean", "-y", *extra],
        cwd=str(HERE),
    )
    if r.returncode != 0:
        return r.returncode

    r2 = subprocess.call([sys.executable, str(HERE / "tools" / "stage_dist_library_ffmpeg.py")], cwd=str(HERE))
    if r2 != 0:
        return int(r2)

    # Create one zip artifact at repo root: OpenLap-<version>-Windows-portable.zip
    dist_dir = HERE / "dist" / "OpenLap"
    if not dist_dir.is_dir():
        print(f"[build_windows_portable] Missing dist folder: {dist_dir}")
        return 1

    try:
        from _version import __version__
        ver = str(__version__).strip() or "unknown"
    except Exception:
        ver = "unknown"

    zip_name = HERE / f"OpenLap-{ver}-Windows-portable.zip"
    if zip_name.exists():
        zip_name.unlink()

    # make_archive() wants the base name without extension
    base = str(zip_name.with_suffix(""))
    # Zip includes a top-level "OpenLap/" folder
    shutil.make_archive(base, "zip", root_dir=str(dist_dir.parent), base_dir=dist_dir.name)
    print(f"[build_windows_portable] Wrote {zip_name.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
