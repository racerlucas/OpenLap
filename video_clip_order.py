"""
Order multi-segment camera clips using SMPTE timecode + frame-accurate continuity.

For each file we read the first embedded ``timecode`` / QuickTime timecode tag (same
discovery as ``auto_sync._probe_video_first_timecode_str``), parse
``HH:MM:SS:FF`` (non-drop / linear grid) or ``HH:MM:SS;FF`` (NTSC drop-frame).

**Non-drop (``:``):** first frame index is linear ``((h*60+m)*60+s)*fbase + f`` with
``fbase = round(fps)`` (typical NDF at 29.97 uses a 30-frame field).

**Drop-frame (``;``) at 30000/1001 or 60000/1001:** first frame index uses the
standard Heidelberger / SMPTE mapping (see ``smpte_ntsc_df``). ``avg_frame_rate``
from ffprobe selects 29.97 vs 59.94 when unambiguous.

``last`` index is **derived**: ``first_linear + (frame_count - 1)`` where
``frame_count`` comes from ``nb_frames`` when present, else ``round(duration * fps)``.

If any clip lacks a parsable TC or fps, we fall back to ``_sort_video_paths_by_metadata_only``.
Incompatible timecode spaces (mixed ``:``/``;``, mixed ``avg_frame_rate``, or mixed
``fbase``/DF mode) also fall back. Continuity mismatches produce warnings but the
TC-based sort order is still returned when ordering remained TC-based.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from smpte_ntsc_df import (
    drop_frame_tc_to_linear_index,
    is_ntsc_df_float_fps,
    ntsc_df_framerate_float,
    parse_avg_frame_rate_rational,
)

logger = logging.getLogger(__name__)


def parse_smpte_timecode(tc: str) -> Optional[Tuple[int, int, int, int, bool]]:
    """Parse SMPTE token → ``(h, m, s, f, is_drop_frame_separator)``."""
    if not tc:
        return None
    txt = str(tc).strip()
    m = re.match(r'^(\d{1,2}):(\d{1,2}):(\d{1,2})[:;](\d{1,2})$', txt)
    if not m:
        return None
    try:
        h, mm, s, f = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        is_df = ';' in txt
        return h, mm, s, f, is_df
    except ValueError:
        return None


def _linear_tc_frames(h: int, mm: int, s: int, f: int, fbase: int) -> int:
    fbase = max(1, int(fbase))
    return ((h * 60 + mm) * 60 + s) * fbase + f


def _linear_to_smpte(total_f: int, fbase: int) -> Tuple[int, int, int, int]:
    fbase = max(1, int(fbase))
    ff = total_f % fbase
    sec = total_f // fbase
    s = sec % 60
    sec //= 60
    mm = sec % 60
    h = sec // 60
    return h, mm, s, ff


def _frame_count(info: dict) -> int:
    nb = int(info.get('nb_frames') or 0)
    if nb > 0:
        return nb
    fps = float(info.get('fps') or 0)
    dur = float(info.get('duration') or 0)
    if fps > 0 and dur > 0:
        return max(1, int(round(dur * fps)))
    return 1


@dataclass
class ClipTcSpan:
    path: str
    fps: float
    fbase: int
    first_tc_raw: str
    first_linear: int
    last_linear: int
    is_drop_parsed: bool
    use_df_math: bool
    avg_num: int = 0
    avg_den: int = 1


def probe_clip_tc_span(path: str) -> Optional[ClipTcSpan]:
    from auto_sync import _probe_video, _probe_video_first_timecode_str

    tc_raw = _probe_video_first_timecode_str(path)
    if not tc_raw:
        return None
    parsed = parse_smpte_timecode(tc_raw)
    if not parsed:
        return None
    h, mm, s, ff, is_df = parsed
    info = _probe_video(path)
    fps = float(info.get('fps') or 0)
    if fps <= 0:
        return None

    ar = parse_avg_frame_rate_rational(info.get('avg_frame_rate'))
    avg_num, avg_den = (ar if ar else (0, 1))

    use_df_math = False
    df_rate: Optional[float] = None

    if is_df:
        df_rate = ntsc_df_framerate_float(avg_num, avg_den) if ar else None
        if df_rate is None and is_ntsc_df_float_fps(fps):
            d2997 = abs(fps - 30000.0 / 1001.0)
            d5994 = abs(fps - 60000.0 / 1001.0)
            df_rate = 30000.0 / 1001.0 if d2997 <= d5994 else 60000.0 / 1001.0
        if df_rate is not None:
            use_df_math = True
            fbase = int(round(df_rate))
            if ff >= fbase:
                return None
            first_linear = drop_frame_tc_to_linear_index(h, mm, s, ff, df_rate)
        else:
            fbase = max(1, int(round(fps)))
            if ff >= fbase:
                return None
            first_linear = _linear_tc_frames(h, mm, s, ff, fbase)
    else:
        fbase = max(1, int(round(fps)))
        if ff >= fbase:
            return None
        first_linear = _linear_tc_frames(h, mm, s, ff, fbase)

    n = _frame_count(info)
    last_linear = first_linear + max(0, n - 1)
    return ClipTcSpan(
        path=str(path),
        fps=fps,
        fbase=fbase,
        first_tc_raw=tc_raw,
        first_linear=first_linear,
        last_linear=last_linear,
        is_drop_parsed=is_df,
        use_df_math=use_df_math,
        avg_num=avg_num,
        avg_den=avg_den,
    )


def _smpte_word(linear: int, fbase: int) -> str:
    h, mm, s, f = _linear_to_smpte(linear, fbase)
    return f'{h:02d}:{mm:02d}:{s:02d}:{f:02d}'


def _format_span_boundary(sp: ClipTcSpan, linear: int) -> str:
    if sp.use_df_math:
        return f'#{linear}'
    return _smpte_word(linear, sp.fbase)


def sort_video_paths_with_timecode(paths: List[str]) -> Tuple[List[str], List[str]]:
    """Return ``(ordered_paths, warnings)``.

    Prefers SMPTE + frame continuity; falls back to metadata-only ordering from
    ``session_scanner`` when TC / fps grid cannot be established for **all** clips.
    """
    warnings: List[str] = []
    if len(paths) <= 1:
        return list(paths), warnings

    spans: List[ClipTcSpan] = []
    for p in paths:
        sp = probe_clip_tc_span(p)
        if sp is None:
            from session_scanner import _sort_video_paths_by_metadata_only

            warnings.append(
                '部分文件缺少可解析的 SMPTE timecode（或 fps），已改用录制时间/元数据排序。'
            )
            logger.info('video_clip_order: fallback metadata sort (missing TC) for %s', p)
            return _sort_video_paths_by_metadata_only(paths), warnings
        spans.append(sp)

    df_sep_flags = [s.is_drop_parsed for s in spans]
    if any(df_sep_flags) and not all(df_sep_flags):
        from session_scanner import _sort_video_paths_by_metadata_only

        warnings.append(
            '多段视频混用了 drop-frame (;) 与非 drop-frame (:) timecode，无法统一换算；已改用元数据排序。'
        )
        return _sort_video_paths_by_metadata_only(paths), warnings

    tc_spaces = {(s.is_drop_parsed, s.use_df_math, s.fbase) for s in spans}
    if len(tc_spaces) > 1:
        from session_scanner import _sort_video_paths_by_metadata_only

        warnings.append(
            f'多段时间码换算制式不一致 {sorted(tc_spaces)!r}，已改用元数据排序。'
        )
        return _sort_video_paths_by_metadata_only(paths), warnings

    nz_avg = {(s.avg_num, s.avg_den) for s in spans if s.avg_den > 0}
    if len(nz_avg) > 1:
        from session_scanner import _sort_video_paths_by_metadata_only

        warnings.append(
            f'多段视频 avg_frame_rate 不一致 {sorted(nz_avg)!r}，已改用元数据排序。'
        )
        return _sort_video_paths_by_metadata_only(paths), warnings

    bases = {s.fbase for s in spans}
    if len(bases) > 1:
        from session_scanner import _sort_video_paths_by_metadata_only

        warnings.append(
            f'多段视频 round(fps) 网格不一致 {sorted(bases)!r}，已改用元数据排序。'
        )
        return _sort_video_paths_by_metadata_only(paths), warnings

    if any(s.is_drop_parsed and not s.use_df_math for s in spans):
        warnings.append(
            '检测到 drop-frame (;) 但帧率不是标准 NTSC（29.97/59.94）；'
            '已按线性网格近似，连续性检查可能与设备有偏差。'
        )

    ordered = sorted(spans, key=lambda s: s.first_linear)
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        exp = a.last_linear + 1
        if exp != b.first_linear:
            warnings.append(
                '时间码帧连续性存疑: '
                f'{os.path.basename(a.path)} 推算末帧≈{_format_span_boundary(a, a.last_linear)} '
                f'→ 下一文件首帧 {b.first_tc_raw!r} ({os.path.basename(b.path)}) '
                f'(期望 idx={exp}, 实际 {b.first_linear})'
            )
    return [s.path for s in ordered], warnings
