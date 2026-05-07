"""
overlay_paint_plan.py — Shared overlay iteration + JSON plan for Canvas export
==============================================================================
``iter_overlay_layers`` mirrors the gauge loop in ``overlay_worker`` so
Matplotlib export and headless Canvas export consume the **same** per-gauge
data dicts (numbers, history, map geometry).
"""
from __future__ import annotations
import math
from typing import Any, Dict, Iterator, List, Tuple

from gauge_channels import (
    GAUGE_CHANNELS,
    MULTI_CHANNEL,
    build_multi_data,
    gauge_data,
    gauge_data_lap_info,
    _ema_smooth,
)


def _inject_theme_name(d: dict, theme: str) -> None:
    """Editor gauges, Canvas export, and Matplotlib ``style_registry`` all use ``theme``."""
    d['theme'] = theme


def unpack_overlay_worker_args(args: tuple) -> Dict[str, Any]:
    (frame_bytes, shape, cur_pt_idx,
     lap_lats, lap_lons,
     history, ref_history, lap_duration,
     vw, vh,
     show_map, show_telemetry,
     is_bike,
     overlay_layout,
     max_speed,
     sectors,
     *_extra) = args
    return {
        'frame_bytes':      frame_bytes,
        'shape':            shape,
        'cur_pt_idx':       cur_pt_idx,
        'lap_lats':         lap_lats,
        'lap_lons':         lap_lons,
        'history':          history,
        'ref_history':      ref_history,
        'lap_duration':     lap_duration,
        'vw':               vw,
        'vh':               vh,
        'show_map':         show_map,
        'show_telemetry':   show_telemetry,
        'is_bike':          is_bike,
        'overlay_layout':   overlay_layout,
        'max_speed':        max_speed,
        'sectors':          sectors,
        'session_meta':     _extra[0] if len(_extra) > 0 else {},
        'ref_lats':         _extra[1] if len(_extra) > 1 else [],
        'ref_lons':         _extra[2] if len(_extra) > 2 else [],
        'ref_duration':     _extra[3] if len(_extra) > 3 else 0.0,
        'overlay_only':     _extra[4] if len(_extra) > 4 else False,
        'track_map_lats':   _extra[5] if len(_extra) > 5 else [],
        'track_map_lons':   _extra[6] if len(_extra) > 6 else [],
        'track_map_areas':  _extra[7] if len(_extra) > 7 else [],
    }


