"""Tests for telemetry_algorithms map helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from telemetry_algorithms import (
    build_complete_map_track,
    lap_display_num_for_point,
    _fit_centerline_from_laps,
    _resample_points,
)


@dataclass
class Pt:
    lat: float
    lon: float


@dataclass
class FakeLap:
    points: List[Pt]
    is_outlap: bool = False
    is_inlap: bool = False


@dataclass
class FakeDP:
    elapsed: float
    lap: int = 1


@dataclass
class FakeLapTimed:
    lap_num: int
    points: List[FakeDP]
    is_outlap: bool = False
    is_inlap: bool = False


def test_fit_centerline_median_two_parallel_lines():
    """Two offset straight 'laps' → median should sit between them."""
    n = 50
    lap_a = [Pt(0.0, i * 0.0001) for i in range(n)]
    lap_b = [Pt(0.0002, i * 0.0001) for i in range(n)]
    ra, ro = _resample_points(lap_a, 40)
    rb, ro2 = _resample_points(lap_b, 40)
    la, lo = _fit_centerline_from_laps([(ra, ro), (rb, ro2)])
    mid_lat = np.median([0.0, 0.0002])
    assert np.allclose(float(np.mean(la)), mid_lat, rtol=0.05, atol=2e-5)


def test_build_complete_map_track_uses_median_not_raw_stack():
    """Several timed laps with lateral offset → single smooth centerline-ish polyline."""
    laps = []
    for j in range(3):
        pts = [Pt(0.0001 * j + 0.0, i * 0.0001) for i in range(30)]
        laps.append(FakeLap(points=pts, is_outlap=False, is_inlap=False))
    lats, lons = build_complete_map_track(laps, max_points=200, smooth_window=1, timed_samples=60)
    assert len(lats) >= 10
    assert len(lats) == len(lons)
    # Should not explode to thousands of points from naive concat of all raw GPS
    assert len(lats) <= 250


def test_lap_display_num_for_point_outlap_is_zero():
    """Outlap row uses display lap 0 (OUT LAP) even if device lap_num is 1."""
    out = FakeLapTimed(
        lap_num=1,
        points=[FakeDP(0.0, 1), FakeDP(10.0, 1)],
        is_outlap=True,
    )
    timed = FakeLapTimed(
        lap_num=2,
        points=[FakeDP(10.01, 2), FakeDP(80.0, 2)],
        is_outlap=False,
    )
    laps = [out, timed]
    assert lap_display_num_for_point(laps, 5.0, raw_lap_num=1) == 0
    assert lap_display_num_for_point(laps, 50.0, raw_lap_num=2) == 2
