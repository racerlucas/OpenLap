"""
Portable / dev paths for OpenLap user-writable state.

- Unfrozen (``python main.py``): ``<repo>/.openlap/`` next to this module (not under ``~``).
- PyInstaller onedir: directory containing ``OpenLap.exe`` — same layout beside the exe.
- Override: set ``OPENLAP_DATA_DIR`` to an absolute path (tests, CI, custom layouts).

Config, scan cache, tracks, logs, map/weather/video caches, bundled-library layout, RaceBox auth,
and Playwright Chromium all live under ``app_data_dir()`` (see ``racebox_auth_file()``,
``library_ffmpeg_dir()``, ``playwright_browsers_dir()``).

On first dev run, if ``<repo>/.openlap/config.json`` is missing but ``~/.openlap/config.json``
exists, it is copied once (plus ``scan_cache.json`` when present) so upgrades from older setups stay smooth.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_legacy_migrated: bool = False
_racebox_auth_migrated: bool = False


def _maybe_migrate_from_home_dot_openlap(dest: Path) -> None:
    """One-time copy from legacy ``~/.openlap`` when project data dir is still empty."""
    global _legacy_migrated
    if _legacy_migrated:
        return
    _legacy_migrated = True
    legacy = Path.home() / ".openlap"
    if not legacy.is_dir():
        return
    try:
        if (dest / "config.json").is_file():
            return
        src_cfg = legacy / "config.json"
        if not src_cfg.is_file():
            return
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_cfg, dest / "config.json")
        sc = legacy / "scan_cache.json"
        if sc.is_file() and not (dest / "scan_cache.json").is_file():
            shutil.copy2(sc, dest / "scan_cache.json")
    except OSError:
        pass


def _maybe_migrate_racebox_auth(dest: Path) -> None:
    """Copy legacy RaceBox Playwright state from %APPDATA%/OpenLap (or ~/OpenLap) once."""
    global _racebox_auth_migrated
    if _racebox_auth_migrated:
        return
    _racebox_auth_migrated = True
    try:
        if dest.is_file():
            return
        candidates: list[Path] = []
        appdata = (os.environ.get("APPDATA") or "").strip()
        if appdata:
            candidates.append(Path(appdata) / "OpenLap" / "racebox_auth.json")
        home_openlap = Path.home() / "OpenLap" / "racebox_auth.json"
        if home_openlap not in candidates:
            candidates.append(home_openlap)
        for src in candidates:
            if not src.is_file():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            return
    except OSError:
        pass


def app_data_dir() -> Path:
    """Root directory for config, caches, tracks, logs."""
    override = (os.environ.get("OPENLAP_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    root = _REPO_ROOT / ".openlap"
    _maybe_migrate_from_home_dot_openlap(root)
    return root


def config_file() -> Path:
    return app_data_dir() / "config.json"


def scan_cache_file() -> Path:
    return app_data_dir() / "scan_cache.json"


def tracks_dir() -> Path:
    return app_data_dir() / "tracks"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def track_maps_dir() -> Path:
    return app_data_dir() / "track_maps"


def weather_cache_file() -> Path:
    return app_data_dir() / "weather_cache.json"


def video_cache_dir() -> Path:
    return app_data_dir() / "video_cache"


def library_ffmpeg_dir() -> Path:
    """Portable layout: ``<app_dir>/Library/ffmpeg`` (sibling of ``OpenLap.exe``)."""
    return app_data_dir() / "Library" / "ffmpeg"


def racebox_auth_file() -> Path:
    """Playwright storage state for racebox.pro login (``RaceBoxSource``)."""
    p = app_data_dir() / "racebox_auth.json"
    _maybe_migrate_racebox_auth(p)
    return p


def playwright_browsers_dir() -> Path:
    """Directory for Playwright browser bundles (Chromium); same layout as upstream ``ms-playwright``."""
    return app_data_dir() / "ms-playwright"
