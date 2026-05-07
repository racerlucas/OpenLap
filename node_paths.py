"""
Resolve ``node.exe`` for headless Canvas export (same idea as ``ffmpeg_paths``).

Portable Windows layout (PyInstaller onedir, next to ``OpenLap.exe``):
  ``<exe_dir>/Library/node/node.exe`` — staged at build time by
  ``tools/stage_dist_library_node.py`` after ``tools/fetch_node.py``.

Dev / repo (Windows):
  ``<repo>/third_party/node/win64/node.exe`` (optional; run ``python tools/fetch_node.py``).

Fallback: ``shutil.which('node')`` so developers can still use a system install.

Override: set ``OPENLAP_NODE`` to the full path of ``node`` / ``node.exe``.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent


def _staged_third_party_node_win() -> Optional[Path]:
    if sys.platform != "win32":
        return None
    p = _ROOT / "third_party" / "node" / "win64" / "node.exe"
    return p if p.is_file() else None


def _portable_library_node() -> Optional[Path]:
    if not getattr(sys, "frozen", False):
        return None
    from openlap_paths import library_node_dir

    d = library_node_dir()
    if sys.platform == "win32":
        p = d / "node.exe"
    else:
        p = d / "node"
    return p if p.is_file() else None


def get_node_exe() -> str:
    """Absolute path to ``node`` when bundled/staged; else ``which('node')`` or ``''``."""
    ev = (os.environ.get("OPENLAP_NODE") or "").strip()
    if ev:
        ep = Path(ev)
        if ep.is_file():
            return str(ep)
        w = shutil.which(ev)
        if w:
            return w
    p = _portable_library_node() or _staged_third_party_node_win()
    if p is not None:
        return str(p)
    w = shutil.which("node")
    return w if w else ""
