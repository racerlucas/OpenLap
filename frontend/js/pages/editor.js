/**
 * editor.js — Overlay Editor page.
 *
 * Layout / drag / RAF / video UI are **frontend-only**. Anything that affects whether
 * preview **numbers** match export must come from Python: ``get_channel_meta``,
 * ``get_editor_catalog``, ``load_preview_history``, ``compute_preview_delta``,
 * ``get_preview_map_tracks`` — same pipeline as ``video_renderer`` / ``telemetry_algorithms``.
 *
 * Canvas gauge modules are **presentation**; they consume the same ``data`` keys as
 * export styles where applicable, but need not match matplotlib pixel-for-pixel.
 */
(function () {
  /** Must match ``app_config.DEFAULT_OVERLAY_REF_MODE`` — used when layout omits ``ref_mode``. */
  const DEFAULT_REF_MODE = 'session_best_so_far';
  // ── Registry: maps style name → render function ────────────────────────────
  const GAUGE_RENDERERS = {
    'Numeric':    (ctx, d, w, h) => GaugeNumeric.render(ctx, d, w, h),
    'Info':       (ctx, d, w, h) => GaugeInfo.render(ctx, d, w, h),
    'Scoreboard': (ctx, d, w, h) => GaugeScoreboard.render(ctx, d, w, h),
    'Bar':        (ctx, d, w, h) => GaugeBar.render(ctx, d, w, h),
    'Line':       (ctx, d, w, h) => GaugeLine.render(ctx, d, w, h),
    'Delta':      (ctx, d, w, h) => GaugeDelta.render(ctx, d, w, h),
    'Delta Bar':  (ctx, d, w, h) => GaugeDeltaBar.render(ctx, d, w, h),
    'Compare':    (ctx, d, w, h) => GaugeCompare.render(ctx, d, w, h),
    'Multi-Line': (ctx, d, w, h) => GaugeMultiline.render(ctx, d, w, h),
    'Splits':     (ctx, d, w, h) => GaugeSplits.render(ctx, d, w, h),
    'Sector Bar': (ctx, d, w, h) => GaugeSectorBar.render(ctx, d, w, h),
    'Dial':       (ctx, d, w, h) => GaugeDial.render(ctx, d, w, h),
    'G-Meter':    (ctx, d, w, h) => GaugeGmeter.render(ctx, d, w, h),
    'Lean':       (ctx, d, w, h) => GaugeLean.render(ctx, d, w, h),
    'Circuit':    (ctx, d, w, h) => GaugeMap.render(ctx, d, w, h),
    'Zoomed':     (ctx, d, w, h) => GaugeMap.renderZoomed(ctx, d, w, h),
    'Image':      (ctx, d, w, h) => GaugeImage.render(ctx, d, w, h),
  };

  // Bootstrap map; replaced on load by ``get_editor_catalog`` from ``gauge_channels``.
  let _CHANNEL_STYLES = {
    speed:       ['Dial', 'Bar', 'Numeric', 'Line', 'Compare'],
    rpm:         ['Numeric', 'Bar', 'Dial', 'Line'],
    exhaust_temp:['Numeric', 'Bar', 'Line'],
    gforce_lon:  ['Bar', 'Dial', 'Numeric', 'Line', 'Compare'],
    gforce_lat:  ['Bar', 'Dial', 'Numeric', 'Line', 'Compare'],
    g_meter:     ['G-Meter'],
    lean:        ['Lean', 'Bar', 'Dial', 'Line', 'Numeric'],
    altitude:    ['Line', 'Bar', 'Numeric'],
    lap_time:    ['Numeric', 'Splits', 'Sector Bar', 'Line', 'Compare', 'Bar'],
    delta_time:  ['Delta', 'Delta Bar', 'Numeric', 'Line', 'Compare'],
    map:         ['Circuit', 'Zoomed'],
    info:        ['Info'],
    lap_info:    ['Scoreboard'],
    multi:       ['Multi-Line'],
    image:       ['Image'],
  };
  let _themeNames = ['Dark', 'Light', 'Colorful', 'Monochrome', 'Minimal'];
  let _channelDefaultsMap = {
    info: { selected_fields: ['track','datetime','vehicle','weather','wind'], info_overrides: {}, text_align: 'left' },
    lap_info: { selected_fields: ['lap','best','current','delta'], text_align: 'split', best_mode: 'so_far' },
    multi: { multi_channels: ['speed', 'gforce_lat'] },
    image: { image_path: '', opacity: 1.0, fit: 'contain' },
    map: { zoom_radius_m: 150, show_ref: true, map_rotate_deg: 0, map_mirror_x: false, map_mirror_y: false },
  };

  let _ALL_CHANNELS = [
    { value: 'speed',       label: '速度' },
    { value: 'rpm',         label: 'RPM' },
    { value: 'exhaust_temp',label: '排气温度' },
    { value: 'gforce_lon',  label: '纵向 G' },
    { value: 'gforce_lat',  label: '横向 G' },
    { value: 'g_meter',     label: 'G 仪表' },
    { value: 'lean',        label: '倾角' },
    { value: 'altitude',    label: '海拔' },
    { value: 'lap_time',    label: '圈速' },
    { value: 'delta_time',  label: '实时秒差' },
    { value: 'map',         label: '地图' },
    { value: 'info',        label: '本节信息' },
    { value: 'lap_info',    label: '单圈信息' },
    { value: 'multi',       label: '多曲线' },
    { value: 'image',       label: '图片 / Logo' },
  ];

  // Channels that can appear inside a Multi-Line gauge
  let _MULTI_CHANNEL_OPTS = [
    { value: 'speed',        label: '速度' },
    { value: 'rpm',          label: 'RPM' },
    { value: 'exhaust_temp', label: '排气温度' },
    { value: 'gforce_lon',   label: '纵向 G' },
    { value: 'gforce_lat',   label: '横向 G' },
    { value: 'lean',         label: '倾角' },
    { value: 'altitude',     label: '海拔' },
    { value: 'lap_time',     label: '圈速' },
    { value: 'delta_time',   label: '实时秒差' },
  ];

  /** Multi-line / UI swatches — filled from ``get_editor_catalog().gauge_colours`` (``gauge_channels.GAUGE_COLOURS``). */
  let _gaugeColours = [];

  function _gaugeColourAt(i) {
    const arr = _gaugeColours.length ? _gaugeColours : ['#888888'];
    const n = arr.length;
    return arr[((Math.floor(i) % n) + n) % n];
  }

  // ── Utilities ─────────────────────────────────────────────────────────────
  function _esc(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _csvKeyVariants(p) {
    const raw = String(p || '');
    if (!raw) return [];
    const out = [raw];
    if (raw.includes('\\')) out.push(raw.replace(/\\/g, '/'));
    if (raw.includes('/')) out.push(raw.replace(/\//g, '\\'));
    const seen = new Set();
    return out.filter(x => (x && !seen.has(x) && (seen.add(x), true)));
  }

  function _savedOffsetFromConfig(cfg, csvPath) {
    const offsets = cfg?.offsets;
    if (!offsets || typeof offsets !== 'object') return null;
    for (const k of _csvKeyVariants(csvPath)) {
      const n = Number(offsets[k]);
      if (Number.isFinite(n)) return n;
    }
    return null;
  }

  // ── State ──────────────────────────────────────────────────────────────────
  let _layout    = null;   // {is_bike, theme, gauges:[...]}
  let _presets   = [];
  let _container = null;
  let _selected  = null;   // index of selected gauge
  let _drag      = null;   // {type:'move'|'resize', gaugeIdx, startMx, startMy, startG}
  let _animFrame  = null;

  // Live preview state
  let _liveSession     = null;  // {csv_path, lap_idx, video_paths, sync_offset, csv_start}
  let _liveSessionMeta = null;  // {track, laps, best, best_secs} from getSessionMeta
  let _liveLaps        = null;  // [{lap_idx, duration, is_best, elapsed_start}] from getLaps
  /** Top #lap-sel dropdown only: highlight + prev/next + export staging label — not for gauge math. */
  let _selLapIdx       = 0;
  /** Lap index last passed to ``loadPreviewHistory`` / aligned RPC + video ``telT``; set in ``_loadLapData`` only. */
  let _previewDataLapIdx = 0;
  let _livePoints      = null;  // [{t, speed, gx, gy, rpm, alt, lat, lon, lean}, ...]
  let _liveLats        = null;  // pre-extracted lat array for map gauge
  let _liveLons        = null;
  let _mapRefLats      = null;  // preprocessed ref map track (export parity)
  let _mapRefLons      = null;
  let _liveOffset      = 0;     // sync_offset: video_time - offset = lap_elapsed_time
  let _liveFrameIdx    = 0;
  let _livePort        = 0;
  let _liveRafId       = null;
  let _liveCumDist     = null;  // cumulative distance array for current lap points
  // In-memory cache for preview telemetry points per lapIdx to avoid reloading
  // on every lap jump (switchLap always calls _loadLapData).
  // lapIdx -> [{t, speed, gx, gy, rpm, alt, lat, lon, lean}, ...]
  let _lapHistoryCache = new Map();
  let _refLapPoints    = null;  // cached reference lap telemetry points
  let _refCumDist      = null;  // cumulative distance for reference lap
  let _refKey          = '';    // cache key: csv|lap_num|mode
  let _refLapCsvPath   = '';
  let _refLapNum       = 0;
  let _liveDeltaNow    = null;  // realtime delta at current frame (seconds)
  let _deltaHistory    = [];    // sparkline values for Delta gauge
  let _lastRefNearestIdx = null; // cached nearest ref index for delta
  let _mountGen        = 0;     // incremented on unmount; guards stale async continuations
  let _resizeObserver  = null;
  let _trackMapGeometry = null; // {lats, lons} from OSM — loaded async on session change
  let _liveSpeedMax = 300;      // preview speed scale aligned with export rule
  /** Smoothed GPS for map dot (reduces vertex-snapping jitter vs track polyline). */
  let _mapSmoothedDot = null;
  let _mapDotSessionKey = '';

  /** Presets / bad saves may leave `gauges` null or non-array — normalise before any .length / .forEach. */
  function _ensureLayoutGauges() {
    if (_layout && !Array.isArray(_layout.gauges)) _layout.gauges = [];
  }

  // Frontend stays display-only: semantic telemetry processing is backend-owned.

  // Constants (normalised)
  const MIN_NORM        = 0.04;
  const SNAP_NORM       = 0.02;   // edge-snap threshold
  const SNAP_ELEM_NORM  = 0.015;  // element-to-element snap threshold
  const SNAP_SIZE_STEP  = 0.05;   // size grid step for resize snap
  const HANDLE_NORM     = 0.012;  // resize handle size as fraction of preview width

  function _deltaBarOpts(g) {
    const defFs = 1.0;
    const defEx = 0.45;
    if (!g || g.channel !== 'delta_time' || g.style !== 'Delta Bar') {
      return { fullScale: defFs, curve: defEx };
    }
    const fs = Number(g.delta_bar_full_scale);
    const ex = Number(g.delta_bar_curve);
    return {
      fullScale: Number.isFinite(fs) ? fs : defFs,
      curve: Number.isFinite(ex) ? ex : defEx,
    };
  }

  function _applyDeltaBarOptsToData(g, d) {
    if (!g || !d || g.channel !== 'delta_time' || g.style !== 'Delta Bar') return d;
    const o = _deltaBarOpts(g);
    d.delta_bar_full_scale = o.fullScale;
    d.delta_bar_curve = o.curve;
    return d;
  }

  // ── Dummy data ─────────────────────────────────────────────────────────────
  function dummyData(channel, style, theme, gauge = null) {
    const base = { theme };
    switch (channel) {
      case 'info': {
        const ov = gauge?.info_overrides || {};
        const defs = _channelDefaults('info');
        return {
          ...base,
          info_track:   ov.track    || 'Spa-Francorchamps',
          info_date:    ov.date     || '2024-06-15',
          info_time:    ov.time     || '14:32',
          info_vehicle: ov.vehicle  || 'Porsche 992 GT3 R',
          info_session: ov.session  || 'Practice',
          info_weather: ov.weather  || '22°C  Partly cloudy',
          info_wind:    ov.wind     || 'NW  8 km/h',
          selected_fields: gauge?.selected_fields || defs.selected_fields || [],
        };
      }
      case 'lap_info': {
        const defs = _channelDefaults('lap_info');
        return {
          ...base, lap_num: 3, total_laps: 8,
          lap_elapsed: 45.234, best_so_far: 83.456, delta_time: -0.234,
          session_best: 82.901,
          best_mode: gauge?.best_mode || defs.best_mode || 'so_far',
          selected_fields: gauge?.selected_fields || defs.selected_fields || [],
        };
      }
      case 'image': return {
        ...base,
        image_path: gauge?.image_path || '',
        image_url:  '',   // no live URL in dummy mode — placeholder shown
        opacity:    gauge?.opacity ?? 1.0,
        fit:        gauge?.fit     || 'contain',
      };
      case 'map': return {
        ...base,
        lats: [], lons: [], cur_idx: 0,
        zoom_radius_m: gauge?.zoom_radius_m ?? 150,
        show_ref: gauge?.show_ref !== false,
        map_rotate_deg: gauge?.map_rotate_deg ?? 0,
        map_mirror_x: gauge?.map_mirror_x === true,
        map_mirror_y: gauge?.map_mirror_y === true,
        ref_lats: [], ref_lons: [],
      };
      case 'multi': {
        const defs = _channelDefaults('multi');
        const mh = (amp, off) => Array.from({length:40}, (_,i)=>amp*Math.sin(i*0.25+off)+off);
        const keys = (gauge?.multi_channels && gauge.multi_channels.length)
          ? gauge.multi_channels
          : (defs.multi_channels || []);
        const multi_channels = keys.map((ch, ci) => {
          const m = _LIVE_FIELDS[ch] || { label: ch, unit: '', min: 0, max: 100, sym: false, key: ch };
          const amp = (m.max - m.min) * 0.35;
          const off = (m.max + m.min) / 2;
          return {
            channel: ch, label: m.label, unit: m.unit,
            values: mh(amp, off), value: off + amp * 0.5,
            min_val: m.min, max_val: m.max, symmetric: m.sym, color_idx: ci,
          };
        });
        return { ...base, multi_channels };
      }
      default: {
        const meta = {
          speed:       {label:'Speed',     unit:'km/h', min:0,   max:250, sym:false, val:185},
          rpm:         {label:'RPM',       unit:'rpm',  min:0,   max:14000, sym:false, val:7200},
          exhaust_temp:{label:'Exh Temp',  unit:'°C',   min:0,   max:900, sym:false, val:650},
          gforce_total:{label:'Total G',   unit:'G',    min:0,   max:3,   sym:false, val:1.8},
          gforce_lon:  {label:'Long G',    unit:'G',    min:-3,  max:3,   sym:true,  val:-1.2},
          gforce_lat:  {label:'Lat G',     unit:'G',    min:-3,  max:3,   sym:true,  val:2.1},
          g_meter:     {label:'G-Meter',   unit:'G',    min:-3,  max:3,   sym:true,  val:1.5},
          lean:        {label:'Lean',      unit:'°',    min:-60, max:60,  sym:true,  val:-35},
          altitude:    {label:'Altitude',  unit:'m',    min:0,   max:1000, sym:false, val:220},
          lap_time:    {label:'Lap Time',  unit:'',     min:0,   max:120, sym:false, val:84.5},
          delta_time:  {label:'Delta',     unit:'s',    min:-30, max:30,  sym:true,  val:-0.234},
        }[channel] || {label:'Value', unit:'', min:0, max:100, sym:false, val:42};

        const hist = Array.from({length:40}, (_,i) => {
          const t = i * 0.1;
          return meta.min + (meta.max - meta.min) * (0.35 + 0.25 * Math.sin(t * 1.3) + 0.10 * Math.sin(t * 3.1));
        });
        const d = {
          ...base,
          value: meta.val, history_vals: hist, ref_history_vals: [],
          label: meta.label, unit: meta.unit,
          min_val: meta.min, max_val: meta.max,
          symmetric: meta.sym, channel,
          sectors: style === 'Splits' || style === 'Sector Bar' ? [
            {num:1, ref_t:24.5, cur_t:24.3, delta:-0.20, done:true, boundary_elapsed:24.3},
            {num:2, ref_t:23.1, cur_t:24.4, delta:1.30,  done:true, boundary_elapsed:48.7},
            {num:3, ref_t:25.8, cur_t:null, delta:null,  done:false, boundary_elapsed:Infinity},
          ] : [],
        };
        if (channel === 'g_meter') {
          d.value_gy = 0.8;
          d.history_gy = Array.from({length:40}, (_,i) => 1.5 * Math.cos(i * 0.25));
        }
        if (channel === 'delta_time') _applyDeltaBarOptsToData(gauge, d);
        return d;
      }
    }
  }

  // ── Live data builder ──────────────────────────────────────────────────────
  let _LIVE_FIELDS = {
    speed:       { key:'speed',       label:'Speed',    unit:'km/h', min:0,   max:300,   sym:false },
    gforce_total:{ key:'g_total',     label:'Total G',  unit:'G',    min:0,   max:3,     sym:false },
    gforce_lon:  { key:'gx',          label:'Long G',   unit:'G',    min:-3,  max:3,     sym:true  },
    gforce_lat:  { key:'gy',          label:'Lat G',    unit:'G',    min:-3,  max:3,     sym:true  },
    rpm:         { key:'rpm',         label:'RPM',      unit:'rpm',  min:0,   max:14000, sym:false },
    exhaust_temp:{ key:'exhaust_temp',label:'Exh Temp', unit:'°C',   min:0,   max:900,   sym:false },
    altitude:    { key:'alt',         label:'Altitude', unit:'m',    min:0,   max:1000,  sym:false },
    lean:        { key:'lean',        label:'Lean',     unit:'°',    min:-60, max:60,    sym:true  },
    lap_time:    { key:'t',           label:'Lap Time', unit:'',     min:0,   max:120,   sym:false },
  };

  function _applyChannelMeta(meta) {
    if (!meta || typeof meta !== 'object') return;
    for (const [ch, src] of Object.entries(meta)) {
      if (!src || typeof src !== 'object') continue;
      const base = _LIVE_FIELDS[ch] || { key: src.hist_key || ch, label: src.label || ch, unit: src.unit || '', min: 0, max: 100, sym: false };
      _LIVE_FIELDS[ch] = {
        ...base,
        key: src.hist_key ?? base.key,
        label: src.label ?? base.label,
        unit: src.unit ?? base.unit,
        min: Number.isFinite(Number(src.min)) ? Number(src.min) : base.min,
        max: Number.isFinite(Number(src.max)) ? Number(src.max) : base.max,
        sym: Boolean(src.symmetric ?? base.sym),
      };
    }
  }

  function _applyEditorCatalog(catalog) {
    if (catalog?.theme_names?.length) {
      _themeNames = catalog.theme_names.map(x => String(x)).filter(Boolean);
    }
    if (catalog?.channel_styles && typeof catalog.channel_styles === 'object') {
      const next = {};
      for (const [ch, styles] of Object.entries(catalog.channel_styles)) {
        if (!Array.isArray(styles)) continue;
        const usable = styles.filter(s => !!GAUGE_RENDERERS[s]);
        if (usable.length) next[ch] = usable;
      }
      if (Object.keys(next).length) _CHANNEL_STYLES = next;
    }
    if (catalog?.channel_labels && typeof catalog.channel_labels === 'object') {
      const ordered = _ALL_CHANNELS.map(c => c.value);
      const keys = [...new Set([...ordered, ...Object.keys(catalog.channel_labels)])];
      _ALL_CHANNELS = keys.map(value => ({
        value,
        label: catalog.channel_labels[value] || (_ALL_CHANNELS.find(x => x.value === value)?.label) || value,
      }));
    }
    if (Array.isArray(catalog?.multi_channels) && catalog.multi_channels.length) {
      _MULTI_CHANNEL_OPTS = catalog.multi_channels.map(value => ({
        value,
        label: (catalog.channel_labels && catalog.channel_labels[value]) || (_ALL_CHANNELS.find(x => x.value === value)?.label) || value,
      }));
    }
    if (catalog?.channel_defaults && typeof catalog.channel_defaults === 'object') {
      _channelDefaultsMap = { ..._channelDefaultsMap, ...catalog.channel_defaults };
    }
    if (Array.isArray(catalog?.gauge_colours) && catalog.gauge_colours.length) {
      _gaugeColours = catalog.gauge_colours.map(x => String(x));
    }
  }

  function _stylesForChannel(channel) {
    const raw = _CHANNEL_STYLES[channel] || ['Numeric'];
    const usable = raw.filter(s => !!GAUGE_RENDERERS[s]);
    return usable.length ? usable : ['Numeric'];
  }

  function _nearestMapIdx(mapLats, mapLons, lat, lon) {
    const n = Math.min(mapLats?.length || 0, mapLons?.length || 0);
    if (n < 2 || !Number.isFinite(lat) || !Number.isFinite(lon)) return 0;
    let best = 0;
    let bestD2 = Infinity;
    for (let i = 0; i < n; i++) {
      const dLat = (mapLats[i] || 0) - lat;
      const dLon = (mapLons[i] || 0) - lon;
      const d2 = dLat * dLat + dLon * dLon;
      if (d2 < bestD2) {
        bestD2 = d2;
        best = i;
      }
    }
    return best;
  }

  function _computeSpeedMax(points, endIdx) {
    if (!points?.length) return 300;
    const e = Math.max(0, Math.min(endIdx ?? (points.length - 1), points.length - 1));
    let rawMax = 0;
    for (let i = 0; i <= e; i++) rawMax = Math.max(rawMax, Number(points[i]?.speed) || 0);
    return Math.max(50, Math.ceil(rawMax * 1.10 / 50) * 50 || 300);
  }

  function buildLiveData(channel, style, frameIdx, gauge = null) {
    const theme = _layout?.theme || 'Dark';
    const base  = { theme, channel };
    if (!_livePoints || !_livePoints.length) return dummyData(channel, style, theme, gauge);
    const idx = Math.max(0, Math.min(frameIdx, _livePoints.length - 1));
    const p   = _livePoints[idx];
    const histStart = Math.max(0, idx - 40);
    const hist = _livePoints.slice(histStart, idx + 1);
    const lapEnd = _lapBoundaryIdx();

    if (channel === 'map') {
      const osmOn = gauge?.track_map_enabled !== false;
      const mapLats = _liveLats || [];
      const mapLons = _liveLons || [];
      const vid = _liveVideo();
      const lapStart = _liveLaps?.[_previewDataLapIdx]?.elapsed_start ?? 0;
      let rawDot = { lat: Number(p?.lat), lon: Number(p?.lon) };
      if (vid && Number(vid.duration) > 0 && _liveSession != null) {
        const telT = vid.currentTime - _liveOffset - lapStart;
        const ip = _interpLiveGpsAtTelT(telT);
        if (ip && Number.isFinite(ip.lat) && Number.isFinite(ip.lon)) rawDot = ip;
      }
      const dotKey = `${_liveSession?.csv_path || ''}|${_previewDataLapIdx}`;
      if (_mapDotSessionKey !== dotKey) {
        _mapDotSessionKey = dotKey;
        _mapSmoothedDot = { ...rawDot };
      } else if (_mapSmoothedDot && Number.isFinite(rawDot.lat) && Number.isFinite(rawDot.lon)) {
        const a = 0.34; // keep in sync with video_renderer._MAP_DOT_EMA_ALPHA
        _mapSmoothedDot = {
          lat: _mapSmoothedDot.lat + a * (rawDot.lat - _mapSmoothedDot.lat),
          lon: _mapSmoothedDot.lon + a * (rawDot.lon - _mapSmoothedDot.lon),
        };
      } else {
        _mapSmoothedDot = { ...rawDot };
      }
      const curIdx = _nearestMapIdx(mapLats, mapLons, rawDot.lat, rawDot.lon);
      const refDispLats = (_mapRefLats && _mapRefLats.length)
        ? _mapRefLats
        : (_refLapPoints ? _refLapPoints.map(p => p.lat) : []);
      const refDispLons = (_mapRefLons && _mapRefLons.length)
        ? _mapRefLons
        : (_refLapPoints ? _refLapPoints.map(p => p.lon) : []);
      const refIdx = refDispLats.length
        ? Math.round((refDispLats.length - 1) * _progressAtLiveIndexWithin(Math.min(idx, lapEnd), lapEnd))
        : 0;
      return {
        theme,
        lats: mapLats, lons: mapLons, cur_idx: curIdx,
        dot_lat: _mapSmoothedDot?.lat ?? rawDot.lat,
        dot_lon: _mapSmoothedDot?.lon ?? rawDot.lon,
        zoom_radius_m:  gauge?.zoom_radius_m ?? 150,
        show_ref:       gauge?.show_ref !== false,
        map_rotate_deg: gauge?.map_rotate_deg ?? 0,
        map_mirror_x:   gauge?.map_mirror_x === true,
        map_mirror_y:   gauge?.map_mirror_y === true,
        ref_lats: refDispLats,
        ref_lons: refDispLons,
        ref_cur_idx: refIdx,
        track_map_lats:  (osmOn && _trackMapGeometry) ? (_trackMapGeometry.lats  || []) : [],
        track_map_lons:  (osmOn && _trackMapGeometry) ? (_trackMapGeometry.lons  || []) : [],
        track_map_areas: (osmOn && _trackMapGeometry) ? (_trackMapGeometry.areas || []) : [],
      };
    }
    if (channel === 'g_meter') {
      const gxSm = hist.map(pt => pt.gx_s ?? pt.gx ?? 0);
      const gySm = hist.map(pt => pt.gy_s ?? pt.gy ?? 0);
      return {
        theme, channel,
        value:       gxSm[gxSm.length - 1] ?? 0,
        value_gy:    gySm[gySm.length - 1] ?? 0,
        history_vals: gxSm,
        history_gy:  gySm,
        min_val: -3, max_val: 3, symmetric: true,
      };
    }
    if (channel === 'delta_time') {
      return _applyDeltaBarOptsToData(gauge, {
        theme, channel,
        value: _liveDeltaNow,
        history_vals: _deltaHistory.length ? _deltaHistory : [],
        ref_history_vals: [],
        label: 'Delta',
        unit: 's',
        min_val: -30,
        max_val: 30,
        symmetric: true,
        sectors: [],
      });
    }
    if (channel === 'multi') {
      const defs = _channelDefaults('multi');
      const keys = (gauge?.multi_channels && gauge.multi_channels.length)
        ? gauge.multi_channels : (defs.multi_channels || []);
      const multi_channels = keys.map((ch, ci) => {
        const m = _LIVE_FIELDS[ch] || { label: ch, unit: '', min: 0, max: 100, sym: false, key: ch };
        const rawVals = (ch === 'gforce_total')
          ? hist.map(pt => pt.g_total_s ?? pt.g_total ?? 0)
          : hist.map(pt => pt[m.key] ?? 0);
        const vals = rawVals;
        const refValsRaw = _refLapPoints?.length
          ? hist.map((_, j) => {
              const hi = Math.min(histStart + j, lapEnd);
              const pgr = _progressAtLiveIndexWithin(hi, lapEnd);
              const v = _refValueAtProgress(pgr, m.key);
              return v ?? 0;
            })
          : [];
        const refVals = refValsRaw;
        return {
          channel: ch, label: m.label, unit: m.unit,
          values:    vals,
          ref_values: refVals,
          value:     vals[vals.length - 1] ?? 0,
          min_val:   m.min, max_val: m.max, symmetric: m.sym, color_idx: ci,
        };
      });
      return { theme, multi_channels, palette: _gaugeColours };
    }
    if (channel === 'info') {
      const ov   = gauge?.info_overrides || {};
      const meta = _liveSessionMeta || {};
      const defs = _channelDefaults('info');
      return {
        ...base,
        info_track:      meta.info_track   || meta.track   || ov.track   || '',
        info_date:       meta.info_date    || ov.date   || '',
        info_time:       meta.info_time    || ov.time   || '',
        info_vehicle:    meta.info_vehicle || meta.vehicle || ov.vehicle || '',
        info_session:    meta.info_session || meta.session || ov.session || '',
        info_weather:    meta.info_weather || ov.weather  || '',
        info_wind:       meta.info_wind    || ov.wind     || '',
        text_align:      gauge?.text_align || defs.text_align || 'left',
        selected_fields: gauge?.selected_fields || defs.selected_fields || [],
      };
    }
    if (channel === 'lap_info') {
      const laps      = _liveLaps || [];
      const idx2      = Math.max(0, Math.min(frameIdx, (_livePoints?.length || 1) - 1));
      const p2        = _livePoints?.[idx2];
      const defs = _channelDefaults('lap_info');
      // Lap label: only per-sample Python fields / device lap counter — never #lap-sel index.
      let lapNum;
      if (p2?.li_lap_num != null && Number.isFinite(Number(p2.li_lap_num))) {
        lapNum = Math.max(0, Math.round(Number(p2.li_lap_num)));
      } else {
        const rawLap = Number(p2?.lap);
        lapNum = Number.isFinite(rawLap) ? Math.max(0, Math.round(rawLap)) : 1;
      }
      const totalLaps = (p2?.li_total_laps != null && Number.isFinite(Number(p2.li_total_laps)))
        ? Math.max(1, Math.round(Number(p2.li_total_laps)))
        : Math.max(1, laps.length);
      const bestMode = gauge?.best_mode || defs.best_mode || 'so_far';
      return {
        ...base,
        lap_num:     lapNum,
        total_laps:  totalLaps,
        lap_elapsed: (p2?.t_display != null) ? Number(p2.t_display) : (p2?.t ?? 0),
        best_so_far: p2?.li_best_so_far ?? null,
        session_best: p2?.li_session_best ?? (_liveSessionMeta?.best_secs ?? null),
        best_mode: bestMode,
        delta_time:  _liveDeltaNow ?? null,
        text_align:  gauge?.text_align || defs.text_align || 'split',
        selected_fields: gauge?.selected_fields || defs.selected_fields || [],
      };
    }
    if (channel === 'image') {
      const path = gauge?.image_path || '';
      const url  = (path && _livePort)
        ? `http://127.0.0.1:${_livePort}/?f=${encodeURIComponent(path)}`
        : '';
      return {
        ...base,
        image_path: path,
        image_url:  url,
        opacity:    gauge?.opacity ?? 1.0,
        fit:        gauge?.fit     || 'contain',
      };
    }
    if (channel === 'lap_time') {
      const m = _LIVE_FIELDS.lap_time;
      const ltVal = (p?.t_display != null) ? Number(p.t_display) : (p?.t ?? 0);
      const refHist = _refLapPoints?.length
        ? hist.map((_, j) => {
            const hi = Math.min(histStart + j, lapEnd);
            const pgr = _progressAtLiveIndexWithin(hi, lapEnd);
            const v = _refValueAtProgress(pgr, m.key);
            return v ?? 0;
          })
        : [];
      return {
        theme, channel,
        value: ltVal,
        history_vals: hist.map(pt => (pt?.t_display != null) ? Number(pt.t_display) : (pt?.t ?? 0)),
        ref_history_vals: refHist,
        label: m.label, unit: m.unit,
        min_val: m.min, max_val: m.max, symmetric: m.sym,
        sectors: [],
      };
    }
    const m = _LIVE_FIELDS[channel];
    if (!m) return dummyData(channel, style, theme, gauge);
    const refHist = _refLapPoints?.length
      ? hist.map((_, j) => {
          const hi = Math.min(histStart + j, lapEnd);
          const pgr = _progressAtLiveIndexWithin(hi, lapEnd);
          const v = _refValueAtProgress(pgr, m.key);
          return v ?? 0;
        })
      : [];
    const gValsRaw = (channel === 'gforce_total')
      ? hist.map(pt => pt.g_total_s ?? pt.g_total ?? 0)
      : hist.map(pt => pt[m.key] ?? 0);
    const gVals = gValsRaw;
    const refVals = refHist;
    return {
      theme, channel,
      value:            gVals[gVals.length - 1] ?? 0,
      history_vals:     gVals,
      ref_history_vals: refVals,
      label: m.label, unit: m.unit,
      min_val: m.min, max_val: (channel === 'speed' ? _liveSpeedMax : m.max), symmetric: m.sym,
      sectors: [],
    };
  }

  // ── Preview timeline (extends past selected lap for live video) ─────────────
  function _timeKey(pt) {
    if (pt && pt.sess_rel != null && Number.isFinite(pt.sess_rel)) return pt.sess_rel;
    return pt?.t ?? 0;
  }


  /** Last frame index still on the preview-anchor lap (ref ghost / compare — tied to loaded history). */
  function _lapBoundaryIdx() {
    const pts = _livePoints;
    if (!pts?.length) return 0;
    const dur = _liveLaps?.[_previewDataLapIdx]?.duration;
    if (dur != null && dur > 0) {
      if (_timeKey(pts[0]) > dur) return 0;
      if (_timeKey(pts[pts.length - 1]) <= dur) return pts.length - 1;
      let lo = 0, hi = pts.length - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (_timeKey(pts[mid]) <= dur) lo = mid; else hi = mid - 1;
      }
      return lo;
    }
    const selNum = _liveLaps?.[_previewDataLapIdx]?.lap_num;
    if (selNum == null) return pts.length - 1;
    let last = 0;
    for (let i = 0; i < pts.length; i++) {
      if ((pts[i].lap ?? selNum) === selNum) last = i;
      else break;
    }
    return last;
  }

  function _progressAtLiveIndexWithin(idx, endIdx) {
    if (!_livePoints?.length) return 0;
    const e = Math.max(0, Math.min(endIdx, _livePoints.length - 1));
    const i = Math.max(0, Math.min(idx, e));
    const total = (_liveCumDist?.[e] ?? 0);
    if (total > 1e-3) return (_liveCumDist?.[i] ?? 0) / total;
    const kEnd = _timeKey(_livePoints[e]);
    const kI = _timeKey(_livePoints[i]);
    return kEnd > 0 ? (kI / kEnd) : 0;
  }

  // ── Live preview helpers ────────────────────────────────────────────────────

  function _liveVideo() { return _container?.querySelector('#preview-video') || null; }

  function _haversineMeters(lat1, lon1, lat2, lon2) {
    const R = 6371000;
    const toRad = d => d * Math.PI / 180;
    const dLat = toRad(lat2 - lat1);
    const dLon = toRad(lon2 - lon1);
    const a = Math.sin(dLat / 2) ** 2
      + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  function _buildCumDist(points) {
    if (!points?.length) return [0];
    const out = new Array(points.length).fill(0);
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1], b = points[i];
      if (a?.lat == null || a?.lon == null || b?.lat == null || b?.lon == null) {
        out[i] = out[i - 1];
      } else {
        out[i] = out[i - 1] + _haversineMeters(a.lat, a.lon, b.lat, b.lon);
      }
    }
    return out;
  }

  async function _loadRefLapIfNeeded() {
    if (!_layout || !_liveSession || !_liveLaps?.length) {
      _refLapPoints = null; _refCumDist = null; _refKey = '';
      _mapRefLats = null; _mapRefLons = null;
      return;
    }
    const mode = _layout.ref_mode || DEFAULT_REF_MODE;
    if (mode === 'none') {
      _refLapPoints = null; _refCumDist = null; _refKey = '';
      _refLapCsvPath = ''; _refLapNum = 0;
      _mapRefLats = null; _mapRefLons = null;
      return;
    }

    const resolved = await API.resolvePreviewReferenceLap(
      _liveSession.csv_path,
      _previewDataLapIdx,
      mode,
      _layout.ref_lap_csv_path || '',
      Number(_layout.ref_lap_num || 0) || 0,
    ).catch(() => null);
    const refCsv = resolved?.ref_csv_path || _liveSession.csv_path;
    const refLapNum = Number(resolved?.ref_lap_num || 0) || null;

    if (!refLapNum) {
      _refLapPoints = null; _refCumDist = null; _refKey = '';
      _refLapCsvPath = ''; _refLapNum = 0;
      _mapRefLats = null; _mapRefLons = null;
      return;
    }
    const key = `${mode}|${refCsv}|${refLapNum}`;
    if (key === _refKey && _refLapPoints?.length) return;

    const laps = await API.getLaps(refCsv).catch(() => []);
    const refLap = (laps || []).find(l => Number(l.lap_num) === Number(refLapNum));
    if (!refLap) {
      _refLapPoints = null; _refCumDist = null; _refKey = '';
      _refLapCsvPath = ''; _refLapNum = 0;
      _mapRefLats = null; _mapRefLons = null;
      return;
    }
    const refPtsRaw = await API.loadLapHistory(refCsv, refLap.lap_idx).catch(() => []);
    _refLapPoints = (refPtsRaw || []).map(p => ({
      ...p,
      gx_s: p?.gx_s ?? (p?.gx ?? 0),
      gy_s: p?.gy_s ?? (p?.gy ?? 0),
      g_total: p?.g_total ?? 0,
      g_total_s: p?.g_total_s ?? p?.g_total ?? 0,
    }));
    _refCumDist = _buildCumDist(_refLapPoints);
    _refKey = key;
    _refLapCsvPath = refCsv;
    _refLapNum = Number(refLapNum) || 0;
  }

  async function _recomputeDeltaSeries() {
    if (!_livePoints?.length) {
      _liveDeltaNow = null;
      _deltaHistory = [];
      return;
    }
    const refMode = _layout?.ref_mode || DEFAULT_REF_MODE;
    const dynamicSoFar = refMode === 'session_best_so_far';
    if ((!dynamicSoFar && (!_refLapCsvPath || !_refLapNum)) || !_liveSession) {
      console.warn('[delta_preview] skip recompute (no ref or no session)', {
        refMode,
        dynamicSoFar,
        refCsv: _refLapCsvPath || '',
        refNum: _refLapNum || 0,
        hasSession: !!_liveSession,
        dataLapIdx: _previewDataLapIdx,
      });
      _livePoints = _livePoints.map(p => ({ ...p, delta_time: null }));
      _liveDeltaNow = null;
      _deltaHistory = [];
      return;
    }
    const deltas = await API.computePreviewDelta(
      _liveSession.csv_path, _previewDataLapIdx, _refLapCsvPath, _refLapNum, refMode
    ).catch((err) => {
      console.warn('[delta_preview] computePreviewDelta RPC rejected/failed', err);
      return [];
    });
    const nLive = _livePoints.length;
    const nD = Array.isArray(deltas) ? deltas.length : 0;
    let finite = 0;
    if (Array.isArray(deltas)) {
      for (const raw of deltas) {
        const dv = Number(raw);
        if (raw != null && Number.isFinite(dv)) finite += 1;
      }
    }
    if (nD !== nLive) {
      console.warn('[delta_preview] length mismatch (RPC vs live points)', { nD, nLive, refMode });
    }
    if (nD > 0 && finite === 0) {
      console.warn('[delta_preview] RPC returned rows but no finite numbers (check Python log)', deltas.slice(0, 8));
    }
    if (nD > 0 && finite > 0) {
      console.info('[delta_preview] applied', { nD, nLive, finite, refMode });
    }
    _livePoints = _livePoints.map((p, i) => {
      const raw = deltas?.[i];
      const dv = Number(raw);
      return { ...p, delta_time: (raw != null && Number.isFinite(dv)) ? dv : null };
    });
  }

  async function _syncPreviewMapTracks() {
    if (!_liveSession || !_liveLaps?.length) return;
    const tracks = await API.getPreviewMapTracks(
      _liveSession.csv_path,
      _previewDataLapIdx,
      _refLapCsvPath || '',
      Number(_refLapNum || 0) || 0,
    ).catch(() => null);
    if (tracks) {
      _liveLats = tracks.lap_lats || _liveLats || [];
      _liveLons = tracks.lap_lons || _liveLons || [];
      _mapRefLats = tracks.ref_lats || [];
      _mapRefLons = tracks.ref_lons || [];
    }
  }

  function _refTimeAtProgress(progress01) {
    if (!_refLapPoints?.length) return null;
    const p = Math.max(0, Math.min(1, progress01));
    const last = _refLapPoints.length - 1;
    const total = (_refCumDist?.[last] ?? 0);
    if (total > 1e-3) {
      const target = p * total;
      let lo = 0, hi = last;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if ((_refCumDist[mid] ?? 0) < target) lo = mid + 1; else hi = mid;
      }
      const i1 = Math.max(1, lo), i0 = i1 - 1;
      const d0 = _refCumDist[i0] ?? 0, d1 = _refCumDist[i1] ?? d0;
      const t0 = _refLapPoints[i0]?.t ?? 0, t1 = _refLapPoints[i1]?.t ?? t0;
      const u = d1 > d0 ? (target - d0) / (d1 - d0) : 0;
      return t0 + (t1 - t0) * u;
    }
    const dur = _refLapPoints[last]?.t ?? 0;
    return p * dur;
  }

  function _refValueAtProgress(progress01, key) {
    if (!_refLapPoints?.length) return null;
    const p = Math.max(0, Math.min(1, progress01));
    const last = _refLapPoints.length - 1;
    const total = (_refCumDist?.[last] ?? 0);
    if (total > 1e-3) {
      const target = p * total;
      let lo = 0, hi = last;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if ((_refCumDist[mid] ?? 0) < target) lo = mid + 1; else hi = mid;
      }
      const i1 = Math.max(1, lo), i0 = i1 - 1;
      const d0 = _refCumDist[i0] ?? 0, d1 = _refCumDist[i1] ?? d0;
      const v0 = (_refLapPoints[i0]?.[key] ?? 0);
      const v1 = (_refLapPoints[i1]?.[key] ?? v0);
      const u = d1 > d0 ? (target - d0) / (d1 - d0) : 0;
      return v0 + (v1 - v0) * u;
    }
    const refDur = _refLapPoints[last]?.t ?? 1;
    const t = p * refDur;
    let lo = 0, hi = last;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if ((_refLapPoints[mid]?.t ?? 0) < t) lo = mid + 1; else hi = mid;
    }
    const i1 = Math.max(1, lo), i0 = i1 - 1;
    const t0 = _refLapPoints[i0]?.t ?? 0, t1 = _refLapPoints[i1]?.t ?? t0;
    const v0 = (_refLapPoints[i0]?.[key] ?? 0);
    const v1 = (_refLapPoints[i1]?.[key] ?? v0);
    const u = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
    return v0 + (v1 - v0) * u;
  }

  function _progressAtLiveIndex(idx) {
    if (!_livePoints?.length) return 0;
    const i = Math.max(0, Math.min(idx, _livePoints.length - 1));
    const total = (_liveCumDist?.[_liveCumDist.length - 1] ?? 0);
    if (total > 1e-3) return (_liveCumDist?.[i] ?? 0) / total;
    const curT = _timeKey(_livePoints[i]);
    const dur = _timeKey(_livePoints[_livePoints.length - 1]) || 1;
    return dur > 0 ? (curT / dur) : 0;
  }

  function _updateDeltaAtFrame(frameIdx) {
    if (!_livePoints?.length) {
      _liveDeltaNow = null;
      _deltaHistory = [];
      return;
    }
    const idx = Math.max(0, Math.min(frameIdx, _livePoints.length - 1));
    const dv = _livePoints[idx]?.delta_time;
    _liveDeltaNow = Number.isFinite(dv) ? Number(dv) : null;
    const start = Math.max(0, idx - 179);
    _deltaHistory = _livePoints
      .slice(start, idx + 1)
      .map(p => p?.delta_time)
      .filter(v => Number.isFinite(v));
  }

  /** Align preview frame / delta with current video time (or frame 0 if no video). */
  function _syncPreviewFrameToVideo() {
    if (!_livePoints?.length) return;
    const vid = _liveVideo();
    if (vid && vid.readyState >= 1 && vid.duration) {
      const lapStart = _liveLaps?.[_previewDataLapIdx]?.elapsed_start ?? 0;
      const telT = vid.currentTime - _liveOffset - lapStart;
      _liveFrameIdx = _findFrameIdx(telT);
    } else {
      _liveFrameIdx = 0;
    }
    _updateDeltaAtFrame(_liveFrameIdx);
  }

  /**
   * Best-effort seek video to the chosen lap start (sync_offset + lap.elapsed_start).
   * We set `currentTime` immediately and re-apply once `loadedmetadata` fires,
   * so entering editor / jumping laps doesn't wait for a late seek.
   */
  function _seekVideoToLapStart(lapIdx) {
    const vid = _liveVideo();
    if (!vid) return;
    const lap = _liveLaps?.[lapIdx];
    if (!lap) return;
    const seekTo = (Number(_liveOffset) || 0) + (Number(lap.elapsed_start) || 0);

    const apply = () => {
      try {
        const dur = Number(vid.duration);
        const clamped = dur > 0 && Number.isFinite(dur)
          ? Math.max(0, Math.min(dur, seekTo))
          : Math.max(0, seekTo);
        vid.currentTime = clamped;
        const scrub = _container?.querySelector('#live-scrub');
        if (scrub) scrub.value = String(Math.round(clamped * 1000));
      } catch (_) { /* ignore */ }
    };

    apply();

    const ready = vid.readyState >= 1 && Number.isFinite(Number(vid.duration)) && Number(vid.duration) > 0;
    if (!ready) {
      try { vid.addEventListener('loadedmetadata', apply, { once: true }); } catch (_) {}
    }
  }

  function _findFrameIdx(telT) {
    const pts = _livePoints;
    if (!pts || !pts.length) return 0;
    const k0 = _timeKey(pts[0]);
    if (telT <= k0) return 0;
    const kLast = _timeKey(pts[pts.length - 1]);
    if (telT >= kLast) return pts.length - 1;
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (_timeKey(pts[mid]) < telT) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  /** Linearly interpolate GPS along the preview timeline (sub-sample smooth motion). */
  function _interpLiveGpsAtTelT(telT) {
    const pts = _livePoints;
    if (!pts?.length) return null;
    const k0 = _timeKey(pts[0]);
    if (telT <= k0) {
      const a = pts[0];
      return { lat: Number(a?.lat), lon: Number(a?.lon) };
    }
    const kLast = _timeKey(pts[pts.length - 1]);
    if (telT >= kLast) {
      const b = pts[pts.length - 1];
      return { lat: Number(b?.lat), lon: Number(b?.lon) };
    }
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (_timeKey(pts[mid]) < telT) lo = mid + 1; else hi = mid;
    }
    const i1 = lo;
    const i0 = Math.max(0, i1 - 1);
    const t0 = _timeKey(pts[i0]);
    const t1 = _timeKey(pts[i1]);
    const u = t1 > t0 ? (telT - t0) / (t1 - t0) : 0;
    const la0 = Number(pts[i0]?.lat);
    const lo0 = Number(pts[i0]?.lon);
    const la1 = Number(pts[i1]?.lat);
    const lo1 = Number(pts[i1]?.lon);
    if (![la0, lo0, la1, lo1].every(Number.isFinite)) return null;
    return {
      lat: la0 + u * (la1 - la0),
      lon: lo0 + u * (lo1 - lo0),
    };
  }

  function _rerenderLive() {
    const area = getPreviewEl();
    if (!area || !_layout) return;
    _ensureLayoutGauges();
    area.querySelectorAll('.gauge-canvas').forEach(el => {
      const idx = parseInt(el.dataset.gaugeIdx);
      if (!isNaN(idx) && _layout.gauges[idx]) renderGaugeEl(el, _layout.gauges[idx]);
    });
  }

  function _startLiveRaf() {
    if (_liveRafId) return;
    let lastIdx = -1;
    function tick() {
      const vid = _liveVideo();
      if (vid && _livePoints) {
        // Session timeline: same base as export — sess_t = vid_t - sync_offset, then minus lap start.
        const lapStart = _liveLaps?.[_previewDataLapIdx]?.elapsed_start ?? 0;
        const telT     = vid.currentTime - _liveOffset - lapStart;
        const newIdx   = _findFrameIdx(telT);
        if (newIdx !== lastIdx) {
          lastIdx = newIdx;
          _liveFrameIdx = newIdx;
          _updateDeltaAtFrame(newIdx);
          _rerenderLive();
        }
      }
      _liveRafId = requestAnimationFrame(tick);
    }
    _liveRafId = requestAnimationFrame(tick);
  }

  function _stopLiveRaf() {
    if (_liveRafId) { cancelAnimationFrame(_liveRafId); _liveRafId = null; }
  }

  // ── Lap time formatter ─────────────────────────────────────────────────────
  function _fmtLapTime(secs) {
    if (secs == null || isNaN(secs)) return '—';
    const m = Math.floor(secs / 60);
    const s = (secs % 60).toFixed(3).padStart(6, '0');
    return `${m}:${s}`;
  }

  // ── Update lap selector dropdown ────────────────────────────────────────────
  function _updateLapSelector() {
    const sel     = _container?.querySelector('#lap-sel');
    const prevBtn = _container?.querySelector('#lap-prev');
    const nextBtn = _container?.querySelector('#lap-next');
    if (!sel) return;

    const laps = _liveLaps || [];
    if (!laps.length) {
      sel.innerHTML = '<option value="">— no laps —</option>';
      if (prevBtn) prevBtn.disabled = true;
      if (nextBtn) nextBtn.disabled = true;
      return;
    }

    let timedCount = 0;
    sel.innerHTML = laps.map((l, i) => {
      const dur  = l.duration != null ? _fmtLapTime(l.duration) : '?';
      const star = l.is_best ? ' ★' : '';
      let label;
      if (l.is_outlap) {
        label = 'Outlap';
      } else if (l.is_inlap) {
        label = 'Inlap';
      } else {
        timedCount++;
        label = `Lap ${timedCount}`;
      }
      return `<option value="${i}" ${i === _selLapIdx ? 'selected' : ''}>${label}  ${dur}${star}</option>`;
    }).join('');

    if (prevBtn) prevBtn.disabled = (_selLapIdx <= 0);
    if (nextBtn) nextBtn.disabled = (_selLapIdx >= laps.length - 1);
  }

  // ── Wire video / scrub controls (called once from mount) ────────────────────
  function _wireVideoControls() {
    const vid     = _liveVideo();
    const scrub   = _container?.querySelector('#live-scrub');
    const timeEl  = _container?.querySelector('#live-time');
    const playBtn = _container?.querySelector('#live-play');
    
    if (vid) {
      // Apply the actual video aspect ratio to the preview area and persist it in
      // State so the next mount can use it immediately (avoiding the 16/9 flash).
      function _applyAspect() {
        if (!vid.videoWidth || !vid.videoHeight) return;
        const area = getPreviewEl();
        if (area) {
          area.style.aspectRatio = `${vid.videoWidth} / ${vid.videoHeight}`;
          requestAnimationFrame(() => rebuildGaugeCanvases());
        }
        // Persist so mount() can render the correct ratio before loadedmetadata fires.
        const ps = State.get('previewSession');
        if (ps && (ps.video_w !== vid.videoWidth || ps.video_h !== vid.videoHeight)) {
          State.set('previewSession', { ...ps, video_w: vid.videoWidth, video_h: vid.videoHeight });
        }
      }

      vid.addEventListener('loadedmetadata', () => {
        if (scrub) scrub.max = Math.round(vid.duration * 1000);
        _applyAspect();
        // Do not force-seek on entering editor: start from the video's beginning.
        // Gauge/telemetry alignment will catch up via timeupdate + _syncPreviewFrameToVideo.
        if (scrub) scrub.value = Math.round(vid.currentTime * 1000);
      });

      // Metadata may already be available if the browser cached it from a previous
      // load — in that case loadedmetadata won't fire again, so apply immediately.
      if (vid.readyState >= 1) {
        _applyAspect();
        if (scrub && vid.duration) scrub.max = Math.round(vid.duration * 1000);
        // Do not force-seek on fast remount; keep video at its natural start position.
        if (scrub) scrub.value = Math.round(vid.currentTime * 1000);
      }
      vid.addEventListener('timeupdate', () => {
        if (scrub && !vid.seeking) scrub.value = Math.round(vid.currentTime * 1000);
        if (timeEl) timeEl.textContent = _fmtVTime(vid.currentTime);
        // Hard-sync gauges on every video time tick so drag/seek always updates
        // even if RAF was interrupted by remount or tab focus changes.
        if (_livePoints?.length) {
          const lapStart = _liveLaps?.[_previewDataLapIdx]?.elapsed_start ?? 0;
          const telT = vid.currentTime - _liveOffset - lapStart;
          _liveFrameIdx = _findFrameIdx(telT);
          _updateDeltaAtFrame(_liveFrameIdx);
          _rerenderLive();
        }
      });
      vid.addEventListener('play',  () => { if (playBtn) playBtn.textContent = '⏸'; });
      vid.addEventListener('pause', () => { if (playBtn) playBtn.textContent = '▶'; });
    }

    const safePlay = (v) => {
      if (!v || !v.isConnected) return;
      try {
        const p = v.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (_) { /* ignore */ }
    };

    playBtn?.addEventListener('click', () => {
      if (!vid) return;
      if (vid.paused) safePlay(vid);
      else {
        try { vid.pause(); } catch (_) { /* ignore */ }
      }
    });

    scrub?.addEventListener('input', e => {
      const v = _liveVideo();
      if (v && v.readyState >= 1 && v.duration) {
        // Video present — seek it, and immediately refresh gauges for responsiveness.
        const newT = parseFloat(e.target.value) / 1000;
        v.currentTime = newT;
        if (_livePoints?.length) {
          const lapStart = _liveLaps?.[_previewDataLapIdx]?.elapsed_start ?? 0;
          const telT = newT - _liveOffset - lapStart;
          _liveFrameIdx = _findFrameIdx(telT);
          _updateDeltaAtFrame(_liveFrameIdx);
          _rerenderLive();
        }
      } else {
        // No video — drive telemetry directly from scrub position
        const maxMs = parseFloat(scrub.max) || 1000;
        const maxT  = _livePoints?.length ? _timeKey(_livePoints[_livePoints.length - 1]) : 1;
        const t     = (parseFloat(e.target.value) / maxMs) * maxT;
        _liveFrameIdx = _findFrameIdx(t);
        _updateDeltaAtFrame(_liveFrameIdx);
        if (timeEl) timeEl.textContent = _fmtVTime(t);
        _rerenderLive();
      }
    });
  }

  // ── Load telemetry data for one lap ────────────────────────────────────────
  async function _loadLapData(lapIdx, mountGen) {
    if (!_liveSession) return;
    const labelEl = _container?.querySelector('#live-label');
    const scrub   = _container?.querySelector('#live-scrub');

    // Anchor all preview samples + RPC + video ``telT`` to this lap index (not #lap-sel alone).
    _previewDataLapIdx = lapIdx;

    _livePoints   = null;
    _liveLats     = null;
    _liveLons     = null;
    _mapRefLats   = null;
    _mapRefLons   = null;
    _liveCumDist  = null;
    _liveFrameIdx = 0;
    _liveDeltaNow = null;
    _deltaHistory = [];
    _lastRefNearestIdx = null;
    _liveSpeedMax = 300;
    _mapSmoothedDot = null;
    _mapDotSessionKey = '';
    // Only zero scrub for telemetry-only preview. With video, scrub tracks
    // vid.currentTime — resetting to 0 fights switchLap's seek and desyncs delta.
    const vidEarly = _liveVideo();
    const hasVid = !!(vidEarly && (vidEarly.currentSrc || vidEarly.src));
    if (scrub && (!vidEarly || (!vidEarly.duration && !hasVid))) scrub.value = 0;

    try {
      // If the same lap is jumped to again, reuse cached telemetry points.
      // switchLap always calls _loadLapData, so without this we'd repeatedly hit
      // Python for loadPreviewHistory().
      const cached = _lapHistoryCache.get(lapIdx);
      if (cached && Array.isArray(cached)) {
        _livePoints = cached;
        _liveLats   = _livePoints.map(p => p.lat);
        _liveLons   = _livePoints.map(p => p.lon);
        _liveCumDist = _buildCumDist(_livePoints);
        _liveSpeedMax = _computeSpeedMax(_livePoints, _lapBoundaryIdx());
        await _loadRefLapIfNeeded();
        await _syncPreviewMapTracks();
        await _recomputeDeltaSeries();
        _syncPreviewFrameToVideo();
      } else {
      const pts = await API.loadPreviewHistory(_liveSession.csv_path, lapIdx);
      if (mountGen !== undefined && _mountGen !== mountGen) return;  // stale

      const raw = Array.isArray(pts) ? pts : [];
      _livePoints = raw.map(pt => ({
        ...pt,
        gx_s: pt?.gx_s ?? (pt?.gx ?? 0),
        gy_s: pt?.gy_s ?? (pt?.gy ?? 0),
        g_total: (pt?.g_total != null) ? pt.g_total : 0,
        g_total_s: (pt?.g_total_s != null)
          ? pt.g_total_s
          : ((pt?.g_total != null) ? pt.g_total : 0),
      }));
      _liveLats   = _livePoints.map(p => p.lat);
      _liveLons   = _livePoints.map(p => p.lon);
      _liveCumDist = _buildCumDist(_livePoints);
      _liveSpeedMax = _computeSpeedMax(_livePoints, _lapBoundaryIdx());
      await _loadRefLapIfNeeded();
      await _syncPreviewMapTracks();
      await _recomputeDeltaSeries();
      _syncPreviewFrameToVideo();

      if (labelEl) labelEl.textContent = `${_livePoints?.length || 0} samples · lap ${lapIdx + 1}`;
      // Store base telemetry points for reuse on future lap jumps.
      if (_livePoints && Array.isArray(_livePoints)) _lapHistoryCache.set(lapIdx, _livePoints);
      }

      // If no video, set scrub range from telemetry time span
      const vid = _liveVideo();
      if ((!vid || !vid.duration) && scrub && (_livePoints?.length || 0)) {
        const endT = _timeKey(_livePoints[_livePoints.length - 1]);
        scrub.max = Math.round(endT * 1000);
      } else if (vid && scrub && vid.duration) {
        scrub.value = String(Math.round(vid.currentTime * 1000));
      }

      // Render immediately; video seek may still be finishing — RAF will correct.
      _rerenderLive();
    } catch (e) {
      console.error('[_loadLapData] failed for lap', lapIdx, e);
      if (labelEl) labelEl.textContent = '遥测数据加载失败';
      _livePoints = [];
      _liveLats = [];
      _liveLons = [];
      _liveCumDist = [0];
      _liveSpeedMax = 300;
    }
  }

  // ── Switch lap (called from lap selector) ──────────────────────────────────
  async function switchLap(lapIdx) {
    if (!_liveSession || !_liveLaps) return;
    if (lapIdx < 0 || lapIdx >= _liveLaps.length) return;

    const myGen = _mountGen;  // guard against navigation during lap load
    _selLapIdx = lapIdx;
    _stopLiveRaf();

    // Editor ignores Data-tab preview offsets; keep using saved offset only.
    if (_liveSession?.sync_offset != null && Number.isFinite(Number(_liveSession.sync_offset))) {
      _liveOffset = Number(_liveSession.sync_offset);
    }

    // Seek video immediately (best-effort) so jumping laps doesn't wait for metadata.
    _seekVideoToLapStart(lapIdx);

    // Keep State in sync so Export page sees the right lap
    State.set('previewSession', {
      ...(State.get('previewSession') || {}),
      lap_idx: lapIdx,
    });

    _updateLapSelector();
    await _loadLapData(lapIdx, myGen);
    if (_mountGen !== myGen) return;
    _startLiveRaf();
  }

  // ── Stage current lap for export ───────────────────────────────────────────
  function _stageLapForExport() {
    if (!_liveSession) return;
    const lap   = _liveLaps?.[_selLapIdx];
    const meta  = _liveSessionMeta || {};
    const scope = State.get('exportScope') || 'full';
    const durStr = lap?.duration != null ? _fmtLapTime(lap.duration) : '?';
    const lapLabel = [
      meta.track || '',
      `Lap ${_selLapIdx + 1}${lap?.is_best ? ' ★' : ''}`,
      durStr,
    ].filter(Boolean).join(' — ');

    const item = {
      csv_path:    _liveSession.csv_path,
      lap_idx:     _selLapIdx,
      video_paths: _liveSession.video_paths || [],
      sync_offset: _liveSession.sync_offset ?? 0,
      source:      _liveSession.source || '',
      duration:    lap?.duration ?? null,
      is_best:     lap?.is_best ?? false,
      track:       meta.track || '',
      csv_start:   _liveSession.csv_start || null,
      lap_label:   lapLabel,
      scope,
    };
    const current = State.get('selectedItems') || [];
    // Avoid duplicates
    const exists = current.some(x => x.csv_path === item.csv_path && x.lap_idx === item.lap_idx);
    if (!exists) State.set('selectedItems', [...current, item]);

    // Brief visual feedback on button
    const btn = _container?.querySelector('#stage-export-btn');
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓ 已添加';
      btn.disabled = true;
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1500);
    }
  }

  // ── Load full session (metadata + laps + first lap telemetry) ──────────────
  async function loadLiveSession(session) {
    const myGen  = _mountGen;  // bail out if unmounted before we finish
    _liveSession = session;
    // Editor always uses persisted (saved) offset from config, not Data-tab preview temps.
    try {
      const cfg = await API.getConfig().catch(() => ({}));
      const saved = _savedOffsetFromConfig(cfg, session.csv_path);
      if (saved != null) {
        session.sync_offset = saved;
      }
    } catch (_) {}
    _liveOffset  = session.sync_offset ?? 0;
    _trackMapGeometry = null;
    _lapHistoryCache = new Map();

    // Fetch metadata and lap list in parallel — no track-map call here so these
    // are never blocked by a slow session-file reload on the Python side.
    const [meta, laps, channelMeta] = await Promise.all([
      API.getSessionMeta(session.csv_path).catch(() => ({})),
      API.getLaps(session.csv_path).catch(() => []),
      API.getChannelMeta().catch(() => ({})),
    ]);
    if (_mountGen !== myGen) return;  // navigated away while awaiting

    _liveSessionMeta = meta;
    _liveLaps        = Array.isArray(laps) ? laps : [];
    _applyChannelMeta(channelMeta);
    // Default to the first lap in list (including outlap) so entering editor
    // starts from the beginning of the video timeline.
    const lapList = _liveLaps;
    let startIdx = session.lap_idx ?? 0;
    if (lapList.length) {
      startIdx = Math.max(0, Math.min(startIdx, lapList.length - 1));
    } else {
      startIdx = 0;
    }
    _selLapIdx = startIdx;

    // Start from the video beginning by default (no forced seek here).

    _updateLapSelector();
    await _loadLapData(_selLapIdx, myGen);
    if (_mountGen !== myGen) return;
    _startLiveRaf();

    // Fetch OSM track map AFTER telemetry is loaded — centroid comes from already-loaded
    // GPS data so Python never has to reload the session file for this call.
    _fetchTrackMapGeometry(myGen);
  }

  // ── Fetch OSM track map geometry using GPS centroid from loaded telemetry ────
  function _fetchTrackMapGeometry(mountGen) {
    if (!_liveSession || !_liveLats?.length) return;
    const n    = _liveLats.length;
    const clat = _liveLats.reduce((a, b) => a + b, 0) / n;
    const clon = _liveLons.reduce((a, b) => a + b, 0) / n;
    API.getTrackMapGeometry(_liveSession.csv_path, clat, clon)
      .then(geom => {
        if (_mountGen !== mountGen) return;
        _trackMapGeometry = (geom?.lats?.length) ? geom : null;
        if (_trackMapGeometry) _rerenderLive();
      })
      .catch(() => {});
  }

  function _fmtVTime(t) {
    const m = Math.floor(t / 60);
    const s = (t % 60).toFixed(3).padStart(6, '0');
    return `${m}:${s}`;
  }

  // ── Preview area helpers ────────────────────────────────────────────────────
  function getPreviewEl() { return _container?.querySelector('#preview-area'); }

  function previewDims() {
    const el = getPreviewEl();
    if (!el) return {w: 1280, h: 720};
    return { w: el.offsetWidth, h: el.offsetHeight };
  }

  // ── Gauge canvas rendering ─────────────────────────────────────────────────
  function renderGaugeEl(gEl, gauge) {
    const {w, h} = previewDims();
    const gw = Math.max(32, Math.round(gauge.w * w));
    const gh = Math.max(24, Math.round(gauge.h * h));

    gEl.width  = gw;
    gEl.height = gh;
    gEl.style.left = `${gauge.x * 100}%`;
    gEl.style.top  = `${gauge.y * 100}%`;
    gEl.style.width  = `${gauge.w * 100}%`;
    gEl.style.height = `${gauge.h * 100}%`;

    const ctx = gEl.getContext('2d');
    ctx.clearRect(0, 0, gw, gh);

    const renderer = GAUGE_RENDERERS[gauge.style];
    if (!renderer) return;

    try {
      const data = (_livePoints && _livePoints.length)
        ? buildLiveData(gauge.channel, gauge.style, _liveFrameIdx, gauge)
        : dummyData(gauge.channel, gauge.style, _layout?.theme || 'Dark', gauge);
      renderer(ctx, data, gw, gh);
    } catch (e) {
      console.error('[renderGaugeEl] channel:', gauge.channel, 'style:', gauge.style, e);
      // Draw error placeholder
      ctx.fillStyle = 'rgba(239,68,68,0.4)';
      ctx.fillRect(0, 0, gw, gh);
      ctx.fillStyle = 'white';
      ctx.font = `${Math.max(8, Math.round(Math.min(gw, gh) * 0.12))}px sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('Error', gw / 2, gh / 2);
    }
  }

  function rebuildGaugeCanvases() {
    const area = getPreviewEl();
    if (!area || !_layout) return;
    _ensureLayoutGauges();

    // Remove old gauge canvases and resize handles
    area.querySelectorAll('.gauge-canvas, .resize-handle').forEach(el => el.remove());

    _layout.gauges.forEach((g, idx) => {
      if (!g.visible) return;

      const canvas = document.createElement('canvas');
      canvas.className = 'gauge-canvas';
      canvas.dataset.gaugeIdx = idx;
      canvas.style.cssText = `
        position: absolute;
        cursor: move;
        box-sizing: border-box;
        z-index: 2;
      `;
      // WYSIWYG: no canvas outline — export frames have no editor chrome; selection is only in the list.
      canvas.style.outline = 'none';

      area.appendChild(canvas);
      renderGaugeEl(canvas, g);
    });

    // Draw resize handles as siblings in the area (NOT children of canvas,
    // which can swallow pointer events and clip overflow).
    _layout.gauges.forEach((g, idx) => {
      if (!g.visible || idx !== _selected) return;
      const handle = document.createElement('div');
      handle.className = 'resize-handle';
      handle.dataset.gaugeIdx = idx;
      handle.style.cssText = `
        position: absolute;
        left: ${(g.x + g.w) * 100}%;
        top: ${(g.y + g.h) * 100}%;
        transform: translate(-50%, -50%);
        width: 10px;
        height: 10px;
        background: rgba(79,142,247,0.55);
        border: 1px solid rgba(255,255,255,0.35);
        border-radius: 2px;
        cursor: se-resize;
        z-index: 10;
        box-sizing: border-box;
      `;
      area.appendChild(handle);
    });
  }

  function rerenderAll() {
    const area = getPreviewEl();
    if (!area || !_layout) return;
    _ensureLayoutGauges();
    area.querySelectorAll('.gauge-canvas').forEach(el => {
      const idx = parseInt(el.dataset.gaugeIdx);
      if (!isNaN(idx) && _layout.gauges[idx]) {
        renderGaugeEl(el, _layout.gauges[idx]);
      }
    });
  }

  // ── Snap guides ─────────────────────────────────────────────────────────────

  function _clearGuides() {
    const area = getPreviewEl();
    if (!area) return;
    area.querySelectorAll('.snap-guide').forEach(el => el.remove());
  }

  function _showGuide(area, axis, pos) {
    // axis: 'x' = vertical line at normalised x, 'y' = horizontal line at normalised y
    const el = document.createElement('div');
    el.className = 'snap-guide';
    el.style.cssText = `
      position:absolute; pointer-events:none; z-index:20;
      background:transparent;
      border-${axis === 'x' ? 'left' : 'top'}:1px dashed cyan;
      ${axis === 'x'
        ? `left:${pos * 100}%; top:0; width:0; height:100%;`
        : `top:${pos * 100}%; left:0; height:0; width:100%;`}
    `;
    area.appendChild(el);
  }

  // ── Snap logic ───────────────────────────────────────────────────────────────

  /**
   * Snap a value to: canvas edges (0,1), element edges of other gauges,
   * and optionally a size grid. Returns {val, snapped}.
   *
   * edges: array of normalised positions to snap to
   * threshold: snap distance
   * gridStep: if >0, also snap to nearest multiple of gridStep
   */
  function _snapVal(raw, edges, threshold, gridStep = 0) {
    let best = Infinity, snappedTo = null;
    for (const e of edges) {
      const d = Math.abs(raw - e);
      if (d < threshold && d < best) { best = d; snappedTo = e; }
    }
    if (gridStep > 0) {
      const gridSnap = Math.round(raw / gridStep) * gridStep;
      const d = Math.abs(raw - gridSnap);
      if (d < threshold && d < best) { best = d; snappedTo = gridSnap; }
    }
    return snappedTo !== null ? { val: snappedTo, snapped: true } : { val: raw, snapped: false };
  }

  function _applySnap(g, type, draggedIdx) {
    const area   = getPreviewEl();
    const guides = { x: new Set(), y: new Set() };
    _ensureLayoutGauges();

    // Collect edge snap positions: canvas boundaries
    const xEdges = [0, 1];
    const yEdges = [0, 1];

    // Collect element-edge snap positions from other gauges
    for (let i = 0; i < _layout.gauges.length; i++) {
      if (i === draggedIdx) continue;
      const o = _layout.gauges[i];
      xEdges.push(o.x, o.x + o.w, o.x + o.w / 2);
      yEdges.push(o.y, o.y + o.h, o.y + o.h / 2);
    }
    // Also snap to canvas centre
    xEdges.push(0.5); yEdges.push(0.5);

    if (type === 'move') {
      // Snap left edge, right edge, horizontal centre
      const lSnap = _snapVal(g.x,           xEdges, SNAP_NORM);
      const rSnap = _snapVal(g.x + g.w,     xEdges, SNAP_NORM);
      const cxSnp = _snapVal(g.x + g.w / 2, xEdges, SNAP_NORM);

      if (lSnap.snapped)      { g.x = lSnap.val;           guides.x.add(lSnap.val); }
      else if (rSnap.snapped) { g.x = rSnap.val - g.w;     guides.x.add(rSnap.val); }
      else if (cxSnp.snapped) { g.x = cxSnp.val - g.w / 2; guides.x.add(cxSnp.val); }

      const tSnap = _snapVal(g.y,           yEdges, SNAP_NORM);
      const bSnap = _snapVal(g.y + g.h,     yEdges, SNAP_NORM);
      const cySnp = _snapVal(g.y + g.h / 2, yEdges, SNAP_NORM);

      if (tSnap.snapped)      { g.y = tSnap.val;           guides.y.add(tSnap.val); }
      else if (bSnap.snapped) { g.y = bSnap.val - g.h;     guides.y.add(bSnap.val); }
      else if (cySnp.snapped) { g.y = cySnp.val - g.h / 2; guides.y.add(cySnp.val); }

    } else { // resize
      const wSnap = _snapVal(g.w, [], SNAP_NORM, SNAP_SIZE_STEP);
      const hSnap = _snapVal(g.h, [], SNAP_NORM, SNAP_SIZE_STEP);
      // Also snap right edge to element edges
      const rSnap = _snapVal(g.x + g.w, xEdges, SNAP_ELEM_NORM);
      const bSnap = _snapVal(g.y + g.h, yEdges, SNAP_ELEM_NORM);

      if (rSnap.snapped) { g.w = rSnap.val - g.x; guides.x.add(rSnap.val); }
      else if (wSnap.snapped) { g.w = wSnap.val; }

      if (bSnap.snapped) { g.h = bSnap.val - g.y; guides.y.add(bSnap.val); }
      else if (hSnap.snapped) { g.h = hSnap.val; }
    }

    // Clamp after snap
    g.x = Math.max(0, Math.min(1 - g.w, g.x));
    g.y = Math.max(0, Math.min(1 - g.h, g.y));
    g.w = Math.max(MIN_NORM, Math.min(1 - g.x, g.w));
    g.h = Math.max(MIN_NORM, Math.min(1 - g.y, g.h));

    // Draw guides
    if (area) {
      _clearGuides();
      guides.x.forEach(v => _showGuide(area, 'x', v));
      guides.y.forEach(v => _showGuide(area, 'y', v));
    }
  }

  // ── Mouse events ────────────────────────────────────────────────────────────
  function setupMouseEvents() {
    const area = getPreviewEl();
    if (!area) return;

    area.addEventListener('mousedown', e => {
      const canvas = e.target.closest('.gauge-canvas');
      const handle = e.target.closest('.resize-handle');

      if (handle) {
        const idx = parseInt(handle.dataset.gaugeIdx);
        if (!isNaN(idx)) {
          _drag = {
            type: 'resize',
            gaugeIdx: idx,
            startMx: e.clientX,
            startMy: e.clientY,
            startG: { ...(_layout.gauges[idx]) },
          };
          e.preventDefault();
          e.stopPropagation();
        }
        return;
      }

      if (canvas) {
        const idx = parseInt(canvas.dataset.gaugeIdx);
        if (!isNaN(idx)) {
          selectGauge(idx);
          _drag = {
            type: 'move',
            gaugeIdx: idx,
            startMx: e.clientX,
            startMy: e.clientY,
            startG: { ...(_layout.gauges[idx]) },
          };
          e.preventDefault();
        }
        return;
      }

      // Click on background → deselect
      selectGauge(null);
    });

    document.addEventListener('mousemove', e => {
      if (!_drag) return;
      const {w, h} = previewDims();
      const dx = (e.clientX - _drag.startMx) / w;
      const dy = (e.clientY - _drag.startMy) / h;
      const g  = _layout.gauges[_drag.gaugeIdx];
      const sg = _drag.startG;

      if (_drag.type === 'move') {
        g.x = Math.max(0, Math.min(1 - g.w, sg.x + dx));
        g.y = Math.max(0, Math.min(1 - g.h, sg.y + dy));
      } else {
        g.w = Math.max(MIN_NORM, sg.w + dx);
        g.h = Math.max(MIN_NORM, sg.h + dy);
        g.w = Math.min(g.w, 1 - g.x);
        g.h = Math.min(g.h, 1 - g.y);
      }

      // Apply snapping (modifies g in-place, draws guide lines)
      _applySnap(g, _drag.type, _drag.gaugeIdx);

      // Update canvas position directly (fast path — no full rebuild)
      const canvas = area.querySelector(`.gauge-canvas[data-gauge-idx="${_drag.gaugeIdx}"]`);
      if (canvas) {
        canvas.style.left   = `${g.x * 100}%`;
        canvas.style.top    = `${g.y * 100}%`;
        canvas.style.width  = `${g.w * 100}%`;
        canvas.style.height = `${g.h * 100}%`;
      }
      // Keep the resize handle at the bottom-right corner
      const rHandle = area.querySelector(`.resize-handle[data-gauge-idx="${_drag.gaugeIdx}"]`);
      if (rHandle) {
        rHandle.style.left = `${(g.x + g.w) * 100}%`;
        rHandle.style.top  = `${(g.y + g.h) * 100}%`;
      }
      updatePropPanel();
    });

    document.addEventListener('mouseup', e => {
      if (!_drag) return;
      _drag = null;
      _clearGuides();
      rebuildGaugeCanvases();   // re-render at correct size after resize
      saveLayout();
    });
  }

  // ── Selection ───────────────────────────────────────────────────────────────
  function selectGauge(idx) {
    _selected = idx;
    rebuildGaugeCanvases();
    updatePropPanel();
  }

  // ── Channel-specific property HTML ──────────────────────────────────────────

  const _INFO_FIELDS_ALL = [
    { key: 'track',    label: 'Track' },
    { key: 'datetime', label: 'Date / Time' },
    { key: 'vehicle',  label: 'Vehicle' },
    { key: 'session',  label: 'Session' },
    { key: 'weather',  label: 'Weather' },
    { key: 'wind',     label: 'Wind' },
  ];

  function _buildChannelProps(g) {
    if (g.channel === 'info') {
      const sel = g.selected_fields || _channelDefaults('info').selected_fields || [];
      const ov  = g.info_overrides  || {};
      const align = g.text_align || 'left';
      return `
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">Fields &amp; Overrides</div>
          <div class="form-row" style="margin-bottom:6px;">
            <span class="form-label">对齐</span>
            <select id="info-text-align" style="width:110px;font-size:10px;">
              <option value="left" ${align==='left'?'selected':''}>左对齐</option>
              <option value="center" ${align==='center'?'selected':''}>中对齐</option>
              <option value="right" ${align==='right'?'selected':''}>右对齐</option>
            </select>
          </div>
          ${_INFO_FIELDS_ALL.map(f => `
            <div style="display:flex;align-items:center;gap:5px;margin-bottom:5px;">
              <input type="checkbox" class="info-field-chk" data-field="${f.key}"
                     ${sel.includes(f.key) ? 'checked' : ''}
                     style="flex-shrink:0;margin:0;">
              <span style="font-size:10px;color:var(--text2);width:65px;flex-shrink:0">${f.label}</span>
              <input type="text" class="info-ov-input" data-field="${f.key}"
                     value="${_esc(ov[f.key] || '')}" placeholder="from session"
                     style="flex:1;font-size:9px;font-family:var(--mono);min-width:0;">
            </div>
          `).join('')}
        </div>`;
    }

    if (g.channel === 'lap_info') {
      const LAP_INFO_ROWS = [
        { key: 'lap',     label: 'Lap #' },
        { key: 'best',    label: 'Best' },
        { key: 'current', label: 'Current' },
        { key: 'delta',   label: 'Delta' },
      ];
      const sel = g.selected_fields || _channelDefaults('lap_info').selected_fields || [];
      const align = g.text_align || 'split';
      const bestMode = g.best_mode || _channelDefaults('lap_info').best_mode || 'so_far';
      return `
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">显示行</div>
          <div class="form-row" style="margin-bottom:6px;">
            <span class="form-label">对齐</span>
            <select id="lapinfo-text-align" style="width:110px;font-size:10px;">
              <option value="split" ${align==='split'?'selected':''}>分列(默认)</option>
              <option value="left" ${align==='left'?'selected':''}>左对齐</option>
              <option value="center" ${align==='center'?'selected':''}>中对齐</option>
              <option value="right" ${align==='right'?'selected':''}>右对齐</option>
            </select>
          </div>
          <div class="form-row" style="margin-bottom:6px;">
            <span class="form-label">BEST 显示</span>
            <select id="lapinfo-best-mode" style="width:150px;font-size:10px;">
              <option value="so_far" ${bestMode==='so_far'?'selected':''}>本节到当前圈为止最快</option>
              <option value="session" ${bestMode==='session'?'selected':''}>本节最快圈</option>
            </select>
          </div>
          ${LAP_INFO_ROWS.map(f => `
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">
              <input type="checkbox" class="lapinfo-field-chk" data-field="${f.key}"
                     ${sel.includes(f.key) ? 'checked' : ''}
                     style="flex-shrink:0;margin:0;">
              <span style="font-size:10px;color:var(--text2);">${f.label}</span>
            </div>
          `).join('')}
        </div>`;
    }

    if (g.channel === 'image') {
      const path    = g.image_path || '';
      const opacity = Math.round((g.opacity ?? 1.0) * 100);
      const fit     = g.fit || 'contain';
      return `
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">图片文件</div>
          <div style="display:flex;gap:4px;align-items:center;">
            <input type="text" id="img-path-inp" class="input-field"
                   value="${_esc(path)}" placeholder="C:\\path\\to\\logo.png"
                   style="flex:1;font-size:9px;font-family:var(--mono);min-width:0;">
            <button class="btn btn-sm" id="img-browse-btn" style="flex-shrink:0;">浏览</button>
          </div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
            <label style="font-size:9px;color:var(--text2);white-space:nowrap;">Opacity</label>
            <input type="range" id="img-opacity" min="0" max="100" value="${opacity}"
                   style="flex:1;">
            <span id="img-opacity-val" style="font-size:9px;color:var(--text);width:28px;text-align:right;">${opacity}%</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:6px;">
            <label style="font-size:9px;color:var(--text2);white-space:nowrap;">Fit</label>
            <select id="img-fit" style="flex:1;font-size:10px;">
              <option value="contain" ${fit==='contain'?'selected':''}>Contain (letterbox)</option>
              <option value="cover"   ${fit==='cover'  ?'selected':''}>Cover (crop)</option>
              <option value="stretch" ${fit==='stretch'?'selected':''}>Stretch</option>
            </select>
          </div>
          <div style="font-size:9px;color:var(--text3);margin-top:5px;">PNG with alpha recommended.</div>
        </div>`;
    }

    if (g.channel === 'map') {
      const osmEnabled = g.track_map_enabled !== false;
      const radius     = g.zoom_radius_m ?? 150;
      const showRef    = g.show_ref !== false;
      const rotateDeg  = g.map_rotate_deg ?? 0;
      const mirrorX    = g.map_mirror_x === true;
      const mirrorY    = g.map_mirror_y === true;
      const zoomedHtml = g.style === 'Zoomed' ? `
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">Zoom Settings</div>
          <div class="form-row">
            <span class="form-label">Radius&nbsp;(m)</span>
            <input type="number" id="map-radius" value="${radius}"
                   min="10" max="5000" step="10"
                   style="width:70px;font-variant-numeric:tabular-nums;">
          </div>
          <div class="form-row" style="margin-top:6px;">
            <span class="form-label">Show ref lap</span>
            <input type="checkbox" id="map-show-ref" ${showRef ? 'checked' : ''}>
          </div>
          <div style="font-size:9px;color:var(--text3);margin-top:5px;">
            Reference lap trace shown in purple.<br>
            Ref GPS loaded when a ref lap is set.
          </div>
        </div>` : '';
      return `
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">Circuit Outline (OSM)</div>
          <div class="form-row">
            <span class="form-label">Show outline</span>
            <input type="checkbox" id="map-osm-enabled" ${osmEnabled ? 'checked' : ''}>
          </div>
          <button class="btn btn-sm" id="map-osm-configure"
                  style="width:100%;margin-top:6px;font-size:10px;">
            配置赛道地图
          </button>
          <div id="map-osm-status" style="font-size:9px;color:var(--text3);margin-top:5px;line-height:1.4;"></div>
          <div id="map-osm-picker" style="display:none;margin-top:8px;"></div>
        </div>
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:8px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">视图变换</div>
          <div class="form-row">
            <span class="form-label">旋转 (°)</span>
            <input type="number" id="map-rotate-deg" value="${rotateDeg}" min="-180" max="180" step="1"
                   style="width:70px;font-variant-numeric:tabular-nums;">
          </div>
          <div class="form-row" style="margin-top:6px;">
            <span class="form-label">左右镜像</span>
            <input type="checkbox" id="map-mirror-x" ${mirrorX ? 'checked' : ''}>
          </div>
          <div class="form-row" style="margin-top:6px;">
            <span class="form-label">上下镜像</span>
            <input type="checkbox" id="map-mirror-y" ${mirrorY ? 'checked' : ''}>
          </div>
        </div>
        ${zoomedHtml}`;
    }

    if (g.channel === 'multi') {
      const keys = g.multi_channels || _channelDefaults('multi').multi_channels || [];
      const opts = _MULTI_CHANNEL_OPTS.map(o =>
        `<option value="${o.value}">${o.label}</option>`).join('');
      return `
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">通道</div>
          <div id="multi-ch-list">
            ${keys.map((ch, i) => {
              const lbl = _MULTI_CHANNEL_OPTS.find(o => o.value === ch)?.label || ch;
              return `<div style="display:flex;align-items:center;gap:4px;margin-bottom:4px;">
                <div style="width:10px;height:10px;border-radius:2px;flex-shrink:0;
                            background:${_gaugeColourAt(i)}"></div>
                <span style="flex:1;font-size:10px;color:var(--text)">${lbl}</span>
                <button class="btn btn-sm multi-rm-btn" data-mch="${ch}"
                        style="padding:1px 6px;color:var(--err);border-color:var(--err);">✕</button>
              </div>`;
            }).join('')}
          </div>
          <div style="display:flex;gap:4px;margin-top:6px;">
            <select id="multi-add-sel" style="flex:1;font-size:10px;">${opts}</select>
            <button class="btn btn-sm" id="multi-add-btn" style="flex-shrink:0;">+ 添加</button>
          </div>
        </div>`;
    }

    if (g.channel === 'delta_time' && g.style === 'Delta Bar') {
      const { fullScale: fs, curve: ex } = _deltaBarOpts(g);
      return `
        <div style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
          <div style="font-size:9px;color:var(--text3);margin-bottom:6px;
                      text-transform:uppercase;letter-spacing:0.04em;">Delta Bar</div>
          <div class="form-row">
            <span class="form-label" title="|delta| 达到该秒数时条占满半边">满幅 (s)</span>
            <input type="number" id="dbar-fullscale" value="${fs}"
                   min="0.05" max="10" step="0.05"
                   style="width:70px;font-variant-numeric:tabular-nums;">
          </div>
          <div class="form-row" style="margin-top:6px;">
            <span class="form-label" title="越小越“夸张”小 delta；越大越接近线性">曲线</span>
            <input type="number" id="dbar-curve" value="${ex}"
                   min="0.15" max="1" step="0.05"
                   style="width:70px;font-variant-numeric:tabular-nums;">
          </div>
          <div style="font-size:9px;color:var(--text3);margin-top:5px;line-height:1.35;">
            参考 iRacing 类 HUD：用更小满幅 + 非线性放大，让 ±0.2s 级别也更显眼。
          </div>
        </div>`;
    }

    return '';
  }

  function _bindChannelPropEvents(panel, g) {
    if (g.channel === 'delta_time' && g.style === 'Delta Bar') {
      const fsInp = panel.querySelector('#dbar-fullscale');
      const exInp = panel.querySelector('#dbar-curve');
      const _apply = () => {
        const nfs = parseFloat(fsInp?.value);
        const nex = parseFloat(exInp?.value);
        if (Number.isFinite(nfs)) g.delta_bar_full_scale = Math.max(0.05, Math.min(10, nfs));
        if (Number.isFinite(nex)) g.delta_bar_curve = Math.max(0.15, Math.min(1, nex));
        rebuildGaugeCanvases();
        saveLayout();
      };
      fsInp?.addEventListener('change', _apply);
      exInp?.addEventListener('change', _apply);
    }
    if (g.channel === 'image') {
      const inp       = panel.querySelector('#img-path-inp');
      const browseBtn = panel.querySelector('#img-browse-btn');
      const opSlider  = panel.querySelector('#img-opacity');
      const opVal     = panel.querySelector('#img-opacity-val');
      const fitSel    = panel.querySelector('#img-fit');

      const _applyPath = (path) => {
        // Clear cached image so the new path loads fresh
        if (_livePort && g.image_path) {
          const oldUrl = `http://127.0.0.1:${_livePort}/?f=${encodeURIComponent(g.image_path)}`;
          GaugeImage.clearCache(oldUrl);
        }
        g.image_path = path;
        rebuildGaugeCanvases();
        saveLayout();
      };

      inp?.addEventListener('change', () => _applyPath(inp.value.trim()));

      browseBtn?.addEventListener('click', async () => {
        const path = await API.openFileDialog(
          ['图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)']
        );
        if (path) { inp.value = path; _applyPath(path); }
      });

      opSlider?.addEventListener('input', () => {
        const pct = parseInt(opSlider.value);
        if (opVal) opVal.textContent = pct + '%';
        g.opacity = pct / 100;
        rebuildGaugeCanvases();
        saveLayout();
      });

      fitSel?.addEventListener('change', () => {
        g.fit = fitSel.value;
        rebuildGaugeCanvases();
        saveLayout();
      });
    }

    if (g.channel === 'map') {
      panel.querySelector('#map-osm-enabled')?.addEventListener('change', e => {
        g.track_map_enabled = e.target.checked;
        rebuildGaugeCanvases();
        saveLayout();
      });

      panel.querySelector('#map-osm-configure')?.addEventListener('click', async () => {
        const picker   = panel.querySelector('#map-osm-picker');
        const statusEl = panel.querySelector('#map-osm-status');
        if (!picker) return;

        const csvPath = _liveSession?.csv_path;
        if (!csvPath) {
          if (statusEl) statusEl.textContent = '请先在编辑器中加载节。';
          return;
        }

        // Toggle: if already open, close
        if (picker.style.display !== 'none') {
          picker.style.display = 'none';
          return;
        }

        picker.style.display = 'block';
        picker.innerHTML = '<div style="font-size:9px;color:var(--text3);">正在搜索 OSM…</div>';

        try {
          const result = await API.getTrackMapCandidates(csvPath);
          const { candidates, selected_osm_id, auto_osm_id, track_key } = result;

          if (!candidates.length) {
            picker.innerHTML = `<div style="font-size:9px;color:var(--text3);">
              在 OpenStreetMap 附近未找到赛车赛道。<br>
              请确认赛道包含 <em>leisure=track + sport=motor_racing</em> 标签。
            </div>`;
            if (statusEl) statusEl.textContent = '附近未找到 OSM 赛道。';
            return;
          }

          const currentId = selected_osm_id || auto_osm_id;
          const rows = candidates.map(c => {
            const isSelected = c.osm_id === currentId;
            const isAuto     = c.osm_id === auto_osm_id && !selected_osm_id;
            const distKm     = (c.centroid_dist_m / 1000).toFixed(1);
            const badge      = isAuto && !selected_osm_id
              ? '<span style="background:#2255aa;color:#fff;border-radius:2px;padding:0 3px;font-size:8px;margin-left:4px;">auto</span>'
              : '';
            return `<div class="osm-cand-row" data-osm-id="${c.osm_id}"
                style="padding:5px 7px;border-radius:4px;cursor:pointer;margin-bottom:2px;font-size:10px;
                       background:${isSelected ? 'var(--acc)' : 'var(--bg2)'};
                       color:${isSelected ? '#fff' : 'var(--text)'};
                       border:1px solid ${isSelected ? 'var(--acc)' : 'var(--border)'};">
              ${_esc(c.name)}${badge}
              <span style="float:right;font-size:8px;opacity:0.6;">${distKm} km</span>
            </div>`;
          }).join('');

          const clearRow = selected_osm_id
            ? `<div class="osm-cand-row" data-osm-id=""
                style="padding:5px 7px;border-radius:4px;cursor:pointer;margin-bottom:2px;font-size:10px;
                       background:var(--bg2);color:var(--text3);border:1px solid var(--border);">
                自动检测（清除手动选择）
              </div>`
            : '';

          picker.innerHTML = `
            <div style="font-size:9px;color:var(--text3);margin-bottom:5px;">
              附近赛道列表 · 点击选择：
            </div>
            ${clearRow}${rows}`;

          picker.querySelectorAll('.osm-cand-row').forEach(row => {
            row.addEventListener('click', async () => {
              const osmId = row.dataset.osmId;
              await API.setTrackMapSelection(track_key, osmId);
              // Refresh preview geometry
              _trackMapGeometry = null;
              API.getTrackMapGeometry(csvPath).then(geom => {
                _trackMapGeometry = (geom && geom.lats && geom.lats.length) ? geom : null;
                rebuildGaugeCanvases();
              }).catch(() => {});
              // Update UI: re-open picker to show new selection
              const name = candidates.find(c => c.osm_id === osmId)?.name || '自动';
              if (statusEl) statusEl.textContent = osmId ? `当前使用：${name}` : '自动检测';
              picker.style.display = 'none';
              rebuildGaugeCanvases();
            });
          });

          // Show current selection in status
          if (statusEl) {
            const selName = candidates.find(c => c.osm_id === currentId)?.name;
            statusEl.textContent = selected_osm_id
              ? `当前使用：${selName || selected_osm_id}`
              : (auto_osm_id
                  ? `自动：${candidates.find(c => c.osm_id === auto_osm_id)?.name || auto_osm_id}`
                  : '附近未找到赛道。');
          }
        } catch (err) {
          picker.innerHTML = `<div style="font-size:9px;color:var(--err);">失败：${err.message || err}</div>`;
        }
      });

      panel.querySelector('#map-rotate-deg')?.addEventListener('change', e => {
        g.map_rotate_deg = Math.max(-180, Math.min(180, parseFloat(e.target.value) || 0));
        rebuildGaugeCanvases();
        saveLayout();
      });
      panel.querySelector('#map-mirror-x')?.addEventListener('change', e => {
        g.map_mirror_x = !!e.target.checked;
        rebuildGaugeCanvases();
        saveLayout();
      });
      panel.querySelector('#map-mirror-y')?.addEventListener('change', e => {
        g.map_mirror_y = !!e.target.checked;
        rebuildGaugeCanvases();
        saveLayout();
      });

      if (g.style === 'Zoomed') {
        panel.querySelector('#map-radius')?.addEventListener('change', e => {
          g.zoom_radius_m = Math.max(10, Math.min(5000, parseInt(e.target.value) || 150));
          rebuildGaugeCanvases();
          saveLayout();
        });
        panel.querySelector('#map-show-ref')?.addEventListener('change', e => {
          g.show_ref = e.target.checked;
          rebuildGaugeCanvases();
          saveLayout();
        });
      }
    }

    if (g.channel === 'lap_info') {
      panel.querySelector('#lapinfo-text-align')?.addEventListener('change', e => {
        g.text_align = e.target.value;
        rebuildGaugeCanvases();
        saveLayout();
      });
      panel.querySelector('#lapinfo-best-mode')?.addEventListener('change', e => {
        g.best_mode = e.target.value === 'session' ? 'session' : 'so_far';
        rebuildGaugeCanvases();
        saveLayout();
      });
      panel.querySelectorAll('.lapinfo-field-chk').forEach(chk => {
        chk.addEventListener('change', () => {
          const checked = [...panel.querySelectorAll('.lapinfo-field-chk')]
            .filter(c => c.checked).map(c => c.dataset.field);
          g.selected_fields = checked;
          rebuildGaugeCanvases();
          saveLayout();
        });
      });
    }

    if (g.channel === 'info') {
      panel.querySelector('#info-text-align')?.addEventListener('change', e => {
        g.text_align = e.target.value;
        rebuildGaugeCanvases();
        saveLayout();
      });
      panel.querySelectorAll('.info-field-chk').forEach(chk => {
        chk.addEventListener('change', () => {
          const checked = [...panel.querySelectorAll('.info-field-chk')]
            .filter(c => c.checked).map(c => c.dataset.field);
          g.selected_fields = checked;
          rebuildGaugeCanvases();
          saveLayout();
        });
      });

      panel.querySelectorAll('.info-ov-input').forEach(inp => {
        inp.addEventListener('change', () => {
          if (!g.info_overrides) g.info_overrides = {};
          const val = inp.value.trim();
          if (val) g.info_overrides[inp.dataset.field] = val;
          else     delete g.info_overrides[inp.dataset.field];
          rebuildGaugeCanvases();
          saveLayout();
        });
      });
    }

    if (g.channel === 'multi') {
      const _rebuildMulti = () => {
        // Rebuild just the channel list DOM (no full prop panel refresh)
        const listEl = panel.querySelector('#multi-ch-list');
        if (!listEl) return;
        const keys = g.multi_channels || [];
        listEl.innerHTML = keys.map((ch, i) => {
          const lbl = _MULTI_CHANNEL_OPTS.find(o => o.value === ch)?.label || ch;
          return `<div style="display:flex;align-items:center;gap:4px;margin-bottom:4px;">
            <div style="width:10px;height:10px;border-radius:2px;flex-shrink:0;
                        background:${_gaugeColourAt(i)}"></div>
            <span style="flex:1;font-size:10px;color:var(--text)">${lbl}</span>
            <button class="btn btn-sm multi-rm-btn" data-mch="${ch}"
                    style="padding:1px 6px;color:var(--err);border-color:var(--err);">✕</button>
          </div>`;
        }).join('');
        listEl.querySelectorAll('.multi-rm-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const ch = btn.dataset.mch;
            g.multi_channels = (g.multi_channels || []).filter(c => c !== ch);
            _rebuildMulti();
            rebuildGaugeCanvases();
            saveLayout();
          });
        });
      };

      // Wire existing remove buttons
      panel.querySelectorAll('.multi-rm-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const ch = btn.dataset.mch;
          g.multi_channels = (g.multi_channels || []).filter(c => c !== ch);
          _rebuildMulti();
          rebuildGaugeCanvases();
          saveLayout();
        });
      });

      panel.querySelector('#multi-add-btn')?.addEventListener('click', () => {
        const sel = panel.querySelector('#multi-add-sel');
        const ch  = sel?.value;
        if (!ch) return;
        if (!g.multi_channels) g.multi_channels = [];
        if (!g.multi_channels.includes(ch)) {
          g.multi_channels.push(ch);
          _rebuildMulti();
          rebuildGaugeCanvases();
          saveLayout();
        }
      });
    }
  }

  // ── Properties panel ────────────────────────────────────────────────────────
  function updatePropPanel() {
    const panel = _container?.querySelector('#prop-panel');
    if (!panel) return;

    if (_selected === null || !_layout?.gauges[_selected]) {
      panel.innerHTML = `<div style="color:var(--text3); font-size:11px; padding:12px">
        请选择一个仪表以编辑属性。</div>`;
      return;
    }

    const g = _layout.gauges[_selected];
    const styles = _stylesForChannel(g.channel);
    const styleOptions = styles.map(s =>
      `<option value="${s}" ${s === g.style ? 'selected' : ''}>${s}</option>`
    ).join('');

    panel.innerHTML = `
      <div style="padding:12px; display:flex; flex-direction:column; gap:8px;">
        <div style="font-size:10px; font-weight:700; color:var(--text2);
                    text-transform:uppercase; letter-spacing:0.04em; margin-bottom:4px">
          仪表属性
        </div>

        <div class="form-row">
          <span class="form-label">通道</span>
          <select id="prop-channel" style="flex:1">
            ${_ALL_CHANNELS.map(c => `<option value="${c.value}" ${c.value===g.channel?'selected':''}>${c.label}</option>`).join('')}
          </select>
        </div>

        <div class="form-row">
          <span class="form-label">样式</span>
          <select id="prop-style" style="flex:1">${styleOptions}</select>
        </div>

        <div class="form-row">
          <span class="form-label">可见</span>
          <input type="checkbox" id="prop-visible" ${g.visible !== false ? 'checked' : ''}>
        </div>

        <div style="border-top:1px solid var(--border); padding-top:8px; margin-top:4px;">
          <div style="font-size:9px; color:var(--text3); margin-bottom:6px;">位置（归一化 0–1）</div>
          <div class="form-row">
            <span class="form-label" style="min-width:20px">X</span>
            <input type="number" id="prop-x" value="${g.x.toFixed(3)}" step="0.01" style="width:70px">
            <span class="form-label" style="min-width:20px">Y</span>
            <input type="number" id="prop-y" value="${g.y.toFixed(3)}" step="0.01" style="width:70px">
          </div>
          <div class="form-row">
            <span class="form-label" style="min-width:20px">W</span>
            <input type="number" id="prop-w" value="${g.w.toFixed(3)}" step="0.01" style="width:70px">
            <span class="form-label" style="min-width:20px">H</span>
            <input type="number" id="prop-h" value="${g.h.toFixed(3)}" step="0.01" style="width:70px">
          </div>
        </div>

        ${_buildChannelProps(g)}

        <button class="btn btn-sm" id="prop-delete"
                style="margin-top:8px; border-color:var(--err); color:var(--err);">
          删除仪表
        </button>
      </div>`;

    // Wire up change handlers
    panel.querySelector('#prop-channel').addEventListener('change', e => {
      g.channel = e.target.value;
      const newStyles = _stylesForChannel(g.channel);
      g.style = newStyles[0];
      // Apply per-channel defaults if not already set
      const defs = _channelDefaults(g.channel);
      for (const [k, v] of Object.entries(defs)) {
        if (g[k] === undefined) g[k] = v;
      }
      selectGauge(_selected);  // refresh (updates style dropdown too)
      saveLayout();
    });

    panel.querySelector('#prop-style').addEventListener('change', e => {
      g.style = e.target.value;
      if (g.channel === 'delta_time' && g.style === 'Delta Bar') {
        if (g.delta_bar_full_scale === undefined) g.delta_bar_full_scale = 1.0;
        if (g.delta_bar_curve === undefined) g.delta_bar_curve = 0.45;
      }
      updatePropPanel();   // refresh channel-specific props (e.g. Zoomed map settings)
      rebuildGaugeCanvases();
      saveLayout();
    });

    panel.querySelector('#prop-visible').addEventListener('change', e => {
      g.visible = e.target.checked;
      rebuildGaugeCanvases();
      saveLayout();
    });

    for (const key of ['x', 'y', 'w', 'h']) {
      panel.querySelector(`#prop-${key}`).addEventListener('change', e => {
        g[key] = Math.max(0, Math.min(1, parseFloat(e.target.value) || 0));
        rebuildGaugeCanvases();
        saveLayout();
      });
    }

    panel.querySelector('#prop-delete').addEventListener('click', () => {
      _layout.gauges.splice(_selected, 1);
      _selected = null;
      rebuildGaugeCanvases();
      rebuildGaugeList();
      updatePropPanel();
      saveLayout();
    });

    _bindChannelPropEvents(panel, g);
  }

  // ── Gauge list (left sidebar) ───────────────────────────────────────────────
  function rebuildGaugeList() {
    const list = _container?.querySelector('#gauge-list');
    if (!list || !_layout) return;
    _ensureLayoutGauges();

    list.innerHTML = _layout.gauges.map((g, idx) => {
      const ch      = _ALL_CHANNELS.find(c => c.value === g.channel)?.label || g.channel;
      const col     = _gaugeColourAt(idx);
      const visible = g.visible !== false;
      return `
        <div class="gauge-list-item ${idx === _selected ? 'selected' : ''}"
             data-idx="${idx}"
             style="display:flex; align-items:center; gap:8px; padding:7px 12px;
                    cursor:pointer; border-bottom:1px solid var(--border);
                    ${idx === _selected ? 'background:rgba(79,142,247,0.12)' : ''}">
          <div style="width:8px; height:8px; border-radius:50%;
                      background:${col}; flex-shrink:0;"></div>
          <div style="flex:1; min-width:0;">
            <div style="font-size:11px; color:var(--text); white-space:nowrap;
                        overflow:hidden; text-overflow:ellipsis;
                        ${visible ? '' : 'opacity:0.4;'}">${ch}</div>
            <div style="font-size:9px; color:var(--text3)">${g.style}</div>
          </div>
          <button class="vis-toggle" data-vis-idx="${idx}"
                  title="${visible ? '隐藏仪表' : '显示仪表'}"
                  style="background:none;border:none;cursor:pointer;
                         font-size:13px;padding:2px 4px;color:var(--text2);
                         flex-shrink:0;line-height:1;">${visible ? '●' : '○'}</button>
        </div>`;
    }).join('');

    list.querySelectorAll('.gauge-list-item').forEach(el => {
      el.addEventListener('click', e => {
        if (e.target.closest('.vis-toggle')) return; // handled separately
        selectGauge(parseInt(el.dataset.idx));
      });
    });

    list.querySelectorAll('.vis-toggle').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const idx = parseInt(btn.dataset.visIdx);
        if (!isNaN(idx) && _layout.gauges[idx]) {
          _layout.gauges[idx].visible = !(_layout.gauges[idx].visible !== false);
          rebuildGaugeList();
          rebuildGaugeCanvases();
          saveLayout();
        }
      });
    });
  }

  // ── Add gauge ───────────────────────────────────────────────────────────────
  function _channelDefaults(channel) {
    return _channelDefaultsMap[channel] ? { ..._channelDefaultsMap[channel] } : {};
  }

  function addGauge() {
    _ensureLayoutGauges();
    const speedStyles = _stylesForChannel('speed');
    const newG = {
      channel: 'speed',
      style:   speedStyles[0] || 'Dial',
      visible: true,
      x: 0.01,
      y: 0.74,
      w: 0.13,
      h: 0.23,
    };
    _layout.gauges.push(newG);
    selectGauge(_layout.gauges.length - 1);
    rebuildGaugeList();
    saveLayout();
  }

  // ── Preset management ────────────────────────────────────────────────────────
  async function loadPresetList() {
    try {
      _presets = await API.listPresets();
      rebuildPresetSelector();
    } catch (err) {
      console.warn('Failed to load preset list:', err);
    }
  }

  function rebuildPresetSelector() {
    const sel = _container?.querySelector('#preset-select');
    if (!sel) return;
    const cur = _layout?.active_preset || '';
    sel.innerHTML = `<option value="">— 无预设 —</option>` +
      _presets.map(p => `<option value="${p}" ${p===cur?'selected':''}>${p}</option>`).join('');
  }

  async function saveAsPreset() {
    const name = prompt('请输入预设名称：');
    if (!name) return;
    await API.saveOverlayAs(name, _layout);
    _layout.active_preset = name;
    await loadPresetList();
  }

  // ── Theme selector ───────────────────────────────────────────────────────────
  function rebuildThemeSelector() {
    const sel = _container?.querySelector('#theme-select');
    if (!sel || !_layout) return;
    const names = _themeNames.includes(_layout.theme) ? _themeNames : [..._themeNames, _layout.theme];
    sel.innerHTML = names.map(t => `<option value="${t}">${t}</option>`).join('');
    sel.value = _layout.theme;
  }

  // ── Save layout ──────────────────────────────────────────────────────────────
  async function saveLayout() {
    try {
      await API.saveOverlay(_layout);
    } catch (err) {
      console.error('saveLayout failed:', err);
    }
  }

  // ── Mount ────────────────────────────────────────────────────────────────────
  async function mount(container) {
    const myGen = _mountGen;  // capture generation — if unmount runs before we finish, myGen !== _mountGen
    _container = container;
    _selected  = null;

    // Resolve video URL early (same pattern as Data tab — presentation only).
    const prevSession = State.get('previewSession');
    // Only fetch port once — the server never changes address, and re-calling on fast
    // re-navigation can fail/reject and zero out _livePort, hiding the video element.
    if (!_livePort) _livePort = await API.getVideoServerPort().catch(() => 0);
    if (_mountGen !== myGen) return;  // navigated away while awaiting port

    const catEarly = await API.getEditorCatalog().catch(() => ({}));
    if (_mountGen !== myGen) return;
    _applyEditorCatalog(catEarly);

    // Fast-remount path: if the same session is already loaded in memory, skip all
    // Python API calls (getOverlay, listPresets, getSessionMeta, getLaps, loadLapHistory).
    // Just rebuild the DOM from preserved state and restart the RAF.
    const sameSession = !!(
      _livePoints?.length > 0 &&
      _liveSession?.csv_path === prevSession?.csv_path &&
      _layout
    );

    // Editor uses saved offset only; ignore Data-tab preview temp offsets.

    const vp = prevSession?.video_paths?.[0] ?? null;
    const videoSrc = (vp && _livePort)
      ? `http://127.0.0.1:${_livePort}/?f=${encodeURIComponent(vp)}`
      : '';

    const hasVideo   = !!videoSrc;
    const hintText   = hasVideo ? '' : '请先在“数据”页选择节，然后点击“打开到叠加编辑”。';
    const videoStyle = `position:absolute;inset:0;width:100%;height:100%;object-fit:contain;z-index:0;opacity:${hasVideo ? '0.9' : '0'}`;

    // Use persisted video dimensions for the initial aspect-ratio so there is no
    // 16/9 flash on re-mount even when the browser returns metadata from cache.
    const initAspect = (prevSession?.video_w && prevSession?.video_h)
      ? `${prevSession.video_w} / ${prevSession.video_h}`
      : '16 / 9';

    container.innerHTML = `
      <div style="display:flex; flex-direction:column; height:100vh; overflow:hidden;">

        <!-- Toolbar -->
        <div style="padding:8px 16px; border-bottom:1px solid var(--border);
                    display:flex; align-items:center; gap:8px; flex-shrink:0;
                    background:var(--sidebar);">
          <span style="font-size:12px; font-weight:700; color:var(--text)">叠加编辑</span>

          <!-- Lap selector -->
          <div style="display:flex;align-items:center;gap:3px;margin-left:8px;flex-shrink:0;">
            <button class="btn btn-sm" id="lap-prev" title="上一圈"
                    style="padding:2px 8px;" disabled>◀</button>
            <select id="lap-sel" style="font-size:10px;min-width:120px;">
              <option value="">— 未加载节 —</option>
            </select>
            <button class="btn btn-sm" id="lap-next" title="下一圈"
                    style="padding:2px 8px;" disabled>▶</button>
          </div>

          <label for="overlay-scope" style="font-size:10px;color:var(--text2);margin-left:6px;">导出范围</label>
          <select id="overlay-scope" title="导出范围（仅影响导出，不影响预览）"
                  style="font-size:10px;flex-shrink:0;min-width:90px;">
            <option value="selected_lap">当前圈</option>
            <option value="fastest">最快圈</option>
            <option value="all_laps">全部圈（每圈一个文件）</option>
            <option value="all_laps_data_end">全部圈（单文件：从视频开头到数据结尾）</option>
            <option value="full">完整节</option>
          </select>
          <button class="btn btn-sm" id="stage-export-btn"
                  title="将当前圈加入导出队列"
                  style="flex-shrink:0;border-color:var(--ok);color:var(--ok);">+ Export</button>

          <div style="flex:1"></div>
          <label style="font-size:10px; color:var(--text2)">主题</label>
          <select id="theme-select" style="font-size:10px;">
            <option value="Dark">Dark</option>
            <option value="Light">Light</option>
            <option value="Colorful">Colorful</option>
            <option value="Monochrome">Monochrome</option>
            <option value="Minimal">Minimal</option>
          </select>
          <select id="preset-select" style="font-size:10px; max-width:120px">
            <option value="">— 无预设 —</option>
          </select>
          <button class="btn btn-sm" id="save-preset-btn">另存为…</button>
          <button class="btn btn-sm btn-accent" id="save-layout-btn">保存</button>
        </div>

        <!-- Main content row -->
        <div style="flex:1; display:flex; overflow:hidden;">

          <!-- Left: video preview + scrub strip -->
          <div style="flex:1; display:flex; flex-direction:column; overflow:hidden; background:var(--bg);">

            <div style="flex:1; display:flex; align-items:center; justify-content:center;
                        padding:16px; overflow:hidden;">
              <div id="preview-area"
                   style="position:relative; aspect-ratio:${initAspect}; width:100%; max-width:100%; max-height:100%;
                          background:#111827; border:1px solid var(--border);
                          border-radius:4px; overflow:hidden;">
                <video id="preview-video" preload="auto"
                       ${videoSrc ? `src="${videoSrc}"` : ''}
                       style="${videoStyle}"></video>
                <div id="preview-hint" style="position:absolute;inset:0;display:flex;
                     align-items:center;justify-content:center;pointer-events:none;z-index:1;
                     ${hasVideo ? 'display:none' : ''}">
                  <span style="font-size:12px;color:rgba(255,255,255,0.2);user-select:none;text-align:center;padding:16px">
                    ${hintText}
                  </span>
                </div>
              </div>
            </div>
            <!-- Scrub strip (outside video, top edge touches video bottom) -->
            <div id="live-strip" style="padding:10px 12px 8px 12px; margin-top:0;
                  border-top:0; background:var(--sidebar);
                  display:flex; align-items:center; gap:10px;">
              <button id="live-play" class="btn btn-sm"
                      style="width:44px;height:34px;padding:0;flex-shrink:0;font-size:16px;line-height:1;">▶</button>
              <span id="live-time" style="font-size:12px;color:var(--acc2);
                    font-variant-numeric:tabular-nums;width:74px;flex-shrink:0">0:00.000</span>
              <input type="range" id="live-scrub" min="0" max="1000" step="1" value="0"
                     style="flex:1;accent-color:var(--acc);cursor:pointer;height:20px;">
              <span id="live-label" style="font-size:10px;color:var(--text2);flex-shrink:0;
                    max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                ${hasVideo ? '正在加载遥测数据…' : '未加载节'}
              </span>
            </div>
          </div>

          <!-- Right: gauge list + properties -->
          <div style="width:280px;min-width:240px;display:flex;flex-direction:column;
                      border-left:1px solid var(--border);background:var(--sidebar);overflow:hidden;">
            <div style="padding:10px 12px;border-bottom:1px solid var(--border);flex-shrink:0;">
              <button class="btn btn-sm btn-accent" id="add-gauge-btn" style="width:100%">+ 添加仪表</button>
            </div>
            <div id="gauge-list" style="flex:1;overflow-y:auto;min-height:80px;"></div>
            <div id="prop-panel" style="border-top:1px solid var(--border);overflow-y:auto;flex-shrink:0;max-height:55%;">
              <div style="color:var(--text3);font-size:11px;padding:12px">请选择一个仪表以编辑属性。</div>
            </div>
            <!-- Reference lap -->
            <div style="border-top:1px solid var(--border);padding:10px 12px;flex-shrink:0;">
              <div style="font-size:9px;font-weight:700;color:var(--text2);text-transform:uppercase;
                          letter-spacing:0.05em;margin-bottom:6px;">参考圈</div>
              <select id="ref-mode-sel" style="width:100%;font-size:11px;">
                <option value="none">无</option>
                <option value="session_best">本节最快圈</option>
                <option value="session_best_so_far">本节到当前圈为止最快圈</option>
                <option value="personal_best">个人历史最快</option>
                <option value="day_best">当日最快圈</option>
                <option value="manual">手动选择…</option>
              </select>
              <div id="ref-manual-picker" style="display:none;margin-top:8px;max-height:200px;
                   overflow-y:auto;border:1px solid var(--border);border-radius:3px;padding:4px;">
                <div id="ref-picker-content" style="font-size:10px;color:var(--text3)">加载中…</div>
              </div>
              <div id="ref-current-info" style="margin-top:6px;font-size:10px;color:var(--text3)">参考圈：未设置</div>
            </div>
          </div>

        </div>
      </div>`;

    // Load overlay layout + presets — skipped on fast remount (already in memory)
    if (!sameSession) {
      try {
        _layout = await API.getOverlay();
        _layout.gauges = Array.isArray(_layout.gauges) ? _layout.gauges : [];
      } catch (_) {
        _layout = { is_bike: false, theme: 'Dark', gauges: [] };
      }
      if (_mountGen !== myGen) return;

      await loadPresetList();
      if (_mountGen !== myGen) return;
    } else {
      rebuildPresetSelector();  // repopulate dropdown from cached _presets
    }
    rebuildThemeSelector();
    rebuildGaugeList();
    rebuildGaugeCanvases();
    setupMouseEvents();

    // Restore ref_mode from layout
    const refSel    = container.querySelector('#ref-mode-sel');
    const refPicker = container.querySelector('#ref-manual-picker');
    const refInfoEl = container.querySelector('#ref-current-info');

    function updateRefInfoText() {
      if (!refInfoEl) return;
      const mode = _layout?.ref_mode || DEFAULT_REF_MODE;
      const csv = _layout?.ref_lap_csv_path || _liveSession?.csv_path || '';
      const lapNum = _layout?.ref_lap_num || 0;
      const pts = _refLapPoints?.length || 0;
      const csvName = csv ? csv.replace(/\\/g, '/').split('/').pop() : '—';
      const modeName = ({
        none: '无',
        session_best: '本节最快圈',
        session_best_so_far: '本节到当前圈为止最快圈',
        personal_best: '个人历史最快',
        day_best: '当日最快圈',
        manual: '手动选择',
      })[mode] || mode;
      refInfoEl.textContent = `参考圈：${modeName} | ${csvName} | Lap ${lapNum || '—'} | 点数 ${pts}`;
    }

    function _fmtLapTime(secs) {
      if (!secs && secs !== 0) return '—';
      const m = Math.floor(secs / 60);
      return m + ':' + (secs % 60).toFixed(3).padStart(6, '0');
    }

    async function refreshManualPicker() {
      if (!refPicker) return;
      const isManual = _layout.ref_mode === 'manual';
      refPicker.style.display = isManual ? '' : 'none';
      if (!isManual) return;

      const content = container.querySelector('#ref-picker-content');
      if (content) content.textContent = '正在加载圈次…';

      const ps = State.get('previewSession');
      if (!ps?.csv_path) {
        if (content) content.textContent = '未加载节。';
        return;
      }

      try {
        const groups = await API.getLapsForRefPicker(ps.csv_path);
        if (!groups?.length) {
          if (content) content.textContent = '该赛道未找到可用圈次。';
          return;
        }

        const selCsv = _layout.ref_lap_csv_path || '';
        const selNum = _layout.ref_lap_num || 0;
        let   html   = '';

        for (const g of groups) {
          const date = g.date ? new Date(g.date).toLocaleDateString() : '—';
          html += `<div style="color:var(--text2);font-size:9px;margin:4px 0 2px;
                               font-weight:600;padding:0 2px">${date}</div>`;
          for (const lap of g.laps) {
            const sel = (g.csv_path === selCsv && lap.lap_num === selNum);
            const dur = _fmtLapTime(lap.duration);
            html += `<div class="ref-lap-row" data-csv="${lap.csv_path||g.csv_path}"
                          data-num="${lap.lap_num}"
                          style="padding:2px 4px;cursor:pointer;border-radius:2px;display:flex;
                                 justify-content:space-between;font-size:10px;
                                 background:${sel ? 'var(--acc)' : 'transparent'};
                                 color:${sel ? '#fff' : 'inherit'}">
                       <span>Lap ${lap.lap_num}${lap.is_best ? ' ★' : ''}</span>
                       <span style="color:${sel ? '#fff' : 'var(--acc2)'}">${dur}</span>
                     </div>`;
          }
        }

        if (content) {
          content.innerHTML = html;
          content.querySelectorAll('.ref-lap-row').forEach(row => {
            row.addEventListener('click', () => {
              _layout.ref_lap_csv_path = row.dataset.csv;
              _layout.ref_lap_num      = parseInt(row.dataset.num);
              saveLayout();
              refreshManualPicker();
              _loadRefLapIfNeeded().then(async () => {
                await _syncPreviewMapTracks();
                await _recomputeDeltaSeries();
                _updateDeltaAtFrame(_liveFrameIdx);
                _rerenderLive();
                updateRefInfoText();
              });
            });
          });
        }
      } catch (e) {
        if (content) content.textContent = `错误：${e}`;
      }
      updateRefInfoText();
    }

    if (refSel) {
      refSel.value = _layout.ref_mode || DEFAULT_REF_MODE;
      refSel.addEventListener('change', e => {
        _layout.ref_mode = e.target.value;
        saveLayout();
        refreshManualPicker();
        _loadRefLapIfNeeded().then(async () => {
          await _syncPreviewMapTracks();
          await _recomputeDeltaSeries();
          _updateDeltaAtFrame(_liveFrameIdx);
          _rerenderLive();
          updateRefInfoText();
        });
      });
      refreshManualPicker();
      updateRefInfoText();
    }

    container.querySelector('#add-gauge-btn').addEventListener('click', addGauge);

    container.querySelector('#save-layout-btn').addEventListener('click', async () => {
      await saveLayout();
      const btn = container.querySelector('#save-layout-btn');
      const orig = btn.textContent;
      btn.textContent = '已保存 ✓';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    });

    container.querySelector('#save-preset-btn').addEventListener('click', saveAsPreset);

    container.querySelector('#theme-select').addEventListener('change', e => {
      _layout.theme = e.target.value;
      rebuildGaugeCanvases();
      saveLayout();
    });

    container.querySelector('#preset-select').addEventListener('change', async e => {
      const name = e.target.value;
      if (!name) return;
      const presets = await API.getConfig().then(c => c.presets || {});
      if (presets[name]) {
        _layout = { ...presets[name], active_preset: name };
        _ensureLayoutGauges();
        _selected = null;
        rebuildThemeSelector();
        rebuildGaugeList();
        rebuildGaugeCanvases();
        updatePropPanel();
        const rs = container.querySelector('#ref-mode-sel');
        if (rs) rs.value = _layout.ref_mode || DEFAULT_REF_MODE;
        refreshManualPicker();
      }
    });

    // Lap selector
    container.querySelector('#lap-sel')?.addEventListener('change', e => {
      const idx = parseInt(e.target.value);
      if (!isNaN(idx)) switchLap(idx);
    });
    container.querySelector('#lap-prev')?.addEventListener('click', () => {
      switchLap(_selLapIdx - 1);
    });
    container.querySelector('#lap-next')?.addEventListener('click', () => {
      switchLap(_selLapIdx + 1);
    });

    // Scope selector → persist in State so Export tab reads it
    const scopeSel = container.querySelector('#overlay-scope');
    if (scopeSel) {
      scopeSel.value = State.get('exportScope') || 'full';
      scopeSel.addEventListener('change', e => State.set('exportScope', e.target.value));
    }

    // Stage for export
    container.querySelector('#stage-export-btn')?.addEventListener('click', _stageLapForExport);

    _resizeObserver = new ResizeObserver(() => rebuildGaugeCanvases());
    const area = container.querySelector('#preview-area');
    if (area) _resizeObserver.observe(area);

    // Wire video/scrub controls once, then load session data
    _wireVideoControls();

    if (sameSession) {
      // ── Fast path: same session already in memory — restore UI without Python calls ──
      _updateLapSelector();
      _rerenderLive();
      _startLiveRaf();
      // If track map geometry isn't loaded yet, try fetching it now
      if (!_trackMapGeometry) _fetchTrackMapGeometry(myGen);
    } else {
      // ── Slow path: new session or first mount — full async load ──
      if (prevSession) loadLiveSession(prevSession);
    }
  }

  function unmount() {
    _mountGen++;   // invalidate all in-flight async ops from the current mount
    if (_animFrame) { cancelAnimationFrame(_animFrame); _animFrame = null; }
    _stopLiveRaf();
    if (_resizeObserver) { _resizeObserver.disconnect(); _resizeObserver = null; }
    _mapSmoothedDot = null;
    _mapDotSessionKey = '';
    _container = null;
    // Live session state (_livePoints, _liveLaps, _liveSession, etc.) is intentionally
    // preserved so that returning to this page skips all Python round-trips.
  }

  Router.register('editor', { mount, unmount });
})();
