#!/usr/bin/env python3
"""
After ``pyinstaller OpenLap.spec``, copy staged ``node.exe`` into ``dist/OpenLap/Library/node/``.

Same layout as ``openlap_paths.library_node_dir()`` at runtime when frozen.

Usage (from repository root):

  pyinstaller OpenLap.spec --clean -y
  python tools/stage_dist_library_node.py

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
        print(f"[stage_dist_library_node] Missing dist folder: {root}", file=sys.stderr)
        return 1

    if sys.platform != "win32":
        print("[stage_dist_library_node] Skipping (Windows-only staged layout).")
        return 0

    src = HERE / "third_party" / "node" / "win64" / "node.exe"
    dst_dir = root / "Library" / "node"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "node.exe"

    if not src.is_file():
        print(
            "[stage_dist_library_node] Staged node.exe not found. Run:\n"
            "  python tools/fetch_node.py\n"
            f"  Expected: {src}",
            file=sys.stderr,
        )
        return 1

    shutil.copy2(src, dst)
    info_src = HERE / "third_party" / "node" / "win64" / "BUILD_INFO.txt"
    if info_src.is_file():
        shutil.copy2(info_src, dst_dir / "BUILD_INFO.txt")

    print(f"[stage_dist_library_node] → {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
