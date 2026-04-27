from vbox_data import load_vbo


def test_load_vbo_uses_column_names_lap_channel(tmp_path):
    vbo = tmp_path / "lap_in_column_names.vbo"
    vbo.write_text(
        "\n".join([
            "File created on 26/04/2026 at 02:45:39",
            "",
            "[header]",
            "satellites",
            "time",
            "latitude",
            "longitude",
            "velocity kmh",
            "",
            "[column names]",
            "sats time lat long velocity lap",
            "",
            "[data]",
            "016 125402.00 +01379.10000 -06801.10000 050.0 001",
            "016 125403.00 +01379.10010 -06801.10010 051.0 001",
            "016 125404.00 +01379.10020 -06801.10020 052.0 002",
            "016 125405.00 +01379.10030 -06801.10030 053.0 002",
            "016 125406.00 +01379.10040 -06801.10040 054.0 003",
        ]),
        encoding="utf-8",
    )

    s = load_vbo(str(vbo))

    assert len(s.laps) == 3
    assert [l.lap_num for l in s.laps] == [1, 2, 3]


def test_load_vbo_speed_kmh_hint_from_header(tmp_path):
    vbo = tmp_path / "speed_kmh_hint.vbo"
    vbo.write_text(
        "\n".join([
            "File created on 26/04/2026 at 02:45:39",
            "",
            "[header]",
            "satellites",
            "time",
            "latitude",
            "longitude",
            "velocity kmh",
            "",
            "[column names]",
            "sats time lat long velocity lap",
            "",
            "[data]",
            "016 125402.00 +01379.10000 -06801.10000 080.0 001",
            "016 125403.00 +01379.10010 -06801.10010 081.0 001",
        ]),
        encoding="utf-8",
    )

    s = load_vbo(str(vbo))
    assert max(p.speed for p in s.all_points) == 81.0
