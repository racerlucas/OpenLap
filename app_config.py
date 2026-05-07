# app_config.py — persistent application configuration
# Default save location: <repo>/.openlap/config.json (dev) or next to OpenLap.exe (frozen).

from __future__ import annotations
import json
import logging
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from openlap_paths import config_file, scan_cache_file

logger = logging.getLogger(__name__)
_OLD_CONFIG_V2  = Path.home() / '.telemetry_overlay' / 'config.json'

# When overlay JSON omits ``ref_mode``, preview + export use best-so-far delta for the current lap.
DEFAULT_OVERLAY_REF_MODE = 'session_best_so_far'
_OLD_CONFIG_V1  = Path.home() / '.racebox_studio'    / 'config.json'


@dataclass
class OverlayElement:
    """One overlay element with normalized position/size (0..1 of video dimensions)."""
    visible: bool = True
    x: float = 0.0   # left edge as fraction of video width
    y: float = 0.0   # top edge as fraction of video height
    w: float = 0.25  # width as fraction of video width
    h: float = 0.25  # height as fraction of video height


def _default_gauges() -> List[dict]:
    """Return the built-in default gauge layout as plain dicts."""
    return [
        {'channel': 'map',        'style': 'Circuit', 'visible': True, 'x': 0.74, 'y': 0.02, 'w': 0.24, 'h': 0.30},
        {'channel': 'speed',      'style': 'Dial',    'visible': True, 'x': 0.01, 'y': 0.74, 'w': 0.13, 'h': 0.23},
        {'channel': 'gforce_lat', 'style': 'Bar',     'visible': True, 'x': 0.15, 'y': 0.74, 'w': 0.10, 'h': 0.23},
        {'channel': 'gforce_lon', 'style': 'Bar',     'visible': True, 'x': 0.26, 'y': 0.74, 'w': 0.10, 'h': 0.23},
        {'channel': 'lap_time',   'style': 'Numeric', 'visible': True, 'x': 0.37, 'y': 0.74, 'w': 0.13, 'h': 0.23},
    ]


@dataclass
class OverlayLayout:
    is_bike:          bool       = False
    theme:            str        = 'Dark'
    ref_mode:         str        = DEFAULT_OVERLAY_REF_MODE  # 'none' | 'session_best' | … | 'session_best_so_far' | 'manual'
    ref_lap_csv_path: str        = ''       # used when ref_mode='manual'
    ref_lap_num:      int        = 0        # used when ref_mode='manual'
    gauges:           List[dict] = field(default_factory=_default_gauges)


