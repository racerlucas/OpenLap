"""
video_renderer.py — Video rendering engine
===========================================
Handles video joining (ffmpeg), frame rendering (multiprocessing),
and final mux. No GUI state — all inputs passed explicitly.

**Telemetry time base:** ``sess_t = vid_t - sync_offset`` then
``session.interpolate_at(sess_t)`` — same session timeline as
``load_preview_history`` / editor preview. Lap timer, delta, lap-info rows, and
map geometry must use the same helpers as the preview RPC path
(``telemetry_algorithms``); only video I/O and pixel compositing live here.
"""

from __future__ import annotations
import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from collections import deque
from multiprocessing import Pool
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _unique_export_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for n in range(2, 10000):
        cand = f"{root}_{n}{ext}"
        if not os.path.exists(cand):
            return cand
    return path


def _fallback_session_utc(session: Session) -> datetime:
    pts = session.all_points
    if pts:
        t = pts[0].time
        if t.tzinfo is None:
            return t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc)
    du = getattr(session, 'date_utc', '') or ''
    try:
        if du.strip():
            return datetime.fromisoformat(du.strip().replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc)

from utils import _run, _popen
import cv2
import numpy as np
from telemetry_algorithms import (
    LAP_TIME_HOLD_AFTER_FINISH_S,
    MAP_MAX_POINTS,
    MAP_REF_SMOOTH_WINDOW,
    MAP_SMOOTH_WINDOW,
    MAP_TIMED_SAMPLES,
    build_effective_session_meta,
    build_complete_map_track,
    build_map_track,
    build_lap_info_lookup,
    build_history_row,
    lap_info_fields_for_sample,
    lap_time_display_value,
)

from data_model import Session, Lap, absolute_time_at_elapsed
from export_codec import resolve_export_container_extension
from overlay_worker import render_frame_worker, scale_factor, default_layout
from exceptions import VideoConcatError, VideoMuxError, LapOutOfRangeError

_N_SECTORS = 3  # number of track sectors used for delta-time display

# ── FFmpeg helpers ─────────────────────────────────────────────────────────────

def detect_encoder() -> str:
    """Detect best available hardware encoder, fall back to libx264."""
    tests = [
        (['ffmpeg', '-hide_banner', '-f', 'lavfi', '-i', 'nullsrc',
          '-t', '0.1', '-c:v', 'h264_nvenc', '-f', 'null', '-'], 'h264_nvenc'),
        (['ffmpeg', '-hide_banner', '-f', 'lavfi', '-i', 'nullsrc',
          '-t', '0.1', '-c:v', 'h264_amf',   '-f', 'null', '-'], 'h264_amf'),
        (['ffmpeg', '-hide_banner', '-f', 'lavfi', '-i', 'nullsrc',
          '-t', '0.1', '-c:v', 'h264_qsv',   '-f', 'null', '-'], 'h264_qsv'),
    ]
    for cmd, enc in tests:
        try:
            r = _run(cmd, timeout=5)
            if r.returncode == 0:
                return enc
        except Exception:
            pass
    return 'libx264'


def concat_videos(input_files: List[str], output: str) -> None:
    """Join video files using ffmpeg concat demuxer (no re-encode)."""
    with tempfile.NamedTemporaryFile('w', suffix='.txt',
                                     delete=False, encoding='utf-8') as f:
        for p in input_files:
            f.write(f"file '{os.path.abspath(p)}'\n")
        concat_file = f.name
    try:
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
               '-i', concat_file, '-c', 'copy', output]
        r = _run(cmd)
        if r.returncode != 0:
            cmd2 = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                    '-i', concat_file,
                    '-c:v', 'libx264', '-crf', '18', '-c:a', 'aac', output]
            r2 = _run(cmd2)
            if r2.returncode != 0:
                err = r2.stderr.decode(errors='replace')
                logger.error('FFmpeg concat failed:\n%s', err)
                raise VideoConcatError(err[-600:])
    finally:
        os.unlink(concat_file)


class MultiCap:
    """
    Virtual VideoCapture over multiple files.
    Exposes the same .get()/.set()/.read()/.release() interface as a
    single cv2.VideoCapture, so callers need no special-casing.
    Frame indices are global across all clips; seeks are O(1).
    """

    def __init__(self, paths: List[str]):
        self._caps: List[cv2.VideoCapture] = []
        self._offsets: List[int] = []   # global start frame of each clip
        self._counts:  List[int] = []   # frame count of each clip
        self._fps: float = 30.0
        self._total: int = 0
        self._cur_global: int = 0

        offset = 0
        for p in paths:
            cap = cv2.VideoCapture(p)
            if not cap.isOpened():
                raise IOError(f"Cannot open video: {p}")
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            cnt = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self._caps.append(cap)
            self._offsets.append(offset)
            self._counts.append(cnt)
            self._fps = fps          # assume homogeneous; last wins
            offset += cnt

        self._total = offset

    # ── cv2.VideoCapture-compatible interface ──────────────────────────────

    def isOpened(self) -> bool:
        return bool(self._caps)

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FPS:
            return self._fps
        if prop_id == cv2.CAP_PROP_FRAME_COUNT:
            return float(self._total)
        return 0.0

    def set(self, prop_id: int, value: float) -> bool:
        if prop_id == cv2.CAP_PROP_POS_FRAMES:
            self._cur_global = int(value)
            return True
        return False

    def read(self):
        fidx = self._cur_global
        if fidx < 0 or fidx >= self._total:
            return False, None

        # Find which clip owns this global frame
        clip_idx = 0
        for i, (off, cnt) in enumerate(zip(self._offsets, self._counts)):
            if off + cnt > fidx:
                clip_idx = i
                break

        local_frame = fidx - self._offsets[clip_idx]
        cap = self._caps[clip_idx]
        cap.set(cv2.CAP_PROP_POS_FRAMES, local_frame)
        ret, frame = cap.read()
        self._cur_global = fidx + 1
        return ret, frame

    def release(self):
        for cap in self._caps:
            cap.release()
        self._caps.clear()


