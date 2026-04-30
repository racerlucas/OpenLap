/**
 * Test helpers for loading IIFE-style frontend modules into the jsdom global scope.
 *
 * All page modules are browser IIFEs that reference State, API, Router as free
 * variables and self-register via Router.register().  We simulate that by:
 *   1. Setting the mocks on globalThis before loading.
 *   2. Executing the file body via new Function so free variable lookups hit globalThis.
 *   3. Extracting the registered page from the Router mock.
 */
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const JS_ROOT   = resolve(__dirname, '../js');

// ── Module loaders ────────────────────────────────────────────────────────────

/**
 * Load state.js and assign a fresh State instance to globalThis.State.
 * Call this in beforeEach so each test gets isolated reactive state.
 */
export function loadState() {
  const code = readFileSync(resolve(JS_ROOT, 'state.js'), 'utf8');
  // state.js uses `const State = (() => {...})()`.  Running it inside a
  // Function body makes `State` a local; we return it and pin to globalThis.
  globalThis.State = new Function(`${code}; return State;`)();
}

/**
 * Load a page IIFE (e.g. 'pages/export.js').
 * Expects globalThis.State / API / Router to already be set.
 * The IIFE self-registers with the Router mock via Router.register().
 */
export function loadPage(relPath) {
  const code = readFileSync(resolve(JS_ROOT, relPath), 'utf8');
  new Function(code)();
}

// ── Mock factories ─────────────────────────────────────────────────────────────

/**
 * Create a Router mock that captures registered pages.
 * Assign to globalThis.Router before calling loadPage().
 */
export function makeRouter() {
  const pages = {};
  return {
    register: (name, mod) => { pages[name] = mod; },
    navigate:  vi.fn(),
    getPage:   (name) => pages[name],
  };
}

/**
 * Create an API mock with safe async defaults.
 * Pass `overrides` to replace specific methods per test.
 */
export function makeAPI(overrides = {}) {
  return {
    on:                vi.fn(() => () => {}),
    getConfig:         vi.fn(async () => ({
      export_path: '',
      encoder: 'libx264',
      export_video_codec: 'h264',
      export_encoder_family: 'auto',
      crf: 18,
      workers: 4,
      export_container_choice: 'match_source',
      export_rate_mode: 'cq',
      export_video_bitrate_kbps: 0,
      export_video_max_bitrate_kbps: 0,
      export_audio_bitrate_kbps: 0,
      export_max_quality: false,
      export_target_res: 'auto',
      export_target_fps: 'auto',
      export_scope: 'full',
      export_padding: 5,
      export_clip_start_s: 0,
      export_clip_end_s: 0,
      export_overlay_only: false,
      export_lap_range_start: 1,
      export_lap_range_end: null,
      all_telemetry_paths: [],
      offsets: {},
      bike_overrides: {},
      overlay: { is_bike: false, theme: 'Dark', gauges: [] },
    })),
    getLaps:           vi.fn(async () => []),
    scanSessions:      vi.fn(async () => []),
    saveConfig:        vi.fn(async () => null),
    checkEncoders:     vi.fn(async () => ({ version: 'test', encoders: [] })),
    resolveExportEncoder: vi.fn(async () => ({ ok: true, encoder: 'libx264' })),
    startExport:       vi.fn(async () => null),
    cancelExport:      vi.fn(async () => null),
    getVideoProbe:     vi.fn(async () => ({
      ok: true, width: 1920, height: 1080, fps: 60, duration: 120, extension: '.mp4', has_audio: true,
    })),
    estimateExportSize: vi.fn(async () => ({
      ok: true,
      skipped: false,
      estimated_bytes: 12_500_000,
      total_bytes: 12_500_000,
      total_seconds: 60,
      segment_lengths_s: [60],
      output_extension: '.mp4',
      container_extension: '.mp4',
    })),
    openFolderDialog:  vi.fn(async () => null),
    getSessionMeta:    vi.fn(async () => ({ track: '', laps: '', best: '', best_secs: null })),
    getVideoServerPort: vi.fn(async () => 0),
    saveSessionsCache:  vi.fn(async () => null),
    ...overrides,
  };
}

// ── DOM helpers ───────────────────────────────────────────────────────────────

/** Create and attach a container div; pass it to page.mount(). */
export function makeContainer() {
  const div = document.createElement('div');
  document.body.appendChild(div);
  return div;
}

/** Remove a container from the DOM after a test. */
export function cleanupContainer(div) {
  if (div?.parentNode) div.parentNode.removeChild(div);
}

/** Flush all pending microtasks and a single macrotask tick. */
export function flushAsync() {
  return new Promise(r => setTimeout(r, 0));
}
