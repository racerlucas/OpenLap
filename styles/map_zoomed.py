"""
Map style: Zoomed
=================
Centred on the current GPS position with a configurable radius (metres).
Optionally renders the reference-lap trace in purple.
Optionally draws an OpenStreetMap circuit outline as a road-like background.

ELEMENT_TYPE : "map"
Data keys    : lats, lons, cur_idx,
               zoom_radius_m  (default 150),
               show_ref       (default False),
               ref_lats, ref_lons  (reference-lap GPS arrays, may be empty),
               track_map_lats, track_map_lons  (optional OSM geometry)
"""
STYLE_NAME   = "Zoomed"
ELEMENT_TYPE = "map"

import math
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def _chaikin(xs, ys, rounds=2):
    for _ in range(rounds):
        nxs = [xs[0]]
        nys = [ys[0]]
        for i in range(len(xs) - 1):
            nxs.extend([0.75 * xs[i] + 0.25 * xs[i + 1],
                         0.25 * xs[i] + 0.75 * xs[i + 1]])
            nys.extend([0.75 * ys[i] + 0.25 * ys[i + 1],
                         0.25 * ys[i] + 0.75 * ys[i + 1]])
        nxs.append(xs[-1])
        nys.append(ys[-1])
        xs, ys = nxs, nys
    return xs, ys


def _gps_to_local(lats, lons, center_lat, center_lon):
    """Convert lat/lon sequences to local (x, y) in metres."""
    lat_m = 111000.0
    lon_m = 111000.0 * math.cos(math.radians(center_lat))
    x = [(lo - center_lon) * lon_m for lo in lons]
    y = [(la - center_lat) * lat_m for la in lats]
    return x, y


def _transform_xy(xs, ys, rotate_deg=0.0, mirror_x=False, mirror_y=False,
                  center_x=None, center_y=None):
    if not xs or not ys:
        return xs, ys
    cx = float(center_x) if center_x is not None else (min(xs) + max(xs)) * 0.5
    cy = float(center_y) if center_y is not None else (min(ys) + max(ys)) * 0.5
    rad = math.radians(rotate_deg)
    cr, sr = math.cos(rad), math.sin(rad)
    ox, oy = [], []
    for x, y in zip(xs, ys):
        dx = x - cx
        dy = y - cy
        if mirror_x:
            dx = -dx
        if mirror_y:
            dy = -dy
        ox.append(cx + (dx * cr - dy * sr))
        oy.append(cy + (dx * sr + dy * cr))
    return ox, oy


