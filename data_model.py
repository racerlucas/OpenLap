"""
data_model.py — Shared data types for OpenLap
==============================================
DataPoint, Lap, and Session live here so that all data loaders can import
from a common module without creating circular dependencies through
racebox_data.py.

These types are the **unified data contract** for preview, export, and RPC:
loaders must populate the same fields; UI must not invent parallel telemetry shapes.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class DataPoint:
    record:      int
    time:        datetime
    lat:         float
    lon:         float
    alt:         float
    speed:       float        # km/h
    gforce_x:    float        # longitudinal G
    gforce_y:    float        # lateral G (car) or 0.0 (bike)
    gforce_z:    float        # vertical G
    lap:         int
    gyro_x:      float
    gyro_y:      float
    gyro_z:      float
    lean_angle:   float = 0.0  # degrees (positive = right lean)
    elapsed:      float = 0.0
    lap_elapsed:  float = 0.0
    rpm:          float = 0.0
    exhaust_temp: float = 0.0  # °C

    @staticmethod
    def from_row(row: dict, is_bike: bool) -> 'DataPoint':
        # Negate RaceBox sensor LeanAngle: sensor convention positive=left lean;
        # DataPoint convention is positive=right lean.
        return DataPoint(
            record     = int(row['Record']),
            time       = datetime.fromisoformat(row['Time'].replace('Z', '+00:00')),
            lat        = float(row['Latitude']),
            lon        = float(row['Longitude']),
            alt        = float(row['Altitude']),
            speed      = float(row['Speed']),
            gforce_x   = float(row['GForceX']),
            gforce_y   = 0.0 if is_bike else float(row.get('GForceY', 0.0)),
            gforce_z   = float(row['GForceZ']),
            lap        = int(row['Lap']),
            gyro_x     = float(row['GyroX']),
            gyro_y     = float(row['GyroY']),
            gyro_z     = float(row['GyroZ']),
            lean_angle = -float(row.get('LeanAngle', 0.0)) if is_bike else 0.0,
        )


@dataclass
class Lap:
    lap_num:   int
    points:    List[DataPoint]
    duration:  float
    is_outlap: bool = False
    is_inlap:  bool = False
    # Start/finish crossing (session seconds + WGS84) — same geometry used for
    # lap duration; enables delta_time.compute_lap_profile to anchor (0,0) at SF.
    crossing_start_elapsed: Optional[float] = None
    crossing_end_elapsed: Optional[float] = None
    sf_entry_lat: Optional[float] = None
    sf_entry_lon: Optional[float] = None
    sf_exit_lat: Optional[float] = None
    sf_exit_lon: Optional[float] = None

    @property
    def elapsed_start(self) -> float:
        return self.points[0].elapsed if self.points else 0.0

    @property
    def elapsed_end(self) -> float:
        return self.points[-1].elapsed if self.points else 0.0

    @property
    def max_speed(self) -> float:
        return max((p.speed for p in self.points), default=0.0)

    @property
    def max_lat_g(self) -> float:
        return max((abs(p.gforce_y) for p in self.points), default=0.0)

    @property
    def max_lon_g(self) -> float:
        return max((abs(p.gforce_x) for p in self.points), default=0.0)

    @property
    def max_lean(self) -> float:
        return max((abs(p.lean_angle) for p in self.points), default=0.0)

    def format_duration(self) -> str:
        m, s = int(self.duration // 60), self.duration % 60
        return f"{m}:{s:06.3f}"


@dataclass
class Session:
    source:        str
    date_utc:      str
    track:         str
    configuration: str
    session_type:  str
    best_lap_time: float
    all_points:    List[DataPoint]
    laps:          List[Lap]
    is_bike:       bool = False
    csv_path:      str  = ''

    @property
    def start_time(self) -> Optional[datetime]:
        return self.all_points[0].time if self.all_points else None

    @property
    def end_time(self) -> Optional[datetime]:
        return self.all_points[-1].time if self.all_points else None

    @property
    def timed_laps(self) -> List[Lap]:
        return [l for l in self.laps if not l.is_outlap and not l.is_inlap]

    @property
    def fastest_lap(self) -> Optional[Lap]:
        timed = self.timed_laps
        return min(timed, key=lambda l: l.duration) if timed else None

    def lap_by_num(self, n: int) -> Optional[Lap]:
        return next((l for l in self.laps if l.lap_num == n), None)

    def interpolate_at(self, elapsed: float) -> Optional[DataPoint]:
        pts = self.all_points
        if not pts or elapsed < pts[0].elapsed or elapsed > pts[-1].elapsed:
            return None
        lo, hi = 0, len(pts) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if pts[mid].elapsed <= elapsed:
                lo = mid
            else:
                hi = mid
        p0, p1 = pts[lo], pts[hi]
        dt = p1.elapsed - p0.elapsed
        if dt == 0:
            return p0
        a = (elapsed - p0.elapsed) / dt
        L = lambda attr: getattr(p0, attr) + (getattr(p1, attr) - getattr(p0, attr)) * a
        lap_num = p0.lap
        lap_elapsed = L('lap_elapsed')
        if p1.lap != p0.lap:
            cross_elapsed = estimate_lap_crossing_elapsed(p0, p1)
            if elapsed >= cross_elapsed:
                lap_num = p1.lap
                lap_elapsed = max(0.0, elapsed - cross_elapsed)
            else:
                lap_num = p0.lap
                lap_elapsed = max(0.0, p0.lap_elapsed + (elapsed - p0.elapsed))
        return DataPoint(
            record=p0.record, time=p0.time,
            lat=L('lat'), lon=L('lon'), alt=L('alt'), speed=L('speed'),
            gforce_x=L('gforce_x'), gforce_y=L('gforce_y'), gforce_z=L('gforce_z'),
            lap=lap_num, gyro_x=L('gyro_x'), gyro_y=L('gyro_y'), gyro_z=L('gyro_z'),
            lean_angle=L('lean_angle'), elapsed=elapsed, lap_elapsed=lap_elapsed,
            rpm=L('rpm'), exhaust_temp=L('exhaust_temp'),
        )


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def estimate_lap_crossing_elapsed(prev_pt: DataPoint, curr_pt: DataPoint) -> float:
    """Estimate start/finish crossing time between two consecutive points.

    By default we treat the first sample in the new lap as the boundary; when
    the first point carries a positive ``lap_elapsed`` we back-project it and
    clamp to the segment.
    """
    lo = float(prev_pt.elapsed)
    hi = float(curr_pt.elapsed)
    if hi <= lo:
        return lo
    est = (lo + hi) * 0.5
    if float(curr_pt.lap_elapsed) > 0.0:
        est = float(curr_pt.elapsed) - float(curr_pt.lap_elapsed)
    return _clamp(est, lo, hi)


def _interp_latlon_at_elapsed(prev_pt: DataPoint, next_pt: DataPoint, t_cross: float) -> tuple[float, float]:
    """Linear lat/lon at ``t_cross`` between two samples (for SF crossing)."""
    lo, hi = float(prev_pt.elapsed), float(next_pt.elapsed)
    if hi <= lo + 1e-12:
        a = 0.0
    else:
        a = (float(t_cross) - lo) / (hi - lo)
    a = max(0.0, min(1.0, a))
    la = float(prev_pt.lat) + a * (float(next_pt.lat) - float(prev_pt.lat))
    ln = float(prev_pt.lon) + a * (float(next_pt.lon) - float(prev_pt.lon))
    return la, ln


def build_laps_from_points(
    all_points: List[DataPoint],
    *,
    outlap_lap_num: Optional[int] = 0,
) -> List[Lap]:
    """Rebuild laps with interpolated lap boundaries and consistent lap_elapsed."""
    if not all_points:
        return []

    buckets: Dict[int, List[DataPoint]] = {}
    first_idx_by_lap: Dict[int, int] = {}
    order: List[int] = []
    for idx, pt in enumerate(all_points):
        if pt.lap not in buckets:
            buckets[pt.lap] = []
            first_idx_by_lap[pt.lap] = idx
            order.append(pt.lap)
        buckets[pt.lap].append(pt)

    starts: Dict[int, float] = {}
    first_lap = order[0]
    starts[first_lap] = float(buckets[first_lap][0].elapsed)

    for i in range(1, len(order)):
        lap_num = order[i]
        first_pt = buckets[lap_num][0]
        prev_idx = max(0, first_idx_by_lap[lap_num] - 1)
        prev_pt = all_points[prev_idx]
        starts[lap_num] = estimate_lap_crossing_elapsed(prev_pt, first_pt)

    laps: List[Lap] = []
    for i, lap_num in enumerate(order):
        pts = buckets[lap_num]
        start = starts[lap_num]
        first_pt = pts[0]
        prev_idx = max(0, first_idx_by_lap[lap_num] - 1)
        prev_pt = all_points[prev_idx]

        if i == 0 or prev_pt is first_pt:
            entry_lat, entry_lon = float(first_pt.lat), float(first_pt.lon)
        else:
            entry_lat, entry_lon = _interp_latlon_at_elapsed(prev_pt, first_pt, start)

        if i + 1 < len(order):
            end = starts[order[i + 1]]
            last_pt = pts[-1]
            next_first = buckets[order[i + 1]][0]
            exit_lat, exit_lon = _interp_latlon_at_elapsed(last_pt, next_first, end)
        else:
            end = float(pts[-1].elapsed)
            exit_lat, exit_lon = float(pts[-1].lat), float(pts[-1].lon)

        lap_elapsed_origin = float(pts[0].elapsed)
        for pt in pts:
            # Keep per-lap samples anchored at 0 on the first recorded point.
            # Lap *duration* still uses interpolated crossing boundaries.
            pt.lap_elapsed = max(0.0, float(pt.elapsed) - lap_elapsed_origin)
        if i + 1 < len(order):
            dur = max(0.0, end - start)
        else:
            dur = max(0.0, float(pts[-1].elapsed) - start)
        laps.append(Lap(
            lap_num=lap_num,
            points=pts,
            duration=dur,
            is_outlap=(outlap_lap_num is not None and lap_num == outlap_lap_num),
            crossing_start_elapsed=start,
            crossing_end_elapsed=end,
            sf_entry_lat=entry_lat,
            sf_entry_lon=entry_lon,
            sf_exit_lat=exit_lat,
            sf_exit_lon=exit_lon,
        ))
    return laps