def iter_overlay_layers(args: tuple) -> Iterator[Tuple[str, str, int, int, int, int, dict]]:
    """Yield ``(element_type, style, gx, gy, gw, gh, data_dict)`` for each drawable layer."""
    from overlay_worker import default_layout

    u = unpack_overlay_worker_args(args)
    vw, vh = int(u['vw']), int(u['vh'])
    layout = u['overlay_layout'] or default_layout()
    theme = layout.get('theme', 'Dark')
    lap_lats, lap_lons = u['lap_lats'], u['lap_lons']
    history, ref_history = u['history'], u['ref_history']
    cur_pt_idx = u['cur_pt_idx']
    show_map, show_telemetry = u['show_map'], u['show_telemetry']
    is_bike, max_speed, sectors = u['is_bike'], u['max_speed'], u['sectors']
    lap_duration = u['lap_duration']
    session_meta = u['session_meta']
    ref_lats, ref_lons, ref_duration = u['ref_lats'], u['ref_lons'], u['ref_duration']
    track_map_lats = u['track_map_lats']
    track_map_lons = u['track_map_lons']
    track_map_areas = u['track_map_areas']

    for g in layout.get('gauges', []):
        if not g.get('visible', True):
            continue
        channel = g.get('channel', 'speed')
        style = g.get('style', 'Numeric')
        gx = int(g.get('x', 0.0) * vw)
        gy = int(g.get('y', 0.0) * vh)
        gw = max(32, int(g.get('w', 0.12) * vw))
        gh = max(24, int(g.get('h', 0.20) * vh))

        if channel == 'info':
            gd = dict(session_meta)
            for k, v in (g.get('info_overrides') or {}).items():
                if v and not gd.get(f'info_{k}'):
                    gd[f'info_{k}'] = v
            gd['selected_fields'] = g.get('selected_fields') or g.get('channels') or []
            _inject_theme_name(gd, theme)
            yield ('gauge', style, gx, gy, gw, gh, gd)
            continue

        if channel == 'lap_info':
            gd = gauge_data_lap_info(history)
            gd['selected_fields'] = g.get('selected_fields') or ['lap', 'best', 'current', 'delta']
            gd['best_mode'] = g.get('best_mode', 'so_far')
            _inject_theme_name(gd, theme)
            yield ('gauge', style, gx, gy, gw, gh, gd)
            continue

        if channel == 'image':
            gd = {
                'image_path': g.get('image_path', ''),
                'opacity':    float(g.get('opacity', 1.0)),
                'fit':        g.get('fit', 'contain'),
            }
            _inject_theme_name(gd, theme)
            yield ('gauge', style, gx, gy, gw, gh, gd)
            continue

        if channel == 'map':
            if not (show_map and lap_lats):
                continue
            if ref_lats:
                if ref_duration > 0 and history:
                    cur_elapsed = history[-1].get('t', 0.0)
                    ref_frac = min(1.0, max(0.0, cur_elapsed / ref_duration))
                    ref_cur_idx = int(ref_frac * max(0, len(ref_lats) - 1))
                else:
                    ref_cur_idx = int(cur_pt_idx / max(1, len(lap_lats) - 1)
                                      * max(0, len(ref_lats) - 1))
            else:
                ref_cur_idx = 0
            osm_on = g.get('track_map_enabled', True)
            data = {
                'lats': lap_lats, 'lons': lap_lons, 'cur_idx': cur_pt_idx,
                'zoom_radius_m':  g.get('zoom_radius_m', 150),
                'show_ref':       g.get('show_ref', True),
                'map_rotate_deg': g.get('map_rotate_deg', 0),
                'map_mirror_x':   g.get('map_mirror_x', False),
                'map_mirror_y':   g.get('map_mirror_y', False),
                'ref_lats':       ref_lats,
                'ref_lons':       ref_lons,
                'ref_cur_idx':    ref_cur_idx,
                'track_map_lats':  track_map_lats if osm_on else [],
                'track_map_lons':  track_map_lons if osm_on else [],
                'track_map_areas': track_map_areas if osm_on else [],
            }
            if history:
                try:
                    last = history[-1]
                    dlat = float(last.get('lat', float('nan')))
                    dlon = float(last.get('lon', float('nan')))
                    if math.isfinite(dlat) and math.isfinite(dlon):
                        data['dot_lat'] = dlat
                        data['dot_lon'] = dlon
                except (TypeError, ValueError):
                    pass
            _inject_theme_name(data, theme)
            mgw, mgh = max(60, gw), max(60, gh)
            yield ('map', style, gx, gy, mgw, mgh, data)
        elif show_telemetry and history:
            if channel == MULTI_CHANNEL:
                sub_channels = g.get('multi_channels') or g.get('channels') or []
                if not sub_channels:
                    continue
                gd = build_multi_data(sub_channels, history,
                                      ref_history if ref_history else [])
                _inject_theme_name(gd, theme)
            else:
                gd = gauge_data(channel, history)
                gd['lap_duration'] = lap_duration
                gd['is_bike'] = is_bike
                _inject_theme_name(gd, theme)
                cur_elapsed = history[-1].get('t', 0.0) if history else 0.0
                gd['sectors'] = [
                    {**s, 'done': s['done'] and s.get('boundary_elapsed', float('inf')) <= cur_elapsed}
                    for s in sectors
                ]
                if channel == 'delta_time' and style == 'Delta Bar':
                    for _k in ('delta_bar_full_scale', 'delta_bar_curve'):
                        if _k in g:
                            gd[_k] = g[_k]
                if channel == 'speed':
                    gd['max_val'] = max_speed
                if ref_history:
                    hk = GAUGE_CHANNELS.get(channel, GAUGE_CHANNELS['speed'])['hist_key']
                    ref_vals = [p.get(hk, 0.0) for p in ref_history]
                    if channel in ('gforce_total', 'gforce_lat', 'gforce_lon', 'g_meter'):
                        ref_vals = _ema_smooth(ref_vals)
                    gd['ref_history_vals'] = ref_vals
                if channel == 'g_meter':
                    gy_hist = [p.get('gy', 0.0) for p in history]
                    gy_hist = _ema_smooth(gy_hist)
                    gd['history_gy'] = gy_hist
                    gd['value_gy'] = gy_hist[-1] if gy_hist else 0.0
            yield ('gauge', style, gx, gy, gw, gh, gd)


def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, int) and not isinstance(obj, bool):
        return int(obj)
    if hasattr(obj, 'tolist'):
        try:
            return _json_safe(obj.tolist())
        except Exception:
            pass
    if hasattr(obj, 'item'):
        try:
            v = obj.item()
            return _json_safe(v)
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return str(obj)


def build_canvas_paint_plan(args: tuple) -> dict:
    """JSON-serialisable plan for the Node ``canvas_export`` renderer."""
    u = unpack_overlay_worker_args(args)
    layers: List[dict] = []
    for el_type, style, gx, gy, gw, gh, d in iter_overlay_layers(args):
        layers.append({
            'el': el_type, 'style': style,
            'x': gx, 'y': gy, 'w': gw, 'h': gh,
            'data': _json_safe(d),
        })
    return {
        'vw': int(u['vw']),
        'vh': int(u['vh']),
        'overlay_only': bool(u['overlay_only']),
        'layers': layers,
    }