def _build_export_vf_chain(encode_options: Optional[dict]) -> str:
    """scale (optional) + fps (optional) + even dimensions + yuv420p for encoders."""
    eo = encode_options or {}
    parts: List[str] = []
    tr = str(eo.get('export_target_res', 'auto') or 'auto').strip().lower()
    tf = str(eo.get('export_target_fps', 'auto') or 'auto').strip().lower()
    if tr != 'auto' and 'x' in tr:
        try:
            a, b = tr.split('x', 1)
            tw, th = int(float(a)), int(float(b))
            tw -= tw % 2
            th -= th % 2
            if tw > 0 and th > 0:
                parts.append(f'scale={tw}:{th}:flags=lanczos+accurate_rnd')
        except Exception:
            pass
    if tf != 'auto':
        try:
            fv = float(tf)
            if fv > 0:
                parts.append(f'fps={fv}')
        except Exception:
            pass
    parts.append('scale=trunc(iw/2)*2:trunc(ih/2)*2')
    parts.append('format=yuv420p')
    return ','.join(parts)


def _build_video_encode_flags(encoder: str, crf: int, encode_options: Optional[dict]) -> List[str]:
    """Encoder-specific rate control and quality flags (excluding ``-c:v``).

    CQ / CRF / NVENC CQ only apply when ``export_rate_mode`` is ``cq``.
    For CBR/VBR with target bitrate unset (0 / auto), we derive a nominal kbps
    from resolution × fps using the same heuristic as size estimation (still
    keyed by *crf* as a quality seed), then apply true CBR/VBR flags — the
    slider is hidden in the UI for CBR/VBR because it is not an FFmpeg CRF in
    those modes.
    """
    eo = encode_options or {}
    rm = str(eo.get('export_rate_mode', 'cq') or 'cq').lower()
    vb = int(eo.get('export_video_bitrate_kbps', 0) or 0)
    vmax = int(eo.get('export_video_max_bitrate_kbps', 0) or 0)
    maxq = bool(eo.get('export_max_quality', False))
    enc = (encoder or '').lower()
    c = max(0, min(51, int(crf)))

    if rm in ('cbr', 'vbr') and vb <= 0:
        vw_m = int(eo.get('_export_mux_vw') or 0)
        vh_m = int(eo.get('_export_mux_vh') or 0)
        fp_m = float(eo.get('_export_mux_fps') or 0.0)
        if vw_m > 0 and vh_m > 0 and fp_m > 0:
            from export_codec import estimate_effective_avg_video_kbps
            vb = int(estimate_effective_avg_video_kbps(
                enc, 'cq', c, 0, vmax, vw_m, vh_m, fp_m,
            ))
        else:
            vb = 8000
        vb = max(500, min(80_000, vb))

    use_cq = (rm == 'cq')

    if enc == 'libx264':
        out = ['-preset', 'slower' if maxq else 'medium']
        if use_cq:
            out += ['-crf', str(c)]
        else:
            mx = vmax if vmax > 0 else max(vb, int(vb * 1.85 + 0.5))
            if rm == 'cbr':
                out += ['-b:v', f'{vb}k', '-minrate', f'{vb}k', '-maxrate', f'{vb}k',
                        '-bufsize', f'{vb * 2}k']
            else:
                out += ['-b:v', f'{vb}k', '-maxrate', f'{mx}k', '-bufsize', f'{max(mx, vb) * 2}k']
        out += ['-profile:v', 'main', '-g', '60']
        return out

    if enc == 'libx265':
        out = ['-preset', 'slow' if maxq else 'medium']
        if use_cq:
            out += ['-crf', str(c)]
        else:
            mx = vmax if vmax > 0 else max(vb, int(vb * 1.85 + 0.5))
            if rm == 'cbr':
                out += ['-b:v', f'{vb}k', '-minrate', f'{vb}k', '-maxrate', f'{vb}k',
                        '-bufsize', f'{vb * 2}k']
            else:
                out += ['-b:v', f'{vb}k', '-maxrate', f'{mx}k', '-bufsize', f'{max(mx, vb) * 2}k']
        out += ['-profile:v', 'main', '-g', '60']
        return out

    if 'nvenc' in enc:
        out = ['-preset', 'p7' if maxq else 'p4']
        if enc == 'h264_nvenc':
            out += ['-tune', 'uhq' if maxq else 'hq']
        if use_cq:
            out += ['-rc', 'vbr', '-cq', str(c), '-b:v', '0']
        else:
            if rm == 'cbr':
                out += ['-rc', 'cbr', '-b:v', f'{vb}k']
                mxk = f'{vmax}k' if vmax > 0 else f'{vb}k'
                bsz = (max(vb, vmax) * 2) if vmax > 0 else vb * 2
                out += ['-maxrate', mxk, '-bufsize', f'{bsz}k']
            else:
                mx = vmax if vmax > 0 else max(vb, int(vb * 1.85 + 0.5))
                out += ['-rc', 'vbr', '-b:v', f'{vb}k', '-maxrate', f'{mx}k',
                        '-bufsize', f'{max(mx, vb) * 2}k']
        prof = 'high' if '264' in enc else 'main'
        out += ['-profile:v', prof, '-g', '60']
        return out

    out = ['-profile:v', 'main', '-g', '60']
    if use_cq:
        out += ['-qp', str(c)]
    elif vb > 0:
        out += ['-b:v', f'{vb}k']
        if vmax > 0:
            out += ['-maxrate', f'{vmax}k', '-bufsize', f'{max(vmax, vb) * 2}k']
    else:
        out += ['-qp', str(c)]
    return out


