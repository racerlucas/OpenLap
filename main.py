"""
main.py — OpenLap entry point.

Run with:
    python main.py

Telemetry / preview / export boundaries live in ``telemetry_algorithms.py`` and
``webview_api.py`` module docstrings (what must match vs what may diverge).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


def _setup_logging() -> None:
    # Windows multiprocessing uses "spawn": worker processes re-import the
    # __main__ module and execute top-level code. If every worker attaches a
    # RotatingFileHandler to the same file, log rollover will race and crash
    # with WinError 32. Keep file logging in the main process only.
    import multiprocessing as mp

    is_child_process = (mp.parent_process() is not None) or (mp.current_process().name != 'MainProcess')

    log_dir = Path.home() / '.openlap' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter('%(asctime)s %(levelname)-8s %(name)s — %(message)s')
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root = logging.getLogger()

    # Avoid duplicate handlers if this module is imported multiple times.
    if getattr(root, '_openlap_logging_configured', False):
        return
    setattr(root, '_openlap_logging_configured', True)

    root.setLevel(logging.DEBUG if not is_child_process else logging.INFO)
    root.addHandler(ch)

    if not is_child_process:
        fh = logging.handlers.RotatingFileHandler(
            str(log_dir / 'openlap.log'), maxBytes=2*1024*1024, backupCount=3, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Matplotlib can be extremely chatty at DEBUG (e.g. font matching).
    logging.getLogger('matplotlib').setLevel(logging.WARNING)
    logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)


_setup_logging()

from opencv_ffmpeg_env import apply_ffmpeg_capture_thread_limit

apply_ffmpeg_capture_thread_limit()

# ── Locate frontend assets ────────────────────────────────────────────────────
# When running from source:  frontend/ is next to main.py
# When bundled by PyInstaller: sys._MEIPASS contains extracted files
if getattr(sys, 'frozen', False):
    _BASE = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    _BASE = Path(__file__).parent

FRONTEND_DIR  = _BASE / 'frontend'
FRONTEND_HTML = FRONTEND_DIR / 'index.html'

if not FRONTEND_HTML.exists():
    sys.exit(f'Frontend not found at {FRONTEND_HTML}')


def main():
    import webview
    from webview_api import WebviewAPI

    api = WebviewAPI()

    # pywebview's `icon` is only honored by the Windows (edgechromium/mshtml)
    # and Linux (GTK/QT) backends. On macOS the cocoa backend takes its icon
    # from the app bundle's Info.plist, so we skip it entirely there.
    _icon: str | None = None
    if sys.platform != 'darwin':
        candidate = str(_BASE / 'frontend' / 'icon.ico')
        if os.path.isfile(candidate):
            _icon = candidate

    window = webview.create_window(
        title      = 'OpenLap',
        url        = str(FRONTEND_HTML),
        js_api     = api,
        width      = 1280,
        height     = 840,
        min_size   = (960, 640),
        background_color = '#0d0f18',
    )

    api.set_window(window)

    # Use GUI thread blocking call — webview.start() must be on main thread.
    # Disable DevTools in packaged builds; keep enabled when running from source.
    webview.start(debug=not getattr(sys, 'frozen', False), icon=_icon)


if __name__ == '__main__':
    # Required for multiprocessing on Windows (used by video export workers)
    from multiprocessing import freeze_support
    freeze_support()
    main()
