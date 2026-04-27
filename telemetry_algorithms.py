"""
telemetry_algorithms.py — shared telemetry math helpers.
"""
from __future__ import annotations

from typing import Iterable, Optional
import numpy as np

LAP_TIME_HOLD_AFTER_FINISH_S = 3.0
MAP_MAX_POINTS = 600
MAP_SMOOTH_WINDOW = 1
MAP_TIMED_SAMPLES = 240
MAP_REF_SMOOTH_WINDOW = 9


def compute_best_so_far_state(laps: Iterable) -> tuple[int, dict[int, Optional[float]], Optional[float]]:
    """Return (total_timed, best_by_lap, best_fallback) for lap scoreboard.

    best_by_lap[lap_num] stores the best completed timed-lap duration seen
    strictly before that lap started.
    """
    timed = sorted(
        [l for l in laps if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)],
        key=lambda l: getattr(l, 'lap_num', 0),
    )
    total_timed = len(timed)
    best_by_lap: dict[int, Optional[float]] = {}
    running_best: Optional[float] = None
    for lap in timed:
        lap_num = int(getattr(lap, 'lap_num', 0))
        best_by_lap[lap_num] = running_best
        dur = getattr(lap, 'duration', None)
        if dur is not None and (running_best is None or dur < running_best):
            running_best = float(dur)
    best_fallback = running_best
    return total_timed, best_by_lap, best_fallback


def lap_time_display_value(raw_lap_t: float, lap_dur: float,
                           live_lap_elapsed: float,
                           hold_s: float = LAP_TIME_HOLD_AFTER_FINISH_S) -> float:
    """Shared lap-time display policy for preview/export.

    - while on selected lap: show clamped lap timer
    - for a short window after finish: hold final lap time
    - afterwards: resume live lap_elapsed
    """
    rt = float(raw_lap_t)
    dur = float(lap_dur)
    live = float(live_lap_elapsed)
    if dur <= 0:
        return live
    if rt <= dur:
        return max(0.0, rt)
    if rt <= dur + max(0.0, float(hold_s)):
        return dur
    return live