def _ffmpeg_mux_exec(
    cmd: List[str],
    *,
    total_s: float,
    prog_start: float,
    prog_end: float,
    progress_cb,
) -> None:
    import threading, subprocess as _sp

    if progress_cb and total_s > 0:
        proc = _popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE)
        stderr_buf: list[bytes] = []

        def _drain():
            stderr_buf.extend(proc.stderr)

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        pct_range = prog_end - prog_start
        for raw_line in proc.stdout:
            line = raw_line.decode(errors='replace').strip()
            if line.startswith('out_time_ms='):
                try:
                    us = int(line.split('=', 1)[1])
                    elapsed = us / 1_000_000.0
                    frac = min(1.0, elapsed / total_s)
                    pct = prog_start + frac * pct_range
                    progress_cb(pct, f"Muxing audio…  {elapsed:.1f} / {total_s:.1f}s")
                except (ValueError, ZeroDivisionError):
                    pass
        proc.wait()
        t.join()
        if proc.returncode != 0:
            err = b''.join(stderr_buf).decode(errors='replace')
            logger.error('FFmpeg mux failed:\n%s', err)
            raise VideoMuxError(err[-600:])
    else:
        r = _run(cmd)
        if r.returncode != 0:
            err = r.stderr.decode(errors='replace')
            logger.error('FFmpeg mux failed:\n%s', err)
            raise VideoMuxError(err[-600:])


def mux_audio(raw_video: str, audio_source: str,
               output: str, encoder: str, crf: int = 18,
               audio_start: float = 0.0,
               total_s: float = 0.0,
               prog_start: float = 87.0,
               prog_end: float = 100.0,
               progress_cb=None,
               creation_time_utc: Optional[datetime] = None,
               encode_options: Optional[dict] = None) -> None:
    """Re-encode raw OpenCV video, mux source audio (copy preferred), trim to shortest.

    When *progress_cb* and *total_s* are provided the function parses ffmpeg's
    machine-readable progress output and calls progress_cb(pct, msg) as the
    mux advances, interpolating between prog_start and prog_end.
    """
    eo = encode_options if isinstance(encode_options, dict) else {}
    vf = _build_export_vf_chain(eo)
    vflags = _build_video_encode_flags(encoder, crf, eo)
    _ab_cfg = int(eo.get('export_audio_bitrate_kbps', 0) or 0)
    # 0 = 跟随原片（优先 -c:a copy）；仅在必须重编码 AAC 时用中等码率。
    ab_kbps = max(32, min(320, _ab_cfg)) if _ab_cfg > 0 else 192

    meta_args: list = []
    if creation_time_utc is not None:
        ct = creation_time_utc.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000000Z')
        meta_args = ['-metadata', f'creation_time={ct}']

    out_ext = os.path.splitext(output)[1].lower()
    tail: list[str] = []
    if out_ext in ('.mp4', '.m4v'):
        tail = ['-movflags', '+faststart']

    def _build_cmd(audio_copy: bool) -> List[str]:
        ac = (['-c:a', 'copy'] if audio_copy else
              ['-c:a', 'aac', '-b:a', f'{ab_kbps}k'])
        return (['ffmpeg', '-y', '-hide_banner',
                 '-i', raw_video,
                 '-ss', f'{audio_start:.6f}', '-i', audio_source,
                 '-map', '0:v', '-map', '1:a?',
                 '-vf', vf,
                 '-c:v', encoder] + vflags + ac + ['-shortest']
                + meta_args + tail)

    def _run_attempt(audio_copy: bool) -> None:
        base = _build_cmd(audio_copy)
        if progress_cb and total_s > 0:
            cmd = base + ['-progress', 'pipe:1', '-nostats', output]
        else:
            cmd = base + [output]
        _ffmpeg_mux_exec(cmd, total_s=total_s, prog_start=prog_start,
                         prog_end=prog_end, progress_cb=progress_cb)

    try:
        _run_attempt(True)
    except VideoMuxError as e:
        logger.warning('Mux with -c:a copy failed; retrying AAC: %s', e)
        _run_attempt(False)


def _build_bgr_pipe_mux_cmd(
    video_path: str,
    output: str,
    encoder: str,
    crf: int,
    vw: int,
    vh: int,
    fps: float,
    audio_start: float,
    audio_copy: bool,
    encode_options: Optional[dict],
    creation_time_utc: Optional[datetime],
    progress_pipe: bool,
) -> List[str]:
    """FFmpeg argv: BGR rawvideo on stdin + trimmed audio from *video_path* → *output*.

    Same filter / rate-control / metadata as :func:`mux_audio`, without the MJPEG
    intermediate file (encode runs while frames are produced).
    """
    eo = encode_options if isinstance(encode_options, dict) else {}
    vf = _build_export_vf_chain(eo)
    vflags = _build_video_encode_flags(encoder, crf, eo)
    _ab_cfg = int(eo.get('export_audio_bitrate_kbps', 0) or 0)
    ab_kbps = max(32, min(320, _ab_cfg)) if _ab_cfg > 0 else 192

    meta_args: list = []
    if creation_time_utc is not None:
        ct = creation_time_utc.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000000Z')
        meta_args = ['-metadata', f'creation_time={ct}']

    out_ext = os.path.splitext(output)[1].lower()
    tail: list[str] = []
    if out_ext in ('.mp4', '.m4v'):
        tail = ['-movflags', '+faststart']

    ac = (['-c:a', 'copy'] if audio_copy else
          ['-c:a', 'aac', '-b:a', f'{ab_kbps}k'])
    fps_s = f'{fps:.6f}'.rstrip('0').rstrip('.') if fps else '30'
    base = (['ffmpeg', '-y', '-hide_banner',
             '-f', 'rawvideo', '-pix_fmt', 'bgr24',
             '-s', f'{int(vw)}x{int(vh)}', '-r', fps_s,
             '-thread_queue_size', '512',
             '-i', 'pipe:0',
             '-ss', f'{audio_start:.6f}', '-i', video_path,
             '-map', '0:v', '-map', '1:a?',
             '-vf', vf,
             '-c:v', encoder] + vflags + ac + ['-shortest']
            + meta_args + tail)
    if progress_pipe:
        return base + ['-progress', 'pipe:1', '-nostats', output]
    return base + [output]


