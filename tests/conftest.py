import sys
import os
from pathlib import Path

import pytest

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from opencv_ffmpeg_env import apply_ffmpeg_capture_thread_limit

apply_ffmpeg_capture_thread_limit()

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def racebox_car_csv_path():
    return str(FIXTURES_DIR / "racebox_car.csv")


@pytest.fixture
def racebox_bike_csv_path():
    return str(FIXTURES_DIR / "racebox_bike.csv")


@pytest.fixture
def aim_csv_path():
    return str(FIXTURES_DIR / "aim_session.csv")


@pytest.fixture
def not_telemetry_csv_path():
    return str(FIXTURES_DIR / "not_a_telemetry_file.csv")


@pytest.fixture
def racebox_car_session(racebox_car_csv_path):
    from racebox_data import load_csv
    return load_csv(racebox_car_csv_path)


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect app data root to tmp_path so tests stay isolated."""
    import openlap_paths

    monkeypatch.setattr(openlap_paths, "app_data_dir", lambda: tmp_path)
    import app_config

    nonexistent = tmp_path / "nonexistent.json"
    monkeypatch.setattr(app_config, "_OLD_CONFIG_V2", nonexistent)
    monkeypatch.setattr(app_config, "_OLD_CONFIG_V1", nonexistent)
    return tmp_path
