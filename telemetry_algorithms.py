"""
telemetry_algorithms.py — Shared telemetry math for preview and export.

**Unify here:** lap timing display, lap-info / best-so-far state, G-meter smoothing
hooks, map polyline builders, per-preview delta series, ``build_history_row`` field
names — anything where a drift between Data-tab / overlay-editor preview and
``video_renderer`` export would be a user-visible bug.

**Do not unify:** JS Canvas drawing, matplotlib style code, RPC wiring — keep
those separate unless they change the inputs or formulas above.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional, Any

import numpy as np

logger = logging.getLogger(__name__)

LAP_TIME_HOLD_AFTER_FINISH_S = 3.0
MAP_MAX_POINTS = 600
MAP_SMOOTH_WINDOW = 1
MAP_TIMED_SAMPLES = 240
MAP_REF_SMOOTH_WINDOW = 9
# Out/in-lap points farther than this from timed-lap centerline are kept (pit / alternate paths).
MAP_OVERLAY_MIN_SEP_M = 38.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (WGS84 sphere)."""
    r = 6371000.0
    p1 = np.radians(lat1)
    p2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dl = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2.0) ** 2
    return float(2.0 * r * np.arcsin(np.sqrt(min(1.0, max(0.0, a)))))


def _min_dist_point_to_polyline_m(lat: float, lon: float, plat: np.ndarray, plon: np.ndarray) -> float:
    """Min distance from one WGS84 point to a dense polyline (vertex sampling)."""
    if plat.size < 1 or plon.size < 1:
        return 1e9
    d = np.array(
        [_haversine_m(lat, lon, float(plat[i]), float(plon[i])) for i in range(min(plat.size, plon.size))],
        dtype=float,
    )
    return float(np.min(d)) if d.size else 1e9


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


def lap_at_session_elapsed(laps: Iterable, elapsed: float):
    """Return the Lap whose point time span contains ``elapsed``, else None."""
    e = float(elapsed)
    for lap in laps:
        pts = getattr(lap, 'points', None) or []
        if len(pts) < 1:
            continue
        lo = float(pts[0].elapsed)
        hi = float(pts[-1].elapsed)
        if lo - 1e-4 <= e <= hi + 1e-4:
            return lap
    return None


def lap_index_at_session_elapsed(laps: Iterable, elapsed: float) -> Optional[int]:
    """Return index into ``laps`` for the Lap whose elapsed span contains ``elapsed``."""
    e = float(elapsed)
    for i, lap in enumerate(laps or []):
        pts = getattr(lap, 'points', None) or []
        if len(pts) < 1:
            continue
        lo = float(pts[0].elapsed)
        hi = float(pts[-1].elapsed)
        if lo - 1e-4 <= e <= hi + 1e-4:
            return int(i)
    return None


def lap_index_for_lap_num(laps: Iterable, lap_num: int) -> Optional[int]:
    """First lap row index whose ``lap_num`` matches (aligns with :meth:`Session.interpolate_at` ``p.lap``)."""
    want = int(lap_num)
    for i, lap in enumerate(laps or []):
        if int(getattr(lap, 'lap_num', 0)) == want:
            return int(i)
    return None


def best_so_far_timed_lap_before_index(sess, current_lap_idx: Optional[int]):
    """Fastest timed lap among segments strictly before ``current_lap_idx`` (preview/export parity).

    Same rule as ``reference_resolver.resolve_reference_lap(..., session_best_so_far)``
    when ``current_lap_idx`` is valid: only laps with index ``< current_lap_idx``,
    excluding outlap/inlap.
    """
    if current_lap_idx is None:
        return None
    idx = int(current_lap_idx)
    if idx < 0:
        return None
    laps = getattr(sess, 'laps', None) or []
    if idx >= len(laps):
        return None
    prev_rows = laps[:idx]
    prev = [l for l in prev_rows if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)]
    if not prev:
        return None
    return min(prev, key=lambda l: l.duration)


