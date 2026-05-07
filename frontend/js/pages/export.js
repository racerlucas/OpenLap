/**
 * export.js — Export page (Phase 3).
 *
 * Reads selected items from State.get('selectedItems').
 * Calls API.startExport(params) / API.cancelExport().
 * Receives progress via openlap CustomEvents: export_progress, export_log, export_done.
 */
(function () {
  /** Match ``app_config.DEFAULT_OVERLAY_REF_MODE`` when overlay omits ``ref_mode``. */
  const DEFAULT_REF_MODE = 'session_best_so_far';

  let _container   = null;
  let _exporting   = false;
  let _logLines    = [];
  let _unlistenFns = [];   // DOM-lifetime listeners only (State subscriptions)
  let _progressPct = 0;
  let _progressMsg = '';
  let _badgeText   = '空闲';
  let _badgeCls    = 'badge-dim';

  // Lap range picker state
  let _rangePickerStart = null;  // selected start lap_num
  let _rangePickerEnd   = null;  // selected end lap_num
  let _rangePickerLaps  = [];    // from getLaps: all laps incl. outlap/inlap

  // Video probe（分辨率/帧率）；VBR/CBR 下才防抖调用 estimate_export_size 显示「体积参考」。CQ 不调用。
  let _videoProbe   = null;
  let _probeVideo   = '';
  let _vbrCbrEstimateTimer = null;
  let _persistExportTimer = null;

  // Export speed (derived from "Frame i/N" progress messages)
  let _lastFrameProg = null; // { done, total, t_ms }
  let _emaFps = null;

  // Encoder probe cache (from API.checkEncoders)
  let _encAvail = {}; // { ffmpeg_encoder_name: boolean }
  let _ffmpegVersion = '';

  // ── Mount / Unmount ──────────────────────────────────────────────────────────

  function _itemFromPreview(ps) {
    if (!ps) return null;
    return {
      csv_path:    ps.csv_path,
      lap_idx:     ps.lap_idx,
      video_paths: ps.video_paths || [],
      sync_offset: ps.sync_offset ?? 0,
      source:      ps.source || '',
      duration:    null,
      is_best:     false,
    };
  }

  async function mount(container) {
    _container = container;
    // Do NOT reset _logLines or _exporting — they persist while navigating away

    // Always (re-)derive selectedItems from previewSession on mount
    const initPs = State.get('previewSession');
    State.set('selectedItems', initPs ? [_itemFromPreview(initPs)] : []);

    const cfg = { ...(await API.getConfig().catch(() => ({}))), ...(State.get('config') || {}) };

    const items = State.get('selectedItems') || [];

    container.innerHTML = _buildHTML(cfg, items);

    _bindEvents(cfg, items);
    _refreshItemList(items);
    _updateStartBtn(items);
    _updateCanvasExportHint(container);

    // Background encoder probe done event (fires even if Export page isn't active)
    // We only bind this listener when the page is mounted to avoid duplicate updates.
    const _onProbeDone = (detail) => {
      if (!detail) return;
      if (detail.version) _ffmpegVersion = String(detail.version);
      const a = detail.avail;
      if (a && typeof a === 'object') {
        _encAvail = {};
        Object.keys(a).forEach(k => { _encAvail[String(k)] = !!a[k]; });
      }
      _syncEncoderFamilyOptions();
      const msg = container.querySelector('#exp-enc-msg');
      if (msg) msg.textContent = _ffmpegVersion ? `FFmpeg ${_ffmpegVersion}` : '';
    };
    _unlistenFns.push(API.on('encoder_probe_done', _onProbeDone));

    (async () => {
      try {
        const r = await API.checkEncoders();
        const msg = container.querySelector('#exp-enc-msg');
        _ffmpegVersion = r?.version ? String(r.version) : '';
        _encAvail = {};
        (r?.encoders || []).forEach(row => {
          if (row && row.name) _encAvail[String(row.name)] = !!row.available;
        });
        _syncEncoderFamilyOptions();
        if (msg) msg.textContent = _ffmpegVersion ? `FFmpeg ${_ffmpegVersion}` : '';
      } catch (_) { /* ignore */ }
      try {
        await _refreshResolvedEncoder(container);
      } catch (_) { /* ignore */ }
      _syncBitrateOptions();
      _queueVbrCbrEstimate();
    })();

    // Restore persistent state accumulated while the tab was not visible
    const logEl = container.querySelector('#exp-log');
    if (logEl && _logLines.length) {
      logEl.value = _logLines.join('\n');
      logEl.scrollTop = logEl.scrollHeight;
    }
    _setExporting(_exporting);   // re-applies button/cancel visibility
    _setProgress(_progressPct, _progressMsg);
    _setBadge(_badgeText, _badgeCls);

    _refreshProbeIfNeeded().then(() => _queueVbrCbrEstimate());
    _refreshProbeIfNeeded().then(() => _syncBitrateOptions());

    // State subscriptions — torn down on unmount (DOM lifetime only)
    _unlistenFns.push(State.on('selectedItems', newItems => {
      _refreshItemList(newItems);
      _updateStartBtn(newItems);
      // Reload lap list when session changes (reset picker state for new session)
      const newCsv = newItems[0]?.csv_path;
      const oldCsv = (State.get('selectedItems') || [])[0]?.csv_path;
      if (newCsv !== oldCsv) {
        _rangePickerStart = null;
        _rangePickerEnd   = null;
        _rangePickerLaps  = [];
      }
      const scope = _container?.querySelector('#exp-scope')?.value;
      if (scope === 'lap_range') _loadLapRange();
      _refreshProbeIfNeeded().then(() => _queueVbrCbrEstimate());
    }));
    _unlistenFns.push(State.on('previewSession', ps => {
      if (ps) State.set('selectedItems', [_itemFromPreview(ps)]);
    }));
    // NOTE: export_progress / export_log / export_done are registered once at
    // module level (see bottom of file) and are never unregistered, so events
    // are captured regardless of which tab is active.
  }

  function unmount() {
    if (_persistExportTimer) {
      clearTimeout(_persistExportTimer);
      _persistExportTimer = null;
    }
    const flushPayload = _container ? _collectExportCfgPayload() : null;
    _unlistenFns.forEach(fn => fn());
    _unlistenFns = [];
    _container   = null;
    if (flushPayload && Object.keys(flushPayload).length) {
      API.saveConfig(flushPayload).catch(() => {});
    }
  }

  // ── Export encoding helpers ─────────────────────────────────────────────────────

  function _parseKbpsAutoInput(el) {
    if (!el) return 0;
    const t = String(el.value || '').trim().toLowerCase();
    if (t === '' || t === 'auto') return 0;
    const n = parseInt(t, 10);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  }

  /** Full export card payload for save_config (encoding + timing + scope). */
  function _collectExportCfgPayload() {
    if (!_container) return {};
    const $ = id => _container.querySelector('#' + id);
    const base = _readExportOpts();
    const rs = parseInt($('exp-range-start')?.value, 10) || 1;
    const reRaw = $('exp-range-end')?.value?.trim();
    const reParsed = reRaw ? parseInt(reRaw, 10) : NaN;
    const export_lap_range_end = reRaw && Number.isFinite(reParsed) ? reParsed : null;
    let pad = parseFloat($('exp-padding')?.value);
    if (!Number.isFinite(pad)) pad = 5;
    pad = Math.min(60, Math.max(0, pad));
    const { export_path: _dropPath, ...persistOpt } = base;
    void _dropPath;
    return {
      ...persistOpt,
      export_path: '',
      export_scope: ($('exp-scope')?.value || 'full'),
      export_padding: pad,
      export_clip_start_s: Math.max(0, parseFloat($('exp-clip-start')?.value) || 0),
      export_clip_end_s: Math.max(0, parseFloat($('exp-clip-end')?.value) || 0),
      export_overlay_only: !!$('exp-overlay-only')?.checked,
      export_lap_range_start: Math.max(1, rs),
      export_lap_range_end,
      export_process_priority: ($('exp-export-prio')?.value || 'normal'),
    };
  }

  function _queuePersistExportCfg() {
    if (!_container) return;
    if (_persistExportTimer) clearTimeout(_persistExportTimer);
    _persistExportTimer = setTimeout(() => {
      _persistExportTimer = null;
      if (!_container) return;
      API.saveConfig(_collectExportCfgPayload()).catch(() => {});
    }, 450);
  }

  /** Node + canvas_export 就绪状态（导出仅 Canvas，无回退）。 */
  async function _updateCanvasExportHint(root) {
    if (!root) return;
    const el = root.querySelector('#exp-canvas-export-hint');
    if (!el) return;
    try {
      const st = await API.canvasExportStatus();
      if (st.ready) {
        el.textContent =
          '叠加层由 Canvas 渲染（与编辑器同源 gauge JS）；修改 gauges 后请运行 python tools/bundle_canvas_gauges.py。';
        el.style.color = 'var(--text3)';
      } else {
        const parts = [];
        if (!st.node) parts.push('未找到 Node 运行时（便携版需 Library/node/node.exe；开发机可装 Node 或设 OPENLAP_NODE）');
        if (!st.bundle || !st.server) parts.push('缺少 canvas_export 文件（gauge_bundle / render_server）');
        if (!st.napi_installed) parts.push('请在仓库 canvas_export 目录执行 npm install');
        el.textContent = '以下项不满足时导出将直接失败：' + parts.join('；') + '。';
        el.style.color = 'var(--warn, #d97706)';
      }
    } catch (_) {
      el.textContent = '';
    }
  }

  async function _refreshResolvedEncoder(root) {
    if (!root) return;
    const $ = id => root.querySelector('#' + id);
    const codec = $('exp-codec')?.value || 'h264';
    const fam = $('exp-encoder-family')?.value || 'auto';
    const hid = $('exp-encoder-resolved');
    try {
      const r = await API.resolveExportEncoder(codec, fam);
      if (r?.ok && hid) hid.value = r.encoder;
    } catch (_) {
      if (hid && !hid.value) hid.value = 'libx264';
    }
    _renderEncoderStatus(root);
  }

  function _syncEncoderFamilyOptions() {
    if (!_container) return;
    const famSel = _container.querySelector('#exp-encoder-family');
    const codec = (_container.querySelector('#exp-codec')?.value || 'h264').toLowerCase();
    if (!famSel) return;

    const want = enc => !!_encAvail[String(enc)];
    const hasNvenc = codec === 'h265' ? want('hevc_nvenc') : want('h264_nvenc');
    const hasAmf   = codec === 'h265' ? want('hevc_amf')   : want('h264_amf');
    const hasQsv   = codec === 'h265' ? want('hevc_qsv')   : want('h264_qsv');
    const hasVtb   = codec === 'h265' ? want('hevc_videotoolbox') : want('h264_videotoolbox');
    const hasHwAny = hasNvenc || hasAmf || hasQsv || hasVtb;

    const keep = new Set(['auto', 'cpu']);
    if (codec !== 'av1') {
      if (hasNvenc) keep.add('nvenc');
      if (hasAmf) keep.add('amf');
      if (hasQsv) keep.add('qsv');
      if (hasVtb) keep.add('videotoolbox');
    }

    const prev = famSel.value || 'auto';
    Array.from(famSel.options || []).forEach(opt => {
      const v = String(opt.value || '').toLowerCase();
      opt.hidden = !keep.has(v);
      if (v === 'auto') {
        opt.textContent = hasHwAny ? '自动（检测硬件）' : '自动（未检测到硬件）';
      }
    });

    const curOpt = Array.from(famSel.options || []).find(o => o.value === prev);
    if (!curOpt || curOpt.hidden) famSel.value = 'auto';
  }

  function _renderEncoderStatus(root) {
    const msg = root?.querySelector('#exp-enc-msg');
    const hid = root?.querySelector('#exp-encoder-resolved');
    if (!msg) return;
    const enc = String(hid?.value || '').trim();
    const fam = String(root?.querySelector('#exp-encoder-family')?.value || 'auto');
    const codec = String(root?.querySelector('#exp-codec')?.value || 'h264');
    const base = _ffmpegVersion ? `FFmpeg ${_ffmpegVersion}` : 'FFmpeg';
    if (!enc) { msg.textContent = base; return; }
    const isCpu = (enc === 'libx264' || enc === 'libx265' || enc === 'libsvtav1');
    const hint = (fam === 'auto' && isCpu) ? '（自动回退 CPU）' : '';
    msg.textContent = `${base} · 实际编码器：${enc}${hint}`;
    msg.title = `codec=${codec}, family=${fam}`;
  }

  // ── HTML skeleton ─────────────────────────────────────────────────────────────

  function _buildHTML(cfg, items) {
    const expSc = String(cfg.export_scope || 'full');
    const expPad = (cfg.export_padding != null && cfg.export_padding !== '')
      ? Number(cfg.export_padding) : 5;
    const cStart = (cfg.export_clip_start_s != null && cfg.export_clip_start_s !== '')
      ? Number(cfg.export_clip_start_s) : 0;
    const cEnd = (cfg.export_clip_end_s != null && cfg.export_clip_end_s !== '')
      ? Number(cfg.export_clip_end_s) : 0;
    const lrStart = (cfg.export_lap_range_start != null)
      ? Math.max(1, parseInt(cfg.export_lap_range_start, 10) || 1) : 1;
    const lrEndRaw = cfg.export_lap_range_end;
    const lrEndStr = (lrEndRaw != null && lrEndRaw !== '')
      ? String(parseInt(lrEndRaw, 10) || '') : '';
    const ovOnly = !!cfg.export_overlay_only;
    const scSel = v => (expSc === v ? 'selected' : '');
    const rm0 = String(cfg.export_rate_mode || 'cq').toLowerCase();
    const panelCqHidden = rm0 !== 'cq' ? 'hidden' : '';
    const panelVbrHidden = rm0 !== 'vbr' ? 'hidden' : '';
    const panelCbrHidden = rm0 !== 'cbr' ? 'hidden' : '';
    const expPrio = String(cfg.export_process_priority || 'normal').toLowerCase();
    const prioSel = v => (expPrio === v ? 'selected' : '');
    const tip = t => `<span class="info-tip" tabindex="0" data-tip="${_esc(t)}">?</span>`;
    return `
<div class="page export-page">
  <div class="toolbar">
    <div class="toolbar-left">
      <span class="page-title">导出</span>
    </div>
    <div class="toolbar-right">
      <button class="btn btn-primary" id="exp-start-btn" disabled>开始导出</button>
      <button class="btn btn-secondary hidden" id="exp-cancel-btn">取消</button>
    </div>
  </div>
  <div class="page-divider"></div>

  <div class="export-layout">

    <!-- LEFT: config panels -->
    <div class="export-config">

      <!-- Selected items -->
      <div class="card export-card">
        <div class="card-header">
          <span class="card-title">导出队列</span>
          <span class="badge" id="exp-item-count">0</span>
        </div>
        <div class="card-body" id="exp-item-list">
          <div class="empty-hint">尚未选择圈次，请到“数据”页添加。</div>
        </div>
      </div>

      <!-- Encoder & output folder -->
      <div class="card export-card">
        <div class="card-header">
          <span class="card-title">视频导出</span>
          <span class="save-msg" id="exp-save-msg" style="font-size:11px;margin-left:auto"></span>
        </div>
        <div class="card-body">
          <div class="form-row exp-export-path-row">
            <label>导出目录</label>
            <div class="exp-export-path-stack">
              <div class="path-row">
                <input type="text" id="exp-export-path" class="input-field exp-path-input"
                       value=""
                       placeholder="本次导出填写；留空 = 写入原视频同一文件夹">
                <button type="button" class="btn btn-secondary btn-sm" id="exp-browse-export">浏览…</button>
              </div>
              <div class="exp-path-hint">此文件夹路径不会保存在配置里（下次打开需重新填或浏览）；留空则输出到与原视频同一目录。文件名形如 VID_日期_时间；重名自动加 _2、_3。</div>
            </div>
          </div>
          <input type="hidden" id="exp-container-choice" value="match_source">
          <input type="hidden" id="exp-encoder-resolved" value="${_esc(cfg.encoder || 'libx264')}">
          <div class="form-row">
            <label title="封装扩展名与源文件一致">容器</label>
            <span class="exp-static-val">随原视频（自动）</span>
          </div>
          <div class="form-row">
            <label title="H.264 / H.265 / AV1 指压缩标准，与下方编码器（实现）独立">视频格式</label>
            <select id="exp-codec" class="input-field">
              <option value="h264" ${(cfg.export_video_codec || 'h264') === 'h264' ? 'selected' : ''}>H.264 / AVC</option>
              <option value="h265" ${cfg.export_video_codec === 'h265' ? 'selected' : ''}>H.265 / HEVC</option>
              <option value="av1" ${cfg.export_video_codec === 'av1' ? 'selected' : ''}>AV1</option>
            </select>
          </div>
          <div class="form-row">
            <label title="自动：依次尝试 NVENC、AMF、QSV、VideoToolbox，最后 CPU">编码器</label>
            <select id="exp-encoder-family" class="input-field">
              <option value="auto" ${(cfg.export_encoder_family || 'auto') === 'auto' ? 'selected' : ''}>自动（检测硬件）</option>
              <option value="cpu" ${cfg.export_encoder_family === 'cpu' ? 'selected' : ''}>CPU（libx264 / libx265）</option>
              <option value="nvenc" ${cfg.export_encoder_family === 'nvenc' ? 'selected' : ''}>NVIDIA NVENC</option>
              <option value="amf" ${cfg.export_encoder_family === 'amf' ? 'selected' : ''}>AMD AMF</option>
              <option value="qsv" ${cfg.export_encoder_family === 'qsv' ? 'selected' : ''}>Intel Quick Sync</option>
              <option value="videotoolbox" ${cfg.export_encoder_family === 'videotoolbox' ? 'selected' : ''}>Apple VideoToolbox</option>
            </select>
          </div>

          <div class="form-row">
            <label>编码模式 ${tip(
              'CQ：恒定质量（x264/x265 用 CRF；NVENC/AMF/QSV 用 CQ）。推荐先试 CRF 20–23。\n' +
              'VBR：可变码率（目标平均 + 可选峰值上限），适合压体积。\n' +
              'CBR：近似恒定码率，适合对峰值敏感的链路。\n' +
              '提示：VBR/CBR 未填写码率（auto）时，会按目标分辨率×目标帧率自动推一个“名义码率”，仅作起步值。'
            )}</label>
            <select id="exp-rate-mode" class="input-field">
              <option value="cq"  ${rm0 === 'cq'  ? 'selected' : ''}>CQ · 恒定质量</option>
              <option value="vbr" ${rm0 === 'vbr' ? 'selected' : ''}>VBR · 可变码率</option>
              <option value="cbr" ${rm0 === 'cbr' ? 'selected' : ''}>CBR · 恒定码率</option>
            </select>
          </div>

          <div id="exp-panel-cq" class="exp-rate-panel ${panelCqHidden}">
            <div class="form-row">
              <label title="数值越小画质越好、文件越大。">质量（CRF / CQ）</label>
              <div class="range-row">
                <input type="range" id="exp-crf" min="12" max="32" step="1"
                       value="${cfg.crf ?? 18}">
                <span class="range-val" id="exp-crf-val">${cfg.crf ?? 18}</span>
              </div>
            </div>
            <div class="form-row">
              <label>最高质量</label>
              <label class="toggle-switch">
                <input type="checkbox" id="exp-max-quality" ${cfg.export_max_quality ? 'checked' : ''}>
                <span class="toggle-thumb"></span>
              </label>
            </div>
          </div>

          <div id="exp-panel-vbr" class="exp-rate-panel ${panelVbrHidden}">
            <div class="form-row exp-vb-row" id="exp-vb-row">
              <label id="exp-vb-label" for="exp-vb-kbps">平均视频码率 (kbps) ${tip(
                '留空 / auto：导出时按“目标分辨率×目标帧率”的像素吞吐量估一个名义码率。\n' +
                '实现：kbps ≈ (w×h×fps×bpp) / 1000；bpp 在 CRF18 附近约 0.10，随“质量( CRF/CQ )”上调/下调；HEVC 约乘 0.70；并限制在 300–80000kbps。\n' +
                '如果你填了“最大码率”，会把名义码率上限裁到 maxrate。'
              )}</label>
              <input type="text" id="exp-vb-kbps" class="input-field input-narrow" inputmode="numeric" pattern="[0-9]*"
                     list="exp-vb-kbps-list"
                     value="${(cfg.export_video_bitrate_kbps > 0) ? String(cfg.export_video_bitrate_kbps) : ''}"
                     placeholder="auto" title="留空 = 导出时在管线内按分辨率×帧率给出默认目标码率；VBR/CBR 下会参与下方体积参考估算。">
            </div>
            <div class="form-row" id="exp-vmax-row">
              <label for="exp-vmax-kbps">最大码率 (kbps)</label>
              <input type="text" id="exp-vmax-kbps" class="input-field input-narrow" inputmode="numeric" pattern="[0-9]*"
                     list="exp-vmax-kbps-list"
                     value="${(cfg.export_video_max_bitrate_kbps > 0) ? String(cfg.export_video_max_bitrate_kbps) : ''}"
                     placeholder="auto" title="留空 = 与 FFmpeg 默认峰值（约 1.85× 平均）一致">
            </div>
            <datalist id="exp-vb-kbps-list"></datalist>
            <datalist id="exp-vmax-kbps-list"></datalist>
          </div>

          <div id="exp-panel-cbr" class="exp-rate-panel ${panelCbrHidden}">
            <div id="exp-cbr-slot-vb" class="exp-cbr-slot"></div>
          </div>

          <div id="exp-bitrate-pool" class="hidden" aria-hidden="true"></div>

          <div id="exp-vbr-cbr-estimate-row" class="form-row hidden">
            <label title="仅 VBR/CBR：按当前目标码率、时段与封装粗算输出体积；实际以编码为准。CQ 恒定质量无固定码率，故不做体积预估算。">体积参考</label>
            <div id="exp-vbr-cbr-size-hint" class="exp-vbr-cbr-size-hint">—</div>
          </div>

          <div class="form-row">
            <label>并发进程 ${tip(
              '导出时用于“逐帧渲染叠加层”的并行 worker 数（多进程）。\n' +
              '越大通常越快，但更吃 CPU/内存；过大可能导致系统卡顿。\n' +
              '它不改变画质，只影响渲染阶段速度（编码阶段仍由 ffmpeg 负责）。'
            )}</label>
            <input type="number" id="exp-workers" class="input-field input-narrow"
                   value="${cfg.workers ?? 4}" min="1" max="16" step="1">
          </div>

          <div class="form-row">
            <label>导出进程优先级 ${tip(
              '仅 Windows：降低 OpenLap 在导出时的进程优先级，减轻与其它程序抢 CPU（导出可能更慢）。\n' +
              '也可用环境变量 OPENLAP_EXPORT_PRIORITY 覆盖。'
            )}</label>
            <select id="exp-export-prio" class="input-field">
              <option value="normal" ${prioSel('normal')}>正常</option>
              <option value="below_normal" ${prioSel('below_normal')}>低于正常</option>
              <option value="idle" ${prioSel('idle')}>空闲（最温和）</option>
            </select>
          </div>

          <div class="form-row">
            <label>音频</label>
            <select id="exp-ab-kbps" class="input-field">
              <option value="0" ${!(cfg.export_audio_bitrate_kbps > 0) ? 'selected' : ''}>原片码率（优先无损拷贝）</option>
              ${[64, 96, 128, 160, 192, 256, 320].map(v =>
                `<option value="${v}" ${cfg.export_audio_bitrate_kbps === v ? 'selected' : ''}>重编码 ${v} kbps</option>`,
              ).join('')}
            </select>
          </div>

          <div class="form-row">
            <label>分辨率</label>
            <select id="exp-res" class="input-field">
              <option value="auto" selected>自动（原分辨率）</option>
            </select>
          </div>

          <div class="form-row">
            <label>帧率</label>
            <select id="exp-fps" class="input-field">
              <option value="auto" selected>自动（原帧率）</option>
            </select>
          </div>

          <div class="form-row exp-ffmpeg-foot">
            <span id="exp-enc-msg" class="status-msg" style="font-size:10px"></span>
          </div>
          <div class="form-row" style="margin-top:6px">
            <button type="button" class="btn btn-accent btn-sm" id="exp-save-export-cfg">保存选项</button>
          </div>
        </div>
      </div>

      <!-- Timing -->
      <div class="card export-card">
        <div class="card-header"><span class="card-title">时间范围</span></div>
        <div class="card-body">
          <div class="form-row">
            <label>前后补时 (s)</label>
            <input type="number" id="exp-padding" class="input-field input-narrow"
                   value="${expPad}" min="0" max="60" step="0.5">
          </div>
          <div class="form-row exp-clip-row hidden">
            <label>片段起点 (s)</label>
            <input type="number" id="exp-clip-start" class="input-field input-narrow"
                   value="${cStart}" min="0" step="0.1">
          </div>
          <div class="form-row exp-clip-row hidden">
            <label>片段终点 (s)</label>
            <input type="number" id="exp-clip-end" class="input-field input-narrow"
                   value="${cEnd}" min="0" step="0.1">
          </div>
          <div class="exp-range-row hidden" style="flex-direction:column;gap:4px;padding:2px 0 4px">
            <div id="exp-range-header"
                 style="font-size:10px;color:var(--text3);font-style:italic">
              全部圈次（含出场/收车）— 先点起始圈，再点结束圈
            </div>
            <div id="exp-lap-list"
                 style="max-height:170px;overflow-y:auto;border:1px solid var(--border);
                        border-radius:3px;background:var(--bg)">
              <div style="padding:8px;font-size:10px;color:var(--text3)">
                请选择节后查看圈次。
              </div>
            </div>
            <input type="hidden" id="exp-range-start" value="${lrStart}">
            <input type="hidden" id="exp-range-end" value="${_esc(lrEndStr)}">
          </div>
          <div class="form-row">
            <label>导出范围</label>
            <select id="exp-scope" class="input-field">
              <option value="selected_lap" ${scSel('selected_lap')}>当前圈</option>
              <option value="lap_range" ${scSel('lap_range')}>圈段范围（单视频）</option>
              <option value="fastest_lap" ${scSel('fastest_lap')}>最快圈</option>
              <option value="all_laps" ${scSel('all_laps')}>全部圈（每圈一个文件）</option>
              <option value="all_laps_data_end" ${scSel('all_laps_data_end')}>全部圈（单文件：从视频开头到数据结尾）</option>
              <option value="full" ${scSel('full')}>完整节</option>
            </select>
          </div>
          <div class="form-row">
            <label>仅导出叠加层 (.mov)</label>
            <input type="checkbox" id="exp-overlay-only" class="input-checkbox"
                   ${ovOnly ? 'checked' : ''}
                   title="导出透明 ProRes 4444 叠加层，可在 DaVinci Resolve / Premiere / Final Cut 里盖到原视频上。">
          </div>
          <div class="form-row" style="margin-top:2px">
            <span id="exp-canvas-export-hint" style="font-size:10px;color:var(--text3);line-height:1.35"></span>
          </div>
        </div>
      </div>

    </div><!-- /.export-config -->

    <!-- RIGHT: progress + log -->
    <div class="export-progress-panel">
      <div class="card export-card full-height">
        <div class="card-header">
          <span class="card-title">进度</span>
          <span class="badge badge-dim" id="exp-status-badge">空闲</span>
        </div>
        <div class="card-body progress-body">
          <div class="progress-bar-wrap">
            <div class="progress-bar-track">
              <div class="progress-bar-fill" id="exp-progress-fill" style="width:0%"></div>
            </div>
            <span class="progress-pct" id="exp-progress-pct">0%</span>
          </div>
          <div class="progress-status" id="exp-progress-msg"></div>
          <textarea class="log-area" id="exp-log" readonly placeholder="导出日志会显示在这里…"></textarea>
        </div>
      </div>
    </div>

  </div><!-- /.export-layout -->
</div>`;
  }

  function _saveExportCfg() {
    if (!_container) return;
    const $ = id => _container.querySelector('#' + id);
    API.saveConfig(_collectExportCfgPayload()).then(() => {
      const msg = $('exp-save-msg');
      if (msg) {
        msg.textContent = '已保存';
        msg.style.color = 'var(--ok)';
        setTimeout(() => { msg.textContent = ''; }, 2000);
      }
    }).catch(() => {});
  }

  /** Encoder / paths from Export panel (used when DOM is present). */
  function _readExportOpts() {
    if (!_container) return {};
    const $ = id => _container.querySelector('#' + id);
    const pathEl = $('exp-export-path');
    const codecEl = $('exp-codec');
    const famEl = $('exp-encoder-family');
    const hidEnc = $('exp-encoder-resolved');
    const crfEl  = $('exp-crf');
    const wrkEl  = $('exp-workers');
    const ccEl   = $('exp-container-choice');
    const rmEl   = $('exp-rate-mode');
    const vbEl   = $('exp-vb-kbps');
    const vmaxEl = $('exp-vmax-kbps');
    const abEl   = $('exp-ab-kbps');
    const mqEl   = $('exp-max-quality');
    const resEl  = $('exp-res');
    const fpsEl  = $('exp-fps');
    const prioEl = $('exp-export-prio');
    if (!pathEl && !codecEl) return {};
    return {
      export_path: (pathEl?.value || '').trim(),
      export_video_codec: (codecEl?.value || 'h264').toLowerCase(),
      export_encoder_family: (famEl?.value || 'auto').toLowerCase(),
      encoder: (hidEnc?.value || '').trim() || 'libx264',
      crf:         Math.min(32, Math.max(12, parseInt(crfEl?.value, 10) || 18)),
      workers:     Math.min(16, Math.max(1, parseInt(wrkEl?.value, 10) || 4)),
      export_container_choice: ccEl?.value || 'match_source',
      export_rate_mode: (rmEl?.value || 'cq').toLowerCase(),
      export_video_bitrate_kbps: _parseKbpsAutoInput(vbEl),
      export_video_max_bitrate_kbps: _parseKbpsAutoInput(vmaxEl),
      export_audio_bitrate_kbps: Math.max(0, Math.min(320, parseInt(abEl?.value, 10) || 0)),
      export_max_quality: !!mqEl?.checked,
      export_target_res: resEl?.value || 'auto',
      export_target_fps: fpsEl?.value || 'auto',
      export_process_priority: String(prioEl?.value || 'normal').toLowerCase(),
    };
  }

  function _fmtBytes(n) {
    const v = Number(n || 0);
    if (!isFinite(v) || v <= 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let x = v;
    let i = 0;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return `${x.toFixed(i === 0 ? 0 : 2)} ${units[i]}`;
  }

  function _cqBpp(crf) {
    const c = Math.max(0, Math.min(51, parseInt(crf, 10) || 18));
    const bpp = 0.10 - (c - 18) * 0.003;
    return Math.max(0.03, Math.min(0.16, bpp));
  }

  function _computeAutoVideoKbps() {
    if (!_container) return null;
    const $ = id => _container.querySelector('#' + id);
    const probe = _videoProbe || {};
    const crf = parseInt($('exp-crf')?.value, 10) || 18;
    const vmax = _parseKbpsAutoInput($('exp-vmax-kbps'));
    const enc = String($('exp-encoder-resolved')?.value || '').toLowerCase();

    // Target resolution / fps
    let w = parseInt(probe.width, 10) || 0;
    let h = parseInt(probe.height, 10) || 0;
    let fps = parseFloat(probe.fps) || 0;

    const res = String($('exp-res')?.value || 'auto').toLowerCase();
    if (res !== 'auto' && res.includes('x')) {
      try {
        const [a, b] = res.split('x', 2);
        const tw = Math.max(2, parseInt(a, 10) || w);
        const th = Math.max(2, parseInt(b, 10) || h);
        w = tw - (tw % 2);
        h = th - (th % 2);
      } catch (_) { /* ignore */ }
    }
    const tf = String($('exp-fps')?.value || 'auto').toLowerCase();
    if (tf !== 'auto') {
      const f = parseFloat(tf);
      if (isFinite(f) && f > 0) fps = f;
    }

    if (!(w > 0 && h > 0 && fps > 0)) return null;

    const pxRate = Math.max(1, w) * Math.max(1, h) * Math.max(1e-3, fps);
    let kbps = (pxRate * _cqBpp(crf)) / 1000.0;
    if (enc.includes('265') || enc.includes('hevc')) kbps *= 0.70;
    kbps = Math.max(300.0, Math.min(80_000.0, kbps));
    if (vmax > 0) kbps = Math.min(kbps, vmax);
    return Math.round(kbps);
  }

  function _syncBitrateOptions() {
    if (!_container) return;
    const vb = _container.querySelector('#exp-vb-kbps');
    const vmax = _container.querySelector('#exp-vmax-kbps');
    const vbList = _container.querySelector('#exp-vb-kbps-list');
    const vmaxList = _container.querySelector('#exp-vmax-kbps-list');
    if (!vbList || !vmaxList) return;

    const common = [500, 800, 1000, 1500, 2000, 2500, 3000, 5000, 8000, 10000, 15000, 20000, 25000, 30000, 40000, 50000];
    const autoKbps = _computeAutoVideoKbps();
    const autoLabel = autoKbps ? `auto（≈${autoKbps} kbps）` : 'auto（计算中…）';

    const opts = [
      `<option value="auto" label="${_esc(autoLabel)}"></option>`,
      ...common.map(n => `<option value="${n}"></option>`),
    ].join('');
    vbList.innerHTML = opts;
    vmaxList.innerHTML = [
      `<option value="auto" label="auto（≈1.85×平均；可留空）"></option>`,
      ...common.map(n => `<option value="${n}"></option>`),
    ].join('');

    if (vb) vb.placeholder = autoKbps ? `auto（≈${autoKbps}）` : 'auto';
    if (vmax) vmax.placeholder = 'auto';
  }

  function _syncVbrCbrEstimateRowVisibility() {
    if (!_container) return;
    const rm = (_container.querySelector('#exp-rate-mode')?.value || 'cq').toLowerCase();
    const row = _container.querySelector('#exp-vbr-cbr-estimate-row');
    if (row) row.classList.toggle('hidden', rm !== 'vbr' && rm !== 'cbr');
  }

  /** 仅 VBR/CBR：防抖调用 estimate_export_size，更新「体积参考」。CQ 不调接口。 */
  function _queueVbrCbrEstimate() {
    if (_vbrCbrEstimateTimer) clearTimeout(_vbrCbrEstimateTimer);
    _vbrCbrEstimateTimer = setTimeout(async () => {
      _vbrCbrEstimateTimer = null;
      if (!_container) return;
      const hintEl = _container.querySelector('#exp-vbr-cbr-size-hint');
      const items = State.get('selectedItems') || [];
      const rm = (_container.querySelector('#exp-rate-mode')?.value || 'cq').toLowerCase();
      if (rm !== 'vbr' && rm !== 'cbr') {
        if (hintEl) hintEl.textContent = '—';
        return;
      }
      if (!items.length) {
        if (hintEl) hintEl.textContent = '—';
        return;
      }

      const exp = _readExportOpts();
      const scope = _container.querySelector('#exp-scope')?.value || 'full';
      const padding = parseFloat(_container.querySelector('#exp-padding')?.value) || 0;
      const clip_start_s = parseFloat(_container.querySelector('#exp-clip-start')?.value) || 0;
      const clip_end_s = parseFloat(_container.querySelector('#exp-clip-end')?.value) || 0;
      const rs = parseInt(_container.querySelector('#exp-range-start')?.value, 10) || 1;
      const reRaw = _container.querySelector('#exp-range-end')?.value?.trim();
      const re = reRaw ? parseInt(reRaw, 10) : null;

      const params = {
        items,
        scope,
        padding,
        clip_start_s,
        clip_end_s,
        overlay_only: !!_container.querySelector('#exp-overlay-only')?.checked,
        export_video_codec: exp.export_video_codec,
        export_encoder_family: exp.export_encoder_family,
        encoder: exp.encoder,
        crf: exp.crf,
        workers: exp.workers,
        export_path: exp.export_path,
        export_container_choice: exp.export_container_choice,
        export_rate_mode: exp.export_rate_mode,
        export_video_bitrate_kbps: exp.export_video_bitrate_kbps,
        export_video_max_bitrate_kbps: exp.export_video_max_bitrate_kbps,
        export_audio_bitrate_kbps: exp.export_audio_bitrate_kbps,
        export_max_quality: exp.export_max_quality,
        export_target_res: exp.export_target_res,
        export_target_fps: exp.export_target_fps,
      };
      if (scope === 'lap_range') {
        params.items = params.items.map(it => ({
          ...it,
          scope: 'lap_range',
          lap_range_start: rs,
          lap_range_end: re,
        }));
      }

      try {
        const r = await API.estimateExportSize(params);
        if (!hintEl) return;
        if (!r || r.ok === false || r.skipped) {
          hintEl.textContent = r?.hint || '—';
          return;
        }
        const bytes = r.total_bytes != null ? r.total_bytes : r.estimated_bytes;
        const size = _fmtBytes(bytes);
        const totalSec = r.total_seconds != null && isFinite(r.total_seconds)
          ? r.total_seconds
          : (Array.isArray(r.segment_lengths_s)
            ? r.segment_lengths_s.reduce((a, b) => a + Number(b || 0), 0)
            : null);
        const dur = (totalSec != null && isFinite(totalSec))
          ? `${Math.round(totalSec)}s`
          : '';
        const ext = (r.output_extension || r.container_extension)
          ? ` · ${r.output_extension || r.container_extension}`
          : '';
        hintEl.textContent = `${size}${dur ? ' · ' + dur : ''}${ext}`;
      } catch (_) {
        if (hintEl) hintEl.textContent = '—';
      }
    }, 250);
  }

  async function _refreshProbeIfNeeded() {
    if (!_container) return;
    const items = State.get('selectedItems') || [];
    const vpath = items[0]?.video_paths?.[0] || '';
    if (!vpath || vpath === _probeVideo) return;
    _probeVideo = vpath;
    _videoProbe = null;
    try {
      const r = await API.getVideoProbe(vpath);
      if (r && r.ok) _videoProbe = r;
    } catch (e) {}
    _populateResFps();
  }

  function _populateResFps() {
    if (!_container) return;
    const resSel = _container.querySelector('#exp-res');
    const fpsSel = _container.querySelector('#exp-fps');
    if (!resSel || !fpsSel) return;

    const keepRes = resSel.value;
    const keepFps = fpsSel.value;

    const w0 = Math.max(0, parseInt(_videoProbe?.width || 0));
    const h0 = Math.max(0, parseInt(_videoProbe?.height || 0));
    const fps0 = Math.max(0, parseFloat(_videoProbe?.fps || 0));

    const resOptions = [{ value: 'auto', label: '自动（原分辨率）' }];
    if (w0 > 0 && h0 > 0) {
      const targetsH = [h0, 2160, 1440, 1080, 720, 540, 480, 360, 240].filter(x => x > 0 && x <= h0);
      const seen = new Set();
      for (const th of targetsH) {
        const tw = Math.floor((w0 * th) / h0);
        const evenW = Math.max(2, tw - (tw % 2));
        const evenH = Math.max(2, th - (th % 2));
        const key = `${evenW}x${evenH}`;
        if (seen.has(key)) continue;
        seen.add(key);
        if (evenW <= w0 && evenH <= h0) resOptions.push({ value: key, label: key });
      }
    }

    const fpsOptions = [{ value: 'auto', label: '自动（原帧率）' }];
    if (fps0 > 0) {
      const candidates = [fps0, 120, 100, 60, 50, 30, 25, 24, 20, 15, 10, 5]
        .filter(x => x > 0 && x <= fps0 + 1e-6);
      const seen = new Set();
      for (const f of candidates) {
        const val = (Math.round(f * 1000) / 1000).toString();
        if (seen.has(val)) continue;
        seen.add(val);
        fpsOptions.push({ value: val, label: `${val} fps` });
      }
    }

    resSel.innerHTML = resOptions.map(o => `<option value="${o.value}">${o.label}</option>`).join('');
    fpsSel.innerHTML = fpsOptions.map(o => `<option value="${o.value}">${o.label}</option>`).join('');

    if (keepRes && Array.from(resSel.options).some(o => o.value === keepRes)) resSel.value = keepRes;
    if (keepFps && Array.from(fpsSel.options).some(o => o.value === keepFps)) fpsSel.value = keepFps;
  }

  // ── Event wiring ──────────────────────────────────────────────────────────────

  function _bindEvents(cfg, items) {
    const $ = id => _container.querySelector('#' + id);

    // ── Lap range picker ──────────────────────────────────────────────────────

    function _renderLapList() {
      const listEl  = $('exp-lap-list');
      const hdrEl   = $('exp-range-header');
      if (!listEl) return;

      if (!_rangePickerLaps.length) {
        listEl.innerHTML =
          '<div style="padding:8px;font-size:10px;color:var(--text3)">暂无圈数据。</div>';
        if (hdrEl) hdrEl.textContent = '本节没有可用的圈，无法选取范围。';
        return;
      }

      const s = _rangePickerStart, e = _rangePickerEnd;

      if (hdrEl) {
        if (s == null) {
          hdrEl.textContent = '点击某圈设置起点。';
        } else if (e == null) {
          hdrEl.textContent = `起点：第 ${s} 圈 — 再点一圈设置终点。`;
        } else {
          hdrEl.textContent = `范围：第 ${s} 圈 → 第 ${e} 圈`;
        }
      }

      listEl.innerHTML = _rangePickerLaps.map(lap => {
        const inRange  = s != null && e != null && lap.lap_num >= s && lap.lap_num <= e;
        const isStart  = lap.lap_num === s;
        const isEnd    = lap.lap_num === e;
        const dur      = _fmtTime(lap.duration);

        let bg    = 'transparent';
        let color = 'inherit';
        let badge = '';
        if (isStart && isEnd) { bg = 'var(--acc)';   color = '#fff'; badge = '<span style="font-size:8px;opacity:.8;margin-left:4px">起止</span>'; }
        else if (isStart)     { bg = 'var(--acc)';   color = '#fff'; badge = '<span style="font-size:8px;opacity:.8;margin-left:4px">起点</span>'; }
        else if (isEnd)       { bg = 'var(--acc)';   color = '#fff'; badge = '<span style="font-size:8px;opacity:.8;margin-left:4px">终点</span>'; }
        else if (inRange)     { bg = 'rgba(var(--acc-rgb,99,102,241),0.15)'; }

        const tagExtra = lap.is_outlap ? ' <span style="opacity:.75">(出场)</span>'
          : lap.is_inlap ? ' <span style="opacity:.75">(收车)</span>' : '';
        return `<div class="exp-lap-item" data-num="${lap.lap_num}"
                     style="display:flex;justify-content:space-between;align-items:center;
                            padding:4px 8px;cursor:pointer;user-select:none;
                            background:${bg};color:${color};font-size:10px;
                            border-bottom:1px solid var(--border)">
                  <span>Lap ${lap.lap_num}${tagExtra}${lap.is_best ? ' ★' : ''}${badge}</span>
                  <span style="font-variant-numeric:tabular-nums;color:${color === '#fff' ? '#fff' : 'var(--acc2)'}">${dur}</span>
                </div>`;
      }).join('');

      // Update hidden inputs
      const startInp = $('exp-range-start');
      const endInp   = $('exp-range-end');
      if (startInp) startInp.value = s ?? 1;
      if (endInp)   endInp.value   = e ?? '';

      // Wire clicks
      listEl.querySelectorAll('.exp-lap-item').forEach(row => {
        row.addEventListener('click', () => {
          const num = parseInt(row.dataset.num);
          if (_rangePickerStart == null || (_rangePickerStart != null && _rangePickerEnd != null)) {
            // No selection or complete selection: start fresh
            _rangePickerStart = num;
            _rangePickerEnd   = null;
          } else if (num > _rangePickerStart) {
            // Extend to end
            _rangePickerEnd = num;
          } else if (num === _rangePickerStart) {
            // Click same lap: make it a single-lap range
            _rangePickerEnd = num;
          } else {
            // Click before start: reset start
            _rangePickerStart = num;
            _rangePickerEnd   = null;
          }
          _renderLapList();
          _queueVbrCbrEstimate();
          _queuePersistExportCfg();
        });
      });
    }

    async function _loadLapRange() {
      const listEl = $('exp-lap-list');
      if (!listEl) return;
      const items = State.get('selectedItems') || [];
      const csvPath = items[0]?.csv_path;
      if (!csvPath) {
        _rangePickerLaps = [];
        _renderLapList();
        return;
      }
      listEl.innerHTML = '<div style="padding:8px;font-size:10px;color:var(--text3)">加载中…</div>';
      try {
        const all = await API.getLaps(csvPath);
        _rangePickerLaps = (all || []).slice().sort((a, b) =>
          (a.lap_num ?? 0) - (b.lap_num ?? 0),
        ).map(l => ({
          lap_num:   l.lap_num,
          duration:  l.duration,
          is_best:   !!l.is_best,
          is_outlap: !!l.is_outlap,
          is_inlap:  !!l.is_inlap,
        }));
        // Restore lap range from hidden inputs (config) when valid; else full range
        if (_rangePickerLaps.length) {
          const rs = parseInt($('exp-range-start')?.value, 10);
          const reRaw = $('exp-range-end')?.value?.trim();
          const re = reRaw ? parseInt(reRaw, 10) : null;
          const nums = new Set(_rangePickerLaps.map(l => l.lap_num));
          const lapOk = n => n != null && !Number.isNaN(n) && nums.has(n);
          if (lapOk(rs) && lapOk(re) && re >= rs) {
            _rangePickerStart = rs;
            _rangePickerEnd = re;
          } else if (_rangePickerStart == null) {
            _rangePickerStart = _rangePickerLaps[0].lap_num;
            _rangePickerEnd = _rangePickerLaps[_rangePickerLaps.length - 1].lap_num;
          }
        } else {
          _rangePickerStart = null;
          _rangePickerEnd = null;
        }
      } catch (e) {
        _rangePickerLaps = [];
      }
      _renderLapList();
    }

    // Show/hide scope-specific rows
    const _syncScopeRows = () => {
      const scope = $('exp-scope')?.value || 'full';
      _container.querySelectorAll('.exp-clip-row').forEach(r => {
        r.classList.toggle('hidden', scope !== 'clip');
      });
      _container.querySelectorAll('.exp-range-row').forEach(r => {
        r.classList.toggle('hidden', scope !== 'lap_range');
      });
      if (scope === 'lap_range') _loadLapRange();
    };
    _syncScopeRows();
    $('exp-scope').addEventListener('change', () => {
      _syncScopeRows();
      _queueVbrCbrEstimate();
      _queuePersistExportCfg();
    });

    $('exp-browse-export').addEventListener('click', async () => {
      const path = await API.openFolderDialog();
      const inp = $('exp-export-path');
      if (path && inp) inp.value = path;
      _queueVbrCbrEstimate();
    });
    const pathInp = $('exp-export-path');
    if (pathInp) {
      pathInp.addEventListener('input', () => { _queueVbrCbrEstimate(); });
      pathInp.addEventListener('change', () => { _queueVbrCbrEstimate(); });
    }
    $('exp-crf').addEventListener('input', e => {
      const lab = $('exp-crf-val');
      if (lab) lab.textContent = e.target.value;
      _queueVbrCbrEstimate();
      _queuePersistExportCfg();
    });
    $('exp-save-export-cfg').addEventListener('click', () => _saveExportCfg());

    const _onCodecOrFamily = () => {
      _syncEncoderFamilyOptions();
      _refreshResolvedEncoder(_container).then(() => {
        _syncBitrateOptions();
        _queueVbrCbrEstimate();
        _queuePersistExportCfg();
      });
    };
    $('exp-codec')?.addEventListener('change', _onCodecOrFamily);
    $('exp-encoder-family')?.addEventListener('change', _onCodecOrFamily);

    function _syncRateModePanels() {
      if (!_container) return;
      const $ = id => _container.querySelector('#' + id);
      const rm = ($('exp-rate-mode')?.value || 'cq').toLowerCase();
      const cq = _container.querySelector('#exp-panel-cq');
      const vbr = _container.querySelector('#exp-panel-vbr');
      const cbr = _container.querySelector('#exp-panel-cbr');
      const pool = _container.querySelector('#exp-bitrate-pool');
      const vbRow = _container.querySelector('#exp-vb-row');
      const vmaxRow = _container.querySelector('#exp-vmax-row');
      const cbrSlot = _container.querySelector('#exp-cbr-slot-vb');

      [cq, vbr, cbr].forEach(p => { if (p) p.classList.add('hidden'); });
      if (rm === 'cq' && cq) {
        cq.classList.remove('hidden');
        if (pool && vbRow) pool.appendChild(vbRow);
        if (pool && vmaxRow) pool.appendChild(vmaxRow);
      } else if (rm === 'vbr' && vbr) {
        vbr.classList.remove('hidden');
        const hint = vbr.querySelector('.exp-mode-hint');
        if (hint && vbRow && vmaxRow) {
          hint.after(vbRow);
          vbRow.after(vmaxRow);
        } else if (vbr && vbRow && vmaxRow) {
          vbr.appendChild(vbRow);
          vbr.appendChild(vmaxRow);
        }
        const lbl = $('exp-vb-label');
        if (lbl) lbl.textContent = '平均视频码率 (kbps)';
      } else if (rm === 'cbr' && cbr) {
        cbr.classList.remove('hidden');
        if (cbrSlot && vbRow) cbrSlot.appendChild(vbRow);
        if (pool && vmaxRow) pool.appendChild(vmaxRow);
        const lbl = $('exp-vb-label');
        if (lbl) lbl.textContent = '目标视频码率 (kbps)';
      }
      _syncVbrCbrEstimateRowVisibility();
      _queueVbrCbrEstimate();
    }
    _syncRateModePanels();
    $('exp-rate-mode')?.addEventListener('change', () => {
      _syncRateModePanels();
      _queuePersistExportCfg();
    });

    const _estAndPersist = () => { _syncBitrateOptions(); _queueVbrCbrEstimate(); _queuePersistExportCfg(); };
    [
      'exp-padding', 'exp-clip-start', 'exp-clip-end', 'exp-overlay-only',
      'exp-workers', 'exp-export-prio',
      'exp-container-choice', 'exp-vb-kbps', 'exp-vmax-kbps', 'exp-ab-kbps',
      'exp-max-quality', 'exp-res', 'exp-fps',
    ].forEach(id => {
      const el = $(id);
      if (!el) return;
      el.addEventListener('input', _estAndPersist);
      el.addEventListener('change', _estAndPersist);
    });
    // Start
    $('exp-start-btn').addEventListener('click', () => _startExport());

    // Cancel
    $('exp-cancel-btn').addEventListener('click', async () => {
      await API.cancelExport();
      _setExporting(false);
    });
  }

  // ── Helpers ───────────────────────────────────────────────────────────────────

  function _refreshItemList(items) {
    if (!_container) return;
    const list  = _container.querySelector('#exp-item-list');
    const badge = _container.querySelector('#exp-item-count');
    if (!list) return;

    badge.textContent = items.length;

    if (!items.length) {
      list.innerHTML = '<div class="empty-hint">尚未选择圈次，请到“数据”页添加。</div>';
      return;
    }

    // Group items by session CSV path for a tidy display
    const bySession = {};
    for (const item of items) {
      const key = item.csv_path || '未知';
      if (!bySession[key]) bySession[key] = { csv_path: key, source: item.source || '', laps: [] };
      bySession[key].laps.push(item);
    }

    list.innerHTML = Object.values(bySession).map(sess => {
      // Use track name if available, fall back to CSV basename
      const track    = sess.laps[0]?.track || '';
      const csvDate  = sess.laps[0]?.csv_start
        ? new Date(sess.laps[0].csv_start).toLocaleDateString() : '';
      const headline = track
        ? `${track}${csvDate ? '  ·  ' + csvDate : ''}`
        : _baseName(sess.csv_path);

      const chips = sess.laps.map(l => {
        const label = l.lap_label
          ? `Lap ${l.lap_idx + 1}${l.is_best ? ' ★' : ''} — ${_fmtTime(l.duration)}`
          : (l.duration != null ? _fmtTime(l.duration) : '—');
        const scope = l.scope ? ` [${l.scope.replace('_', ' ')}]` : '';
        return `<span class="lap-chip${l.is_best ? ' best' : ''}"
                      title="${_esc(l.csv_path)} · lap ${l.lap_idx + 1}${_esc(scope)}">
                  ${_esc(label)}${_esc(scope)}
                  <button class="chip-remove" data-csv="${_esc(l.csv_path)}" data-lapidx="${l.lap_idx}" title="Remove this lap">✕</button>
                </span>`;
      }).join('');
      return `
        <div class="item-row">
          <div class="item-name" title="${_esc(sess.csv_path)}">${_esc(headline)}</div>
          <div class="item-chips">${chips}</div>
        </div>`;
    }).join('');

    // Per-lap remove buttons (inside each chip)
    list.querySelectorAll('.chip-remove').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const csv    = btn.dataset.csv;
        const lapIdx = parseInt(btn.dataset.lapidx);
        const current = State.get('selectedItems') || [];
        State.set('selectedItems', current.filter(i => !(i.csv_path === csv && i.lap_idx === lapIdx)));
      });
    });
  }

  function _updateStartBtn(items) {
    if (!_container) return;
    const btn = _container.querySelector('#exp-start-btn');
    if (btn) btn.disabled = items.length === 0 || _exporting;
  }

  async function _startExport() {
    if (!_container) return;
    const items = State.get('selectedItems') || [];
    if (!items.length) return;

    const $ = id => _container.querySelector('#' + id);

    // Fetch config (encoder) and overlay layout (ref_mode, is_bike, show_map/tel)
    const [cfg, layout] = await Promise.all([
      API.getConfig().catch(() => ({})),
      API.getOverlay().catch(() => ({})),
    ]);

    const exp = _readExportOpts();
    await API.saveConfig(_collectExportCfgPayload()).catch(() => {});

    const _rangeStart = parseInt($('exp-range-start')?.value) || 1;
    const _rangeEndRaw = $('exp-range-end')?.value?.trim();
    const _rangeEnd   = _rangeEndRaw ? parseInt(_rangeEndRaw) : null;

    const params = {
      items:            items,
      scope:            $('exp-scope')?.value    || 'full',
      clip_start_s:     parseFloat($('exp-clip-start')?.value) || 0,
      clip_end_s:       parseFloat($('exp-clip-end')?.value)   || 0,
      padding:          parseFloat($('exp-padding').value)    || 5.0,
      ref_mode:         layout.ref_mode          || DEFAULT_REF_MODE,
      ref_lap_csv_path: layout.ref_lap_csv_path  || '',
      ref_lap_num:      layout.ref_lap_num        || 0,
      export_video_codec: exp.export_video_codec ?? cfg.export_video_codec ?? 'h264',
      export_encoder_family: exp.export_encoder_family ?? cfg.export_encoder_family ?? 'auto',
      encoder:          exp.encoder ?? cfg.encoder ?? 'libx264',
      crf:              exp.crf ?? cfg.crf ?? 18,
      workers:          exp.workers ?? cfg.workers ?? 4,
      is_bike:          layout.is_bike           ?? false,
      show_map:         layout.show_map          ?? true,
      show_tel:         layout.show_tel          ?? true,
      export_path:      (exp.export_path || '').trim(),
      export_container_choice: exp.export_container_choice ?? cfg.export_container_choice ?? 'match_source',
      export_rate_mode: exp.export_rate_mode ?? cfg.export_rate_mode ?? 'cq',
      export_video_bitrate_kbps: exp.export_video_bitrate_kbps ?? cfg.export_video_bitrate_kbps ?? 0,
      export_video_max_bitrate_kbps: exp.export_video_max_bitrate_kbps ?? cfg.export_video_max_bitrate_kbps ?? 0,
      export_audio_bitrate_kbps: exp.export_audio_bitrate_kbps ?? cfg.export_audio_bitrate_kbps ?? 0,
      export_max_quality: exp.export_max_quality ?? cfg.export_max_quality ?? false,
      export_target_res: exp.export_target_res ?? cfg.export_target_res ?? 'auto',
      export_target_fps: exp.export_target_fps ?? cfg.export_target_fps ?? 'auto',
      export_process_priority: exp.export_process_priority ?? cfg.export_process_priority ?? 'normal',
      overlay_only:     $('exp-overlay-only')?.checked || false,
      layout,
    };

    // For lap_range scope, embed range on every item
    if (params.scope === 'lap_range') {
      params.items = params.items.map(item => ({
        ...item,
        scope:           'lap_range',
        lap_range_start: _rangeStart,
        lap_range_end:   _rangeEnd,
      }));
    }

    _logLines    = [];
    _progressPct = 0;
    _progressMsg = '';
    _lastFrameProg = null;
    _emaFps = null;
    $('exp-log').value = '';
    _setProgress(0, '准备开始…');
    _setExporting(true);
    _setBadge('进行中', 'badge-run');

    await API.startExport(params);
  }

  // ── Event handlers ────────────────────────────────────────────────────────────

  function _onProgress(detail) {
    // Python progress_cb sends 0–100 directly; clamp to avoid display glitches
    const pct = Math.min(100, Math.max(0, Math.round(detail.value || 0)));
    const rawMsg = detail.message || '';
    let msg = rawMsg;

    // Speed/ETA: parse backend's "Frame done/total" message.
    const m = /Frame\s+(\d+)\s*\/\s*(\d+)/i.exec(rawMsg);
    if (m) {
      const done  = Math.max(0, parseInt(m[1], 10) || 0);
      const total = Math.max(0, parseInt(m[2], 10) || 0);
      const now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
      if (_lastFrameProg && done >= _lastFrameProg.done) {
        const dt = Math.max(1, now - _lastFrameProg.t_ms) / 1000.0;
        const df = done - _lastFrameProg.done;
        const inst = (df > 0 && dt > 0) ? (df / dt) : 0;
        if (inst > 0 && Number.isFinite(inst)) {
          _emaFps = (_emaFps == null) ? inst : (_emaFps * 0.85 + inst * 0.15);
        }
      }
      _lastFrameProg = { done, total, t_ms: now };

      if (_emaFps && _emaFps > 0.5 && total > 0 && done <= total) {
        const remain = Math.max(0, total - done);
        const etaS = remain / _emaFps;
        msg = `${rawMsg}  ·  ${_emaFps.toFixed(1)} fps  ·  ETA ${_fmtEta(etaS)}`;
      }
    }

    _setProgress(pct, msg);
  }

  function _onLog(detail) {
    const msg = detail.message || '';
    // Always accumulate — even when the Export tab is not visible
    _logLines.push(msg);
    if (_logLines.length > 500) _logLines.shift();
    if (!_container) return;
    const ta = _container.querySelector('#exp-log');
    if (ta) {
      ta.value = _logLines.join('\n');
      ta.scrollTop = ta.scrollHeight;
    }
  }

  function _onDone(detail) {
    const ok  = detail.ok !== false;
  const msg = detail.message || (ok ? '导出完成。' : '导出失败。');
    _onLog({ message: msg });
    _setProgress(ok ? 100 : 0, msg);
    _setExporting(false);
    _setBadge(ok ? '完成' : '错误', ok ? 'badge-ok' : 'badge-err');
    // Export finished — offer auto-sync the chance to run now.
    // Python's start_auto_sync skips sessions already synced or failed, so
    // it is safe to call with all matched sessions (it filters internally).
    const sessions = (State.get('sessions') || []).filter(s => s.matched && s.video_paths?.length);
    if (sessions.length) API.startAutoSync(sessions).catch(() => {});
  }

  // ── UI state helpers ──────────────────────────────────────────────────────────

  function _setExporting(active) {
    _exporting = active;
    if (!_container) return;
    const start  = _container.querySelector('#exp-start-btn');
    const cancel = _container.querySelector('#exp-cancel-btn');
    const items  = State.get('selectedItems') || [];
    if (start) {
      start.disabled    = active || items.length === 0;
      start.textContent = active ? '导出中…' : '开始导出';
      start.classList.toggle('btn-exporting', active);
    }
    if (cancel) { cancel.classList.toggle('hidden', !active); }
  }

  function _setProgress(pct, msg) {
    _progressPct = pct;
    _progressMsg = msg;
    if (!_container) return;
    const fill = _container.querySelector('#exp-progress-fill');
    const pctEl = _container.querySelector('#exp-progress-pct');
    const msgEl = _container.querySelector('#exp-progress-msg');
    if (fill)  fill.style.width = pct + '%';
    if (pctEl) pctEl.textContent = Math.round(pct) + '%';
    if (msgEl) msgEl.textContent = msg;
  }

  function _setBadge(text, cls) {
    _badgeText = text;
    _badgeCls  = cls || 'badge-dim';
    if (!_container) return;
    const badge = _container.querySelector('#exp-status-badge');
    if (!badge) return;
    badge.textContent = _badgeText;
    badge.className   = 'badge ' + _badgeCls;
  }

  // ── Tiny utilities ────────────────────────────────────────────────────────────

  function _esc(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _baseName(p) {
    return (p || '').replace(/\\/g, '/').split('/').pop() || p;
  }

  function _fmtTime(secs) {
    if (secs == null || isNaN(secs)) return '—';
    const m  = Math.floor(secs / 60);
    const s  = secs % 60;
    const ss = s.toFixed(3).padStart(6, '0');
    return `${m}:${ss}`;
  }

  function _fmtEta(secs) {
    if (secs == null || !Number.isFinite(secs) || secs < 0) return '—';
    const s = Math.round(secs);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
    return `${m}:${String(r).padStart(2, '0')}`;
  }

  Router.register('export', { mount, unmount });

  // ── Persistent export event listeners ────────────────────────────────────────
  // Registered once when the module loads — NOT tied to the Export tab being
  // visible. This ensures progress, log lines, and done state are captured even
  // when the user is on the Data, Overlay, or Settings tab during an export.
  API.on('export_progress', _onProgress);
  API.on('export_log',      _onLog);
  API.on('export_done',     _onDone);
})();
