"""smpte_ntsc_df — NTSC drop-frame timecode ↔ linear index."""
from __future__ import annotations

import pytest

from smpte_ntsc_df import (
    drop_frame_tc_to_linear_index,
    is_avg_frame_rate_2997,
    is_avg_frame_rate_5994,
    is_ntsc_df_float_fps,
    ntsc_df_framerate_float,
    parse_avg_frame_rate_rational,
)


def test_parse_avg_frame_rate():
    assert parse_avg_frame_rate_rational('30000/1001') == (30000, 1001)
    assert parse_avg_frame_rate_rational('60000/1001') == (60000, 1001)
    assert parse_avg_frame_rate_rational('0/0') is None
    assert parse_avg_frame_rate_rational(None) is None


def test_rational_flags():
    assert is_avg_frame_rate_2997(30000, 1001)
    assert not is_avg_frame_rate_2997(60000, 1001)
    assert is_avg_frame_rate_5994(60000, 1001)
    assert ntsc_df_framerate_float(30000, 1001) == pytest.approx(30000 / 1001)
    assert ntsc_df_framerate_float(60000, 1001) == pytest.approx(60000 / 1001)


def test_is_ntsc_df_float_fps():
    assert is_ntsc_df_float_fps(30000 / 1001)
    assert is_ntsc_df_float_fps(60000 / 1001)
    assert not is_ntsc_df_float_fps(25.0)


@pytest.mark.parametrize(
    'h,m,s,f,expected',
    [
        (0, 0, 0, 0, 0),
        (0, 1, 0, 0, 1798),  # 1 min DF: 1800 - 2 dropped at non-10th minute
        (0, 10, 0, 0, 17982),  # 10 min: 18000*10 - 2*(10-1)
    ],
)
def test_drop_frame_2997_index(h, m, s, f, expected):
    r = 30000.0 / 1001.0
    assert drop_frame_tc_to_linear_index(h, m, s, f, r) == expected


def test_drop_frame_5994_one_minute():
    r = 60000.0 / 1001.0
    # 60 * 60 * 1 - 4 * (1 - 0) = 3600 - 4
    assert drop_frame_tc_to_linear_index(0, 1, 0, 0, r) == 3596
