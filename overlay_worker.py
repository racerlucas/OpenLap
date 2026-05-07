"""
overlay_worker.py — Overlay rendering entry point (export frames)
==================================================================
Rendering is delegated to style plugins in ``styles/``. This module owns
blend_rgba, default_layout, and the multiprocessing worker.

**Data:** ``gauge_channels.gauge_data`` + history rows from the same pipeline as
preview (see ``telemetry_algorithms`` / ``webview_api``). **Presentation:**
matplotlib styles are independent of ``frontend/js/gauges/*.js`` — match *keys*
and numeric values, not pixel layout.
"""
from __future__ import annotations
import logging
from typing import Tuple
from overlay_utils import blend_rgba, blend_rgba_onto_rgba, scale_factor

logger = logging.getLogger(__name__)

# Prefer the same UI stack as the editor preview so ``fontsize`` pt matches Canvas px.
try:
    import matplotlib as _mpl
    _sans = ['Segoe UI', 'Microsoft YaHei UI', 'PingFang SC', 'Noto Sans CJK SC',
             'DejaVu Sans', 'Bitstream Vera Sans', 'sans-serif']
    _mpl.rcParams['font.sans-serif'] = _sans
    _mpl.rcParams['font.family'] = 'sans-serif'
except Exception:
    pass

def default_layout() -> dict:
    """Return a default overlay layout dict (used when no config is present)."""
    return {
        'is_bike': False,
        'theme':   'Dark',
        'gauges': [
            {'channel': 'map',        'style': 'Circuit', 'visible': True, 'x': 0.74, 'y': 0.02, 'w': 0.24, 'h': 0.30},
            {'channel': 'speed',      'style': 'Dial',    'visible': True, 'x': 0.01, 'y': 0.74, 'w': 0.13, 'h': 0.23},
            {'channel': 'gforce_lat', 'style': 'Bar',     'visible': True, 'x': 0.15, 'y': 0.74, 'w': 0.10, 'h': 0.23},
            {'channel': 'gforce_lon', 'style': 'Bar',     'visible': True, 'x': 0.26, 'y': 0.74, 'w': 0.10, 'h': 0.23},
            {'channel': 'lap_time',   'style': 'Numeric', 'visible': True, 'x': 0.37, 'y': 0.74, 'w': 0.13, 'h': 0.23},
        ],
    }


def render_overlay_matplotlib(args: Tuple) -> bytes:
    """
    Multiprocessing worker: renders overlay onto one video frame (Matplotlib).

    args = (frame_bytes, shape, cur_pt_idx,
            lap_lats, lap_lons,
            history,        # list of {t, speed, gx, gy, lean, rpm, exhaust_temp, delta_time}
            ref_history,    # list of same shape for reference lap, or []
            lap_duration,
            vw, vh,
            show_map, show_telemetry,
            is_bike,
            overlay_layout, # dict — see default_layout()
            max_speed,      # float — session max speed rounded up +10%
            sectors)        # list of pre-computed sector dicts, or []
    """
    import numpy as np
    from style_registry import render_style
    from overlay_paint_plan import iter_overlay_layers, unpack_overlay_worker_args

    u = unpack_overlay_worker_args(args)
    vw, vh = int(u['vw']), int(u['vh'])
    overlay_only = bool(u['overlay_only'])
    frame_bytes, shape = u['frame_bytes'], u['shape']

    if overlay_only:
        frame  = np.zeros((vh, vw, 4), dtype=np.uint8)
        _blend = blend_rgba_onto_rgba
    else:
        frame  = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(shape).copy()
        _blend = blend_rgba

    # ── Gauges and map (shared iteration with Canvas export in overlay_paint_plan)
    for el_type, style, gx, gy, gw, gh, data in iter_overlay_layers(args):
        try:
            img = render_style(el_type, style, data, gw, gh)
            _blend(frame, img, gx, gy)
        except Exception as e:
            logger.debug('Failed to render %s/%s: %s', el_type, style, e)

    return frame.tobytes()


# Video export uses ``overlay_dispatch.render_frame_worker`` (Canvas only).
# ``render_overlay_matplotlib`` remains for style plugin / matplotlib tooling.
