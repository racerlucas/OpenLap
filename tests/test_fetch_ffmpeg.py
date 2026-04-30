"""Tests for tools/fetch_ffmpeg.py URL resolution (no real download)."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

_fp = Path(__file__).resolve().parents[1] / 'tools' / 'fetch_ffmpeg.py'
_spec = importlib.util.spec_from_file_location('fetch_ffmpeg_mod', _fp)
ff = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ff)


def test_latest_btb_picks_win64_gpl_zip():
    fake = {
        'assets': [
            {'name': 'noise.zip', 'browser_download_url': 'https://x/noise.zip'},
            {
                'name': 'ffmpeg-n99.0-win64-lgpl-shared.zip',
                'browser_download_url': 'https://x/lgpl.zip',
            },
            {
                'name': 'ffmpeg-n99.0-win64-gpl-20260101.zip',
                'browser_download_url': 'https://x/good.zip',
            },
            {
                'name': 'ffmpeg-n99.0-win64-gpl-shared.zip',
                'browser_download_url': 'https://x/shared.zip',
            },
        ],
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(fake).encode('utf-8')

    with patch('urllib.request.urlopen', return_value=_Resp()):
        url = ff.latest_btb_win64_gpl_zip_url()
    assert url == 'https://x/good.zip'


def test_latest_prefers_essentials_name_when_present():
    fake = {
        'assets': [
            {
                'name': 'ffmpeg-n1-win64-gpl.zip',
                'browser_download_url': 'https://x/full.zip',
            },
            {
                'name': 'ffmpeg-n1-win64-gpl-essentials.zip',
                'browser_download_url': 'https://x/ess.zip',
            },
        ],
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(fake).encode('utf-8')

    with patch('urllib.request.urlopen', return_value=_Resp()):
        url = ff.latest_btb_win64_gpl_zip_url()
    assert url == 'https://x/ess.zip'


def test_resolve_url_env_overrides(monkeypatch):
    monkeypatch.setenv('OPENLAP_FFMPEG_URL', 'https://example.com/custom.zip')
    assert ff.resolve_url('stable') == 'https://example.com/custom.zip'
    monkeypatch.delenv('OPENLAP_FFMPEG_URL', raising=False)
    assert ff.resolve_url('stable') == ff.STABLE_URL
