# -*- mode: python ; coding: utf-8 -*-
# OpenLap.spec — PyInstaller build spec
#
# Build:
#   pip install pyinstaller
#   pyinstaller OpenLap.spec
#
# Output: dist/OpenLap/  (onedir, faster startup than onefile)
#
# Requires:
#   - On Windows, each PyInstaller run auto-downloads **latest** FFmpeg (BtbN
#     GitHub release) into third_party/ffmpeg/win64/bin/ via tools/fetch_ffmpeg.py.
#     Skip: set environment variable SKIP_FFMPEG_FETCH=1
#   - Fallback if fetch skipped / failed: ffmpeg.exe next to this spec or PATH (ffmpeg_paths.py)
#   - All Python deps installed in the active environment

import os, sys, shutil, subprocess as _sp
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
            'Move them to ~/.openlap/tracks/ or set ALLOW_BUNDLE_TRACKS=1 to override.'
        )

# ── Auto-fetch FFmpeg (Windows) before bundling ───────────────────────────────
if (
    sys.platform == 'win32'
    and os.environ.get('SKIP_FFMPEG_FETCH', '').strip().lower() not in ('1', 'true', 'yes')
):
    _ff_script = HERE / 'tools' / 'fetch_ffmpeg.py'
    if _ff_script.is_file():
        print('[OpenLap.spec] Downloading latest FFmpeg → third_party/ffmpeg/win64/bin/ …')
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

# ── Locate ffmpeg / ffprobe ───────────────────────────────────────────────────
def _find_bin(name):
    """Find ffmpeg/ffprobe: prefer staged third_party, then spec dir, then PATH."""
    staged = HERE / 'third_party' / 'ffmpeg' / 'win64' / 'bin' / (name + '.exe')
    if staged.is_file():
        return str(staged)
    local = HERE / (name + '.exe')
    if local.is_file():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    return None

FFMPEG_BIN  = _find_bin('ffmpeg')
FFPROBE_BIN = _find_bin('ffprobe')

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

# FFmpeg binaries
for _bin, _name in [(FFMPEG_BIN, 'ffmpeg.exe'), (FFPROBE_BIN, 'ffprobe.exe')]:
    if _bin:
        datas.append((_bin, '.'))

# FFmpeg build info (when staged via tools/fetch_ffmpeg.py)
_ff_info = HERE / 'third_party' / 'ffmpeg' / 'win64' / 'BUILD_INFO.json'
if _ff_info.is_file():
    datas.append((str(_ff_info), 'third_party/ffmpeg/win64'))

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
