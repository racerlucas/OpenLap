"""Tests for bundled FFmpeg path resolution."""
import sys
from pathlib import Path
from unittest.mock import patch

import ffmpeg_paths


def test_resolve_media_cmd_replaces_names():
    with patch('ffmpeg_paths.get_ffmpeg_bin', return_value=r'C:\fake\ffmpeg.exe'):
        with patch('ffmpeg_paths.get_ffprobe_bin', return_value=r'C:\fake\ffprobe.exe'):
            assert ffmpeg_paths.resolve_media_cmd(['ffmpeg', '-version'])[0] == r'C:\fake\ffmpeg.exe'
            assert ffmpeg_paths.resolve_media_cmd(['ffprobe', '-version'])[0] == r'C:\fake\ffprobe.exe'
    assert ffmpeg_paths.resolve_media_cmd(['other', 'x'])[0] == 'other'


def test_staged_windows_binaries(tmp_path, monkeypatch):
    """Prefer third_party/.../bin/ffmpeg.exe when present."""
    root = tmp_path / 'proj'
    root.mkdir()
    bin_dir = root / 'third_party' / 'ffmpeg' / 'win64' / 'bin'
    bin_dir.mkdir(parents=True)
    ff = bin_dir / 'ffmpeg.exe'
    fp = bin_dir / 'ffprobe.exe'
    ff.write_bytes(b'')
    fp.write_bytes(b'')

    monkeypatch.setattr(ffmpeg_paths, '_ROOT', root)
    monkeypatch.setattr(sys, 'platform', 'win32')

    assert Path(ffmpeg_paths.get_ffmpeg_bin()) == ff
    assert Path(ffmpeg_paths.get_ffprobe_bin()) == fp