def _spawn_bgr_pipe_muxer(
    *,
    video_path: str,
    output: str,
    encoder: str,
    crf: int,
    vw: int,
    vh: int,
    fps: float,
    audio_start: float,
    audio_copy: bool,
    encode_options: Optional[dict],
    creation_time_utc: Optional[datetime],
    total_s: float,
    enc_sec_holder: list,
):
    """Start ffmpeg; caller writes BGR frames to *proc.stdin* then closes it.

    A daemon thread drains FFmpeg ``-progress`` lines into *enc_sec_holder[0]*
    (encoded timeline position in seconds) so the main loop can merge frame and
    encode progress without fighting the UI callback.

    Returns ``(proc, stderr_buf, stderr_thread, progress_thread)``.
    Join *stderr_thread* after *proc.wait()*.
    """
    import subprocess as _sp
    import threading as _th

    use_progress = total_s > 0
    cmd = _build_bgr_pipe_mux_cmd(
        video_path, output, encoder, crf, vw, vh, fps, audio_start, audio_copy,
        encode_options, creation_time_utc, use_progress,
    )
    stderr_buf: list[bytes] = []
    stdout_arg = _sp.PIPE if use_progress else _sp.DEVNULL
    proc = _popen(cmd, stdin=_sp.PIPE, stderr=_sp.PIPE, stdout=stdout_arg)

    def _drain_stderr():
        stderr_buf.extend(proc.stderr)

    t_err = _th.Thread(target=_drain_stderr, daemon=True)
    t_err.start()

    t_prog = None
    if use_progress:

        def _read_progress():
            for raw_line in proc.stdout:
                line = raw_line.decode(errors='replace').strip()
                if line.startswith('out_time_ms='):
                    try:
                        us = int(line.split('=', 1)[1])
                        enc_sec_holder[0] = min(total_s, us / 1_000_000.0)
                    except (ValueError, ZeroDivisionError):
                        pass

        t_prog = _th.Thread(target=_read_progress, daemon=True)
        t_prog.start()

    return proc, stderr_buf, t_err, t_prog


def _finalize_bgr_pipe_muxer(proc, stderr_buf, t_err, t_prog, output_path: str) -> None:
    """Close stdin, wait for ffmpeg, raise :class:`VideoMuxError` on failure."""
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    if t_prog is not None:
        t_prog.join()
    proc.wait()
    t_err.join()
    if proc.returncode != 0:
        err = b''.join(stderr_buf).decode(errors='replace')
        logger.error('FFmpeg pipe mux failed:\n%s', err)
        try:
            if output_path and os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass
        raise VideoMuxError(err[-600:])


def video_duration(path: str) -> float:
    """Return video duration in seconds via ffprobe."""
    try:
        r = _run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path], text=True)
        return float(r.stdout.strip())
    except Exception:
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        fc  = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        return fc / fps if fps else 0.0


# ── Render job ────────────────────────────────────────────────────────────────

class RenderJob:
    """Describes one output video to render."""
    def __init__(self, label: str, lap: Optional[Lap]):
        self.label     = label
        self.lap       = lap
        self.gpx_start = lap.elapsed_start if lap else None
        self.gpx_end   = lap.elapsed_end   if lap else None
        self.duration  = lap.duration      if lap else 0.0


# ── Render helpers ────────────────────────────────────────────────────────────

