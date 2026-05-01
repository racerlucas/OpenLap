#!/usr/bin/env python3
"""Build Windows portable onedir and create a zip artifact.

Build pipeline:
- Run PyInstaller using ``OpenLap.spec`` (onedir: dist/OpenLap/)
- Stage FFmpeg into ``dist/OpenLap/Library/ffmpeg/``
- Zip the whole onedir folder into one file for GitHub Releases
"""
from __future__ import annotations

import os
import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

HERE = Path(__file__).resolve().parent.parent


def _zip_dir_flat(src_dir: Path, zip_path: Path) -> None:
    """Zip *contents* of src_dir to zip root (no extra parent folder)."""
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as zf:
        for p in src_dir.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(src_dir).as_posix()
            zf.write(p, rel)


def main() -> int:
    if sys.platform != "win32":
        print("[build_windows_portable] This helper is Windows-only.")
        return 2

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument(
        "--zip-only",
        action="store_true",
        help="Only keep the zip artifact (delete dist/OpenLap after zipping). Default behavior.",
    )
    ap.add_argument(
        "--keep-dist",
        action="store_true",
        help="Keep dist/OpenLap folder after zipping (useful for debugging).",
    )
    args, extra = ap.parse_known_args(sys.argv[1:])

    # Default is zip-only unless explicitly overridden.
    keep_dist = bool(args.keep_dist)
    if args.zip_only:
        keep_dist = False

    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "OpenLap.spec", "--clean", "-y", *extra],
        cwd=str(HERE),
    )
    if r.returncode != 0:
        return r.returncode

    r2 = subprocess.call([sys.executable, str(HERE / "tools" / "stage_dist_library_ffmpeg.py")], cwd=str(HERE))
    if r2 != 0:
        return int(r2)

    # Create one zip artifact in dist/: OpenLap_<version>.zip
    dist_dir = HERE / "dist" / "OpenLap"
    if not dist_dir.is_dir():
        print(f"[build_windows_portable] Missing dist folder: {dist_dir}")
        return 1

    try:
        # Running from tools/ means repo root is not on sys.path by default.
        sys.path.insert(0, str(HERE))
        from _version import __version__
        ver = str(__version__).strip() or "unknown"
    except Exception:
        ver = "unknown"

    zip_name = HERE / "dist" / f"OpenLap_{ver}.zip"
    zip_name.parent.mkdir(parents=True, exist_ok=True)
    if zip_name.exists():
        zip_name.unlink()

    _zip_dir_flat(dist_dir, zip_name)

    # Remove the unpacked onedir so the build output is a single artifact.
    # CLI flag wins; env var is kept for backward-compat.
    if not keep_dist:
        keep_env = os.environ.get("KEEP_DIST_DIR", "").strip().lower() in ("1", "true", "yes")
        if keep_env:
            keep_dist = True
    if not keep_dist:
        shutil.rmtree(dist_dir, ignore_errors=True)

    print(f"[build_windows_portable] Wrote {zip_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
