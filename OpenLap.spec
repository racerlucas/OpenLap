# -*- mode: python ; coding: utf-8 -*-
# OpenLap.spec — PyInstaller build spec
#
# Build:
#   pip install pyinstaller
#   python tools/build_windows_portable.py
#   (or: pyinstaller OpenLap.spec  then  python tools/stage_dist_library_ffmpeg.py
#        and  python tools/stage_dist_library_node.py and  python tools/stage_dist_tracks.py)
#
# Output: dist/OpenLap/  (onedir portable folder: OpenLap.exe, _internal/, Library/ffmpeg/, Library/node/, …)
# User data (config, tracks, caches) resolves next to the exe when frozen; dev uses <repo>/.openlap/.
# FFmpeg is NOT packed into _internal/: run stage_dist_library_ffmpeg.py so it lives under Library/ffmpeg/.
# Node node.exe for Canvas export: fetch_node.py → third_party/, then stage_dist_library_node.py → Library/node/.
# Track templates are NOT in datas/: run tools/stage_dist_tracks.py so only README + *.template.json
# sit under ``tracks/`` next to OpenLap.exe (never bundle local real ``*.json`` from the repo).
#
# Requires:
#   - On Windows, each PyInstaller run auto-downloads **latest** FFmpeg (BtbN
#     GitHub release) into third_party/ffmpeg/win64/bin/ via tools/fetch_ffmpeg.py.
#     Skip: set environment variable SKIP_FFMPEG_FETCH=1
#   - Fallback if fetch skipped / failed: ffmpeg.exe next to this spec or PATH (ffmpeg_paths.py)
#   - Node win-x64: tools/fetch_node.py stages node.exe into third_party/node/win64/ when missing
#     (SKIP_NODE_FETCH=1 to skip; copy node.exe manually). Copy to dist via stage_dist_library_node.py.
#   - All Python deps installed in the active environment

import os, sys, subprocess as _sp
from pathlib import Path
import playwright as _pw_mod
import PyInstaller as _pyi

HERE = Path(SPECPATH)

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

    # ── Auto-fetch Node.js win-x64 ``node.exe`` (Canvas overlay export) ─────────
    _node_exe = HERE / 'third_party' / 'node' / 'win64' / 'node.exe'
    _skip_node = os.environ.get('SKIP_NODE_FETCH', '').strip().lower() in ('1', 'true', 'yes')
    _force_node = os.environ.get('FORCE_NODE_FETCH', '').strip().lower() in ('1', 'true', 'yes')
    if not _skip_node and (not _node_exe.is_file() or _force_node):
        _node_script = HERE / 'tools' / 'fetch_node.py'
        if _node_script.is_file():
            if _force_node and _node_exe.is_file():
                try:
                    _node_exe.unlink()
                except OSError:
                    pass
            why = 'forced' if _force_node else 'missing staged node.exe'
            print(f'[OpenLap.spec] Node.js win-x64 ({why}) → third_party/node/win64/ …')
            _prn = _sp.run([sys.executable, str(_node_script)], cwd=str(HERE))
            if _prn.returncode != 0:
                raise RuntimeError(
                    '[OpenLap.spec] Node fetch failed (exit %s). '
                    'Check network, or set SKIP_NODE_FETCH=1 and copy node.exe into third_party/node/win64/.'
                    % _prn.returncode
                )
        else:
            print('[OpenLap.spec] warning: tools/fetch_node.py missing — Canvas export needs node on PATH or manual third_party/node/')

# ── Data files ────────────────────────────────────────────────────────────────
datas = [
    # Frontend (HTML/CSS/JS)
    (str(HERE / 'frontend'), 'frontend'),
    # Headless Canvas overlay export; ``npm install`` in canvas_export at **build** time;
    # portable runtime uses ``Library/node/node.exe`` (see tools/stage_dist_library_node.py).
    (str(HERE / 'canvas_export'), 'canvas_export'),
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
    'node_paths',
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
    # PyInstaller 6+ defaults to "_internal". Make it look like conventional apps.
    # Keep backward-compat for older versions that don't support this kwarg.
    **({'contents_directory': 'bin'} if tuple(int(x) for x in _pyi.__version__.split('.')[:2]) >= (6, 0) else {}),
    name='OpenLap',
)

# ── macOS .app bundle ───────────────────────────────────────────────────────
# On macOS we ship an .app (zip it for Releases).
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='OpenLap.app',
        icon=None,  # No .icns in repo yet; keep default to avoid build failures
        bundle_identifier='com.openlap.app',
        info_plist={
            'NSHighResolutionCapable': True,
        },
    )
