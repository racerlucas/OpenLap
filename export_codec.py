"""
export_codec.py — Export sizing + container helpers
===================================================

Used by WebviewAPI.estimate_export_size() to provide Shutter-like UX hints
without running a full encode.

This module intentionally aims for **stable, conservative heuristics** rather
than perfect parity with FFmpeg; real file sizes vary with content complexity.
"""

from __future__ import annotations

import os
from typing import List, Optional


def normalized_container_extension(video_path: Optional[str]) -> str:
    """Return a normalized container extension like '.mp4' or '.mkv'."""
    ext = os.path.splitext(str(video_path or ''))[1].lower().strip()
    if ext in ('.mp4', '.m4v'):
        return '.mp4'
    if ext in ('.mov',):
        return '.mov'
    if ext in ('.mkv',):
        return '.mkv'
    if ext in ('.avi',):
        return '.avi'
    return ext if ext.startswith('.') and len(ext) <= 6 else '.mp4'


def resolve_export_container_extension(
    source_video_path: Optional[str],
    overlay_only: bool,
    container_choice: str,
) -> str:
    """Resolve export container extension from user choice.

    Current UX: only 'match_source' (auto) is exposed, but we keep the API
    flexible for future MP4/MKV/MOV choices.
    """
    if overlay_only:
        return '.mov'
    cc = (container_choice or 'match_source').strip().lower()
    if cc in ('match_source', 'auto'):
        return normalized_container_extension(source_video_path)
    if cc in ('mp4', '.mp4'):
        return '.mp4'
    if cc in ('mkv', '.mkv'):
        return '.mkv'
    if cc in ('mov', '.mov'):
        return '.mov'
    return normalized_container_extension(source_video_path)


def estimate_segment_lengths_s(
    sess,
    *,
    item: dict,
    scope: str,
    padding: float,
    clip_start_s: float,
    clip_end_s: float,
    video_duration_s: float,
) -> List[float]:
    """Return one or more output segment durations for a single export request."""
    pad = max(0.0, float(padding or 0.0))
    sc = str(scope or 'full')

    # Full session encodes the whole source video duration.
    if sc == 'full':
        return [max(0.0, float(video_duration_s or 0.0))]

    # Clip scope uses explicit seconds.
    if sc == 'clip':
        a = max(0.0, float(clip_start_s or 0.0))
        b = max(a, float(clip_end_s or 0.0))
        if video_duration_s and video_duration_s > 0:
            b = min(b, float(video_duration_s))
        return [max(0.0, b - a)]

    # Lap-based scopes derive from session laps (elapsed timeline).
    laps = list(getattr(sess, 'laps', []) or [])
    if not laps:
        return []

    def _lap_dur(lap) -> float:
        try:
            d = float(getattr(lap, 'duration', 0.0) or 0.0)
        except Exception:
            d = 0.0
        return max(0.0, d)

    if sc == 'selected_lap':
        lap_idx = int(item.get('lap_idx', 0) or 0)
        if lap_idx < 0 or lap_idx >= len(laps):
            return []
        return [max(0.0, _lap_dur(laps[lap_idx]) + 2.0 * pad)]

    if sc in ('fastest', 'fastest_lap'):
        timed = [l for l in laps if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)]
        if not timed:
            return []
        best = min(timed, key=_lap_dur)
        return [max(0.0, _lap_dur(best) + 2.0 * pad)]

    if sc == 'all_laps':
        # Export runner exports timed laps only for all_laps.
        timed = [l for l in laps if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)]
        return [max(0.0, _lap_dur(l) + 2.0 * pad) for l in timed if _lap_dur(l) > 0]

    if sc == 'lap_range':
        ordered = sorted(laps, key=lambda l: int(getattr(l, 'lap_num', 0) or 0))
        start_num = item.get('lap_range_start')
        end_num = item.get('lap_range_end')
        if start_num is None:
            start_num = getattr(ordered[0], 'lap_num', 0)
        if end_num is None:
            end_num = getattr(ordered[-1], 'lap_num', 0)
        start_num, end_num = int(start_num), int(end_num)
        included = [l for l in ordered if start_num <= int(getattr(l, 'lap_num', 0) or 0) <= end_num]
        if not included:
            return []
        try:
            t0 = float(getattr(included[0], 'elapsed_start', 0.0) or 0.0)
            t1 = float(getattr(included[-1], 'elapsed_end', 0.0) or 0.0)
            return [max(0.0, (t1 - t0) + 2.0 * pad)]
        except Exception:
            # Fallback: sum durations
            return [max(0.0, sum(_lap_dur(l) for l in included) + 2.0 * pad)]

    # Unknown scope: conservative fallback.
    return [max(0.0, float(video_duration_s or 0.0))]


def _cq_bpp(crf: int) -> float:
    """Very rough bpp heuristic for H.264/H.265 CQ modes."""
    c = int(crf or 18)
    # Around CRF 18: ~0.10 bpp; higher CRF reduces size.
    bpp = 0.10 - (c - 18) * 0.003
    return max(0.03, min(0.16, bpp))


def estimate_effective_avg_video_kbps(
    encoder: str,
    rate_mode: str,
    crf: int,
    video_bitrate_kbps: int,
    video_max_bitrate_kbps: int,
    w: int,
    h: int,
    fps: float,
) -> float:
    """Estimate average video bitrate in kbps."""
    vb = int(video_bitrate_kbps or 0)
    vmax = int(video_max_bitrate_kbps or 0)
    rm = str(rate_mode or 'cq').lower()

    if vb > 0:
        return float(min(vb, vmax) if vmax > 0 else vb)

    # CQ heuristic.
    px_rate = max(1.0, float(max(1, int(w))) * float(max(1, int(h))) * float(max(1e-3, float(fps or 0.0))))
    kbps = (px_rate * _cq_bpp(crf)) / 1000.0

    # Mild encoder adjustment: H.265 tends to be smaller at same quality.
    enc = str(encoder or '').lower()
    if '265' in enc or 'hevc' in enc:
        kbps *= 0.70

    kbps = max(300.0, min(80_000.0, kbps))
    if vmax > 0:
        kbps = min(kbps, float(vmax))
    return kbps


def estimate_output_size_bytes(duration_s: float, video_kbps: float, audio_kbps: float, two_pass: bool) -> int:
    """Estimate output size in bytes from bitrate and duration."""
    d = max(0.0, float(duration_s or 0.0))
    v = max(0.0, float(video_kbps or 0.0))
    a = max(0.0, float(audio_kbps or 0.0))
    total_kbps = v + a
    base = d * total_kbps * 1000.0 / 8.0
    # 2-pass doesn't change final size much; keep a small overhead for metadata/variability.
    fudge = 1.02 if two_pass else 1.05
    return int(base * fudge)

