import json
import pytest
from dataclasses import asdict

from app_config import (
    AppConfig, OverlayLayout,
    overlay_from_dict, _from_dict,
)


# ── Default values ─────────────────────────────────────────────────────────────

def test_default_telemetry_path():
    assert AppConfig().telemetry_path == ""


def test_default_map_style():
    map_gauge = next((g for g in AppConfig().overlay.gauges if g['channel'] == 'map'), None)
    assert map_gauge is not None
    assert map_gauge['style'] == 'Circuit'


def test_default_theme():
    assert AppConfig().overlay.theme == 'Dark'


def test_default_has_gauges():
    assert len(AppConfig().overlay.gauges) > 0


def test_default_export_scope_and_timing():
    c = AppConfig()
    assert c.export_scope == 'full'
    assert c.export_padding == pytest.approx(5.0)
    assert c.export_overlay_only is False
    assert c.export_lap_range_start == 1
    assert c.export_lap_range_end is None


# ── Save / load round-trip ─────────────────────────────────────────────────────

def test_save_and_load_round_trip(tmp_config_dir):
    cfg = AppConfig()
    cfg.telemetry_path = '/some/path'
    cfg.save()

    loaded = AppConfig.load()
    assert loaded.telemetry_path == '/some/path'


def test_overlay_theme_preserved(tmp_config_dir):
    cfg = AppConfig()
    cfg.overlay.theme = 'Light'
    cfg.save()

    loaded = AppConfig.load()
    assert loaded.overlay.theme == 'Light'


def test_offsets_preserved(tmp_config_dir):
    cfg = AppConfig()
    cfg.offsets['/some/file.csv'] = 3.14
    cfg.save()

    loaded = AppConfig.load()
    assert loaded.offsets['/some/file.csv'] == pytest.approx(3.14)


def test_gauges_preserved_after_round_trip(tmp_config_dir):
    cfg = AppConfig()
    cfg.overlay.gauges[0]['channel'] = 'rpm'
    cfg.save()

    loaded = AppConfig.load()
    assert loaded.overlay.gauges[0]['channel'] == 'rpm'


def test_presets_preserved(tmp_config_dir):
    cfg = AppConfig()
    cfg.presets['MyPreset'] = asdict(cfg.overlay)
    cfg.save()

    loaded = AppConfig.load()
    assert 'MyPreset' in loaded.presets


def test_export_timing_prefs_round_trip(tmp_config_dir):
    cfg = AppConfig()
    cfg.export_scope = 'lap_range'
    cfg.export_padding = 7.5
    cfg.export_clip_start_s = 1.25
    cfg.export_clip_end_s = 99.0
    cfg.export_overlay_only = True
    cfg.export_lap_range_start = 2
    cfg.export_lap_range_end = 8
    cfg.save()

    loaded = AppConfig.load()
    assert loaded.export_scope == 'lap_range'
    assert loaded.export_padding == pytest.approx(7.5)
    assert loaded.export_clip_start_s == pytest.approx(1.25)
    assert loaded.export_clip_end_s == pytest.approx(99.0)
    assert loaded.export_overlay_only is True
    assert loaded.export_lap_range_start == 2
    assert loaded.export_lap_range_end == 8


# ── Missing / corrupt file ─────────────────────────────────────────────────────

def test_load_missing_file_returns_defaults(tmp_config_dir):
    # No config file written — should return defaults without error
    loaded = AppConfig.load()
    assert loaded.telemetry_path == ""


def test_load_corrupt_file_returns_defaults(tmp_config_dir):
    from openlap_paths import config_file

    config_file().write_text("not valid json")
    loaded = AppConfig.load()
    assert loaded.telemetry_path == ""


# ── overlay_from_dict ──────────────────────────────────────────────────────────

def test_overlay_from_dict_empty():
    layout = overlay_from_dict({})
    assert isinstance(layout, OverlayLayout)


def test_overlay_from_dict_round_trip():
    original   = OverlayLayout()
    serialized = asdict(original)
    restored   = overlay_from_dict(serialized)
    assert restored.theme == original.theme
    assert len(restored.gauges) == len(original.gauges)
    orig_map    = next((g for g in original.gauges  if g['channel'] == 'map'), None)
    restored_map = next((g for g in restored.gauges if g['channel'] == 'map'), None)
    assert orig_map is not None and restored_map is not None
    assert restored_map['style'] == orig_map['style']


def test_overlay_from_dict_missing_keys():
    layout   = overlay_from_dict({'theme': 'Carbon'})
    assert layout.theme == 'Carbon'
    map_gauge = next((g for g in layout.gauges if g['channel'] == 'map'), None)
    assert map_gauge is not None
    assert map_gauge['style'] == 'Circuit'  # default


# ── _from_dict ─────────────────────────────────────────────────────────────────

def test_from_dict_empty():
    cfg = _from_dict({})
    assert cfg.telemetry_path == ""
    assert isinstance(cfg.overlay, OverlayLayout)


def test_from_dict_auto_sync_workers_defaults_and_clamps():
    assert _from_dict({}).auto_sync_workers == 2
    assert _from_dict({'auto_sync_workers': 99}).auto_sync_workers == 8
    assert _from_dict({'auto_sync_workers': 0}).auto_sync_workers == 1


def test_default_auto_sync_workers():
    assert AppConfig().auto_sync_workers == 2


def test_default_auto_sync_use_motion_false():
    assert AppConfig().auto_sync_use_motion is False
