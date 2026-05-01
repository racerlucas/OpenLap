"""openlap_paths — app data root resolution."""
from __future__ import annotations

import sys
from pathlib import Path


def test_app_data_dir_openlap_data_dir_override(monkeypatch, tmp_path):
    import openlap_paths

    monkeypatch.setenv("OPENLAP_DATA_DIR", str(tmp_path))
    assert openlap_paths.app_data_dir() == tmp_path.resolve()


def test_config_and_scan_under_app_data(monkeypatch, tmp_path):
    import openlap_paths

    monkeypatch.setenv("OPENLAP_DATA_DIR", str(tmp_path))
    assert openlap_paths.config_file() == tmp_path.resolve() / "config.json"
    assert openlap_paths.scan_cache_file() == tmp_path.resolve() / "scan_cache.json"
    assert openlap_paths.tracks_dir() == tmp_path.resolve() / "tracks"
    assert openlap_paths.library_ffmpeg_dir() == tmp_path.resolve() / "Library" / "ffmpeg"
    assert openlap_paths.racebox_auth_file() == tmp_path.resolve() / "racebox_auth.json"
    assert openlap_paths.playwright_browsers_dir() == tmp_path.resolve() / "ms-playwright"


def test_racebox_auth_migrates_from_legacy_app_data(monkeypatch, tmp_path):
    import openlap_paths

    monkeypatch.setattr(openlap_paths, "_racebox_auth_migrated", False)
    appdata = tmp_path / "appdata"
    (appdata / "OpenLap").mkdir(parents=True)
    (appdata / "OpenLap" / "racebox_auth.json").write_text('{"cookies": []}', encoding="utf-8")
    dest_root = tmp_path / "portable"
    monkeypatch.setenv("OPENLAP_DATA_DIR", str(dest_root))
    monkeypatch.setenv("APPDATA", str(appdata))
    p = openlap_paths.racebox_auth_file()
    assert p == dest_root.resolve() / "racebox_auth.json"
    assert p.read_text(encoding="utf-8") == '{"cookies": []}'


def test_unfrozen_app_data_is_repo_dot_openlap(monkeypatch):
    import openlap_paths

    monkeypatch.delenv("OPENLAP_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(openlap_paths, "_legacy_migrated", True)
    monkeypatch.setattr(openlap_paths, "_maybe_migrate_from_home_dot_openlap", lambda _d: None)
    p = openlap_paths.app_data_dir()
    assert p.name == ".openlap"
    assert p.parent == Path(openlap_paths.__file__).resolve().parent


def test_migrate_legacy_home_dot_openlap(monkeypatch, tmp_path):
    import openlap_paths

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    legacy = fake_home / ".openlap"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"telemetry_path": "x"}')
    (legacy / "scan_cache.json").write_text('{"sessions": []}')
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(openlap_paths, "_REPO_ROOT", proj)
    monkeypatch.setattr(openlap_paths, "_legacy_migrated", False)
    monkeypatch.delenv("OPENLAP_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    dest = openlap_paths.app_data_dir()
    assert dest == proj / ".openlap"
    assert (dest / "config.json").read_text(encoding="utf-8") == '{"telemetry_path": "x"}'
    assert (dest / "scan_cache.json").read_text(encoding="utf-8") == '{"sessions": []}'
