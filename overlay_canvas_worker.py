"""
overlay_canvas_worker.py — Headless Canvas overlay export (only path)
======================================================================
Uses ``canvas_export/render_server.cjs`` + ``gauge_bundle.cjs`` (same
``frontend/js/gauges/*.js`` as the editor) and ``overlay_paint_plan``.

**Requirements:** A ``node`` runtime (portable: ``Library/node/node.exe``; dev:
``third_party/node/win64/node.exe`` from ``python tools/fetch_node.py``, or
``node`` on PATH), ``npm install`` in ``canvas_export/`` at **build** time, and
``python tools/bundle_canvas_gauges.py`` after editing gauge sources.

There is no Matplotlib fallback — :func:`assert_canvas_export_ready` runs at
export start so failures are immediate and explicit.
"""
from __future__ import annotations
import json
import logging
import os
import struct
import subprocess
import threading
from typing import Any, Dict, Optional, Tuple

import numpy as np

from node_paths import get_node_exe
from overlay_paint_plan import build_canvas_paint_plan, unpack_overlay_worker_args
from utils import _win_flags

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANVAS_DIR = os.path.join(_HERE, 'canvas_export')
_SERVER = os.path.join(_CANVAS_DIR, 'render_server.cjs')
_BUNDLE = os.path.join(_CANVAS_DIR, 'gauge_bundle.cjs')
_NODE_MODULES = os.path.join(_CANVAS_DIR, 'node_modules', '@napi-rs', 'canvas')


def assert_canvas_export_ready() -> None:
    """Raise ``RuntimeError`` if Canvas export cannot run (no silent fallback)."""
    node = get_node_exe()
    if not node:
        raise RuntimeError(
            'OpenLap 视频叠加导出需要 Node 运行时：便携版应在 OpenLap.exe 同目录下包含 '
            'Library/node/node.exe（请用 python tools/build_windows_portable.py 完整打包；'
            '或先 python tools/fetch_node.py，再 pyinstaller 后运行 tools/stage_dist_library_node.py）。'
            '开发环境可安装 Node 加入 PATH，或设置环境变量 OPENLAP_NODE。'
            '另需在构建前于 canvas_export 目录执行 npm install。'
        )
    if not os.path.isfile(_SERVER):
        raise RuntimeError(f'缺少 {_SERVER!r}')
    if not os.path.isfile(_BUNDLE):
        raise RuntimeError(
            f'缺少 {_BUNDLE!r} — 请运行: python tools/bundle_canvas_gauges.py'
        )
    if not os.path.isdir(_NODE_MODULES):
        raise RuntimeError(
            '未安装 canvas_export 依赖 — 在仓库内执行: cd canvas_export && npm install'
        )


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise EOFError(f'expected {n} bytes, got {len(buf)}')
        buf.extend(chunk)
    return bytes(buf)


def _blend_full_rgba_onto_bgr_video(dst_bgr: np.ndarray, src_rgba: np.ndarray) -> None:
    """Alpha-composite full-frame ``src_rgba`` (straight RGBA) onto ``dst_bgr`` (H×W×3 BGR).

    Avoid allocating several full-frame float32 planes at once (4K × 3 workers easily
    OOMs); blend in horizontal strips so peak RAM is a few MiB per strip.
    """
    h, _w = dst_bgr.shape[:2]
    a_u8 = src_rgba[:, :, 3]
    src_bgr_u8 = src_rgba[:, :, :3][:, :, ::-1]
    strip = 96
    inv_scale = 1.0 / 255.0
    for y0 in range(0, h, strip):
        y1 = min(h, y0 + strip)
        sl = slice(y0, y1)
        a = (a_u8[sl].astype(np.float32) * inv_scale)[:, :, np.newaxis]
        om = 1.0 - a
        acc = dst_bgr[sl].astype(np.float32)
        acc *= om
        acc += src_bgr_u8[sl].astype(np.float32) * a
        dst_bgr[sl] = np.clip(acc, 0, 255).astype(np.uint8)


class _NodeCanvasServer:
    """One long-lived Node subprocess per Python worker process."""

    __slots__ = ('_proc', '_lock')

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _ensure(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None
        if self._proc is not None:
            return self._proc
        assert_canvas_export_ready()
        node = get_node_exe()
        assert node
        kw: Dict[str, Any] = dict(
            cwd=_CANVAS_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        kw.update(_win_flags())
        self._proc = subprocess.Popen([node, os.path.basename(_SERVER)], **kw)
        return self._proc

    def render_rgba(self, plan: dict) -> np.ndarray:
        line = json.dumps(plan, separators=(',', ':')).encode('utf-8')
        if len(line) > 48 * 1024 * 1024:
            raise ValueError('canvas paint plan exceeds 48 MiB (JSON line too large)')
        with self._lock:
            proc = self._ensure()
            assert proc.stdin and proc.stdout
            proc.stdin.write(line + b'\n')
            proc.stdin.flush()
            hdr = _read_exact(proc.stdout, 12)
            w, h, nbytes = struct.unpack('<III', hdr)
            if w <= 0 or h <= 0 or nbytes != w * h * 4:
                raise ValueError(f'invalid canvas response header: w={w} h={h} nbytes={nbytes}')
            raw = _read_exact(proc.stdout, nbytes)
        return np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4)).copy()


_srv: Optional[_NodeCanvasServer] = None
_srv_lock = threading.Lock()


def _server_singleton() -> _NodeCanvasServer:
    global _srv
    with _srv_lock:
        if _srv is None:
            _srv = _NodeCanvasServer()
        return _srv


def render_overlay_canvas(args: Tuple) -> bytes:
    u = unpack_overlay_worker_args(args)
    plan = build_canvas_paint_plan(args)
    rgba = _server_singleton().render_rgba(plan)

    if u['overlay_only']:
        return rgba.tobytes()

    frame = np.frombuffer(u['frame_bytes'], dtype=np.uint8).reshape(u['shape']).copy()
    _blend_full_rgba_onto_bgr_video(frame, rgba)
    return frame.tobytes()
