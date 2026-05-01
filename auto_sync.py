"""
auto_sync.py — Automatic video-telemetry sync offset detection.

Default (``use_motion=False``): **metadata / timecode only**
    session_start_utc − video_creation_time_utc → ``sync_offset`` baseline, snapped
    to the video frame grid. No ffmpeg decode, no motion vs G-force correlation.
    Suited to helmet/handheld kart footage (OIS, vibration) where motion matching
    is unreliable; users refine with Mark / scrub.

Optional (``use_motion=True``): **metadata-first, then motion refine**
1) Same metadata baseline as above.
2) Refine with motion vs telemetry on a window anchored at the first timed lap.
3) If correlation confidence stays low, still return the snapped metadata baseline
   as a provisional offset.

sync_offset convention (matches OpenLap's manual Mark offset):
    session_time = video_time - sync_offset
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
from scipy import signal as sp_signal

from utils import _popen, _run

logger = logging.getLogger(__name__)

FPS                  = 5.0
CONFIDENCE_THRESHOLD = 6.0
MIN_CONFIDENCE       = 3.0
SEARCH_WINDOW_S      = 120.0
METADATA_FINE_WINDOW_S = 10.0
RESIZE_W             = 320
CHECK_EVERY_S        = 20.0
# UI / push throttling: correlate() stays on CHECK_EVERY_S, but we still report how
# much video time has been decoded so the frontend is not stuck at "0s" for ~20s.
DECODE_PROGRESS_INTERVAL_S = 1.0


def _snap_offset_to_frame(offset_s: float, video_fps: float) -> float:
    """Quantize offset to the nearest video frame boundary (1/fps)."""
    fps = float(video_fps or 0.0)
    if fps <= 0:
        return float(offset_s)
    return float(round(float(offset_s) * fps) / fps)

def _clip(v: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, v)))


def _slice_signal(sig: np.ndarray, fps: float, t0: float, t1: float) -> tuple[np.ndarray, float]:
    """Slice a 1D signal by time [t0, t1] seconds. Returns (slice, actual_t0)."""
    f = float(fps or 0.0)
    if f <= 0 or sig is None or len(sig) == 0:
        return sig, 0.0
    n = int(len(sig))
    i0 = int(_clip(round(t0 * f), 0, n - 1))
    i1 = int(_clip(round(t1 * f), 0, n))
    if i1 <= i0:
        i1 = min(n, i0 + 1)
    return sig[i0:i1], float(i0 / f)


def _decode_window_for_baseline(
    *,
    center_offset_s: float,
    window_s: float,
    margin_s: float,
    file_duration_s: float,
) -> tuple[float, float]:
    """Compute (start_s, dur_s) for ffmpeg -ss/-t when using a baseline."""
    dur_total = float(file_duration_s or 0.0)
    if dur_total <= 0:
        return 0.0, 0.0
    start = max(0.0, float(center_offset_s) - float(window_s) - float(margin_s))
    end = float(center_offset_s) + float(window_s) + float(margin_s)
    # If center is before the start of the file, decode the head window.
    if end <= 0.0:
        start = 0.0
        end = min(dur_total, 2.0 * float(window_s) + 2.0 * float(margin_s))
    # If center is beyond file duration, just decode the tail.
    if start >= dur_total:
        start = max(0.0, dur_total - (2.0 * float(window_s) + 2.0 * float(margin_s)))
    end = min(dur_total, max(start, end))
    # Avoid a 0-length window (ffmpeg would output 0 frames).
    if end <= start:
        end = min(dur_total, start + (2.0 * float(window_s) + 2.0 * float(margin_s)))
    dur = max(0.0, end - start)
    return float(start), float(dur)


# ── Telemetry loading ─────────────────────────────────────────────────────────

def _load_session(csv_path: str, source: str):
    if source == 'RaceBox':
        from racebox_data import load_csv
        return load_csv(csv_path)
    if source == 'AIM':
        from aim_data import load_csv
        return load_csv(csv_path)
    if source == 'VBOX':
        # VBOX .vbo (Circuit Tools / VBox) session format
        from vbox_data import load_vbo
        return load_vbo(csv_path)
    if source == 'GPX':
        from gpx_data import load_gpx
        return load_gpx(csv_path)
    if source == 'MoTeC':
        from motec_data import load_ld
        return load_ld(csv_path)
    raise ValueError(f'Unknown telemetry source: {source!r}')


def _load_telemetry_session_grid(
    csv_path: str, source: str, fps: float,
) -> Tuple[np.ndarray, np.ndarray, object]:
    """Return (G-magnitude resampled to *fps*, session_elapsed_seconds[], Session).

    ``session_t[i]`` is the session elapsed time (s) for telemetry sample *i*.
    """
    session = _load_session(csv_path, source)
    pts = session.all_points
    t = np.array([p.elapsed for p in pts], dtype=np.float64)
    gx = np.array([p.gforce_x for p in pts], dtype=np.float64)
    gy = np.array([p.gforce_y for p in pts], dtype=np.float64)
    gmag = np.sqrt(gx**2 + gy**2)
    if gmag.max() < 0.05:
        speed_ms = np.array([p.speed for p in pts]) / 3.6
        gmag = np.abs(np.gradient(speed_ms, t)) / 9.81
    out_t = np.arange(t[0], t[-1], 1.0 / fps)
    return np.interp(out_t, t, gmag).astype(np.float64), out_t.astype(np.float64), session


def _load_telemetry(csv_path: str, source: str, fps: float) -> np.ndarray:
    """Return G-magnitude signal resampled to fps (no session time axis)."""
    g, _, _ = _load_telemetry_session_grid(csv_path, source, fps)
    return g


def _first_timed_lap_session_interval(session) -> Optional[Tuple[float, float]]:
    """Return (elapsed_lo, elapsed_hi) for the first non-out/in lap, or None."""
    laps = getattr(session, 'laps', None) or []
    for lap in laps:
        if getattr(lap, 'is_outlap', False) or getattr(lap, 'is_inlap', False):
            continue
        pts = getattr(lap, 'points', None) or []
        if len(pts) < 2:
            continue
        L0 = float(pts[0].elapsed)
        L1 = float(pts[-1].elapsed)
        dur = float(getattr(lap, 'duration', 0.0) or 0.0) or (L1 - L0)
        L1 = max(L1, L0 + max(8.0, dur))
        return L0, L1
    return None


def _slice_telemetry_session(
    tel: np.ndarray,
    session_t: np.ndarray,
    s_lo: float,
    s_hi: float,
    *,
    min_samples: int,
) -> Tuple[np.ndarray, float]:
    """Slice telemetry to session elapsed ``[s_lo, s_hi]``; returns (sig, t0_session_first)."""
    if tel.size == 0 or session_t.size != tel.size:
        return tel, 0.0
    s_lo, s_hi = float(min(s_lo, s_hi)), float(max(s_lo, s_hi))
    i0 = int(np.searchsorted(session_t, s_lo, side='left'))
    i1 = int(np.searchsorted(session_t, s_hi, side='right'))
    i0 = max(0, min(i0, tel.size - 1))
    i1 = max(i0 + 1, min(i1, tel.size))
    if i1 - i0 < int(min_samples):
        pad = (int(min_samples) - (i1 - i0) + 1) // 2
        i0 = max(0, i0 - pad)
        i1 = min(tel.size, i1 + pad)
    return tel[i0:i1].copy(), float(session_t[i0])


# ── Video probing ─────────────────────────────────────────────────────────────

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def _read_stderr_tail(proc: subprocess.Popen, max_bytes: int = 16_000) -> str:
    try:
        if not proc.stderr:
            return ''
        data = proc.stderr.read(max_bytes) or b''
        if isinstance(data, str):
            return data[-max_bytes:]
        return data.decode('utf-8', errors='replace')[-max_bytes:]
    except Exception:
        return ''


def _probe_video(vpath: str) -> dict:
    result = _run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_streams', '-select_streams', 'v:0', vpath],
        capture_output=True, text=True, check=True,
        creationflags=_NO_WINDOW,
    )
    stream = json.loads(result.stdout)['streams'][0]
    num, den = map(int, stream['r_frame_rate'].split('/'))
    fps = num / den
    duration = float(stream.get('duration') or 0)
    nb_frames = int(stream.get('nb_frames') or 0)
    if duration == 0:
        duration = (nb_frames / fps) if (fps > 0 and nb_frames > 0) else 0.0
    return {
        'fps': fps,
        'width':  int(stream['width']),
        'height': int(stream['height']),
        'duration': duration,
        'nb_frames': nb_frames,
        'avg_frame_rate': stream.get('avg_frame_rate') or stream.get('r_frame_rate'),
    }


def _probe_video_creation_time_utc(vpath: str) -> Optional[datetime]:
    """Best-effort absolute start time for the video (UTC).

    ``creation_time`` supplies the calendar anchor; when any embedded SMPTE timecode
    parses, we **prefer that time-of-day** (frame-accurate via fps) over the raw
    creation timestamp — creation is often mux/rounding skew. ``creation_time`` still
    picks among ±1 calendar day when the timecode wraps midnight relative to the anchor.
    """
    def _parse(raw: str) -> Optional[datetime]:
        txt = (raw or '').strip()
        if not txt:
            return None
        txt = txt.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(txt)
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _parse_timecode(tc: str) -> Optional[tuple[int, int, int, int]]:
        """
        Parse SMPTE timecode 'HH:MM:SS:FF' or 'HH:MM:SS;FF' (drop-frame separator)
        -> (h,m,s,frames). Does not attempt drop-frame arithmetic; best-effort only.
        """
        if not tc:
            return None
        txt = str(tc).strip()
        m = re.match(r'^(\d{1,2}):(\d{1,2}):(\d{1,2})[:;](\d{1,2})$', txt)
        if not m:
            return None
        try:
            h, mm, s, f = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
            return h, mm, s, f
        except Exception:
            return None

    def _collect_timecode_tokens(data: dict) -> list[str]:
        out: list[str] = []
        fmt_tags = (data.get('format', {}) or {}).get('tags', {}) or {}
        for k in ('timecode', 'com.apple.quicktime.timecode'):
            v = fmt_tags.get(k)
            if v:
                out.append(str(v))
        for st in (data.get('streams', []) or []):
            tags = (st.get('tags', {}) or {}) if isinstance(st, dict) else {}
            for k in ('timecode', 'com.apple.quicktime.timecode'):
                v = tags.get(k)
                if v:
                    out.append(str(v))
        # de-dupe while preserving order
        seen = set()
        uniq: list[str] = []
        for t in out:
            tt = t.strip()
            if tt and tt not in seen:
                seen.add(tt)
                uniq.append(tt)
        return uniq

    def _apply_timecode_to_anchor(anchor: datetime, tc_raw: str, fps: float) -> Optional[datetime]:
        tc = _parse_timecode(tc_raw)
        if not tc:
            return None
        th, tm, ts, tf = tc
        if fps <= 0:
            return None
        sub = float(tf) / float(fps)
        # Anchor calendar in UTC; apply SMPTE time-of-day from timecode track.
        base = anchor.astimezone(timezone.utc).replace(
            hour=th, minute=tm, second=ts, microsecond=int(round(sub * 1_000_000))
        )
        # Choose the calendar day (±1) closest to the original anchor (handles midnight edges).
        best = min(
            (base - timedelta(days=1), base, base + timedelta(days=1)),
            key=lambda dt: abs((dt - anchor).total_seconds()),
        )
        return best

    try:
        # 1) creation_time anchor (often second-resolution)
        result = _run(
            [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_entries',
                'format_tags=creation_time,com.apple.quicktime.creationdate:'
                'stream_tags=creation_time,com.apple.quicktime.creationdate',
                '-select_streams', 'v:0',
                vpath,
            ],
            capture_output=True, text=True, check=True,
            creationflags=_NO_WINDOW,
        )
        data0 = json.loads(result.stdout or '{}')
        fmt0 = (data0.get('format', {}) or {}).get('tags', {}) or {}
        streams0 = data0.get('streams', []) or []
        st0 = (streams0[0].get('tags', {}) if streams0 else {}) or {}
        raw = (
            fmt0.get('creation_time')
            or fmt0.get('com.apple.quicktime.creationdate')
            or st0.get('creation_time')
            or st0.get('com.apple.quicktime.creationdate')
        )
        anchor = _parse(raw)
        if not anchor:
            return None

        # 2) SMPTE timecode tokens may live on non-video streams; scan all streams.
        result2 = _run(
            [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_entries', 'stream_tags=timecode,com.apple.quicktime.timecode',
                '-show_streams',
                vpath,
            ],
            capture_output=True, text=True, check=True,
            creationflags=_NO_WINDOW,
        )
        data2 = json.loads(result2.stdout or '{}')
        tc_tokens = _collect_timecode_tokens({'streams': data2.get('streams', []) or []})

        # Also include container-level tags (some muxers put timecode here).
        result3 = _run(
            [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_entries', 'format_tags=timecode,com.apple.quicktime.timecode',
                vpath,
            ],
            capture_output=True, text=True, check=True,
            creationflags=_NO_WINDOW,
        )
        data3 = json.loads(result3.stdout or '{}')
        tc_tokens = _collect_timecode_tokens({'format': data3.get('format', {}), 'streams': []}) + tc_tokens

        info = _probe_video(vpath)
        fps = float(info.get('fps') or 0.0)

        if tc_tokens and fps > 0:
            # Prefer a token that parses; if multiple, pick closest to creation anchor.
            candidates: list[datetime] = []
            for tok in tc_tokens:
                dt_tc = _apply_timecode_to_anchor(anchor, tok, fps)
                if dt_tc:
                    candidates.append(dt_tc)
            if candidates:
                best = min(candidates, key=lambda dt: abs((dt - anchor).total_seconds()))
                delta = abs((best - anchor).total_seconds())
                logger.info(
                    'auto_sync: video clock from SMPTE (preferred over creation) Δ=%.3fs vs anchor '
                    '(video=%s tc=%r anchor=%s best=%s fps=%.3f)',
                    delta,
                    vpath,
                    tc_tokens[0],
                    anchor.isoformat(),
                    best.isoformat(),
                    fps,
                )
                return best

        return anchor
    except Exception as e:
        logger.info('auto_sync: ffprobe creation_time failed for %s: %s', vpath, e)
        logger.debug('auto_sync: creation_time ffprobe traceback', exc_info=True)
        return None


def _probe_video_first_timecode_str(vpath: str) -> Optional[str]:
    """Return the first SMPTE timecode token found in the file (best-effort)."""
    try:
        tokens: list[str] = []

        # Container-level tags (some muxers put timecode here).
        r3 = _run(
            [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_entries', 'format_tags=timecode,com.apple.quicktime.timecode',
                vpath,
            ],
            capture_output=True, text=True, check=True,
            creationflags=_NO_WINDOW,
        )
        data3 = json.loads(r3.stdout or '{}')
        fmt_tags = (data3.get('format', {}) or {}).get('tags', {}) or {}
        for k in ('timecode', 'com.apple.quicktime.timecode'):
            v = fmt_tags.get(k)
            if v:
                tokens.append(str(v).strip())

        # Scan all streams (timecode track is often not the video stream).
        r2 = _run(
            [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_entries', 'stream_tags=timecode,com.apple.quicktime.timecode',
                '-show_streams',
                vpath,
            ],
            capture_output=True, text=True, check=True,
            creationflags=_NO_WINDOW,
        )
        data2 = json.loads(r2.stdout or '{}')
        for st in (data2.get('streams', []) or []):
            tags = (st.get('tags', {}) or {}) if isinstance(st, dict) else {}
            for k in ('timecode', 'com.apple.quicktime.timecode'):
                v = tags.get(k)
                if v:
                    tokens.append(str(v).strip())

        # Return first non-empty token.
        for t in tokens:
            if t:
                return t
        return None
    except Exception:
        logger.debug('auto_sync: ffprobe timecode token failed for %s', vpath, exc_info=True)
        return None


def _session_start_utc(csv_path: str) -> Optional[datetime]:
    """Read telemetry session absolute start timestamp (UTC)."""
    try:
        from session_scanner import _read_csv_start_time
        dt = _read_csv_start_time(csv_path)
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logger.info('auto_sync: session start time read failed for %s: %s', csv_path, e)
        logger.debug('auto_sync: session start traceback', exc_info=True)
        return None


def _metadata_baseline_offset(
    csv_path: str,
    video_paths: List[str],
) -> Tuple[Optional[float], Optional[datetime], Optional[datetime]]:
    """Return baseline offset using session/video metadata timestamps."""
    sess_start = _session_start_utc(csv_path)
    if not sess_start:
        logger.info(
            'auto_sync: no metadata baseline — CSV session start time missing/unreadable (%s)',
            csv_path,
        )
        return None, None, None
    paths = list(video_paths or [])
    if not paths:
        logger.info(
            'auto_sync: no metadata baseline — no video_paths (%s)',
            csv_path,
        )
        return None, sess_start, None
    for vp in paths:
        vid_start = _probe_video_creation_time_utc(vp)
        if vid_start:
            tc0 = _probe_video_first_timecode_str(vp)
            # sync_offset convention: session_time = video_time - sync_offset
            # At video_time=0, session_time = (real_time_video_start - real_time_session_start).
            # Therefore sync_offset = session_start - video_start.
            off = float((sess_start - vid_start).total_seconds())
            logger.info(
                'auto_sync: metadata baseline=%.3fs (telemetry_t0_utc=%s video_start_utc=%s tc0=%r video=%s)',
                off,
                sess_start.isoformat(),
                vid_start.isoformat(),
                tc0,
                vp,
            )
            return off, sess_start, vid_start
    logger.info(
        'auto_sync: no metadata baseline — creation_time missing on all %d video(s) for %s '
        '(will use wide ±%.0fs cross-correlation only)',
        len(paths),
        csv_path,
        SEARCH_WINDOW_S,
    )
    return None, sess_start, None


# ── Cross-correlation ─────────────────────────────────────────────────────────

def _z_normalize(x: np.ndarray) -> np.ndarray:
    std = x.std()
    return (x - x.mean()) / std if std > 1e-10 else x - x.mean()


def _parabolic_peak(xcorr: np.ndarray, idx: int) -> float:
    if idx <= 0 or idx >= len(xcorr) - 1:
        return float(idx)
    y0, y1, y2 = xcorr[idx - 1], xcorr[idx], xcorr[idx + 1]
    denom = y0 - 2 * y1 + y2
    if abs(denom) < 1e-12:
        return float(idx)
    return idx + 0.5 * (y0 - y2) / denom


def _correlate(
    vid_sig: np.ndarray,
    tel_sig: np.ndarray,
    fps: float,
    search_window_s: float,
    center_offset_s: Optional[float] = None,
) -> Tuple[float, float]:
    v = _z_normalize(vid_sig)
    t = _z_normalize(tel_sig)
    xcorr = sp_signal.correlate(v, t, mode='full')
    lags  = sp_signal.correlation_lags(len(v), len(t))
    lag_s = lags / fps
    center = float(center_offset_s or 0.0)
    mask = np.abs(lag_s - center) <= search_window_s
    if not mask.any():
        return 0.0, 0.0
    win_indices = np.where(mask)[0]
    best_in_win = win_indices[np.argmax(xcorr[mask])]
    sub_idx = _parabolic_peak(xcorr, best_in_win)
    offset = (sub_idx - (len(tel_sig) - 1)) / fps
    rms = float(np.sqrt(np.mean(xcorr**2)))
    confidence = float(xcorr[best_in_win]) / rms if rms > 0 else 0.0
    return float(offset), confidence


# ── Main entry point ──────────────────────────────────────────────────────────

def run_auto_sync(
    csv_path:             str,
    video_paths:          List[str],
    source:               str,
    fps:                  float = FPS,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    min_confidence:       float = MIN_CONFIDENCE,
    search_window_s:      float = SEARCH_WINDOW_S,
    metadata_fine_window_s: float = METADATA_FINE_WINDOW_S,
    cancel_event:         Optional[threading.Event] = None,
    progress_cb:          Optional[Callable] = None,
    snap_to_frame:        bool = True,
    use_motion:           bool = False,
) -> Tuple[Optional[float], float]:
    """
    Detect sync offset for one session.

    When ``use_motion`` is False (default): only file / stream timestamps → baseline
    offset; no ffmpeg streaming or cross-correlation. Returns ``(None, 0)`` if no
    metadata baseline exists.

    When ``use_motion`` is True: streams ffmpeg frames and correlates motion vs
    telemetry until ``confidence_threshold`` or exhaustion; may fall back to the
    snapped metadata baseline when correlation is weak.

    progress_cb(vid_t, offset, confidence, **kwargs) — optional progress updates.
    cancel_event — threading.Event; set to abort early (motion mode only).

    Returns:
        (offset, confidence).  Metadata-only mode returns confidence ``0.0`` so
        the UI/backend treat the result as ``auto_baseline`` for manual refinement.
    """
    baseline_offset, sess_start_utc, vid_start_utc = _metadata_baseline_offset(
        csv_path,
        video_paths,
    )
    if not use_motion:
        if progress_cb:
            try:
                progress_cb(
                    0.0,
                    baseline_offset if baseline_offset is not None else 0.0,
                    0.0,
                    stage='metadata',
                    baseline_offset=baseline_offset,
                    session_start_utc=sess_start_utc.isoformat() if sess_start_utc else None,
                    video_start_utc=vid_start_utc.isoformat() if vid_start_utc else None,
                    mode='metadata-only',
                )
            except Exception:
                pass
        if baseline_offset is None:
            logger.info(
                'auto_sync: metadata-only — no timestamp baseline for %s (align manually)',
                csv_path,
            )
            return None, 0.0
        off = float(baseline_offset)
        if snap_to_frame and video_paths:
            try:
                info = _probe_video(video_paths[0])
                vid_fps = float(info.get('fps') or 0.0)
                if vid_fps > 0:
                    snapped = _snap_offset_to_frame(off, vid_fps)
                    if abs(snapped - off) > 1e-9:
                        tc0 = _probe_video_first_timecode_str(video_paths[0])
                        logger.info(
                            'auto_sync: metadata-only snap fps=%.3f offset %.6f → %.6f (telemetry_t0_utc=%s video_start_utc=%s tc0=%r)',
                            vid_fps,
                            off,
                            snapped,
                            sess_start_utc.isoformat() if sess_start_utc else None,
                            vid_start_utc.isoformat() if vid_start_utc else None,
                            tc0,
                        )
                    off = snapped
            except Exception as e:
                logger.debug('auto_sync: metadata-only snap_to_frame failed: %s', e, exc_info=True)
        logger.info(
            'auto_sync: metadata-only OK %s offset=%.3fs (motion refine skipped)',
            csv_path,
            off,
        )
        return off, 0.0

    # Use a higher analysis FPS during metadata+refine so sub-second metadata
    # timestamps don't cap alignment precision. Upper bound keeps CPU reasonable.
    work_fps = float(fps)
    if baseline_offset is not None and video_paths:
        try:
            info0 = _probe_video(video_paths[0])
            native_fps = float(info0.get('fps') or 0.0)
            if native_fps > 0:
                # Use video FPS rounded up to the nearest 10 (e.g. 29.97→30, 59.94→60).
                # Cap to 120 to keep CPU bounded.
                import math as _m
                work_fps = min(120.0, float(_m.ceil(native_fps / 10.0) * 10.0))
        except Exception:
            pass

    try:
        tel_sig_full, session_t, session = _load_telemetry_session_grid(csv_path, source, work_fps)
    except Exception:
        logger.exception('auto_sync: telemetry load failed for %s (source=%r)', csv_path, source)
        return None, 0.0

    logger.info(
        'auto_sync: begin %s source=%r telemetry_samples=%d fps=%.2f '
        'confidence_write>=%.1f accept_if>=%.1f',
        csv_path,
        source,
        int(len(tel_sig_full)),
        work_fps,
        confidence_threshold,
        min_confidence,
    )
    if progress_cb:
        try:
            progress_cb(
                0.0,
                baseline_offset if baseline_offset is not None else 0.0,
                0.0,
                stage='metadata',
                baseline_offset=baseline_offset,
                session_start_utc=sess_start_utc.isoformat() if sess_start_utc else None,
                video_start_utc=vid_start_utc.isoformat() if vid_start_utc else None,
            )
        except Exception:
            pass

    # Metadata-first mode: fixed ±10s lag search around the baseline correlation peak.
    lap_iv: Optional[Tuple[float, float]] = None
    lap_V_lo: Optional[float] = None
    lap_V_hi: Optional[float] = None
    tel_lap: Optional[Tuple[np.ndarray, float]] = None
    if baseline_offset is not None:
        corr_center = baseline_offset
        corr_window = 10.0
        tel_margin_s = max(0.5, 2.0 / max(1e-6, work_fps))
        lap_iv = _first_timed_lap_session_interval(session)
        if lap_iv:
            L0, L1 = lap_iv
            O = float(baseline_offset)
            pre = max(3.0, tel_margin_s, corr_window * 0.5)
            post = pre
            lap_V_lo = L0 + O
            lap_V_hi = L1 + O
            s_lo = float(L0 - pre)
            s_hi = float(L1 + post)
            if session_t.size:
                s_lo = max(s_lo, float(session_t[0]))
                s_hi = min(s_hi, float(session_t[-1]))
            span_t = max(float(lap_V_hi - lap_V_lo) + 4.0 * corr_window, 20.0)
            min_samp = max(120, int(span_t * work_fps))
            tel_lap = _slice_telemetry_session(
                tel_sig_full, session_t, s_lo, s_hi, min_samples=min_samp,
            )
            logger.info(
                'auto_sync: motion refine = first timed lap session [%.3f..%.3f]s → '
                'video [%.3f..%.3f]s (%d tel samples, baseline=%.3fs)',
                L0,
                L1,
                lap_V_lo,
                lap_V_hi,
                int(tel_lap[0].size),
                O,
            )
    else:
        corr_center = None
        corr_window = float(search_window_s)
        tel_margin_s = max(0.5, 2.0 / max(1e-6, work_fps))

    all_sig:       list = []
    cumulative           = 0.0
    best_offset          = 0.0
    best_conf            = 0.0
    frames_per_check     = max(1, int(CHECK_EVERY_S * work_fps))

    for vpath in video_paths:
        if cancel_event and cancel_event.is_set():
            break
        try:
            info = _probe_video(vpath)
        except Exception as e:
            logger.warning('auto_sync: ffprobe stream probe failed for %s: %s', vpath, e)
            continue

        orig_h   = info['height']
        new_h    = max(2, int(orig_h * RESIZE_W / info['width']))
        new_h   += new_h % 2
        duration = info['duration']

        # If we have a metadata baseline and an absolute video start time for this file,
        # decode only the small overlapping window instead of the entire prefix.
        local_center = None
        if baseline_offset is not None and sess_start_utc is not None:
            vst = _probe_video_creation_time_utc(vpath)
            if vst:
                local_center = float((sess_start_utc - vst).total_seconds())

        margin_s = max(0.5, 2.0 / max(1e-6, work_fps))
        local_ss = 0.0
        local_t = max(0.01, float(duration or 0.0) or 60.0)
        tel_sig = tel_sig_full
        tel_t0_sess = float(session_t[0]) if session_t.size else 0.0
        tel_anchor_v = 0.0

        if tel_lap is not None:
            tel_sig, tel_t0_sess = tel_lap

        if lap_V_lo is not None and lap_V_hi is not None and duration > 0:
            c0 = float(cumulative)
            seg_lo = max(lap_V_lo, c0)
            seg_hi = min(lap_V_hi, c0 + float(duration))
            if seg_hi <= seg_lo + 1e-3:
                cumulative += float(duration)
                continue
            local_ss = seg_lo - c0
            local_t = max(0.25, seg_hi - seg_lo)
        elif baseline_offset is not None and local_center is not None and duration > 0:
            local_ss, local_t = _decode_window_for_baseline(
                center_offset_s=local_center,
                window_s=corr_window,
                margin_s=margin_s,
                file_duration_s=float(duration),
            )
            video_seg_start_pre = float(cumulative + local_ss)
            S_clip = video_seg_start_pre - float(baseline_offset)
            min_samp = max(120, int(4 * corr_window * work_fps))
            tel_sig, tel_t0_sess = _slice_telemetry_session(
                tel_sig_full,
                session_t,
                S_clip - corr_window - tel_margin_s,
                S_clip + local_t + corr_window + tel_margin_s,
                min_samples=min_samp,
            )
        elif baseline_offset is not None and duration > 0:
            local_ss = 0.0
            local_t = min(90.0, max(0.25, float(duration)))
            span = max(120.0, local_t + 2 * corr_window)
            min_samp = max(120, int(span * work_fps))
            tel_sig, tel_t0_sess = _slice_telemetry_session(
                tel_sig_full,
                session_t,
                float(session_t[0]) if session_t.size else 0.0,
                float(session_t[0]) + span if session_t.size else span,
                min_samples=min_samp,
            )

        # session_time = video_time - sync_offset  →  video_time = session_time + sync_offset
        video_seg_start = float(cumulative + local_ss)
        if baseline_offset is not None:
            corr_center_rel = float(
                video_seg_start - tel_t0_sess - float(baseline_offset)
            )
            tel_anchor_v = float(tel_t0_sess + float(baseline_offset))
        else:
            corr_center_rel = float(corr_center or 0.0)
            tel_anchor_v = 0.0

        cmd = [
            # Workaround: FFmpeg frame-threading can assert/crash on some codecs
            # on Windows ("Assertion fctx->async_lock failed ... pthread_frame.c").
            # For auto-sync we prefer robustness over speed, so force single-thread decode.
            'ffmpeg', '-hide_banner', '-nostdin', '-threads', '1',
            '-ss', f'{local_ss:.6f}', '-i', vpath,
            '-t', f'{local_t:.6f}',
            '-vf', f'fps={work_fps},scale={RESIZE_W}:{new_h}',
            '-f', 'rawvideo', '-pix_fmt', 'gray',
            '-loglevel', 'error', 'pipe:1',
        ]
        try:
            proc = _popen(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          creationflags=_NO_WINDOW)
        except Exception as e:
            logger.warning('auto_sync: ffmpeg launch failed for %s: %s', vpath, e)
            continue

        frame_size    = RESIZE_W * new_h
        prev          = None
        frame_idx     = 0
        stopped_early = False
        decode_report_frames = max(1, int(work_fps * float(DECODE_PROGRESS_INTERVAL_S)))

        while True:
            if cancel_event and cancel_event.is_set():
                proc.kill()
                proc.wait()
                break
            raw = proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            frame = (
                np.frombuffer(raw, dtype=np.uint8)
                .reshape(new_h, RESIZE_W)
                .astype(np.float32)
            )
            motion = float(np.mean(np.abs(frame - prev))) if prev is not None else 0.0
            all_sig.append(motion)
            prev       = frame
            frame_idx += 1

            # Cheap progress for WebView: real decoded timeline, without extra correlate().
            if progress_cb and frame_idx % decode_report_frames == 0:
                vid_t_decode = float(video_seg_start + frame_idx / work_fps)
                try:
                    progress_cb(
                        vid_t_decode,
                        best_offset,
                        best_conf,
                        stage='refine',
                        baseline_offset=baseline_offset,
                        search_window_s=corr_window if baseline_offset is not None else None,
                        mode='metadata+refine' if baseline_offset is not None else 'crosscorr-only',
                    )
                except Exception:
                    pass

            if frame_idx % frames_per_check == 0 and len(all_sig) > 10:
                vid_s  = np.array(all_sig)
                offset, conf = _correlate(
                    vid_s, tel_sig, work_fps, corr_window, center_offset_s=corr_center_rel
                )
                # offset returned by _correlate assumes both signals start at t=0.
                # Adjust back into full-session definition:
                #   offset_full = offset + (video_segment_start - telemetry_segment_start)
                offset += (video_seg_start - tel_anchor_v)
                vid_t_now = video_seg_start + frame_idx / work_fps
                if progress_cb:
                    try:
                        progress_cb(
                            vid_t_now, offset, conf,
                            stage='refine',
                            baseline_offset=baseline_offset,
                            search_window_s=corr_window,
                            mode='metadata+refine' if baseline_offset is not None else 'crosscorr-only',
                        )
                    except Exception:
                        pass
                if conf >= confidence_threshold:
                    proc.kill()
                    proc.wait()
                    best_offset, best_conf = offset, conf
                    stopped_early = True
                    cumulative += frame_idx / work_fps
                    break
                best_offset, best_conf = offset, conf

        if not stopped_early:
            proc.wait()
            if proc.returncode not in (0, None):
                err = _read_stderr_tail(proc)
                if err:
                    logger.warning('auto_sync: ffmpeg decode error for %s:\n%s', vpath, err.strip())
            cumulative += duration
        else:
            break

    if not all_sig:
        logger.warning(
            'auto_sync: no motion samples decoded for %s (ffprobe ok but 0 frames — '
            'check video path / ffmpeg / codec)',
            csv_path,
        )

    if best_conf < min_confidence:
        mode = 'metadata+refine' if baseline_offset is not None else 'crosscorr-only'
        logger.info(
            'auto_sync: REJECT %s — best_conf=%.3f < min_confidence=%.3f '
            '(early_stop_threshold=%.1f, mode=%s, motion_frames=%d, '
            'best_offset_if_any=%.3fs, corr_window_s=%.2f, baseline=%s)',
            csv_path,
            best_conf,
            min_confidence,
            confidence_threshold,
            mode,
            len(all_sig),
            best_offset,
            corr_window,
            f'{baseline_offset:.3f}' if baseline_offset is not None else 'None',
        )
        # If we have a metadata baseline, still return it as a usable starting point for
        # manual frame-by-frame refinement (even when correlation is inconclusive).
        if baseline_offset is not None:
            off = float(baseline_offset)
            if snap_to_frame and video_paths:
                try:
                    info = _probe_video(video_paths[0])
                    vid_fps = float(info.get('fps') or 0.0)
                    snapped = _snap_offset_to_frame(off, vid_fps)
                    if abs(snapped - off) > 1e-9:
                        logger.info(
                            'auto_sync: baseline_fallback snap_to_frame fps=%.3f offset %.6f → %.6f (Δ=%.6fs)',
                            vid_fps,
                            off,
                            snapped,
                            snapped - off,
                        )
                    off = snapped
                except Exception as e:
                    logger.debug('auto_sync: baseline_fallback snap_to_frame failed: %s', e, exc_info=True)
            logger.warning(
                'auto_sync: BASELINE_FALLBACK %s offset=%.3fs (corr_best_conf=%.3f < min=%.3f) — '
                'treat as provisional; refine manually',
                csv_path,
                off,
                best_conf,
                min_confidence,
            )
            return off, float(best_conf)

        return None, best_conf

    logger.info(
        'auto_sync: OK %s offset=%.3fs confidence=%.3f (min_accept=%.1f)',
        csv_path,
        best_offset,
        best_conf,
        min_confidence,
    )
    if snap_to_frame and video_paths:
        try:
            info = _probe_video(video_paths[0])
            vid_fps = float(info.get('fps') or 0.0)
            snapped = _snap_offset_to_frame(best_offset, vid_fps)
            if abs(snapped - best_offset) > 1e-9:
                logger.info(
                    'auto_sync: snap_to_frame fps=%.3f offset %.6f → %.6f (Δ=%.6fs)',
                    vid_fps,
                    best_offset,
                    snapped,
                    snapped - best_offset,
                )
            best_offset = snapped
        except Exception as e:
            logger.debug('auto_sync: snap_to_frame failed: %s', e, exc_info=True)
    return best_offset, best_conf
