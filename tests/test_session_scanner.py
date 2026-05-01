from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from session_scanner import (
    VideoFile, VideoGroup,
    group_videos, _make_group,
    _read_csv_start_time, _csv_source,
    match_sessions, MatchedSession,
    MAX_GAP, MATCH_WINDOW,
    sort_video_paths_by_start_time,
)
from pathlib import Path


def _utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _make_video(path: str, creation_time: datetime, duration: float) -> VideoFile:
    return VideoFile(path=path, creation_time=creation_time, duration=duration)


# ── group_videos ───────────────────────────────────────────────────────────────

def test_group_videos_empty():
    assert group_videos([]) == []


def test_group_videos_single():
    v = _make_video('/a.mp4', _utc(2024, 1, 1), 60.0)
    groups = group_videos([v])
    assert len(groups) == 1
    assert groups[0].total_dur == pytest.approx(60.0)


def test_group_videos_consecutive_grouped():
    t0 = _utc(2024, 1, 1)
    v1 = _make_video('/a.mp4', t0, 60.0)
    # Second video starts 10 seconds after first ends — within MAX_GAP
    v2 = _make_video('/b.mp4', t0 + timedelta(seconds=70), 60.0)
    groups = group_videos([v1, v2])
    assert len(groups) == 1
    assert groups[0].total_dur == pytest.approx(120.0)


def test_group_videos_gap_splits():
    t0 = _utc(2024, 1, 1)
    v1 = _make_video('/a.mp4', t0, 60.0)
    # Gap of 300s — well beyond MAX_GAP (120s)
    v2 = _make_video('/b.mp4', t0 + timedelta(seconds=360), 60.0)
    groups = group_videos([v1, v2])
    assert len(groups) == 2


def test_group_videos_total_duration():
    t0 = _utc(2024, 1, 1)
    v1 = _make_video('/a.mp4', t0, 30.0)
    v2 = _make_video('/b.mp4', t0 + timedelta(seconds=35), 45.0)
    groups = group_videos([v1, v2])
    assert groups[0].total_dur == pytest.approx(75.0)


def test_group_videos_start_time():
    t0 = _utc(2024, 1, 1, 10, 0, 0)
    v1 = _make_video('/a.mp4', t0, 60.0)
    v2 = _make_video('/b.mp4', t0 + timedelta(seconds=65), 60.0)
    groups = group_videos([v1, v2])
    assert groups[0].start_time == t0


# ── _read_csv_start_time ───────────────────────────────────────────────────────

def test_read_csv_start_time_racebox(racebox_car_csv_path):
    dt = _read_csv_start_time(racebox_car_csv_path)
    assert dt is not None
    assert dt.tzinfo is not None  # must be timezone-aware
    assert dt.year == 2024
    assert dt.month == 6
    assert dt.day == 15


def test_read_csv_start_time_aim(aim_csv_path):
    dt = _read_csv_start_time(aim_csv_path)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2024