def sample_session_points(session, start_elapsed: float, end_elapsed: float, sample_hz: float = 60.0):
    """Sample session telemetry via ``Session.interpolate_at`` on a fixed time grid."""
    if not session or not getattr(session, 'all_points', None):
        return []
    t0 = float(start_elapsed)
    t1 = max(t0, float(end_elapsed))
    hz = max(1.0, float(sample_hz))
    dt = 1.0 / hz
    n_steps = max(0, int((t1 - t0) * hz))
    sample_times = [t0 + i * dt for i in range(n_steps + 1)]
    if not sample_times or sample_times[-1] < t1:
        sample_times.append(t1)
    out = []
    for sess_abs in sample_times:
        p = session.interpolate_at(sess_abs)
        if p is not None:
            out.append((float(sess_abs), p))
    return out


def channel_fields_from_datapoint(p) -> dict[str, Any]:
    """Scalar telemetry fields shared by history rows and single-lap preview lists."""
    return {
        'speed':        float(getattr(p, 'speed', 0.0)),
        'gx':           float(getattr(p, 'gforce_x', 0.0)),
        'gy':           float(getattr(p, 'gforce_y', 0.0)),
        'rpm':          float(getattr(p, 'rpm', 0.0) or 0.0),
        'exhaust_temp': float(getattr(p, 'exhaust_temp', 0.0) or 0.0),
        'alt':          float(getattr(p, 'alt', 0.0) or 0.0),
        'lat':          float(getattr(p, 'lat', 0.0) or 0.0),
        'lon':          float(getattr(p, 'lon', 0.0) or 0.0),
        'lean':         float(getattr(p, 'lean_angle', 0.0) or 0.0),
    }


def apply_g_meter_smoothing_inplace(points: list) -> None:
    """Mutate rows in-place: add gx_s, gy_s, g_total, g_total_s (same recipe as gauge preview/export)."""
    from gauge_channels import _ema_smooth

    if not points:
        return
    gx_s = _ema_smooth([p['gx'] for p in points])
    gy_s = _ema_smooth([p['gy'] for p in points])
    g_total_raw = [((p['gx'] ** 2 + p['gy'] ** 2) ** 0.5) for p in points]
    g_total_s = _ema_smooth(g_total_raw)
    for i, p in enumerate(points):
        p['gx_s'] = gx_s[i] if i < len(gx_s) else p['gx']
        p['gy_s'] = gy_s[i] if i < len(gy_s) else p['gy']
        p['g_total'] = g_total_raw[i] if i < len(g_total_raw) else 0.0
        p['g_total_s'] = g_total_s[i] if i < len(g_total_s) else p['g_total']


