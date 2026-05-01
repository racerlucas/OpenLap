# Contributing to OpenLap

Thanks for your interest. OpenLap is a PyWebView desktop app — Python backend, vanilla-JS/HTML Canvas frontend. Read `CLAUDE.md` first for the full architecture overview.

## Getting started

```bash
git clone https://github.com/LaurensVR3/OpenLap.git
cd OpenLap
pip install -e ".[dev]"
python main.py
```

FFmpeg must be on your PATH. On Windows: `winget install Gyan.FFmpeg`.

## Running tests

```bash
# Python (209 tests)
python -m pytest tests/ -q

# JavaScript (39 tests, requires Node)
npm install
npm run test:run
```

All tests must pass before opening a PR. If you're adding a feature, add a test.

## Project layout

```
main.py               entry point; sets default PLAYWRIGHT_BROWSERS_PATH under openlap_paths
webview_api.py        every public method here is callable from JS
auto_sync.py          background video-telemetry sync detection (cross-correlation)
app_config.py         AppConfig dataclass — persisted under app data dir (see openlap_paths)
openlap_paths.py      single source of truth for portable app data roots (frozen vs dev, migrations)
frontend/             vanilla JS + HTML, no build step
  js/pages/           one file per tab (data, editor, export, settings)
  js/gauges/          JS canvas renderers — one per gauge style
styles/               Python/matplotlib renderers — one per gauge style
tests/                pytest (Python) + Vitest (JS in frontend/tests/)
```

Preview (JS Canvas) and export (matplotlib) are **separate presentation stacks**. They should agree on **what data** each gauge reads (`gauge_channels`, `build_history_row`, `telemetry_algorithms`) — not on identical pixels. When you add a style, add the export plugin; add or adjust the Canvas renderer only if the editor needs to preview it.

## Adding a gauge style

1. Copy `styles/gauge_numeric.py` → `styles/gauge_myname.py`. Set `STYLE_NAME` and implement `render(data, w, h) -> np.ndarray` (export path).
2. Optionally copy `frontend/js/gauges/numeric.js` → `frontend/js/gauges/myname.js` for editor preview; wire it in `frontend/js/pages/editor.js` (`GAUGE_RENDERERS`).
3. The Python plugin is auto-discovered by `style_registry.py` — no registration needed.
4. Ensure any new **history keys** / `gauge_data` fields are documented and match what `load_preview_history` / `build_history_row` emit.

## Adding a telemetry channel

Channels are defined in `data_model.py` (`DataPoint` dataclass). All four loaders must populate the same fields — never add source-specific fields to `DataPoint`.

## Good first issues

- **Add a test** for an untested module (`webview_api.py`, `style_registry.py`, any gauge style)
- **Improve an error message** — search for `console.warn` or `logger.exception` and see if the user-facing message could be clearer
- **Add a gauge style** — see above
- **Improve GPX lap detection** — currently the whole GPX track is treated as one lap; real lap detection using start/finish line crossing would make GPX useful for track days

## Pull requests

- Keep PRs focused — one feature or fix per PR
- Match the existing code style (no linter enforced, just be consistent)
- Update `README.md` if you add user-visible functionality
- If your change touches the overlay editor or export pipeline, test an actual export on a real session before opening the PR
