"""export_encoder — resolve codec + family to FFmpeg encoder name."""

import pytest

from export_encoder import (
    infer_codec_and_family_from_legacy_encoder,
    resolve_export_encoder,
)


def test_infer_h264_nvenc():
    c, f = infer_codec_and_family_from_legacy_encoder('h264_nvenc')
    assert c == 'h264'
    assert f == 'nvenc'


def test_infer_libx265():
    c, f = infer_codec_and_family_from_legacy_encoder('libx265')
    assert c == 'h265'
    assert f == 'cpu'


def test_resolve_cpu_h264():
    avail = {'libx264': True, 'h264_nvenc': False}
    assert resolve_export_encoder('h264', 'cpu', avail) == 'libx264'


def test_resolve_auto_prefers_nvenc_when_available():
    avail = {'h264_nvenc': True, 'libx264': True}
    assert resolve_export_encoder('h264', 'auto', avail) == 'h264_nvenc'


def test_resolve_nvenc_falls_back_when_unavailable():
    avail = {'h264_nvenc': False, 'libx264': True}
    assert resolve_export_encoder('h264', 'nvenc', avail) == 'libx264'


@pytest.mark.parametrize('codec,family,expected', [
    ('h265', 'cpu', 'libx265'),
    ('h265', 'nvenc', 'hevc_nvenc'),
    ('av1', 'auto', 'libsvtav1'),
])
def test_resolve_matrix_with_full_hw(codec, family, expected):
    avail = {
        'libx264': True, 'libx265': True, 'libsvtav1': True,
        'h264_nvenc': True, 'hevc_nvenc': True, 'h264_amf': True, 'hevc_amf': True,
        'h264_qsv': True, 'hevc_qsv': True,
        'h264_videotoolbox': True, 'hevc_videotoolbox': True,
    }
    assert resolve_export_encoder(codec, family, avail) == expected
