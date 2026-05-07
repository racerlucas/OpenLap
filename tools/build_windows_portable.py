#!/usr/bin/env python3
"""Build Windows portable onedir and create a zip artifact.

Build pipeline:
- Run PyInstaller using ``OpenLap.spec`` (onedir: ``<distpath>/OpenLap/``; default ``dist/OpenLap/``)
- Stage FFmpeg into ``<distpath>/OpenLap/Library/ffmpeg/``
- Stage Node ``node.exe`` into ``<distpath>/OpenLap/Library/node/``
- Stage only ``tracks/README.txt`` and ``tracks/*.template.json`` into ``<distpath>/OpenLap/tracks/``
- Zip the whole onedir folder into one file under ``dist/OpenLap_<version>.zip``

If the repository is on a UNC/SMB path, this script defaults ``--distpath`` / ``--workpath``
to ``%%LOCALAPPDATA%%\\OpenLap\\pyinstaller_builds\\…`` so PyInstaller's ``--clean`` step
does not try to deep-delete trees on the network share (often WinError 5 / 87).

With ``--keep-dist``, if the onedir was built under that local ``--distpath``, the script
mirrors it to ``<repo>/dist/OpenLap/`` after zipping so the unpacked folder exists on the
share (PyInstaller never wrote there directly).
"""
from __future__ import annotations

import os
import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

HERE = Path(__file__).resolve().parent.parent


def _is_unc(p: Path) -> bool:
    s = str(p)
    return s.startswith("\\\\") or s.startswith("//")


def _default_local_build_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or tempfile.gettempdir())
    return base / "OpenLap" / "pyinstaller_builds"


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
        help="Only keep the zip artifact (delete unpacked onedir after zipping). Default behavior.",
    )
    ap.add_argument(
        "--keep-dist",
        action="store_true",
        help=(
            "After a successful zip, keep the unpacked onedir. "
            "If PyInstaller output is under repo dist/OpenLap/, it stays there. "
            "If output is on another drive (e.g. UNC auto-local distpath), copy it to repo dist/OpenLap/ "
            "(then remove the local staging copy). Does not disable PyInstaller --clean."
        ),
    )
    ap.add_argument(
        "--distpath",
        default="",
        help="PyInstaller --distpath (parent of OpenLap/). Default: repo/dist, or %%LOCALAPPDATA%% when repo is on UNC.",
    )
    ap.add_argument(
        "--workpath",
        default="",
        help="PyInstaller --workpath. Default: repo/build, or next to --distpath when repo is on UNC.",
    )
    ap.add_argument(
        "--no-local-unc",
        action="store_true",
        help="Do not auto-redirect dist/work to a local drive when the repo lives on UNC/SMB.",
    )
    args, extra = ap.parse_known_args(sys.argv[1:])

    # Default is zip-only unless explicitly overridden.
    keep_dist = bool(args.keep_dist)
    if args.zip_only:
        keep_dist = False

    distpath = (args.distpath or "").strip()
    workpath = (args.workpath or "").strip()

    # PyInstaller --clean uses shutil.rmtree on the onedir; deep trees on UNC/SMB often fail (WinError 5 / 87).
    use_local_unc = (not args.no_local_unc) and _is_unc(HERE)
    if use_local_unc and not distpath:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        distpath = str(_default_local_build_root() / f"dist-{stamp}")
    if use_local_unc and not workpath:
        workpath = str(Path(distpath).parent / f"work-{Path(distpath).name}")

    if use_local_unc and (distpath or workpath):
        print(
            "[build_windows_portable] Repo on UNC/SMB: using local PyInstaller paths "
            f"(distpath={distpath!s}, workpath={workpath!s}). "
            "Zip still goes to repo dist/. With --keep-dist, onedir is copied to repo dist/OpenLap/ after zip.",
        )

    pyi_cmd = [sys.executable, "-m", "PyInstaller", "OpenLap.spec", "--clean", "-y", *extra]
    if distpath:
        pyi_cmd += ["--distpath", str(Path(distpath).resolve())]
    if workpath:
        pyi_cmd += ["--workpath", str(Path(workpath).resolve())]

    r = subprocess.run(pyi_cmd, cwd=str(HERE))
    if r.returncode != 0:
        return r.returncode

    dist_dir = Path(distpath).resolve() / "OpenLap" if distpath else (HERE / "dist" / "OpenLap")
    r2 = subprocess.call(
        [sys.executable, str(HERE / "tools" / "stage_dist_library_ffmpeg.py"), str(dist_dir)],
        cwd=str(HERE),
    )
    if r2 != 0:
        return int(r2)

    r2b = subprocess.call(
        [sys.executable, str(HERE / "tools" / "stage_dist_library_node.py"), str(dist_dir)],
        cwd=str(HERE),
    )
    if r2b != 0:
        return int(r2b)

    r3 = subprocess.call(
        [sys.executable, str(HERE / "tools" / "stage_dist_tracks.py"), str(dist_dir)],
        cwd=str(HERE),
    )
    if r3 != 0:
        return int(r3)

    # Create one zip artifact in dist/: OpenLap_<version>.zip
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

    # Remove or relocate the unpacked onedir (default: zip-only).
    # CLI flag wins; env var is kept for backward-compat.
    if not keep_dist:
        keep_env = os.environ.get("KEEP_DIST_DIR", "").strip().lower() in ("1", "true", "yes")
        if keep_env:
            keep_dist = True

    repo_dist_openlap = HERE / "dist" / "OpenLap"
    if not keep_dist:
        shutil.rmtree(dist_dir, ignore_errors=True)
    elif dist_dir.resolve() != repo_dist_openlap.resolve():
        # UNC (or custom --distpath): user expects repo dist/OpenLap/ — mirror from local staging.
        repo_dist_openlap.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(repo_dist_openlap, ignore_errors=True)
        try:
            shutil.copytree(dist_dir, repo_dist_openlap, symlinks=False, dirs_exist_ok=False)
        except OSError as exc:
            print(
                f"[build_windows_portable] Warning: could not copy onedir to {repo_dist_openlap}: {exc}\n"
                f"  Left build output at: {dist_dir}",
                file=sys.stderr,
            )
        else:
            shutil.rmtree(dist_dir, ignore_errors=True)
            print(f"[build_windows_portable] Mirrored onedir to {repo_dist_openlap}")

    print(f"[build_windows_portable] Wrote {zip_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
