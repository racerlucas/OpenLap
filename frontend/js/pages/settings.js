/**
 * settings.js — Settings page (Phase 4).
 *
 * Sections:
 *   1. Telemetry & video folders (per-source)
 *   2. RaceBox cloud login
 *   3. Encoder / ffmpeg detection
 *   4. About
 */
(function () {
  let _config     = null;
  let _unlistenFns = [];

  // ── Mount / Unmount ──────────────────────────────────────────────────────────

  async function mount(container) {
    _config = await API.getConfig();
    container.innerHTML = _buildHTML(_config);
    _bindEvents(container);
  }

  function unmount() {
    _unlistenFns.forEach(fn => fn());
    _unlistenFns = [];
  }

  // ── HTML ─────────────────────────────────────────────────────────────────────

  function _buildHTML(cfg) {
    return `
<div class="page settings-page">
  <div class="toolbar">
    <div class="toolbar-left">
      <span class="page-title">设置</span>
    </div>
    <div class="toolbar-right">
      <span id="save-msg" class="save-msg"></span>
      <button class="btn btn-accent" id="save-btn">保存</button>
    </div>
  </div>
  <div class="page-divider"></div>

  <div class="settings-body">

    <!-- Telemetry folders -->
    <section class="settings-section">
      <div class="section-title">遥测目录</div>
      ${_folderRow('RaceBox',     'racebox_path', cfg.racebox_path, 'RaceBox Mini CSV 导出目录')}
      ${_folderRow('AIM Mychron','aim_path',      cfg.aim_path,     'AIM XRK / CSV 文件目录')}
      ${_folderRow('MoTeC',       'motec_path',   cfg.motec_path,   'MoTeC .ld 二进制文件目录')}
      ${_folderRow('GPX',         'gpx_path',     cfg.gpx_path,     '.gpx 轨迹文件目录')}
      ${_folderRow('VBOX',        'vbox_path',    cfg.vbox_path,    'Racelogic VBOX .vbo 文件目录')}
    </section>

    <!-- Video & output -->
    <section class="settings-section">
      <div class="section-title">视频与输出</div>
      ${_folderRow('视频源目录', 'video_path',  cfg.video_path,  '行车记录仪 / 车载视频')}
      ${_folderRow('导出目录','export_path', cfg.export_path, '导出视频保存位置')}
    </section>

    <!-- RaceBox cloud -->
    <section class="settings-section">
      <div class="section-title">RaceBox 云端</div>
      <p class="section-hint">可直接从 racebox.pro 下载节。首次使用会弹出登录窗口，授权信息会被保存。</p>
      <div id="rb-setup-wrap">
        <div id="rb-pw-status" class="form-row" style="font-size:10px;color:var(--text3)">检测中…</div>
        <div class="form-row" id="rb-install-row">
          <button class="btn btn-secondary" id="rb-install-btn">下载登录组件</button>
          <span id="rb-install-msg" class="status-msg"></span>
        </div>
        <div id="rb-setup-log-wrap" class="hidden" style="margin-top:6px;">
          <textarea id="rb-setup-log" class="log-area" readonly
                    style="height:80px;font-size:10px;"
                    placeholder="安装日志…"></textarea>
        </div>
      </div>
      <div id="rb-ready-wrap" class="hidden">
        <div class="form-row">
          <button class="btn btn-secondary" id="rb-login-btn">检查授权</button>
          <button class="btn btn-accent btn-sm" id="rb-download-btn">⬇ 下载新节</button>
          <button class="btn btn-secondary btn-sm hidden" id="rb-cancel-btn">取消</button>
          <span id="rb-login-msg" class="status-msg"></span>
        </div>
      </div>
      <div id="rb-log-wrap" class="hidden" style="margin-top:8px;">
        <div class="progress-bar-wrap" style="margin-bottom:6px;">
          <div class="progress-bar-track">
            <div class="progress-bar-fill" id="rb-progress-fill" style="width:0%"></div>
          </div>
          <span class="progress-pct" id="rb-progress-pct">0%</span>
        </div>
        <textarea id="rb-log" class="log-area" readonly
                  style="height:120px;font-size:10px;"
                  placeholder="下载日志会显示在这里…"></textarea>
      </div>
    </section>

    <!-- AIM Mychron DLL -->
    <section class="settings-section">
      <div class="section-title">AIM Mychron</div>
      <p class="section-hint">AIM .xrk / .xrz / .drk files are converted to CSV automatically on scan. This requires the MatLabXRK DLL provided free by AIM.</p>
      <div id="aim-dll-status" class="form-row" style="font-size:10px;color:var(--text3)">Checking…</div>
      <div class="form-row" style="margin-top:6px">
        <button class="btn btn-secondary" id="aim-dll-btn">Download DLL</button>
        <span id="aim-dll-msg" class="status-msg"></span>
      </div>
    </section>

    <!-- Encoder -->
    <section class="settings-section">
      <div class="section-title">编码器</div>
      <p class="section-hint">OpenLap 使用 FFmpeg 处理视频，这些设置会作用于每次导出。</p>
      <div class="form-row">
        <label>编码格式</label>
        <select data-config-key="encoder" class="input-field">
          <option value="libx264" ${cfg.encoder === 'libx264' || !cfg.encoder ? 'selected' : ''}>H.264 (libx264) — 通用</option>
          <option value="libx265" ${cfg.encoder === 'libx265' ? 'selected' : ''}>H.265 (libx265) — 文件更小</option>
          <option value="h264_nvenc" ${cfg.encoder === 'h264_nvenc' ? 'selected' : ''}>H.264 NVENC — NVIDIA 显卡</option>
          <option value="h264_videotoolbox" ${cfg.encoder === 'h264_videotoolbox' ? 'selected' : ''}>H.264 VideoToolbox — Apple</option>
        </select>
      </div>
      <div class="form-row">
        <label>质量（CRF）</label>
        <div class="range-row">
          <input type="range" id="enc-crf" data-config-key="crf"
                 min="12" max="32" step="1" value="${cfg.crf ?? 18}">
          <span class="range-val" id="enc-crf-val">${cfg.crf ?? 18}</span>
        </div>
      </div>
      <div class="form-row">
        <label>并发进程</label>
        <input type="number" data-config-key="workers" class="input-field input-narrow"
               value="${cfg.workers ?? 4}" min="1" max="16" step="1">
      </div>
      <div class="form-row" style="margin-top:8px">
        <button class="btn btn-secondary" id="enc-check-btn">检测编码器</button>
        <span id="enc-msg" class="status-msg"></span>
      </div>
      <div id="enc-results" class="enc-results hidden"></div>
    </section>

    <!-- Auto Sync -->
    <section class="settings-section">
      <div class="section-title">自动同步</div>
      <p class="section-hint">默认用<strong>文件时间码</strong>（遥测开始时间 vs 视频创建/流时间）估算偏移并吸附到视频帧，不做画面运动分析；卡丁头盔、防抖等场景更稳。无时间码时不会写偏移，请在数据页手动对齐。
        可选开启「运动精细对齐」用 G 力与画面运动互相关（固定车载机位更合适，每节约数十秒解码）。仅对尚无偏移的节运行。</p>
      <div class="form-row">
        <label>扫描后启用自动同步</label>
        <label class="toggle-switch">
          <input type="checkbox" data-config-key="auto_sync_enabled"
                 ${cfg.auto_sync_enabled ? 'checked' : ''}>
          <span class="toggle-thumb"></span>
        </label>
      </div>
      <div class="form-row">
        <label title="同时分析多节；调高会更快但更吃 CPU/磁盘。导出运行时仍会暂停自动同步。">自动同步并行数</label>
        <input type="number" data-config-key="auto_sync_workers" class="input-field input-narrow"
               value="${cfg.auto_sync_workers ?? 2}" min="1" max="8" step="1">
      </div>
      <div class="form-row">
        <label title="解码视频并与 G 力曲线互相关；头盔/防抖画面易失败，默认关闭">启用运动精细对齐（车载固定机位）</label>
        <label class="toggle-switch">
          <input type="checkbox" data-config-key="auto_sync_use_motion"
                 ${cfg.auto_sync_use_motion ? 'checked' : ''}>
          <span class="toggle-thumb"></span>
        </label>
      </div>
    </section>

    <!-- About -->
    <section class="settings-section">
      <div class="section-title">关于</div>
      <div class="about-row"><span class="about-key">版本</span><span class="about-val" id="about-version">—</span></div>
      <div class="about-row"><span class="about-key">Python</span><span class="about-val" id="about-python">—</span></div>
      <div class="about-row"><span class="about-key">配置文件</span>
        <span class="about-val" id="about-config" style="font-family:var(--mono); font-size:10px">—</span>
      </div>
    </section>

  </div><!-- /.settings-body -->
</div>`;
  }

  function _folderRow(label, key, value, hint) {
    return `
      <div class="sett-path-group">
        <div class="form-row">
          <label>${_esc(label)}</label>
          <div class="path-row">
            <input type="text" data-config-key="${key}" class="input-field"
                   value="${_esc(value || '')}" placeholder="未配置"
                   style="font-family:var(--mono); font-size:10px;">
            <button class="btn btn-secondary btn-sm" data-browse-key="${key}">浏览…</button>
          </div>
        </div>
        ${hint ? `<div class="path-hint">${_esc(hint)}</div>` : ''}
      </div>`;
  }

  // ── Event wiring ──────────────────────────────────────────────────────────────

  function _bindEvents(container) {
    const $ = id => container.querySelector('#' + id);

    // Save button
    $('save-btn').addEventListener('click', () => _save(container));

    // Browse buttons
    container.querySelectorAll('[data-browse-key]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const path = await API.openFolderDialog();
        if (path) {
          const input = container.querySelector(`[data-config-key="${btn.dataset.browseKey}"]`);
          if (input) input.value = path;
        }
      });
    });

    // RaceBox playwright/chromium status
    function _refreshRbStatus() {
      API.raceboxPlaywrightStatus().then(s => {
        const statusEl   = $('rb-pw-status');
        const installRow = $('rb-install-row');
        const setupWrap  = $('rb-setup-wrap');
        const readyWrap  = $('rb-ready-wrap');
        if (!s || !s.playwright) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--err)">● 当前构建不支持浏览器引擎。</span>';
          if (installRow) installRow.classList.add('hidden');
          return;
        }
        if (!s.chromium) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--warn)">○ 登录组件未安装，需要一次性下载约 130MB 的 Chromium（无界面，仅用于登录 racebox.pro）。</span>';
          if (installRow) installRow.classList.remove('hidden');
          if (setupWrap)  setupWrap.classList.remove('hidden');
          if (readyWrap)  readyWrap.classList.add('hidden');
        } else {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--ok)">● 登录组件已就绪。</span>';
          if (installRow) installRow.classList.add('hidden');
          if (setupWrap)  setupWrap.classList.remove('hidden');
          if (readyWrap)  readyWrap.classList.remove('hidden');
        }
      }).catch(() => {});
    }
    _refreshRbStatus();

    _unlistenFns.push(API.on('racebox_setup_log', detail => {
      const wrap = $('rb-setup-log-wrap');
      const ta   = $('rb-setup-log');
      if (wrap) wrap.classList.remove('hidden');
      if (ta)   { ta.value += (detail.message || '') + '\n'; ta.scrollTop = ta.scrollHeight; }
    }));
    _unlistenFns.push(API.on('racebox_setup_done', detail => {
      const ok = detail.ok !== false;
      _setMsg($('rb-install-msg'), detail.message || '', ok ? 'ok' : 'err');
      const btn = $('rb-install-btn');
      if (btn) btn.disabled = false;
      if (ok) _refreshRbStatus();
    }));

    $('rb-install-btn').addEventListener('click', () => {
      const btn = $('rb-install-btn');
      if (btn) btn.disabled = true;
      const ta = $('rb-setup-log');
      if (ta) ta.value = '';
      const logWrap = $('rb-setup-log-wrap');
      if (logWrap) logWrap.classList.remove('hidden');
      _setMsg($('rb-install-msg'), '', 'dim');
      API.installPlaywrightChromium();
    });

    // RaceBox auth check
    $('rb-login-btn').addEventListener('click', async () => {
      const btn   = $('rb-login-btn');
      const msgEl = $('rb-login-msg');
      btn.textContent       = '检查中…';
      btn.disabled          = true;
      btn.style.color       = '';
      btn.style.borderColor = '';
      _setMsg(msgEl, '', 'dim');
      try {
        const result = await API.raceboxLogin('', '');
        if (result?.ok) {
          btn.textContent       = '✓ 授权通过';
          btn.style.color       = 'var(--ok)';
          btn.style.borderColor = 'var(--ok)';
        } else {
          btn.textContent = '检查授权';
          const notAvail = result?.error?.includes('Playwright') || result?.error?.includes('not available');
          _setMsg(msgEl, result?.error || '未授权。', notAvail ? 'dim' : 'warn');
        }
      } catch (e) {
        btn.textContent = '检查授权';
        _setMsg(msgEl, String(e), 'err');
      } finally {
        btn.disabled = false;
      }
    });

    // RaceBox download — subscribes to push events for live progress
    let _rbLogLines = [];

    function _rbSetProgress(pct, msg) {
      const fill  = $('rb-progress-fill');
      const pctEl = $('rb-progress-pct');
      if (fill)  fill.style.width   = Math.min(100, pct) + '%';
      if (pctEl) pctEl.textContent  = Math.min(100, Math.round(pct)) + '%';
    }

    function _rbAppendLog(msg) {
      _rbLogLines.push(msg);
      if (_rbLogLines.length > 200) _rbLogLines.shift();
      const ta = $('rb-log');
      if (ta) { ta.value = _rbLogLines.join('\n'); ta.scrollTop = ta.scrollHeight; }
    }

    function _rbSetDownloading(active) {
      const btn    = $('rb-download-btn');
      const cancel = $('rb-cancel-btn');
      if (btn)    btn.classList.toggle('hidden', active);
      if (cancel) cancel.classList.toggle('hidden', !active);
    }

    _unlistenFns.push(API.on('racebox_log', detail => {
      _rbAppendLog(detail.message || '');
    }));

    _unlistenFns.push(API.on('racebox_progress', detail => {
      _rbSetProgress(detail.value || 0, detail.message || '');
    }));

    _unlistenFns.push(API.on('racebox_done', detail => {
      _rbSetDownloading(false);
      const msgEl = $('rb-login-msg');
      const ok    = detail.ok !== false;
      const msg   = detail.message || (ok ? '完成。' : '失败。');
      _setMsg(msgEl, msg, ok ? 'ok' : 'err');
      _rbAppendLog('── ' + msg);
      _rbSetProgress(ok ? 100 : 0);
    }));

    $('rb-download-btn').addEventListener('click', async () => {
      _rbLogLines = [];
      const ta = $('rb-log');
      if (ta) ta.value = '';
      _rbSetProgress(0);
      $('rb-log-wrap').classList.remove('hidden');
      _rbSetDownloading(true);
      _setMsg($('rb-login-msg'), '', 'dim');
      _rbAppendLog('开始下载…（首次登录可能会打开浏览器窗口）');
      await API.downloadRaceboxSessions();
    });

    $('rb-cancel-btn').addEventListener('click', async () => {
      await API.cancelRaceboxDownload();
      _rbSetDownloading(false);
      _rbAppendLog('正在取消…');
    });

    // CRF slider label sync
    $('enc-crf').addEventListener('input', e => {
      $('enc-crf-val').textContent = e.target.value;
    });

    // Encoder detection
    $('enc-check-btn').addEventListener('click', async () => {
      const msgEl     = $('enc-msg');
      const resultsEl = $('enc-results');
      _setMsg(msgEl, '检测中…', 'dim');
      $('enc-check-btn').disabled = true;
      resultsEl.classList.add('hidden');

      try {
        const result = await API.checkEncoders();
        if (!result) {
          _setMsg(msgEl, '未找到 FFmpeg。', 'err');
          return;
        }
        if (result.error) {
          _setMsg(msgEl, result.error, 'err');
          return;
        }

        _setMsg(msgEl, `FFmpeg ${result.version || '已检测到'}。`, 'ok');
        const encoders = result.encoders || [];
        resultsEl.innerHTML = encoders.map(e =>
          `<div class="enc-row">
             <span class="enc-name">${_esc(e.name)}</span>
             <span class="enc-label">${_esc(e.label)}</span>
             <span class="badge ${e.available ? 'badge-ok' : 'badge-muted'}">${e.available ? '可用' : '不可用'}</span>
           </div>`
        ).join('');
        resultsEl.classList.remove('hidden');
      } catch (err) {
        _setMsg(msgEl, String(err), 'err');
      } finally {
        $('enc-check-btn').disabled = false;
      }
    });

    // AIM DLL status + download
    function _refreshAimStatus() {
      API.aimDllStatus().then(r => {
        const el = $('aim-dll-status');
        if (!el) return;
        if (r && r.found) {
          el.innerHTML = '<span style="color:var(--ok)">● 已找到 MatLabXRK DLL，可使用 AIM XRK 转换。</span>';
        } else {
          el.innerHTML = '<span style="color:var(--text3)">○ 未找到 MatLabXRK DLL，AIM XRK 转换不可用。</span>';
        }
      }).catch(() => {});
    }
    _refreshAimStatus();

    _unlistenFns.push(API.on('aim_dll_progress', detail => {
      _setMsg($('aim-dll-msg'), detail.message || '', 'dim');
    }));
    _unlistenFns.push(API.on('aim_dll_done', detail => {
      const ok = detail.ok !== false;
      _setMsg($('aim-dll-msg'), detail.message || '', ok ? 'ok' : 'err');
      $('aim-dll-btn').disabled = false;
      if (ok) _refreshAimStatus();
    }));

    $('aim-dll-btn').addEventListener('click', () => {
      $('aim-dll-btn').disabled = true;
      _setMsg($('aim-dll-msg'), '下载中…', 'dim');
      API.downloadAimDll();
    });

    // Populate about section
    API.getAboutInfo().then(info => {
      if (!info) return;
      const verEl = $('about-version');
      const pyEl  = $('about-python');
      const cfgEl = $('about-config');
      if (verEl && info.version) verEl.textContent = info.version;
      if (pyEl  && info.python)  pyEl.textContent  = info.python;
      if (cfgEl && info.config)  cfgEl.textContent = info.config;
    }).catch(() => {});
  }

  // ── Helpers ───────────────────────────────────────────────────────────────────

  async function _save(container) {
    const updated = { ..._config };
    const _intKeys  = new Set(['crf', 'workers', 'auto_sync_workers']);
    const _boolKeys = new Set(['auto_sync_enabled']);
    container.querySelectorAll('[data-config-key]').forEach(el => {
      const key = el.dataset.configKey;
      if (_boolKeys.has(key) || el.type === 'checkbox') {
        updated[key] = el.checked;
      } else if (_intKeys.has(key)) {
        let n = parseInt(el.value, 10);
        if (!Number.isFinite(n)) n = key === 'auto_sync_workers' ? 2 : 0;
        if (key === 'auto_sync_workers') n = Math.min(8, Math.max(1, n));
        updated[key] = n;
      } else {
        updated[key] = el.value.trim();
      }
    });
    // Persist email (never password — that goes through the login flow only)
    const emailEl = container.querySelector('#rb-email');
    if (emailEl) updated.racebox_email = emailEl.value.trim();

    await API.saveConfig(updated);
    _config = updated;

    const msg = container.querySelector('#save-msg');
    if (msg) {
      _setMsg(msg, '已保存。', 'ok');
      setTimeout(() => { if (msg) msg.textContent = ''; }, 2000);
    }
  }

  function _setMsg(el, text, variant) {
    if (!el) return;
    el.textContent = text;
    el.className   = 'status-msg status-' + variant;
  }

  function _esc(s) {
    return String(s || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  Router.register('settings', { mount, unmount });
})();
