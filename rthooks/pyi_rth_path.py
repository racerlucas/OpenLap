"""
PyInstaller runtime hook — fix PATH and Playwright browser location.

1. Prepend ``<exe_dir>/Library/node``, ``<exe_dir>/Library/ffmpeg``, then ``_MEIPASS``
   to PATH so bundled ``node`` / ffmpeg/ffprobe are found when invoked by bare name.

2. Set PLAYWRIGHT_BROWSERS_PATH to ``<exe_dir>/ms-playwright`` so Chromium lives beside
   the portable app (same as ``openlap_paths.playwright_browsers_dir()`` when frozen).
   Without this, the bundled driver looks under _internal\\...\\.local-browsers\\, which
   is never populated.
"""
import os
import sys

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    _exe_dir = os.path.dirname(sys.executable)
    _portable_node = os.path.join(_exe_dir, 'Library', 'node')
    _portable_ff = os.path.join(_exe_dir, 'Library', 'ffmpeg')
    os.environ['PATH'] = (
        _portable_node
        + os.pathsep
        + _portable_ff
        + os.pathsep
        + sys._MEIPASS
        + os.pathsep
        + os.environ.get('PATH', '')
    )

    _pw_browsers = os.path.join(_exe_dir, 'ms-playwright')
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', _pw_browsers)
