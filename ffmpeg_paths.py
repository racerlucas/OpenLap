"""
Resolve ffmpeg / ffprobe executables for dev trees and PyInstaller bundles.

Dev / repo:
  ``<repo>/third_party/ffmpeg/win64/bin/ffmpeg.exe`` (+ ffprobe.exe)

Frozen (Windows portable onedir):
  ``<exe_dir>/Library/ffmpeg/ffmpeg.exe`` — populated at **build time** by
  ``tools/stage_dist_library_ffmpeg.py`` (not inside ``_internal/``).

Fallback: ``sys._MEIPASS`` root (legacy bundles), staged ``third_party``, repo root, PATH.

``rthooks/pyi_rth_path.py`` prepends ``Library/ffmpeg`` and ``_MEIPASS`` to PATH.

Override at runtime: set ``FFMPEG_BIN`` / ``FFPROBE_BIN`` to full paths.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parent


def _portable_library_bin(name: str) -> Optional[Path]:
    if not getattr(sys, 'frozen', False):
        return None
    from openlap_paths import library_ffmpeg_dir

    ext = '.exe' if sys.platform == 'win32' else ''
    d = library_ffmpeg_dir()
    for fn in (f'{name}{ext}', name):
        p = d / fn
        if p.is_file():
            return p
    return None


def _meipass_bin(name: str) -> Optional[Path]:
    if not getattr(sys, 'frozen', False) or not hasattr(sys, '_MEIPASS'):
        return None
    me = Path(sys._MEIPASS)
    ext = '.exe' if sys.platform == 'win32' else ''
    for fn in (f'{name}{ext}', name):
        p = me / fn
        if p.is_file():
            return p
    return None


def _staged_third_party_bin(name: str) -> Optional[Path]:
    """Prefer project-staged FFmpeg (``tools/fetch_ffmpeg.py`` / manual copy)."""
    if sys.platform == 'win32':
        p = _ROOT / 'third_party' / 'ffmpeg' / 'win64' / 'bin' / f'{name}.exe'
        return p if p.is_file() else None
    for sub in ('linux64', 'linux', 'osx64', 'darwin'):
        p = _ROOT / 'third_party' / 'ffmpeg' / sub / 'bin' / name
        if p.is_file():
            return p
    return None


def _repo_root_bin(name: str) -> Optional[Path]:
    ext = '.exe' if sys.platform == 'win32' else ''
    p = _ROOT / f'{name}{ext}'
    return p if p.is_file() else None


def get_ffmpeg_bin() -> str:
    """Absolute path to ffmpeg when bundled/staged; else ``shutil.which`` or ``'ffmpeg'``."""
    ev = os.environ.get('FFMPEG_BIN')
    if ev:
        ep = Path(ev)
        if ep.is_file():
            return str(ep)
        if shutil.which(ev):
            return ev
    p = (
        _portable_library_bin('ffmpeg')
        or _meipass_bin('ffmpeg')
        or _staged_third_party_bin('ffmpeg')
        or _repo_root_bin('ffmpeg')
    )
    if p is not None:
        return str(p)
    w = shutil.which('ffmpeg')
    return w if w else 'ffmpeg'


def get_ffprobe_bin() -> str:
    """Absolute path to ffprobe when bundled/staged; else ``which`` or ``'ffprobe'``."""
    ev = os.environ.get('FFPROBE_BIN')
    if ev:
        ep = Path(ev)
        if ep.is_file():
            return str(ep)
        if shutil.which(ev):
            return ev
    p = (
        _portable_library_bin('ffprobe')
        or _meipass_bin('ffprobe')
        or _staged_third_party_bin('ffprobe')
        or _repo_root_bin('ffprobe')
    )
    if p is not None:
        return str(p)
    w = shutil.which('ffprobe')
    return w if w else 'ffprobe'


def resolve_media_cmd(cmd: List[str]) -> List[str]:
    """If ``cmd[0]`` is ``ffmpeg`` or ``ffprobe``, replace with resolved executable."""
    if not cmd:
        return cmd
    c0 = cmd[0]
    if c0 == 'ffmpeg':
        return [get_ffmpeg_bin()] + list(cmd[1:])
    if c0 == 'ffprobe':
        return [get_ffprobe_bin()] + list(cmd[1:])
    return list(cmd)
