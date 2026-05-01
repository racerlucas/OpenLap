# -*- mode: python ; coding: utf-8 -*-
# OpenLap.spec — PyInstaller build spec
#
# Build:
#   pip install pyinstaller
#   python tools/build_windows_portable.py
#   (or: pyinstaller OpenLap.spec  then  python tools/stage_dist_library_ffmpeg.py)
#
# Output: dist/OpenLap/  (onedir portable folder: OpenLap.exe, _internal/, Library/ffmpeg/, …)
# User data (config, tracks, caches) resolves next to the exe when frozen; dev uses <repo>/.openlap/.
# FFmpeg is NOT packed into _internal/: run stage_dist_library_ffmpeg.py so it lives under Library/ffmpeg/.
#
# Requires:
#   - On Windows, each PyInstaller run auto-downloads **latest** FFmpeg (BtbN
#     GitHub release) into third_party/ffmpeg/win64/bin/ via tools/fetch_ffmpeg.py.
#     Skip: set environment variable SKIP_FFMPEG_FETCH=1
#   - Fallback if fetch skipped / failed: ffmpeg.exe next to this spec or PATH (ffmpeg_paths.py)
#   - All Python deps installed in the active environment

import os, sys, subprocess as _sp
from pathlib import Path
import playwright as _pw_mod

HERE = Path(SPECPATH)

# ── Safety: never bundle repository-local commercial tracks ───────────────────
# Real track JSONs are not allowed to ship inside the app bundle. If the working
# tree contains any non-template tracks/*.json, fail fast unless explicitly allowed.
_tracks_dir = HERE / 'tracks'
if _tracks_dir.is_dir():
    _real_tracks = [p for p in _tracks_dir.glob('*.json') if not p.name.endswith('.template.json')]
    if _real_tracks and os.environ.get('ALLOW_BUNDLE_TRACKS', '').strip().lower() not in ('1', 'true', 'yes'):
        raise RuntimeError(
            '[OpenLap.spec] Refusing to build: found non-template tracks/*.json (commercial). '
            'Keep user tracks outside the repo (e.g. next to the packaged exe under tracks/) '
            'or set ALLOW_BUNDLE_TRACKS=1 to override.'
        )

# ── Auto-fetch FFmpeg (Windows) before bundling ───────────────────────────────
# Default behavior: only download when the staged binaries are missing.
# Force update: set FORCE_FFMPEG_FETCH=1
if sys.platform == 'win32':
    _staged_dir = HERE / 'third_party' / 'ffmpeg' / 'win64' / 'bin'
    _staged_ok = (_staged_dir / 'ffmpeg.exe').is_file() and (_staged_dir / 'ffprobe.exe').is_file()
    _skip = os.environ.get('SKIP_FFMPEG_FETCH', '').strip().lower() in ('1', 'true', 'yes')
    _force = os.environ.get('FORCE_FFMPEG_FETCH', '').strip().lower() in ('1', 'true', 'yes')
    if not _skip and (not _staged_ok or _force):
        _ff_script = HERE / 'tools' / 'fetch_ffmpeg.py'
        if _ff_script.is_file():
            why = 'forced' if _force else 'missing staged binaries'
            print(f'[OpenLap.spec] Downloading latest FFmpeg ({why}) → third_party/ffmpeg/win64/bin/ …')
            _pr = _sp.run(
                [sys.executable, str(_ff_script), '--latest'],
                cwd=str(HERE),
            )
            if _pr.returncode != 0:
                raise RuntimeError(
                    '[OpenLap.spec] FFmpeg fetch failed (exit %s). '
                    'Check network, or set SKIP_FFMPEG_FETCH=1 and place ffmpeg.exe/ffprobe.exe manually.'
                    % _pr.returncode
                )
        else:
            print('[OpenLap.spec] warning: tools/fetch_ffmpeg.py missing — using existing third_party / PATH')

# ── Data files ────────────────────────────────────────────────────────────────
datas = [
    # Frontend (HTML/CSS/JS)
    (str(HERE / 'frontend'), 'frontend'),
    # Style plugins (matplotlib gauge renderers for video export)
    (str(HERE / 'styles'), 'styles'),
    # Playwright — bundle the entire package including its Node.js driver
    # so RaceBox cloud download works without any extra installs.
    (os.path.dirname(_pw_mod.__file__), 'playwright'),
]

# AIM / DLL files present in the project root
_dlls = [
    'MatLabXRK-2022-64-ReleaseU.dll',
    'libiconv-2.dll',
    'libxml2-2.dll',
    'libz.dll',
    'pthreadVC2_x64.dll',
]
for dll in _dlls:
    p = HERE / dll
    if p.is_file():
        datas.append((str(p), '.'))

# FFmpeg: staged under third_party/… then copied to dist/OpenLap/Library/ffmpeg/ by
# tools/stage_dist_library_ffmpeg.py (not bundled into _internal/).

# Licenses / notices
_lic_dir = HERE / 'licenses'
if _lic_dir.is_dir():
    datas.append((str(_lic_dir), 'licenses'))

# ── Hidden imports ────────────────────────────────────────────────────────────
# PyInstaller cannot automatically detect dynamically-imported modules.
# Include all style plugins and data loaders referenced at runtime.
hidden_imports = [
    # Style plugins (loaded by style_registry.py via importlib)
    'styles.gauge_bar',
    'styles.gauge_compare',
    'styles.gauge_delta',
    'styles.gauge_dial',
    'styles.gauge_gmeter',
    'styles.gauge_image',
    'styles.gauge_info',
    'styles.gauge_lap_scoreboard',
    'styles.gauge_lean',
    'styles.gauge_line',
    'styles.gauge_multiline',
    'styles.gauge_numeric',
    'styles.gauge_sector_bar',
    'styles.gauge_splits',
    'styles.map_circuit',
    'styles.map_progress',
    'styles.map_zoomed',
    # Data loaders
    'racebox_data',
    'aim_data',
    'gpx_data',
    'motec_data',
    # PyWebView internals (platform-specific backends)
    'webview',
    'webview.platforms',
    'webview.platforms.winforms',  # Windows
    'clr',                         # pythonnet (required by winforms backend)
    # Multiprocessing support
    'multiprocessing.pool',
    'multiprocessing.managers',
    # OpenCV
    'cv2',
    # Matplotlib backends (headless)
    'matplotlib',
    'matplotlib.backends.backend_agg',
    # Misc runtime imports
    'numpy',
    'pandas',
    'PIL',
    'PIL.Image',
    'xml.etree.ElementTree',
    'json',
    'logging.handlers',
    # Playwright (RaceBox cloud download)
    'playwright',
    'playwright.sync_api',
    'playwright._impl._driver',
    'playwright._impl._transport',
    'playwright._impl._connection',
    'playwright._impl._browser_type',
    'racebox_downloader',
]

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ['main.py'],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthooks/pyi_rth_path.py'],
    excludes=[
        # Exclude heavy packages we do not need at runtime
        'tkinter',
        'PyQt5', 'PyQt6',
        'PySide2', 'PySide6',
        'wx',
        'IPython',
        'notebook',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OpenLap',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # No terminal window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / 'frontend' / 'icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OpenLap',
)

# ── macOS .app bundle (no-op on Windows) ─────────────────────────────────────
# Uncomment on macOS:
# app = BUNDLE(
#     coll,
#     name='OpenLap.app',
#     icon=None,
#     bundle_identifier='com.openlap.app',
#     info_plist={
#         'NSHighResolutionCapable': True,
#         'CFBundleShortVersionString': '0.1.0',
#     },
# )
