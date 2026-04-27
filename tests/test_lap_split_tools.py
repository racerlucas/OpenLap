import json
import shutil
from pathlib import Path

from lap_split_tools import auto_split_file_with_track_json

FIXTURE_VBO = Path(__file__).resolve().parent / "fixtures" / "sample.vbo"


def test_auto_split_vbo_preserves_file_created_on_line(tmp_path):
    track_json = tmp_path / "track.json"
    track_json.write_text(json.dumps({
        "metadata": {"name": "test"},
        "label_info": {
            "direction": "CCW",
            "line_lonlat": [[113.0, 23.0], [113.0001, 23.0001]],
        },
    }), encoding="utf-8")

    vbo = tmp_path / "session.vbo"
    shutil.copyfile(FIXTURE_VBO, vbo)
    original_first = vbo.read_text(encoding="latin-1").splitlines()[0]

    auto_split_file_with_track_json(str(track_json), str(vbo))

    first_line = vbo.read_text(encoding="latin-1").splitlines()[0]
    assert first_line == original_first
