from pathlib import Path

from vbox_data import load_vbo


FIXTURE_VBO = Path(__file__).resolve().parent / "fixtures" / "sample.vbo"


def test_load_vbo_parses_fixture_session():
    s = load_vbo(str(FIXTURE_VBO))
    assert len(s.laps) >= 1
    assert len(s.all_points) > 100


def test_load_vbo_speed_kmh_hint_from_header():
    s = load_vbo(str(FIXTURE_VBO))
    assert max(p.speed for p in s.all_points) > 1.0


def test_load_vbo_parses_longacc_latacc_as_gforces():
    s = load_vbo(str(FIXTURE_VBO))
    assert any(abs(p.gforce_x) > 0 for p in s.all_points)
    assert any(abs(p.gforce_y) > 0 for p in s.all_points)