def _setup_delta_time(reference_lap, job, session):
    """Pre-compute all delta-time state needed before the frame loop.

    Returns a dict with keys:
        delta_fn, cur_lap_t, cur_lap_d, cur_lap_profiles,
        ref_dist_u, ref_channels, sectors
    Returns None for all keys when reference_lap is None.
    """
    if reference_lap is None:
        return dict(delta_fn=None, cur_lap_t=None, cur_lap_d=None,
                    cur_lap_profiles={}, ref_dist_u=None,
                    ref_channels={}, sectors=[])

    import numpy as np
    from delta_time import compute_lap_profile, make_delta_fn

    delta_fn = make_delta_fn(reference_lap, current_lap_duration=job.duration)

    if job.lap is not None:
        cur_lap_t, cur_lap_d = compute_lap_profile(job.lap)
        cur_lap_profiles     = {}
    else:
        cur_lap_t, cur_lap_d = None, None
        # Only build profiles for timed laps — outlap/inlap have meaningless
        # distance profiles and using them corrupts delta for the real laps.
        cur_lap_profiles     = {lap.lap_num: compute_lap_profile(lap)
                                 for lap in session.timed_laps}

    # Reference channel arrays (indexed by unique distance)
    ref_elapsed_full, ref_dist_full = compute_lap_profile(reference_lap)
    _, ref_u_idx = np.unique(ref_dist_full, return_index=True)
    ref_dist_u   = ref_dist_full[ref_u_idx]
    ref_pts      = reference_lap.points

    def _ref_arr(attr):
        return np.array([getattr(p, attr, 0.0) for p in ref_pts], dtype=float)[ref_u_idx]

    ref_channels = {
        't':            ref_elapsed_full[ref_u_idx],
        'speed':        _ref_arr('speed'),
        'gx':           _ref_arr('gforce_x'),
        'gy':           _ref_arr('gforce_y'),
        'lean':         _ref_arr('lean_angle'),
        'rpm':          _ref_arr('rpm'),
        'exhaust_temp': _ref_arr('exhaust_temp'),
        'alt':          _ref_arr('alt'),
    }

    # Sector splits
    sectors = []
    if job.lap is not None and cur_lap_t is not None and len(ref_dist_u) > 1:
        N_SECTORS  = _N_SECTORS
        total_dist = float(ref_dist_u[-1])
        if total_dist > 50.0:
            ref_elapsed_u = ref_elapsed_full[ref_u_idx]
            _, cur_u_idx  = np.unique(cur_lap_d, return_index=True)
            cur_dist_u    = cur_lap_d[cur_u_idx]
            cur_elapsed_u = cur_lap_t[cur_u_idx]
            max_cur_dist  = float(cur_dist_u[-1])
            boundaries    = [total_dist * i / N_SECTORS for i in range(1, N_SECTORS + 1)]

            for i, b in enumerate(boundaries):
                prev_b    = boundaries[i - 1] if i > 0 else 0.0
                ref_entry = float(np.interp(prev_b, ref_dist_u, ref_elapsed_u))
                ref_exit  = float(np.interp(b,      ref_dist_u, ref_elapsed_u))
                ref_sec_t = ref_exit - ref_entry

                if b <= max_cur_dist:
                    cur_entry        = float(np.interp(prev_b, cur_dist_u, cur_elapsed_u))
                    cur_exit         = float(np.interp(b,      cur_dist_u, cur_elapsed_u))
                    cur_sec_t        = cur_exit - cur_entry
                    delta            = cur_sec_t - ref_sec_t
                    done             = True
                    boundary_elapsed = cur_exit
                else:
                    cur_sec_t        = None
                    delta            = None
                    done             = False
                    boundary_elapsed = float('inf')

                sectors.append({
                    'num':              i + 1,
                    'ref_t':            ref_sec_t,
                    'cur_t':            cur_sec_t,
                    'delta':            delta,
                    'done':             done,
                    'boundary_elapsed': boundary_elapsed,
                })

    return dict(delta_fn=delta_fn, cur_lap_t=cur_lap_t, cur_lap_d=cur_lap_d,
                cur_lap_profiles=cur_lap_profiles, ref_dist_u=ref_dist_u,
                ref_channels=ref_channels, sectors=sectors)


def _build_session_meta(session, info_overrides: dict = None) -> dict:
    """Assemble the session-info dict passed to the info gauge."""
    from weather import fetch_weather
    meta = build_effective_session_meta(
        session,
        info_overrides=info_overrides,
        weather_fetcher=fetch_weather,
    )
    meta['info_source'] = getattr(session, 'source', '') or ''
    return meta


def _build_map_data(job, session, show_map):
    """Downsample GPS track and build a numpy array for fast nearest-point lookup.

    Returns (map_lats, map_lons, map_arr_np) where map_arr_np is shape (N, 2)
    or None when show_map is False / no GPS data is available.
    """
    if not show_map:
        return [], [], None

    lats, lons = build_complete_map_track(
        getattr(session, 'laps', []),
        max_points=MAP_MAX_POINTS,
        smooth_window=MAP_SMOOTH_WINDOW,
        timed_samples=MAP_TIMED_SAMPLES,
    )
    if not lats:
        pts = job.lap.points if job.lap else session.all_points
        lats, lons = build_map_track(pts, max_points=MAP_MAX_POINTS, smooth_window=MAP_SMOOTH_WINDOW)
    if not lats:
        return [], [], None

    import numpy as np
    arr = np.array(list(zip(lats, lons)), dtype=np.float64)
    return lats, lons, arr


# ── Main render function ───────────────────────────────────────────────────────

