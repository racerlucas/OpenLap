"""Consolidated auto-sync unit tests (was split across several one-off files)."""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from auto_sync import (
    _decode_window_for_baseline,
    _first_timed_lap_session_interval,
    _load_session,
    _load_telemetry_session_grid,
    _metadata_baseline_offset,
    _probe_video_creation_time_utc,
    _slice_telemetry_session,
    _snap_offset_to_frame,
    run_auto_sync,
)


def test_run_auto_sync_metadata_only_no_decode(monkeypatch):
    """use_motion=False returns snapped baseline without loading telemetry."""
    sess = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    vid = datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        'auto_sync._metadata_baseline_offset',
        lambda _c, _v: (-5.0, sess, vid),
    )
    monkeypatch.setattr(
        'auto_sync._probe_video',
        lambda _p: {'fps': 30.0, 'width': 1920, 'height': 1080, 'duration': 60.0},
    )
    calls = []

    def _cb(*a, **k):
        calls.append(k.get('mode'))

    off, conf = run_auto_sync(
        'dummy.csv', ['dummy.mp4'], 'RaceBox', use_motion=False, progress_cb=_cb,
    )
    assert conf == 0.0
    assert off == pytest.approx(-5.0)
    assert calls == ['metadata-only']


def test_metadata_baseline_offset_sign_matches_manual_convention(monkeypatch):
    """
    Convention: session_time = video_time - sync_offset

    If the video starts AFTER the telemetry session by +219s,
    then at video_time=0, session_time=+219s, so sync_offset must be -219s.
    """
    sess_start = datetime(2026, 4, 25, 9, 11, 40, tzinfo=timezone.utc)
    vid_start = datetime(2026, 4, 25, 9, 15, 19, tzinfo=timezone.utc)  # +219s later

    monkeypatch.setattr("auto_sync._session_start_utc", lambda _p: sess_start)
    monkeypatch.setattr("auto_sync._probe_video_creation_time_utc", lambda _vp: vid_start)

    off, got_sess, got_vid = _metadata_baseline_offset("x.vbo", ["v.mp4"])
    assert got_sess == sess_start
    assert got_vid == vid_start
    assert off == -219.0


def test_snap_offset_to_frame_rounds_to_nearest_frame():
    fps = 30.0
    assert _snap_offset_to_frame(1.001, fps) == (30 / 30)  # 1.0
    assert _snap_offset_to_frame(1.016, fps) == (30 / 30)  # 1.0
    assert _snap_offset_to_frame(1.017, fps) == (31 / 30)  # 1.033333...


def test_snap_offset_to_frame_handles_invalid_fps():
    assert _snap_offset_to_frame(1.234, 0.0) == 1.234
    assert _snap_offset_to_frame(1.234, -25.0) == 1.234


def test_decode_window_clips_and_has_duration():
    s, d = _decode_window_for_baseline(center_offset_s=100.0, window_s=8.0, margin_s=0.5, file_duration_s=300.0)
    assert s == pytest.approx(91.5)
    assert d == pytest.approx(17.0)

    s, d = _decode_window_for_baseline(center_offset_s=2.0, window_s=8.0, margin_s=0.5, file_duration_s=300.0)
    assert s == pytest.approx(0.0)
    assert d > 0

    s, d = _decode_window_for_baseline(center_offset_s=500.0, window_s=8.0, margin_s=0.5, file_duration_s=300.0)
    assert s <= 300.0
    assert d >= 0

    s, d = _decode_window_for_baseline(center_offset_s=-1000.0, window_s=8.0, margin_s=0.5, file_duration_s=300.0)
    assert s == pytest.approx(0.0)
    assert d == pytest.approx(17.0)


FIXTURE_VBO = Path(__file__).resolve().parent / "fixtures" / "sample.vbo"


def test_load_session_supports_vbox_vbo_source():
    sess = _load_session(str(FIXTURE_VBO), "VBOX")
    assert sess is not None
    assert len(getattr(sess, "all_points", []) or []) > 100


def test_first_timed_lap_interval_on_fixture_vbo():
    sess = _load_session(str(FIXTURE_VBO), "VBOX")
    iv = _first_timed_lap_session_interval(sess)
    assert iv is not None
    L0, L1 = iv
    assert L0 >= 0
    assert L1 > L0


def test_slice_telemetry_session_respects_bounds():
    g, st, _ = _load_telemetry_session_grid(str(FIXTURE_VBO), "VBOX", 20.0)
    sl, t0 = _slice_telemetry_session(g, st, 10.0, 50.0, min_samples=100)
    assert len(sl) >= 100
    i0 = int(np.searchsorted(st, 10.0, side='left'))
    assert np.isclose(t0, float(st[i0]))