def compute_preview_delta_series(
    cur_sess,
    samples: list,
    ref_lap,
    dynamic_so_far: bool,
) -> list:
    """One delta value per ``samples`` row (same order as :func:`sample_session_points` output).

    Returns JSON-friendly floats; uses ``None`` where delta is undefined (no ref or bad profile).
    """
    from delta_time import compute_lap_profile, make_delta_fn

    lap_profiles: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    lap_delta_fns: dict[tuple[int, int], object] = {}
    lap_by_num = {int(getattr(l, 'lap_num', 0)): l for l in (getattr(cur_sess, 'laps', None) or [])}

    def _profile_for_lap_num(lap_num: int):
        if lap_num in lap_profiles:
            return lap_profiles[lap_num]
        lap_obj = lap_by_num.get(int(lap_num))
        if lap_obj is None:
            lap_profiles[lap_num] = (np.array([]), np.array([]))
            return lap_profiles[lap_num]
        t_arr, d_arr = compute_lap_profile(lap_obj)
        lap_profiles[lap_num] = (t_arr, d_arr)
        return t_arr, d_arr

    def _delta_fn_for_lap_num(lap_num: int, ref_obj):
        if ref_obj is None:
            return None
        ref_num = int(getattr(ref_obj, 'lap_num', 0))
        key = (int(lap_num), ref_num)
        if key in lap_delta_fns:
            return lap_delta_fns[key]
        lap_obj = lap_by_num.get(int(lap_num))
        if lap_obj is None:
            lap_delta_fns[key] = None
            return None
        lap_delta_fns[key] = make_delta_fn(ref_obj, current_lap_duration=lap_obj.duration)
        return lap_delta_fns[key]

    laps_list = getattr(cur_sess, 'laps', None) or []
    last_valid = 0.0
    out: list = []
    n_none_no_lap_idx = 0
    n_none_no_active_ref = 0
    n_none_bad_profile = 0
    n_ok = 0
    for sess_abs, p in samples:
        try:
            idx = lap_index_for_lap_num(laps_list, int(getattr(p, 'lap', 0)))
            if idx is None:
                idx = lap_index_at_session_elapsed(laps_list, float(sess_abs))
            if idx is None:
                n_none_no_lap_idx += 1
                out.append(None)
                continue
            lap_obj = laps_list[idx]
            lap_num = int(getattr(lap_obj, 'lap_num', 0))

            active_ref = best_so_far_timed_lap_before_index(cur_sess, idx) if dynamic_so_far else ref_lap

            if active_ref is None:
                n_none_no_active_ref += 1
                out.append(None)
                continue
            t_arr, d_arr = _profile_for_lap_num(lap_num)
            delta_fn = _delta_fn_for_lap_num(lap_num, active_ref)
            if delta_fn is None or len(t_arr) < 2 or len(d_arr) < 2:
                n_none_bad_profile += 1
                out.append(None)
                continue
            lap_elapsed = float(p.lap_elapsed)
            cur_dist = float(np.interp(lap_elapsed, t_arr, d_arr))
            if not np.isfinite(cur_dist):
                cur_dist = 0.0
            dv = float(delta_fn(lap_elapsed, cur_dist))
            v = float(dv) if np.isfinite(dv) else 0.0
            last_valid = v
            n_ok += 1
            out.append(v)
        except Exception:
            out.append(last_valid)
    logger.info(
        '[delta_preview] compute_preview_delta_series: samples=%d ok=%d '
        'none_no_lap_idx=%d none_no_active_ref=%d none_bad_profile_or_fn=%d dynamic_so_far=%s ref_lap_num=%s',
        len(samples),
        n_ok,
        n_none_no_lap_idx,
        n_none_no_active_ref,
        n_none_bad_profile,
        dynamic_so_far,
        int(getattr(ref_lap, 'lap_num', 0)) if ref_lap is not None else None,
    )
    return out


def lap_display_num_for_point(laps: Iterable, elapsed: float, raw_lap_num: int) -> int:
    """Lap scoreboard display number: outlap → 0 (OUT LAP), else device lap number."""
    # Strong hint from source lap counter: if this lap_num is tagged outlap,
    # always present it as Lap 0.
    for lap in laps:
        if int(getattr(lap, 'lap_num', 0)) == int(raw_lap_num) and getattr(lap, 'is_outlap', False):
            return 0
    lap = lap_at_session_elapsed(laps, elapsed)
    if lap is not None and getattr(lap, 'is_outlap', False):
        return 0
    return int(raw_lap_num)


def build_lap_info_lookup(laps: Iterable) -> dict[str, Any]:
    """Build shared lap-info lookup state for preview/export parity.

    Returns dict with:
    - display_by_lap_num: lap_num -> display lap index (outlap=0, then 1..N excluding outlap)
    - best_so_far_by_lap_num: lap_num -> best timed-lap duration strictly before this lap
    - total_display_laps: denominator shown in lap info (exclude outlap, keep inlap)
    - session_best: best timed-lap duration in this session
    """
    lap_list = list(laps or [])
    _, best_by_lap, best_fallback = compute_best_so_far_state(lap_list)
    total_display = max(1, len([l for l in lap_list if not getattr(l, 'is_outlap', False)]))

    display_by_lap_num: dict[int, int] = {}
    best_so_far_by_lap_num: dict[int, Optional[float]] = {}
    run_best: Optional[float] = None
    disp_counter = 0
    for lap_row in lap_list:
        ln = int(getattr(lap_row, 'lap_num', 0))
        if getattr(lap_row, 'is_outlap', False):
            display_by_lap_num[ln] = 0
        else:
            disp_counter += 1
            display_by_lap_num[ln] = disp_counter
        best_so_far_by_lap_num[ln] = run_best
        if (not getattr(lap_row, 'is_outlap', False)) and (not getattr(lap_row, 'is_inlap', False)):
            dur = getattr(lap_row, 'duration', None)
            if dur is not None:
                d = float(dur)
                if run_best is None or d < run_best:
                    run_best = d

    return {
        'display_by_lap_num': display_by_lap_num,
        'best_so_far_by_lap_num': best_so_far_by_lap_num,
        'best_by_lap': best_by_lap,
        'total_display_laps': total_display,
        'session_best': best_fallback,
    }


