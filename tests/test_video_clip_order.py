"""video_clip_order — SMPTE ordering + frame continuity warnings."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_sort_by_timecode_continuous(tmp_path, monkeypatch):
    a = str((tmp_path / 'a.mp4').resolve())
    b = str((tmp_path / 'b.mp4').resolve())
    Path(a).write_bytes(b'0')
    Path(b).write_bytes(b'0')

    def fake_tc(p: str):
        if p == a:
            return '10:00:00:00'
        return '10:00:00:10'

    def fake_probe(p: str):
        return {'fps': 30.0, 'duration': 10.0 / 30.0, 'nb_frames': 10, 'width': 1920, 'height': 1080}

    monkeypatch.setattr('auto_sync._probe_video_first_timecode_str', fake_tc)
    monkeypatch.setattr('auto_sync._probe_video', fake_probe)

    from video_clip_order import sort_video_paths_with_timecode

    ordered, warn = sort_video_paths_with_timecode([b, a])
    assert ordered == [a, b]
    assert not any('连续性存疑' in w for w in warn)


def test_sort_gap_warns_but_orders_by_tc(tmp_path, monkeypatch):
    a = str((tmp_path / 'a.mp4').resolve())
    b = str((tmp_path / 'b.mp4').resolve())
    Path(a).write_bytes(b'0')
    Path(b).write_bytes(b'0')

    def fake_tc(p: str):
        if p == a:
            return '10:00:00:00'
        return '10:00:01:00'

    def fake_probe(p: str):
        return {'fps': 30.0, 'duration': 5.0 / 30.0, 'nb_frames': 5, 'width': 1280, 'height': 720}

    monkeypatch.setattr('auto_sync._probe_video_first_timecode_str', fake_tc)
    monkeypatch.setattr('auto_sync._probe_video', fake_probe)

    from video_clip_order import sort_video_paths_with_timecode

    ordered, warn = sort_video_paths_with_timecode([b, a])
    assert ordered == [a, b]
    assert any('连续性存疑' in w for w in warn)


def test_sort_drop_frame_2997_continuous(tmp_path, monkeypatch):
    """NTSC drop-frame (;) + 30000/1001: linear index matches Heidelberger."""
    a = str((tmp_path / 'a.mp4').resolve())
    b = str((tmp_path / 'b.mp4').resolve())
    Path(a).write_bytes(b'0')
    Path(b).write_bytes(b'0')

    def fake_tc(p: str):
        if p == a:
            return '00:00:00;00'
        return '00:00:00;10'

    def fake_probe(_p: str):
        return {
            'fps': 30000.0 / 1001.0,
            'duration': 10.0 / (30000.0 / 1001.0),
            'nb_frames': 10,
            'width': 1920,
            'height': 1080,
            'avg_frame_rate': '30000/1001',
        }

    monkeypatch.setattr('auto_sync._probe_video_first_timecode_str', fake_tc)
    monkeypatch.setattr('auto_sync._probe_video', fake_probe)

    from video_clip_order import sort_video_paths_with_timecode

    ordered, warn = sort_video_paths_with_timecode([b, a])
    assert ordered == [a, b]
    assert not any('连续性存疑' in w for w in warn)
    assert not any('近似' in w for w in warn)


def test_fallback_when_timecode_missing(tmp_path, monkeypatch):
    a = str((tmp_path / 'a.mp4').resolve())
    b = str((tmp_path / 'b.mp4').resolve())
    Path(a).write_bytes(b'0')
    Path(b).write_bytes(b'0')

    monkeypatch.setattr('auto_sync._probe_video_first_timecode_str', lambda _p: None)

    from video_clip_order import sort_video_paths_with_timecode

    with patch('session_scanner._sort_video_paths_by_metadata_only', side_effect=lambda ps: list(reversed(ps))):
        ordered, warn = sort_video_paths_with_timecode([a, b])
    assert ordered == [b, a]
    assert any('元数据' in w or 'timecode' in w for w in warn)
