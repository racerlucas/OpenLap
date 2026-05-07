# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the app
python main.py

# Python tests (209 passing)
python -m pytest tests/ -q
python -m pytest tests/test_racebox_data.py -q          # single file
python -m pytest tests/ -k "test_delta" -q              # single test by name

# JS tests (frontend/tests/, run with Vitest + jsdom; 39 passing)
npm run test:run           # one-shot
npm test                   # watch mode

# Video overlay export (Canvas only — same gauge sources as the editor; no Matplotlib fallback)
cd canvas_export && npm install   # once: @napi-rs/canvas + native binary
python tools/bundle_canvas_gauges.py   # regenerate gauge_bundle.cjs after editing frontend/js/gauges/*.js
npm run build:canvas-bundle            # same (from repo root package.json)
# Portable Windows build ships ``Library/node/node.exe`` (see ``tools/fetch_node.py`` / ``node_paths.py``).
# Dev: ``python tools/fetch_node.py`` or ``node`` on PATH; plus the files above — otherwise ``assert_canvas_export_ready()`` fails.

# CI: scheduled FFmpeg check → bump patch version + Windows portable zip release
#   (.github/workflows/release-on-ffmpeg-update.yml, pin in .github/ffmpeg-release-pin.txt)

# Build Windows portable folder (onedir → dist/OpenLap/, not a single onefile exe)
pip install pyinstaller
# ``OpenLap.spec`` (on Windows) auto-runs ``tools/fetch_ffmpeg.py --latest`` before Analysis —
# downloads BtbN win64 GPL zip into third_party/ffmpeg/win64/bin/.
# Offline / no network: ``set SKIP_FFMPEG_FETCH=1`` then place ffmpeg.exe / ffprobe.exe there (or PATH).
# Manual fetch: ``python tools/fetch_ffmpeg.py`` (latest) or ``python tools/fetch_ffmpeg.py --stable`` (Gyan).
python tools/build_windows_portable.py
# (Equivalent: ``pyinstaller OpenLap.spec --clean -y`` then ``python tools/stage_dist_library_ffmpeg.py``.)
# Repo on UNC/SMB: ``build_windows_portable.py`` defaults ``--distpath`` / ``--workpath`` under ``%LOCALAPPDATA%``
# so ``--clean`` can delete the old onedir reliably; or pass e.g. ``--distpath C:\tmp\ol-dist --workpath C:\tmp\ol-build``.
# ``--keep-dist`` then copies the local onedir to ``dist/OpenLap/`` on the share after zipping.
# Dev: config/scan cache/tracks/caches under ``<repo>/.openlap/`` (gitignored). Frozen: next to OpenLap.exe.
# FFmpeg/ffprobe are copied to dist/OpenLap/Library/ffmpeg/ at build time (not inside _internal/).
```

## Architecture

OpenLap is a **PyWebView desktop app**: Python is the backend, a vanilla-JS/HTML Canvas frontend is the UI. There is no web server — pywebview loads `frontend/index.html` directly as a local file.

### Frontend ↔ Backend communication

Two channels:

1. **JS → Python (RPC):** `await window.pywebview.api.method_name(args)` — every public method on `WebviewAPI` (in `webview_api.py`) is callable from JS. Return values must be JSON-serialisable.
2. **Python → JS (push events):** `WebviewAPI._push(event_type, **payload)` calls `window.evaluate_js(...)` to fire a `CustomEvent('openlap', {detail})` that JS listens for with `window.addEventListener('openlap', ...)`.

Video playback uses a third channel: a local HTTP server (`_VideoFileHandler` in `webview_api.py`) on a random port that serves arbitrary local files with HTTP range support. JS gets the port via `get_video_server_port()` and builds URLs like `http://127.0.0.1:{port}/?f={encodedPath}`.

### Preview vs export (WYSIWYG goal)

| Stack | Location | Role |
|---|---|---|
| JS Canvas (pywebview / Chromium) | `frontend/js/gauges/*.js` | Overlay **editor preview** |
| JS Canvas (Node / Skia) | `canvas_export/gauge_bundle.cjs` + `@napi-rs/canvas` | **Video overlay export only path** — bundle is built from the same gauge sources as preview; `overlay_paint_plan.py` supplies layer payloads (`build_canvas_paint_plan`) aligned with `iter_overlay_layers` / preview data keys |

**Unify (numbers and data contract):** same `Session` / `DataPoint`, `build_history_row` / `gauge_channels`, delta/map in `telemetry_algorithms.py`, and the same preview RPC helpers (`load_preview_history`, `compute_preview_delta`, `get_preview_map_tracks`) vs export frame sampling in `video_renderer.py`. Do not let preview and export diverge on computed values.

**Separate (drawing):** preview Chromium vs export Skia — same JS gauge logic and layout inputs, but not guaranteed pixel-identical (AA, font rasterisation). Do not chase pixel-perfect parity between the two Canvas backends unless there is a product reason.

