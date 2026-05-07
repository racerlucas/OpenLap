"""
Map style: Circuit
==================
Classic overhead circuit map with track outline, current position dot, and start marker.

Optionally draws an OpenStreetMap circuit outline as a road-like background
when `track_map_lats` / `track_map_lons` are present in data.

ELEMENT_TYPE : "map"
Data keys    : lats, lons, cur_idx,
               track_map_lats, track_map_lons  (optional OSM geometry)
"""
STYLE_NAME   = "Circuit"
ELEMENT_TYPE = "map"

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import math


def _gps_dot_ok(data: dict) -> tuple[float, float, bool]:
    try:
        la = float(data.get('dot_lat', float('nan')))
        lo = float(data.get('dot_lon', float('nan')))
        if math.isfinite(la) and math.isfinite(lo):
            return la, lo, True
    except (TypeError, ValueError):
        pass
    return 0.0, 0.0, False


def _chaikin(xs, ys, rounds=2):
    """Chaikin corner-cutting: smooths an open polyline in-place."""
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


def render(data: dict, w: int, h: int):
    from overlay_utils import fig_to_rgba, px_to_pt

    lats    = data['lats']
    lons    = data['lons']
    cur_idx = data['cur_idx']
    dpi     = 100

    T               = data.get('_tc', {})
    map_bg          = T.get('map_bg_rgba',     (0, 0, 0, 0.65))
    track_outer     = T.get('map_track_outer', '#1a2a3a')
    track_inner     = T.get('map_track_inner', '#2255aa')
    dot_col         = T.get('map_dot',         '#ff2222')
    start_col       = T.get('map_start',       '#00ff88')

    osm_lats  = list(data.get('track_map_lats')  or [])
    osm_lons  = list(data.get('track_map_lons')  or [])
    osm_areas = list(data.get('track_map_areas') or [])
    has_osm   = bool(osm_lats and osm_lons)
    rotate_deg = float(data.get('map_rotate_deg', 0.0) or 0.0)
    mirror_x   = bool(data.get('map_mirror_x', False))
    mirror_y   = bool(data.get('map_mirror_y', False))

    def _transform_xy(xs, ys, rcx: float, rcy: float):
        if not xs or not ys:
            return xs, ys
        rad = math.radians(rotate_deg)
        cr, sr = math.cos(rad), math.sin(rad)
        ox, oy = [], []
        for x, y in zip(xs, ys):
            dx = x - rcx
            dy = y - rcy
            if mirror_x:
                dx = -dx
            if mirror_y:
                dy = -dy
            ox.append(rcx + (dx * cr - dy * sr))
            oy.append(rcy + (dx * sr + dy * cr))
        return ox, oy

    # One rotation pivot for GPS + OSM + areas (matches single bbox in preview).
    _allo = list(lons) + list(osm_lons)
    _alla = list(lats) + list(osm_lats)
    for _a in osm_areas:
        _allo.extend(_a.get('lons', []) or [])
        _alla.extend(_a.get('lats', []) or [])
    if _allo and _alla:
        rcx = (min(_allo) + max(_allo)) * 0.5
        rcy = (min(_alla) + max(_alla)) * 0.5
    else:
        rcx = (min(lons) + max(lons)) * 0.5
        rcy = (min(lats) + max(lats)) * 0.5

    lons, lats = _transform_xy(lons, lats, rcx, rcy)
    if has_osm:
        osm_lons, osm_lats = _transform_xy(osm_lons, osm_lats, rcx, rcy)
    transformed_areas = []
    for area in osm_areas:
        a_lats = area.get('lats', [])
        a_lons = area.get('lons', [])
        if a_lats and a_lons:
            tlons, tlats = _transform_xy(a_lons, a_lats, rcx, rcy)
            transformed_areas.append({'lats': tlats, 'lons': tlons})
        else:
            transformed_areas.append(area)
    osm_areas = transformed_areas

    dot_la, dot_lo, use_dot = _gps_dot_ok(data)
    if use_dot:
        _dl, _da = _transform_xy([dot_lo], [dot_la], rcx, rcy)
        dot_lo_t, dot_la_t = _dl[0], _da[0]
    else:
        dot_lo_t = dot_la_t = None
    fig, ax = plt.subplots(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    ax.set_facecolor(map_bg)

    # Draw OSM area polygons first (lowest layer)
    for area in osm_areas:
        a_lats = area.get('lats', [])
        a_lons = area.get('lons', [])
        if len(a_lats) >= 3:
            ax.fill(a_lons, a_lats, color='#4a5568', alpha=0.55, zorder=0)

    # Line widths: Canvas uses fractions of gauge width; convert pt→match px.
    lw_osm_a = px_to_pt(max(6.0, w * 0.045), dpi)
    lw_osm_b = px_to_pt(max(4.0, w * 0.028), dpi)
    lw_out   = px_to_pt(max(4.0, w * 0.030), dpi)
    lw_in    = px_to_pt(max(2.0, w * 0.015), dpi)

    # Draw OSM road background (above area fill, below GPS trace) — smoothed
    if has_osm:
        s_lons, s_lats = _chaikin(osm_lons, osm_lats)
        ax.plot(s_lons, s_lats, color='#4a5568', lw=lw_osm_a,
                solid_capstyle='round', solid_joinstyle='round', zorder=0)
        ax.plot(s_lons, s_lats, color='#2d3748', lw=lw_osm_b,
                solid_capstyle='round', solid_joinstyle='round', zorder=0)

    s_lons_gps, s_lats_gps = _chaikin(lons, lats)
    ax.plot(s_lons_gps, s_lats_gps, color=track_outer, lw=lw_out,
            solid_capstyle='round', solid_joinstyle='round', zorder=1)
    ax.plot(s_lons_gps, s_lats_gps, color=track_inner, lw=lw_in,
            solid_capstyle='round', solid_joinstyle='round', zorder=2)

    r_px = max(4.0, w * 0.025)
    ms_dot = px_to_pt(2.0 * r_px, dpi)
    mew_dot = px_to_pt(max(1.0, w * 0.006), dpi)
    if use_dot and dot_lo_t is not None:
        ax.plot(dot_lo_t, dot_la_t, 'o',
                color=dot_col, ms=max(7.0, ms_dot),
                mec='white', mew=mew_dot, zorder=6)
    elif 0 <= cur_idx < len(lats):
        ax.plot(lons[cur_idx], lats[cur_idx], 'o',
                color=dot_col, ms=max(7.0, ms_dot),
                mec='white', mew=mew_dot, zorder=6)

    ms_start = px_to_pt(2.0 * max(3.0, w * 0.020), dpi)
    ax.plot(lons[0], lats[0], 's',
            color=start_col, ms=max(5.0, ms_start * 0.85),
            mec='white', mew=px_to_pt(max(1.0, w * 0.006), dpi), zorder=5)

    # Combined bounding box so neither GPS trace nor OSM outline/areas get clipped
    area_lats = [la for a in osm_areas for la in a.get('lats', [])]
    area_lons = [lo for a in osm_areas for lo in a.get('lons', [])]
    all_lats = lats + osm_lats + area_lats
    all_lons = lons + osm_lons + area_lons
    ml  = (max(all_lats) - min(all_lats)) * 0.14 or 0.0008
    mlo = (max(all_lons) - min(all_lons)) * 0.14 or 0.0008
    ax.set_xlim(min(all_lons) - mlo, max(all_lons) + mlo)
    ax.set_ylim(min(all_lats) - ml,  max(all_lats) + ml)
    ax.set_aspect('equal')
    ax.axis('off')
    fig.tight_layout(pad=0.2)

    return fig_to_rgba(fig, (w, h))