def render_lap(
    video_path:     str,
    out_path:       str,
    session:        Session,
    job:            RenderJob,
    sync_offset:    float,
    encoder:        str,
    crf:            int,
    n_workers:      int,
    show_map:       bool,
    show_telemetry: bool,
    padding:        float = 5.0,
    is_bike:        bool  = False,
    overlay_layout: Optional[dict] = None,   # normalized positions/sizes
    progress_cb:    Optional[Callable[[float, str], None]] = None,
    log_cb:         Optional[Callable[[str], None]] = None,
    reference_lap:      Optional[Lap] = None,   # lap to compare against for delta time
    info_overrides:     Optional[dict] = None, # manual session-info overrides {info_track, …}
    overlay_only:       bool  = False,         # render transparent overlay .mov (ProRes 4444)
    track_map_geometry: Optional[list] = None, # [{lat,lon}] OSM circuit outline, or None
    track_map_areas:    Optional[list] = None, # [{lats,lons}] OSM area polygons, or None
    encode_options:     Optional[dict] = None,
    container_choice:   str = 'match_source',
    force_vid_start_s:  Optional[float] = None,
    force_vid_end_s:    Optional[float] = None,
    cancel_event=None,
) -> None:
    """
    Render one video with telemetry overlay.

    overlay_layout: dict with 'map' and 'telemetry' keys, each containing
                    {visible, x, y, w, h} normalized 0..1.
                    Defaults to default_layout() if None.
    """
    layout = overlay_layout or default_layout()
    ff_proc = None
    ff_stderr: list = []
    ff_t_err = None
    ff_t_prog = None
    enc_sec = [0.0]

    def log(msg):
        if log_cb: log_cb(msg)
    def prog(pct, msg):
        if progress_cb: progress_cb(pct, msg)

    from exceptions import ExportCancelledError

    def _cancelled() -> bool:
        try:
            return cancel_event is not None and cancel_event.is_set()
        except Exception:
            return False

    def _safe_remove(p: str) -> None:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    # ── Delta time setup ───────────────────────────────────────────────────────
    dt_state = _setup_delta_time(reference_lap, job, session)
    _delta_fn         = dt_state['delta_fn']
    _cur_lap_t        = dt_state['cur_lap_t']
    _cur_lap_d        = dt_state['cur_lap_d']
    _cur_lap_profiles = dt_state['cur_lap_profiles']
    _ref_dist_u       = dt_state['ref_dist_u']
    _ref_channels     = dt_state['ref_channels']
    _sectors          = dt_state['sectors']

    cap   = cv2.VideoCapture(video_path)
    fps   = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    vw    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vh    = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # ── Frame range ────────────────────────────────────────────────────────────
    sync_offset = sync_offset or 0.0

    # Optional override: explicit video window in seconds (useful for
    # “video start → data end” exports where the session window does not start at 0s video time).
    if force_vid_start_s is not None or force_vid_end_s is not None:
        vid_start = float(force_vid_start_s or 0.0)
        if vid_start < 0:
            vid_start = 0.0
        # Default end: whole video; caller can clamp further.
        vid_end = float(force_vid_end_s) if force_vid_end_s is not None else (total / fps if fps else 0.0)
        if video_path and fps and total:
            vid_end = min(vid_end, total / fps)
        if vid_end < vid_start:
            vid_end = vid_start
        f_start = max(0, int(vid_start * fps))
        f_end   = min(total, int(math.ceil(vid_end * fps)))
        lap_t0 = 0.0
        lap_dur = 0.0
        padding = 0.0
    elif job.gpx_start is not None:
        vid_lap_start = sync_offset + job.gpx_start
        vid_lap_end   = sync_offset + job.gpx_end
        vid_start     = max(0.0, vid_lap_start - padding)
        vid_end       = min(total / fps, vid_lap_end + padding)
        f_start       = max(0, int(vid_start * fps))
        f_end         = min(total, int(math.ceil(vid_end * fps)))
        lap_t0        = job.gpx_start
        lap_dur       = job.duration
    else:
        f_start = 0; f_end = total
        vid_start = 0.0
        lap_t0 = 0.0; lap_dur = 0.0; padding = 0.0

    n_frames    = f_end - f_start
    audio_start = vid_start
    mux_dur_s = n_frames / fps if fps else 0.0

    vid_dur_s = total / fps if fps else 0.0
    log(f"  Encoder: {encoder}  |  Video: {vw}×{vh} @ {fps:.2f}fps  |  Duration: {vid_dur_s:.1f}s")
    if job.gpx_start is not None:
        log(f"  Lap duration: {job.duration:.2f}s  (session pos: {job.gpx_start:.1f}s → {job.gpx_end:.1f}s)")
        log(f"  Sync offset:  {sync_offset:.3f}s  →  video window: {vid_start:.1f}s → {vid_end:.1f}s  ({n_frames} frames)")

    if n_frames <= 0:
        cap.release()
        if job.gpx_start is not None:
            need_start = vid_lap_start - padding
            raise LapOutOfRangeError(
                f"Lap is {job.duration:.1f}s long (at session position {job.gpx_start:.1f}s–{job.gpx_end:.1f}s), "
                f"but with sync offset {sync_offset:.1f}s this maps to video time {need_start:.1f}s–{vid_lap_end+padding:.1f}s, "
                f"which is outside the video duration of {vid_dur_s:.1f}s. "
                f"Set the sync offset in the Data tab (scrub to where lap 1 starts, then click Mark)."
            )
        else:
            raise LapOutOfRangeError(
                f"Video appears empty or unreadable (0 frames). "
                f"Check that the video file is not corrupt: {video_path}"
            )

    # Output file: VID_<local Y-M-D>_<H-M-S>.<ext>; metadata uses same instant in UTC.
    vid_t0 = (float(f_start) / fps) if fps else 0.0
    sess_t0 = vid_t0 - sync_offset
    utc_first = absolute_time_at_elapsed(session, sess_t0) or _fallback_session_utc(session)
    export_dir = os.path.dirname(os.path.abspath(out_path))
    ext_vid = resolve_export_container_extension(
        video_path or '', overlay_only, (container_choice or 'match_source').strip() or 'match_source',
    )
    local_dt = utc_first.astimezone()
    out_path = _unique_export_path(os.path.join(
        export_dir,
        f"VID_{local_dt.strftime('%Y-%m-%d')}_{local_dt.strftime('%H-%M-%S')}{ext_vid}",
    ))

    if overlay_only:
        import subprocess as _sp, threading as _th, queue as _q_mod
        _ov_blank = np.zeros((vh, vw, 4), dtype=np.uint8)
        ct_tag = utc_first.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000000Z')
        _ov_proc  = _popen(
            ['ffmpeg', '-y', '-hide_banner',
             '-f', 'rawvideo', '-vcodec', 'rawvideo',
             '-s', f'{vw}x{vh}', '-r', str(fps),
             '-pix_fmt', 'rgba', '-i', 'pipe:0',
             '-vcodec', 'prores_ks', '-profile:v', '4444',
             '-pix_fmt', 'yuva444p10le',
             '-metadata', f'creation_time={ct_tag}',
             out_path],
            stdin=_sp.PIPE, stderr=_sp.PIPE,
        )
        _ov_stderr: list = []
        _ov_queue: _q_mod.Queue = _q_mod.Queue(maxsize=n_workers * 4)

        def _ov_write_loop():
            while True:
                item = _ov_queue.get()
                if item is None:
                    _ov_proc.stdin.close()
                    break
                _ov_proc.stdin.write(item)

        _th.Thread(target=lambda: _ov_stderr.extend(_ov_proc.stderr), daemon=True).start()
        _ov_writer = _th.Thread(target=_ov_write_loop, daemon=False)
        _ov_writer.start()
        writer  = None
        tmp_raw = None
    else:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_start)
        eo_mux = dict(encode_options) if isinstance(encode_options, dict) else {}
        eo_mux['_export_mux_vw'] = vw
        eo_mux['_export_mux_vh'] = vh
        eo_mux['_export_mux_fps'] = float(fps)
        enc_sec[0] = 0.0
        ff_proc, ff_stderr, ff_t_err, ff_t_prog = _spawn_bgr_pipe_muxer(
            video_path=video_path,
            output=out_path,
            encoder=encoder,
            crf=crf,
            vw=vw,
            vh=vh,
            fps=float(fps),
            audio_start=audio_start,
            audio_copy=True,
            encode_options=eo_mux,
            creation_time_utc=utc_first,
            total_s=mux_dur_s,
            enc_sec_holder=enc_sec,
        )
        writer = None
        tmp_raw = None

    # ── Session metadata + max speed + map data ───────────────────────────────
    _session_meta = _build_session_meta(session, info_overrides)

    speed_pts = job.lap.points if job.lap else session.all_points
    if speed_pts:
        raw_max   = max(p.speed for p in speed_pts)
        max_speed = max(50.0, math.ceil(raw_max * 1.10 / 50) * 50)
    else:
        max_speed = 300.0

    map_lats, map_lons, _map_arr_np = _build_map_data(job, session, show_map)

    # OSM circuit outline geometry (constant across all frames)
    _track_map_lats:  list = []
    _track_map_lons:  list = []
    _track_map_areas: list = track_map_areas or []
    if track_map_geometry:
        _track_map_lats = [g['lat'] for g in track_map_geometry]
        _track_map_lons = [g['lon'] for g in track_map_geometry]

    # Reference lap GPS (downsampled) — used by the Zoomed map style
    _ref_map_lats: list = []
    _ref_map_lons: list = []
    _ref_lap_duration: float = 0.0
    if reference_lap and reference_lap.points:
        _ref_map_lats, _ref_map_lons = build_map_track(
            reference_lap.points, max_points=MAP_MAX_POINTS, smooth_window=MAP_REF_SMOOTH_WINDOW
        )
        _ref_lap_duration = reference_lap.duration

    # ── Lap-scoreboard pre-computation (shared with preview path) ────────────
    _lap_info_lookup = build_lap_info_lookup(session.laps)

    # ── History buffers (deque gives O(1) eviction, no manual trimming) ───────
    HISTORY_MAX   = int(10.0 * fps)
    history_buf   = deque(maxlen=HISTORY_MAX)
    _ref_hist_buf = deque(maxlen=HISTORY_MAX)

    chunk     = max(4, n_workers * 2)
    frame_idx = f_start
    processed = 0

    pool = Pool(n_workers) if n_workers > 1 else None
    cancelled = False
    try:
        while frame_idx < f_end:
            if _cancelled():
                cancelled = True
                raise ExportCancelledError('cancelled')
            chunk_frames, chunk_meta = [], []

            for _ in range(chunk):
                if frame_idx >= f_end:
                    break
                if overlay_only:
                    frm = _ov_blank
                else:
                    ret, frm = cap.read()
                    if not ret:
                        break

                vid_t     = frame_idx / fps
                sess_t    = vid_t - sync_offset
                raw_lap_t = sess_t - lap_t0

                pt = session.interpolate_at(sess_t)
                if pt:
                    _li = lap_info_fields_for_sample(
                        session.laps, float(sess_t), int(pt.lap), _lap_info_lookup
                    )
                    # For per-lap export:
                    # - keep timer within lap bounds while the lap is active
                    # - hold final lap time for a short post-finish window
                    # - then resume live lap_elapsed so other timeline context
                    #   stays coherent after the hold
                    # For full-session export: always use live pt.lap_elapsed.
                    if job.gpx_start is not None:
                        lap_t_display = lap_time_display_value(
                            raw_lap_t=raw_lap_t,
                            lap_dur=lap_dur,
                            live_lap_elapsed=pt.lap_elapsed,
                            hold_s=LAP_TIME_HOLD_AFTER_FINISH_S,
                        )
                    else:
                        lap_t_display = pt.lap_elapsed

                    # ── Delta time ─────────────────────────────────────────────
                    delta_val = 0.0
                    cur_d     = 0.0
                    if _delta_fn is not None:
                        try:
                            if _cur_lap_t is not None:
                                cur_d = float(np.interp(
                                    pt.lap_elapsed, _cur_lap_t, _cur_lap_d))
                                if not math.isfinite(cur_d):
                                    cur_d = 0.0
                            else:
                                profile = _cur_lap_profiles.get(pt.lap)
                                if profile is not None:
                                    cur_d = float(np.interp(
                                        pt.lap_elapsed, profile[0], profile[1]))
                                    if not math.isfinite(cur_d):
                                        cur_d = 0.0
                            delta_val = _delta_fn(pt.lap_elapsed, cur_d)
                        except Exception:
                            delta_val = 0.0

                    # ── Reference history ──────────────────────────────────────
                    if _ref_dist_u is not None:
                        try:
                            d_ref = min(cur_d, float(_ref_dist_u[-1]))
                            ref_t = float(np.interp(d_ref, _ref_dist_u, _ref_channels['t']))
                            ref_gx = float(np.interp(d_ref, _ref_dist_u, _ref_channels['gx']))
                            ref_gy = float(np.interp(d_ref, _ref_dist_u, _ref_channels['gy']))
                            _ref_hist_buf.append({
                                'speed':        float(np.interp(d_ref, _ref_dist_u, _ref_channels['speed'])),
                                'gx':           ref_gx,
                                'gy':           ref_gy,
                                'g_total':      math.hypot(ref_gx, ref_gy),
                                'lean':         float(np.interp(d_ref, _ref_dist_u, _ref_channels['lean'])),
                                'rpm':          float(np.interp(d_ref, _ref_dist_u, _ref_channels['rpm'])),
                                'exhaust_temp': float(np.interp(d_ref, _ref_dist_u, _ref_channels['exhaust_temp'])),
                                't':            ref_t,
                                'delta_time':   0.0,
                                'alt':          float(np.interp(d_ref, _ref_dist_u, _ref_channels.get('alt', [0.0]*len(_ref_dist_u)))),
                            })
                        except Exception:
                            pass

                    row = build_history_row(
                        p=pt,
                        lap_t=lap_t_display,
                        delta_time=delta_val,
                        lap_info=_li,
                    )
                    history_buf.append(row)

                # ── Map nearest-point (vectorised numpy, one call per frame) ───
                cur_map_idx = 0
                if pt and _map_arr_np is not None:
                    q   = np.array([pt.lat, pt.lon])
                    d2  = _map_arr_np - q
                    cur_map_idx = int(np.argmin((d2 * d2).sum(axis=1)))

                chunk_frames.append(frm)
                chunk_meta.append((list(history_buf), list(_ref_hist_buf), cur_map_idx))
                frame_idx += 1

            if not chunk_frames:
                break

            args_list = [
                (b'' if overlay_only else frm.tobytes(),
                 (vh, vw, 4) if overlay_only else frm.shape,
                 cur_map_idx,
                 map_lats, map_lons,
                 hist, ref_hist, lap_dur,
                 vw, vh,
                 show_map, show_telemetry,
                 is_bike,
                 layout,
                 max_speed,
                 _sectors,
                 _session_meta,
                 _ref_map_lats,
                 _ref_map_lons,
                 _ref_lap_duration,
                 overlay_only,
                 _track_map_lats,
                 _track_map_lons,
                 _track_map_areas)
                for frm, (hist, ref_hist, cur_map_idx) in zip(chunk_frames, chunk_meta)
            ]

            results = pool.map(render_frame_worker, args_list) if pool else \
                      [render_frame_worker(a) for a in args_list]

            if overlay_only:
                for raw in results:
                    _ov_queue.put(raw)   # writer thread feeds ffmpeg; never blocks main loop
                    processed += 1
            else:
                for raw in results:
                    ff_proc.stdin.write(raw)
                    processed += 1

            fr = processed / max(1, n_frames)
            en = (enc_sec[0] / mux_dur_s) if mux_dur_s > 0 else 0.0
            en = min(1.0, max(0.0, en))
            pct = min(99.5, 100.0 * (0.80 * fr + 0.20 * en))
            prog(pct, f"Frame {processed}/{n_frames}")
    finally:
        if pool:
            pool.terminate()
            pool.join()
        if cancelled:
            try:
                if overlay_only:
                    try:
                        _ov_queue.put(None)
                    except Exception:
                        pass
                    try:
                        _ov_proc.kill()
                    except Exception:
                        pass
                elif ff_proc is not None:
                    try:
                        ff_proc.kill()
                    except Exception:
                        pass
            except Exception:
                pass

    if overlay_only:
        cap.release()
        _ov_queue.put(None)   # signal writer thread to close stdin and exit
        _ov_writer.join()
        _ov_proc.wait()
        if _ov_proc.returncode != 0:
            err = b''.join(_ov_stderr).decode(errors='replace')
            logger.error('FFmpeg ProRes export failed:\n%s', err)
            _safe_remove(out_path)
            raise VideoMuxError(err[-600:])
        if _cancelled():
            _safe_remove(out_path)
            raise ExportCancelledError('cancelled')
        prog(100, "")
        log(f"  ✓ Saved: {out_path}")
    else:
        cap.release()
        log("  Finishing encode (mux audio)…")
        prog(96.0, "Finishing encode…")
        try:
            if _cancelled():
                cancelled = True
                raise ExportCancelledError('cancelled')
            _finalize_bgr_pipe_muxer(ff_proc, ff_stderr, ff_t_err, ff_t_prog, out_path)
            if _cancelled():
                _safe_remove(out_path)
                raise ExportCancelledError('cancelled')
            prog(100, "")
            log(f"  ✓ Saved: {out_path}")
        except Exception as e:
            if isinstance(e, ExportCancelledError) or _cancelled():
                cancelled = True
                _safe_remove(out_path)
                raise ExportCancelledError('cancelled')
            log(f"  ✗ Encode/mux failed: {e}")
            _safe_remove(out_path)
            raise
