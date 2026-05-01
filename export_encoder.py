"""
export_encoder.py — Map UI codec + encoder family → FFmpeg -vcodec name.

Probes FFmpeg for encoder availability (nullsrc smoke test) and resolves
``auto`` to the first working hardware encoder for the chosen codec, else CPU.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from ffmpeg_paths import get_ffmpeg_bin


def _ffmpeg_bin() -> str:
    return get_ffmpeg_bin()


def _win_flags() -> dict:
    if sys.platform != 'win32':
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {'startupinfo': si, 'creationflags': subprocess.CREATE_NO_WINDOW}


def _run_probe(enc: str) -> bool:
    ffmpeg_bin = _ffmpeg_bin()
    try:
        # NOTE: Some hardware encoders (notably NVENC HEVC on certain driver stacks)
        # can fail unless the input pixel format is explicitly set.
        # Keep this probe fast and deterministic: generate a tiny yuv420p stream
        # and encode a few frames to null.
        r = subprocess.run(
            [
                ffmpeg_bin, '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'nullsrc=s=128x128:d=0.10',
                '-pix_fmt', 'yuv420p',
                '-frames:v', '3',
                '-c:v', enc,
                '-f', 'null', '-',
            ],
            capture_output=True,
            timeout=12,
            **_win_flags(),
        )
        return r.returncode == 0
    except Exception:
        return False


# (ffmpeg_encoder_name, human label)
ENCODER_PROBE_ORDER: List[Tuple[str, str]] = [
    ('libx264', 'H.264 CPU (libx264)'),
    ('libx265', 'H.265 CPU (libx265)'),
    ('h264_nvenc', 'H.264 NVIDIA NVENC'),
    ('hevc_nvenc', 'H.265 NVIDIA NVENC'),
    ('h264_amf', 'H.264 AMD AMF'),
    ('hevc_amf', 'H.265 AMD AMF'),
    ('h264_qsv', 'H.264 Intel QSV'),
    ('hevc_qsv', 'H.265 Intel QSV'),
    ('h264_videotoolbox', 'H.264 Apple VideoToolbox'),
    ('hevc_videotoolbox', 'H.265 Apple VideoToolbox'),
    ('libsvtav1', 'AV1 CPU (libsvtav1)'),
]


def probe_encoder_availability() -> Dict[str, bool]:
    """Return {ffmpeg_encoder_name: works_with_nullsrc}."""
    return {name: _run_probe(name) for name, _ in ENCODER_PROBE_ORDER}


def infer_codec_and_family_from_legacy_encoder(enc: str) -> Tuple[str, str]:
    """Best-effort migration from single ``encoder`` config string."""
    e = (enc or 'libx264').strip().lower()
    if 'svtav1' in e or e == 'libsvtav1':
        return 'av1', 'cpu'
    if '265' in e or 'hevc' in e:
        codec = 'h265'
    elif 'av1' in e:
        codec = 'av1'
    else:
        codec = 'h264'
    if 'nvenc' in e:
        fam = 'nvenc'
    elif 'amf' in e:
        fam = 'amf'
    elif 'qsv' in e:
        fam = 'qsv'
    elif 'videotoolbox' in e:
        fam = 'videotoolbox'
    elif e.startswith('libx') or e in ('libx264', 'libx265'):
        fam = 'cpu'
    else:
        fam = 'auto'
    return codec, fam


def resolve_export_encoder(
    codec: str,
    family: str,
    available: Optional[Dict[str, bool]] = None,
) -> str:
    """
    Return FFmpeg video encoder name for export.

    *codec*: ``h264`` | ``h265`` | ``av1``
    *family*: ``auto`` | ``cpu`` | ``nvenc`` | ``amf`` | ``qsv`` | ``videotoolbox``
    """
    c = (codec or 'h264').strip().lower()
    if c in ('h.264', '264'):
        c = 'h264'
    if c in ('h.265', '265'):
        c = 'h265'
    fam = (family or 'auto').strip().lower()
    avail = available if isinstance(available, dict) else probe_encoder_availability()

    def first_avail(order: list[str], fallback: str) -> str:
        for cand in order:
            if avail.get(cand):
                return cand
        return fallback

    if c == 'av1':
        return first_avail(['libsvtav1'], 'libx264')

    if c == 'h265':
        if fam == 'cpu':
            return first_avail(['libx265'], 'libx264')
        if fam == 'nvenc':
            return first_avail(['hevc_nvenc', 'libx265'], 'libx264')
        if fam == 'amf':
            return first_avail(['hevc_amf', 'libx265'], 'libx264')
        if fam == 'qsv':
            return first_avail(['hevc_qsv', 'libx265'], 'libx264')
        if fam == 'videotoolbox':
            return first_avail(['hevc_videotoolbox', 'libx265'], 'libx264')
        return first_avail(
            ['hevc_nvenc', 'hevc_amf', 'hevc_qsv', 'hevc_videotoolbox', 'libx265'],
            'libx264',
        )

    # h264
    if fam == 'cpu':
        return first_avail(['libx264'], 'libx264')
    if fam == 'nvenc':
        return first_avail(['h264_nvenc', 'libx264'], 'libx264')
    if fam == 'amf':
        return first_avail(['h264_amf', 'libx264'], 'libx264')
    if fam == 'qsv':
        return first_avail(['h264_qsv', 'libx264'], 'libx264')
    if fam == 'videotoolbox':
        return first_avail(['h264_videotoolbox', 'libx264'], 'libx264')
    return first_avail(
        ['h264_nvenc', 'h264_amf', 'h264_qsv', 'h264_videotoolbox', 'libx264'],
        'libx264',
    )


def encoder_rows_for_ui(available: Optional[Dict[str, bool]] = None) -> list[dict]:
    """Rows for Settings / Export encoder checklist (same order as probes)."""
    avail = available if isinstance(available, dict) else probe_encoder_availability()
    return [
        {'name': n, 'label': lab, 'available': bool(avail.get(n))}
        for n, lab in ENCODER_PROBE_ORDER
    ]
