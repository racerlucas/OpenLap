/**
 * data.js — Data Selection page.
 *
 * Layout: sessions list (left, scrollable) | detail + sync panel (right, fixed 340px)
 *
 * Flow:
 *  1. Mount: load cache immediately, auto-trigger background scan
 *  2. Sessions grouped by actual date (YYYY-MM-DD), newest-first
 *  3. Click session → right panel shows info, lap chips, video sync
 *  4. Lap chips → staged, then "Add to Export" queues them in State
 *  5. Align video: <video> element + scrub slider + mark button → saves offset
 */
(function () {

  // ── Persistent module state ───────────────────────────────────────────────────
  let _sessions   = [];   // flat list, sorted newest-first
  let _meta       = {};   // csv_path → {track, laps, best}
  let _lapDetails = {};   // csv_path → [{lap_idx, duration, is_best}]
  let _selCsv     = null; // currently selected session csv_path
  let _splitSel   = new Set(); // csv_path set for batch lap-split
  let _config     = null;
  let _scanning    = false;
  let _autoSyncing = false;
  let _autoSyncByCsv = {}; // csv_path -> { running:boolean, current:number, total:number, status:string, text:string, pct:number|null }
  let _statusMsg   = '';
  let _container   = null;
  let _metaQueue   = [];   // sessions waiting for meta fetch
  let _metaBusy    = false;
  let _videoPort   = 0;    // localhost port of the Python video file server
  let _unlistenFns = [];   // push-event unlisten callbacks
  let _syncDecoderSessionId = '';
  let _syncDecoderVideoPath = '';
  let _pageUnmounted = false; // guards async video.play() after route unmount

  // Best per day: csv_path → true if this session has the day's best lap
  let _dayBest = {};

  // ── Utilities ─────────────────────────────────────────────────────────────────

  function fmtTime(secs) {
    if (secs == null || secs < 0 || isNaN(secs)) return '—';
    const m = Math.floor(secs / 60);
    const s = (secs % 60).toFixed(3).padStart(6, '0');
    return `${m}:${s}`;
  }

  function fmtDateTime(iso) {
    if (!iso) return '—';
    try {
      return _dateFromIso(iso).toLocaleString(undefined,
        { year:'numeric', month:'2-digit', day:'2-digit',
          hour:'2-digit', minute:'2-digit' });
    } catch { return iso; }
  }

  function _dateFromIso(iso) {
    // Some cached sessions may store a naive ISO string without timezone.
    // Treat those as UTC so display stays consistent (local time) before/after scan.
    const s = String(iso || '');
    const hasTz = /([zZ]|[+\-]\d{2}:\d{2})$/.test(s);
    const normalized = hasTz ? s : (s ? (s + 'Z') : s);
    return new Date(normalized);
  }

  function dateKey(iso) {
    if (!iso) return 'Unknown';
    try {
      const d = _dateFromIso(iso);
      if (isNaN(d.getTime())) return 'Unknown';
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return `${y}-${m}-${day}`;
    } catch {
      return 'Unknown';
    }
  }

  function esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function baseName(p) {
    return (p||'').replace(/\\/g,'/').split('/').pop() || p;
  }

  /** Match auto_sync push csv_path to scan rows (UNC vs slash / case). */
  function _normCsvPathKey(p) {
    return String(p || '').replace(/\//g, '\\').toLowerCase();
  }
  function _findSessionByCsvPath(csvPath) {
    if (!csvPath || !_sessions.length) return null;
    const direct = _sessions.find(x => x.csv_path === csvPath);
    if (direct) return direct;
    const key = _normCsvPathKey(csvPath);
    return _sessions.find(x => _normCsvPathKey(x.csv_path) === key) || null;
  }

  /** Path key variants for matching _config.offsets (mirrors Python _csv_key_variants). */
  function _csvPathKeyVariants(csvPath) {
    if (!csvPath) return [];
    const raw = String(csvPath);
    const out = [raw];
    if (raw.includes('\\')) out.push(raw.replace(/\\/g, '/'));
    if (raw.includes('/')) out.push(raw.replace(/\//g, '\\'));
    return [...new Set(out.filter(Boolean))];
  }

  /** Read persisted offset/source using any path-variant key (avoids false “丢失” after cache/rescan). */
  function _storedSyncFromConfig(csvPath) {
    const o = _config?.offsets;
    if (!o || typeof o !== 'object') return { offset: null, source: undefined };
    const srcMap = _config?.offset_sources;
    for (const k of _csvPathKeyVariants(csvPath)) {
      if (Object.prototype.hasOwnProperty.call(o, k) && o[k] != null && Number.isFinite(Number(o[k]))) {
        const src = srcMap && Object.prototype.hasOwnProperty.call(srcMap, k) ? srcMap[k] : undefined;
        return { offset: Number(o[k]), source: src };
      }
    }
    return { offset: null, source: undefined };
  }

  function _applyStoredSyncToSession(s) {
    if (!s || !s.csv_path) return;
    const { offset, source } = _storedSyncFromConfig(s.csv_path);
    if (offset != null) {
      s.sync_offset = offset;
      if (source !== undefined) s.sync_source = source;
    }
  }

  /**
   * 当 sync_offset 为负时，用「第 1 圈」可能对不上视频 0s 起点（telemetry 早于录像）。
   * 选第一个计时圈（非 out/in）使 off + elapsed_start >= 0，否则退回 pool 中最后一圈。
   */
  function pickAlignRefLapNumForOffset(off, laps, preferredRef, opts = null) {
    const o = Number(off);
    const pref = Number(preferredRef);
    if (!Number.isFinite(o) || !laps?.length) return pref;
    const minVt = opts && Number.isFinite(Number(opts.minVt)) ? Number(opts.minVt) : 0;
    const maxVt = opts && Number.isFinite(Number(opts.maxVt)) ? Number(opts.maxVt) : null;
    const timed = laps.filter(l => !l.is_outlap && !l.is_inlap);
    const pool = timed.length ? timed : laps;
    const prefLap = laps.find(l => Number(l.lap_num) === pref);
    const prefE = prefLap ? (Number(prefLap.elapsed_start) || 0) : 0;
    const prefVt = o + prefE;
    if (prefVt >= (minVt - 1e-3) && (maxVt == null || prefVt <= (maxVt + 1e-3))) return pref;
    const sorted = [...pool].sort((a, b) => (Number(a.elapsed_start) || 0) - (Number(b.elapsed_start) || 0));
    for (const lap of sorted) {
      const e = Number(lap.elapsed_start) || 0;
      const vt = o + e;
      if (vt >= (minVt - 1e-3) && (maxVt == null || vt <= (maxVt + 1e-3))) return Number(lap.lap_num);
    }
    // If nothing fits both bounds, prefer the last lap that is within maxVt (if provided).
    if (maxVt != null) {
      for (let i = sorted.length - 1; i >= 0; i--) {
        const e = Number(sorted[i].elapsed_start) || 0;
        const vt = o + e;
        if (vt <= (maxVt + 1e-3)) return Number(sorted[i].lap_num);
      }
    }
    const last = sorted[sorted.length - 1];
    return last ? Number(last.lap_num) : pref;
  }

  /** Auto-detected offsets: strong correlation vs metadata baseline fallback */
  function syncIsAutoFamily(src) {
    return src === 'auto' || src === 'auto_baseline';
  }
  function syncIsBaselineAuto(src) {
    return src === 'auto_baseline';
  }

  /** Working offset: calibration panel writes _temp_sync_offset until「标记/确认」持久化 */
  function sessionEffectiveOffset(s) {
    if (!s) return null;
    if (s._temp_sync_offset != null && Number.isFinite(Number(s._temp_sync_offset)))
      return Number(s._temp_sync_offset);
    if (s.sync_offset != null && Number.isFinite(Number(s.sync_offset))) return Number(s.sync_offset);
    return null;
  }

  function sessionOffsetIsPreview(s) {
    if (s._temp_sync_offset == null || !Number.isFinite(Number(s._temp_sync_offset))) return false;
    const t = Number(s._temp_sync_offset);
    if (s.sync_offset == null || !Number.isFinite(Number(s.sync_offset))) return true;
    return Math.abs(t - Number(s.sync_offset)) > 1e-6;
  }

  /** 本节信息「偏移」：仅展示已写入配置的 sync_offset（不含拖条/播放的临时推算） */
  function formatSavedSessionOffsetLabel(s) {
    if (!s || s.sync_offset == null || !Number.isFinite(Number(s.sync_offset))) return '未设置';
    const v = Number(s.sync_offset);
    if (syncIsAutoFamily(s.sync_source)) return `${v.toFixed(3)}s ~自动`;
    return `${v.toFixed(3)}s ✓`;
  }

  function videoUrl(winPath) {
    // Pass path as a query param so browser slash-normalization never mangles UNC paths
    if (_videoPort) return `http://127.0.0.1:${_videoPort}/?f=${encodeURIComponent(winPath)}`;
    return 'file:///' + winPath.replace(/\\/g, '/');
  }

  // ── Debug (off by default) ────────────────────────────────────────────────────
  // Enable in devtools console:
  //   localStorage.setItem('openlap_debug_sync_video', '1')
  // Disable:
  //   localStorage.setItem('openlap_debug_sync_video', '0')
  function _dbgSyncVideoEnabled() {
    // Also allow an in-page toggle for environments where devtools localStorage is isolated:
    //   window.__openlap_debug_sync_video = true
    try {
      if (typeof window !== 'undefined' && window.__openlap_debug_sync_video === true) return true;
    } catch (_) {}
    try { return localStorage.getItem('openlap_debug_sync_video') === '1'; } catch (_) { return false; }
  }
  function _dbgVideoState(v, extra = {}) {
    if (!v) return { ...extra, v: null };
    const rsMap = ['HAVE_NOTHING', 'HAVE_METADATA', 'HAVE_CURRENT_DATA', 'HAVE_FUTURE_DATA', 'HAVE_ENOUGH_DATA'];
    const nsMap = ['NETWORK_EMPTY', 'NETWORK_IDLE', 'NETWORK_LOADING', 'NETWORK_NO_SOURCE'];
    return {
      ...extra,
      readyState: v.readyState,
      readyStateName: rsMap[v.readyState] || String(v.readyState),
      networkState: v.networkState,
      networkStateName: nsMap[v.networkState] || String(v.networkState),
      paused: !!v.paused,
      ended: !!v.ended,
      currentTime: Number(v.currentTime || 0),
      duration: Number.isFinite(Number(v.duration)) ? Number(v.duration) : null,
      videoWidth: v.videoWidth || 0,
      videoHeight: v.videoHeight || 0,
      src: String(v.currentSrc || v.src || ''),
    };
  }
  function dbgSyncVideo(tag, extra = {}) {
    if (!_dbgSyncVideoEnabled()) return;
    // Use console.log so it won't be hidden by "Verbose"/"Debug" filters in some devtools.
    try { console.log('[sync_video]', tag, extra); } catch (_) {}
  }
  // Always-visible UI logs for user-triggered actions (click/drag).
  function logSyncVideoUI(tag, extra = {}) {
    try { console.log('[sync_video_ui]', tag, extra); } catch (_) {}
  }

  function _domBox(el) {
    try {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const cs = window.getComputedStyle ? window.getComputedStyle(el) : null;
      return {
        w: Math.round(r.width),
        h: Math.round(r.height),
        x: Math.round(r.left),
        y: Math.round(r.top),
        display: cs ? cs.display : (el.style?.display || ''),
        visibility: cs ? cs.visibility : (el.style?.visibility || ''),
        opacity: cs ? cs.opacity : (el.style?.opacity || ''),
        zIndex: cs ? cs.zIndex : (el.style?.zIndex || ''),
        pointerEvents: cs ? cs.pointerEvents : (el.style?.pointerEvents || ''),
      };
    } catch (_) {
      return null;
    }
  }

  // ── Compute day-best ──────────────────────────────────────────────────────────

  function recomputeDayBest() {
    _dayBest = {};
    const byDay = {};
    for (const s of _sessions) {
      const d = dateKey(s.csv_start);
      if (!byDay[d]) byDay[d] = [];
      const m = _meta[s.csv_path];
      if (m && m.best_secs != null) byDay[d].push({ csv: s.csv_path, best: m.best_secs });
    }
    for (const entries of Object.values(byDay)) {
      if (!entries.length) continue;
      entries.sort((a, b) => a.best - b.best);
      _dayBest[entries[0].csv] = true;
    }
  }

  // ── Session grouping ──────────────────────────────────────────────────────────

  function sortedGroups() {
    const sorted = [..._sessions].sort((a, b) => {
      const ta = a.csv_start ? _dateFromIso(a.csv_start).getTime() : 0;
      const tb = b.csv_start ? _dateFromIso(b.csv_start).getTime() : 0;
      return tb - ta;
    });
    const groups = [];
    let lastDay = null;
    for (const s of sorted) {
      const day = dateKey(s.csv_start);
      if (day !== lastDay) { groups.push({ day, sessions: [] }); lastDay = day; }
      groups[groups.length - 1].sessions.push(s);
    }
    return groups;
  }

  // ── Left panel: session list ──────────────────────────────────────────────────

  function renderLeft() {
    const pane = _container?.querySelector('#data-left');
    if (!pane) return;

    if (_sessions.length === 0) {
      pane.innerHTML = `<div class="dl-empty">暂无节次，请先在设置中配置目录后点击扫描。</div>`;
      return;
    }

    const groups = sortedGroups();
    pane.innerHTML = groups.map(g => `
      <div class="dl-day-hdr">${esc(g.day)}</div>
      ${g.sessions.map(s => sessionRow(s)).join('')}
    `).join('');

    pane.querySelectorAll('.dl-row').forEach(row => {
      row.addEventListener('click', () => selectSession(row.dataset.csv));
    });
    pane.querySelectorAll('.dl-sel').forEach(cb => {
      cb.addEventListener('click', (e) => e.stopPropagation());
      cb.addEventListener('change', (e) => {
        const csv = e.target.dataset.csv;
        if (!csv) return;
        if (e.target.checked) _splitSel.add(csv);
        else _splitSel.delete(csv);
        refreshFooter();
      });
    });
  }

  function sessionRow(s) {
    const m       = _meta[s.csv_path] || {};
    const isSel   = s.csv_path === _selCsv;
    const isSplitSel = _splitSel.has(s.csv_path);
    const isDayB  = _dayBest[s.csv_path];
    const time = s.csv_start
      ? _dateFromIso(s.csv_start).toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' })
      : '—';
    const trackOverride = _config?.session_info?.[s.csv_path]?.info_track;
    const track   = trackOverride || m.track || baseName(s.csv_path);
    const lapStr  = m.laps || s.laps || '—';
    const bestStr = m.best || (m.best_secs != null && Number.isFinite(Number(m.best_secs))
      ? fmtTime(Number(m.best_secs))
      : (s.best || '—'));
    const vidStr  = (m.video_dur_s != null && Number.isFinite(Number(m.video_dur_s)) && Number(m.video_dur_s) > 0)
      ? fmtTime(Number(m.video_dur_s))
      : (s.matched ? '—' : '');
    const syncLabel = s.needs_conversion           ? '↻ conv'
                    : (!s.matched)                  ? 'no vid'
                    : s.sync_offset != null && syncIsAutoFamily(s.sync_source) ? '~自动'
                    : s.sync_offset != null         ? '✓ user'
                    : '≈ unset';
    const iconCls   = s.needs_conversion           ? 'di-pending'
                    : (!s.matched)                  ? 'di-novid'
                    : s.sync_offset != null && syncIsAutoFamily(s.sync_source) ? 'di-auto'
                    : s.sync_offset != null         ? 'di-user'
                    : 'di-unsync';

    return `<div class="dl-row${isSel?' sel':''}${isDayB?' day-best':''}" data-csv="${esc(s.csv_path)}">
      <span class="dl-col" style="display:flex;justify-content:center;align-items:center">
        <input type="checkbox" class="dl-sel" data-csv="${esc(s.csv_path)}" ${isSplitSel ? 'checked' : ''} title="用于批量切圈">
      </span>
      <span class="dl-sync ${iconCls}">${syncLabel}</span>
      <span class="dl-time">${esc(time)}</span>
      <span class="dl-track" title="${esc(s.csv_path)}">${esc(track)}</span>
      <span class="dl-source">${esc(s.source||'RaceBox')}</span>
      <span class="dl-num">${esc(lapStr)}</span>
      <span class="dl-num">${esc(bestStr)}</span>
      <span class="dl-num" title="视频时长（探测自第 1 段）">${esc(vidStr)}</span>
    </div>`;
  }

  function selectSession(csvPath) {
    _selCsv = csvPath;
    _container?.querySelectorAll('.dl-row').forEach(r => {
      r.classList.toggle('sel', r.dataset.csv === csvPath);
    });
    renderRight();

    const s = _sessions.find(x => x.csv_path === csvPath);
    if (!s) return;

    // Always keep previewSession in sync with the selected session
    // so navigating to Overlay via the sidebar also works
    State.set('previewSession', {
      csv_path:    s.csv_path,
      lap_idx:     0,
      video_paths: s.video_paths || [],
      sync_offset: sessionEffectiveOffset(s) ?? 0,
      source:      s.source || 'RaceBox',
      csv_start:   s.csv_start  || null,
    });

    if (!_lapDetails[csvPath]) loadLaps(s);
  }

  // ── Right panel: session detail + sync ────────────────────────────────────────

  function renderRight() {
    const pane = _container?.querySelector('#data-right');
    if (!pane) return;

    const s = _sessions.find(x => x.csv_path === _selCsv);
    if (!s) {
      pane.innerHTML = `<div class="dr-empty">请选择一节查看详情并校准视频。</div>`;
      return;
    }

    _applyStoredSyncToSession(s);

    const m   = _meta[s.csv_path] || {};
    const effOff = sessionEffectiveOffset(s);
    const savedOff = s.sync_offset != null && Number.isFinite(Number(s.sync_offset)) ? Number(s.sync_offset) : null;

    // Session info
    const vidPaths    = s.video_paths || [];
    const hasVid      = vidPaths.length > 0;
    const trackOverride  = _config?.session_info?.[s.csv_path]?.info_track;
    const effectiveTrack = trackOverride || m.track || '';
    const detailLaps = m.laps || s.laps || '—';
    const detailBest = m.best
      || (m.best_secs != null && Number.isFinite(Number(m.best_secs)) ? fmtTime(Number(m.best_secs)) : '')
      || s.best
      || '—';

    pane.innerHTML = `
<!-- Info card -->
<div class="dr-card">
  <div class="dr-card-title">本节信息</div>
  <div class="dr-rows">
    <div class="dr-row"><span class="dr-lbl">来源</span><span class="dr-val">${esc(s.source||'RaceBox')}</span></div>
    <div class="dr-row">
      <span class="dr-lbl">赛道</span>
      <span class="dr-val dr-track-val" id="dr-track-display">
        ${esc(effectiveTrack||'—')}
        <button class="btn-inline-edit" id="dr-track-edit-btn" title="Edit track name" style="margin-left:4px;opacity:0.6;font-size:9px;cursor:pointer;border:none;background:none;color:var(--acc2);padding:0 2px">✎</button>
      </span>
    </div>
    <div class="dr-row"><span class="dr-lbl">日期</span><span class="dr-val">${esc(fmtDateTime(s.csv_start))}</span></div>
    <div class="dr-row"><span class="dr-lbl">圈数</span><span class="dr-val">${esc(detailLaps)}</span></div>
    <div class="dr-row"><span class="dr-lbl">最佳</span><span class="dr-val" style="color:var(--ok)">${esc(detailBest)}</span></div>
    <div class="dr-row"><span class="dr-lbl">视频</span><span class="dr-val ${hasVid?'':'dr-warn'}">${hasVid ? `✓ ${vidPaths.length} 段` : '✗ 未匹配'}</span></div>
    <div class="dr-row">
      <span class="dr-lbl">偏移</span>
      <span class="dr-val" id="dr-off-edit-wrap" style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span class="${savedOff!=null&&Number.isFinite(savedOff)?'':'dr-warn'}" id="dr-off-display">${esc(formatSavedSessionOffsetLabel(s))}</span>
        <button type="button" class="btn-inline-edit" id="dr-off-edit-btn" title="编辑同步偏移（秒），应用后跳转视频到参考圈对齐位置"
                style="opacity:0.65;font-size:9px;cursor:pointer;border:none;background:none;color:var(--acc2);padding:0 2px">✎</button>
      </span>
    </div>
  </div>
  <div class="dr-actions">
    <label class="dr-mode-label">模式:
      <select class="input-field dr-mode-sel" id="dr-bike-sel">
        <option value="car">汽车</option>
        <option value="bike"${(s.is_bike?' selected':'')}>摩托</option>
      </select>
    </label>
  </div>
</div>

<!-- AIM conversion -->
${s.needs_conversion ? `
<div class="dr-card" id="dr-conv-card">
  <div class="dr-card-title">AIM XRK 转换</div>
  <div class="dr-hint">该节需先从 XRK 格式转换后才能使用。</div>
  <div class="dr-actions" style="margin-top:8px">
    <button class="btn btn-secondary btn-sm" id="dr-conv-btn">转换为 CSV</button>
    <span id="dr-conv-msg" class="status-msg"></span>
  </div>
</div>` : ''}

<!-- Video align -->
${hasVid ? `
${renderAlignCard(s, vidPaths, effOff)}
<div class="dr-card" style="margin-top:10px">
  <div class="dr-card-title">视频</div>
  <div class="dr-actions" style="margin-top:8px">
    <button class="btn btn-secondary btn-sm" id="dr-assign-vid-btn">选择/更换视频（可多选）…</button>
    <button class="btn btn-secondary btn-sm" id="dr-clear-vid-btn" style="opacity:0.75">清除绑定</button>
    <span id="dr-assign-vid-msg" class="status-msg"></span>
  </div>
</div>
` : `
<div class="dr-card">
  <div class="dr-card-title">视频</div>
  <div class="dr-hint" style="color:var(--warn)">未找到匹配视频。</div>
  <div class="dr-actions" style="margin-top:8px">
    <button class="btn btn-secondary btn-sm" id="dr-assign-vid-btn">选择/更换视频（可多选）…</button>
    <button class="btn btn-secondary btn-sm" id="dr-clear-vid-btn" style="opacity:0.75">清除绑定</button>
    <span id="dr-assign-vid-msg" class="status-msg"></span>
  </div>
</div>`}

${renderLapTagCard(s)}

<!-- Primary CTA: Open in Overlay -->
<button class="btn btn-accent" id="dr-goto-overlay"
        style="width:100%; padding:9px; font-size:11px; font-weight:600; border-radius:var(--radius); flex-shrink:0; margin-top:auto;">
  打开到叠加编辑 →
</button>
`;

    wirePropPanel(s, pane);
  }

  function renderAlignCard(s, vidPaths, effOffForReadoutInit) {
    const offReadout = (effOffForReadoutInit != null && Number.isFinite(effOffForReadoutInit))
      ? `${effOffForReadoutInit >= 0 ? '+' : ''}${effOffForReadoutInit.toFixed(3)}s`
      : '—';
    const offPreview = sessionOffsetIsPreview(s);
    const isAuto    = syncIsAutoFamily(s.sync_source);
    const isBaselineAuto = syncIsBaselineAuto(s.sync_source);
    const savedAlignOff = s.sync_offset != null && Number.isFinite(Number(s.sync_offset)) ? Number(s.sync_offset) : null;
    const as = _autoSyncByCsv?.[s.csv_path];
    const isSyncing = !!as?.running;
    const laps = _lapDetails[s.csv_path] || [];
    const cfgInfo = _config?.session_info?.[s.csv_path] || {};
    // Default reference lap (auto-picked by offset so vt=offset+elapsed_start lands inside [0, video]).
    // This avoids the "always clamps to 0s" feel when offset is negative and outlap is used as ref.
    const firstTimed = laps.find(l => !l.is_outlap && !l.is_inlap) || laps.find(l => !l.is_outlap) || laps[0] || null;
    const defaultRefLap = Number(firstTimed?.lap_num ?? 1) || 1;
    // Only treat sync_ref_lap_num as a preference if the user explicitly changed it.
    // (Earlier builds could auto-write sync_ref_lap_num and "poison" defaults to lap 3, etc.)
    const userPrefRefLap = cfgInfo?.sync_ref_lap_user === true;
    const preferredRefLap = userPrefRefLap
      ? Number(cfgInfo?.sync_ref_lap_num || defaultRefLap)
      : defaultRefLap;
    const offForPick = (effOffForReadoutInit != null && Number.isFinite(Number(effOffForReadoutInit)))
      ? Number(effOffForReadoutInit)
      : (sessionEffectiveOffset(s) ?? 0);
    // Default ref lap should be the *first* lap whose start time appears on the video timeline.
    // (vt = off + elapsed_start >= 0). Allow vt=0 — it's still "in timeline".
    const savedRefLap = pickAlignRefLapNumForOffset(offForPick, laps, preferredRefLap, { minVt: 0 });
    const preciseMode = !!cfgInfo?.sync_precise_mode;
    const lapOptions = laps.map((lap, idx) => {
      const lapNum = Number(lap.lap_num ?? (idx + 1));
      const sel = lapNum === savedRefLap ? 'selected' : '';
      return `<option value="${lapNum}" ${sel}>第 ${lapNum} 圈</option>`;
    }).join('');
    const autoSyncBtnText = (s.sync_offset != null) ? '🪄 重新对齐' : '🪄 自动对齐';
    const autoSyncBtnTitle = (s.sync_offset != null)
      ? '强制重新运行自动对齐（不会覆盖已确认的用户偏移）'
      : '按元数据时间 + 微调进行自动对齐';
    const autoNote  = isSyncing
      ? `<div style="font-size:9px;color:#ffb74d;margin-bottom:6px;padding:5px 6px;
                     background:rgba(255,183,77,0.08);border-radius:4px;border-left:2px solid #ffb74d">
           正在自动检测同步偏移… 完成后可拖动校准。
         </div>`
      : isAuto
      ? `<div style="font-size:9px;color:#b0c4e8;margin-bottom:6px;padding:5px 6px;
                     background:rgba(100,140,220,0.1);border-radius:4px;border-left:2px solid #90caf9">
           自动对齐已采用 <strong>${savedAlignOff != null ? savedAlignOff.toFixed(3)+'s' : '—'}</strong> 作为本节的同步偏移（~自动，第一版）。
           ${isBaselineAuto ? '本结果以元数据为锚，运动相关置信度较低属正常。' : '运动相关匹配置信度高。'}
           可直接<strong>打开叠加预览</strong>；若要再抠起点线，用 seek / 选「对齐第X圈」微调即可；满意后点「确认」只是把当前对齐<strong>标为用户锁定</strong>（非必须）。
         </div>`
      : '';
    return `
<div class="dr-card dr-align-card">
  <div class="dr-card-title">校准视频</div>
  ${autoNote}
  <div class="sync-frame-wrap" data-autosync="${isSyncing ? '1' : '0'}">
    <video id="sync-video-live" class="sync-video" muted playsinline preload="auto" style="display:none"></video>
    <img id="sync-frame" class="sync-video" alt="">
    <div id="sync-loading" class="sync-loading">
      <span class="sync-spinner" aria-hidden="true"></span>
      <span id="sync-loading-text">视频加载中…</span>
    </div>
    <div id="sync-autosync-overlay" class="sync-autosync-overlay ${isSyncing ? '' : 'hidden'}" aria-live="polite">
      <div class="sync-autosync-panel">
        <div class="sync-autosync-title">自动对齐进行中…</div>
        <div class="sync-autosync-sub" id="sync-autosync-text">${esc(as?.text || '手动校准已暂时锁定')}</div>
        <div class="sync-autosync-bar">
          <div class="sync-autosync-bar-fill ${as?.pct == null ? 'indeterminate' : ''}" id="sync-autosync-bar-fill" style="width:${as?.pct != null ? `${Math.max(0, Math.min(100, as.pct)).toFixed(1)}%` : '40%'}"></div>
        </div>
      </div>
    </div>
  </div>
  <div class="sync-controls" data-autosync="${isSyncing ? '1' : '0'}">
    <button class="btn btn-sm" id="sv-mm">◀◀ −1s</button>
    <button class="btn btn-sm" id="sv-m">◀ −1f</button>
    <button class="btn btn-sm" id="sv-play">▶ 播放</button>
    <button class="btn btn-sm" id="sv-p">▶ +1f</button>
    <button class="btn btn-sm" id="sv-pp">▶▶ +1s</button>
    <span class="sync-time" id="sv-time">0:00.000</span>
  </div>
  <input type="range" id="sv-scrub" class="sync-scrub" min="0" max="1000" value="0" step="1">
  <div class="sync-mark-row">
    <button class="btn btn-ok btn-sm" id="sv-mark">${isAuto ? '✓ 确认对齐圈起点' : '🏁 标记对齐圈起点'}</button>
    <button class="btn btn-secondary btn-sm" id="sv-auto-sync" title="${esc(autoSyncBtnTitle)}">${autoSyncBtnText}</button>
    <label style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text2);padding:0 4px;white-space:nowrap" title="仅对 ◀−1f / ▶+1f 使用 OpenCV 精确帧；进度条与 ±1s 仍优先 GPU 流畅定位">
      <input type="checkbox" id="sv-precise-mode" ${preciseMode ? 'checked' : ''}>
      精确模式
    </label>
    ${lapOptions ? `<select id="sv-ref-lap" class="input-field input-narrow sync-off-input" title="选择按哪一圈对齐">${lapOptions}</select>` : ''}
    <span class="sync-mark-val" id="sv-mark-val">${offPreview ? '预览' : (savedAlignOff != null && !isAuto ? '✓ saved' : '')}</span>
  </div>
  <div class="sync-offset-readout" title="由当前帧与参考圈反算的 offset，拖条时会临时预览；自动对齐写入的值已生效，可直接预览。秒数输入请点 ✎">
    <span class="sync-offset-readout-lbl">推算 offset（只读）</span>
    <code id="sv-off-readout" class="sync-off-readout-code">${esc(offReadout)}</code>
  </div>
  ${vidPaths.length > 1 ? `<div style="font-size:9px;color:var(--text3);margin-top:4px">另有 ${vidPaths.length-1} 段视频</div>` : ''}
</div>`;
  }

  async function triggerAutoSyncForSession(s, reason = '') {
    if (!s || !s.csv_path || !(s.video_paths && s.video_paths.length)) return false;
    try {
      const r = await API.startAutoSync([s], { force: true });
      if (r?.queued > 0) {
        _autoSyncing = true;
        _autoSyncByCsv[s.csv_path] = {
          running: true,
          current: 0,
          total: 0,
          status: 'starting',
          text: '正在启动… 手动校准已暂时锁定',
          pct: null,
        };
        const why = reason ? `（${reason}）` : '';
        setStatus(`自动同步已开始${why}…`);
        if (_selCsv === s.csv_path) renderRight();
        return true;
      }
      const why = reason ? `（${reason}）` : '';
      const reasonMap = {
        disabled: '全局自动同步开关关闭',
        no_eligible_sessions: '该节当前不满足自动同步条件',
      };
      const msg = reasonMap[r?.reason] || `未启动（${r?.reason || 'unknown'}）`;
      setStatus(`自动同步未启动${why}：${msg}`);
    } catch (_) {}
    return false;
  }

  function _setAutoSyncUiFor(csvPath, patch) {
    if (!csvPath) return;
    const prev = _autoSyncByCsv[csvPath] || {};
    _autoSyncByCsv[csvPath] = { ...prev, ...patch };
  }

  function _updateAutoSyncOverlayIfVisible(csvPath) {
    if (!_container || _selCsv !== csvPath) return;
    const overlay = _container.querySelector('#sync-autosync-overlay');
    if (!overlay) return;
    const st = _autoSyncByCsv[csvPath];
    // Safety: if global auto-sync is no longer running, never keep controls locked.
    if (!st?.running || !_autoSyncing) {
      overlay.classList.add('hidden');
      overlay.closest('.sync-frame-wrap')?.setAttribute('data-autosync', '0');
      overlay.closest('.dr-align-card')?.querySelector('.sync-controls')?.setAttribute('data-autosync', '0');
      // Unlock controls
      _container.querySelectorAll('#sv-mm,#sv-m,#sv-play,#sv-p,#sv-pp,#sv-scrub,#sv-mark,#sv-auto-sync,#sv-precise-mode,#sv-ref-lap')
        .forEach(el => { try { el.disabled = false; } catch {} });
      return;
    }

    overlay.classList.remove('hidden');
    overlay.closest('.sync-frame-wrap')?.setAttribute('data-autosync', '1');
    overlay.closest('.dr-align-card')?.querySelector('.sync-controls')?.setAttribute('data-autosync', '1');
    const textEl = overlay.querySelector('#sync-autosync-text');
    if (textEl) textEl.textContent = st.text || '自动对齐进行中…';
    const fill = overlay.querySelector('#sync-autosync-bar-fill');
    if (fill) {
      if (st.pct == null) {
        fill.classList.add('indeterminate');
        fill.style.width = '40%';
      } else {
        fill.classList.remove('indeterminate');
        fill.style.width = `${Math.max(0, Math.min(100, st.pct)).toFixed(1)}%`;
      }
    }
    // Lock controls while running
    _container.querySelectorAll('#sv-mm,#sv-m,#sv-play,#sv-p,#sv-pp,#sv-scrub,#sv-mark,#sv-auto-sync,#sv-precise-mode,#sv-ref-lap')
      .forEach(el => { try { el.disabled = true; } catch {} });
  }

  function renderLapTagCard(s) {
    const laps = _lapDetails[s.csv_path] || [];
    if (!laps.length) return '';
    return `
<div class="dr-card">
  <details style="width:100%">
    <summary class="dr-card-title" style="cursor:pointer;user-select:none">圈标签（手动）</summary>
    <div class="dr-hint" style="margin:6px 0 6px 0">默认首圈为 outlap、末圈为 inlap，可在这里手动设置。</div>
    <div style="max-height:120px;overflow:auto;border:1px solid var(--line);border-radius:6px;padding:6px">
      ${laps.map((lap, idx) => `
        <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 2px;border-bottom:${idx===laps.length-1?'none':'1px solid var(--line)'}">
          <span style="font-size:10px;color:var(--text2)">Lap ${lap.lap_num ?? (idx + 1)}</span>
          <div style="display:flex;gap:10px;align-items:center">
            <label style="font-size:10px;display:flex;gap:4px;align-items:center">
              <input type="checkbox" class="lap-tag-toggle" data-lap-num="${lap.lap_num}" data-tag="outlap" ${lap.is_outlap ? 'checked' : ''}>
              outlap
            </label>
            <label style="font-size:10px;display:flex;gap:4px;align-items:center">
              <input type="checkbox" class="lap-tag-toggle" data-lap-num="${lap.lap_num}" data-tag="inlap" ${lap.is_inlap ? 'checked' : ''}>
              inlap
            </label>
          </div>
        </div>
      `).join('')}
    </div>
  </details>
</div>`;
  }

  function _showBulkRenamePrompt(pane, oldName, newName, count) {
    // Remove any existing prompt first
    pane.querySelector('#bulk-rename-banner')?.remove();

    const banner = document.createElement('div');
    banner.id = 'bulk-rename-banner';
    banner.style.cssText = `
      margin:8px 0 0; padding:8px 10px; border-radius:4px;
      background:rgba(99,102,241,0.12); border:1px solid rgba(99,102,241,0.35);
      font-size:10px; color:var(--text1); line-height:1.5;
    `;
    banner.innerHTML = `
      <div style="margin-bottom:6px">
        <strong>${count} 节</strong>
        also named <em>"${esc(oldName)}"</em>.<br>
        Rename all of them to <em>"${esc(newName)}"</em> as well?
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm btn-accent" id="bulk-rename-yes">Yes, rename all</button>
        <button class="btn btn-sm" id="bulk-rename-no">No, just this one</button>
      </div>
    `;

    // Insert after the info card (first .dr-card)
    const firstCard = pane.querySelector('.dr-card');
    firstCard ? firstCard.after(banner) : pane.prepend(banner);

    banner.querySelector('#bulk-rename-yes').addEventListener('click', async () => {
      banner.innerHTML = '<span style="color:var(--text3)">Renaming…</span>';
      try {
        // Compute the exact paths to rename here in JS where _meta is available,
        // rather than letting the backend guess from the (often empty) scan cache.
        if (!_config.session_info) _config.session_info = {};
        const pathsToRename = [];
        for (const sess of _sessions) {
          if (sess.csv_path === _selCsv) continue;
          const ov = _config.session_info[sess.csv_path]?.info_track;
          const mt = _meta[sess.csv_path]?.track;
          if ((ov || mt || '').trim().toLowerCase() === oldName.toLowerCase()) {
            pathsToRename.push(sess.csv_path);
          }
        }
        await API.bulkRenameTrack(pathsToRename, newName);
        // Apply to local config cache
        for (const csvPath of pathsToRename) {
          const ex = _config.session_info[csvPath] || {};
          _config.session_info[csvPath] = { ...ex, info_track: newName };
        }
        banner.remove();
        renderLeft();
      } catch (e) {
        banner.innerHTML = `<span style="color:var(--err)">错误：${esc(String(e))}</span>`;
      }
    });

    banner.querySelector('#bulk-rename-no').addEventListener('click', () => banner.remove());
  }

  function wirePropPanel(s, pane) {
    // Inline track name edit
    pane.querySelector('#dr-track-edit-btn')?.addEventListener('click', () => {
      const display     = pane.querySelector('#dr-track-display');
      const trackOverride  = _config?.session_info?.[s.csv_path]?.info_track;
      const currentTrack   = trackOverride || _meta[s.csv_path]?.track || '';
      display.innerHTML = `
        <input type="text" class="input-field" id="dr-track-input"
               value="${esc(currentTrack)}" style="width:130px;font-size:11px;padding:1px 4px">
        <button class="btn btn-sm" id="dr-track-save" style="padding:1px 5px;margin-left:2px">✓</button>
        <button class="btn btn-sm" id="dr-track-cancel" style="padding:1px 5px">✕</button>
      `;
      const inp = pane.querySelector('#dr-track-input');
      inp?.focus(); inp?.select();

      const saveTrack = async () => {
        const newTrack = pane.querySelector('#dr-track-input')?.value?.trim() ?? '';
        if (newTrack === currentTrack) { renderRight(); return; }

        // Save this session first
        const existing = _config?.session_info?.[s.csv_path] || {};
        await API.editSessionInfo(s.csv_path, { ...existing, info_track: newTrack });
        if (!_config.session_info) _config.session_info = {};
        _config.session_info[s.csv_path] = { ...existing, info_track: newTrack };

        // Count other sessions sharing the old effective track name
        const others = currentTrack ? _sessions.filter(sess => {
          if (sess.csv_path === s.csv_path) return false;
          const ov = _config?.session_info?.[sess.csv_path]?.info_track;
          const mt = _meta[sess.csv_path]?.track;
          return (ov || mt || '').trim().toLowerCase() === currentTrack.toLowerCase();
        }) : [];

        renderRight();
        renderLeft();

        if (others.length > 0) {
          _showBulkRenamePrompt(pane, currentTrack, newTrack, others.length);
        }
      };

      pane.querySelector('#dr-track-save')?.addEventListener('click', saveTrack);
      pane.querySelector('#dr-track-cancel')?.addEventListener('click', () => renderRight());
      inp?.addEventListener('keydown', e => {
        if (e.key === 'Enter')  saveTrack();
        if (e.key === 'Escape') renderRight();
      });
    });

    // Open in Overlay
    pane.querySelector('#dr-goto-overlay')?.addEventListener('click', () => {
      const laps = _lapDetails[s.csv_path] || [];
      const lap  = laps[0] || null; // default focus: first lap (including outlap)
      State.set('previewSession', {
        csv_path:    s.csv_path,
        lap_idx:     lap ? lap.lap_idx : 0,
        video_paths: s.video_paths || [],
        sync_offset: sessionEffectiveOffset(s) ?? 0,
        source:      s.source || 'RaceBox',
        csv_start:   s.csv_start  || null,
      });
      Router.navigate('editor');
    });

    // AIM XRK conversion
    pane.querySelector('#dr-conv-btn')?.addEventListener('click', async () => {
      const btn = pane.querySelector('#dr-conv-btn');
      const msg = pane.querySelector('#dr-conv-msg');
      btn.disabled = true;
      if (msg) { msg.textContent = '转换中…'; msg.className = 'status-msg status-dim'; }
      try {
        const result = await API.convertXrkSession(s.csv_path);
        if (result && result.ok) {
          if (msg) { msg.textContent = 'Done — re-scanning…'; msg.className = 'status-msg status-ok'; }
          s.needs_conversion = false;
          await doScan(true);
        } else {
          if (msg) { msg.textContent = result?.error || '转换失败。'; msg.className = 'status-msg status-err'; }
          btn.disabled = false;
        }
      } catch (e) {
        if (msg) { msg.textContent = String(e); msg.className = 'status-msg status-err'; }
        btn.disabled = false;
      }
    });

    const _bindAssign = () => {
      const btn = pane.querySelector('#dr-assign-vid-btn');
      const msg = pane.querySelector('#dr-assign-vid-msg');
      if (!btn) return;
      // Manual video assignment (one or more clips; backend orders by recording time for export)
      btn.addEventListener('click', async () => {
      btn.disabled = true;
      const filter = ['视频文件 (*.mp4;*.mov;*.avi;*.mkv;*.MP4;*.MOV)'];
      let paths = await API.openFilesDialog(filter).catch(() => []);
      if (!paths || !paths.length) {
        const one = await API.openFileDialog(filter).catch(() => null);
        paths = one ? [one] : [];
      }
      if (!paths.length) { btn.disabled = false; return; }
      try {
        const res = await API.assignVideos(s.csv_path, paths);
        s.video_paths = (res && Array.isArray(res.video_paths)) ? res.video_paths : paths;
        s.matched     = true;
        if (msg && res?.warnings?.length) {
          msg.textContent = res.warnings.join(' ');
          msg.className = 'status-msg status-dim';
        }
        // Update previewSession so editor picks up the new video immediately
        const prev = State.get('previewSession');
        if (prev?.csv_path === s.csv_path) {
          State.set('previewSession', { ...prev, video_paths: s.video_paths, sync_offset: s.sync_offset ?? 0 });
        }
        await API.saveSessionsCache(_sessions).catch(() => {});
        renderRight();
        renderLeft();
        // Always trigger one auto-sync attempt after manual video assignment.
        await triggerAutoSyncForSession(s, '视频已绑定');
      } catch (e) {
        if (msg) { msg.textContent = String(e); msg.className = 'status-msg status-err'; }
        btn.disabled = false;
      }
      });
    };
    _bindAssign();

    // Clear manual binding
    pane.querySelector('#dr-clear-vid-btn')?.addEventListener('click', async () => {
      const msg = pane.querySelector('#dr-assign-vid-msg');
      try {
        await API.assignVideos(s.csv_path, []);
        s.video_paths = [];
        s.matched = false;
        const prev = State.get('previewSession');
        if (prev?.csv_path === s.csv_path) {
          State.set('previewSession', { ...prev, video_paths: [], sync_offset: s.sync_offset ?? 0 });
        }
        await API.saveSessionsCache(_sessions).catch(() => {});
        if (msg) { msg.textContent = '已清除绑定。'; msg.className = 'status-msg status-dim'; }
        renderRight();
        renderLeft();
      } catch (e) {
        if (msg) { msg.textContent = String(e); msg.className = 'status-msg status-err'; }
      }
    });

    // Bike mode
    pane.querySelector('#dr-bike-sel')?.addEventListener('change', async e => {
      s.is_bike = e.target.value === 'bike';
      const cfg = await API.getConfig();
      const overrides = { ...(cfg.bike_overrides || {}) };
      overrides[s.csv_path] = s.is_bike;
      await API.saveConfig({ bike_overrides: overrides });
    });

    // Video sync
    wireVideoSync(s, pane);
    // Safety: auto-sync overlay may have locked controls before this pane was rebuilt.
    // Refresh it once after wiring so scrub/buttons are usable if auto-sync is no longer running.
    try { _updateAutoSyncOverlayIfVisible(s.csv_path); } catch (_) {}

    // Session offset — same UX as track: inline edit in「本节信息」, applies seek on video
    pane.querySelector('#dr-off-edit-btn')?.addEventListener('click', () => {
      const wrap = pane.querySelector('#dr-off-edit-wrap');
      if (!wrap) return;
      const cur = s.sync_offset != null && Number.isFinite(Number(s.sync_offset)) ? Number(s.sync_offset) : null;
      const curStr = cur != null ? String(cur) : '0';
      wrap.innerHTML = `
        <input type="number" step="0.001" class="input-field" id="dr-off-input" value="${esc(curStr)}"
               style="width:110px;font-size:11px;padding:2px 6px;font-family:var(--mono)">
        <button type="button" class="btn btn-sm" id="dr-off-apply" style="padding:1px 8px">应用</button>
        <button type="button" class="btn btn-sm" id="dr-off-cancel" style="padding:1px 6px">✕</button>
      `;
      const inp = pane.querySelector('#dr-off-input');
      inp?.focus();
      inp?.select?.();

      const cancel = () => { renderRight(); };
      const apply = async () => {
        const v = Number(inp?.value);
        if (!Number.isFinite(v)) return;
        const fn = pane._openlapApplyOffset;
        if (typeof fn !== 'function') {
          setStatus('视频面板未就绪，请稍候再试');
          cancel();
          return;
        }
        await fn(v);
        // Do NOT re-render the whole panel here: wireVideoSync() re-initializes
        // from saved sync_offset and would clobber the just-applied preview offset (_temp).
        // Instead, update only the small "saved/preview" badge.
        const markEl = pane.querySelector('#sv-mark-val');
        if (markEl) {
          const offPreview = sessionOffsetIsPreview(s);
          const isAuto = syncIsAutoFamily(s.sync_source);
          const savedAlignOff =
            s.sync_offset != null && Number.isFinite(Number(s.sync_offset)) ? Number(s.sync_offset) : null;
          markEl.textContent = offPreview ? '预览' : (savedAlignOff != null && !isAuto ? '✓ saved' : '');
        }
      };
      pane.querySelector('#dr-off-apply')?.addEventListener('click', apply);
      pane.querySelector('#dr-off-cancel')?.addEventListener('click', cancel);
      inp?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); apply(); }
        if (e.key === 'Escape') { e.preventDefault(); cancel(); }
      });
    });

    // Manual lap tag toggles
    pane.querySelectorAll('.lap-tag-toggle').forEach(el => {
      el.addEventListener('change', async e => {
        const lapNum = parseInt(e.target.dataset.lapNum);
        const tag = e.target.dataset.tag;
        const enabled = !!e.target.checked;
        try {
          await API.setLapTag(s.csv_path, lapNum, tag, enabled);
          _lapDetails[s.csv_path] = await API.getLaps(s.csv_path);
          _meta[s.csv_path] = await API.getSessionMeta(s.csv_path);
          recomputeDayBest();
          renderLeft();
          if (_selCsv === s.csv_path) renderRight();
        } catch (err) {
          setStatus('圈标签设置失败：' + String(err));
        }
      });
    });
  }

  function wireVideoSync(s, pane) {
    const frameEl = pane.querySelector('#sync-frame');
    const liveVideoEl = pane.querySelector('#sync-video-live');
    const frameWrapEl = frameEl?.closest?.('.sync-frame-wrap') || null;
    const loadingEl = pane.querySelector('#sync-loading');
    const loadingTextEl = pane.querySelector('#sync-loading-text');
    const scrub = pane.querySelector('#sv-scrub');
    const timeEl = pane.querySelector('#sv-time');
    const markEl = pane.querySelector('#sv-mark-val');
    const refLapSel = pane.querySelector('#sv-ref-lap');
    const playBtn = pane.querySelector('#sv-play');
    const autoSyncBtn = pane.querySelector('#sv-auto-sync');
    const preciseModeEl = pane.querySelector('#sv-precise-mode');
    if (!frameEl) return;

    const videoPath = s.video_paths?.[0];
    if (!videoPath) return;

    // Generation guard: session switching can leave old async init tasks running.
    // Only the latest wireVideoSync call for this pane is allowed to mutate global
    // decode session state or update the scrubber/video elements.
    const myGen = (pane._openlapSyncGen || 0) + 1;
    pane._openlapSyncGen = myGen;
    const alive = () => (
      _container && pane && pane.isConnected && pane._openlapSyncGen === myGen
      && liveVideoEl && liveVideoEl.isConnected
    );

    function applyVideoAspectRatioFromMeta() {
      try {
        if (!alive()) return;
        if (!frameWrapEl || !liveVideoEl) return;
        const vw = Number(liveVideoEl.videoWidth || 0);
        const vh = Number(liveVideoEl.videoHeight || 0);
        if (!(vw > 0 && vh > 0)) return;
        frameWrapEl.style.setProperty('--sync-ar', `${vw} / ${vh}`);
      } catch (_) {}
    }

    // Ensure we start from the video layer (avoids JPEG/img blank screens).
    try { liveVideoEl && (liveVideoEl.style.display = 'block'); } catch (_) {}
    try { frameEl && (frameEl.style.display = 'none'); } catch (_) {}
    dbgSyncVideo('wireVideoSync_init', {
      csv: s.csv_path,
      videoPath,
      autoSyncing: _autoSyncing,
      autoSyncState: _autoSyncByCsv?.[s.csv_path] || null,
      video: _dbgVideoState(liveVideoEl),
    });
    logSyncSeek('wire_init', { has_video_el: !!liveVideoEl, has_scrub: !!scrub });

    let fps = 30;
    let frameCount = 0;
    let curFrame = 0;
    let busy = false;
    let decoderSessionId = '';
    let playing = false;
    // Guard against WebView2 firing pause during play startup (can abort play() promises).
    let playStarting = false;
    let playTimer = null;
    let playStartFrame = 0;
    let playStartMs = 0;
    let decodeSeq = 0;
    // Calibration playback: default to <video> pipeline to match editor preview.
    // Only switch to OpenCV/JPEG when we explicitly need shutter-encoder-like frame jumps.
    let liveMode = true;
    let gpuReady = false;
    /** Scrub on OpenCV path: coalesce rapid input to latest frame only. */
    let scrubDecodeBusy = false;
    let scrubDecodeQueuedTarget = null;
    /** Debounced OpenCV JPEG refresh after native <video> seeks (keeps UI snappy like old pure-video build). */
    let alignJpegTimer = null;
    let preciseMode = !!_config?.session_info?.[s.csv_path]?.sync_precise_mode;
    /** User ±1s/±1f / 换参考圈等：锁步进按钮防连点，并显示「定位中」；不锁进度条以免拖条丢事件 */
    let syncSeekUiHeld = false;
    /** 拖条按住期间同步锁步进（不可能边拖边点 ± 帧）；与 syncSeekUiHeld 叠加时由两者共同决定何时解锁 */
    let scrubPointerHeld = false;
    const seekStepButtonSelectors = ['#sv-mm', '#sv-m', '#sv-p', '#sv-pp'];
    function setSeekStepButtonsDisabled(disabled) {
      seekStepButtonSelectors.forEach(sel => {
        const el = pane.querySelector(sel);
        if (el) el.disabled = !!disabled;
      });
      if (refLapSel) refLapSel.disabled = !!disabled;
    }
    function refreshSeekStepButtonsDisabled() {
      setSeekStepButtonsDisabled(syncSeekUiHeld || scrubPointerHeld);
    }
    function beginSyncSeekUi() {
      if (syncSeekUiHeld) return false;
      syncSeekUiHeld = true;
      refreshSeekStepButtonsDisabled();
      setLoading(true, '定位中…');
      return true;
    }
    function endSyncSeekUi() {
      if (!syncSeekUiHeld) return;
      syncSeekUiHeld = false;
      if (!scrubPointerHeld) setSeekStepButtonsDisabled(false);
      setLoading(false);
    }

    function stopBeforeSeek() {
      if (playing) stopPlayback();
      else stopPlayback({ bumpDecode: false });
    }
    /** Same idea as preview: prefer browser <video> seeking; OpenCV only for precise/fallback. */
    function canUseNativeVideoSeek() {
      if (!liveVideoEl || !videoPath) return false;
      try {
        return liveVideoEl.readyState >= 1
          && Number.isFinite(liveVideoEl.duration)
          && liveVideoEl.duration > 0;
      } catch (_) {
        return false;
      }
    }
    function scheduleAlignJpegRefresh() {
      if (!decoderSessionId) return;
      if (alignJpegTimer) clearTimeout(alignJpegTimer);
      alignJpegTimer = setTimeout(async () => {
        alignJpegTimer = null;
        try {
          // Non-precise jumps must stay on <video> pipeline.
          // Only allow OpenCV/JPEG shutter-encoder-like refresh when user enables 精确模式.
          if (!preciseMode) return;
          if (!frameEl?.isConnected || playing || liveMode) return;
          await decodeToFrameCpu(curFrame, true);
        } catch (_) { /* ignore */ }
      }, 45);
    }
    /** Wait for in-flight OpenCV decode so GPU play / seek does not race libavcodec (Windows pthread_frame assert). */
    async function awaitDecodeIdle() {
      for (let i = 0; i < 150 && busy; i++) {
        await new Promise((r) => setTimeout(r, 10));
      }
    }
    /** 对齐 seek：优先拖条/预览的临时 offset，否则已保存值（与 sessionEffectiveOffset 一致） */
    const baselineOrActiveOffsetForSeek = () => {
      const eff = sessionEffectiveOffset(s);
      return eff != null && Number.isFinite(eff) ? eff : 0;
    };
    const setTempOffset = (val) => {
      const n = Number(val);
      if (!Number.isFinite(n)) return;
      // Avoid poisoning the session with a bogus temp offset when the video
      // timeline is not ready yet (no GPU readiness + no decode session/frame_count).
      // In that state curFrame often stays 0, so derived offsets collapse to -refElapsed.
      if (!gpuReady && !decoderSessionId && !(frameCount > 0)) return;
      s._temp_sync_offset = n;
      // Keep previewSession offset in sync (preview only; not persisted until mark).
      const prev = State.get('previewSession');
      if (prev && prev.csv_path === s.csv_path) {
        State.set('previewSession', { ...prev, sync_offset: n });
      }
    };

    function setLoading(show, text = '视频加载中…') {
      if (loadingTextEl) loadingTextEl.textContent = text;
      if (loadingEl) loadingEl.style.display = show ? 'flex' : 'none';
    }

    function fmtVTime(t) {
      const m = Math.floor(t / 60);
      const sec = (t % 60).toFixed(3).padStart(6, '0');
      return `${m}:${sec}`;
    }
    function frameToTime(idx) {
      const f = Math.max(1e-6, Number(fps) || 30);
      return Math.max(0, idx / f);
    }
    function timeToFrame(t) {
      const f = Math.max(1e-6, Number(fps) || 30);
      return Math.max(0, Math.round(Math.max(0, Number(t) || 0) * f));
    }
    function maxFrame() {
      const fc = Number(frameCount || 0);
      if (Number.isFinite(fc) && fc > 0) return Math.max(0, fc - 1);
      // Fallback: if OpenCV frame_count is not available yet, approximate from
      // <video> duration so the scrubber doesn't get stuck at 0 for later sessions.
      try {
        const d = Number(liveVideoEl?.duration);
        const f = Math.max(1e-6, Number(fps) || 30);
        if (Number.isFinite(d) && d > 0.25) {
          return Math.max(0, Math.round(d * f) - 1);
        }
      } catch (_) {}
      return 0;
    }
    /** Prefer OpenCV frame_count; before it arrives use <video> duration so seek clamps are not stuck at 0s. */
    function videoDurationSec() {
      const fromFrames = frameToTime(Math.max(0, maxFrame()));
      if (fromFrames > 0.25) return fromFrames;
      try {
        const d = Number(liveVideoEl?.duration);
        if (Number.isFinite(d) && d > 0.25) return d;
      } catch (_) {}
      return Math.max(fromFrames, 0);
    }
    /** Clamp telemetry-derived video time to file bounds [0, duration]. */
    function clampVideoTimeSec(tSec) {
      const t = Number(tSec) || 0;
      const d = videoDurationSec();
      // If duration is unknown (0), only clamp to >=0 so offset math can proceed
      // without collapsing everything to 0s.
      if (!(d > 0.25)) return Math.max(0, t);
      return Math.max(0, Math.min(d, t));
    }
    /**
     * When showing JPEG preview (liveMode off), keep the hidden <video> seeked to curFrame
     * so「应用 offset」后切 GPU / ±帧 仍从正确时间线起步。
     */
    function syncHiddenLiveVideoFromCurFrame() {
      if (!liveVideoEl || !videoPath) return;
      if (liveMode) return;
      if (playing) return;
      try {
        const t = frameToTime(Math.max(0, Math.min(maxFrame(), curFrame)));
        if (Number.isFinite(t)) liveVideoEl.currentTime = t;
      } catch (_) {}
    }

    const safePlay = async (v) => {
      if (_pageUnmounted) return;
      if (!v || !v.isConnected || !videoPath) return;
      try {
        const p = v.play();
        if (p && typeof p.catch === 'function') {
          await p.catch((err) => {
            // Important: surface play() failures (policy / decode / network) for diagnosis.
            logSyncVideoUI('video_play_rejected', {
              name: err?.name,
              message: String(err?.message || err || ''),
              video: _dbgVideoState(v),
            });
          });
        }
      } catch (err) {
        logSyncVideoUI('video_play_throw', {
          name: err?.name,
          message: String(err?.message || err || ''),
          video: _dbgVideoState(v),
        });
      }
    };

    async function warmUpLivePaintOrFallback() {
      if (!liveVideoEl || !videoPath) return;
      dbgSyncVideo('warmUp_begin', {
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl),
      });
      // Wait for the seek to settle (avoid racing paint).
      try {
        await new Promise(resolve => {
          let done = false;
          const finish = () => {
            if (done) return;
            done = true;
            clearTimeout(timer);
            resolve();
          };
          const onSeeked = () => finish();
          const timer = setTimeout(finish, 800);
          try { liveVideoEl.addEventListener('seeked', onSeeked, { once: true }); } catch (_) {}
          // If seeked already fired, timeout will resolve.
        });
      } catch (_) {}
      // Prefer a paint signal without starting playback.
      // If supported, requestVideoFrameCallback is the cleanest way to wait for a rendered frame.
      try {
        const v = liveVideoEl;
        if (v && typeof v.requestVideoFrameCallback === 'function') {
          await new Promise(resolve => {
            let done = false;
            const fin = () => { if (done) return; done = true; resolve(); };
            try { v.requestVideoFrameCallback(() => fin()); } catch (_) { fin(); }
            setTimeout(fin, 250);
          });
        } else {
          // Fallback: a couple of RAFs usually lets the compositor catch up.
          await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        }
      } catch (_) {}
      // As a last resort, nudge layout/compositor without playing.
      try {
        const prevVis = liveVideoEl.style.visibility;
        liveVideoEl.style.visibility = 'hidden';
        void liveVideoEl.offsetHeight;
        liveVideoEl.style.visibility = prevVis || 'visible';
      } catch (_) {}
      dbgSyncVideo('warmUp_end', {
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl),
      });
    }
    function applyMeta(forceWriteTemp = false) {
      if (scrub) {
        scrub.min = '0';
        // maxFrame() falls back to duration*fps when OpenCV frame_count is not ready yet.
        scrub.max = String(maxFrame());
        scrub.step = '1';
        // Do not fight the native range thumb while the user is dragging (WebView2 + timeupdate).
        if (!scrubPointerHeld) scrub.value = String(curFrame);
      }
      const t = frameToTime(curFrame);
      if (timeEl) timeEl.textContent = fmtVTime(t);
      // Readout: video_time at curFrame minus ref lap → implied sync offset (只读条 + 本节信息)
      const ro = pane.querySelector('#sv-off-readout');
      if (ro) {
        const refElapsed = getRefLapElapsed();
        const off = t - refElapsed;
        const sign = off >= 0 ? '+' : '';
        ro.textContent = `${sign}${off.toFixed(3)}s`;
        // Only write preview offset when the user is actively scrubbing (forceWriteTemp=true).
        // Programmatic seeks (init / ref-lap jump) should never mutate _temp_sync_offset,
        // otherwise the session can get "self-locked" into vt=0 via off=t-refElapsed at frame 0.
        if (forceWriteTemp) setTempOffset(off);
      }
      syncHiddenLiveVideoFromCurFrame();
    }
    function updatePlayBtn() {
      if (!playBtn) return;
      playBtn.textContent = playing ? '⏸ 暂停' : '▶ 播放';
      playBtn.title = playing ? '暂停预览' : '播放预览';
    }
    function switchToLiveMode() {
      if (!liveVideoEl) return;
      liveMode = true;
      liveVideoEl.style.display = 'block';
      frameEl.style.display = 'none';
      try { liveVideoEl.classList.add('ready'); } catch (_) {}
      setLoading(false);
      dbgSyncVideo('switchToLiveMode', {
        liveMode,
        gpuReady,
        curFrame,
        liveVideoDisplay: liveVideoEl.style.display,
        frameDisplay: frameEl?.style?.display,
        video: _dbgVideoState(liveVideoEl),
      });
    }
    function switchToFrameMode() {
      if (!liveVideoEl) return;
      liveMode = false;
      liveVideoEl.style.display = 'none';
      frameEl.style.display = 'block';
      try { liveVideoEl.classList.remove('ready'); } catch (_) {}
      dbgSyncVideo('switchToFrameMode', {
        liveMode,
        gpuReady,
        curFrame,
        liveVideoDisplay: liveVideoEl.style.display,
        frameDisplay: frameEl?.style?.display,
        video: _dbgVideoState(liveVideoEl),
      });
    }
    /**
     * @param {{ bumpDecode?: boolean }} [opts]
     * bumpDecode=false: user pressed pause — do not bump decodeSeq so in-flight decode can finish
     *   and curFrame stays exactly where playback stopped (for frame-by-frame tuning).
     */
    function stopPlayback(opts) {
      const bumpDecode = opts?.bumpDecode !== false;
      playing = false;
      if (playTimer) {
        clearTimeout(playTimer);
        playTimer = null;
      }
      // Only trust <video>.currentTime when we were actually showing the live layer.
      // In JPEG/frame playback liveMode is false and the hidden video often stays at 0 — syncing from it would snap the scrub to the start.
      const wasLiveMode = liveMode;
      if (bumpDecode) decodeSeq += 1;
      if (liveVideoEl) {
        try { liveVideoEl.pause(); } catch (_) {}
        if (wasLiveMode) {
          const t = Number(liveVideoEl.currentTime || 0);
          if (Number.isFinite(t)) {
            curFrame = Math.max(0, Math.min(maxFrame(), timeToFrame(t)));
            applyMeta();
          }
        }
      }
      // Keep live frame visible after pause to avoid black flash.
      if (wasLiveMode) switchToLiveMode();
      else switchToFrameMode();
      updatePlayBtn();
      dbgSyncVideo('stopPlayback', {
        bumpDecode,
        wasLiveMode,
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl),
      });
    }
    async function startPlayback() {
      if (alignJpegTimer) {
        clearTimeout(alignJpegTimer);
        alignJpegTimer = null;
      }
      if (playing) return;
      playing = true;
      playStarting = true;
      updatePlayBtn();

      if (!liveVideoEl || !videoPath) {
        setLoading(true, '视频未就绪（等待播放器初始化…）');
        playing = false;
        updatePlayBtn();
        return;
      }

      async function ensureLiveReady() {
        try {
          const expected = videoUrl(videoPath);
          if (liveVideoEl.src !== expected) {
            liveVideoEl.src = expected;
            liveVideoEl.load();
          }
          if (liveVideoEl.readyState >= 2) return true;
          return await new Promise(resolve => {
            let done = false;
            const finish = (ok) => {
              if (done) return;
              done = true;
              liveVideoEl.removeEventListener('loadeddata', onReady);
              liveVideoEl.removeEventListener('canplay', onReady);
              liveVideoEl.removeEventListener('error', onErr);
              clearTimeout(timer);
              resolve(!!ok);
            };
            const onReady = () => finish(true);
            const onErr = () => finish(false);
            const timer = setTimeout(() => finish(false), 1500);
            liveVideoEl.addEventListener('loadeddata', onReady, { once: true });
            liveVideoEl.addEventListener('canplay', onReady, { once: true });
            liveVideoEl.addEventListener('error', onErr, { once: true });
          });
        } catch (_) {
          return false;
        }
      }

      const ready = await ensureLiveReady();
      if (!playing) return;
      if (!ready) {
        setLoading(true, '视频未就绪（请稍候或关闭“播放”改用静态帧校准）');
        playing = false;
        updatePlayBtn();
        dbgSyncVideo('startPlayback_notReady', {
          playing,
          liveMode,
          gpuReady,
          curFrame,
          video: _dbgVideoState(liveVideoEl),
        });
        return;
      }

      try { switchToLiveMode(); } catch (_) {}
      gpuReady = true;

      // Keep <video> current frame in sync.
      try {
        const tPlay = frameToTime(curFrame);
        liveVideoEl.currentTime = tPlay;
      } catch (_) {}

      dbgSyncVideo('startPlayback_beforePlay', {
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl, { tPlay: frameToTime(curFrame) }),
      });
      await safePlay(liveVideoEl);
      // Diagnose/force paint: sometimes WebView2 advances currentTime but shows black.
      try {
        const v = liveVideoEl;
        const loadingBox = _domBox(loadingEl);
        const frameBox = _domBox(frameEl);
        const videoBox = _domBox(v);
        logSyncVideoUI('after_play_paint_check', {
          playing,
          liveMode,
          gpuReady,
          curFrame,
          video: _dbgVideoState(v),
          boxes: { video: videoBox, frame: frameBox, loading: loadingBox },
        });
        let gotFrameCb = false;
        if (v && typeof v.requestVideoFrameCallback === 'function') {
          v.requestVideoFrameCallback(() => {
            gotFrameCb = true;
            logSyncVideoUI('rvfc_fired', { video: _dbgVideoState(v), box: _domBox(v) });
          });
        }
        setTimeout(() => {
          if (_pageUnmounted) return;
          // If no RVFC fired quickly, try a visibility toggle to force repaint.
          if (!gotFrameCb && v && v.isConnected) {
            try {
              const prevVis = v.style.visibility;
              v.style.visibility = 'hidden';
              // Force layout
              void v.offsetHeight;
              v.style.visibility = prevVis || 'visible';
              logSyncVideoUI('force_repaint_visibility_toggle', { video: _dbgVideoState(v), box: _domBox(v) });
            } catch (_) {}
          }
        }, 350);
      } catch (_) {}
      dbgSyncVideo('startPlayback_afterPlay', {
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl),
      });
      // If play didn't actually start, clear starting flag shortly so pause can work normally.
      setTimeout(() => { playStarting = false; }, 500);
    }
    /** OpenCV JPEG path only (used when <video> GPU preview is unusable). */
    async function decodeToFrameCpu(targetFrame, quiet = false, forceWriteTemp = false) {
      if (!alive()) return;
      await awaitDecodeIdle();
      if (!alive()) return;
      switchToFrameMode();
      // Invalidate any in-flight seek for this panel so an older RPC cannot paint after a newer one
      // (e.g. 换参考圈时仍共用 decodeSeq=0 的旧解码会盖住新帧 — Python 已 seek 但 <img> 仍是上一段)。
      decodeSeq += 1;
      const mySeq = decodeSeq;
      busy = true;
      if (!quiet) setLoading(true, '正在解码帧…');
      try {
        if (!alive()) return;
        if (!decoderSessionId) {
          if (!quiet) {
            setStatus('无法解码：未建立 OpenCV 会话（视频路径可能无法打开或编码不支持）');
          }
          return;
        }
        const rsp = await API.decodeSessionSeek(decoderSessionId, targetFrame, null);
        if (!alive()) return;
        if (mySeq !== decodeSeq) return;
        if (!rsp?.ok) {
          frameEl.classList.remove('ready');
          if (!quiet) {
            setLoading(true, '视频帧解码失败');
            setStatus('帧解码失败：' + (rsp?.error || 'unknown'));
          }
          return;
        }
        if (!frameEl?.isConnected) return;
        if (Number(rsp.fps) > 0) fps = Number(rsp.fps);
        frameCount = Number(rsp.frame_count || frameCount || 0);
        curFrame = Math.max(0, Number(rsp.frame_idx || 0));
        frameEl.src = `data:image/jpeg;base64,${rsp.image_b64 || ''}`;
        frameEl.classList.add('ready');
        // Ensure the decoded JPEG is actually painted before hiding the loading overlay.
        // WebView2 can keep a black frame briefly even after src assignment.
        try {
          const img = frameEl;
          const timeoutMs = 900;
          const t0 = Date.now();
          const waitLoad = () => new Promise((resolve) => {
            let done = false;
            const fin = () => { if (done) return; done = true; resolve(); };
            try {
              if (img.complete && img.naturalWidth > 0) return fin();
            } catch (_) {}
            try { img.addEventListener('load', fin, { once: true }); } catch (_) {}
            try { img.addEventListener('error', fin, { once: true }); } catch (_) {}
            setTimeout(fin, Math.max(0, timeoutMs - (Date.now() - t0)));
          });
          // Prefer decode() when available; fall back to load event.
          if (typeof img.decode === 'function') {
            try {
              await Promise.race([
                img.decode(),
                new Promise((r) => setTimeout(r, timeoutMs)),
              ]);
            } catch (_) {}
          }
          await waitLoad();
        } catch (_) {}
        if (!alive()) return;
        if (mySeq !== decodeSeq) return;
        applyMeta(forceWriteTemp);
        logSyncSeek('opencv_paint', { curFrame, frameCount, fps, scrubMax: scrub ? scrub.max : null, scrubVal: scrub ? scrub.value : null });
      } finally {
        busy = false;
        if (!quiet) setLoading(false);
      }

      // Log only when paused and values change (avoid flooding).
      try {
        if (!playing) {
          const mx = scrub ? String(scrub.max) : '';
          const vv = scrub ? String(scrub.value) : '';
          if (pane._openlapLastMetaLog == null) pane._openlapLastMetaLog = {};
          const prev = pane._openlapLastMetaLog;
          if (prev.mx !== mx || prev.vv !== vv || prev.cf !== curFrame) {
            pane._openlapLastMetaLog = { mx, vv, cf: curFrame };
            logSyncSeek('applyMeta', {
              curFrame,
              frameCount,
              fps,
              maxFrame: maxFrame(),
              scrubMax: mx,
              scrubVal: vv,
              gpuReady,
              liveMode,
              videoTime: Number(liveVideoEl?.currentTime || 0),
            });
          }
        }
      } catch (_) {}
    }

    async function decodeToFrame(targetFrame, quiet = false, opts = {}) {
      if (!alive()) return;
      const forceWT = !!opts.forceWriteTemp;
      const forceOpenCv = !!opts.forceOpenCv;
      if (!forceOpenCv && gpuReady && liveVideoEl) {
        try {
          if (!alive()) return;
          const mf = maxFrame();
          const raw = Number(targetFrame || 0);
          const clamped = mf > 0 ? Math.max(0, Math.min(mf, raw)) : Math.max(0, raw);
          const t = frameToTime(clamped);
          logSyncSeek('decodeToFrame_gpu', {
            requestedFrame: raw,
            clampedFrame: clamped,
            tSec: t,
            forceWriteTemp: forceWT,
          });
          try { liveVideoEl.pause(); } catch (_) {}
          switchToLiveMode();
          try { liveVideoEl.classList.add('ready'); } catch (_) {}
          liveVideoEl.currentTime = t;
          curFrame = clamped;
          applyMeta(forceWT);
          decodeSeq += 1;
          dbgSyncVideo('decodeToFrame_gpu', {
            quiet,
            forceWriteTemp: forceWT,
            requestedFrame: raw,
            clampedFrame: clamped,
            tSec: t,
            playing,
            liveMode,
            gpuReady,
            curFrame,
            video: _dbgVideoState(liveVideoEl),
          });
          return;
        } catch (_) {
          // live path unavailable, fall through
        }
      }
      logSyncSeek('decodeToFrame_opencv', {
        requestedFrame: Number(targetFrame || 0),
        forceWriteTemp: forceWT,
      });
      await decodeToFrameCpu(targetFrame, quiet, forceWT);
    }
    async function decodeToFrameScrubCoalesced(targetFrame) {
      scrubDecodeQueuedTarget = Number(targetFrame || 0);
      if (scrubDecodeBusy) return;
      scrubDecodeBusy = true;
      try {
        while (scrubDecodeQueuedTarget != null) {
          const latest = scrubDecodeQueuedTarget;
          scrubDecodeQueuedTarget = null;
          await decodeToFrame(latest, false, { forceWriteTemp: true });
        }
      } finally {
        scrubDecodeBusy = false;
      }
    }

    function getRefLapElapsed() {
      const laps = _lapDetails[s.csv_path] || [];
      if (!laps.length) return 0;
      const refLapNum = Number(refLapSel?.value || _config?.session_info?.[s.csv_path]?.sync_ref_lap_num || 0);
      const picked = laps.find(l => Number(l.lap_num) === refLapNum);
      if (picked) return picked.elapsed_start || 0;
      // If selection/config is missing or invalid, fall back to the first lap (including outlap).
      return (laps[0]?.elapsed_start) || 0;
    }

    /**
     * 若 off+参考圈起点 落不到视频时间线内，自动切换到一个更合适的参考圈（仅本次 UI 默认，不自动写配置）。
     * 口径：默认参考圈选「第一个起点落在视频时间线内的圈」(vt >= 0)；
     * 若已知视频时长，尽量保证 vt <= duration。
     */
    async function ensureRefLapFitsVideoTimeline(offForPick) {
      const laps = _lapDetails[s.csv_path] || [];
      if (!laps.length) return;
      const o = Number(offForPick);
      if (!Number.isFinite(o)) return;
      const d = videoDurationSec();
      const minVt = 0;
      const maxVt = (d > 0.25) ? Math.max(0, d - 0.25) : null;
      const cfg = _config?.session_info?.[s.csv_path] || {};
      const defaultRef = (laps.find(l => !l.is_outlap && !l.is_inlap)?.lap_num)
        ?? laps.find(l => !l.is_outlap)?.lap_num
        ?? laps[0]?.lap_num
        ?? 1;
      const preferred = Number(refLapSel?.value || cfg.sync_ref_lap_num || defaultRef) || Number(defaultRef);
      const prefLap = laps.find(l => Number(l.lap_num) === preferred);
      const prefE = prefLap ? (Number(prefLap.elapsed_start) || 0) : 0;
      const prefVt = o + prefE;
      if (prefVt >= (minVt - 1e-3) && (maxVt == null || prefVt <= (maxVt + 1e-3))) return;
      const newNum = pickAlignRefLapNumForOffset(o, laps, preferred, { minVt, maxVt });
      if (newNum === preferred) return;
      try {
        if (refLapSel) refLapSel.value = String(newNum);
      } catch (_) { /* keep UI ref on failure */ }
    }

    // For auto-sync confirmation: always align to the first lap (including outlap).
    function getAutoConfirmRefElapsed() {
      const laps = _lapDetails[s.csv_path] || [];
      return (laps[0]?.elapsed_start) || 0;
    }
    let _lastSyncSeekLogMs = 0;
    function logSyncSeek(tag, extra = {}) {
      try {
        const now = Date.now();
        // Throttle to avoid flooding logs on scrub/timeupdate.
        if (now - _lastSyncSeekLogMs < 120) return;
        _lastSyncSeekLogMs = now;
        API.debugSyncSeek(tag, {
          csv_path: s.csv_path,
          video_path: videoPath,
          gen: pane?._openlapSyncGen || 0,
          ...extra,
        }).catch(() => {});
      } catch (_) { /* ignore */ }
    }
    async function seekToRefLap() {
      if (alignJpegTimer) {
        clearTimeout(alignJpegTimer);
        alignJpegTimer = null;
      }
      stopBeforeSeek();
      const off = baselineOrActiveOffsetForSeek();
      await ensureRefLapFitsVideoTimeline(off);
      const refLapNumNow = Number(refLapSel?.value || _config?.session_info?.[s.csv_path]?.sync_ref_lap_num || 0) || 0;
      const refE = getRefLapElapsed();
      const vt = clampVideoTimeSec(off + refE);
      const target = timeToFrame(vt);
      logSyncSeek('seekToRefLap', {
        offsetUsed: off,
        refLapNum: refLapNumNow,
        refLapElapsed: refE,
        vtTargetSec: vt,
        targetFrame: target,
        gpuLive: !!(gpuReady && liveVideoEl),
      });
      dbgSyncVideo('seekToRefLap', {
        off,
        refE,
        vt,
        target,
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl),
      });
      // Video-first: use <video> when gpuReady, otherwise fall back to OpenCV/JPEG.
      await decodeToFrame(target, true);
      // If <video> path was used and we're paused, WebView2 may need a small warm-up.
      if (gpuReady && liveMode) {
        try { await warmUpLivePaintOrFallback(); } catch (_) {}
      }
    }

    (async () => {
      if (!alive()) return;
      // Ensure only one persistent decode session stays alive for Data page.
      let opened = null;
      if (_syncDecoderSessionId && (_syncDecoderVideoPath !== videoPath)) {
        if (!alive()) return;
        await API.closeDecodeSession(_syncDecoderSessionId).catch(() => {});
        _syncDecoderSessionId = '';
        _syncDecoderVideoPath = '';
      }
      // Always open decode session to populate fps/frame_count for the scrubber.
      // We prefer <video> for rendering (so this doesn't decode frames unless we fall back).
      if (!_syncDecoderSessionId) {
        if (!alive()) return;
        opened = await API.openDecodeSession(videoPath, 10).catch(() => null);
        if (!alive()) return;
        if (opened?.session_id) {
          _syncDecoderSessionId = opened.session_id;
          _syncDecoderVideoPath = videoPath;
          if (Number(opened?.fps) > 0) fps = Number(opened.fps);
          if (Number(opened?.frame_count) > 0) frameCount = Number(opened.frame_count);
        }
      }
      decoderSessionId = _syncDecoderSessionId;
      if (!decoderSessionId && opened && opened.ok === false && opened.error) {
        setStatus(`OpenCV 无法打开视频：${opened.error}（将尝试浏览器预览；仍失败请开「精确模式」）`);
      }
      try {
        if (!alive()) return;
        const info = await API.getVideoFps(videoPath);
        if (!alive()) return;
        if (info?.ok && Number(info.fps) > 0) fps = Number(info.fps);
        if (info?.ok && Number(info.frame_count) > 0) frameCount = Number(info.frame_count);
      } catch (_) {}
      if (Number.isFinite(opened?.fps) && Number(opened.fps) > 0) fps = Number(opened.fps);
      if (liveVideoEl && videoPath) {
        try {
          if (!alive()) return;
          liveVideoEl.src = videoUrl(videoPath);
          liveVideoEl.load();
          await new Promise(resolve => {
            let done = false;
            const finish = () => {
              if (done) return;
              done = true;
              liveVideoEl.removeEventListener('loadedmetadata', onReady);
              liveVideoEl.removeEventListener('canplay', onReady);
              liveVideoEl.removeEventListener('error', onErr);
              clearTimeout(timer);
              resolve();
            };
            const onReady = () => { gpuReady = true; finish(); };
            const onErr = () => { gpuReady = false; finish(); };
            const timer = setTimeout(finish, 1500);
            liveVideoEl.addEventListener('loadedmetadata', onReady, { once: true });
            liveVideoEl.addEventListener('canplay', onReady, { once: true });
            liveVideoEl.addEventListener('error', onErr, { once: true });
          });
          if (gpuReady) {
            if (!alive()) return;
            switchToLiveMode();
            // currentTime assignment is async; do not read liveVideoEl.currentTime here or applyMeta()
            // will infer the wrong offset and clobber sync_offset via setTempOffset (baseline → 推算错位).
            await ensureRefLapFitsVideoTimeline(baselineOrActiveOffsetForSeek());
            const vt0 = clampVideoTimeSec(baselineOrActiveOffsetForSeek() + getRefLapElapsed());
            // Seek to the aligned frame and WAIT for it to land before declaring ready,
            // otherwise WebView2 may stay black until the next user interaction.
            try { setLoading(true, '定位到对齐帧…'); } catch (_) {}
            try { liveVideoEl.classList.remove('ready'); } catch (_) {}
            liveVideoEl.currentTime = vt0;
            try {
              await new Promise(resolve => {
                let done = false;
                const finish = () => { if (done) return; done = true; resolve(); };
                const onSeeked = () => finish();
                try { liveVideoEl.addEventListener('seeked', onSeeked, { once: true }); } catch (_) {}
                setTimeout(finish, 900);
              });
            } catch (_) {}
            if (!alive()) return;

            // After seek settles, derive curFrame from the actual video time.
            const tSnap0 = Number(liveVideoEl.currentTime || vt0);
            const tf0 = timeToFrame(tSnap0);
            const mf0 = maxFrame();
            curFrame = mf0 > 0 ? Math.max(0, Math.min(mf0, tf0)) : tf0;
            logSyncSeek('init_seek_snap', { vt0, tSnap0, tf0, mf0, curFrame, scrubMax: scrub ? scrub.max : null });
            applyMeta();
            logSyncSeek('init_after_applyMeta', { scrubMax: scrub ? scrub.max : null, scrubVal: scrub ? scrub.value : null, curFrame });
            applyVideoAspectRatioFromMeta();
            try { liveVideoEl.classList.add('ready'); } catch (_) {}
            // WebView2 may report metadata before first paint; don't eagerly fall back to JPEG.
            await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
            if (!alive()) return;
            const vw = liveVideoEl.videoWidth || 0;
            const vh = liveVideoEl.videoHeight || 0;
            if (vw >= 2 && vh >= 2) {
              const tSnap = Number(liveVideoEl.currentTime || 0);
              if (Number.isFinite(tSnap) && (tSnap > 0.02 || vt0 <= 0.02)) {
                const drift = Math.abs(tSnap - vt0);
                if (drift < 0.25 + 1 / Math.max(6, Number(fps) || 30)) {
                  const mfs = maxFrame();
                  const tfs = timeToFrame(tSnap);
                  curFrame = mfs > 0 ? Math.max(0, Math.min(mfs, tfs)) : tfs;
                }
              }
              applyMeta();
            }
            // After initial seek + metadata init, ensure a frame is painted in paused state.
            if (!playing) {
              try { await warmUpLivePaintOrFallback(); } catch (_) {}
            }
            try { setLoading(false); } catch (_) {}
            logSyncSeek('init_panel_gpu', {
              vt0TargetSec: vt0,
              curFrameAfterInit: curFrame,
              liveVideoCurrentTime: Number(liveVideoEl?.currentTime || 0),
              videoWxH: `${liveVideoEl?.videoWidth || 0}x${liveVideoEl?.videoHeight || 0}`,
              gpuReadyStill: gpuReady,
            });
          }
        } catch (_) {
          gpuReady = false;
        }
      }
      // If GPU init failed later, we already have (or re-used) decode session for fallback.
      if (!gpuReady) {
        if (!alive()) return;
        await seekToRefLap();
      }
      if (!decoderSessionId && !gpuReady) {
        setStatus('校准视频无法打开：OpenCV 与浏览器预览均失败，请检查网络路径/编码或转为 H.264');
      }
      setLoading(false);
      if (liveVideoEl && videoPath) {
        try {
          // Debug: always attach listeners; handler checks the toggle at runtime.
          const handler = (ev) => {
            if (!_dbgSyncVideoEnabled()) return;
            dbgSyncVideo('video_event_' + ev.type, _dbgVideoState(liveVideoEl));
          };
          const types = [
            'loadedmetadata', 'loadeddata', 'canplay', 'canplaythrough',
            'playing', 'pause', 'waiting', 'stalled', 'suspend',
            'seeking', 'seeked', 'timeupdate', 'ended', 'error',
          ];
          types.forEach(t => {
            try { liveVideoEl.addEventListener(t, handler); } catch (_) {}
          });
          dbgSyncVideo('video_listeners_attached', _dbgVideoState(liveVideoEl));

          liveVideoEl.addEventListener('timeupdate', () => {
            if (!liveMode) return;
            const t = Number(liveVideoEl.currentTime || 0);
            if (!Number.isFinite(t)) return;
            curFrame = Math.max(0, Math.min(maxFrame(), timeToFrame(t)));
            applyMeta();
          });
          liveVideoEl.addEventListener('playing', () => {
            // Video has rendered enough to be visible.
            playStarting = false;
            applyVideoAspectRatioFromMeta();
            try { liveVideoEl.classList.add('ready'); } catch (_) {}
            dbgSyncVideo('video_event_playing', _dbgVideoState(liveVideoEl));
          });
          liveVideoEl.addEventListener('loadeddata', () => {
            applyVideoAspectRatioFromMeta();
            try { liveVideoEl.classList.add('ready'); } catch (_) {}
          });
          liveVideoEl.addEventListener('canplay', () => {
            applyVideoAspectRatioFromMeta();
            try { liveVideoEl.classList.add('ready'); } catch (_) {}
          });
          liveVideoEl.addEventListener('seeked', () => {
            if (!liveMode) return;
            const t = Number(liveVideoEl.currentTime || 0);
            if (!Number.isFinite(t)) return;
            curFrame = Math.max(0, Math.min(maxFrame(), timeToFrame(t)));
            applyMeta();
          });
          liveVideoEl.addEventListener('ended', () => {
            if (playing) stopPlayback();
          });
          liveVideoEl.addEventListener('pause', () => {
            // If pause fires during play startup, don't treat it as a user pause (avoids AbortError loops).
            if (playStarting) return;
            if (!playing) return;
            stopPlayback({ bumpDecode: false });
          });
        } catch (_) {}
      }
    })();

    refLapSel?.addEventListener('change', async () => {
      const n = Number(refLapSel.value || 0);
      const existing = _config?.session_info?.[s.csv_path] || {};
      await API.editSessionInfo(s.csv_path, { ...existing, sync_ref_lap_num: n, sync_ref_lap_user: true });
      if (!_config.session_info) _config.session_info = {};
      _config.session_info[s.csv_path] = { ...existing, sync_ref_lap_num: n, sync_ref_lap_user: true };
      const ui = beginSyncSeekUi();
      try {
        await seekToRefLap();
      } finally {
        if (ui) endSyncSeekUi();
      }
    });

    /** Apply numeric offset from「本节信息」内联编辑；跳转视频到 ref 对齐位置 */
    async function applyOffsetFromValue(v) {
      if (!Number.isFinite(v)) return;
      stopPlayback();
      await ensureRefLapFitsVideoTimeline(v);
      setTempOffset(v);
      const refE = getRefLapElapsed();
      const vt = clampVideoTimeSec(v + refE);
      const target = timeToFrame(vt);
      logSyncSeek('applyOffsetFromValue', {
        inputOffset: v,
        refLapElapsed: refE,
        vtTargetSec: vt,
        targetFrame: target,
      });
      // Video-first seek; keep this operation "non-precise" even when 精确模式开启.
      // For the fallback case we still want temp offset to be visible, so forceWriteTemp=true.
      dbgSyncVideo('applyOffsetFromValue', {
        v,
        refE,
        vt,
        target,
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl),
      });
      await decodeToFrame(target, true, { forceWriteTemp: true });
      if (gpuReady && liveMode) {
        try { await warmUpLivePaintOrFallback(); } catch (_) {}
      }
    }
    pane._openlapApplyOffset = applyOffsetFromValue;

    // Never use setPointerCapture on <input type="range"> — it breaks native thumb dragging
    // in pywebview / WebView2 (pointer moves no longer update the slider).
    scrub?.addEventListener('pointerdown', () => {
      scrubPointerHeld = true;
      refreshSeekStepButtonsDisabled();
    });
    const _scrubPointerEnd = () => {
      if (!scrubPointerHeld) return;
      scrubPointerHeld = false;
      if (!syncSeekUiHeld) setSeekStepButtonsDisabled(false);
      applyMeta();
    };
    scrub?.addEventListener('pointerup', _scrubPointerEnd);
    scrub?.addEventListener('pointercancel', _scrubPointerEnd);
    scrub?.addEventListener('change', _scrubPointerEnd);

    scrub?.addEventListener('input', async () => {
      stopBeforeSeek();
      const target = parseInt(scrub.value || '0', 10) || 0;
      logSyncVideoUI('ui_scrub_input', { target, playing, liveMode, gpuReady, curFrame, video: _dbgVideoState(liveVideoEl) });
      try {
        const tReq = frameToTime(target);
        const t = clampVideoTimeSec(tReq);
        const nextFrame = Math.max(0, Math.min(maxFrame(), timeToFrame(t)));
        // Important: set temp offset first so applyMeta doesn't lock out auto-saved offsets.
        setTempOffset(frameToTime(nextFrame) - getRefLapElapsed());

        // 精确模式只作用于 ±1f；拖条始终走 <video> / 非强制 OpenCV，保持流畅粗定位。
        if (gpuReady && liveVideoEl) {
          switchToLiveMode();
          liveVideoEl.currentTime = t;
          curFrame = nextFrame;
          applyMeta();
          dbgSyncVideo('scrub_gpu', {
            target,
            tReq,
            t,
            nextFrame,
            playing,
            liveMode,
            gpuReady,
            curFrame,
            video: _dbgVideoState(liveVideoEl),
          });
        } else {
          // Fallback: when <video> can't be used, still update panel via OpenCV/JPEG.
          await decodeToFrame(nextFrame, true);
          dbgSyncVideo('scrub_fallback', {
            target,
            tReq,
            t,
            nextFrame,
            playing,
            liveMode,
            gpuReady,
            curFrame,
            video: _dbgVideoState(liveVideoEl),
          });
        }
      } catch (_) {
        // ignore
      }
    });

    async function step(frameDelta, quiet = false) {
      let uiStarted = false;
      if (!quiet) {
        if (busy || syncSeekUiHeld) return;
        uiStarted = beginSyncSeekUi();
      }
      try {
        const d = Number(frameDelta || 0);
        const absDelta = Math.abs(d);
        const wantsPreciseFrame = preciseMode && !quiet && absDelta > 0 && absDelta <= 1.01;
        if (wantsPreciseFrame) {
          const dir = d > 0 ? 1 : -1;
          await decodeToFrame(curFrame + dir, true, { forceWriteTemp: true, forceOpenCv: true });
          return;
        }

        if (gpuReady && liveVideoEl) {
          try {
            const f = Math.max(1e-6, Number(fps) || 30);
            const curT = Number(liveVideoEl.currentTime || 0);
            const nextT = Math.max(0, curT + (Number(frameDelta || 0) / f));
            try { liveVideoEl.pause(); } catch (_) {}
            switchToLiveMode();
            liveVideoEl.currentTime = nextT;
            curFrame = Math.max(0, Math.min(maxFrame(), timeToFrame(nextT)));
            setTempOffset(frameToTime(curFrame) - getRefLapElapsed());
            applyMeta();
            if (!quiet && liveVideoEl) {
              await new Promise((resolve) => {
                let done = false;
                const fin = () => {
                  if (done) return;
                  done = true;
                  try { liveVideoEl.removeEventListener('seeked', fin); } catch (_) {}
                  resolve();
                };
                liveVideoEl.addEventListener('seeked', fin, { once: true });
                setTimeout(fin, 120);
              });
            }
            return;
          } catch (_) {
            // fallback below
          }
        }

        // Non-precise jumps: video-first already failed; fall back to decodeToFrame().
        // This keeps 精确模式的“仅按帧走 OpenCV”规则：只有 wantsPreciseFrame 才会 forceOpenCv。
        const f = Math.max(1e-6, Number(fps) || 30);
        const curT = frameToTime(curFrame);
        const dt = Number(frameDelta || 0) / f;
        const nextT = Math.max(0, curT + dt);
        const nextFrame = Math.max(0, Math.min(maxFrame(), timeToFrame(nextT)));
        setTempOffset(frameToTime(nextFrame) - getRefLapElapsed());
        await decodeToFrame(nextFrame, quiet);
        return;
      } finally {
        if (uiStarted) endSyncSeekUi();
      }
    }

    pane.querySelector('#sv-mm')?.addEventListener('click', async () => {
      logSyncVideoUI('ui_click_-1s', { fps, playing, liveMode, gpuReady, curFrame, video: _dbgVideoState(liveVideoEl) });
      stopBeforeSeek();
      await step(-fps);
    });
    pane.querySelector('#sv-m')?.addEventListener('click', async () => {
      logSyncVideoUI('ui_click_-1f', { fps, playing, liveMode, gpuReady, curFrame, video: _dbgVideoState(liveVideoEl) });
      stopBeforeSeek();
      await step(-1);
    });
    pane.querySelector('#sv-p')?.addEventListener('click', async () => {
      logSyncVideoUI('ui_click_+1f', { fps, playing, liveMode, gpuReady, curFrame, video: _dbgVideoState(liveVideoEl) });
      stopBeforeSeek();
      await step(1);
    });
    pane.querySelector('#sv-pp')?.addEventListener('click', async () => {
      logSyncVideoUI('ui_click_+1s', { fps, playing, liveMode, gpuReady, curFrame, video: _dbgVideoState(liveVideoEl) });
      stopBeforeSeek();
      await step(fps);
    });
    playBtn?.addEventListener('click', () => {
      logSyncVideoUI('ui_click_play_toggle', {
        playing,
        liveMode,
        gpuReady,
        curFrame,
        video: _dbgVideoState(liveVideoEl),
        boxes: { video: _domBox(liveVideoEl), frame: _domBox(frameEl), loading: _domBox(loadingEl) },
      });
      if (playing) stopPlayback({ bumpDecode: false });
      else startPlayback();
    });
    autoSyncBtn?.addEventListener('click', async () => {
      autoSyncBtn.disabled = true;
      try {
        const started = await triggerAutoSyncForSession(s, '手动触发');
        if (!started) {
          // triggerAutoSyncForSession 已设置详细状态文案
        }
      } finally {
        autoSyncBtn.disabled = false;
      }
    });
    preciseModeEl?.addEventListener('change', async () => {
      const enabled = !!preciseModeEl.checked;
      try {
        const existing = _config?.session_info?.[s.csv_path] || {};
        await API.editSessionInfo(s.csv_path, { ...existing, sync_precise_mode: enabled });
        if (!_config.session_info) _config.session_info = {};
        _config.session_info[s.csv_path] = { ...existing, sync_precise_mode: enabled };
        preciseMode = enabled;
        stopPlayback();
        setStatus(enabled ? '已开启精确模式：±1f 走 OpenCV 精确帧' : '已关闭精确模式：±1f 也走 GPU 流畅定位');
        // 勿 renderRight()：整卡重建会丢掉拖条临时 offset。
      } catch (e) {
        try { preciseModeEl.checked = !enabled; } catch (_) {}
        preciseMode = !enabled;
        setStatus('切换精确模式失败：' + String(e));
      }
    });

    pane.querySelector('#sv-mark')?.addEventListener('click', async () => {
      stopPlayback();
      // Guard: if we don't have a real timeline yet, curFrame is often stuck at 0,
      // which would "save" a bogus offset of (-refElapsed) and permanently desync preview/export.
      if (!gpuReady && !decoderSessionId && !(frameCount > 0)) {
        setLoading(true, '视频未就绪：请先等待画面可拖动/可播放后再标记。');
        setTimeout(() => { try { setLoading(false); } catch (_) {} }, 1200);
        return;
      }
      const rawTime = frameToTime(curFrame);
      // Confirm should lock against the currently selected reference lap.
      // Using lap-1 baseline for auto results can flip sign/magnitude unexpectedly.
      const refElapsed = getRefLapElapsed();
      const offset = rawTime - refElapsed;
      s.sync_offset = offset;
      s.sync_source = 'user';
      try { delete s._temp_sync_offset; } catch (_) { s._temp_sync_offset = undefined; }
      await saveOffset(s);
      renderLeft();
      renderRight();
      if (markEl) markEl.textContent = '✓ saved';
    });
    updatePlayBtn();
  }

  // Called after renderRight() from the auto_sync_progress 'done' handler.
  // wireVideoSync already attaches a loadedmetadata/canplay listener, but if the
  // browser returns readyState >= 1 immediately (cached metadata), those events
  // never fire and the existing readyState check inside wireVideoSync might have
  // run before video.load() finished.  This function does one final check and
  // seeks if the element is now ready.
  function _seekVideoAfterAutoSync(s, syncOffset) {
    const off = Number(syncOffset);
    if (!Number.isFinite(off)) return;
    requestAnimationFrame(() => {
      requestAnimationFrame(async () => {
        if (!_container) return;
        if (_normCsvPathKey(s.csv_path) !== _normCsvPathKey(_selCsv || '')) return;
        const pane = _container.querySelector('#data-right');
        const fn = pane && pane._openlapApplyOffset;
        if (typeof fn === 'function') await fn(off);
      });
    });
  }

  async function saveOffset(s) {
    const offsets        = { ...(_config?.offsets || {}),         [s.csv_path]: s.sync_offset };
    const offset_sources = { ...(_config?.offset_sources || {}),  [s.csv_path]: 'user' };
    _config = { ..._config, offsets, offset_sources };
    await API.saveConfig({ offsets, offset_sources });
    // Keep previewSession offset in sync
    const prev = State.get('previewSession');
    if (prev && prev.csv_path === s.csv_path) {
      State.set('previewSession', { ...prev, sync_offset: s.sync_offset });
    }
  }

  // ── Footer ────────────────────────────────────────────────────────────────────

  function refreshFooter() {
    const footer = _container?.querySelector('#data-footer');
    if (!footer) return;
    const splitN = _splitSel.size;
    const hint = _sessions.length
      ? (splitN > 0
          ? `共 ${_sessions.length} 节，已勾选 ${splitN} 节用于批量切圈。`
          : `共 ${_sessions.length} 节，请选择一节开始。`)
      : '请选择一节开始。';
    footer.innerHTML = `<span class="footer-hint">${esc(hint)}</span>`;
  }

  // ── Lap loading ───────────────────────────────────────────────────────────────

  async function loadLaps(session) {
    if (_lapDetails[session.csv_path]) { renderRight(); return; }
    try {
      const laps = await API.getLaps(session.csv_path);
      _lapDetails[session.csv_path] = laps;
    } catch (err) {
      console.warn('getLaps failed for', session.csv_path, err);
      _lapDetails[session.csv_path] = [];
    }
    if (_selCsv === session.csv_path) {
      renderRight();
      // Keep previewSession default lap as first lap (including outlap)
      const laps  = _lapDetails[session.csv_path] || [];
      const first = laps[0] || null;
      const prev  = State.get('previewSession');
      if (prev && prev.csv_path === session.csv_path && first) {
        State.set('previewSession', { ...prev, lap_idx: first.lap_idx });
      }
    }
  }

  // ── Meta enrichment (track, laps, best) ──────────────────────────────────────

  async function enrichMeta(sessions) {
    // Queue sessions that don't have meta yet
    for (const s of sessions) {
      if (!_meta[s.csv_path]) _metaQueue.push(s);
    }
    if (_metaBusy) return;
    _metaBusy = true;

    while (_metaQueue.length > 0) {
      const batch = _metaQueue.splice(0, 6);
      await Promise.all(batch.map(async s => {
        try {
          const m = await API.getSessionMeta(s.csv_path);
          _meta[s.csv_path] = m;
          // Write track back into the session object so it persists in the cache
          if (m.track) s.track = m.track;
          // Video duration (first segment) — used in session list column
          try {
            const cur = _meta[s.csv_path] || {};
            if (cur.video_dur_s == null && s.video_paths && s.video_paths[0]) {
              const p = await API.getVideoProbe(s.video_paths[0]).catch(() => null);
              if (p?.ok && Number.isFinite(Number(p.duration)) && Number(p.duration) > 0) {
                _meta[s.csv_path] = { ...cur, video_dur_s: Number(p.duration) };
              }
            }
          } catch (_) { /* ignore */ }
          // Refresh sync fields from config if they changed since last scan
          _applyStoredSyncToSession(s);
        } catch (err) {
          console.warn('getSessionMeta failed for', s.csv_path, err);
        }
      }));
      recomputeDayBest();
      renderLeft();
      if (_selCsv) renderRight();
    }
    _metaBusy = false;
    // Persist enriched track names to cache so backend queries (ref picker,
    // personal best, bulk rename) can match by track without loading every session.
    API.saveSessionsCache(_sessions).catch(() => {});
  }

  // ── Scan ──────────────────────────────────────────────────────────────────────

  function setStatus(msg) {
    _statusMsg = msg;
    const el = _container?.querySelector('#scan-status');
    if (el) el.textContent = msg;
    refreshFooter();
  }

  async function doScan(auto = false) {
    if (_scanning) return;
    _config = _config || await API.getConfig();
    const paths = _config?.all_telemetry_paths || [];
    if (!paths.length) {
      setStatus('未配置遥测目录，请先到设置页配置。');
      return;
    }

    _scanning = true;
    _autoSyncing = false;
    setStatus(auto ? '自动扫描中…' : '扫描中…');
    _container?.querySelector('#scan-btn')?.setAttribute('disabled', '');

    try {
      const all = [];
      for (const p of paths) {
        const r = await API.scanSessions(p);
        all.push(...r);
      }
      _sessions = all;
      // Prune batch selections that are no longer present.
      const existing = new Set(_sessions.map(s => s.csv_path));
      _splitSel = new Set([..._splitSel].filter(p => existing.has(p)));
      // Seed metadata immediately from scan result so laps/best show without delay.
      for (const s of _sessions) {
        if (!_meta[s.csv_path]) _meta[s.csv_path] = {};
        if (s.track) _meta[s.csv_path].track = s.track;
        if (s.laps) _meta[s.csv_path].laps = s.laps;
        if (s.best) _meta[s.csv_path].best = s.best;
      }
      // Apply stored offsets and sources (path-variant keys)
      for (const s of _sessions) _applyStoredSyncToSession(s);
      // Publish to global State so other pages (e.g. export) can access the list
      State.set('sessions', _sessions);
      _metaQueue = []; // reset queue so new sessions get fetched
      renderLeft();
      setStatus(`已找到 ${_sessions.length} 节。`);
      enrichMeta(_sessions);
      // Persist the full merged list so next startup shows cached results immediately
      API.saveSessionsCache(_sessions).catch(() => {});
      // Trigger background auto-sync if enabled
      if (_config?.auto_sync_enabled) {
        // IMPORTANT UX: if a session already has a saved offset, do NOT start background auto-sync for it.
        // (Manual "自动对齐/重新对齐" button can still be used explicitly.)
        const candidates = _sessions.filter(s => {
          if (!(s.matched && s.video_paths?.length)) return false;
          const saved = (s.sync_offset != null && Number.isFinite(Number(s.sync_offset)));
          return !saved;
        });
        const blocked = {
          disabled: '全局自动同步已关闭',
        };
        if (candidates.length === 0) return;
        API.startAutoSync(candidates).then(r => {
          if (r?.queued > 0) {
            _autoSyncing = true;
            const rq = r.reason === 'queued_after_export'
              ? `已排队 ${r.queued} 节（导出结束后自动运行，已保存偏移不变）`
              : r.reason === 'queued'
                ? `已排队 ${r.queued} 节（当前同步批结束后自动继续）`
                : `正在自动同步 ${r.queued} 节…`;
            setStatus(`已找到 ${_sessions.length} 节，${rq}`);
          } else if (blocked[r?.reason]) {
            setStatus(`已找到 ${_sessions.length} 节。自动同步未启动（${blocked[r.reason]}）。`);
          }
        }).catch(() => {});
      }
    } catch (e) {
      setStatus('扫描失败：' + e);
    }

    _scanning = false;
    _container?.querySelector('#scan-btn')?.removeAttribute('disabled');
  }

  function _extractDroppedPaths(evt) {
    const dt = evt?.dataTransfer;
    if (!dt) return [];
    const out = [];
    // Prefer FileList first
    if (dt.files && dt.files.length) {
      for (const f of dt.files) {
        const p = f.path || '';
        if (p) out.push(p);
      }
    }
    // Fallback: uri-list / text payload
    const uri = dt.getData && (dt.getData('text/uri-list') || dt.getData('text/plain'));
    if (uri) {
      uri.split(/\r?\n/).map(s => s.trim()).filter(Boolean).forEach(x => out.push(x));
    }
    return [...new Set(out)];
  }

  function _bindDropImport(container) {
    const overlay = container.querySelector('#drop-import-overlay');
    if (!overlay) return;
    let dragDepth = 0;

    const show = () => { overlay.style.display = 'flex'; };
    const hide = () => { overlay.style.display = 'none'; };

    container.addEventListener('dragenter', (e) => {
      e.preventDefault();
      dragDepth++;
      show();
    });
    container.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      show();
    });
    container.addEventListener('dragleave', (e) => {
      e.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) hide();
    });
    container.addEventListener('drop', async (e) => {
      e.preventDefault();
      dragDepth = 0;
      hide();
      const paths = _extractDroppedPaths(e);
      if (!paths.length) {
        setStatus('未读取到可用拖拽路径，请重试。');
        return;
      }
      try {
        setStatus('正在导入拖拽路径…');
        const res = await API.importDroppedPaths(paths, _selCsv || '');
        if (!res?.ok) {
          setStatus(res?.message || '拖拽导入失败。');
          return;
        }
        _config = await API.getConfig();
        setStatus('导入成功，正在重扫…');
        await doScan(true);
      } catch (err) {
        setStatus('拖拽导入失败：' + String(err));
      }
    });
  }

  async function runAutoLapSplitFromJson() {
    try {
      const targetsRaw = (_splitSel.size > 0)
        ? _sessions.filter(s => _splitSel.has(s.csv_path))
        : (_selCsv ? _sessions.filter(s => s.csv_path === _selCsv) : []);
      if (!targetsRaw.length) {
        setStatus('请先在左侧选中一节（.gpx/.vbo）或勾选多节再执行切圈。');
        return;
      }
      const targets = targetsRaw.filter(s => {
        const lower = String(s.csv_path || '').toLowerCase();
        return lower.endsWith('.gpx') || lower.endsWith('.vbo');
      });
      if (!targets.length) {
        setStatus('所选节次都不是 .gpx/.vbo，暂不支持自动切圈。');
        return;
      }
      const skippedType = targetsRaw.length - targets.length;

      let jsonPath = '';
      const tracks = await API.listTrackJsons().catch(() => []);
      if (tracks && tracks.length > 0) {
        const selected = await chooseTrackJsonDialog(tracks);
        if (!selected) return;
        setStatus(`已选择赛道：${selected.display_name || selected.name}`);
        jsonPath = selected.path;
        // Persist selected track to all target sessions so right-panel "赛道" auto-fills.
        const pickedTrackName = (selected.display_name || selected.name || '').trim();
        if (pickedTrackName) {
          for (const s of targets) {
            const existing = _config?.session_info?.[s.csv_path] || {};
            await API.editSessionInfo(s.csv_path, { ...existing, info_track: pickedTrackName });
            if (!_config.session_info) _config.session_info = {};
            _config.session_info[s.csv_path] = { ...existing, info_track: pickedTrackName };
          }
        }
      } else {
        setStatus('未在应用数据 tracks 目录发现赛道 JSON，改为手动选择…');
        jsonPath = await API.openFileDialog(['赛道 JSON (*.json)']);
        if (!jsonPath) return;
      }

      const btn = _container?.querySelector('#lap-split-btn');
      if (btn) btn.setAttribute('disabled', '');
      setStatus(`正在执行批量切圈…（目标 ${targets.length} 节）`);

      let n = 0;
      let k = skippedType;
      for (let i = 0; i < targets.length; i++) {
        const s = targets[i];
        setStatus(`正在切圈 ${i + 1}/${targets.length}：${baseName(s.csv_path)}`);
        const res = await API.autoSplitLapForFile(jsonPath, s.csv_path).catch(() => ({ processed: [], skipped: [s.csv_path] }));
        n += (res?.processed || []).length;
        k += (res?.skipped || []).length;
      }
      setStatus(`切圈完成：${n} 个成功，${k} 个跳过。正在刷新…`);

      for (const s of targets) {
        delete _meta[s.csv_path];
        delete _lapDetails[s.csv_path];
        await enrichMeta([s]);
        await loadLaps(s);
      }
      renderLeft();
      if (_selCsv) renderRight();
      setStatus(`切圈完成：${n} 个成功，${k} 个跳过。`);
    } catch (e) {
      setStatus('切圈失败：' + String(e));
    } finally {
      _container?.querySelector('#lap-split-btn')?.removeAttribute('disabled');
    }
  }

  function chooseTrackJsonDialog(tracks) {
    return new Promise((resolve) => {
      const mask = document.createElement('div');
      mask.style.position = 'fixed';
      mask.style.inset = '0';
      mask.style.background = 'rgba(0,0,0,0.45)';
      mask.style.display = 'flex';
      mask.style.alignItems = 'center';
      mask.style.justifyContent = 'center';
      mask.style.zIndex = '9999';

      const box = document.createElement('div');
      box.style.minWidth = '420px';
      box.style.maxWidth = '70vw';
      box.style.background = '#1f1f1f';
      box.style.border = '1px solid #3a3a3a';
      box.style.borderRadius = '10px';
      box.style.padding = '16px';
      box.style.boxShadow = '0 8px 28px rgba(0,0,0,0.35)';

      const title = document.createElement('div');
      title.textContent = '选择赛道';
      title.style.fontSize = '16px';
      title.style.fontWeight = '600';
      title.style.marginBottom = '8px';
      title.style.color = '#f0f0f0';

      const desc = document.createElement('div');
      desc.textContent = '请选择当前节次对应赛道：';
      desc.style.fontSize = '13px';
      desc.style.color = '#c9c9c9';
      desc.style.marginBottom = '10px';

      const sel = document.createElement('select');
      sel.style.width = '100%';
      sel.style.padding = '8px';
      sel.style.borderRadius = '6px';
      sel.style.border = '1px solid #4a4a4a';
      sel.style.background = '#111';
      sel.style.color = '#eee';
      tracks.forEach((t, i) => {
        const opt = document.createElement('option');
        opt.value = String(i);
        opt.textContent = t.display_name || t.name || `赛道 ${i + 1}`;
        sel.appendChild(opt);
      });

      const actions = document.createElement('div');
      actions.style.display = 'flex';
      actions.style.justifyContent = 'flex-end';
      actions.style.gap = '8px';
      actions.style.marginTop = '14px';

      const cancelBtn = document.createElement('button');
      cancelBtn.textContent = '取消';
      cancelBtn.className = 'btn secondary';

      const okBtn = document.createElement('button');
      okBtn.textContent = '确定';
      okBtn.className = 'btn';

      function cleanup(value) {
        if (mask.parentNode) mask.parentNode.removeChild(mask);
        resolve(value);
      }

      cancelBtn.addEventListener('click', () => cleanup(null));
      okBtn.addEventListener('click', () => {
        const idx = Math.max(0, Math.min(tracks.length - 1, parseInt(sel.value, 10) || 0));
        cleanup(tracks[idx]);
      });
      mask.addEventListener('click', (e) => {
        if (e.target === mask) cleanup(null);
      });

      actions.appendChild(cancelBtn);
      actions.appendChild(okBtn);
      box.appendChild(title);
      box.appendChild(desc);
      box.appendChild(sel);
      box.appendChild(actions);
      mask.appendChild(box);
      document.body.appendChild(mask);
      sel.focus();
    });
  }

  function _isGpxVboPath(p) {
    const lower = String(p || '').toLowerCase();
    return lower.endsWith('.gpx') || lower.endsWith('.vbo');
  }

  function _pickManualLapSplitTelemetryPath() {
    if (_isGpxVboPath(_selCsv)) return _selCsv;
    if (_splitSel.size === 1) {
      const only = [..._splitSel][0];
      if (_isGpxVboPath(only)) return only;
    }
    return '';
  }

  async function launchManualLapSplitGui() {
    const btn = _container?.querySelector('#lap-split-btn');
    try {
      if (btn) btn.setAttribute('disabled', '');
      const tel = _pickManualLapSplitTelemetryPath();
      if (!tel) {
        setStatus('手动切圈需要当前节或唯一勾选的节为 .gpx/.vbo；多节时请先在左侧点选一节。');
        return;
      }
      setStatus('正在启动手动画线切圈工具…');
      const res = await API.launchManualLapSplitGui(tel);
      if (res?.started) {
        setStatus('已启动手动画线工具，并已加载当前节的 GPX/VBO 轨迹。保存后回到这里点“扫描”。');
      } else {
        setStatus('手动画线工具启动失败' + (res?.error ? `：${res.error}` : '') + '。');
      }
    } catch (e) {
      setStatus('手动画线工具启动失败：' + String(e));
    } finally {
      if (btn) btn.removeAttribute('disabled');
    }
  }

  function toggleLapSplitMenu() {
    const menu = _container?.querySelector('#lap-split-menu');
    if (!menu) return;
    const isOpen = menu.style.display === 'block';
    menu.style.display = isOpen ? 'none' : 'block';
  }

  function closeLapSplitMenu() {
    const menu = _container?.querySelector('#lap-split-menu');
    if (menu) menu.style.display = 'none';
  }

  // ── Drag resizer ──────────────────────────────────────────────────────────────

  function initResizer(container) {
    const split     = container.querySelector('#data-split');
    const leftPanel = container.querySelector('#data-left-panel');
    const resizer   = container.querySelector('#data-resizer');
    if (!split || !leftPanel || !resizer) return;

    // Restore saved ratio (default 50%)
    const saved = parseFloat(localStorage.getItem('data-split-ratio') || '0.5');
    leftPanel.style.flexBasis = (saved * 100).toFixed(2) + '%';

    let dragging = false;

    resizer.addEventListener('mousedown', e => {
      dragging = true;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });

    const onMove = e => {
      if (!dragging) return;
      const rect  = split.getBoundingClientRect();
      let   ratio = (e.clientX - rect.left) / rect.width;
      ratio = Math.max(0.2, Math.min(0.8, ratio));
      leftPanel.style.flexBasis = (ratio * 100).toFixed(2) + '%';
    };

    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      // Persist
      const rect      = split.getBoundingClientRect();
      const leftRect  = leftPanel.getBoundingClientRect();
      const ratio     = leftRect.width / rect.width;
      localStorage.setItem('data-split-ratio', ratio.toFixed(4));
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);

    // Clean up on unmount (store removers on the container element)
    container._resizerCleanup = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
    };
  }

  // ── Mount / Unmount ────────────────────────────────────────────────────────────

  async function mount(container) {
    _container = container;
    _pageUnmounted = false;

    container.innerHTML = `
<div class="page data-page">
  <div id="drop-import-overlay" style="display:none;position:absolute;inset:0;z-index:1000;background:rgba(0,0,0,.45);align-items:center;justify-content:center;border:2px dashed var(--acc);pointer-events:none">
    <div style="padding:14px 18px;border-radius:10px;background:var(--bg2);color:var(--text1);font-size:12px">
      拖入遥测文件/文件夹或视频文件/文件夹即可导入
    </div>
  </div>
  <div class="toolbar">
    <div class="toolbar-left">
      <span class="page-title">Data</span>
      <span class="status-text" id="scan-status">${esc(_statusMsg||'加载中…')}</span>
    </div>
    <div class="toolbar-right">
      <div style="position:relative;display:inline-block">
        <button class="btn btn-secondary" id="lap-split-btn">切圈 ▾</button>
        <div id="lap-split-menu" style="display:none;position:absolute;right:0;top:30px;z-index:50;min-width:180px;background:var(--bg2);border:1px solid var(--line);border-radius:6px;padding:6px;box-shadow:0 6px 20px rgba(0,0,0,.35)">
          <button class="btn btn-secondary" id="lap-split-opt-track" style="width:100%;margin-bottom:6px;text-align:left">选择赛道（JSON起终线）</button>
          <button class="btn btn-secondary" id="lap-split-opt-manual" style="width:100%;text-align:left">手动画起终点线</button>
        </div>
      </div>
      <button class="btn btn-secondary" id="scan-btn">↺ 扫描</button>
    </div>
  </div>
  <div class="page-divider"></div>

  <div class="data-split" id="data-split">

    <!-- Left: session list -->
    <div class="data-left-panel" id="data-left-panel">
      <div class="dl-header">
        <span class="dl-col" style="text-align:center">批量</span>
        <span class="dl-col dl-col-sync">Sync</span>
        <span class="dl-col dl-col-time">Time</span>
        <span class="dl-col dl-col-track">赛道</span>
        <span class="dl-col dl-col-src">数据来源</span>
        <span class="dl-col dl-col-num">圈数</span>
        <span class="dl-col dl-col-num">最佳单圈</span>
        <span class="dl-col dl-col-num">视频时长</span>
      </div>
      <div class="dl-scroll" id="data-left"></div>
    </div>

    <!-- Drag resizer -->
    <div class="data-resizer" id="data-resizer"></div>

    <!-- Right: detail + sync -->
    <div class="data-right-panel" id="data-right">
      <div class="dr-empty">请选择一节查看详情并校准视频。</div>
    </div>

  </div>

  <div class="data-footer" id="data-footer">
    <span class="footer-hint">请选择一节开始。</span>
  </div>
</div>`;

    container.querySelector('#scan-btn').addEventListener('click', () => doScan(false));
    container.querySelector('#lap-split-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleLapSplitMenu();
    });
    container.querySelector('#lap-split-opt-track')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      closeLapSplitMenu();
      await runAutoLapSplitFromJson();
    });
    container.querySelector('#lap-split-opt-manual')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      closeLapSplitMenu();
      await launchManualLapSplitGui();
    });
    container.addEventListener('click', () => closeLapSplitMenu());
    initResizer(container);
    _bindDropImport(container);

    // XRK auto-conversion progress from the backend
    _unlistenFns.push(API.on('scan_status', detail => {
      if (_scanning) setStatus(detail.message || '');
    }));

    // Listen to auto-sync push events to update session status in real-time
    let _asIdx = 0, _asTotal = 0, _asDone = 0, _asFailed = 0, _asSkipped = 0;
    _unlistenFns.push(API.on('auto_sync_progress', detail => {
      const s = _findSessionByCsvPath(detail.csv_path);
      if (!s) return;
      if (detail.status === 'processing') {
        const curN = Number(detail.current);
        const totN = Number(detail.total);
        if (Number.isFinite(curN) && Number.isFinite(totN) && totN > 0) {
          if (curN === 1) {
            _asDone = 0;
            _asFailed = 0;
            _asSkipped = 0;
          }
          _asIdx = curN;
          _asTotal = totN;
        }
        _setAutoSyncUiFor(s.csv_path, {
          running: true,
          current: _asIdx,
          total: _asTotal,
          status: 'processing',
          text: `第 ${_asIdx}/${_asTotal} 节：准备分析… 手动校准已暂时锁定`,
          pct: _asTotal ? ((_asIdx - 1) / _asTotal) * 100 : null,
        });
        _updateAutoSyncOverlayIfVisible(s.csv_path);
        setStatus(`自动同步中：第 ${_asIdx}/${_asTotal} 节…`);
      } else if (detail.status === 'checking') {
        const curN = Number(detail.current);
        const totN = Number(detail.total);
        if (Number.isFinite(curN) && Number.isFinite(totN) && totN > 0 && curN > 0) {
          _asIdx = curN;
          _asTotal = totN;
        }
        const isMetaStage = detail.stage === 'metadata';
        const conf = detail.confidence != null && !isNaN(detail.confidence)
          ? Number(detail.confidence).toFixed(2)
          : '—';
        const secs = detail.vid_t != null && !isNaN(detail.vid_t)
          ? Number(detail.vid_t).toFixed(0)
          : '—';
        const mode = detail.mode || '';
        const baseTxt = Number.isFinite(detail.baseline_offset)
          ? `，基准 ${detail.baseline_offset >= 0 ? '+' : ''}${detail.baseline_offset.toFixed(3)}s`
          : '';
        const winTxt = Number.isFinite(detail.search_window_s)
          ? `，微调窗 ±${Number(detail.search_window_s).toFixed(1)}s`
          : '';
        const stageTxt = detail.stage ? ` [${detail.stage}]` : '';
        const modeTxt = mode ? ` (${mode})` : '';
        const progressTxt = isMetaStage
          ? `第 ${_asIdx}/${_asTotal} 节${stageTxt}${modeTxt}：读取元数据 / 准备解码…`
          : `第 ${_asIdx}/${_asTotal} 节${stageTxt}${modeTxt}：已解码 ${secs}s，置信度 ${conf}×（需 6×）`;
        const statusBarTxt = isMetaStage
          ? `自动同步中：第 ${_asIdx}/${_asTotal} 节${stageTxt}${modeTxt} — 读取元数据 / 准备解码${baseTxt}${winTxt}`
          : `自动同步中：第 ${_asIdx}/${_asTotal} 节${stageTxt}${modeTxt} — 已解码 ${secs}s 视频，置信度 ${conf}×（需 6×）${baseTxt}${winTxt}`;
        _setAutoSyncUiFor(s.csv_path, {
          running: true,
          current: _asIdx,
          total: _asTotal,
          status: 'checking',
          text: progressTxt,
          pct: _asTotal ? ((_asIdx - 1) / _asTotal) * 100 : null,
        });
        _updateAutoSyncOverlayIfVisible(s.csv_path);
        setStatus(statusBarTxt);
      } else if (detail.status === 'done') {
        _asDone++;
        // status=done means Python already persisted — mirror it. (Conflicts use status=skipped.)
        // Do not gate on s.sync_source: backend may replace placeholder user (~0) with auto_baseline
        // while this row still had sync_source 'user' from the last scan.
        s.sync_offset = detail.offset;
        s.sync_source = detail.offset_source || 'auto';
        try { delete s._temp_sync_offset; } catch (_) { s._temp_sync_offset = undefined; }
        if (_config) {
          _config.offsets = { ...(_config.offsets || {}), [s.csv_path]: detail.offset };
          _config.offset_sources = { ...(_config.offset_sources || {}), [s.csv_path]: s.sync_source };
        }
        const prev = State.get('previewSession');
        if (prev && _normCsvPathKey(prev.csv_path) === _normCsvPathKey(s.csv_path)) {
          State.set('previewSession', { ...prev, video_paths: s.video_paths || prev.video_paths, sync_offset: detail.offset });
        }
        renderLeft();
        renderRight();
        _seekVideoAfterAutoSync(s, detail.offset);
        const off = detail.offset >= 0 ? `+${detail.offset.toFixed(3)}s` : `${detail.offset.toFixed(3)}s`;
        _setAutoSyncUiFor(s.csv_path, { running: false, status: 'done', text: `已完成：${off}` , pct: _asTotal ? (_asIdx / _asTotal) * 100 : 100 });
        _updateAutoSyncOverlayIfVisible(s.csv_path);
        const metaAnchored = syncIsBaselineAuto(s.sync_source);
        setStatus(
          `自动同步：已采用 ${off}${metaAnchored ? '（元数据锚定）' : ''} 作为本节偏移（置信度 ${detail.confidence?.toFixed(2)}×）。可直接预览；若要配合 seek/对齐圈再抠，进校准页即可。`,
        );
      } else if (detail.status === 'failed') {
        _asFailed++;
        s.auto_sync_failed = true;
        _setAutoSyncUiFor(s.csv_path, { running: false, status: 'failed', text: `未找到高置信匹配，已解锁手动校准`, pct: _asTotal ? (_asIdx / _asTotal) * 100 : 100 });
        _updateAutoSyncOverlayIfVisible(s.csv_path);
        setStatus(`自动同步：未找到高置信匹配（${detail.confidence?.toFixed(2)}×），请手动设置偏移`);
        renderLeft();
        renderRight();
      } else if (detail.status === 'skipped') {
        _asSkipped++;
        const off = detail.offset;
        const offStr = off == null || isNaN(off) ? '—' : (off >= 0 ? `+${Number(off).toFixed(3)}s` : `${Number(off).toFixed(3)}s`);
        const stored = detail.stored_offset;
        const storedStr = stored == null || isNaN(stored)
          ? '—'
          : (stored >= 0 ? `+${Number(stored).toFixed(3)}s` : `${Number(stored).toFixed(3)}s`);
        const sub =
          detail.reason === 'user_offset_locked'
            ? `已算出 ${offStr}，但未写入（保留用户偏移 ${storedStr}）`
            : '已跳过写入';
        _setAutoSyncUiFor(s.csv_path, {
          running: false,
          status: 'skipped',
          text: sub,
          pct: _asTotal ? (_asIdx / _asTotal) * 100 : 100,
        });
        _updateAutoSyncOverlayIfVisible(s.csv_path);
        setStatus(`自动同步：${sub}`);
        renderLeft();
        renderRight();
      }
    }));
    _unlistenFns.push(API.on('auto_sync_done', () => {
      _autoSyncing = false;
      _autoSyncByCsv = {};
      // If user is on the align panel, ensure it's unlocked
      if (_selCsv) {
        _updateAutoSyncOverlayIfVisible(_selCsv);
        renderRight();
        renderLeft();
      }
      const parts = [];
      if (_asDone > 0) parts.push(`成功 ${_asDone}`);
      if (_asSkipped > 0) parts.push(`跳过 ${_asSkipped}`);
      if (_asFailed > 0) parts.push(`失败 ${_asFailed}`);
      const summary = parts.length ? ` — ${parts.join('，')}` : '';
      setStatus(`已找到 ${_sessions.length} 节。自动同步完成${summary}。`);
    }));

    // Await the video server port before rendering so videoUrl() is correct from the start
    if (!_videoPort) {
      _videoPort = await API.getVideoServerPort().catch(() => 0);
    }

    // If we already have sessions from this session's scan, restore immediately
    if (_sessions.length > 0) {
      renderLeft();
      if (_selCsv) renderRight();
      refreshFooter();
      setStatus(_statusMsg);
      return;
    }

    // First visit: load config + cache, then auto-scan
    _config = await API.getConfig();

    // Apply stored offsets to sessions immediately
    const applyOffsets = () => {
      for (const s of _sessions) _applyStoredSyncToSession(s);
    };

    try {
      const cached = await API.scanSessions('__cache__');
      if (cached && cached.length > 0) {
        _sessions = cached;
        applyOffsets();
        State.set('sessions', _sessions);
        renderLeft();
        setStatus(`已加载 ${_sessions.length} 节缓存，后台重扫中…`);
        enrichMeta(_sessions);
        // Auto-scan in background
        setTimeout(() => doScan(true), 200);
        return;
      }
    } catch (err) {
      console.warn('Failed to load session cache:', err);
    }

    // No cache: auto-scan immediately
    setStatus('扫描中…');
    doScan(true);
  }

  function unmount() {
    _pageUnmounted = true;
    if (_container?._resizerCleanup) _container._resizerCleanup();
    _unlistenFns.forEach(fn => fn());
    _unlistenFns = [];
    if (_syncDecoderSessionId) {
      API.closeDecodeSession(_syncDecoderSessionId).catch(() => {});
      _syncDecoderSessionId = '';
      _syncDecoderVideoPath = '';
    }
    _container = null;
    // Module state (_sessions, _meta, etc.) preserved intentionally across navigations
  }

  Router.register('data', { mount, unmount });
})();