def render(data: dict, w: int, h: int):
    import numpy as np
    from overlay_utils import fig_to_rgba, px_to_pt
    from styles.map_circuit import _gps_dot_ok

    lats        = data.get('lats', [])
    lons        = data.get('lons', [])
    cur_idx     = int(data.get('cur_idx', 0))
    radius      = max(10.0, float(data.get('zoom_radius_m', 150)))
    show_ref    = bool(data.get('show_ref', True))
    ref_lats    = data.get('ref_lats', [])
    ref_lons    = data.get('ref_lons', [])
    ref_cur_idx = int(data.get('ref_cur_idx', 0))

    T            = data.get('_tc', {})
    map_bg       = T.get('map_bg_rgba',     (0, 0, 0, 0.65))
    track_outer  = T.get('map_track_outer', '#1a2a3a')
    track_inner  = T.get('map_track_inner', '#2255aa')
    dot_col      = T.get('map_dot',         '#ff2222')
    start_col    = T.get('map_start',       '#00ff88')
    ref_col      = '#cc44ff'

    osm_lats  = list(data.get('track_map_lats')  or [])
    osm_lons  = list(data.get('track_map_lons')  or [])
    osm_areas = list(data.get('track_map_areas') or [])
    rotate_deg = float(data.get('map_rotate_deg', 0.0) or 0.0)
    mirror_x   = bool(data.get('map_mirror_x', False))
    mirror_y   = bool(data.get('map_mirror_y', False))

    if not lats or len(lats) < 2:
        from styles.map_circuit import render as _circuit
        return _circuit(data, w, h)

    safe_idx = max(0, min(cur_idx, len(lats) - 1))
    dot_la, dot_lo, use_dot = _gps_dot_ok(data)
    if use_dot:
        center_lat, center_lon = dot_la, dot_lo
    else:
        center_lat, center_lon = lats[safe_idx], lons[safe_idx]

    x, y        = _gps_to_local(lats, lons, center_lat, center_lon)
    # Keep zoomed map locked on the current position (0,0), matching frontend.
    x, y        = _transform_xy(x, y, rotate_deg, mirror_x, mirror_y, center_x=0.0, center_y=0.0)

    dpi = 100
    lw_osm_a = px_to_pt(max(6.0, w * 0.045), dpi)
    lw_osm_b = px_to_pt(max(4.0, w * 0.028), dpi)
    lw_out   = px_to_pt(max(4.0, w * 0.030), dpi)
    lw_in    = px_to_pt(max(2.0, w * 0.015), dpi)
    lw_ref   = px_to_pt(max(2.0, w * 0.013), dpi)

    fig, ax = plt.subplots(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    ax.set_facecolor(map_bg)

    # Draw OSM area polygons first (lowest layer)
    for area in osm_areas:
        a_lats = area.get('lats', [])
        a_lons = area.get('lons', [])
        if len(a_lats) >= 3:
            ox_a, oy_a = _gps_to_local(a_lats, a_lons, center_lat, center_lon)
            ox_a, oy_a = _transform_xy(
                ox_a, oy_a, rotate_deg, mirror_x, mirror_y, center_x=0.0, center_y=0.0
            )
            ax.fill(ox_a, oy_a, color='#4a5568', alpha=0.55, zorder=0)

    # Draw OSM road background (below GPS trace) — smoothed
    if osm_lats and osm_lons:
        ox, oy = _gps_to_local(osm_lats, osm_lons, center_lat, center_lon)
        ox, oy = _transform_xy(ox, oy, rotate_deg, mirror_x, mirror_y, center_x=0.0, center_y=0.0)
        sx, sy = _chaikin(ox, oy)
        ax.plot(sx, sy, color='#4a5568', lw=lw_osm_a,
                solid_capstyle='round', solid_joinstyle='round', zorder=0)
        ax.plot(sx, sy, color='#2d3748', lw=lw_osm_b,
                solid_capstyle='round', solid_joinstyle='round', zorder=0)

    # Full track outline
    sx, sy = _chaikin(x, y)
    ax.plot(sx, sy, color=track_outer, lw=lw_out,
            solid_capstyle='round', solid_joinstyle='round', zorder=1)
    ax.plot(sx, sy, color=track_inner, lw=lw_in,
            solid_capstyle='round', solid_joinstyle='round', zorder=2)

    # Reference lap trace + reference dot
    if show_ref and ref_lats and len(ref_lats) >= 2:
        rx, ry = _gps_to_local(ref_lats, ref_lons, center_lat, center_lon)
        rx, ry = _transform_xy(rx, ry, rotate_deg, mirror_x, mirror_y, center_x=0.0, center_y=0.0)
        rsx, rsy = _chaikin(rx, ry)
        ax.plot(rsx, rsy, color=ref_col, lw=lw_ref, alpha=0.80,
                solid_capstyle='round', solid_joinstyle='round', zorder=3)
        safe_ref_idx = max(0, min(ref_cur_idx, len(ref_lats) - 1))
        ms_ref = px_to_pt(2.0 * max(3.0, w * 0.022), dpi)
        ax.plot(rx[safe_ref_idx], ry[safe_ref_idx], 'o',
                color=ref_col, ms=max(5.0, ms_ref),
                mec='white', mew=px_to_pt(max(1.0, w * 0.006), dpi), zorder=6)

    # Start marker
    ms_start = px_to_pt(2.0 * max(3.0, w * 0.020), dpi)
    ax.plot(x[0], y[0], 's', color=start_col,
            ms=max(5.0, ms_start * 0.85), mec='white',
            mew=px_to_pt(max(1.0, w * 0.006), dpi), zorder=5)

    # Current position dot (origin when centred on telemetry GPS)
    lat_m_c = 111000.0
    lon_m_c = 111000.0 * math.cos(math.radians(center_lat))
    if use_dot:
        ddx = (float(dot_lo) - center_lon) * lon_m_c
        ddy = (float(dot_la) - center_lat) * lat_m_c
        ddx, ddy = _transform_xy([ddx], [ddy], rotate_deg, mirror_x, mirror_y, center_x=0.0, center_y=0.0)
        px_dot, py_dot = ddx[0], ddy[0]
    else:
        px_dot, py_dot = x[safe_idx], y[safe_idx]
    ms_cur = px_to_pt(2.0 * max(4.0, w * 0.028), dpi)
    ax.plot(px_dot, py_dot, 'o',
            color=dot_col, ms=max(7.0, ms_cur), mec='white',
            mew=px_to_pt(max(1.0, w * 0.007), dpi), zorder=7)

    ax.set_xlim(-radius, radius)
    ax.set_ylim(-radius, radius)
    ax.set_aspect('equal')
    ax.axis('off')

    fig.tight_layout(pad=0.2)
    rgba = fig_to_rgba(fig, (w, h))

    # Radial feather/fade: fade to transparent near the edges
    cy_px, cx_px = h / 2.0, w / 2.0
    ys = np.arange(h, dtype=np.float32) - cy_px
    xs = np.arange(w, dtype=np.float32) - cx_px
    dist = np.sqrt(xs[np.newaxis, :] ** 2 + ys[:, np.newaxis] ** 2)
    inner_r = min(w, h) * 0.32
    outer_r = min(w, h) * 0.50
    fade = np.clip((outer_r - dist) / max(1.0, outer_r - inner_r), 0.0, 1.0)
    rgba = rgba.copy()
    rgba[:, :, 3] = (rgba[:, :, 3].astype(np.float32) * fade).astype(np.uint8)

    return rgba