def build_map_track(points: Iterable, max_points: int = 600,
                    smooth_window: int = 1) -> tuple[list[float], list[float]]:
    """Downsample and optionally smooth GPS track for map rendering.

    Returns (lats, lons) after:
    - regular-step downsampling to <= max_points-ish samples
    - optional moving-average smoothing with odd window >= 3
    """
    pts = list(points or [])
    if not pts:
        return [], []
    step = max(1, len(pts) // max(1, int(max_points)))
    ds = pts[::step]
    lats = []
    lons = []
    for p in ds:
        if isinstance(p, dict):
            lats.append(float(p.get('lat', 0.0)))
            lons.append(float(p.get('lon', 0.0)))
        else:
            lats.append(float(getattr(p, 'lat', 0.0)))
            lons.append(float(getattr(p, 'lon', 0.0)))
    w = int(smooth_window or 1)
    if w >= 3 and len(lats) > w:
        if w % 2 == 0:
            w += 1
        k = np.ones(w, dtype=float) / float(w)
        lats = np.convolve(np.array(lats, dtype=float), k, mode='same').tolist()
        lons = np.convolve(np.array(lons, dtype=float), k, mode='same').tolist()
    return lats, lons


def _resample_points(points: list, n_samples: int) -> tuple[np.ndarray, np.ndarray]:
    """Resample one lap polyline onto normalized distance domain."""
    if not points:
        return np.array([]), np.array([])
    lats = np.array([float(getattr(p, 'lat', 0.0)) for p in points], dtype=float)
    lons = np.array([float(getattr(p, 'lon', 0.0)) for p in points], dtype=float)
    if len(lats) < 2:
        return lats, lons
    lat_m = 111000.0
    lon_m = 111000.0 * np.cos(np.radians(float(np.mean(lats))))
    dx = np.diff(lons) * lon_m
    dy = np.diff(lats) * lat_m
    seg = np.hypot(dx, dy)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    total = float(cum[-1]) if len(cum) else 0.0
    if total <= 1e-6:
        idx_src = np.linspace(0.0, max(0.0, len(lats) - 1.0), num=len(lats))
        idx_dst = np.linspace(0.0, max(0.0, len(lats) - 1.0), num=max(2, int(n_samples)))
        return np.interp(idx_dst, idx_src, lats), np.interp(idx_dst, idx_src, lons)
    dist_u = np.linspace(0.0, total, num=max(2, int(n_samples)))
    return np.interp(dist_u, cum, lats), np.interp(dist_u, cum, lons)


def build_complete_map_track(laps: Iterable, max_points: int = 600,
                             smooth_window: int = 1,
                             timed_samples: int = 240) -> tuple[list[float], list[float]]:
    """Build full map: averaged timed-lap centerline + outlap/inlap overlays."""
    all_laps = list(laps or [])
    timed_laps = [
        l for l in all_laps
        if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)
        and len(getattr(l, 'points', []) or []) >= 2
    ]
    overlay_laps = [
        l for l in all_laps
        if (getattr(l, 'is_outlap', False) or getattr(l, 'is_inlap', False))
        and len(getattr(l, 'points', []) or []) >= 2
    ]

    merged_points: list[dict] = []

    if timed_laps:
        lat_sets = []
        lon_sets = []
        for lap in timed_laps:
            rs_lat, rs_lon = _resample_points(list(getattr(lap, 'points', []) or []), timed_samples)
            if len(rs_lat) >= 2 and len(rs_lon) >= 2:
                lat_sets.append(rs_lat)
                lon_sets.append(rs_lon)
        if lat_sets and lon_sets:
            avg_lats = np.mean(np.stack(lat_sets, axis=0), axis=0)
            avg_lons = np.mean(np.stack(lon_sets, axis=0), axis=0)
            merged_points.extend(
                {'lat': float(la), 'lon': float(lo)}
                for la, lo in zip(avg_lats.tolist(), avg_lons.tolist())
            )

    for lap in overlay_laps:
        merged_points.extend(
            {'lat': float(getattr(p, 'lat', 0.0)), 'lon': float(getattr(p, 'lon', 0.0))}
            for p in (getattr(lap, 'points', []) or [])
        )

    if not merged_points:
        fallback_points = getattr(timed_laps[0], 'points', []) if timed_laps else (
            getattr(all_laps[0], 'points', []) if all_laps else []
        )
        return build_map_track(fallback_points, max_points=max_points, smooth_window=smooth_window)

    return build_map_track(merged_points, max_points=max_points, smooth_window=smooth_window)


def build_effective_session_meta(session, info_overrides: Optional[dict] = None,
                                 weather_fetcher=None) -> dict:
    """Shared session meta composer for preview/export parity."""
    from datetime import datetime

    meta: dict = {
        'track': getattr(session, 'track', '') or '',
        'laps': '',
        'best': '',
        'best_secs': None,
        'info_track': getattr(session, 'track', '') or '',
        'info_vehicle': getattr(session, 'vehicle', '') or '',
        'info_session': getattr(session, 'session_type', '') or '',
        'info_date': '',
        'info_time': '',
        'info_weather': '',
        'info_wind': '',
    }

    laps = list(getattr(session, 'laps', []) or [])
    meta['laps'] = str(len(laps)) if laps else ''
    timed = [l for l in laps if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)]
    durs = [float(getattr(l, 'duration', 0.0) or 0.0) for l in timed if getattr(l, 'duration', None)]
    if durs:
        best = min(durs)
        meta['best_secs'] = best
        meta['best'] = f'{best:.3f}s'

    if getattr(session, 'date_utc', None):
        try:
            dt = datetime.fromisoformat(session.date_utc.replace('Z', '+00:00'))
            meta['info_date'] = dt.strftime('%Y-%m-%d')
            meta['info_time'] = dt.strftime('%H:%M')
        except Exception:
            pass
        if weather_fetcher:
            try:
                first_gps = next(
                    (p for p in (getattr(session, 'all_points', []) or [])
                     if getattr(p, 'lat', 0.0) and getattr(p, 'lon', 0.0)),
                    None
                )
                if first_gps:
                    w, wd = weather_fetcher(first_gps.lat, first_gps.lon, session.date_utc)
                    meta['info_weather'] = w or ''
                    meta['info_wind'] = wd or ''
            except Exception:
                pass

    for key, val in (info_overrides or {}).items():
        if val:
            meta[key] = val

    return meta