def lap_info_fields_for_sample(
    laps: Iterable,
    _sample_elapsed: float,
    raw_lap_num: int,
    lookup: dict[str, Any],
) -> dict[str, Any]:
    """Resolve lap-info display fields for one telemetry sample."""
    # Prefer the per-sample lap counter from ``Session.interpolate_at`` (crossing-aware).
    # ``lap_at_session_elapsed`` uses raw point time spans and can match the wrong lap
    # when spans overlap or leave gaps vs the interpolated grid.
    active_lap_num = int(raw_lap_num)
    display_by = lookup.get('display_by_lap_num', {})
    sofar_by = lookup.get('best_so_far_by_lap_num', {})
    best_by = lookup.get('best_by_lap', {})
    session_best = lookup.get('session_best')
    return {
        'li_lap_num': int(display_by.get(active_lap_num, raw_lap_num)),
        'li_total_laps': int(lookup.get('total_display_laps', 1)),
        'li_best_so_far': sofar_by.get(active_lap_num, best_by.get(active_lap_num, session_best)),
        'li_session_best': session_best,
    }


def build_history_row(
    *,
    p,
    lap_t: float,
    delta_time: Optional[float],
    lap_info: dict[str, Any],
) -> dict[str, Any]:
    """Shared history row builder used by preview + export gauge pipelines."""
    d = channel_fields_from_datapoint(p)
    d['t'] = float(lap_t)
    d['delta_time'] = (float(delta_time) if delta_time is not None else None)
    d['li_lap_num'] = lap_info.get('li_lap_num', 1)
    d['li_total_laps'] = lap_info.get('li_total_laps', 1)
    d['li_best_so_far'] = lap_info.get('li_best_so_far')
    d['li_session_best'] = lap_info.get('li_session_best')
    return d


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


def lap_preview_t0_session_elapsed(lap) -> float:
    """Session elapsed at preview ``sess_rel=0`` — prefer crossing start so ``sess_rel`` matches ``Lap.duration``."""
    cs = getattr(lap, 'crossing_start_elapsed', None)
    if cs is not None:
        return float(cs)
    pts = getattr(lap, 'points', None) or []
    return float(pts[0].elapsed) if pts else 0.0


def lap_duration_for_timer_hold(lap) -> float:
    """Duration for ``lap_time_display_value``; fall back when ``Lap.duration`` is zero or inconsistent."""
    d = float(getattr(lap, 'duration', 0.0) or 0.0)
    if d > 1e-6:
        return d
    cs = getattr(lap, 'crossing_start_elapsed', None)
    ce = getattr(lap, 'crossing_end_elapsed', None)
    if cs is not None and ce is not None and float(ce) > float(cs) + 1e-9:
        return max(0.0, float(ce) - float(cs))
    pts = getattr(lap, 'points', None) or []
    if len(pts) >= 2:
        return max(0.0, float(pts[-1].elapsed) - float(pts[0].elapsed))
    return 0.0


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


