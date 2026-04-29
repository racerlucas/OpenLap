"""
Process-wide OpenCV + FFmpeg settings.

On Windows, default libavcodec frame threading can assert when VideoCapture seeks
rapidly (libavcodec/pthread_frame.c async_lock). Auto-sync subprocess already uses
``ffmpeg -threads 1``; this aligns the OpenCV capture backend.
"""
from __future__ import annotations

import os
import sys


def apply_ffmpeg_capture_thread_limit() -> None:
    if sys.platform != 'win32':
        return
    key = 'OPENCV_FFMPEG_CAPTURE_OPTIONS'
    extra = 'threads;1'
    cur = (os.environ.get(key) or '').strip()
    if not cur:
        os.environ[key] = extra
        return
    low = cur.lower()
    if 'threads;' in low:
        return
    os.environ[key] = f'{cur}|{extra}'
