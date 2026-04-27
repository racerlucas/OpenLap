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
  let _config     = null;
  let _scanning    = false;
  let _autoSyncing = false;
  let _statusMsg   = '';
  let _container   = null;
  let _metaQueue   = [];   // sessions waiting for meta fetch
  let _metaBusy    = false;
  let _videoPort   = 0;    // localhost port of the Python video file server
  let _unlistenFns = [];   // push-event unlisten callbacks
  let _syncDecoderSessionId = '';
  let _syncDecoderVideoPath = '';

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
    const m = String(iso).match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
    if (m) return `${m[1]} ${m[2]}:${m[3]}${m[4] ? ':' + m[4] : ''}`;
    try {
      return new Date(iso).toLocaleString(undefined,
        { year:'numeric', month:'2-digit', day:'2-digit',
          hour:'2-digit', minute:'2-digit' });
    } catch { return iso; }
  }

  function dateKey(iso) {
    if (!iso) return 'Unknown';
    const m = String(iso).match(/^(\d{4}-\d{2}-\d{2})[T ]/);
    if (m) return m[1];
    try { return new Date(iso).toISOString().slice(0, 10); }
    catch { return 'Unknown'; }
  }

  function esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function baseName(p) {
    return (p||'').replace(/\\/g,'/').split('/').pop() || p;
  }

  function videoUrl(winPath) {
    // Pass path as a query param so browser slash-normalization never mangles UNC paths
    if (_videoPort) return `http://127.0.0.1:${_videoPort}/?f=${encodeURIComponent(winPath)}`;
    return 'file:///' + winPath.replace(/\\/g, '/');
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
      const ta = a.csv_start ? new Date(a.csv_start).getTime() : 0;
      const tb = b.csv_start ? new Date(b.csv_start).getTime() : 0;
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
  }

  function sessionRow(s) {
    const m       = _meta[s.csv_path] || {};
    const isSel   = s.csv_path === _selCsv;
    const isDayB  = _dayBest[s.csv_path];
    const timeMatch = s.csv_start ? String(s.csv_start).match(/^[0-9-]+[T ](\d{2}):(\d{2})/) : null;
    const time = timeMatch ? `${timeMatch[1]}:${timeMatch[2]}`
      : (s.csv_start ? new Date(s.csv_start).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}) : '—');
    const trackOverride = _config?.session_info?.[s.csv_path]?.info_track;
    const track   = trackOverride || m.track || baseName(s.csv_path);
    const lapStr  = m.laps  || '—';
    const bestStr = m.best  || (m.best_secs != null ? fmtTime(m.best_secs) : '—');
    const syncLabel = s.needs_conversion           ? '↻ conv'
                    : (!s.matched)                  ? 'no vid'
                    : s.sync_offset != null && s.sync_source === 'auto' ? '~ auto'
                    : s.sync_offset != null         ? '✓ user'
                    : '≈ unset';
    const iconCls   = s.needs_conversion           ? 'di-pending'
                    : (!s.matched)                  ? 'di-novid'
                    : s.sync_offset != null && s.sync_source === 'auto' ? 'di-auto'
                    : s.sync_offset != null         ? 'di-user'
                    : 'di-unsync';

    return `<div class="dl-row${isSel?' sel':''}${isDayB?' day-best':''}" data-csv="${esc(s.csv_path)}">
      <span class="dl-sync ${iconCls}">${syncLabel}</span>
      <span class="dl-time">${esc(time)}</span>
      <span class="dl-track" title="${esc(s.csv_path)}">${esc(track)}</span>
      <span class="dl-source">${esc(s.source||'RaceBox')}</span>
      <span class="dl-num">${esc(lapStr)}</span>
      <span class="dl-num">${esc(bestStr)}</span>
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
      sync_offset: s.sync_offset ?? 0,
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

    const m   = _meta[s.csv_path] || {};
    const off = s.sync_offset;

    // Session info
    const vidPaths    = s.video_paths || [];
    const hasVid      = s.matched && vidPaths.length > 0;
    const trackOverride  = _config?.session_info?.[s.csv_path]?.info_track;
    const effectiveTrack = trackOverride || m.track || '';

    pane.innerHTML = `