def _fit_centerline_from_laps(resampled_laps: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    """Fit a centerline by nearest-point projection onto each lap polyline.

    The first lap acts as the reference polyline. For each reference point, we
    find the nearest point on every other lap polyline, then take the *median*
    of those projected points per station. This fits a robust centerline from
    multiple laps (curves), not merely averaging same-index samples.
    """
    if not resampled_laps:
        return np.array([]), np.array([])
    if len(resampled_laps) == 1:
        return resampled_laps[0]

    ref_lat, ref_lon = resampled_laps[0]
    n = len(ref_lat)
    if n < 2:
        return ref_lat, ref_lon

    lat_contrib = [np.array(ref_lat, dtype=float)]
    lon_contrib = [np.array(ref_lon, dtype=float)]

    for lap_lat, lap_lon in resampled_laps[1:]:
        if len(lap_lat) < 2:
            continue
        # (n, m) squared distances between ref points and one lap polyline.
        d2 = (ref_lat[:, None] - lap_lat[None, :]) ** 2 + (ref_lon[:, None] - lap_lon[None, :]) ** 2
        nn_idx = np.argmin(d2, axis=1)
        lat_contrib.append(lap_lat[nn_idx])
        lon_contrib.append(lap_lon[nn_idx])

    # Robust centerline: median across per-lap projected points.
    lat_stack = np.stack(lat_contrib, axis=0)
    lon_stack = np.stack(lon_contrib, axis=0)
    return np.median(lat_stack, axis=0), np.median(lon_stack, axis=0)


def build_complete_map_track(laps: Iterable, max_points: int = 600,
                             smooth_window: int = 1,
                             timed_samples: int = 240) -> tuple[list[float], list[float]]:
    """Build full map: median timed-lap centerline + filtered out/in-lap overlay.

    When no laps are flagged as timed (e.g. all out/in), fall back to fitting
    from *all* laps with GPS so the line-based median still runs instead of
    degenerating to a single raw trace.

    Out/in-lap raw GPS is not concatenated wholesale (that re-creates a spaghetti
    overlay on top of the centerline). Only points that deviate from the
    centerline by ``MAP_OVERLAY_MIN_SEP_M`` metres are kept (pit lanes, alternate
    paths), after light resampling.
    """
    all_laps = list(laps or [])

    def _usable(lap) -> bool:
        return len(getattr(lap, 'points', []) or []) >= 2

    strict_timed = [
        l for l in all_laps
        if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)
        and _usable(l)
    ]
    if strict_timed:
        timed_laps = strict_timed
        overlay_laps = [
            l for l in all_laps
            if (getattr(l, 'is_outlap', False) or getattr(l, 'is_inlap', False))
            and _usable(l)
        ]
    else:
        # No timed flags: fit centerline from every lap with GPS (do not also
        # append overlay from the same set — that would double-draw spaghetti).
        timed_laps = [l for l in all_laps if _usable(l)]
        overlay_laps = []

    merged_points: list[dict] = []
    center_lat = np.array([])
    center_lon = np.array([])

    if timed_laps:
        resampled_laps: list[tuple[np.ndarray, np.ndarray]] = []
        ref_lat = None
        ref_lon = None
        for lap in timed_laps:
            rs_lat, rs_lon = _resample_points(list(getattr(lap, 'points', []) or []), timed_samples)
            if len(rs_lat) >= 2 and len(rs_lon) >= 2:
                if ref_lat is None:
                    ref_lat, ref_lon = rs_lat, rs_lon
                    resampled_laps.append((rs_lat, rs_lon))
                else:
                    # Align each lap to reference start phase for stability, then
                    # perform line-based center fitting via nearest projections.
                    d2_start = (rs_lat - ref_lat[0]) ** 2 + (rs_lon - ref_lon[0]) ** 2
                    k = int(np.argmin(d2_start))
                    if k > 0:
                        rs_lat = np.roll(rs_lat, -k)
                        rs_lon = np.roll(rs_lon, -k)
                    resampled_laps.append((rs_lat, rs_lon))
        if resampled_laps:
            avg_lats, avg_lons = _fit_centerline_from_laps(resampled_laps)
            center_lat = np.array(avg_lats, dtype=float)
            center_lon = np.array(avg_lons, dtype=float)
            merged_points.extend(
                {'lat': float(la), 'lon': float(lo)}
                for la, lo in zip(avg_lats.tolist(), avg_lons.tolist())
            )

    # Append pit / off-line segments only where they diverge from the centerline.
    if center_lat.size >= 2 and overlay_laps:
        sep = float(MAP_OVERLAY_MIN_SEP_M)
        for lap in overlay_laps:
            rs_lat, rs_lon = _resample_points(list(getattr(lap, 'points', []) or []), max(32, timed_samples // 3))
            for la, lo in zip(rs_lat.tolist(), rs_lon.tolist()):
                if _min_dist_point_to_polyline_m(float(la), float(lo), center_lat, center_lon) >= sep:
                    merged_points.append({'lat': float(la), 'lon': float(lo)})

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

