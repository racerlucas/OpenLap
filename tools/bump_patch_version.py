#!/usr/bin/env python3
"""Bump ``x.y.z`` → ``x.y.(z+1)`` in ``_version.py`` (repo root)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VER_FILE = ROOT / "_version.py"


def main() -> int:
    text = VER_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        print("ERROR: could not parse __version__ in _version.py", file=sys.stderr)
        return 1
    maj, mino, pat = int(m.group(1)), int(m.group(2)), int(m.group(3))
    newv = f"{maj}.{mino}.{pat + 1}"
    VER_FILE.write_text(f'__version__ = "{newv}"\n', encoding="utf-8")
    go = (os.environ.get("GITHUB_OUTPUT") or "").strip()
    if go:
        with open(go, "a", encoding="utf-8") as f:
            f.write(f"new_version={newv}\n")
    print(newv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