@dataclass
class AppConfig:
    # Per-source telemetry directories (preferred)
    racebox_path:   str = ""
    aim_path:       str = ""
    motec_path:     str = ""
    gpx_path:       str = ""
    vbox_path:      str = ""
    # Legacy single telemetry folder — kept as scan-all fallback for old configs
    telemetry_path: str = ""
    video_path:     str = ""
    export_path:    str = ""
    overlay:        OverlayLayout = field(default_factory=OverlayLayout)
    offsets:        Dict[str, float] = field(default_factory=dict)
    # key = absolute CSV path, value = float sync offset in seconds
    bike_overrides: Dict[str, bool]  = field(default_factory=dict)
    # key = absolute CSV path, value = True (bike) / False (car override)
    presets:        Dict[str, dict] = field(default_factory=dict)
    # name -> serialized OverlayLayout dict
    active_preset:  str = ""
    session_info:   Dict[str, dict] = field(default_factory=dict)
    # key = absolute CSV path, value = {track, vehicle, session_type} manual overrides
    racebox_email:  str = ""
    # Stored for convenience; password is never persisted
    encoder: str   = 'libx264'
    # UI: codec (H.264/HEVC/AV1) vs implementation (CPU / NVENC / …); ``encoder`` is the resolved FFmpeg name.
    export_video_codec: str = 'h264'       # h264 | h265 | av1
    export_encoder_family: str = 'auto'    # auto | cpu | nvenc | amf | qsv | videotoolbox
    crf:     int   = 18
    workers: int   = 4
    # Windows: whole-process priority during export (see ``utils.normalize_export_process_priority``).
    export_process_priority: str = 'normal'
    # Export encoding (UI + FFmpeg) — names mirror webview `export_*` keys
    export_rate_mode: str = 'cq'          # cq | vbr | cbr
    export_video_bitrate_kbps: int = 0    # 0 = auto (Shutter-style heuristic)
    export_video_max_bitrate_kbps: int = 0  # 0 = FFmpeg auto (= ~1.85× nominal)
    export_audio_bitrate_kbps: int = 0    # 0 = 跟随原片（估算用 ffprobe；重编码 fallback 由 mux 决定）
    export_two_pass: bool = False
    export_max_quality: bool = False      # FFmpeg preset slower / higher overhead
    export_container_choice: str = 'match_source'  # match_source | mp4 | mkv | mov …
    # Export resample controls (UI only for now; enforced during mux).
    export_target_res: str = 'auto'       # 'auto' | 'WIDTHxHEIGHT'
    export_target_fps: str = 'auto'       # 'auto' | '30' | '29.97' ...
    # Export page — timing / scope (persisted so next launch matches last session)
    export_scope: str = 'full'            # selected_lap | lap_range | fastest_lap | all_laps | full | clip
    export_padding: float = 5.0           # seconds before/after lap (UI 0–60)
    export_clip_start_s: float = 0.0
    export_clip_end_s: float = 0.0
    export_overlay_only: bool = False
    export_lap_range_start: int = 1
    export_lap_range_end: Optional[int] = None  # None = unset / full range to last lap
    offset_sources:    Dict[str, str]  = field(default_factory=dict)
    # 'user' = manually confirmed, 'auto' = auto-detected (unconfirmed)
    auto_sync_failed:  List[str]       = field(default_factory=list)
    # csv_paths where auto-sync was tried but confidence was too low
    auto_sync_enabled: bool            = False
    auto_sync_workers: int            = 2
    # how many sessions to auto-sync concurrently (1 = sequential)
    auto_sync_use_motion: bool        = False
    # False = file timestamps only (default; kart/helmet video). True = G-force vs motion refine.
    track_map_selections: Dict[str, str] = field(default_factory=dict)
    # track_name_lower → osm_way_id; controls which OSM way is used as circuit outline
    lap_flags: Dict[str, dict] = field(default_factory=dict)
    # key = absolute CSV path, value = {'outlap': [lap_num...], 'inlap': [lap_num...]}
    encoder_probe_cache: Dict[str, object] = field(default_factory=dict)
    # Cached FFmpeg encoder availability probe results for fast UI on startup.
    # Structure: { 'ts': ISO, 'ffmpeg_bin': str, 'ffmpeg_version': str, 'avail': {enc: bool...} }

    def all_telemetry_paths(self) -> List[str]:
        """Return all unique non-empty telemetry paths to scan.

        Uses case-insensitive, normalised path comparison so the same
        directory configured with different separators or casing only
        appears once.
        """
        import os as _os
        seen: set = set()
        result: List[str] = []
        for p in (self.racebox_path, self.aim_path, self.motec_path,
                  self.gpx_path, self.vbox_path, self.telemetry_path):
            p = p.strip()
            if not p:
                continue
            key = _os.path.normcase(_os.path.normpath(p))
            if key not in seen:
                seen.add(key)
                result.append(p)
        return result

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        cf = config_file()
        cf.parent.mkdir(parents=True, exist_ok=True)
        with open(cf, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, indent=2, default=str)

    def schedule_save(self, delay: float = 0.5) -> None:
        """Debounced save — flushes to disk at most once per *delay* seconds.

        Safe to call rapidly (e.g. on every gauge drag tick); the actual
        write is deferred until the flurry of calls stops.
        """
        import threading
        existing = getattr(self, '_save_timer', None)
        if existing is not None:
            existing.cancel()
        t = threading.Timer(delay, self.save)
        t.daemon = True
        t.start()
        object.__setattr__(self, '_save_timer', t)  # type: ignore[arg-type]

    @classmethod
    def load(cls) -> 'AppConfig':
        cf = config_file()
        # One-time migration from older config locations
        if not cf.exists():
            _src = next((p for p in (_OLD_CONFIG_V2, _OLD_CONFIG_V1) if p.exists()), None)
            if _src:
                try:
                    cf.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(_src, cf)
                except Exception:
                    logger.debug('Config migration from %s failed', _src, exc_info=True)
        try:
            with open(cf, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return _from_dict(data)
        except Exception:
            logger.warning('Failed to load config from %s, using defaults', cf, exc_info=True)
            return cls()


# ── Scan cache ────────────────────────────────────────────────────────────────

def save_scan_cache(tel_path: str, vid_path: str,
                    sessions: list, session_meta: dict) -> None:
    """Persist lightweight scan results so the tree can be populated immediately on next launch."""
    entries = []
    for m in sessions:
        meta = session_meta.get(m.csv_path, {})
        entries.append({
            'csv_path':        m.csv_path,
            'source':          m.source,
            'csv_start':       m.csv_start.isoformat() if m.csv_start else None,
            'matched':         m.matched,
            'video_paths':     m.video_group.paths if m.video_group else [],
            'video_total_dur': m.video_group.total_dur if m.video_group else 0.0,
            'needs_conversion': m.needs_conversion,
            'xrk_path':        m.xrk_path,
            'track':           meta.get('track', ''),
            'laps':            meta.get('laps', ''),
            'best':            meta.get('best', ''),
        })
    data = {'tel_path': tel_path, 'tel_paths': tel_path, 'vid_path': vid_path, 'sessions': entries}
    try:
        scf = scan_cache_file()
        scf.parent.mkdir(parents=True, exist_ok=True)
        with open(scf, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        logger.debug('Failed to write scan cache', exc_info=True)


def load_scan_cache() -> dict:
    """Return cached scan data, or {} on miss/error."""
    try:
        with open(scan_cache_file(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


# ── Reconstruction helpers ─────────────────────────────────────────────────────

def overlay_from_dict(overlay_data: dict) -> OverlayLayout:
    """Deserialize an OverlayLayout from a plain dict (e.g. from a preset or config)."""
    raw_gauges = list(overlay_data.get('gauges', []))

    # ── Migrate old configs that stored map as a separate field ───────────────
    old_map = overlay_data.get('map', {})
    old_map_style = overlay_data.get('map_style', 'Circuit')
    if old_map and not any(g.get('channel') == 'map' for g in raw_gauges):
        raw_gauges.insert(0, {
            'channel': 'map',
            'style':   old_map_style,
            'visible': old_map.get('visible', True),
            'x': old_map.get('x', 0.74),
            'y': old_map.get('y', 0.02),
            'w': old_map.get('w', 0.24),
            'h': old_map.get('h', 0.30),
        })

    if raw_gauges:
        gauges = []
        for g in raw_gauges:
            gd = dict(g)
            # Migrate old 'channels' field name to 'multi_channels'
            if 'channels' in gd and 'multi_channels' not in gd and gd.get('channel') == 'multi':
                gd['multi_channels'] = gd.pop('channels')
            gauges.append(gd)
    else:
        gauges = _default_gauges()

    return OverlayLayout(
        is_bike          = overlay_data.get('is_bike', False),
        theme            = overlay_data.get('theme',   'Dark'),
        ref_mode         = overlay_data.get('ref_mode', DEFAULT_OVERLAY_REF_MODE),
        ref_lap_csv_path = overlay_data.get('ref_lap_csv_path', ''),
        ref_lap_num      = int(overlay_data.get('ref_lap_num', 0) or 0),
        gauges           = gauges,
    )


def _from_dict(data: dict) -> AppConfig:
    presets       = data.get('presets', {})
    active_preset = data.get('active_preset', '')

    # Always reconstruct the overlay from the saved preset when one is active,
    # so unsaved in-session edits (add/remove gauge etc.) are discarded on restart.
    if active_preset and active_preset in presets:
        overlay = overlay_from_dict(presets[active_preset])
    else:
        overlay = overlay_from_dict(data.get('overlay', {}))

    try:
        _asw = int(data.get('auto_sync_workers', 2))
    except (TypeError, ValueError):
        _asw = 2
    auto_sync_workers = max(1, min(8, _asw))

    _enc_legacy = str(data.get('encoder', 'libx264') or 'libx264')
    _codec = str(data.get('export_video_codec') or '').strip().lower()
    _fam = str(data.get('export_encoder_family') or '').strip().lower()
    if not _codec or not _fam:
        try:
            from export_encoder import infer_codec_and_family_from_legacy_encoder
            ic, ifam = infer_codec_and_family_from_legacy_encoder(_enc_legacy)
            _codec = _codec or ic
            _fam = _fam or ifam
        except Exception:
            _codec = _codec or 'h264'
            _fam = _fam or 'auto'
    try:
        from export_encoder import resolve_export_encoder
        _enc_resolved = resolve_export_encoder(_codec, _fam)
    except Exception:
        _enc_resolved = _enc_legacy
    _codec = _codec if _codec in ('h264', 'h265', 'av1') else 'h264'
    _fam = _fam if _fam in ('auto', 'cpu', 'nvenc', 'amf', 'qsv', 'videotoolbox') else 'auto'
    try:
        _ab = int(data.get('export_audio_bitrate_kbps', 0) or 0)
    except (TypeError, ValueError):
        _ab = 0
    _ab = max(0, min(320, _ab))

    _scope = str(data.get('export_scope', 'full') or 'full').strip()
    _valid_scopes = (
        'selected_lap', 'lap_range', 'fastest_lap', 'all_laps', 'full', 'clip',
    )
    if _scope not in _valid_scopes:
        _scope = 'full'
    try:
        _pad = float(data.get('export_padding', 5.0))
    except (TypeError, ValueError):
        _pad = 5.0
    _pad = max(0.0, min(120.0, _pad))
    try:
        _c0 = float(data.get('export_clip_start_s', 0.0) or 0.0)
    except (TypeError, ValueError):
        _c0 = 0.0
    try:
        _c1 = float(data.get('export_clip_end_s', 0.0) or 0.0)
    except (TypeError, ValueError):
        _c1 = 0.0
    _c0 = max(0.0, _c0)
    _c1 = max(0.0, _c1)
    try:
        _lrs = int(data.get('export_lap_range_start', 1) or 1)
    except (TypeError, ValueError):
        _lrs = 1
    _lrs = max(1, _lrs)

    _lre_raw = data.get('export_lap_range_end', None)
    if _lre_raw is None or (isinstance(_lre_raw, str) and not str(_lre_raw).strip()):
        _lre: Optional[int] = None
    else:
        try:
            _lre = int(_lre_raw)
        except (TypeError, ValueError):
            _lre = None
        if _lre is not None and _lre < _lrs:
            _lre = None

    from utils import normalize_export_process_priority
    _export_prio = normalize_export_process_priority(data.get('export_process_priority', 'normal'))

    return AppConfig(
        racebox_path   = data.get('racebox_path',   ''),
        aim_path       = data.get('aim_path',       ''),
        motec_path     = data.get('motec_path',     ''),
        gpx_path       = data.get('gpx_path',       ''),
        vbox_path      = data.get('vbox_path',      ''),
        telemetry_path = data.get('telemetry_path', ''),
        video_path     = data.get('video_path',     ''),
        export_path    = data.get('export_path',    ''),
        overlay        = overlay,
        offsets        = data.get('offsets',        {}),
        bike_overrides = data.get('bike_overrides', {}),
        presets        = presets,
        active_preset  = active_preset,
        session_info   = data.get('session_info',   {}),
        racebox_email  = data.get('racebox_email',  ''),
        encoder           = _enc_resolved,
        export_video_codec = _codec,
        export_encoder_family = _fam,
        crf               = int(data.get('crf',           18)),
        workers           = int(data.get('workers',       4)),
        export_process_priority = _export_prio,
        export_rate_mode  = data.get('export_rate_mode', 'cq'),
        export_video_bitrate_kbps    = max(0, int(data.get('export_video_bitrate_kbps') or 0)),
        export_video_max_bitrate_kbps = max(0, int(data.get('export_video_max_bitrate_kbps') or 0)),
        export_audio_bitrate_kbps = _ab,
        export_two_pass   = bool(data.get('export_two_pass', False)),
        export_max_quality = bool(data.get('export_max_quality', False)),
        export_container_choice = (data.get('export_container_choice') or 'match_source').strip(),
        export_target_res = str(data.get('export_target_res') or 'auto'),
        export_target_fps = str(data.get('export_target_fps') or 'auto'),
        export_scope         = _scope,
        export_padding       = _pad,
        export_clip_start_s  = _c0,
        export_clip_end_s    = _c1,
        export_overlay_only  = bool(data.get('export_overlay_only', False)),
        export_lap_range_start = _lrs,
        export_lap_range_end   = _lre,
        offset_sources       = data.get('offset_sources',       {}),
        auto_sync_failed     = data.get('auto_sync_failed',     []),
        auto_sync_enabled    = bool(data.get('auto_sync_enabled', False)),
        auto_sync_workers    = auto_sync_workers,
        auto_sync_use_motion = bool(data.get('auto_sync_use_motion', False)),
        track_map_selections = data.get('track_map_selections', {}),
        lap_flags            = data.get('lap_flags', {}),
        encoder_probe_cache  = data.get('encoder_probe_cache', {}) if isinstance(data.get('encoder_probe_cache', {}), dict) else {},
    )
