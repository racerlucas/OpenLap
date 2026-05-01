"""
NTSC drop-frame SMPTE timecode ↔ linear frame index (Heidelberger / SMPTE).

Used for 30000/1001 (29.97) and 60000/1001 (59.94) when the timecode track uses
``;`` as the frame-field separator. Non-drop (``:``) clips keep integer ``fbase``
linear counting in ``video_clip_order``.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Exact rationals for comparisons (avoid float drift).
_NUM_2997, _DEN_2997 = 30000, 1001
_NUM_5994, _DEN_5994 = 60000, 1001


def is_avg_frame_rate_2997(num: int, den: int) -> bool:
    return den > 0 and num * _DEN_2997 == _NUM_2997 * den


def is_avg_frame_rate_5994(num: int, den: int) -> bool:
    return den > 0 and num * _DEN_5994 == _NUM_5994 * den


def ntsc_df_framerate_float(num: int, den: int) -> Optional[float]:
    if is_avg_frame_rate_2997(num, den):
        return _NUM_2997 / _DEN_2997
    if is_avg_frame_rate_5994(num, den):
        return _NUM_5994 / _DEN_5994
    return None


def is_ntsc_df_float_fps(fps: float) -> bool:
    if fps <= 0:
        return False
    return abs(fps - _NUM_2997 / _DEN_2997) < 0.2 or abs(fps - _NUM_5994 / _DEN_5994) < 0.4


def drop_frame_tc_to_linear_index(
    h: int, m: int, s: int, f: int, framerate: float,
) -> int:
    """Map drop-frame SMPTE (with ``;`` field) to a monotonic integer frame index.

    See e.g. David Heidelberger, *Timecode and Frame Rates: An Unofficial Guide*.
    ``framerate`` should be ``30000/1001`` or ``60000/1001`` (float).
    """
    time_base = int(round(framerate + 1e-9))
    drop_frames = int(round(framerate * (2.0 / 30.0) + 1e-9))
    hour_frames = time_base * 60 * 60
    minute_frames = time_base * 60
    total_minutes = 60 * h + m
    return (
        hour_frames * h
        + minute_frames * m
        + time_base * s
        + f
        - drop_frames * (total_minutes - (total_minutes // 10))
    )


def parse_avg_frame_rate_rational(s: Optional[str]) -> Optional[Tuple[int, int]]:
    if not s or str(s).strip() in ('0/0', 'N/A', 'nan'):
        return None
    parts = str(s).strip().split('/')
    if len(parts) != 2:
        return None
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if b == 0:
        return None
    return a, b
