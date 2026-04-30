"""Unit tests for export_codec helpers (container + segment sizing)."""
from pathlib import Path

import pytest

from export_codec import (
    estimate_effective_avg_video_kbps,
    estimate_output_size_bytes,
    estimate_segment_lengths_s,
    normalized_container_extension,
    resolve_export_container_extension,
)


def test_normalized_container_extension_variants():
    assert normalized_container_extension('/a/b/foo.MP4') == '.mp4'
    assert normalized_container_extension('/v/x.mkv') == '.mkv'
    assert normalized_container_extension('') == '.mp4'


def test_resolve_export_overlay_only_mov():
    assert resolve_export_container_extension('/x/foo.mp4', True, 'match_source') == '.mov'


def test_resolve_match_source_uses_input_ext():
    assert resolve_export_container_extension('/x/rec.MKV', False, 'match_source') == '.mkv'


def test_estimate_segment_fastest_scope_matches_runner():
    class Pt:
        def __init__(self, e):
            self.elapsed = float(e)

    class Lap:
        def __init__(self, lap_num, dur, out=False, inn=False):
            self.lap_num = int(lap_num)
            self.duration = float(dur)
            self.is_outlap = out
            self.is_inlap = inn
            self.points = [Pt(0), Pt(dur)]
            self.elapsed_start = 0.0
            self.elapsed_end = dur

    class Sess:
        laps = [
            Lap(1, 100.0),
            Lap(2, 90.0),
            Lap(3, 5.0, out=True),
        ]

    segs = estimate_segment_lengths_s(
        Sess(),
        item={},
        scope='fastest',
        padding=1.0,
        clip_start_s=0.0,
        clip_end_s=0.0,
        video_duration_s=500.0,
    )
    assert len(segs) == 1
    assert abs(segs[0] - (90.0 + 2.0)) < 1e-6


def test_estimate_effective_avg_video_kbps_bitrate_mode():
    kb = estimate_effective_avg_video_kbps(
        'libx264', 'vbr', 18, 8000, 0, 1920, 1080, 30.0,
    )
    assert kb == 8000.0


def test_estimate_output_size_bytes():
    b = estimate_output_size_bytes(10.0, 8000.0, 128.0, False)
    assert b > 0