def test_probe_video_timecode_preferred_over_creation_even_when_far_apart(monkeypatch):
    import json as _json
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        c = ' '.join(cmd)
        if '-select_streams' in cmd and 'v:0' in cmd and 'creation_time' in c:
            payload = {
                'format': {'tags': {'creation_time': '2026-04-25T09:15:19.000000Z'}},
                'streams': [{'tags': {}}],
            }
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if '-show_streams' in cmd and 'stream_tags=timecode' in c:
            payload = {'streams': [{'tags': {'timecode': '17:15:19:00'}}]}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if 'format_tags=timecode' in c and '-show_streams' not in c:
            payload = {'format': {'tags': {}}}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        raise AssertionError(f'unexpected cmd: {c}')

    monkeypatch.setattr('auto_sync.subprocess.run', fake_run)
    monkeypatch.setattr('auto_sync._probe_video', lambda _p: {'fps': 30.0})

    dt = _probe_video_creation_time_utc('dummy.mp4')
    assert dt.isoformat() == '2026-04-25T17:15:19+00:00'


def test_probe_video_timecode_applies_frame_subseconds(monkeypatch):
    import json as _json
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        c = ' '.join(cmd)
        if '-select_streams' in cmd and 'v:0' in cmd and 'creation_time' in c:
            payload = {
                'format': {'tags': {'creation_time': '2026-04-25T09:15:19.000000Z'}},
                'streams': [{'tags': {}}],
            }
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if '-show_streams' in cmd and 'stream_tags=timecode' in c:
            payload = {'streams': [{'tags': {'timecode': '09:15:19:05'}}]}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if 'format_tags=timecode' in c and '-show_streams' not in c:
            payload = {'format': {'tags': {}}}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        raise AssertionError(f'unexpected cmd: {c}')

    monkeypatch.setattr('auto_sync.subprocess.run', fake_run)
    monkeypatch.setattr('auto_sync._probe_video', lambda _p: {'fps': 30.0})

    dt = _probe_video_creation_time_utc('dummy.mp4')
    assert dt == datetime(2026, 4, 25, 9, 15, 19, 166667, tzinfo=timezone.utc)


def test_probe_video_timecode_drop_frame_separator_uses_wall_clock(monkeypatch):
    """Semicolon before frame (07:56:13;17) must parse; SMPTE can trail creation_time by ~80s."""
    import json as _json
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        c = ' '.join(cmd)
        if '-select_streams' in cmd and 'v:0' in cmd and 'creation_time' in c:
            payload = {
                'format': {'tags': {'creation_time': '2026-04-25T07:54:54.000000Z'}},
                'streams': [{'tags': {}}],
            }
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if '-show_streams' in cmd and 'stream_tags=timecode' in c:
            payload = {'streams': [{'tags': {'timecode': '07:56:13;17'}}]}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if 'format_tags=timecode' in c and '-show_streams' not in c:
            payload = {'format': {'tags': {}}}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        raise AssertionError(f'unexpected cmd: {c}')

    monkeypatch.setattr('auto_sync.subprocess.run', fake_run)
    monkeypatch.setattr('auto_sync._probe_video', lambda _p: {'fps': 30.0})

    dt = _probe_video_creation_time_utc('dummy.mp4')
    assert dt == datetime(2026, 4, 25, 7, 56, 13, 566667, tzinfo=timezone.utc)


def test_probe_video_timecode_still_wins_when_creation_differs_by_minutes(monkeypatch):
    import json as _json
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        c = ' '.join(cmd)
        if '-select_streams' in cmd and 'v:0' in cmd and 'creation_time' in c:
            payload = {
                'format': {'tags': {'creation_time': '2026-04-25T09:15:19.000000Z'}},
                'streams': [{'tags': {}}],
            }
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if '-show_streams' in cmd and 'stream_tags=timecode' in c:
            payload = {'streams': [{'tags': {'timecode': '10:00:00:00'}}]}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        if 'format_tags=timecode' in c and '-show_streams' not in c:
            payload = {'format': {'tags': {}}}
            return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr='')
        raise AssertionError(f'unexpected cmd: {c}')

    monkeypatch.setattr('auto_sync.subprocess.run', fake_run)
    monkeypatch.setattr('auto_sync._probe_video', lambda _p: {'fps': 30.0})

    dt = _probe_video_creation_time_utc('dummy.mp4')
    assert dt.isoformat() == '2026-04-25T10:00:00+00:00'
