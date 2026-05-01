#!/usr/bin/env python3
"""Bump OpenLap version for *packaging-only* rebuilds (FFmpeg pin updates).

Policy:
- If current version is ``x.y.z`` → bump to ``x.y.z.1``
- If current version is ``x.y.z.n`` → bump to ``x.y.z.(n+1)``

This keeps functional releases on ``x.y.z`` and FFmpeg-only rebuilds on a 4th
segment (e.g. ``0.3.0.2``).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VER_FILE = ROOT / "_version.py"


def main() -> int:
    text = VER_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?"', text)
    if not m:
        print("ERROR: could not parse __version__ in _version.py", file=sys.stderr)
        return 1
    maj, mino, pat = int(m.group(1)), int(m.group(2)), int(m.group(3))
    build = m.group(4)
    if build is None:
        newv = f"{maj}.{mino}.{pat}.1"
    else:
        newv = f"{maj}.{mino}.{pat}.{int(build) + 1}"
    VER_FILE.write_text(f'__version__ = "{newv}"\n', encoding="utf-8")
    go = (os.environ.get("GITHUB_OUTPUT") or "").strip()
    if go:
        with open(go, "a", encoding="utf-8") as f:
            f.write(f"new_version={newv}\n")
    print(newv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