def test_read_csv_start_time_vbo_has_subsecond():
    p = Path(__file__).resolve().parent / "fixtures" / "sample.vbo"
    # fixture may not include "File created on ..." header in all environments;
    # build a minimal VBO inline to test sub-second parsing.
    # (date from header + HHMMSS.SS from first data row)
    from tempfile import NamedTemporaryFile
    import os
    with NamedTemporaryFile('w', delete=False, suffix='.vbo', encoding='utf-8') as f:
        f.write("\n".join([
            "File created on 25/04/2026 at 17:11:40",
            "",
            "[header]",
            "time",
            "",
            "[column names]",
            "sats time lat long",
            "",
            "[data]",
            "016 091140.12 +0000.00000 +0000.00000",
            "",
        ]))
        tmp = f.name
    try:
        dt = _read_csv_start_time(tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    assert dt is not None
    assert dt.tzinfo is not None
    # first data row time ends with .12 → should preserve milliseconds
    assert dt.microsecond != 0


def test_read_csv_start_time_vbo_reconciles_header_local_vs_data_utc(tmp_path):
    # Header uses local time (UTC+8) 17:11:40, data time channel uses 09:11:40.28 (UTC).
    # We treat [data] time as UTC and pick the correct UTC date nearest header->UTC.
    p = tmp_path / "x.vbo"
    p.write_text(
        "\n".join([
            "File created on 25/04/2026 at 17:11:40",
            "",
            "[header]",
            "time",
            "",
            "[column names]",
            "sats time lat long",
            "",
            "[data]",
            "016 091140.28 +0000.00000 +0000.00000",
            "",
        ]),
        encoding="utf-8",
    )
    dt = _read_csv_start_time(str(p))
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.isoformat().startswith("2026-04-25T09:11:40.280")


# ── _csv_source ────────────────────────────────────────────────────────────────

def test_csv_source_aim(aim_csv_path):
    assert _csv_source(aim_csv_path) == 'AIM'


def test_csv_source_racebox(racebox_car_csv_path):
    assert _csv_source(racebox_car_csv_path) == 'RaceBox'


# ── match_sessions ─────────────────────────────────────────────────────────────

def test_match_sessions_within_window(racebox_car_csv_path):
    csv_start = _read_csv_start_time(racebox_car_csv_path)
    # Video starts 30 seconds after CSV — within MATCH_WINDOW
    video_time = csv_start + timedelta(seconds=30)
    v = _make_video('/video.mp4', video_time, 600.0)
    group = _make_group([v])

    results = match_sessions([racebox_car_csv_path], [group])
    assert len(results) == 1
    assert results[0].matched is True
    assert results[0].time_delta == pytest.approx(30.0, abs=1.0)


def test_match_sessions_outside_window(racebox_car_csv_path):
    csv_start = _read_csv_start_time(racebox_car_csv_path)
    # Video is 4000 seconds away — beyond MATCH_WINDOW (3600s)
    video_time = csv_start + timedelta(seconds=4000)
    v = _make_video('/video.mp4', video_time, 600.0)
    group = _make_group([v])

    results = match_sessions([racebox_car_csv_path], [group])
    assert results[0].matched is False


def test_match_sessions_no_videos(racebox_car_csv_path):
    results = match_sessions([racebox_car_csv_path], [])
    assert len(results) == 1
    assert results[0].matched is False
    assert results[0].video_group is None


def test_match_sessions_overlap_prefers_larger_intersection(tmp_path):
    """When two video groups overlap the CSV window, pick the one with longer overlap."""
    csv = tmp_path / 'session.csv'
    csv.write_text(
        '\n'.join([
            'Data Source,RaceBox Mini',
            'Date UTC,2024-01-01T12:00:00Z',
            'Record,Time,Latitude,Longitude,Altitude,Speed,GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ',
            '1,2024-01-01T12:00:00Z,0,0,0,0,0,0,1,0,0,0,0',
            '2,2024-01-01T12:05:00Z,0,0,0,0,0,0,1,0,0,0,0',
        ]),
        encoding='utf-8',
    )
    # Group A: only first minute overlaps [12:00, 12:05]
    t0 = _utc(2024, 1, 1, 11, 59, 0)
    g_a = _make_group([_make_video(str(tmp_path / 'a.mp4'), t0, 120.0)])
    # Group B: spans entire session window
    t1 = _utc(2024, 1, 1, 12, 0, 0)
    g_b = _make_group([_make_video(str(tmp_path / 'b.mp4'), t1, 600.0)])

    results = match_sessions([str(csv)], [g_a, g_b])
    assert len(results) == 1
    assert results[0].matched is True
    assert results[0].video_group is g_b


def test_match_sessions_sorts_by_csv_start(racebox_car_csv_path, racebox_bike_csv_path):
    # Bike CSV starts at 11:00, car CSV at 10:00 — result should be car first
    results = match_sessions([racebox_bike_csv_path, racebox_car_csv_path], [])
    starts = [r.csv_start for r in results if r.csv_start]
    assert starts == sorted(starts)


def test_sort_video_paths_by_start_time_orders_by_probe(monkeypatch, tmp_path):
    a = str((tmp_path / 'a.mp4').resolve())
    b = str((tmp_path / 'b.mp4').resolve())
    Path(a).write_bytes(b'0')
    Path(b).write_bytes(b'0')

    def fake_probe(path: str):
        if path == a:
            return _utc(2024, 6, 2, 12, 0, 0), 10.0
        return _utc(2024, 6, 2, 11, 0, 0), 10.0

    monkeypatch.setattr('session_scanner._ffprobe_creation_time', fake_probe)
    assert sort_video_paths_by_start_time([a, b]) == [b, a]


def test_sort_video_paths_single_unchanged(tmp_path):
    p = str((tmp_path / 'only.mp4').resolve())
    Path(p).write_bytes(b'0')
    assert sort_video_paths_by_start_time([p]) == [p]