<!-- Info card -->
<div class="dr-card">
  <div class="dr-card-title">节信息</div>
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
    <div class="dr-row"><span class="dr-lbl">圈数</span><span class="dr-val">${esc(m.laps||'—')}</span></div>
    <div class="dr-row"><span class="dr-lbl">最佳</span><span class="dr-val" style="color:var(--ok)">${esc(m.best||'—')}</span></div>
    <div class="dr-row"><span class="dr-lbl">视频</span><span class="dr-val ${hasVid?'':'dr-warn'}">${hasVid ? `✓ ${vidPaths.length} 段` : '✗ 未匹配'}</span></div>
    <div class="dr-row"><span class="dr-lbl">偏移</span><span class="dr-val ${off!=null?'':'dr-warn'}" id="dr-off-display">${
      off!=null
        ? (s.sync_source === 'auto'
            ? `${off.toFixed(3)}s ~ auto`
            : `${off.toFixed(3)}s ✓`)
        : '未设置'
    }</span></div>
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
${hasVid ? renderAlignCard(s, vidPaths, off) : `
<div class="dr-card">
  <div class="dr-card-title">视频</div>
  <div class="dr-hint" style="color:var(--warn)">未找到匹配视频。</div>
  <div class="dr-actions" style="margin-top:8px">
    <button class="btn btn-secondary btn-sm" id="dr-assign-vid-btn">选择视频…</button>
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

  function renderAlignCard(s, vidPaths, off) {
    const offVal    = off != null ? off.toFixed(3) : '';
    const isAuto    = s.sync_source === 'auto';
    const isSyncing = _autoSyncing && s.sync_offset == null && !s.auto_sync_failed;
    const laps = _lapDetails[s.csv_path] || [];
    const defaultRefLap = (laps.find(l => !l.is_outlap)?.lap_num) || (laps[0]?.lap_num) || 1;
    const savedRefLap = Number(_config?.session_info?.[s.csv_path]?.sync_ref_lap_num || defaultRefLap);
    const lapOptions = laps.map((lap, idx) => {
      const lapNum = Number(lap.lap_num ?? (idx + 1));
      const sel = lapNum === savedRefLap ? 'selected' : '';
      return `<option value="${lapNum}" ${sel}>第 ${lapNum} 圈</option>`;
    }).join('');
    const autoNote  = isSyncing
      ? `<div style="font-size:9px;color:#ffb74d;margin-bottom:6px;padding:5px 6px;
                     background:rgba(255,183,77,0.08);border-radius:4px;border-left:2px solid #ffb74d">
           正在自动检测同步偏移… 完成后可拖动校准。
         </div>`
      : isAuto
      ? `<div style="font-size:9px;color:#64b5f6;margin-bottom:6px;padding:5px 6px;
                     background:rgba(100,181,246,0.08);border-radius:4px;border-left:2px solid #64b5f6">
           自动检测结果：${off != null ? off.toFixed(3)+'s' : '—'} — 请拖动核对后点击确认
         </div>`
      : '';
    return `
<div class="dr-card dr-align-card">
  <div class="dr-card-title">校准视频</div>
  ${autoNote}
  <div class="sync-frame-wrap">
    <img id="sync-frame" class="sync-video" alt="">
    <div id="sync-loading" class="sync-loading">
      <span class="sync-spinner" aria-hidden="true"></span>
      <span id="sync-loading-text">视频加载中…</span>
    </div>
  </div>
  <div class="sync-controls">
    <button class="btn btn-sm" id="sv-mm">◀◀ −1s</button>
    <button class="btn btn-sm" id="sv-m">◀ −1f</button>
    <button class="btn btn-sm" id="sv-p">▶ +1f</button>
    <button class="btn btn-sm" id="sv-pp">▶▶ +1s</button>
    <span class="sync-time" id="sv-time">0:00.000</span>
  </div>
  <input type="range" id="sv-scrub" class="sync-scrub" min="0" max="1000" value="0" step="1">
  <div class="sync-mark-row">
    <button class="btn btn-ok btn-sm" id="sv-mark">${isAuto ? '✓ 确认对齐圈起点' : '🏁 标记对齐圈起点'}</button>
    ${lapOptions ? `<select id="sv-ref-lap" class="input-field input-narrow sync-off-input" title="选择按哪一圈对齐">${lapOptions}</select>` : ''}
    <input type="number" id="sv-off-input" class="input-field input-narrow sync-off-input"
           step="0.001" value="${esc(offVal)}" placeholder="0.000" title="Current offset (s) — follows video position">
    <span class="sync-mark-val" id="sv-mark-val">${off!=null && !isAuto ? '✓ saved' : ''}</span>
  </div>
  ${vidPaths.length > 1 ? `<div style="font-size:9px;color:var(--text3);margin-top:4px">另有 ${vidPaths.length-1} 段视频</div>` : ''}
</div>`;
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
      const lap  = laps.find(l => l.is_best) || laps[0];
      State.set('previewSession', {
        csv_path:    s.csv_path,
        lap_idx:     lap ? lap.lap_idx : 0,
        video_paths: s.video_paths || [],
        sync_offset: s.sync_offset ?? 0,
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

    // Manual video assignment
    pane.querySelector('#dr-assign-vid-btn')?.addEventListener('click', async () => {
      const btn = pane.querySelector('#dr-assign-vid-btn');
      const msg = pane.querySelector('#dr-assign-vid-msg');
      btn.disabled = true;
      const videoPath = await API.openFileDialog(['视频文件 (*.mp4;*.mov;*.avi;*.mkv;*.MP4;*.MOV)']).catch(() => null);
      if (!videoPath) { btn.disabled = false; return; }
      try {
        await API.assignVideo(s.csv_path, videoPath);
        s.video_paths = [videoPath];
        s.matched     = true;
        // Update previewSession so editor picks up the new video immediately
        const prev = State.get('previewSession');
        if (prev?.csv_path === s.csv_path) {
          State.set('previewSession', { ...prev, video_paths: s.video_paths, sync_offset: s.sync_offset ?? 0 });
        }
        await API.saveSessionsCache(_sessions).catch(() => {});
        renderRight();
        renderLeft();
        // Trigger auto-sync for this session now that it has a video
        if (_config?.auto_sync_enabled && s.sync_offset == null && !s.auto_sync_failed) {
          API.startAutoSync([s]).then(r => {
            if (r?.queued > 0) {
              _autoSyncing = true;
              setStatus(`视频已绑定，自动同步中…`);
              if (_selCsv === s.csv_path) renderRight();
            }
          }).catch(() => {});
        }
      } catch (e) {
        if (msg) { msg.textContent = String(e); msg.className = 'status-msg status-err'; }
        btn.disabled = false;
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
    const loadingEl = pane.querySelector('#sync-loading');
    const loadingTextEl = pane.querySelector('#sync-loading-text');
    const scrub = pane.querySelector('#sv-scrub');
    const timeEl = pane.querySelector('#sv-time');
    const markEl = pane.querySelector('#sv-mark-val');
    const offInp = pane.querySelector('#sv-off-input');
    const refLapSel = pane.querySelector('#sv-ref-lap');
    if (!frameEl) return;

    const videoPath = s.video_paths?.[0];
    if (!videoPath) return;

    let fps = 30;
    let frameCount = 0;
    let curFrame = 0;
    let busy = false;
    let decoderSessionId = '';

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
      return Math.max(0, (frameCount || 0) - 1);
    }
    function applyMeta() {
      if (scrub) {
        scrub.min = '0';
        scrub.max = String(maxFrame());
        scrub.step = '1';
        scrub.value = String(curFrame);
      }
      const t = frameToTime(curFrame);
      if (timeEl) timeEl.textContent = fmtVTime(t);
      if (offInp) offInp.value = t.toFixed(3);
    }
    async function decodeToFrame(targetFrame) {
      if (busy) return;
      busy = true;
      setLoading(true, '正在解码帧…');
      try {
        if (!decoderSessionId) return;
        const rsp = await API.decodeSessionSeek(decoderSessionId, targetFrame, null);
        if (!rsp?.ok) {
          frameEl.classList.remove('ready');
          setLoading(true, '视频帧解码失败');
          return;
        }
        if (Number(rsp.fps) > 0) fps = Number(rsp.fps);
        frameCount = Number(rsp.frame_count || frameCount || 0);
        curFrame = Math.max(0, Number(rsp.frame_idx || 0));
        frameEl.src = `data:image/jpeg;base64,${rsp.image_b64 || ''}`;
        frameEl.classList.add('ready');
        applyMeta();
        setLoading(false);
      } finally {
        busy = false;
      }
    }

    function getRefLapElapsed() {
      const laps = _lapDetails[s.csv_path] || [];
      if (!laps.length) return 0;
      const refLapNum = Number(refLapSel?.value || _config?.session_info?.[s.csv_path]?.sync_ref_lap_num || 0);
      const picked = laps.find(l => Number(l.lap_num) === refLapNum);
      if (picked) return picked.elapsed_start || 0;
      const firstTimed = laps.find(l => !l.is_outlap);
      return (firstTimed?.elapsed_start) || (laps[0]?.elapsed_start) || 0;
    }
    async function seekToRefLap() {
      if (s.sync_offset == null) return;
      await decodeToFrame(timeToFrame(s.sync_offset + getRefLapElapsed()));
    }

    (async () => {
      // Ensure only one persistent decode session stays alive for Data page.
      let opened = null;
      if (_syncDecoderSessionId && (_syncDecoderVideoPath !== videoPath)) {
        await API.closeDecodeSession(_syncDecoderSessionId).catch(() => {});
        _syncDecoderSessionId = '';
        _syncDecoderVideoPath = '';
      }
      if (!_syncDecoderSessionId) {
        opened = await API.openDecodeSession(videoPath, 10);
        if (!opened?.ok || !opened.session_id) {
          setLoading(true, '视频解码器启动失败');
          return;
        }
        _syncDecoderSessionId = opened.session_id;
        _syncDecoderVideoPath = videoPath;
      }
      decoderSessionId = _syncDecoderSessionId;
      try {
        const info = await API.getVideoFps(videoPath);
        if (info?.ok && Number(info.fps) > 0) fps = Number(info.fps);
      } catch (_) {}
      if (Number.isFinite(opened?.fps) && Number(opened.fps) > 0) fps = Number(opened.fps);
      if (s.sync_offset != null) await seekToRefLap();
      else await decodeToFrame(0);
    })();

    refLapSel?.addEventListener('change', async () => {
      const n = Number(refLapSel.value || 0);
      const existing = _config?.session_info?.[s.csv_path] || {};
      await API.editSessionInfo(s.csv_path, { ...existing, sync_ref_lap_num: n });
      if (!_config.session_info) _config.session_info = {};
      _config.session_info[s.csv_path] = { ...existing, sync_ref_lap_num: n };
      await seekToRefLap();
    });

    scrub?.addEventListener('input', async () => {
      await decodeToFrame(parseInt(scrub.value || '0', 10) || 0);
    });

    async function step(frameDelta) {
      if (!decoderSessionId) return;
      if (Math.abs(frameDelta) > 10) {
        const jump = Math.round((frameDelta > 0 ? 1 : -1) * Math.max(1, fps));
        await decodeToFrame(curFrame + jump);
        return;
      }
      if (busy) return;
      busy = true;
      try {
        const rsp = await API.decodeSessionStep(decoderSessionId, frameDelta > 0 ? 1 : -1);
        if (!rsp?.ok) return;
        if (Number(rsp.fps) > 0) fps = Number(rsp.fps);
        frameCount = Number(rsp.frame_count || frameCount || 0);
        curFrame = Math.max(0, Number(rsp.frame_idx || 0));
        frameEl.src = `data:image/jpeg;base64,${rsp.image_b64 || ''}`;
        applyMeta();
      } finally {
        busy = false;
      }
    }

    pane.querySelector('#sv-mm')?.addEventListener('click', () => { step(-fps); });
    pane.querySelector('#sv-m')?.addEventListener('click', () => { step(-1); });
    pane.querySelector('#sv-p')?.addEventListener('click', () => { step(1); });
    pane.querySelector('#sv-pp')?.addEventListener('click', () => { step(fps); });

    pane.querySelector('#sv-mark')?.addEventListener('click', async () => {
      const rawTime = frameToTime(curFrame);
      const refElapsed = getRefLapElapsed();
      const offset = rawTime - refElapsed;
      s.sync_offset = offset;
      s.sync_source = 'user';
      await saveOffset(s);
      renderLeft();
      renderRight();
      if (markEl) markEl.textContent = '✓ saved';
    });
  }

  // Called after renderRight() from the auto_sync_progress 'done' handler.
  // wireVideoSync already attaches a loadedmetadata/canplay listener, but if the
  // browser returns readyState >= 1 immediately (cached metadata), those events
  // never fire and the existing readyState check inside wireVideoSync might have
  // run before video.load() finished.  This function does one final check and
  // seeks if the element is now ready.
  function _seekVideoAfterAutoSync(s, syncOffset) {
    // Pure frame-player mode re-renders right panel and wireVideoSync seeks by syncOffset.
    // Keep this hook as a no-op for compatibility with existing event flow.
    return;
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
    const hint = _sessions.length
      ? `共 ${_sessions.length} 节，请选择一节开始。`
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
      // Update previewSession with best lap index now that we know it
      const laps  = _lapDetails[session.csv_path] || [];
      const best  = laps.find(l => l.is_best) || laps[0];
      const prev  = State.get('previewSession');
      if (prev && prev.csv_path === session.csv_path && best) {
        State.set('previewSession', { ...prev, lap_idx: best.lap_idx });
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
          // Mirror sync fields from config in case they updated since last scan
          if (_config?.offsets?.[s.csv_path] != null) {
            s.sync_offset = _config.offsets[s.csv_path];
            s.sync_source = _config.offset_sources?.[s.csv_path] ?? s.sync_source;
          }
        } catch (err) {
          console.warn('getSessionMeta failed for', s.csv_path, err);
        }
      }));
      recomputeDayBest();
      renderLeft();
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
      // Apply stored offsets and sources
      for (const s of _sessions) {
        if (_config?.offsets?.[s.csv_path] != null) {
          s.sync_offset = _config.offsets[s.csv_path];
          s.sync_source = _config.offset_sources?.[s.csv_path] ?? s.sync_source;
        }
      }
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
        const candidates = _sessions.filter(s => s.matched && s.video_paths?.length);
        API.startAutoSync(candidates).then(r => {
          if (r?.queued > 0) {
            _autoSyncing = true;
            setStatus(`已找到 ${_sessions.length} 节，正在自动同步 ${r.queued} 节…`);
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
      if (!_selCsv) {
        setStatus('请先在左侧选中一节（.gpx/.vbo）再执行切圈。');
        return;
      }
      const lower = String(_selCsv).toLowerCase();
      if (!(lower.endsWith('.gpx') || lower.endsWith('.vbo'))) {
        setStatus('当前节次不是 .gpx/.vbo，暂不支持自动切圈。');
        return;
      }

      let jsonPath = '';
      const tracks = await API.listTrackJsons().catch(() => []);
      if (tracks && tracks.length > 0) {
        const selected = await chooseTrackJsonDialog(tracks);
        if (!selected) return;
        setStatus(`已选择赛道：${selected.display_name || selected.name}`);
        jsonPath = selected.path;
        // Persist selected track to current session so right-panel "赛道" auto-fills.
        const s = _sessions.find(x => x.csv_path === _selCsv);
        if (s) {
          const pickedTrackName = (selected.display_name || selected.name || '').trim();
          if (pickedTrackName) {
            const existing = _config?.session_info?.[_selCsv] || {};
            await API.editSessionInfo(_selCsv, { ...existing, info_track: pickedTrackName });
            if (!_config.session_info) _config.session_info = {};
            _config.session_info[_selCsv] = { ...existing, info_track: pickedTrackName };
          }
        }
      } else {
        setStatus('tracks 目录未发现赛道 JSON，改为手动选择…');
        jsonPath = await API.openFileDialog(['赛道 JSON (*.json)']);
        if (!jsonPath) return;
      }

      const btn = _container?.querySelector('#lap-split-btn');
      if (btn) btn.setAttribute('disabled', '');
      setStatus('正在对当前选中节次执行切圈…');

      const res = await API.autoSplitLapForFile(jsonPath, _selCsv);
      const n = (res?.processed || []).length;
      const k = (res?.skipped || []).length;
      setStatus(`切圈完成：${n} 个成功，${k} 个跳过。正在刷新当前节次…`);

      const selectedCsv = _selCsv;
      const s = _sessions.find(x => x.csv_path === selectedCsv);
      if (s) {
        delete _meta[selectedCsv];
        delete _lapDetails[selectedCsv];
        await enrichMeta([s]);
        await loadLaps(s);
        // Keep current selection and video/sync context; only refresh current detail.
        if (_selCsv === selectedCsv) renderRight();
      }
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

  async function launchManualLapSplitGui() {
    const btn = _container?.querySelector('#lap-split-btn');
    try {
      if (btn) btn.setAttribute('disabled', '');
      setStatus('正在启动手动画线切圈工具…');
      const res = await API.launchManualLapSplitGui();
      if (res?.started) {
        setStatus('已启动手动画线工具（在 GPX/VBO 轨迹上画线）。保存后回到这里点“扫描”。');
      } else {
        setStatus('手动画线工具启动失败。');
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
        <span class="dl-col dl-col-sync">Sync</span>
        <span class="dl-col dl-col-time">Time</span>
        <span class="dl-col dl-col-track">赛道</span>
        <span class="dl-col dl-col-src">来源</span>
        <span class="dl-col dl-col-num">圈数</span>
        <span class="dl-col dl-col-num">最佳</span>
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
    let _asIdx = 0, _asTotal = 0, _asDone = 0, _asFailed = 0;
    _unlistenFns.push(API.on('auto_sync_progress', detail => {
      const s = _sessions.find(x => x.csv_path === detail.csv_path);
      if (!s) return;
      if (detail.status === 'processing') {
        _asIdx = detail.current; _asTotal = detail.total;
        setStatus(`自动同步中：第 ${_asIdx}/${_asTotal} 节…`);
      } else if (detail.status === 'checking') {
        const conf = detail.confidence?.toFixed(2);
        const secs = detail.vid_t?.toFixed(0);
        setStatus(`自动同步中：第 ${_asIdx}/${_asTotal} 节 — 已解码 ${secs}s 视频，置信度 ${conf}×（需 6×）`);
      } else if (detail.status === 'done') {
        _asDone++;
        // Don't overwrite if the user already confirmed this session while we were processing
        if (s.sync_source !== 'user') {
          s.sync_offset = detail.offset;
          s.sync_source = 'auto';
          // Keep previewSession in sync so editor gets the detected offset
          const prev = State.get('previewSession');
          if (prev?.csv_path === s.csv_path) {
            State.set('previewSession', { ...prev, video_paths: s.video_paths || prev.video_paths, sync_offset: detail.offset });
          }
          renderLeft();
          renderRight();
          // If the video element is already loaded (WebView2 cache), wireVideoSync's
          // readyState check runs before load() triggers metadata — seek it now.
          _seekVideoAfterAutoSync(s, detail.offset);
        }
        const off = detail.offset >= 0 ? `+${detail.offset.toFixed(3)}s` : `${detail.offset.toFixed(3)}s`;
        setStatus(`自动同步：检测到偏移 ${off}，置信度 ${detail.confidence?.toFixed(2)}×`);
      } else if (detail.status === 'failed') {
        _asFailed++;
        s.auto_sync_failed = true;
        setStatus(`自动同步：未找到高置信匹配（${detail.confidence?.toFixed(2)}×），请手动设置偏移`);
        renderLeft();
        renderRight();
      }
    }));
    _unlistenFns.push(API.on('auto_sync_done', () => {
      _autoSyncing = false;
      const summary = _asDone > 0 || _asFailed > 0
        ? ` — 匹配 ${_asDone}，跳过 ${_asFailed}`
        : '';
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
      for (const s of _sessions) {
        if (_config?.offsets?.[s.csv_path] != null)
          s.sync_offset = _config.offsets[s.csv_path];
      }
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