**Portable / PyInstaller:** `OpenLap.spec` includes the `canvas_export/` tree under the bundle. Run `npm install` inside `canvas_export/` **on the build machine** before building so `node_modules/@napi-rs/canvas` is present. The shipped app uses **`Library/node/node.exe`** next to `OpenLap.exe` (staged by `tools/stage_dist_library_node.py` after `tools/fetch_node.py`); no host Node install is required. Dev / macOS can still use `node` on PATH or set `OPENLAP_NODE`. There is no silent fallback if Node or deps are missing.

**Matplotlib styles (`styles/*.py`):** still used by `overlay_worker.render_overlay_matplotlib` (style plugins, tooling). **Video export does not** rasterise overlays through `style_registry` — it always goes through `overlay_dispatch.render_frame_worker` → `overlay_canvas_worker`.

### Python style plugins

Each `.py` in `styles/` must export:
- `STYLE_NAME: str` — display name
- `ELEMENT_TYPE: str` — `"gauge"` or `"map"`
- `render(data, w, h) -> np.ndarray` — returns RGBA array shape `(h, w, 4)`

`style_registry.py` auto-discovers plugins at runtime. `render()` receives a `data` dict with `_tc` (theme colour tokens, injected by `style_registry.render_style`) and `theme` (theme name string).

### Data model

All four loaders (racebox, aim, gpx, motec) return the same types from `racebox_data.py`: `Session`, `Lap`, `DataPoint`. Never add source-specific fields to `DataPoint`. `session_scanner.py` drives scanning across all configured folders and persists scan cache under the app data dir (``<repo>/.openlap/scan_cache.json`` in dev).

### Config

`AppConfig` dataclass persisted to ``<repo>/.openlap/config.json`` when running from source (see `openlap_paths.app_data_dir`). Overlay layout is nested as `OverlayLayout` (with `gauges: List[dict]`). Named presets live in `AppConfig.presets` (name → serialized `OverlayLayout` dict). On load, if `active_preset` is set the overlay is always rebuilt from the preset — unsaved edits to the live layout are discarded on restart.

### Portable paths (all local / app-data-root)

Everything the app writes (config, scan cache, tracks, caches, RaceBox login, joined-video temp files, FFmpeg library layout when staged, Playwright Chromium) resolves under **`openlap_paths.app_data_dir()`**: unfrozen ``<repo>/.openlap/``, frozen **`dirname(OpenLap.exe)`**, optional **`OPENLAP_DATA_DIR`** absolute override — see README “便携与本地数据目录”. No telemetry blobs are uploaded; user-chosen telemetry/video/export folders from Settings remain separate arbitrary local paths.

- ``main.py`` uses ``os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(playwright_browsers_dir()))`` so installs land in ``<app_data>/ms-playwright/`` instead of `%LOCALAPPDATA%`; `OpenLap.spec` rt hook does the equivalent for bundles (`<exe_dir>/ms-playwright`).
- One-off migration from legacy ``~/.openlap`` (config/scan cache) and ``%APPDATA%/OpenLap/racebox_auth.json`` → `openlap_paths`.

Sync offsets are stored in three fields: `offsets` (csv_path → float), `offset_sources` (csv_path → `'user'`|`'auto'`), and `auto_sync_failed` (list of csv_paths where auto-sync was tried but confidence was too low).

### Auto-sync pipeline

`auto_sync.py` detects the video-telemetry sync offset automatically using cross-correlation of video motion signal vs telemetry G-force magnitude. Runs as a background thread in `WebviewAPI._run_auto_sync_bg()` after each scan (opt-in via `auto_sync_enabled`). Key parameters: 5 fps decode, 320px wide frames, ±120s search window, confidence threshold 6× (need ≥3× to write). Uses `CREATE_NO_WINDOW` on Windows so no terminal flashes appear. Export cancels any running auto-sync via `_auto_sync_cancel` event. Auto results (`source='auto'`) are never written over a user-confirmed offset (`source='user'`).

### Video export pipeline

`export_runner.py` → `video_renderer.render_lap()` → multiprocessing pool: each worker runs `overlay_dispatch.render_frame_worker` → `overlay_canvas_worker.render_overlay_canvas` (long-lived Node subprocess per worker, stdin/stdout JSON plan + raw RGBA). `render_lap` calls `assert_canvas_export_ready()` up front so missing Node/bundle/deps fail immediately. Requires `freeze_support()` on Windows (called in `main.py`).

**FFmpeg CLI:** `ffmpeg_paths.py` resolves `ffmpeg` / `ffprobe` (staged under `third_party/ffmpeg/`, PyInstaller `_MEIPASS`, repo root exes, then PATH). `utils._run` / `_popen` rewrite bare `ffmpeg`/`ffprobe` argv0 so export, auto-sync, and ffprobe metadata use the same resolution without relying on a global install.

### Frontend structure

`frontend/js/`:
- `api.js` — thin wrappers around `window.pywebview.api` calls
- `state.js` — client-side app state
- `router.js` — SPA page switching
- `pages/` — per-tab logic (Data, Overlay, Export, Settings)
- `gauges/` — Canvas gauge renderers; `base.js` has shared utilities (`drawBackground`, `scaleFont`, `fmtValue`, theme definitions)

