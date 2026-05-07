"""
overlay_dispatch.py — Video overlay rasterisation (Canvas only)
================================================================
Export frames use the same bundled ``frontend/js/gauges/*.js`` as the overlay
editor (``canvas_export/`` + Node + ``@napi-rs/canvas``). There is **no**
Matplotlib fallback for video overlay.

``args`` may still end with a legacy ``'matplotlib'`` / ``'canvas'`` suffix from
older callers — it is stripped before rendering.

When the first argument is ``('_shm', name, offset, nbytes)`` (from
``video_renderer`` multiprocessing export), workers attach shared memory and
materialise bytes once — avoids pickling multi‑MiB frame blobs per frame.
"""
from __future__ import annotations
import logging
from multiprocessing import shared_memory
from typing import Tuple

logger = logging.getLogger(__name__)


def _strip_legacy_engine_suffix(args: Tuple) -> Tuple:
    if args and isinstance(args[-1], str):
        tail = args[-1].strip().lower()
        if tail in ('matplotlib', 'canvas'):
            return args[:-1]
    return args


def _materialize_shm_frame_slot(slot0) -> object:
    """Turn ``('_shm', name, offset, nbytes)`` into plain ``bytes``; pass-through otherwise."""
    if (
        isinstance(slot0, tuple)
        and len(slot0) == 4
        and slot0[0] == '_shm'
    ):
        _, name, off, nbytes = slot0
        shm = shared_memory.SharedMemory(name=name)
        try:
            return bytes(memoryview(shm.buf)[off : off + int(nbytes)])
        finally:
            shm.close()
    return slot0


def _resolve_shm_worker_args(args: Tuple) -> Tuple:
    args = _strip_legacy_engine_suffix(args)
    if not args:
        return args
    slot0 = _materialize_shm_frame_slot(args[0])
    if slot0 is args[0]:
        return args
    return (slot0,) + args[1:]


def render_frame_worker(args: Tuple) -> bytes:
    """Render one frame's overlay into packed BGR or RGBA bytes (pool entry point)."""
    from overlay_canvas_worker import render_overlay_canvas
    return render_overlay_canvas(_resolve_shm_worker_args(args))
