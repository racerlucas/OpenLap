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
    from overlay_utils import fig_to_rgba

    lats    = data['lats']
    lons    = data['lons']
    cur_idx = data['cur_idx']

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

    def _transform_xy(xs, ys):
        if not xs or not ys:
            return xs, ys
        cx = (min(xs) + max(xs)) * 0.5
        cy = (min(ys) + max(ys)) * 0.5
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

    lons, lats = _transform_xy(lons, lats)
    if has_osm:
        osm_lons, osm_lats = _transform_xy(osm_lons, osm_lats)
    transformed_areas = []
    for area in osm_areas:
        a_lats = area.get('lats', [])
        a_lons = area.get('lons', [])
        if a_lats and a_lons:
            tlons, tlats = _transform_xy(a_lons, a_lats)
            transformed_areas.append({'lats': tlats, 'lons': tlons})
        else:
            transformed_areas.append(area)
    osm_areas = transformed_areas

    dpi = 100
    fig, ax = plt.subplots(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    ax.set_facecolor(map_bg)

    # Draw OSM area polygons first (lowest layer)
    for area in osm_areas:
        a_lats = area.get('lats', [])
        a_lons = area.get('lons', [])
        if len(a_lats) >= 3:
            ax.fill(a_lons, a_lats, color='#4a5568', alpha=0.55, zorder=0)

    # Draw OSM road background (above area fill, below GPS trace) — smoothed
    if has_osm:
        s_lons, s_lats = _chaikin(osm_lons, osm_lats)
        ax.plot(s_lons, s_lats, color='#4a5568', lw=9.0,
                solid_capstyle='round', solid_joinstyle='round', zorder=0)
        ax.plot(s_lons, s_lats, color='#2d3748', lw=5.5,
                solid_capstyle='round', solid_joinstyle='round', zorder=0)

    s_lons_gps, s_lats_gps = _chaikin(lons, lats)
    ax.plot(s_lons_gps, s_lats_gps, color=track_outer, lw=5.0,
            solid_capstyle='round', solid_joinstyle='round', zorder=1)
    ax.plot(s_lons_gps, s_lats_gps, color=track_inner, lw=2.5,
            solid_capstyle='round', solid_joinstyle='round', zorder=2)

    if 0 <= cur_idx < len(lats):
        ax.plot(lons[cur_idx], lats[cur_idx], 'o',
                color=dot_col, ms=max(7, min(w, h) // 30),
                mec='white', mew=1.8, zorder=6)

    ax.plot(lons[0], lats[0], 's',
            color=start_col, ms=max(5, min(w, h) // 40),
            mec='white', mew=1.2, zorder=5)

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
