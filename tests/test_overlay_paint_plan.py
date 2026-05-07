"""Smoke tests for shared Matplotlib / Canvas overlay iteration."""
import numpy as np

from overlay_paint_plan import build_canvas_paint_plan, iter_overlay_layers, unpack_overlay_worker_args


def _minimal_args():
    h, w, c = 72, 128, 3
    frame = np.zeros((h, w, c), dtype=np.uint8)
    lap_t = [51.0 + i * 0.0001 for i in range(8)]
    lon_t = [4.0 + i * 0.0001 for i in range(8)]
    hist = [{'t': 0.1 * i, 'speed': 50.0 + i, 'gx': 0.1, 'gy': 0.2, 'lean': 5.0,
             'rpm': 8000.0, 'exhaust_temp': 400.0, 'delta_time': 0.0, 'lap_time': 0.0,
             'lat': lap_t[i], 'lon': lon_t[i]} for i in range(8)]
    layout = {
        'theme': 'Dark',
        'gauges': [
            {'channel': 'speed', 'style': 'Numeric', 'visible': True,
             'x': 0.02, 'y': 0.02, 'w': 0.15, 'h': 0.12},
        ],
    }
    return (
        frame.tobytes(),
        (h, w, c),
        3,
        lap_t,
        lon_t,
        hist,
        [],
        90.0,
        w,
        h,
        True,
        True,
        False,
        layout,
        200.0,
        [],
        {},
        [],
        [],
        0.0,
        False,
        [],
        [],
        [],
    )


def test_unpack_overlay_worker_args():
    u = unpack_overlay_worker_args(_minimal_args())
    assert u['vw'] == 128 and u['vh'] == 72
    assert u['show_map'] is True


def test_iter_overlay_layers_yields_numeric():
    specs = list(iter_overlay_layers(_minimal_args()))
    assert len(specs) == 1
    el, style, gx, gy, gw, gh, d = specs[0]
    assert el == 'gauge' and style == 'Numeric'
    assert gw >= 32 and gh >= 24
    assert 'speed' in d or d.get('channel') == 'speed'


def test_build_canvas_paint_plan_json_safe():
    plan = build_canvas_paint_plan(_minimal_args())
    assert plan['vw'] == 128 and plan['vh'] == 72
    assert len(plan['layers']) == 1
    import json
    json.dumps(plan)
