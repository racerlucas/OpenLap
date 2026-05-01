/**
 * api.js — Bridge between JavaScript and the Python webview_api.WebviewAPI class.
 *
 * All methods return Promises. Python push-events arrive as CustomEvents on window
 * with type 'openlap' and a detail: {type, ...} payload.
 *
 * In dev mode (no pywebview), calls fall through to _mock stubs so the page
 * can still render without crashing.
 *
 * Preview vs export: RPC methods that load telemetry or compute deltas/map tracks
 * must stay aligned with the Python export pipeline (see webview_api.py docstring).
 * This file only transports JSON — no duplicate business logic here.
 */
const API = (() => {
  // ── Raw call ─────────────────────────────────────────────────────────────────
  async function call(method, ...args) {
    if (window.pywebview && window.pywebview.api) {
      return window.pywebview.api[method](...args);
    }
    // Dev-mode mock — returns empty/safe defaults
    console.warn(`[API mock] ${method}`, args);
    return _mock(method, args);
  }

  function _mock(method, args = []) {
    const mocks = {
      get_config:        () => ({ telemetry_path: '', video_path: '', export_path: '',
                                  racebox_path: '', aim_path: '', motec_path: '', gpx_path: '',
                                  offsets: {}, bike_overrides: {}, presets: {},
                                  active_preset: '', session_info: {},
                                  overlay: { is_bike: false, theme: 'Dark', gauges: [] } }),
      scan_sessions:     () => [],
      get_laps:          () => [],
      load_lap_history:  () => [],
      load_preview_history: () => [],
      compute_preview_delta: () => [],
      get_preview_map_tracks: () => ({ lap_lats: [], lap_lons: [], ref_lats: [], ref_lons: [] }),
      save_config:       () => null,
      get_overlay:       () => ({ is_bike: false, theme: 'Dark', gauges: [] }),
      save_overlay:      () => null,
      save_overlay_as:   () => null,
      list_presets:      () => [],
      open_folder_dialog:() => null,
      open_file_dialog:  () => null,
      start_export:      () => null,
      cancel_export:     () => null,
      get_weather:       () => ({ weather: '—', wind: '—' }),
      get_telemetry_tuning: () => ({ g_ema_alpha: 0.10 }),
      get_channel_meta: () => ({}),
      get_editor_catalog: () => ({
        theme_names: ['Dark','Light','Colorful','Monochrome','Minimal'],
        default_theme: 'Dark',
        channel_styles: {},
        channel_labels: {},
        multi_channels: [],
        gauge_colours: [
          '#00d4ff', '#ff6b35', '#a8ff3e', '#ff3ea8',
          '#ffd700', '#3ea8ff', '#ff3e3e', '#3effd7',
          '#c084fc', '#fb923c',
        ],
        channel_defaults: {},
      }),
      edit_session_info:       () => null,
      bulk_rename_track:       () => ({ updated: 0 }),
      get_laps_for_ref_picker:    () => [],
      resolve_preview_reference_lap: () => ({ ok: true, ref_csv_path: '', ref_lap_num: 0, desc: '' }),
      get_track_map_candidates:   () => ({ candidates: [], selected_osm_id: '', auto_osm_id: '', track_key: '' }),
      set_track_map_selection:    () => null,
      get_track_map_geometry:     () => ({ lats: [], lons: [], areas: [] }),
      racebox_login:     () => ({ ok: false, error: 'mock' }),
      check_encoders:    () => ({ version: 'mock', encoders: [
        { name: 'libx264', label: 'H.264 software', available: true },
      ]}),
      resolve_export_encoder: (codec, family) => ({ ok: true, encoder: 'libx264' }),
      get_about_info:    () => ({ version: '0.0.0-mock', python: '3.x.x', config: '.openlap/config.json (mock)' }),
      get_session_meta:  () => ({ track: '', laps: '', best: '', best_secs: null }),
      get_video_server_port:    () => 0,
      get_video_fps:            () => ({ ok: false, error: 'mock' }),
      get_video_probe:          () => ({ ok: false, error: 'mock' }),
      estimate_export_size:     () => ({ ok: false, error: 'mock' }),
      step_video_frame:         () => ({ ok: false, error: 'mock' }),
      decode_video_frame:       () => ({ ok: false, error: 'mock' }),
      open_decode_session:      () => ({ ok: false, error: 'mock' }),
      close_decode_session:     () => ({ ok: true }),
      decode_session_seek:      () => ({ ok: false, error: 'mock' }),
      decode_session_step:      () => ({ ok: false, error: 'mock' }),
      debug_sync_seek:          () => ({ ok: true }),
      save_sessions_cache:      () => null,
      convert_xrk_session:       () => ({ ok: false, error: 'mock' }),
      assign_video:              () => null,
      aim_dll_status:                () => ({ found: false, path: '' }),
      download_aim_dll:              () => null,
      download_racebox_sessions:     () => null,
      cancel_racebox_download:       () => null,
      racebox_playwright_status:     () => ({ playwright: false, chromium: false }),
      install_playwright_chromium:   () => null,
      start_auto_sync:               () => ({ queued: 0, reason: 'mock' }),
      cancel_auto_sync:              () => null,
      auto_split_laps_from_json:     () => ({ processed: [], skipped: [] }),
      auto_split_lap_for_file:       () => ({ processed: [], skipped: [] }),
      launch_manual_lap_split_gui:   (telemetryPath) => ({
        started: true,
        pid: 0,
        telemetry_path: telemetryPath || null,
      }),
      set_lap_tag:                   () => ({ ok: true }),
      import_dropped_paths:          () => ({ ok: false, message: 'mock' }),
      list_track_jsons:              () => [],
    };
    const fn = mocks[method];
    return fn ? fn(...args) : null;
  }

  // ── Event bus (Python → JS push events) ──────────────────────────────────────
  const _handlers = {};

  window.addEventListener('openlap', e => {
    const { type, ...payload } = e.detail || {};
    (_handlers[type] || []).forEach(cb => cb(payload));
    (_handlers['*'] || []).forEach(cb => cb({ type, ...payload }));
  });

  function on(type, cb) {
    if (!_handlers[type]) _handlers[type] = [];
    _handlers[type].push(cb);
    return () => { _handlers[type] = _handlers[type].filter(f => f !== cb); };
  }

  // ── Public API ────────────────────────────────────────────────────────────────
  return {
    on,

    getConfig:         ()              => call('get_config'),
    saveConfig:        (data)          => call('save_config', data),

    openFolderDialog:  ()              => call('open_folder_dialog'),
    openFileDialog:    (filters)       => call('open_file_dialog', filters),

    scanSessions:      (folder)        => call('scan_sessions', folder),
    getLaps:           (csvPath)       => call('get_laps', csvPath),
    loadLapHistory:    (csvPath, lapIdx) => call('load_lap_history', csvPath, lapIdx),
    loadPreviewHistory: (csvPath, lapIdx) => call('load_preview_history', csvPath, lapIdx),
    computePreviewDelta: (csvPath, lapIdx, refCsvPath, refLapNum, refMode = '') =>
      call('compute_preview_delta', csvPath, lapIdx, refCsvPath, refLapNum, refMode),
    getPreviewMapTracks: (csvPath, lapIdx, refCsvPath, refLapNum) =>
      call('get_preview_map_tracks', csvPath, lapIdx, refCsvPath, refLapNum),

    getOverlay:        ()              => call('get_overlay'),
    saveOverlay:       (data)          => call('save_overlay', data),
    saveOverlayAs:     (name, data)    => call('save_overlay_as', name, data),
    listPresets:       ()              => call('list_presets'),

    startExport:       (params)        => call('start_export', params),
    cancelExport:      ()              => call('cancel_export'),

    getWeather:        (lat, lon, dt)  => call('get_weather', lat, lon, dt),
    getTelemetryTuning: ()             => call('get_telemetry_tuning'),
    getChannelMeta:    ()             => call('get_channel_meta'),
    getEditorCatalog:  ()             => call('get_editor_catalog'),
    editSessionInfo:      (path, overrides)    => call('edit_session_info', path, overrides),
    bulkRenameTrack:      (oldName, newName)   => call('bulk_rename_track', oldName, newName),
    getLapsForRefPicker:  (csvPath)            => call('get_laps_for_ref_picker', csvPath),
    resolvePreviewReferenceLap: (csvPath, lapIdx, refMode, refCsvPath, refLapNum) =>
      call('resolve_preview_reference_lap', csvPath, lapIdx, refMode, refCsvPath, refLapNum),
    getTrackMapCandidates: (csvPath)           => call('get_track_map_candidates', csvPath),
    setTrackMapSelection:  (trackKey, osmId)   => call('set_track_map_selection', trackKey, osmId),
    getTrackMapGeometry:   (csvPath, cLat, cLon) => call('get_track_map_geometry', csvPath, cLat, cLon),

    getSessionMeta:    (csvPath)       => call('get_session_meta', csvPath),
    getVideoServerPort:  ()             => call('get_video_server_port'),
    getVideoFps:         (videoPath)    => call('get_video_fps', videoPath),
    getVideoProbe:       (videoPathOrPaths) => call('get_video_probe', videoPathOrPaths),
    estimateExportSize:  (params)       => call('estimate_export_size', params),
    stepVideoFrame:      (videoPath, currentTime, direction) => call('step_video_frame', videoPath, currentTime, direction),
    decodeVideoFrame:    (videoPath, frameIdx, timeSec) => call('decode_video_frame', videoPath, frameIdx, timeSec),
    openDecodeSession:   (videoPath, cacheRadius) => call('open_decode_session', videoPath, cacheRadius),
    closeDecodeSession:  (sessionId) => call('close_decode_session', sessionId),
    decodeSessionSeek:   (sessionId, frameIdx, timeSec) => call('decode_session_seek', sessionId, frameIdx, timeSec),
    decodeSessionStep:   (sessionId, direction) => call('decode_session_step', sessionId, direction),
    debugSyncSeek:       (tag, details) => call('debug_sync_seek', tag, details || {}),
    saveSessionsCache:   (sessions)     => call('save_sessions_cache', sessions),

    raceboxLogin:      (email, password) => call('racebox_login', email, password),
    checkEncoders:     ()              => call('check_encoders'),
    resolveExportEncoder: (codec, family) => call('resolve_export_encoder', codec, family),
    getAboutInfo:      ()              => call('get_about_info'),

    convertXrkSession:        (csvPath)            => call('convert_xrk_session', csvPath),
    assignVideo:              (csvPath, videoPath) => call('assign_video', csvPath, videoPath),
    aimDllStatus:               ()                   => call('aim_dll_status'),
    downloadAimDll:             ()                   => call('download_aim_dll'),
    downloadRaceboxSessions:    ()                   => call('download_racebox_sessions'),
    cancelRaceboxDownload:      ()                   => call('cancel_racebox_download'),
    raceboxPlaywrightStatus:    ()                   => call('racebox_playwright_status'),
    installPlaywrightChromium:  ()                   => call('install_playwright_chromium'),

    startAutoSync:              (sessions, opts = {}) =>
      call('start_auto_sync', sessions, !!opts?.force),
    cancelAutoSync:             ()                   => call('cancel_auto_sync'),
    autoSplitLapsFromJson:      (jsonPath, inputDir, outputDir) => call('auto_split_laps_from_json', jsonPath, inputDir, outputDir),
    autoSplitLapForFile:        (jsonPath, telemetryPath) => call('auto_split_lap_for_file', jsonPath, telemetryPath),
    launchManualLapSplitGui:    (telemetryPath)      => call('launch_manual_lap_split_gui', telemetryPath || null),
    setLapTag:                  (csvPath, lapNum, tag, enabled) => call('set_lap_tag', csvPath, lapNum, tag, enabled),
    importDroppedPaths:         (paths, selectedCsvPath) => call('import_dropped_paths', paths, selectedCsvPath),
    listTrackJsons:             ()                   => call('list_track_jsons'),
  };
})();
